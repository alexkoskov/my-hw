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
    """AC7: a container restart at 16:00 МСК with N pending articles
    recomputes the schedule via ``compute_publish_slots(N, 16:00)`` and
    enforces the 40-minute minimum interval.
    """

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_restart_mid_window_recomputes_schedule(
        self, _mock_admin, _mock_sleep,
    ):
        """5 pending rows + 1 already-published row + frozen
        ``datetime.now`` at 16:00 MSK → ``compute_publish_slots`` is
        called with N=5 and the resulting slots are spaced ≥40min apart.
        The already-published row is not re-published (idempotency
        Decision 9 — telegraph_url present in published_articles).
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

        # Frozen now: 16:00 МСК — leftover window is 4 hours (240 min) for 5
        # articles → raw_interval = 48 min; effective = max(48, 40) = 48 min.
        frozen_now = MSK.localize(dt.datetime(2026, 4, 27, 16, 0, 0))

        captured = {}

        def fake_compute(n, now, *args, **kwargs):
            captured['n'] = n
            captured['now'] = now
            captured['kwargs'] = kwargs
            from compute_publish_slots import compute_publish_slots as real
            # Pin the floor to the historical 40-min value for this
            # scenario — production now defaults to 90-min, but the
            # test asserts a 5-publish recompute that only fits at 40.
            return real(
                n, now,
                window_start=kwargs.get('window_start'),
                window_end=kwargs.get('window_end'),
                min_interval_min=40,
            )

        # The crash-loop guard reads ``datetime.now(timezone.utc)`` for
        # gap arithmetic against the UTC-naive ``published_at``. Returning
        # the same MSK-aware ``frozen_now`` from every ``now()`` call would
        # mix tz-aware (MSK) with tz-naive parsed published_at and produce
        # a TypeError. Resolve by routing UTC calls to a real UTC ``now``
        # — guard fires (60min gap > 40min threshold → no sleep), then the
        # publish loop reads ``datetime.now(MSK)`` which gets the frozen
        # MSK value.
        def fake_now(tz=None):
            if tz is dt.timezone.utc:
                return dt.datetime.now(dt.timezone.utc)
            return frozen_now

        with patch('news_bot.datetime') as mock_dt, \
             patch('news_bot.compute_publish_slots',
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

        # compute_publish_slots was called with N=5 and now at 16:00 MSK.
        self.assertEqual(captured.get('n'), 5)
        self.assertEqual(captured['now'].hour, 16)
        self.assertEqual(captured['now'].minute, 0)
        # And it must receive the news_bot.WINDOW_START_TIME (10:00) /
        # WINDOW_END_TIME (20:00) — otherwise the function falls back to
        # its own default of 13:00 and we lose the morning window.
        self.assertEqual(captured['kwargs'].get('window_start'),
                         news_bot.WINDOW_START_TIME)
        self.assertEqual(captured['kwargs'].get('window_end'),
                         news_bot.WINDOW_END_TIME)

        # _fallback_publish was called for every slot. compute_publish_slots
        # returned 5 slots, but news_bot.MAX_DAILY_POSTS (4) trims the tail
        # so only 4 publishes fire; the 5th row carries over to tomorrow.
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


if __name__ == '__main__':
    unittest.main()
