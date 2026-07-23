#!/usr/bin/env python3
"""Integration tests for the news-bot prep + distributed-publish path.

After Wave 7 (llm-transcreation-and-distributed-publishing Task 11) the
following integration scenarios are exercised end-to-end against a
tempfile SQLite DB with external network calls mocked:

* Prep phase: ``news_bot.job()`` stages accepted entries into
  ``pending_articles`` and makes zero Telegraph / Telegram calls during
  staging (Decision 10 of manual-review-workflow).
* Outage state machine: API-level Claude failures advance the
  ``bot_state`` rows (started_at, ping_count) and route the next slot
  through Google. Per-article Claude failures do NOT advance state.
  Recovery clears ``bot_state`` and emits a switch-back ping. State
  persists across simulated container restarts (proves it lives in
  ``bot_state``, not in-memory).
* Container-restart mid-window: a fresh ``job()`` call with a frozen
  ``datetime.now`` at 16:00 MSK pulls today's slots from
  ``compute_publish_slots`` for the leftover window (≥40-min interval).
* Manual-review preemption: when an operator publishes a pending row
  between fetch and the next slot, the bot skips that row on the next
  slot via the empty-queue / list_pending guard.
* Crash-loop guard (Decision 9): a recent ``MAX(published_at)`` makes
  ``job()`` sleep ~35min before continuing.

Patch-target convention (Task 7 import):
``news_bot.transcreate_via_claude`` is the pinned target for Claude
mocks. Patching ``claude_transcreation.transcreate_via_claude`` would
NOT take effect because ``news_bot`` imports the function via
``from claude_transcreation import transcreate_via_claude`` and holds a
local module-level reference. Where Google-fallback per-article path is
exercised, ``news_bot.transcreate_text`` is also patched.

DB_FILE patching contract (verified for outage-state tests):
``outage_state._connect()`` reads ``news_bot.DB_FILE`` at call time
(``import news_bot; sqlite3.connect(news_bot.DB_FILE)``), so a single
``patch('news_bot.DB_FILE', tempfile_path)`` reaches both ``news_bot``
and ``outage_state``.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz

import news_bot
import outage_state
import pending_articles_repo
from claude_transcreation import ClaudeOutageError, ClaudeTranscreationError


MSK = pytz.timezone("Europe/Moscow")
UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Shared scaffolding — tempfile DB + token/channel patches.
# ---------------------------------------------------------------------------


class _IntegrationBase(unittest.TestCase):
    """Tempfile DB + safe env-var stubs, shared by every test class below."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        # ``patch('news_bot.DB_FILE', ...)`` reaches outage_state too —
        # outage_state._connect() reads ``news_bot.DB_FILE`` at call time.
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()
        # Silence admin notifications by default. Individual tests stop
        # this patch and introspect the mock when they care.
        self.notify_patcher = patch('news_bot.send_admin_notification')
        self.mock_notify = self.notify_patcher.start()
        # Mattel source returns nothing unless a test overrides it.
        self.mattel_patcher = patch('news_bot.fetch_mattel_news', return_value=[])
        self.mattel_patcher.start()
        # Orangetrack source returns nothing unless a test overrides it
        # (avoids real network calls to orangetrackdiecast.com). Patch
        # SOURCES to a narrower list — patching the function attribute
        # alone doesn't work because SOURCES holds the original reference.
        self.sources_patcher = patch(
            'news_bot.SOURCES',
            [news_bot._fetch_rss_entries, news_bot._fetch_mattel_entries],
        )
        self.sources_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        self.notify_patcher.stop()
        self.mattel_patcher.stop()
        self.sources_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)


class _PrepPhaseBase(_IntegrationBase):
    """Prep-phase tests neuter the distributed-publish loop and crash-loop
    sleep so ``news_bot.job()`` returns after the staging phase. Without
    these patches the publish loop would actually try to publish (or
    block on slot waits).
    """

    def setUp(self):
        super().setUp()
        # Task 8's distributed-publish loop sleeps until each slot. For
        # prep-only invariants that's irrelevant — neuter sleep entirely.
        self.sleep_patcher = patch('news_bot.time.sleep')
        self.sleep_patcher.start()
        # Make sure the publish loop is a no-op for prep-only tests so we
        # only assert what landed in pending_articles.
        self.fallback_patcher = patch('news_bot._fallback_publish')
        self.fallback_patcher.start()

    def tearDown(self):
        self.fallback_patcher.stop()
        self.sleep_patcher.stop()
        super().tearDown()


# ---------------------------------------------------------------------------
# Prep-phase tests (existing coverage — keep green after Wave 7).
# ---------------------------------------------------------------------------


class TestIntegration(_PrepPhaseBase):

    def _create_mock_entry(self, link, title='Test Article', published='2025-01-01'):
        return {
            'link': link,
            'title': title,
            'published': published,
            'summary': 'Summary',
        }

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_via_claude')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_full_pipeline_with_multiple_feeds(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        """Prep-phase stages every accepted entry into ``pending_articles``
        and makes zero Telegraph / Telegram calls. Claude is also NOT
        called during the staging phase — only during the publish loop,
        which is mocked out here via ``_PrepPhaseBase``.
        """
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [
                self._create_mock_entry('http://example.com/article1'),
                self._create_mock_entry('http://example.com/article2'),
            ],
            [self._create_mock_entry('http://example.com/article3')],
        ]
        mock_fetch_article.return_value = {
            'title': 'Article Title',
            'subtitle': 'Editorial lead',
            'paragraphs': ['First paragraph.', 'Second paragraph.'],
            'images': ['http://example.com/image.jpg'],
        }
        mock_transcreate.side_effect = AssertionError(
            "transcreate_via_claude must NOT fire in the staging phase"
        )
        mock_publish.return_value = 'https://telegra.ph/page'
        mock_send_teaser.return_value = True

        news_bot.job()

        mock_load_feeds.assert_called_once()
        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 3)

        # Prep-phase invariants (Decision 10).
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()
        # Claude must NOT fire in the prep phase — publish loop is
        # neutered in _PrepPhaseBase, so any call here is a regression.
        mock_transcreate.assert_not_called()

        # Every article landed in pending_articles.
        self.assertEqual(pending_articles_repo.count_pending(), 3)
        rows = pending_articles_repo.list_pending()
        staged_links = {r['link'] for r in rows}
        self.assertEqual(staged_links, {
            'http://example.com/article1',
            'http://example.com/article2',
            'http://example.com/article3',
        })
        # EN title / subtitle / paragraphs copied through — no GT applied.
        row = rows[0]
        self.assertEqual(row['title'], 'Article Title')
        self.assertEqual(row['subtitle'], 'Editorial lead')
        self.assertEqual(row['paragraphs'], ['First paragraph.', 'Second paragraph.'])

        # processed_news is untouched in prep-phase — dedup moves to
        # ``published_articles`` via the operator flow, not the cron tick.
        conn = sqlite3.connect(self.db_path)
        processed = conn.execute('SELECT link FROM processed_news').fetchall()
        conn.close()
        self.assertEqual(processed, [])

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_via_claude')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_duplicate_skipping(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        """A link that appears on two feeds inside one tick lands in
        pending exactly once — via the PRIMARY KEY UNIQUE guard."""
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [self._create_mock_entry('http://example.com/article1')],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': []
        }

        news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 1)
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_via_claude')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_error_isolation(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        """An empty feed doesn't abort the second one — staged rows
        come only from the successful feed."""
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': []
        }

        news_bot.job()

        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 1)
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article', return_value=None)
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_no_article_data_skips_publish(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_publish, mock_send_teaser,
    ):
        """If ``fetch_full_article`` returns ``None``, nothing is staged —
        matches the existing skip rule carried over from the removed
        ``process_new_articles`` helper."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._create_mock_entry('http://example.com/article1')]

        news_bot.job()

        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()
        self.assertEqual(pending_articles_repo.count_pending(), 0)

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_via_claude')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_busy_tick_plan_of_day_carries_compact_funnel(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        """Intake-funnel watchdog: a busy tick that stages articles fires
        the plan-of-day [E008] ping (never a false 'no news' [E009]) and
        that ping carries the compact one-line intake summary."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._create_mock_entry('http://example.com/busy1'),
            self._create_mock_entry('http://example.com/busy2'),
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': [],
        }

        news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 2)

        msgs = [
            c.args[0] for c in self.mock_notify.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        # Exactly one plan-of-day ping; no false quiet-day ping.
        plan_msgs = [m for m in msgs if '[E008]' in m]
        self.assertEqual(len(plan_msgs), 1, f"expected one E008; got {msgs!r}")
        self.assertFalse([m for m in msgs if '[E009]' in m],
                         f"quiet-day E009 must NOT fire on a busy tick; got {msgs!r}")
        plan = plan_msgs[0]
        # Compact intake summary present with the staged count.
        self.assertIn('Приём:', plan)
        self.assertIn('в очередь 2', plan)

    def test_source_exception_counts_in_funnel_quiet_day_ping(self):
        """Intake-funnel watchdog: when a SOURCES fetcher raises, the tick's
        real ``except`` path increments ``sources_failed`` and the quiet-day
        [E009] ping pinpoints the collapse at the fetch stage. Proves the
        counter that reaches the ping is driven by an actual exception, not a
        hand-set funnel value. A second fetcher returns [] so nothing is
        fetched → queue empty → quiet day."""
        def boom(notifier=None):
            raise RuntimeError('source boom')

        def empty(notifier=None):
            return []

        with patch('news_bot.SOURCES', [boom, empty]):
            news_bot.job()

        # Nothing fetched → nothing staged → queue empty.
        self.assertEqual(pending_articles_repo.count_pending(), 0)

        msgs = [
            c.args[0] for c in self.mock_notify.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        e009 = [m for m in msgs if '[E009]' in m]
        self.assertEqual(len(e009), 1, f"expected one E009; got {msgs!r}")
        ping = e009[0]
        # The real except-path incremented sources_failed=1; the collapse note
        # names the fetch stage with that count.
        self.assertIn('Где схлопнулось: источники не ответили (1)', ping)
        # And the per-source fetch-failure alert (E002) fired for the raiser.
        self.assertTrue([m for m in msgs if '[E002]' in m],
                        f"expected an E002 source-fetch-failure ping; got {msgs!r}")


# ---------------------------------------------------------------------------
# Outage state machine integration tests.
# ---------------------------------------------------------------------------


def _seed_pending_row(link, source='autoevolution', title='EN Title',
                      paragraphs=None, images=None, blocks=None,
                      subtitle='Lead'):
    """Insert a pending row directly via the repo so outage tests skip
    the fetch-and-stage phase and exercise only the publish path.
    """
    pending_articles_repo.insert_pending({
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'First paragraph.', 'Second paragraph.',
        ],
        'images': images if images is not None else [],
        'blocks': blocks,
        'pub_date': '2026-04-27',
    })


class TestOutageStateIntegration(_IntegrationBase):
    """Integration coverage for AC14/AC15/AC16/AC17.

    The outage_state public API (Task 5 contract) is read via getters
    (``get_outage_started_at``, ``get_ping_count``,
    ``is_fallback_active``) — never raw SQL — so tests survive future
    bot_state key renames inside the state machine.

    DB_FILE patching: ``outage_state._connect()`` reads
    ``news_bot.DB_FILE`` at call time (``import news_bot`` then
    ``sqlite3.connect(news_bot.DB_FILE)``). One ``patch('news_bot.DB_FILE',
    tempfile_path)`` therefore reaches both modules — verified via
    ``grep -nE "DB_FILE|_connect" outage_state.py``.
    """

    def setUp(self):
        super().setUp()
        # NOTE TO FUTURE MAINTAINERS: the notify_patcher.stop() below is
        # NOT redundant — _IntegrationBase.setUp installed a generic
        # silencer for `news_bot.send_admin_notification`. Each per-test
        # @patch('news_bot.send_admin_notification') in this class needs
        # to OWN that name, so we stop the base's silencer here. tearDown
        # restarts it so the base's tearDown has something to stop —
        # changing this dance breaks the per-test admin mock interception.
        self.notify_patcher.stop()
        # Neuter sleep — we don't want the real wall-clock to slow
        # the tests down, but each outage test asserts on number of
        # publishes, not on the sleep argument.
        self.sleep_patcher = patch('news_bot.time.sleep')
        self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()
        # Pair with setUp's stop(): re-start the base silencer so
        # _IntegrationBase.tearDown's notify_patcher.stop() has something
        # to stop. Without this, the second test in the class fails
        # because the patcher is already stopped.
        self.notify_patcher.start()
        super().tearDown()

    @patch('news_bot.send_admin_notification')
    def test_api_level_outage_advances_state_machine(self, mock_admin):
        """API-level Claude error (RateLimitError surfacing as
        ClaudeOutageError) advances ``bot_state``: started_at written,
        ping_count == 1, admin notify fired with outage text. Per
        Task 5 transition table, ``is_fallback_active() == False``
        after the FIRST error — fallback only flips after ping #2 +
        2h grace.
        """
        _seed_pending_row('http://example.com/outage1')

        # Frozen "now" — places the publish loop's first slot at 13:00 MSK
        # so we don't depend on wall-clock-local conditions.
        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))

        def fake_now(tz=None):
            # Crash-loop guard uses ``datetime.now(timezone.utc)``; the
            # publish loop uses ``datetime.now(MSK)``. Route accordingly
            # so the guard's UTC arithmetic against the UTC-naive
            # ``published_at`` doesn't fall over.
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.transcreate_via_claude',
                   side_effect=ClaudeOutageError('RateLimitError: rate limited')), \
             patch('news_bot.transcreate_text', side_effect=lambda t, **kw: f"[g] {t}"), \
             patch('news_bot.telegraph_publisher.publish_article',
                   return_value='https://telegra.ph/x'), \
             patch('news_bot.send_telegraph_teaser', return_value=True), \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            # Pass-through `datetime.combine` only — news_bot imports
            # `timezone` and `timedelta` separately at module level, so
            # mock_dt.timezone / mock_dt.timedelta would never resolve.
            mock_dt.combine = dt.datetime.combine

            news_bot.job()

        # State machine advanced: started_at non-None, ping_count==1.
        # Read via the public outage_state getters, not raw SQL.
        self.assertIsNotNone(outage_state.get_outage_started_at())
        self.assertEqual(outage_state.get_ping_count(), 1)
        # Fallback NOT active yet — flips only after ping #2 + 2h grace.
        self.assertFalse(outage_state.is_fallback_active())

        # Admin received the canonical ping #1. We pin to the
        # outage_state._ping_1_text() prefix instead of just 'Claude' — that
        # rules out an unrelated admin notification (e.g. a source-fetcher
        # error message that happens to contain the word "Claude") from
        # accidentally satisfying this assertion.
        from outage_state import _ping_1_text  # private — pinned contract
        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        self.assertIn(
            _ping_1_text(), ping_msgs,
            msg=f"expected ping #1 verbatim; got admin pings: {ping_msgs!r}",
        )

    @patch('news_bot.send_admin_notification')
    def test_per_article_problem_strikes_immediately_no_state_advance(
        self, mock_admin,
    ):
        """``ClaudeTranscreationError`` → re-raise from _fallback_publish;
        the slot loop's generic Exception handler bumps attempt_count.
        NO inline retry, NO Google fallback for per-article failures —
        per-article hiccups go through the slot-level 3-strike path
        across separate slots (≥ MIN_INTERVAL_MINUTES apart). Outage
        state machine is NOT advanced.
        """
        _seed_pending_row('http://example.com/perart1')

        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))

        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        mock_claude = MagicMock(
            side_effect=ClaudeTranscreationError('malformed JSON'),
        )

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text',
                   side_effect=AssertionError(
                       'Google must NOT fire on per-article failure',
                   )), \
             patch('news_bot.telegraph_publisher.publish_article',
                   side_effect=AssertionError(
                       'publish_article must NOT fire when translation failed',
                   )), \
             patch('news_bot.send_telegraph_teaser',
                   side_effect=AssertionError(
                       'teaser must NOT fire when translation failed',
                   )), \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            mock_dt.combine = dt.datetime.combine

            news_bot.job()

        # Single Claude call (regression guard against any inline retry).
        self.assertEqual(mock_claude.call_count, 1)

        # State machine UNTOUCHED for per-article failures.
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertFalse(outage_state.is_fallback_active())

        # Row was NOT published — stayed in pending with attempt_count++.
        published = pending_articles_repo.get_published('http://example.com/perart1')
        self.assertIsNone(published)
        pending = pending_articles_repo.get_pending('http://example.com/perart1')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.get('attempt_count'), 1)

    @patch('news_bot.send_admin_notification')
    def test_recovery_clears_outage_state_and_sends_switchback_ping(
        self, mock_admin,
    ):
        """Pre-seeded outage state (started_at + ping_count==2) is
        cleared automatically on the next successful Claude publish via
        ``_maybe_record_recovery`` inside ``_fallback_publish``. The
        switch-back ping mentions ``Claude`` or ``recovered``.

        2026-04-30 (P1 fix C1+C4): recovery is no longer "left for a
        future hook" — every successful Claude transcreation in
        ``_fallback_publish`` now calls the helper, so a single in-
        flight outage automatically closes the next time Claude works.
        """
        # Seed an outage that has progressed to ping_2_sent.
        t0 = dt.datetime(2026, 4, 27, 11, 0, tzinfo=UTC)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(2)
        outage_state.set_last_ping_sent_at(t0 + dt.timedelta(hours=1))
        self.assertEqual(outage_state.get_ping_count(), 2)
        self.assertIsNotNone(outage_state.get_outage_started_at())

        _seed_pending_row('http://example.com/recovery1')

        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))

        claude_response = {
            'title': '🚀 RU Title',
            'alts': ['alt 1', 'alt 2'],
            'subtitle': 'RU subtitle',
            'paragraphs': ['RU one.', 'RU two.'],
            'blocks': None,
        }

        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.transcreate_via_claude',
                   return_value=claude_response), \
             patch('news_bot.telegraph_publisher.publish_article',
                   return_value='https://telegra.ph/recovery'), \
             patch('news_bot.send_telegraph_teaser', return_value=True), \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            mock_dt.combine = dt.datetime.combine

            news_bot.job()

        # Row published (proves Claude succeeded).
        self.assertIsNotNone(
            pending_articles_repo.get_published('http://example.com/recovery1'),
        )

        # Outage state auto-cleared by _maybe_record_recovery hook.
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertFalse(outage_state.is_fallback_active())

        # Switch-back ping was sent to admin via send_admin_notification.
        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        self.assertTrue(
            any(('Claude' in m) or ('recovered' in m.lower()) or
                ('восстанов' in m.lower()) for m in ping_msgs),
            f"recovery ping must reach admin; got: {ping_msgs!r}",
        )

    def test_outage_state_persists_across_simulated_restart(self):
        """After write + connection close + reopen, the outage_state
        getters return identical values — proves persistence in the
        ``bot_state`` table, not in-memory state.
        """
        t0 = dt.datetime(2026, 4, 27, 11, 0, tzinfo=UTC)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(2)

        # Forcibly close any cached connection on outage_state — it
        # opens short-lived connections via ``_connect``, so there's
        # nothing to close, but we explicitly assert the read works
        # against a fresh connection by opening one ourselves first
        # to mimic a container restart's "fresh process".
        conn = sqlite3.connect(self.db_path)
        conn.close()

        # Re-read via the public API.
        self.assertEqual(outage_state.get_outage_started_at(), t0)
        self.assertEqual(outage_state.get_ping_count(), 2)


# ---------------------------------------------------------------------------
# Container-restart mid-window integration test.
# ---------------------------------------------------------------------------


class TestRestartMidWindow(_IntegrationBase):
    """AC7: a container restart at 10:00 МСК with N pending articles
    recomputes the schedule via ``compute_fixed_slots(N, 10:00)`` —
    publishing at most once per fixed daily slot (10:00/15:00/19:30 МСК).
    """

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_restart_mid_window_recomputes_schedule(
        self, _mock_admin, _mock_sleep,
    ):
        """5 pending rows + 1 already-published row + frozen
        ``datetime.now`` at 10:00 MSK → ``compute_fixed_slots`` is
        called with N=5 and returns the three fixed slots, so exactly 3
        publishes fire (== MAX_DAILY_POSTS) and 2 rows carry over.
        The already-published row is not re-published (idempotency
        Decision 9 — telegraph_url present in published_articles).

        Frozen at 10:00 (the morning fixed slot) so all three fixed slots
        are eligible — at the old 16:00 only the 19:30 slot would remain and
        just one of the five rows would publish.
        """
        # Seed 5 pending rows.
        for i in range(5):
            _seed_pending_row(f'http://example.com/restart{i}', title=f'T{i}')

        # Seed an already-published row 60 minutes ago — proves it isn't
        # reconsidered for re-publish in this run. ``naive_utc_now`` mirrors
        # what SQLite ``CURRENT_TIMESTAMP`` writes (UTC, naive) without the
        # deprecated ``datetime.utcnow()``.
        naive_utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        old_published_at = (
            naive_utc_now - dt.timedelta(minutes=60)
        ).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ('http://example.com/already', 'OldT', 'РУ Old',
                 'https://telegra.ph/old', 'old', 'autoevolution', 0,
                 old_published_at),
            )
            conn.commit()
        finally:
            conn.close()

        # Frozen now: 10:00 МСК — the morning fixed slot. All three fixed
        # slots (10:00/15:00/19:30) are eligible at this tick.
        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 10, 0, 0))

        captured = {}

        def fake_compute(n, now, *args, **kwargs):
            captured['n'] = n
            captured['now'] = now
            captured['kwargs'] = kwargs
            from compute_publish_slots import compute_fixed_slots as real
            # Pass straight through to the real fixed-slot scheduler — the
            # patch only exists to capture the call args.
            return real(n, now, **kwargs)

        # The crash-loop guard reads ``datetime.now(timezone.utc)`` for
        # gap arithmetic against the UTC-naive ``published_at``. Returning
        # the same MSK-aware ``frozen_now`` from every ``now()`` call would
        # mix tz-aware (MSK) with tz-naive parsed published_at and produce
        # a TypeError. Resolve by routing UTC calls to a real UTC ``now``
        # — guard fires (60min gap < 90min threshold → sleeps, but
        # ``time.sleep`` is mocked), then the publish loop reads
        # ``datetime.now(MSK)`` which gets the frozen MSK value.
        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.compute_fixed_slots',
                   side_effect=fake_compute), \
             patch('news_bot._fallback_publish') as mock_publish, \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            # Pass-through `datetime.combine` and `datetime.strptime` —
            # news_bot uses both. The crash-loop guard parses the seeded
            # published_at via `datetime.strptime`; without this
            # passthrough strptime returns a MagicMock and the gap
            # subtraction explodes. (`timezone` and `timedelta` are
            # imported separately at news_bot module level, so they
            # don't need passthrough.)
            mock_dt.combine = dt.datetime.combine
            mock_dt.strptime = dt.datetime.strptime

            news_bot.job()

        # compute_fixed_slots was called with N=5 and now at 10:00 MSK.
        self.assertEqual(captured.get('n'), 5)
        self.assertEqual(captured['now'].hour, 10)
        self.assertEqual(captured['now'].minute, 0)

        # _fallback_publish was called once per fixed slot. compute_fixed_slots
        # returns 3 slots for N=5, so 3 publishes fire and the 2 surplus rows
        # carry over to tomorrow.
        self.assertEqual(mock_publish.call_count, news_bot.MAX_DAILY_POSTS)

        # Already-published row never went through the publish loop.
        for c in mock_publish.call_args_list:
            row_arg = c.args[0]
            self.assertNotEqual(row_arg.get('link'), 'http://example.com/already')


# ---------------------------------------------------------------------------
# Manual-review preemption integration test (AC21).
# ---------------------------------------------------------------------------


class TestManualReviewPreemption(_IntegrationBase):
    """AC21: when an operator publishes a pending row between fetch and
    the next slot, the bot must skip that row on the next slot.

    The bot's distributed-publish loop calls ``list_pending()`` at every
    slot — so a row removed from ``pending_articles`` by
    ``move_to_published(via_review=True)`` between slots is naturally
    skipped (it's no longer in the list).
    """

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_manual_review_preemption_skips_published_row(
        self, _mock_admin, _mock_sleep,
    ):
        # Seed 3 pending rows.
        _seed_pending_row('http://example.com/r1', title='T1')
        _seed_pending_row('http://example.com/r2', title='T2')
        _seed_pending_row('http://example.com/r3', title='T3')

        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))

        # Track which links are seen by ``_fallback_publish``.
        seen_links = []

        def fake_publish(row, via_review=False):
            link = row['link']
            seen_links.append(link)
            # Mimic real publish: ``move_to_published`` requires
            # ``ru_title`` NOT NULL on the pending row, so first stage
            # the RU fields via ``update_staged`` (real-publish path
            # does this in step 3 of ``_fallback_publish``).
            pending_articles_repo.update_staged(
                link, 'РУ ' + link, '', ['РУ p1.', 'РУ p2.'], None,
            )
            pending_articles_repo.move_to_published(
                link=link,
                telegraph_url=f'https://telegra.ph/bot-{link.rsplit("/", 1)[-1]}',
                telegraph_path=f'bot-{link.rsplit("/", 1)[-1]}',
                via_review=False,
            )
            # Between slot 1 (r1) and slot 2 the operator publishes r2
            # via the manual-review path. Simulate that race here, after
            # the first row is processed. Stage ru fields first to
            # satisfy the NOT NULL constraint on ``published_articles.ru_title``.
            if link == 'http://example.com/r1':
                pending_articles_repo.update_staged(
                    'http://example.com/r2', 'РУ Operator r2', '',
                    ['РУ op p1.'], None,
                )
                pending_articles_repo.move_to_published(
                    link='http://example.com/r2',
                    telegraph_url='https://telegra.ph/operator-r2',
                    telegraph_path='operator-r2',
                    via_review=True,
                )
            return True

        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot._fallback_publish', side_effect=fake_publish) as mock_pub, \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            # Pass-through `datetime.combine` only — news_bot imports
            # `timezone` and `timedelta` separately at module level, so
            # mock_dt.timezone / mock_dt.timedelta would never resolve.
            mock_dt.combine = dt.datetime.combine

            news_bot.job()

        # The bot saw r1 and r3; never r2 (operator preempted it).
        self.assertIn('http://example.com/r1', seen_links)
        self.assertIn('http://example.com/r3', seen_links)
        self.assertNotIn('http://example.com/r2', seen_links)

        # Final state of published_articles: 3 rows total — r1, r3 (bot)
        # + r2 (operator). via_review markers reflect the source.
        r1 = pending_articles_repo.get_published('http://example.com/r1')
        r2 = pending_articles_repo.get_published('http://example.com/r2')
        r3 = pending_articles_repo.get_published('http://example.com/r3')
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertIsNotNone(r3)
        # bool() coerces SQLite 0/1 ints to a proper bool for comparison.
        self.assertFalse(bool(r1.get('via_review')))
        self.assertTrue(bool(r2.get('via_review')))
        self.assertFalse(bool(r3.get('via_review')))


# ---------------------------------------------------------------------------
# Crash-loop guard integration test (AC8 / Decision 9).
# ---------------------------------------------------------------------------


class TestCrashLoopGuard(_IntegrationBase):
    """AC8: a recent ``MAX(published_at)`` makes ``job()`` sleep until
    ``last_published + MIN_INTERVAL_MINUTES`` before continuing —
    defends against container restart loops producing burst-publishes.
    """

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    def test_crash_loop_guard_delays_first_publish(
        self, mock_sleep, _mock_admin,
    ):
        # Seed published_articles with a row 5 minutes ago. SQLite's
        # ``CURRENT_TIMESTAMP`` writes UTC-naive, so mirror that exactly
        # (without the deprecated ``datetime.utcnow()``).
        naive_utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        five_min_ago = (
            naive_utc_now - dt.timedelta(minutes=5)
        ).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ('http://example.com/recent', 'T', 'РУ T',
                 'https://telegra.ph/x', 'autoevolution', 0, five_min_ago),
            )
            conn.commit()
        finally:
            conn.close()

        with patch('news_bot.SOURCES', [lambda notifier=None: []]):
            news_bot.job()

        # Guard sleep ≈ (MIN_INTERVAL_MINUTES - 5) * 60 seconds — driven
        # by the module-level constant so a future floor change doesn't
        # silently break this regression check.
        self.assertTrue(mock_sleep.called, "crash-loop guard must sleep")
        first_arg = mock_sleep.call_args_list[0].args[0]
        expected_seconds = (news_bot.MIN_INTERVAL_MINUTES - 5) * 60
        self.assertGreater(first_arg, expected_seconds - 60)
        self.assertLess(first_arg, expected_seconds + 60)


# ---------------------------------------------------------------------------
# Cross-source dedup integration tests (cross-source-dedup feature, Wave 2).
# ---------------------------------------------------------------------------


class TestCrossSourceDedup(_PrepPhaseBase):
    """Integration coverage for the cross-source dedup gate wired into
    ``news_bot.job()`` between ``_is_text_only_checklist`` and
    ``insert_pending`` (tech-spec Decision 14).

    Each test mocks the same surface as ``TestIntegration`` (``load_feeds``
    / ``fetch_rss`` / ``fetch_full_article``) so the gate sees a
    deterministic article stream. The base's generic
    ``send_admin_notification`` silencer is stopped per-test so we can
    introspect the admin-ping mock; the silencer is re-started in
    ``tearDown`` so ``_PrepPhaseBase``'s teardown has something to stop.

    Seeding strategy:
      * "Already published 7d ago" rows are inserted via
        ``insert_pending(...)`` and then promoted via
        ``move_to_published(...)`` — same path the bot would take in prod,
        so the test exercises the AC2 carry-through too.
      * The new article comes from the mocked RSS feed.
    """

    def setUp(self):
        super().setUp()
        # NOTE: do NOT add a second ``news_bot.SOURCES`` patch here.
        # ``_IntegrationBase.setUp`` already pins SOURCES to
        # ``[_fetch_rss_entries, _fetch_mattel_entries]`` (mattel mocked to
        # []), so the gate already sees ONLY the deterministic mocked RSS
        # stream — the live-network ``_fetch_orangetrack_entries`` source is
        # never in the list. Re-assigning ``self.sources_patcher`` would
        # orphan the base's patcher (its ``stop()`` would no-op) and leak the
        # mutated SOURCES list across the whole test process.
        # Stop the base silencer so per-test ``send_admin_notification``
        # patches can OWN the name (same dance as
        # ``TestOutageStateIntegration``).
        self.notify_patcher.stop()

    def tearDown(self):
        # Re-start the base silencer so ``_IntegrationBase.tearDown``'s
        # notify_patcher.stop() has something to stop.
        self.notify_patcher.start()
        super().tearDown()

    def _seed_published(self, link, fingerprint, source='autoevolution',
                        title='Existing Article'):
        """Insert a pending row with ``model_fingerprint`` then promote it
        to ``published_articles`` so ``list_recent_published_fingerprints``
        sees it. Mirrors the production pending→published path.

        ``ru_title`` MUST be set before ``move_to_published`` — the
        published_articles schema NOT-NULLs it, and ``INSERT OR IGNORE``
        would silently swallow the constraint violation, leaving the
        published row missing and the test asserting on an empty table.
        """
        pending_articles_repo.insert_pending({
            'link': link,
            'source_name': source,
            'feed_url': None,
            'title': title,
            'subtitle': '',
            'paragraphs': ['Body.'],
            'images': [],
            'blocks': None,
            'pub_date': '2026-06-01',
            'model_fingerprint': fingerprint,
        })
        pending_articles_repo.update_staged(
            link, ru_title='RU ' + title, ru_subtitle='',
            ru_paragraphs=['Тело.'], ru_blocks=None,
        )
        pending_articles_repo.move_to_published(
            link,
            telegraph_url='https://telegra.ph/x',
            telegraph_path='x',
            via_review=False,
        )
        return link

    def _set_published_at(self, link, sql_datetime_expr):
        """Directly override ``published_articles.published_at`` for ``link``
        with a SQLite datetime expression (e.g. ``datetime('now','-10 days')``).

        Used by the backstop-window boundary tests: ``_seed_published`` always
        stamps ``published_at`` at the ``CURRENT_TIMESTAMP`` default (now), so
        without this a seeded candidate can never sit INSIDE the 30-day pair
        window but OUTSIDE the 7-day backstop window — the two dates the
        set-overlap backstop's Python cutoff distinguishes."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"UPDATE published_articles SET published_at = "
                f"{sql_datetime_expr} WHERE link = ?",
                (link,),
            )
            conn.commit()
        finally:
            conn.close()

    def _make_entry(self, link, title='New Article', published='2026-06-05'):
        return {
            'link': link,
            'title': title,
            'published': published,
            'summary': 'Summary',
        }

    def _reset_tables(self):
        """Wipe the tables the dedup gate reads/writes so a single test can
        re-run ``job()`` with a different candidate seeding order. Clears
        pending + published (candidate rows), processed_news (else the
        blocked link is skipped as already-processed on the second run) and
        the dedup rate-limit bookkeeping in bot_state."""
        conn = sqlite3.connect(self.db_path)
        try:
            for tbl in ('pending_articles', 'published_articles',
                        'processed_news', 'bot_state'):
                try:
                    conn.execute(f'DELETE FROM {tbl}')
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_distinctive_pair_cross_source_blocks(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC3 — a distinctive (|D) pair shared with a published row from a
        DIFFERENT source within 30 days → new article NOT staged, its link
        pinned in ``processed_news``, exactly one E015 (rendering the matched
        pair), and no E014 / E016."""
        self._seed_published(
            'http://t-hunted.example/existing',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['k-pop demon hunters'],
                'pairs': ['porsche 911|k-pop demon hunters|D'],
            },
            source='t-hunted',
        )

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://autoevolution.example/new'),
        ]
        mock_fetch_article.return_value = {
            'title': 'Porsche 911 gets a K-Pop Demon Hunters makeover',
            'subtitle': '',
            'paragraphs': ['More photos inside.'],
            'images': [],
        }

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 0)
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn('http://autoevolution.example/new', processed)

        e015_calls = [
            c for c in mock_admin.call_args_list
            if '[E015]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e015_calls), 1)
        msg = e015_calls[0].args[0]
        self.assertIn('Совпавшие пары', msg)
        self.assertIn('k-pop demon hunters', msg)
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E016]', m)

        # Full logging: the hard-block decision is recorded in the log itself
        # ([E015] + link + match), not only in the Telegram alert.
        block_lines = [
            l for l in cm.output
            if '[E015]' in l and 'http://autoevolution.example/new' in l
        ]
        self.assertTrue(
            block_lines,
            "expected an [E015] hard-block log line naming the blocked link; "
            "got:\n" + "\n".join(cm.output),
        )

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_quiet_day_ping_shows_dedup_collapse(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Intake-funnel watchdog: sources return an entry but it is dropped
        at the cross-source dedup gate → nothing staged, queue empty → the
        quiet-day [E009] ping carries the intake funnel and pinpoints the
        collapse at the dedup stage (drop-count substring)."""
        self._seed_published(
            'http://t-hunted.example/existing',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['k-pop demon hunters'],
                'pairs': ['porsche 911|k-pop demon hunters|D'],
            },
            source='t-hunted',
        )
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://autoevolution.example/new'),
        ]
        mock_fetch_article.return_value = {
            'title': 'Porsche 911 gets a K-Pop Demon Hunters makeover',
            'subtitle': '',
            'paragraphs': ['More photos inside.'],
            'images': [],
        }

        news_bot.job()

        # Article was blocked at dedup → nothing staged, queue empty.
        self.assertEqual(pending_articles_repo.count_pending(), 0)

        msgs = [
            c.args[0] for c in mock_admin.call_args_list
            if c.args and isinstance(c.args[0], str)
        ]
        e009 = [m for m in msgs if '[E009]' in m]
        self.assertEqual(len(e009), 1, f"expected one E009; got {msgs!r}")
        ping = e009[0]
        # Legacy first line kept + funnel breakdown appended.
        self.assertIn('Бот сработал', ping)
        self.assertIn('Воронка', ping)
        # Collapse pinpointed at dedup. Assert the collapse-note-SPECIFIC line
        # (label + PARENTHESISED count) — 'дубль-блок 1' alone comes from the
        # fixed breakdown line and would pass even if the note stopped
        # pinpointing; 'дубль-блок (1)' can only come from _funnel_collapse_note
        # choosing the dedup stage from the REAL (unmocked) drop count.
        self.assertIn('Где схлопнулось: дубль-блок (1)', ping)
        # Scope note: translate/post is not-applicable (queue empty).
        self.assertIn('очередь пуста', ping)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_broad_pair_soft_flag_is_terminal(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC4 + terminality — a shared BROAD pair (car-line 'car culture',
        tier B) soft-flags: the article PUBLISHES with exactly one E014, and
        the verdict is TERMINAL. The seed also shares the full car fingerprint
        (100% strict overlap, cross-source) so the legacy set-overlap backstop
        WOULD hard-block if it ran — proving the broad flag stops the gate
        (no E015, no second ping)."""
        self._seed_published(
            'http://t-hunted.example/existing',
            {
                'strict': ['toyota supra'],
                'brands': ['toyota'],
                'series': ['car culture'],
                'pairs': ['toyota supra|car culture|B'],
            },
            source='t-hunted',
        )

        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'Toyota Supra joins the Car Culture line',
            'subtitle': '',
            'paragraphs': ['A new release.'],
            'images': [],
        }

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # Soft flag → article still publishes.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        # Not marked processed (only hard blocks are).
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn(new_link, processed)

        e014_calls = [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e014_calls), 1,
                         f"expected exactly one E014, got: {mock_admin.call_args_list}")
        # Terminal: the backstop (which would 100%-block) never ran.
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E015]', m)
            self.assertNotIn('[E016]', m)

        # Full logging: the soft-flag decision is recorded in the log itself
        # (link + [E014] + match), not only in the Telegram alert — so a dedup
        # flag is diagnosable straight from the logs.
        soft_lines = [l for l in cm.output if '[E014]' in l and new_link in l]
        self.assertTrue(
            soft_lines,
            f"expected a soft-flag [E014] log line naming {new_link}; got:\n"
            + "\n".join(cm.output),
        )

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_soft_flag_logged_even_when_alert_rate_limited(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Full-logging headline: a soft-flag decision is recorded in the LOG
        even when the per-pair 7-day alert rate-limit (AC5) suppresses the
        Telegram [E014] ping. Pins the log line's placement OUTSIDE the
        ``if alerted:`` guard — a rate-limited flag would otherwise be invisible
        in BOTH channels. (Reverting the log line back inside ``if alerted:``
        makes this test go red while the non-rate-limited AC2 still passes.)"""
        existing_link = 'http://t-hunted.example/existing'
        new_link = 'http://autoevolution.example/new'
        self._seed_published(
            existing_link,
            {
                'strict': ['toyota supra'],
                'brands': ['toyota'],
                'series': ['car culture'],
                'pairs': ['toyota supra|car culture|B'],
            },
            source='t-hunted',
        )
        # Pre-seed the pair rate-limit so the flag's Telegram ping is suppressed.
        conn = pending_articles_repo._connect()
        try:
            pending_articles_repo.mark_pair_pinged(conn, new_link, existing_link)
            conn.commit()
        finally:
            conn.close()

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'Toyota Supra joins the Car Culture line',
            'subtitle': '',
            'paragraphs': ['A new release.'],
            'images': [],
        }

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # Soft flag is non-terminal → article still publishes.
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        # Rate-limited → NO [E014] Telegram ping this run.
        e014_calls = [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(
            len(e014_calls), 0,
            f"rate-limited flag must not ping; got: {mock_admin.call_args_list}",
        )
        # BUT the decision IS logged, tagged rate-limited.
        rl_lines = [
            l for l in cm.output
            if '[E014]' in l and new_link in l and 'rate-limited' in l
        ]
        self.assertTrue(
            rl_lines,
            "expected a rate-limited [E014] soft-flag log line naming "
            f"{new_link}; got:\n" + "\n".join(cm.output),
        )

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_distinctive_wins_over_broad(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """|D wins over |B via scan-and-remember, independent of candidate
        order. The new article shares a broad pair with one published row and
        a distinctive pair with another → verdict must be BLOCK (not flag) no
        matter which candidate the scan meets first. Asserted for BOTH seeding
        orders."""
        broad_seed = (
            'http://t-hunted.example/broad',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['car culture'],
                'pairs': ['porsche 911|car culture|B'],
            },
        )
        distinctive_seed = (
            'http://lamley.example/distinctive',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['k-pop demon hunters'],
                'pairs': ['porsche 911|k-pop demon hunters|D'],
            },
        )
        new_link = 'http://autoevolution.example/new'
        article = {
            'title': 'Porsche 911 in K-Pop Demon Hunters and Car Culture',
            'subtitle': '',
            'paragraphs': ['Details.'],
            'images': [],
        }

        for order in ([broad_seed, distinctive_seed],
                      [distinctive_seed, broad_seed]):
            self._reset_tables()
            mock_admin.reset_mock()
            for link, fp in order:
                self._seed_published(
                    link, fp,
                    source='t-hunted' if 't-hunted' in link else 'lamley',
                )
            mock_load_feeds.return_value = ['http://example.com/feed1.xml']
            mock_fetch_rss.return_value = [self._make_entry(new_link)]
            mock_fetch_article.return_value = article

            news_bot.job()

            self.assertEqual(
                pending_articles_repo.count_pending(), 0,
                f"distinctive pair must hard-block (order={[l for l, _ in order]})",
            )
            e015_calls = [
                c for c in mock_admin.call_args_list
                if '[E015]' in (c.args[0] if c.args else '')
            ]
            self.assertEqual(len(e015_calls), 1)
            # The block is the distinctive pair, never the broad one.
            self.assertIn('k-pop demon hunters', e015_calls[0].args[0])
            for c in mock_admin.call_args_list:
                m = c.args[0] if c.args else ''
                self.assertNotIn('[E014]', m)
                self.assertNotIn('[E016]', m)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_pair_pass_falls_through_to_backstop_block(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC5 — pair rule passes (series recognised but NO shared pair: the
        two articles carry the SAME cars under DIFFERENT series) → the legacy
        7-day set-overlap backstop still runs and, in this positive case,
        really BLOCKS on the 100% cross-source car overlap. Asserted as a
        backstop block (E015 rendering the overlap percent, not a pair)."""
        self._seed_published(
            'http://t-hunted.example/existing',
            {
                'strict': ['subaru legacy gt', 'toyota 4runner'],
                'brands': ['subaru', 'toyota'],
                'series': ['team transport'],
                'pairs': ['subaru legacy gt|team transport|B',
                          'toyota 4runner|team transport|B'],
            },
            source='t-hunted',
        )

        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        # Same two cars, but a DIFFERENT series ('car culture') → no shared
        # pair, so the pair rule passes; strict/brands overlap is 100%.
        mock_fetch_article.return_value = {
            'title': 'New drop.',
            'subtitle': '',
            'paragraphs': [
                'Toyota 4Runner spotted.',
                'Subaru Legacy GT joins Car Culture.',
            ],
            'images': [],
        }

        # Spy on the two 30-day candidate fetches. This is the ONE scenario
        # that reaches BOTH rules in a single gate call (pair rule scans, then
        # falls through to the backstop), so it is the only place a regression
        # to a second SQL round-trip would surface — pin the single-fetch
        # guarantee (tech-spec Decision 5: one 30-day fetch, 7-day subset in
        # Python).
        with patch('news_bot.pending_repo.list_recent_pending_fingerprints',
                   wraps=pending_articles_repo.list_recent_pending_fingerprints
                   ) as m_pend, \
             patch('news_bot.pending_repo.list_recent_published_fingerprints',
                   wraps=pending_articles_repo.list_recent_published_fingerprints
                   ) as m_pub:
            news_bot.job()

        m_pend.assert_called_once()
        m_pub.assert_called_once()

        # Backstop hard-blocked the cross-source car dupe.
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn(new_link, processed)

        e015_calls = [
            c for c in mock_admin.call_args_list
            if '[E015]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e015_calls), 1)
        # Backstop signature: legacy overlap-percent block, NOT a pair block.
        msg = e015_calls[0].args[0]
        self.assertIn('Совпадение:', msg)
        self.assertNotIn('Совпавшие пары', msg)
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E016]', m)

    #: Backstop-window boundary fixture. A cross-source published row whose
    #: FULL car fingerprint (100% strict overlap) would hard-block the new
    #: article via the set-overlap backstop, while its ``team transport`` pairs
    #: never intersect the new article's ``car culture`` pairs — so the tiered
    #: pair rule PASSES and the article always reaches the backstop. Isolating
    #: only the published_at date lets the two tests below probe the 7-day
    #: cutoff and nothing else. Mirrors ``test_pair_pass_falls_through_to_
    #: backstop_block`` (proven to backstop-block when the row is in-window).
    _BACKSTOP_SEED_FP = {
        'strict': ['subaru legacy gt', 'toyota 4runner'],
        'brands': ['subaru', 'toyota'],
        'series': ['team transport'],
        'pairs': ['subaru legacy gt|team transport|B',
                  'toyota 4runner|team transport|B'],
    }
    _BACKSTOP_NEW_ARTICLE = {
        'title': 'New drop.',
        'subtitle': '',
        # Same two cars, but a DIFFERENT series ('car culture') → no shared
        # pair (pair rule passes); 100% strict overlap (backstop would block).
        'paragraphs': [
            'Toyota 4Runner spotted.',
            'Subaru Legacy GT joins Car Culture.',
        ],
        'images': [],
    }

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_backstop_excludes_candidate_older_than_7_days(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Backstop 7-day cutoff, OUTSIDE edge (tech-spec Decision 5). A
        cross-source candidate that WOULD hard-block on 100% car overlap is
        aged to ``published_at = now-10 days`` — INSIDE the 30-day pair-fetch
        window but OUTSIDE the 7-day backstop window. The pair rule passes (no
        shared pair) and the backstop's Python date cutoff must SKIP the row →
        the article PUBLISHES, its link is NOT pinned in ``processed_news`` and
        NO ``[E015]`` fires. Pins the ``_DEDUP_BACKSTOP_WINDOW_DAYS`` cutoff
        math (format string / timezone / off-by-one): neutralising that check
        makes THIS test fail (verified in review)."""
        seed_link = self._seed_published(
            'http://t-hunted.example/existing',
            dict(self._BACKSTOP_SEED_FP),
            source='t-hunted',
        )
        # Age the candidate to 10 days ago — inside 30d (pair fetch still sees
        # it) but outside the backstop's 7d subset.
        self._set_published_at(seed_link, "datetime('now','-10 days')")

        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = dict(self._BACKSTOP_NEW_ARTICLE)

        news_bot.job()

        # Outside the 7-day window → backstop can't match → article publishes.
        self.assertEqual(
            pending_articles_repo.count_pending(), 1,
            "candidate aged past the 7-day backstop window must NOT block",
        )
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn(new_link, processed)
        # No dedup ping of any kind — nothing matched.
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E015]', m)
            self.assertNotIn('[E016]', m)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_backstop_includes_candidate_within_7_days(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Backstop 7-day cutoff, INSIDE edge — the companion that proves the
        cutoff is not over-excluding. Identical seeding to the OUTSIDE case but
        aged to ``published_at = now-3 days`` (inside 7d). The backstop DOES run
        against the row and hard-blocks on the 100% cross-source car overlap →
        the article is NOT staged, its link lands in ``processed_news`` and
        exactly one ``[E015]`` (legacy overlap-percent, not a pair) fires."""
        seed_link = self._seed_published(
            'http://t-hunted.example/existing',
            dict(self._BACKSTOP_SEED_FP),
            source='t-hunted',
        )
        # Age the candidate to 3 days ago — comfortably inside the 7-day window.
        self._set_published_at(seed_link, "datetime('now','-3 days')")

        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = dict(self._BACKSTOP_NEW_ARTICLE)

        news_bot.job()

        # Inside the 7-day window → backstop hard-blocks the cross-source dupe.
        self.assertEqual(
            pending_articles_repo.count_pending(), 0,
            "candidate inside the 7-day backstop window must block",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn(new_link, processed)

        e015_calls = [
            c for c in mock_admin.call_args_list
            if '[E015]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e015_calls), 1)
        # Backstop signature: legacy overlap-percent block, NOT a pair block.
        msg = e015_calls[0].args[0]
        self.assertIn('Совпадение:', msg)
        self.assertNotIn('Совпавшие пары', msg)
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E016]', m)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_toggle_off_skips_pair_rule(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC6 — with ``news_bot.DEDUP_SERIES_ENABLED`` monkeypatched off, the
        pair rule is skipped entirely: a same-source distinctive pair that
        would hard-block when enabled instead PUBLISHES, because only the
        cross-source-only set-overlap backstop runs (and it skips the
        same-source candidate). Behaviour identical to pre-feature dedup."""
        self._seed_published(
            'http://autoevolution.example/existing',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['k-pop demon hunters'],
                'pairs': ['porsche 911|k-pop demon hunters|D'],
            },
            source='autoevolution',
        )

        new_link = 'https://www.autoevolution.com/news/more-photos.html'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'Porsche 911 gets a K-Pop Demon Hunters makeover',
            'subtitle': '',
            'paragraphs': ['More photos inside.'],
            'images': [],
        }

        with patch('news_bot.DEDUP_SERIES_ENABLED', False):
            news_bot.job()

        # Pair rule skipped → same-source distinctive pair does NOT block.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E015]', m)
            self.assertNotIn('[E016]', m)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_hard_block_path(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """100% model overlap → new article NOT staged, its link landed
        in ``processed_news`` (Decision 8), exactly one E015 ping fired."""
        existing_fp = {
            'strict': ['toyota 4runner', 'subaru legacy gt'],
            'brands': ['toyota', 'subaru'],
        }
        self._seed_published(
            'http://t-hunted.example/existing',
            existing_fp,
            source='t-hunted',
        )

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://autoevolution.example/new'),
        ]
        # Article body that ``extract_fingerprint`` will turn into the same
        # tokens as ``existing_fp`` → similarity == 1.0 → hard block.
        mock_fetch_article.return_value = {
            'title': '2018 Toyota 4Runner gold chase',
            'subtitle': '',
            'paragraphs': ['Subaru Legacy GT (BP) shows up here too.'],
            'images': [],
        }

        news_bot.job()

        # New article never reached pending_articles.
        self.assertEqual(pending_articles_repo.count_pending(), 0)

        # Its link is in processed_news so the next tick won't re-fetch.
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn('http://autoevolution.example/new', processed)

        # Exactly one E015 ping fired (no E014 / E016).
        e015_calls = [
            c for c in mock_admin.call_args_list
            if '[E015]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e015_calls), 1)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_pass_through_with_non_empty_fp(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Non-empty fingerprint, no match in window → article staged with
        fingerprint populated, no E014/E015/E016 pings fired. Dominant
        production case — protects against "no match accidentally pings"
        regressions."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://example.com/article1'),
        ]
        mock_fetch_article.return_value = {
            'title': '2018 Toyota 4Runner gold chase',
            'subtitle': '',
            'paragraphs': ['Subaru Legacy GT shows up here.'],
            'images': [],
        }

        news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 1)
        row = pending_articles_repo.get_pending('http://example.com/article1')
        self.assertIsNotNone(row)
        fp = row.get('model_fingerprint')
        self.assertIsInstance(fp, dict)
        # The extractor surfaces the brands in the body → at least one
        # strict token. We don't pin exact tokens (extractor evolves) —
        # the contract being verified is "non-empty fp lands on the row".
        self.assertTrue(fp.get('strict'),
                        f"expected non-empty strict tokens, got {fp!r}")

        # No dedup pings on the dominant pass-through case.
        for c in mock_admin.call_args_list:
            msg = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', msg)
            self.assertNotIn('[E015]', msg)
            self.assertNotIn('[E016]', msg)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_soft_flag_path(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """~35% overlap → both rows in pending, exactly one E014 ping."""
        # Existing row has 4 strict tokens; new has 4. Shared: 1 strict
        # token → 1/7 ≈ 0.143. Bump shared overlap with brands fallback by
        # using 2-brand sets on each side with shared brand → AC10 max
        # selects brands jaccard. Build pair: existing {a4,b1,c1,d1},
        # new {a4,e1,f1,g1}; brands existing {a,b,c,d}, new {a,e,f,g} →
        # brand jaccard = 1/7 ≈ 0.14. Need 0.30-0.49.
        #
        # Cleaner construction: existing {x1, x2, x3}, new {x1, x2, y1}.
        # strict jaccard = 2/4 = 0.5 → block, not flag. Drop to 1/3.
        # existing {a1, b1, c1}, new {a1, d1, e1} → strict 1/5 = 0.2.
        # Use 2 of 5 shared → strict {a1,b1,c1,d1,e1} vs {a1,b1,f1,g1,h1}
        # → 2/8 = 0.25. Need ≥0.30.
        # Use {a1,b1,c1} vs {a1,b1,d1,e1} → 2/5 = 0.40 — flag-range hit.
        # Concretely with model-extractor tokens:
        #   existing strict: ['toyota 4runner', 'subaru legacy gt',
        #                     'honda civic']
        #   new strict:      ['toyota 4runner', 'subaru legacy gt',
        #                     'mazda mx-5', 'nissan 240sx']
        # → shared = 2, union = 5, jaccard = 0.40 → flag.
        existing_fp = {
            'strict': ['toyota 4runner', 'subaru legacy gt', 'honda civic'],
            'brands': ['toyota', 'subaru', 'honda'],
        }
        self._seed_published(
            'http://t-hunted.example/existing',
            existing_fp,
            source='t-hunted',
        )

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://autoevolution.example/new'),
        ]
        mock_fetch_article.return_value = {
            'title': '2018 Toyota 4Runner gold chase',
            'subtitle': '',
            # Four strict tokens, two shared with the existing 3-token
            # fingerprint → 2/5 ≈ 0.40 — soft-flag range. Each brand in
            # its own sentence so the extractor doesn't accidentally
            # swallow one into another's model_extra tail.
            'paragraphs': [
                'Subaru Legacy GT (BP) shows up too.',
                'Ford Mustang Boss is also there.',
                'Nissan Skyline R32 rounds it out.',
            ],
            'images': [],
        }

        news_bot.job()

        # Both rows in pending+published; the new one is in pending.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        new_row = pending_articles_repo.get_pending(
            'http://autoevolution.example/new'
        )
        self.assertIsNotNone(new_row)
        # Fingerprint propagated through the soft-flag path.
        self.assertIsInstance(new_row.get('model_fingerprint'), dict)

        # Exactly one E014 ping, no E015 / E016.
        e014_calls = [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e014_calls), 1,
                         f"expected 1 E014 ping, got: {mock_admin.call_args_list}")
        for c in mock_admin.call_args_list:
            msg = c.args[0] if c.args else ''
            self.assertNotIn('[E015]', msg)
            self.assertNotIn('[E016]', msg)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_soft_flag_rate_limited(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Same (new, existing) pair pinged once already (within 7 days) →
        second ``job()`` invocation does NOT re-fire E014 (AC5)."""
        existing_link = 'http://t-hunted.example/existing'
        new_link = 'http://autoevolution.example/new'
        existing_fp = {
            'strict': ['toyota 4runner', 'subaru legacy gt', 'honda civic'],
            'brands': ['toyota', 'subaru', 'honda'],
        }
        self._seed_published(existing_link, existing_fp, source='t-hunted')

        # Pre-seed the rate-limit row so the gate treats this pair as
        # "already pinged" on the first tick.
        rl_conn = pending_articles_repo._connect()
        try:
            pending_articles_repo.mark_pair_pinged(rl_conn, new_link, existing_link)
            rl_conn.commit()
        finally:
            rl_conn.close()

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': '2018 Toyota 4Runner gold chase',
            'subtitle': '',
            # Four strict tokens, two shared with the existing 3-token
            # fingerprint → 2/5 ≈ 0.40 — soft-flag range. Each brand in
            # its own sentence so the extractor doesn't accidentally
            # swallow one into another's model_extra tail.
            'paragraphs': [
                'Subaru Legacy GT (BP) shows up too.',
                'Ford Mustang Boss is also there.',
                'Nissan Skyline R32 rounds it out.',
            ],
            'images': [],
        }

        news_bot.job()

        # Article staged (soft-flag still passes through), no E014.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        e014_calls = [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(
            len(e014_calls), 0,
            f"E014 must be rate-limited, got: {mock_admin.call_args_list}",
        )

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_within_source_no_series_publishes(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Set-overlap BACKSTOP stays CROSS-source only (Decision 9 reversed
        2026-06-14). Two articles from the SAME source with NO recognised
        series (so the tiered pair rule does not engage) are never compared —
        even at 100% car-fingerprint overlap — because one source republishing
        the same model within 7 days is implausible and within-source
        comparison only produces false positives (autoevolution Ford F-100 vs
        Porsche Team Transport). The same-source new article must PUBLISH."""
        existing_fp = {
            'strict': ['toyota 4runner', 'subaru legacy gt'],
            'brands': ['toyota', 'subaru'],
            'series': [],
            'pairs': [],
        }
        self._seed_published(
            'http://autoevolution.example/existing',
            existing_fp,
            source='autoevolution',
        )

        # New article on the SAME source. Use a REAL autoevolution netloc so
        # `_fetch_rss_entries` resolves source_name='autoevolution' (it
        # rebuilds each item and overwrites source_name via the netloc map,
        # ignoring any pre-set value). 100% overlap — yet must NOT be blocked.
        new_link = 'https://www.autoevolution.com/news/within-source-new.html'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': '2018 Toyota 4Runner gold chase',
            'subtitle': '',
            'paragraphs': ['Subaru Legacy GT (BP) shows up too.'],
            'images': [],
        }

        news_bot.job()

        # Same-source pair is skipped → article passes the gate and is staged
        # to pending (NOT blocked, NOT marked processed).
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        row = pending_articles_repo.get_pending(new_link)
        self.assertIsNotNone(row)
        # Pass-through keeps the computed (non-empty) fingerprint on the row.
        self.assertIsInstance(row['model_fingerprint'], dict)
        self.assertTrue(
            row['model_fingerprint'].get('strict'),
            "pass-through should keep the computed fingerprint",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn(new_link, processed)
        # No block ping (E015) and no soft-flag ping (E014) for same-source.
        for code in ('[E015]', '[E014]'):
            calls = [
                c for c in mock_admin.call_args_list
                if code in (c.args[0] if c.args else '')
            ]
            self.assertEqual(len(calls), 0, f"unexpected {code} ping")

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_within_source_distinctive_pair_blocks(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The tiered pair rule is ANY-source (unlike the backstop): a shared
        DISTINCTIVE pair means the SAME casting + franchise, so a same-source
        "more photos" repost is a real duplicate. Guards against reverting the
        any-source pair scan back into a cross-source-only skip. New article
        shares ``porsche 911|k-pop demon hunters|D`` with a same-source
        published row → hard block, exactly one E015."""
        self._seed_published(
            'http://autoevolution.example/existing',
            {
                'strict': ['porsche 911'],
                'brands': ['porsche'],
                'series': ['k-pop demon hunters'],
                'pairs': ['porsche 911|k-pop demon hunters|D'],
            },
            source='autoevolution',
        )

        # Same source (real autoevolution netloc resolves to 'autoevolution').
        new_link = 'https://www.autoevolution.com/news/more-photos.html'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'Porsche 911 gets a K-Pop Demon Hunters makeover',
            'subtitle': '',
            'paragraphs': ['More photos inside.'],
            'images': [],
        }

        news_bot.job()

        # Hard-blocked despite being same-source (pair rule is any-source).
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn(new_link, processed)

        e015_calls = [
            c for c in mock_admin.call_args_list
            if '[E015]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e015_calls), 1)
        # Rendered via the pair-rule builder path (matched-pairs block), not
        # the legacy overlap-percent block.
        self.assertIn('Совпавшие пары', e015_calls[0].args[0])
        for c in mock_admin.call_args_list:
            msg = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', msg)
            self.assertNotIn('[E016]', msg)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.model_extractor.extract_fingerprint',
           return_value={'strict': [], 'brands': [], 'series': [], 'pairs': []})
    def test_empty_fingerprint(
        self, _mock_extract, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article,
    ):
        """Article with neither brands nor series → passes through with the
        4-key all-empty fingerprint stored (AC8). Empty ``strict`` AND empty
        ``series`` → the gate short-circuits before any candidate fetch and
        fires no pings."""
        # Pre-seed a row to verify the gate doesn't even try to compare —
        # if it did, the empty similarity guard (AC6) returns 0.0 anyway,
        # but the test pins the no-pings invariant.
        self._seed_published(
            'http://t-hunted.example/existing',
            {'strict': ['toyota 4runner'], 'brands': ['toyota']},
            source='t-hunted',
        )

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry('http://example.com/empty'),
        ]
        mock_fetch_article.return_value = {
            'title': 'Industry news headline',
            'subtitle': '',
            'paragraphs': ['No brand mentions in this body at all.'],
            'images': [],
        }

        news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 1)
        row = pending_articles_repo.get_pending('http://example.com/empty')
        self.assertIsNotNone(row)
        self.assertEqual(
            row.get('model_fingerprint'),
            {'strict': [], 'brands': [], 'series': [], 'pairs': []},
        )
        for c in mock_admin.call_args_list:
            msg = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', msg)
            self.assertNotIn('[E015]', msg)
            self.assertNotIn('[E016]', msg)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_both_empty_short_circuit_real_extraction(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC8 end-to-end — a generic non-HW article whose REAL extraction
        yields empty ``strict`` AND empty ``series`` publishes via the
        both-empty short-circuit, with no dedup ping.

        Unlike ``test_empty_fingerprint`` (which FORCES the 4-key empty shape
        via a mock), this runs the ACTUAL ``extract_fingerprint`` over a title
        with no recognisable brand/model and no lexicon series (test-audit L-4),
        so the real both-empty gate short-circuit is exercised end-to-end while
        a candidate row is seeded."""
        # Seed a published candidate so the gate WOULD have something to fetch
        # if it did not short-circuit — makes the no-ping assertion meaningful.
        self._seed_published(
            'http://t-hunted.example/existing',
            {'strict': ['toyota 4runner'], 'brands': ['toyota'],
             'series': [], 'pairs': []},
            source='t-hunted',
        )

        new_link = 'http://autoevolution.example/generic'
        generic_article = {
            'title': 'City council approves new downtown park budget',
            'subtitle': '',
            'paragraphs': [
                'The local council voted on Tuesday to fund a new public park.',
                'Construction is expected to begin next spring near the river.',
            ],
            'images': [],
        }
        # Real-extraction sanity-check: genuinely both-empty (no brand/model,
        # no lexicon series) — this is a reachable input, not a synthetic mock.
        probe = news_bot.model_extractor.extract_fingerprint(generic_article)
        self.assertEqual(probe.get('strict'), [])
        self.assertEqual(probe.get('series'), [])

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = generic_article

        news_bot.job()

        # Both-empty short-circuit → article publishes.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        # No dedup ping of any kind.
        for c in mock_admin.call_args_list:
            msg = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', msg)
            self.assertNotIn('[E015]', msg)
            self.assertNotIn('[E016]', msg)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.model_extractor.extract_fingerprint',
           side_effect=RuntimeError("boom"))
    def test_degraded_mode(
        self, _mock_extract, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article,
    ):
        """Decision 12 / AC9 — extractor regression must NOT block
        publishing. Article lands in pending with ``model_fingerprint``
        NULL; exactly one E016 ping fires per hour."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.side_effect = [
            [self._make_entry('http://example.com/degraded1')],
            [self._make_entry('http://example.com/degraded2')],
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '',
            'paragraphs': ['Body.'], 'images': [],
        }

        news_bot.job()

        # First article published with NULL fingerprint (degraded path).
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        row1 = pending_articles_repo.get_pending(
            'http://example.com/degraded1'
        )
        self.assertIsNotNone(row1)
        self.assertIsNone(row1.get('model_fingerprint'))

        # E016 ping fired (with RuntimeError in the body).
        e016_calls = [
            c for c in mock_admin.call_args_list
            if '[E016]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e016_calls), 1)
        self.assertIn('RuntimeError', e016_calls[0].args[0])

        # Second job() within the same hour MUST NOT re-fire E016
        # (1-hour rate-limit per Decision 6 / AC9).
        mock_admin.reset_mock()
        news_bot.job()
        e016_calls_2 = [
            c for c in mock_admin.call_args_list
            if '[E016]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(
            len(e016_calls_2), 0,
            f"E016 must be rate-limited within 1h, got: {mock_admin.call_args_list}",
        )
        # Second article still published (degraded mode never blocks).
        self.assertEqual(pending_articles_repo.count_pending(), 2)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_theme_only_pop_culture_flags_no_model(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC4 + AC8 — a REAL pop-culture theme-only article (no recognisable
        car model → empty ``strict`` with a non-empty ``series``/``pairs``
        produced by the ACTUAL ``extract_fingerprint``, theme-only key
        ``*|k-pop demon hunters|B``) shares its theme-only pair with a prior
        published row → soft-flag: the article PUBLISHES with exactly one E014
        naming the matched theme, no E015/E016. Exercises the
        pop-culture-no-model path end-to-end through REAL extraction — the
        exact case the empty-fp re-gate exists to let through (the extractor
        genuinely emits ``strict=[]`` here, so this is a reachable scenario,
        not a synthetic fixture). The re-gate LINE itself is pinned by the
        companion ``test_theme_only_pop_culture_not_short_circuited``."""
        # Prior published theme-only row from another source, no car model.
        self._seed_published(
            'http://t-hunted.example/existing',
            {'strict': [], 'brands': [],
             'series': ['k-pop demon hunters'],
             'pairs': ['*|k-pop demon hunters|B']},
            source='t-hunted',
        )

        new_link = 'http://autoevolution.example/new'
        pop_article = {
            'title': 'K-Pop Demon Hunters joins the Hot Wheels character car lineup',
            'subtitle': '',
            'paragraphs': [
                'The animated hit K-Pop Demon Hunters gets a Hot Wheels collectible.',
                'No specific car casting was announced.',
            ],
            'images': [],
        }
        # Real-extraction sanity-check: empty strict + a theme-only pair.
        probe = news_bot.model_extractor.extract_fingerprint(pop_article)
        self.assertEqual(probe.get('strict'), [])
        self.assertIn('*|k-pop demon hunters|B', probe.get('pairs') or [])

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = pop_article

        news_bot.job()

        # Soft flag → article still publishes.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))

        e014_calls = [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(
            len(e014_calls), 1,
            f"expected exactly one E014, got: {mock_admin.call_args_list}",
        )
        self.assertIn('k-pop demon hunters', e014_calls[0].args[0])
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E015]', m)
            self.assertNotIn('[E016]', m)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_theme_only_pop_culture_not_short_circuited(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """AC8 re-gate PIN — a REAL theme-only pop-culture article (empty
        ``strict`` + non-empty ``series``/``pairs`` from the actual
        ``extract_fingerprint``) must NOT hit the empty-fp short-circuit.

        The re-gate ``if not strict and not series`` only changes control flow
        for the empty-strict / non-empty-series shape, and the pair scan
        (which precedes it) masks that difference whenever the toggle is on.
        So we toggle the pair rule OFF: the theme-only article then has to fall
        THROUGH the re-gate into the set-overlap backstop, whose only
        observable act for an empty-strict fingerprint is fetching candidates
        (``similarity`` is 0 on empty strict, so no verdict, no ping — the
        article publishes either way). Reverting the re-gate to the old
        ``if not strict`` short-circuits BEFORE that fetch, leaving the spies
        below uncalled → this test FAILS (verified). The fetch call is the
        load-bearing observation that pins the re-gate line."""
        # A cross-source published row so the backstop fetch has a row to
        # return — not required for the pin, keeps the scenario realistic.
        self._seed_published(
            'http://t-hunted.example/existing',
            {'strict': ['toyota supra'], 'brands': ['toyota'],
             'series': [], 'pairs': []},
            source='t-hunted',
        )

        new_link = 'http://autoevolution.example/new'
        pop_article = {
            'title': 'K-Pop Demon Hunters comes to Hot Wheels shelves',
            'subtitle': '',
            'paragraphs': [
                'The K-Pop Demon Hunters franchise arrives as a Hot Wheels tie-in.',
            ],
            'images': [],
        }
        # Real-extraction sanity-check: empty strict, non-empty series.
        probe = news_bot.model_extractor.extract_fingerprint(pop_article)
        self.assertEqual(probe.get('strict'), [])
        self.assertTrue(probe.get('series'))

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = pop_article

        with patch('news_bot.DEDUP_SERIES_ENABLED', False), \
             patch('news_bot.pending_repo.list_recent_pending_fingerprints',
                   wraps=pending_articles_repo.list_recent_pending_fingerprints
                   ) as m_pend, \
             patch('news_bot.pending_repo.list_recent_published_fingerprints',
                   wraps=pending_articles_repo.list_recent_published_fingerprints
                   ) as m_pub:
            news_bot.job()

        # Re-gate held: the theme-only article was NOT short-circuited — it
        # reached the backstop, which fetched candidates exactly once. The
        # buggy ``if not strict`` revert would short-circuit before this fetch.
        m_pend.assert_called_once()
        m_pub.assert_called_once()

        # Empty strict → backstop can't match → article publishes, no pings.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E015]', m)
            self.assertNotIn('[E016]', m)


class TestFingerprintCarryThrough(_IntegrationBase):
    """AC2 — fingerprint written to ``pending_articles`` survives the
    ``move_to_published`` transition and is queryable via direct SELECT
    on ``published_articles``. Lives on ``_IntegrationBase`` (no
    ``job()``) — direct repo API only."""

    def test_pending_to_published_roundtrip(self):
        fp = {
            'strict': ['toyota 4runner', 'subaru legacy gt'],
            'brands': ['toyota', 'subaru'],
        }
        pending_articles_repo.insert_pending({
            'link': 'http://example.com/roundtrip',
            'source_name': 'autoevolution',
            'feed_url': None,
            'title': 'Roundtrip Title',
            'subtitle': '',
            'paragraphs': ['Body.'],
            'images': [],
            'blocks': None,
            'pub_date': '2026-06-05',
            'model_fingerprint': fp,
        })
        # ``ru_title`` is NOT NULL on published_articles — set it via
        # ``update_staged`` so ``move_to_published``'s ``INSERT OR IGNORE``
        # doesn't silently swallow the constraint violation.
        pending_articles_repo.update_staged(
            'http://example.com/roundtrip',
            ru_title='RU Roundtrip Title', ru_subtitle='',
            ru_paragraphs=['Тело.'], ru_blocks=None,
        )
        pending_articles_repo.move_to_published(
            'http://example.com/roundtrip',
            telegraph_url='https://telegra.ph/x',
            telegraph_path='x',
            via_review=False,
        )

        # Direct SELECT on published_articles — pin the column lives
        # there with the same JSON shape as the pending row.
        conn = sqlite3.connect(self.db_path)
        try:
            stored_raw = conn.execute(
                "SELECT model_fingerprint FROM published_articles WHERE link=?",
                ('http://example.com/roundtrip',),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stored_raw)
        import json as _json
        self.assertEqual(_json.loads(stored_raw[0]), fp)

    def test_pending_to_published_roundtrip_with_pairs(self):
        """AC2 — the 4-key fingerprint shape (``series``/``pairs`` added by the
        dedup-model-series feature) survives the ``move_to_published``
        transition byte-for-byte, not just the legacy 2-key
        ``strict``/``brands`` shape. Guards the carry-through of the new keys
        the cross-source pair rule reads on published candidates."""
        fp = {
            'strict': ['porsche 911'],
            'brands': ['porsche'],
            'series': ['k-pop demon hunters'],
            'pairs': ['porsche 911|k-pop demon hunters|D'],
        }
        pending_articles_repo.insert_pending({
            'link': 'http://example.com/roundtrip-pairs',
            'source_name': 'autoevolution',
            'feed_url': None,
            'title': 'Roundtrip Pairs Title',
            'subtitle': '',
            'paragraphs': ['Body.'],
            'images': [],
            'blocks': None,
            'pub_date': '2026-06-05',
            'model_fingerprint': fp,
        })
        pending_articles_repo.update_staged(
            'http://example.com/roundtrip-pairs',
            ru_title='RU Roundtrip Pairs', ru_subtitle='',
            ru_paragraphs=['Тело.'], ru_blocks=None,
        )
        pending_articles_repo.move_to_published(
            'http://example.com/roundtrip-pairs',
            telegraph_url='https://telegra.ph/x',
            telegraph_path='x',
            via_review=False,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            stored_raw = conn.execute(
                "SELECT model_fingerprint FROM published_articles WHERE link=?",
                ('http://example.com/roundtrip-pairs',),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stored_raw)
        import json as _json
        self.assertEqual(_json.loads(stored_raw[0]), fp)


# ---------------------------------------------------------------------------
# End-of-tick PUBLISH RECAP integration tests ([E034], publish-recap feature).
# ---------------------------------------------------------------------------


class TestPublishRecapIntegration(_IntegrationBase):
    """The publish loop (job() step (e)) accumulates per-slot outcome counters
    and, after the loop, sends a PUBLISH RECAP admin ping ([E034]) that surfaces
    what posted and WHY a post failed. These drive the loop by patching
    ``_publish_with_retries`` (outcome source) + ``compute_fixed_slots`` (slot
    count) + frozen time, like the existing publish-loop tests.
    """

    FROZEN = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))

    def _publish_side_effect(self, outcomes):
        """Build a fake ``_publish_with_retries``. ``outcomes`` maps a row link
        to ``(outcome, err)``. A 'published' outcome also removes the row from
        pending (the real publish path that does so is bypassed here), so the
        next slot pulls the next row. 'held'/'failed' leave the row in place —
        exactly as the real loop expects.
        """
        def fake(row, idx, n_slots):
            link = row['link']
            outcome, err = outcomes[link]
            if outcome == 'published':
                pending_articles_repo.update_staged(
                    link, 'РУ ' + link, '', ['РУ p1.', 'РУ p2.'], None,
                )
                pending_articles_repo.move_to_published(
                    link=link,
                    telegraph_url='https://telegra.ph/x-' + link.rsplit('/', 1)[-1],
                    telegraph_path='x-' + link.rsplit('/', 1)[-1],
                    via_review=False,
                )
            return outcome, err
        return fake

    def _run_job_with_slots(self, n_slots, publish_fake):
        """Run ``job()`` with ``n_slots`` fixed slots at the frozen time,
        SOURCES neutered, sleep mocked, and ``_publish_with_retries`` faked.
        Returns the ``send_admin_notification`` mock for introspection.
        """
        slots = [self.FROZEN for _ in range(n_slots)]

        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return self.FROZEN

        # Own the base silencer so we can read the admin pings.
        self.notify_patcher.stop()
        mock_admin = patch('news_bot.send_admin_notification').start()
        try:
            with patch('news_bot.datetime') as mock_dt, \
                 patch('news_bot.time.sleep'), \
                 patch('news_bot.compute_fixed_slots',
                       return_value=(slots, 0)), \
                 patch('news_bot._publish_with_retries',
                       side_effect=publish_fake), \
                 patch('news_bot.SOURCES', [lambda notifier=None: []]):
                mock_dt.now.side_effect = fake_now
                mock_dt.combine = dt.datetime.combine
                mock_dt.strptime = dt.datetime.strptime
                news_bot.job()
        finally:
            patch.stopall()
            # Re-instate the base silencer so _IntegrationBase.tearDown's
            # notify_patcher.stop() has a live patch to stop.
            self.notify_patcher.start()
        return mock_admin

    @staticmethod
    def _recap_pings(mock_admin):
        return [
            c.args[0] for c in mock_admin.call_args_list
            if c.args and isinstance(c.args[0], str) and '[E034]' in c.args[0]
        ]

    def test_one_published_one_failed_recap_shows_failure(self):
        _seed_pending_row('http://example.com/ok', title='OK')
        _seed_pending_row('http://example.com/bad', title='BAD')
        fake = self._publish_side_effect({
            'http://example.com/ok': ('published', None),
            'http://example.com/bad': (
                'failed', RuntimeError('telegraph down: token=[REDACTED]'),
            ),
        })
        mock_admin = self._run_job_with_slots(2, fake)

        recaps = self._recap_pings(mock_admin)
        self.assertEqual(len(recaps), 1, f"expected one [E034], got: {recaps!r}")
        recap = recaps[0]
        self.assertIn("🟡", recap)
        self.assertIn("опубликовано 1/2", recap)
        self.assertIn("провалов: 1", recap)
        self.assertIn("провал: http://example.com/bad", recap)
        self.assertIn("telegraph down", recap)  # sanitized reason surfaced

    def test_held_slot_recap_shows_held(self):
        _seed_pending_row('http://example.com/held', title='HELD')
        fake = self._publish_side_effect({
            'http://example.com/held': ('held', None),
        })
        mock_admin = self._run_job_with_slots(1, fake)

        recaps = self._recap_pings(mock_admin)
        self.assertEqual(len(recaps), 1, f"expected one [E034], got: {recaps!r}")
        recap = recaps[0]
        self.assertIn("🟡", recap)
        self.assertIn("придержано 1", recap)
        self.assertIn("Claude недоступна", recap)

    def test_all_published_recap_is_compact(self):
        _seed_pending_row('http://example.com/p1', title='P1')
        _seed_pending_row('http://example.com/p2', title='P2')
        fake = self._publish_side_effect({
            'http://example.com/p1': ('published', None),
            'http://example.com/p2': ('published', None),
        })
        mock_admin = self._run_job_with_slots(2, fake)

        recaps = self._recap_pings(mock_admin)
        self.assertEqual(len(recaps), 1, f"expected one [E034], got: {recaps!r}")
        recap = recaps[0]
        self.assertIn("🟢", recap)
        self.assertIn("опубликовано 2/2", recap)
        self.assertNotIn("провал", recap)

    def test_quiet_no_slot_tick_sends_no_recap(self):
        # No pending rows, SOURCES empty → compute_fixed_slots(0, ...) yields
        # zero slots → publish loop never runs → recap must be skipped (the
        # intake E009 heartbeat already covers a quiet tick).
        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return self.FROZEN

        self.notify_patcher.stop()
        mock_admin = patch('news_bot.send_admin_notification').start()
        try:
            with patch('news_bot.datetime') as mock_dt, \
                 patch('news_bot.time.sleep'), \
                 patch('news_bot.SOURCES', [lambda notifier=None: []]):
                mock_dt.now.side_effect = fake_now
                mock_dt.combine = dt.datetime.combine
                mock_dt.strptime = dt.datetime.strptime
                news_bot.job()
        finally:
            patch.stopall()
            self.notify_patcher.start()

        self.assertEqual(self._recap_pings(mock_admin), [],
                         "quiet tick must not send a publish recap")

    def test_broken_recap_builder_does_not_break_tick(self):
        # A recap build error must be swallowed: publishing already happened,
        # so the tick must complete normally and the row stays published.
        _seed_pending_row('http://example.com/safe', title='SAFE')
        fake = self._publish_side_effect({
            'http://example.com/safe': ('published', None),
        })
        slots = [self.FROZEN]

        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return self.FROZEN

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.time.sleep'), \
             patch('news_bot.compute_fixed_slots', return_value=(slots, 0)), \
             patch('news_bot._publish_with_retries', side_effect=fake), \
             patch('news_bot.admin_alerts.alert_publish_recap',
                   side_effect=RuntimeError('recap boom')), \
             patch('news_bot.SOURCES', [lambda notifier=None: []]):
            mock_dt.now.side_effect = fake_now
            mock_dt.combine = dt.datetime.combine
            mock_dt.strptime = dt.datetime.strptime
            # Must NOT raise out of job().
            news_bot.job()

        # Publishing was unaffected by the recap failure.
        self.assertIsNotNone(
            pending_articles_repo.get_published('http://example.com/safe'),
        )

    def test_failed_reason_with_secret_is_sanitized_in_recap(self):
        # Pins the sanitization WIRING end-to-end: job() step (e) must feed the
        # recap the SANITIZED reason (``safe = sanitize_error_message(err)``),
        # never the raw ``str(err)``. We embed a value that we also set as the
        # ``TELEGRAM_BOT_TOKEN`` env var, so sanitize_error_message replaces it
        # with ``[REDACTED]``. The value is deliberately NOT a key/token shape,
        # so ONLY sanitize_error_message (exact env-value match) can scrub it —
        # not the regex ``_redact_text`` (which is bypassed here anyway because
        # ``send_admin_notification`` is mocked). If the collect line ever drops
        # the sanitize call (passes ``str(err)``), the raw secret leaks into the
        # admin [E034] ping and this test fails.
        _seed_pending_row('http://example.com/ok', title='OK')
        _seed_pending_row('http://example.com/bad', title='BAD')
        secret = 'TOPSECRET_bot_value_42'
        fake = self._publish_side_effect({
            'http://example.com/ok': ('published', None),
            'http://example.com/bad': (
                'failed',
                RuntimeError(f'telegraph API 500: bot token {secret} rejected'),
            ),
        })
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': secret}):
            mock_admin = self._run_job_with_slots(2, fake)

        recaps = self._recap_pings(mock_admin)
        self.assertEqual(len(recaps), 1, f"expected one [E034], got: {recaps!r}")
        recap = recaps[0]
        self.assertIn("провалов: 1", recap)
        self.assertIn("провал: http://example.com/bad", recap)
        # The secret was scrubbed before collection; the raw value must NOT
        # appear and the redaction marker MUST.
        self.assertNotIn(secret, recap)
        self.assertIn("[REDACTED]", recap)

    def test_moved_to_failed_after_three_strikes_recap_shows_subset(self):
        # Drive one row to ≥3 strikes within a single tick: the same row fails
        # on 3 consecutive slots → the real increment_attempt reaches 3 →
        # move_to_failed → moved_to_failed_count == 1. The recap must surface
        # the ≥3-strike subset tail, and the row must actually leave pending for
        # failed_articles (end-to-end wiring of the moved_to_failed signal).
        link = 'http://example.com/strikeout'
        _seed_pending_row(link, title='STRIKEOUT')
        fake = self._publish_side_effect({
            link: ('failed', RuntimeError('persistent publish failure')),
        })
        mock_admin = self._run_job_with_slots(3, fake)

        recaps = self._recap_pings(mock_admin)
        self.assertEqual(len(recaps), 1, f"expected one [E034], got: {recaps!r}")
        recap = recaps[0]
        self.assertIn("🟡", recap)
        # 3 failed attempts, exactly one moved to failed_articles (≥3 strikes).
        self.assertIn("провалов: 3 (снято после 3 промахов: 1)", recap)
        self.assertIn(f"провал: {link}", recap)
        # End-to-end wiring: the row left pending and landed in failed_articles.
        # ``_run_job_with_slots`` calls ``patch.stopall()`` (reverting the base
        # DB_FILE patch), so re-point the repo at the same tempfile DB to read
        # the post-job state — the file still exists until tearDown.
        with patch('news_bot.DB_FILE', self.db_path):
            self.assertEqual(pending_articles_repo.list_pending(), [])
            self.assertIsNotNone(pending_articles_repo.get_failed(link))


# ---------------------------------------------------------------------------
# Dedup-review callback decision logic (dedup-review-buttons Task 4).
# ---------------------------------------------------------------------------


class TestResolveDedupCallback(_IntegrationBase):
    """Every branch of ``news_bot.resolve_dedup_callback`` on a real
    tempfile SQLite DB (user-spec: the Telegram finger-press E2E is not
    automatable, so the pure decision core carries the full coverage).

    Return contract under test (fixed with Task 5's listener):
      * terminal outcome → ``(status_text, answer_text)``, both non-empty
        strings (listener edits the message + answers the callback);
      * ignored press (non-admin / non-numeric admin id) →
        ``(None, "")`` — listener must NOT edit the message.
    """

    ADMIN_ID = 424242

    def setUp(self):
        super().setUp()
        # _IntegrationBase patches TELEGRAM_ADMIN_ID to the non-numeric
        # '@admin'; these tests need the numeric-admin contract
        # (tech-spec Decision 5), so layer a numeric patch on top.
        self.numeric_admin_patcher = patch(
            'news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID))
        self.numeric_admin_patcher.start()

    def tearDown(self):
        self.numeric_admin_patcher.stop()
        super().tearDown()

    # -- helpers ----------------------------------------------------------

    def _stage_token(self, link, token='tok-test-1234'):
        pending_articles_repo.put_review_token(token, link)
        return token

    def _in_processed_news(self, link):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_news WHERE link=?", (link,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _insert_published(self, link):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review) "
                "VALUES (?, 'EN', 'RU', 'https://telegra.ph/x', '/x', "
                "        'autoevolution', 0)",
                (link,),
            )
            conn.commit()
        finally:
            conn.close()

    # -- auth gate --------------------------------------------------------

    def test_non_admin_press_ignored_no_state_change(self):
        link = 'https://example.com/dedup-a'
        _seed_pending_row(link)
        token = self._stage_token(link)

        with patch('news_bot.pending_repo.skip_pending') as mock_skip:
            status, answer = news_bot.resolve_dedup_callback(
                'cancel', token, self.ADMIN_ID + 1)

        self.assertIsNone(status)   # "do not edit the message" signal
        self.assertEqual(answer, "")
        mock_skip.assert_not_called()
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        # Token survives an ignored press — the admin can still act later.
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), link)

    def test_non_numeric_admin_id_fail_closed(self):
        link = 'https://example.com/dedup-failclosed'
        _seed_pending_row(link)
        token = self._stage_token(link)

        with patch('news_bot.TELEGRAM_ADMIN_ID', '@sunny413x'):
            status, answer = news_bot.resolve_dedup_callback(
                'cancel', token, self.ADMIN_ID)

        self.assertIsNone(status)
        self.assertEqual(answer, "")
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertFalse(self._in_processed_news(link))
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), link)

    # -- cancel branches --------------------------------------------------

    def test_cancel_pending_row_skips_and_returns_cancelled(self):
        link = 'https://example.com/dedup-cancel'
        _seed_pending_row(link)
        token = self._stage_token(link)

        status, answer = news_bot.resolve_dedup_callback(
            'cancel', token, self.ADMIN_ID)

        self.assertEqual(status, "✅ Отменено оператором")
        self.assertEqual(answer, "✅ Отменено оператором")
        self.assertIsNone(pending_articles_repo.get_pending(link))
        self.assertTrue(self._in_processed_news(link))
        # skip is NOT a publish (Decision 2).
        self.assertIsNone(pending_articles_repo.get_published(link))
        # Token consumed on the terminal outcome...
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))
        # ...so a second press of the same button is a safe stale no-op
        # (idempotence, Decision 9).
        status2, answer2 = news_bot.resolve_dedup_callback(
            'cancel', token, self.ADMIN_ID)
        self.assertEqual(status2, "⚠️ Кнопка устарела")
        self.assertEqual(answer2, "⚠️ Кнопка устарела")

    def test_cancel_then_slot_publish_does_not_publish(self):
        """User-spec: after the operator cancels, the article does not go
        out to the channel in its slot. Drives the slot-selection path the
        way the job loop does (head of ``list_pending()``) with the channel
        side-effect ``_fallback_publish`` stubbed.
        """
        cancelled = 'https://example.com/dedup-cancelled-slot'
        survivor = 'https://example.com/dedup-survivor'
        _seed_pending_row(cancelled, title='Cancelled')
        _seed_pending_row(survivor, title='Survivor')
        token = self._stage_token(cancelled)

        news_bot.resolve_dedup_callback('cancel', token, self.ADMIN_ID)

        # The cancelled link is gone from the publish queue entirely.
        queue_links = [r['link'] for r in pending_articles_repo.list_pending()]
        self.assertNotIn(cancelled, queue_links)
        self.assertIn(survivor, queue_links)

        # Replay the slot loop's per-slot selection (rows[0] of
        # list_pending) until the queue drains, with the publish
        # side-effect stubbed: the cancelled link must never be handed
        # to _fallback_publish.
        with patch('news_bot._fallback_publish') as mock_pub:
            mock_pub.side_effect = lambda row, via_review=False: (
                pending_articles_repo.skip_pending(row['link']))
            while True:
                rows = pending_articles_repo.list_pending()
                if not rows:
                    break
                news_bot._fallback_publish(rows[0])

        published_links = [
            call.args[0]['link'] for call in mock_pub.call_args_list]
        self.assertNotIn(cancelled, published_links)
        self.assertEqual(published_links, [survivor])
        self.assertIsNone(pending_articles_repo.get_published(cancelled))

    def test_cancel_published_returns_already_published(self):
        link = 'https://example.com/dedup-published'
        self._insert_published(link)
        token = self._stage_token(link)

        with patch('news_bot.pending_repo.skip_pending') as mock_skip:
            status, answer = news_bot.resolve_dedup_callback(
                'cancel', token, self.ADMIN_ID)

        self.assertEqual(status, "⚠️ Уже опубликовано, отменить нельзя")
        self.assertEqual(answer, "⚠️ Уже опубликовано, отменить нельзя")
        mock_skip.assert_not_called()
        # The channel post / published row is untouched.
        self.assertIsNotNone(pending_articles_repo.get_published(link))
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    def test_cancel_missing_returns_unavailable(self):
        link = 'https://example.com/dedup-vanished'
        token = self._stage_token(link)  # token valid, article gone

        status, answer = news_bot.resolve_dedup_callback(
            'cancel', token, self.ADMIN_ID)

        self.assertEqual(status, "⚠️ Статья уже недоступна")
        self.assertEqual(answer, "⚠️ Статья уже недоступна")
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    # -- keep / stale -----------------------------------------------------

    def test_keep_returns_kept_no_state_change(self):
        link = 'https://example.com/dedup-keep'
        _seed_pending_row(link)
        token = self._stage_token(link)

        status, answer = news_bot.resolve_dedup_callback(
            'keep', token, self.ADMIN_ID)

        self.assertEqual(status, "👍 Оставлено")
        self.assertEqual(answer, "👍 Оставлено")
        # Queue untouched — the article publishes in its slot as usual.
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertFalse(self._in_processed_news(link))
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    def test_stale_token_returns_expired(self):
        link = 'https://example.com/dedup-stale'
        _seed_pending_row(link)
        # No token staged — simulates bot restart / already-consumed token.
        status, answer = news_bot.resolve_dedup_callback(
            'cancel', 'tok-unknown-9999', self.ADMIN_ID)

        self.assertEqual(status, "⚠️ Кнопка устарела")
        self.assertEqual(answer, "⚠️ Кнопка устарела")
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertFalse(self._in_processed_news(link))


if __name__ == '__main__':
    unittest.main()
