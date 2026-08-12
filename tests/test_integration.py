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
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz

import _llm_common
import dom_blocks
import news_bot
import outage_state
import pending_articles_repo
import telegraph_publisher
from claude_transcreation import ClaudeOutageError, ClaudeTranscreationError


MSK = pytz.timezone("Europe/Moscow")
UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Shared scaffolding — tempfile DB + token/channel patches.
# ---------------------------------------------------------------------------


class _IntegrationBase(unittest.TestCase):
    """Tempfile DB + safe env-var stubs, shared by every test class below.

    Cleanup uses ``addCleanup``, NOT a ``tearDown`` chain (2026-07-25 —
    this was a real intermittent-flake source). The old ``tearDown`` stopped
    seven patchers with seven sequential statements: the FIRST one to raise
    aborted the rest, and ``notify_patcher`` sat fifth in that line. Leaking
    it left ``news_bot.send_admin_notification`` replaced by a MagicMock for
    the REST OF THE SESSION, so unrelated tests that assert on the real
    function (``test_news_bot_dispatcher``, ``test_no_token_leak_in_logs``)
    failed far away from the actual culprit — the classic "fails only in
    combination, never in isolation" signature.

    ``addCleanup`` fixes it structurally: every registered cleanup runs even
    if an earlier one raises, and each is registered the instant its patch
    starts, so a failure part-way through ``setUp`` cannot leak the patches
    that already started either.

    Ordering contract for subclasses: cleanups run LIFO, so a subclass that
    stops ``notify_patcher`` in its own ``setUp`` and re-arms it with
    ``self.addCleanup(self.notify_patcher.start)`` is correctly paired —
    its ``start`` runs BEFORE this class's ``stop``. The stop below is also
    tolerant of an already-stopped patcher, so the older
    ``tearDown``-based subclasses in this file keep working unchanged.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.addCleanup(self._unlink_db)
        # ``patch('news_bot.DB_FILE', ...)`` reaches outage_state too —
        # outage_state._connect() reads ``news_bot.DB_FILE`` at call time.
        self.db_patcher = self._start_patch(
            patch('news_bot.DB_FILE', self.db_path))
        news_bot.init_db()
        self.token_patcher = self._start_patch(
            patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token'))
        self.channel_patcher = self._start_patch(
            patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel'))
        self.admin_patcher = self._start_patch(
            patch('news_bot.TELEGRAM_ADMIN_ID', '@admin'))
        # Silence admin notifications by default. Individual tests stop
        # this patch and introspect the mock when they care.
        self.notify_patcher = patch('news_bot.send_admin_notification')
        self.mock_notify = self.notify_patcher.start()
        self.addCleanup(self._stop_quietly, self.notify_patcher)
        # Mattel source returns nothing unless a test overrides it.
        self.mattel_patcher = self._start_patch(
            patch('news_bot.fetch_mattel_news', return_value=[]))
        # Orangetrack source returns nothing unless a test overrides it
        # (avoids real network calls to orangetrackdiecast.com). Patch
        # SOURCES to a narrower list — patching the function attribute
        # alone doesn't work because SOURCES holds the original reference.
        self.sources_patcher = self._start_patch(patch(
            'news_bot.SOURCES',
            [news_bot._fetch_rss_entries, news_bot._fetch_mattel_entries],
        ))

    def _start_patch(self, patcher):
        """Start ``patcher`` and register its stop immediately."""
        patcher.start()
        self.addCleanup(self._stop_quietly, patcher)
        return patcher

    @staticmethod
    def _stop_quietly(patcher):
        """Stop a patcher, tolerating one a subclass already stopped.

        Subclasses in this file legitimately stop ``notify_patcher`` inside
        their own ``setUp`` so a per-test ``@patch`` can own the name. If
        such a test errors before re-arming it, the unpaired stop here would
        raise and — before ``addCleanup`` — take the remaining cleanups with
        it. Swallowing only this specific condition keeps cleanup total.
        """
        try:
            patcher.stop()
        except RuntimeError:
            # "stop called on unstarted patcher" — already stopped.
            pass

    def _unlink_db(self):
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
        self.sleep_patcher = self._start_patch(patch('news_bot.time.sleep'))
        # Make sure the publish loop is a no-op for prep-only tests so we
        # only assert what landed in pending_articles.
        self.fallback_patcher = self._start_patch(
            patch('news_bot._fallback_publish'))


class TestIntegrationBaseCleanupIsolation(unittest.TestCase):
    """Regression guard for the 2026-07-25 cross-test leak.

    ``_IntegrationBase`` patches seven module attributes, and several
    subclasses deliberately stop ``notify_patcher`` in their own ``setUp``
    so a per-test ``@patch`` can own the name. Under the old sequential
    ``tearDown`` a single unpaired/raising stop aborted the remaining
    stops, leaving ``news_bot.send_admin_notification`` as a MagicMock for
    the rest of the session — which then failed unrelated tests that
    assert on the REAL function (``test_news_bot_dispatcher``,
    ``test_no_token_leak_in_logs``), far away from the culprit.

    These tests run a deliberately broken inner TestCase and assert the
    module is left exactly as it was found.
    """

    ATTRS = ('send_admin_notification', 'DB_FILE', 'TELEGRAM_BOT_TOKEN',
             'TELEGRAM_CHANNEL_ID', 'TELEGRAM_ADMIN_ID', 'SOURCES',
             'fetch_mattel_news')

    def _snapshot(self):
        return {name: getattr(news_bot, name) for name in self.ATTRS}

    def _assert_restored(self, before):
        for name, value in before.items():
            with self.subTest(attr=name):
                self.assertIs(
                    getattr(news_bot, name), value,
                    f"news_bot.{name} leaked out of the test case — "
                    f"every later test in the session now sees a stub",
                )

    def test_cleanup_is_total_when_a_test_errors_after_stopping_a_patcher(self):
        before = self._snapshot()

        class _Leaky(_IntegrationBase):
            def setUp(inner):
                super().setUp()
                # The pattern used by TestContentGateIntake &co.
                inner.notify_patcher.stop()

            def runTest(inner):
                raise RuntimeError('boom mid-test')

        result = unittest.TestResult()
        _Leaky().run(result)
        self.assertTrue(
            result.errors, "inner test was supposed to error out")
        self._assert_restored(before)

    def test_cleanup_is_total_when_setup_itself_raises_part_way(self):
        before = self._snapshot()

        class _BrokenSetUp(_IntegrationBase):
            def setUp(inner):
                super().setUp()
                raise RuntimeError('boom during setUp')

            def runTest(inner):
                pass

        result = unittest.TestResult()
        _BrokenSetUp().run(result)
        self.assertTrue(result.errors)
        self._assert_restored(before)

    def test_a_subclass_re_arming_via_addcleanup_pairs_correctly(self):
        """LIFO ordering contract: a subclass that stops ``notify_patcher``
        in ``setUp`` and re-arms it with ``addCleanup`` must end up with
        the patch STOPPED, not left running."""
        before = self._snapshot()

        class _ReArming(_IntegrationBase):
            def setUp(inner):
                super().setUp()
                inner.notify_patcher.stop()
                inner.addCleanup(inner.notify_patcher.start)

            def runTest(inner):
                pass

        result = unittest.TestResult()
        _ReArming().run(result)
        self.assertEqual((result.errors, result.failures), ([], []))
        self._assert_restored(before)


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
        # Soft flag DEFERS publication by 24h (2026-07-28): the row is
        # staged but withheld from the publishable queue, so the «🚫 Не
        # публиковать» button has a real window. It had none before —
        # on 2026-07-28 the [E014] ping and the publish landed seven
        # seconds apart. Silence still PUBLISHES: the row reappears on
        # its own once the delay elapses (see test_deferred_row_*).
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(pending_articles_repo.list_pending(), [])
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
        # Soft flag DEFERS publication by 24h (2026-07-28): the row is
        # staged but withheld from the publishable queue, so the «🚫 Не
        # публиковать» button has a real window. It had none before —
        # on 2026-07-28 the [E014] ping and the publish landed seven
        # seconds apart. Silence still PUBLISHES: the row reappears on
        # its own once the delay elapses (see test_deferred_row_*).
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(pending_articles_repo.list_pending(), [])
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
        # Soft flag DEFERS publication by 24h (2026-07-28): the row is
        # staged but withheld from the publishable queue, so the «🚫 Не
        # публиковать» button has a real window. It had none before —
        # on 2026-07-28 the [E014] ping and the publish landed seven
        # seconds apart. Silence still PUBLISHES: the row reappears on
        # its own once the delay elapses (see test_deferred_row_*).
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(pending_articles_repo.list_pending(), [])
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
    def test_soft_flag_defers_publication_by_a_day(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The [E014] soft flag stamps `publish_after` ~24h ahead.

        Pins the DURATION, not merely "some defer": the whole point is that
        the operator gets a usable window. On 2026-07-28 the ping and the
        publish were seven seconds apart and the cancel press at 10:14 got
        «Уже опубликовано, отменить нельзя».
        """
        self._seed_published(
            'http://t-hunted.example/existing',
            {'strict': [], 'brands': [],
             'series': ['k-pop demon hunters'],
             'pairs': ['*|k-pop demon hunters|B']},
            source='t-hunted',
        )
        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'K-Pop Demon Hunters joins the Hot Wheels lineup',
            'subtitle': '',
            'paragraphs': ['The K-Pop Demon Hunters tie-in is here.',
                           'No specific casting was announced.'],
            'images': [],
        }

        news_bot.job()

        row = pending_articles_repo.get_pending(new_link)
        self.assertIsNotNone(row)
        stamped = row.get('publish_after')
        self.assertIsNotNone(stamped, "soft flag must stamp publish_after")
        delta = (
            dt.datetime.strptime(stamped, '%Y-%m-%d %H:%M:%S')
            .replace(tzinfo=dt.timezone.utc)
            - dt.datetime.now(dt.timezone.utc)
        )
        self.assertGreater(delta, dt.timedelta(hours=23))
        self.assertLess(delta, dt.timedelta(hours=25))

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_unflagged_article_is_not_deferred(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Ordinary articles keep publishing the same day.

        The defer must be scoped to suspected duplicates — a blanket delay
        would silently slow the whole channel by a day, which is NOT what was
        asked for.
        """
        new_link = 'http://autoevolution.example/plain'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = {
            'title': 'Toyota 4Runner joins the mainline',
            'subtitle': '',
            'paragraphs': ['A 2018 Toyota 4Runner casting arrives.',
                           'It ships in the next case.'],
            'images': [],
        }

        news_bot.job()

        row = pending_articles_repo.get_pending(new_link)
        self.assertIsNotNone(row)
        self.assertIsNone(row.get('publish_after'))
        self.assertEqual(pending_articles_repo.count_pending(), 1)

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
        # Soft flag DEFERS publication by 24h (2026-07-28): the row is
        # staged but withheld from the publishable queue, so the «🚫 Не
        # публиковать» button has a real window. It had none before —
        # on 2026-07-28 the [E014] ping and the publish landed seven
        # seconds apart. Silence still PUBLISHES: the row reappears on
        # its own once the delay elapses (see test_deferred_row_*).
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(pending_articles_repo.list_pending(), [])
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
    def test_broad_line_prose_comention_does_not_soft_flag(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """PROD REGRESSION 2026-07-28 — the [E014] false flag, pinned at the
        GATE, not just at ``shares_pair``.

        Two unrelated articles, neither with an extractable model (t-hunted's
        Lotus yields a brand-only token; autoevolution's Lincoln is outside the
        36-brand lexicon), that merely CO-MENTION a broad recurrent line — and
        on the autoevolution side "pop culture" is ordinary prose, not a line
        name. Before the theme-only precision fix both degraded to
        ``*|pop culture|B`` and the gate fired an [E014].

        This also exercises a fingerprint shape that could not exist before that
        fix — ``strict == []`` and ``pairs == []`` with a NON-empty ``series``.
        It skips Rule 1 (no pairs), misses the AC8 short-circuit (``series`` is
        non-empty) and lands in the set-overlap backstop, which is a no-op on an
        empty ``strict``. Expected end state: the article publishes with NO ping
        of any kind.
        """
        # Existing published row — the real autoevolution-side body, fingerprint
        # built by the ACTUAL extractor (a hand-written fingerprint would not
        # prove the extractor still produces this shape).
        autoevo_article = {
            'title': 'First Hot Wheels Super Treasure Hunt for 2027 Is a Lincoln',
            'subtitle': '',
            'paragraphs': [
                'The Lincoln Continental Mark IV is a pop culture icon that '
                'Hot Wheels has finally cast in Super Treasure Hunt form.',
            ],
            'images': [],
        }
        autoevo_fp = news_bot.model_extractor.extract_fingerprint(autoevo_article)
        self.assertEqual(autoevo_fp.get('strict'), [])
        self.assertIn('pop culture', autoevo_fp.get('series') or [])
        self.assertEqual(autoevo_fp.get('pairs'), [])
        self._seed_published(
            'http://autoevolution.example/lincoln-sth',
            autoevo_fp,
            source='autoevolution',
        )

        new_link = 'http://t-hunted.example/pop-culture-lote'
        t_hunted_article = {
            'title': 'Mais um novo lote da série Pop Culture de 2026, e com novidade',
            'subtitle': '',
            'paragraphs': [
                'Uma das séries mais colecionadas pelos fãs de Hot Wheels é a '
                'Pop Culture, com suas réplicas de veículos que apareceram em '
                'filmes, séries de TV, desenhos ou jogos, e dessa vez tem um '
                'novo lote com uma novidade: o Lotus Esprit Turbo do 007 com '
                'esquis na traseira.',
            ],
            'images': [],
        }
        # The new-article side of the reachable-but-previously-impossible shape.
        probe = news_bot.model_extractor.extract_fingerprint(t_hunted_article)
        self.assertEqual(probe.get('strict'), [])
        self.assertIn('pop culture', probe.get('series') or [])
        self.assertEqual(probe.get('pairs'), [])

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = t_hunted_article

        news_bot.job()

        # Article passes the gate untouched and is staged.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertIsNotNone(pending_articles_repo.get_pending(new_link))
        # And the operator is not pinged at all — not a flag, not a block, and
        # not a degraded-mode fallback.
        for code in ('[E014]', '[E015]', '[E016]'):
            self.assertFalse(
                any(code in (c.args[0] if c.args else '')
                    for c in mock_admin.call_args_list),
                f"unexpected {code}: {mock_admin.call_args_list}",
            )

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

    def test_cancel_race_published_between_check_and_skip(self):
        """Slot-boundary race (security review round 1): the row is still
        pending at the ``get_pending`` check, but the slot loop publishes
        it before ``skip_pending`` runs — the skip silently no-ops. The
        function must then answer the honest «уже опубликовано», not a
        misleading «отменено» (user-spec promise).
        """
        link = 'https://example.com/dedup-race'
        _seed_pending_row(link)
        token = self._stage_token(link)

        real_skip = pending_articles_repo.skip_pending

        def _publish_wins_then_skip(l):
            # Simulate the slot publish landing first: row leaves pending
            # and appears in published_articles; the real skip_pending
            # then no-ops on the missing pending row.
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "DELETE FROM pending_articles WHERE link=?", (l,))
                conn.commit()
            finally:
                conn.close()
            self._insert_published(l)
            real_skip(l)

        with patch('news_bot.pending_repo.skip_pending',
                   side_effect=_publish_wins_then_skip):
            status, answer = news_bot.resolve_dedup_callback(
                'cancel', token, self.ADMIN_ID)

        self.assertEqual(status, "⚠️ Уже опубликовано, отменить нельзя")
        self.assertEqual(answer, "⚠️ Уже опубликовано, отменить нельзя")
        # Channel post untouched; terminal outcome consumes the token.
        self.assertIsNotNone(pending_articles_repo.get_published(link))
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

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

    # -- unknown action (defensive branch) --------------------------------

    def test_unknown_action_safe_fallback_token_not_consumed(self):
        """Defensive branch (code+test review round 1): the Task 5
        listener maps ``c → 'cancel'`` / ``k → 'keep'`` before calling,
        so a raw letter or garbage action should never arrive — but if
        it does, the function answers the safe stale text WITHOUT
        consuming the still-valid token and without touching state.
        """
        link = 'https://example.com/dedup-unknown-action'
        _seed_pending_row(link)
        token = self._stage_token(link)

        for bad_action in ('c', 'k', 'nuke', '', None):
            with patch('news_bot.pending_repo.skip_pending') as mock_skip:
                status, answer = news_bot.resolve_dedup_callback(
                    bad_action, token, self.ADMIN_ID)
            self.assertEqual(status, "⚠️ Кнопка устарела",
                             f"action={bad_action!r}")
            self.assertEqual(answer, "⚠️ Кнопка устарела")
            mock_skip.assert_not_called()

        # No state change; token survives for the real button press.
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertFalse(self._in_processed_news(link))
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), link)

    # -- keep / stale -----------------------------------------------------

    def test_keep_lifts_the_soft_flag_deferral(self):
        # The [E014] alert promises «выпустит её в ближайший слот». Until
        # 2026-08-11 'keep' only answered «Оставлено» and left publish_after
        # alone, so the press changed nothing an operator could observe: the
        # article still waited out the full 24 h. Verified on prod — a press
        # at 10:02 on 11 Aug, and the article published 12 Aug at 15:00 MSK,
        # exactly when the window elapsed.
        link = 'https://example.com/dedup-keep'
        _seed_pending_row(link)
        pending_articles_repo.defer_publish(link, '2099-01-01 00:00:00')
        token = self._stage_token(link)

        status, answer = news_bot.resolve_dedup_callback(
            'keep', token, self.ADMIN_ID)

        self.assertEqual(status, "👍 Оставлено — выйдет в ближайший слот")
        self.assertEqual(answer, status)
        # The deferral is gone, so the next slot can pick the row up...
        self.assertIsNone(pending_articles_repo.get_pending(link)['publish_after'])
        self.assertEqual(pending_articles_repo.count_deferred(), 0)
        self.assertIn(link, [r['link'] for r in pending_articles_repo.list_pending()])
        # ...and nothing else moved: keeping is not publishing.
        self.assertFalse(self._in_processed_news(link))
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    def test_keep_on_an_undeferred_row_is_a_harmless_no_op(self):
        # Not every [E014] row is deferred — the soft flag only defers when the
        # overlap is below the auto-block threshold. Keep must still answer
        # cleanly instead of reporting a release that never happened.
        link = 'https://example.com/dedup-keep-plain'
        _seed_pending_row(link)
        token = self._stage_token(link)

        status, answer = news_bot.resolve_dedup_callback(
            'keep', token, self.ADMIN_ID)

        self.assertEqual(status, "👍 Оставлено — выйдет в ближайший слот")
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertFalse(self._in_processed_news(link))

    def test_keep_does_not_promise_a_slot_to_a_held_article(self):
        # Both marks land in the SAME insert — the dedup soft flag writes
        # publish_after while a content gate writes hold_reason, and the flag
        # branch never consults hold_markers. Clearing the deferral leaves the
        # row parked (list_pending demands hold_reason IS NULL), so answering
        # «выйдет в ближайший слот» would be exactly the lie this fix removes.
        link = 'https://example.com/dedup-keep-held'
        _seed_pending_row(link)
        pending_articles_repo.defer_publish(link, '2099-01-01 00:00:00')
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE pending_articles SET hold_reason=? WHERE link=?",
                         ('постер', link))
            conn.commit()
        finally:
            conn.close()
        token = self._stage_token(link)

        status, answer = news_bot.resolve_dedup_callback(
            'keep', token, self.ADMIN_ID)

        self.assertEqual(status, "👍 Оставлено — но статья ещё на утверждении [E036]")
        self.assertEqual(answer, status)
        # The deferral IS lifted — approving the hold later must publish it
        # without a second wait — but the row is still not publishable now.
        self.assertIsNone(pending_articles_repo.get_pending(link)['publish_after'])
        self.assertNotIn(link, [r['link'] for r in pending_articles_repo.list_pending()])

    def test_keep_never_resurrects_a_row_that_left_the_queue(self):
        # Mirror of the 'cancel' branch: the row can be published or cancelled
        # between the alert and the press. Answer honestly, write nothing.
        for label, published in (('published', True), ('gone', False)):
            with self.subTest(label):
                link = f'https://example.com/dedup-keep-{label}'
                token = self._stage_token(link)
                if published:
                    self._insert_published(link)

                status, answer = news_bot.resolve_dedup_callback(
                    'keep', token, self.ADMIN_ID)

                expected = ("⚠️ Уже опубликовано" if published
                            else "⚠️ Статья уже недоступна")
                self.assertEqual(status, expected)
                self.assertEqual(answer, expected)
                self.assertIsNone(pending_articles_repo.get_pending(link))

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


class TestInFlightCancelGuard(_IntegrationBase):
    """Audit CA-1a: an operator cancel that lands while THIS article's own
    publish is mid-flight (LLM → Telegraph take minutes) must abort the
    publish before the Telegram teaser — the last irreversible step. The
    row is deleted by ``skip_pending`` inside ``resolve_dedup_callback``;
    ``_fallback_publish`` re-checks the pending row right before the
    teaser send and honours the cancel: no channel post, no strike
    (success-without-publish, mirroring the idempotency guard's return).
    """

    RU_RESULT = {
        'title': 'РуЗаголовок',
        'subtitle': 'РуПодзаголовок',
        'paragraphs': ['Русский абзац.'],
        'blocks': None,
    }

    def test_cancel_mid_publish_aborts_before_teaser(self):
        link = 'https://example.com/inflight-cancel'
        _seed_pending_row(link)
        row = pending_articles_repo.get_pending(link)

        def _cancel_lands_then_publish(**kwargs):
            # The operator presses «🚫 Не публиковать» while Telegraph is
            # being created: resolve's cancel branch deletes the pending
            # row. The Telegraph page itself still gets made (harmless
            # orphan) — the guard sits AFTER this step, BEFORE the teaser.
            pending_articles_repo.skip_pending(link)
            return 'https://telegra.ph/inflight-cancel'

        with patch('news_bot.transcreate_via_claude',
                   return_value=dict(self.RU_RESULT)), \
                patch('news_bot.telegraph_publisher.publish_article',
                      side_effect=_cancel_lands_then_publish), \
                patch('news_bot.time.sleep'), \
                patch('news_bot.send_telegraph_teaser') as mock_teaser:
            with self.assertLogs('news_bot', level='INFO') as logs:
                result = news_bot._fallback_publish(row)

        # Success-without-publish: True (no strike in the slot loop) but
        # the channel teaser was never sent and nothing was "published".
        self.assertTrue(result)
        mock_teaser.assert_not_called()
        self.assertIsNone(pending_articles_repo.get_published(link))
        self.assertIsNone(pending_articles_repo.get_pending(link))
        # The abort is visible in the log for the operator.
        self.assertTrue(
            any('[review-cancel]' in line and link in line
                for line in logs.output),
            f"expected a [review-cancel] abort line; got {logs.output!r}")

    def test_publish_proceeds_when_row_still_pending(self):
        """Positive control: with no cancel racing it, the same publish
        passes the pre-teaser guard and completes normally."""
        link = 'https://example.com/inflight-nocancel'
        _seed_pending_row(link)
        row = pending_articles_repo.get_pending(link)

        with patch('news_bot.transcreate_via_claude',
                   return_value=dict(self.RU_RESULT)), \
                patch('news_bot.telegraph_publisher.publish_article',
                      return_value='https://telegra.ph/inflight-nocancel'), \
                patch('news_bot.time.sleep'), \
                patch('news_bot.send_telegraph_teaser',
                      return_value=True) as mock_teaser:
            result = news_bot._fallback_publish(row)

        self.assertTrue(result)
        mock_teaser.assert_called_once()
        self.assertIsNotNone(pending_articles_repo.get_published(link))
        self.assertIsNone(pending_articles_repo.get_pending(link))


class TestDedupReviewButtons(_PrepPhaseBase):
    """Flag-gated inline review keyboard on the [E014] soft-dupe admin ping
    (dedup-review-buttons Task 3).

    Contract under test:
      * ``news_bot.REVIEW_BUTTONS_ENABLED`` is True → the E014 send mints a
        token, persists ``token→link`` via ``put_review_token`` BEFORE the
        send, and passes ``reply_markup`` with exactly two buttons
        (``dd:c:<token>`` / ``dd:k:<token>``, cancel first).
      * Flag False (the default) → the E014 send is byte-identical to the
        pre-feature behaviour: no ``reply_markup``, no token minted, no
        ``review_token:*`` row in ``bot_state``.
      * Only E014 carries the keyboard — sibling alerts in the same
        ``job()`` run (E015 hard-block, quiet-day E009, ...) never get one.

    Helpers are REUSED from ``TestCrossSourceDedup`` by plain-function
    assignment instead of subclassing it — inheriting would re-register its
    20 test methods under this class and run them all twice.
    """

    _seed_published = TestCrossSourceDedup._seed_published
    _make_entry = TestCrossSourceDedup._make_entry

    #: Broad (tier B) pair fingerprint — soft-flags (E014) the matching
    #: article, same seed as ``test_broad_pair_soft_flag_is_terminal``.
    SOFT_FP = {
        'strict': ['toyota supra'],
        'brands': ['toyota'],
        'series': ['car culture'],
        'pairs': ['toyota supra|car culture|B'],
    }
    SOFT_ARTICLE = {
        'title': 'Toyota Supra joins the Car Culture line',
        'subtitle': '',
        'paragraphs': ['A new release.'],
        'images': [],
    }

    #: Distinctive (|D) pair fingerprint — hard-blocks (E015) the matching
    #: article, same seed as ``test_distinctive_pair_cross_source_blocks``.
    BLOCK_FP = {
        'strict': ['porsche 911'],
        'brands': ['porsche'],
        'series': ['k-pop demon hunters'],
        'pairs': ['porsche 911|k-pop demon hunters|D'],
    }
    BLOCK_ARTICLE = {
        'title': 'Porsche 911 gets a K-Pop Demon Hunters makeover',
        'subtitle': '',
        'paragraphs': ['More photos inside.'],
        'images': [],
    }

    def setUp(self):
        super().setUp()
        # Same dance as TestCrossSourceDedup: stop the base silencer so the
        # per-test ``send_admin_notification`` patch OWNS the name.
        self.notify_patcher.stop()
        # SEC-A8-1: the send site now gates on _review_listener_enabled()
        # (flag + bot token + numeric admin), not the bare flag — the base
        # class patches TELEGRAM_ADMIN_ID to the non-numeric '@admin', so
        # flag-ON tests need a numeric admin id for the keyboard to mint.
        self.numeric_admin_patcher = patch(
            'news_bot.TELEGRAM_ADMIN_ID', '424242')
        self.numeric_admin_patcher.start()

    def tearDown(self):
        self.numeric_admin_patcher.stop()
        # Re-start the base silencer so _IntegrationBase.tearDown's
        # notify_patcher.stop() has something to stop.
        self.notify_patcher.start()
        super().tearDown()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _e014_calls(mock_admin):
        return [
            c for c in mock_admin.call_args_list
            if '[E014]' in (c.args[0] if c.args else '')
        ]

    def _review_token_keys(self):
        """All ``review_token:*`` keys currently in ``bot_state``."""
        conn = sqlite3.connect(self.db_path)
        try:
            return [
                r[0] for r in conn.execute(
                    "SELECT key FROM bot_state "
                    "WHERE key LIKE 'review_token:%'",
                ).fetchall()
            ]
        finally:
            conn.close()

    # -- tests ------------------------------------------------------------

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_flag_on_e014_send_includes_review_keyboard(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Flag ON: the soft-flag run sends E014 WITH ``reply_markup`` —
        exactly two buttons ``dd:c:<token>`` / ``dd:k:<token>`` (cancel
        first) — and the token is persisted BEFORE the send: an
        AT-CALL-TIME probe inside the ``send_admin_notification`` mock
        resolves ``get_review_token_link(token)`` to the flagged link the
        moment the send happens (test-review round 1: a post-``job()``
        assertion alone stays green if the put is moved AFTER the send,
        missing the button-tap-races-unwritten-token production race)."""
        self._seed_published(
            'http://t-hunted.example/existing', self.SOFT_FP,
            source='t-hunted',
        )
        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = dict(self.SOFT_ARTICLE)

        # Ordering probe: runs INSIDE the send call, i.e. before job()
        # continues past it. For the E014 (the only call carrying a
        # keyboard) the token extracted from callback_data must ALREADY
        # resolve in bot_state — pinning put_review_token-before-send.
        probe_hits = []

        def _assert_token_written_at_send(message, **kwargs):
            kb_probe = kwargs.get('reply_markup')
            if kb_probe is None:
                return True  # other pings (E009, ...) — not under test
            cd = kb_probe.inline_keyboard[0][0].callback_data
            self.assertTrue(cd.startswith('dd:c:'), cd)
            probe_token = cd[len('dd:c:'):]
            self.assertEqual(
                pending_articles_repo.get_review_token_link(probe_token),
                new_link,
                "token not persisted BEFORE send_admin_notification — "
                "a button tap could race an unwritten token",
            )
            probe_hits.append(probe_token)
            return True

        mock_admin.side_effect = _assert_token_written_at_send

        news_bot.job()

        # The probe actually fired for the keyboard-carrying send —
        # otherwise the at-call-time assertion above would be vacuous.
        self.assertEqual(len(probe_hits), 1, probe_hits)

        e014 = self._e014_calls(mock_admin)
        self.assertEqual(
            len(e014), 1,
            f"expected exactly one E014; got {mock_admin.call_args_list!r}",
        )
        kb = e014[0].kwargs.get('reply_markup')
        self.assertIsNotNone(kb, "E014 send is missing reply_markup")
        buttons = [b for row in kb.inline_keyboard for b in row]
        self.assertEqual(len(buttons), 2)
        cds = [b.callback_data for b in buttons]
        # Cancel FIRST, keep second — order is part of the Task 2 contract.
        self.assertTrue(cds[0].startswith('dd:c:'), cds)
        token = cds[0][len('dd:c:'):]
        self.assertTrue(token, "empty token in callback_data")
        self.assertEqual(cds, [f'dd:c:{token}', f'dd:k:{token}'])
        # Same token the at-send probe saw, still resolvable after job().
        self.assertEqual(probe_hits, [token])
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), new_link,
        )
        # Advice matches reality: buttons ARE attached, so «Что сделать»
        # names them (and never the archived hw_review.py CLI).
        text = e014[0].args[0]
        self.assertIn('🚫 Не публиковать', text)
        self.assertIn('👍 Оставить', text)
        self.assertNotIn('hw_review', text)

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', False)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_flag_off_e014_send_has_no_keyboard(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Flag OFF (the default): the very same soft-flag run sends E014
        WITHOUT ``reply_markup`` and mints no token — ``bot_state`` holds no
        ``review_token:*`` row. Byte-identical to pre-feature behaviour."""
        # Guard the "default OFF" half of the contract: with the env var
        # unset in the test environment the import-time constant is False.
        self.assertNotIn('REVIEW_BUTTONS_ENABLED', os.environ)

        self._seed_published(
            'http://t-hunted.example/existing', self.SOFT_FP,
            source='t-hunted',
        )
        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = dict(self.SOFT_ARTICLE)

        news_bot.job()

        e014 = self._e014_calls(mock_admin)
        self.assertEqual(
            len(e014), 1,
            f"expected exactly one E014; got {mock_admin.call_args_list!r}",
        )
        self.assertIsNone(e014[0].kwargs.get('reply_markup'))
        # No token minted, nothing written to bot_state.
        self.assertEqual(self._review_token_keys(), [])
        # Advice matches reality: no keyboard is rendered, so the text must
        # NOT promise buttons — and must not name the archived CLI either.
        text = e014[0].args[0]
        self.assertNotIn('🚫 Не публиковать', text)
        self.assertNotIn('👍 Оставить', text)
        self.assertNotIn('hw_review', text)

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_flag_on_non_numeric_admin_no_keyboard_no_token(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Audit SEC-A8-1: flag ON but non-numeric admin id (the default
        ``@sunny413x`` shape) — the listener can never start, so the E014
        send site must not mint tokens or render buttons nobody will ever
        serve (no eternal spinner, no orphan ``review_token:*`` rows).
        The E014 alert itself still goes out, just without a keyboard —
        exactly like flag-off.

        This is the ONLY case where the bare flag and the effective gate
        disagree, so it is the case that pins WHY the send site derives
        ``buttons_enabled`` from the keyboard object (``kb is not None``)
        rather than re-reading ``REVIEW_BUTTONS_ENABLED``: with a second
        flag read the advice would tell the operator to press buttons that
        are not under the message."""
        self._seed_published(
            'http://t-hunted.example/existing', self.SOFT_FP,
            source='t-hunted',
        )
        new_link = 'http://autoevolution.example/new'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._make_entry(new_link)]
        mock_fetch_article.return_value = dict(self.SOFT_ARTICLE)

        with patch('news_bot.TELEGRAM_ADMIN_ID', '@sunny413x'):
            news_bot.job()

        e014 = self._e014_calls(mock_admin)
        self.assertEqual(
            len(e014), 1,
            f"expected exactly one E014; got {mock_admin.call_args_list!r}",
        )
        self.assertIsNone(e014[0].kwargs.get('reply_markup'))
        self.assertEqual(self._review_token_keys(), [])
        # No keyboard → the advice must follow the KEYBOARD, not the flag.
        # A `buttons_enabled=REVIEW_BUTTONS_ENABLED` regression at the send
        # site fails exactly here: the flag is True but nothing is rendered.
        text = e014[0].args[0]
        self.assertNotIn('🚫 Не публиковать', text)
        self.assertNotIn('👍 Оставить', text)
        self.assertIn('нечем', text)

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_only_e014_carries_keyboard(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Flag ON, mixed run: one entry soft-flags (E014) and a second
        hard-blocks (E015). Only the E014 call carries ``reply_markup`` —
        every other admin ping in the run goes out without buttons."""
        self._seed_published(
            'http://t-hunted.example/existing-soft', self.SOFT_FP,
            source='t-hunted',
        )
        self._seed_published(
            'http://t-hunted.example/existing-block', self.BLOCK_FP,
            source='t-hunted', title='Existing Block Article',
        )
        soft_link = 'http://autoevolution.example/soft'
        block_link = 'http://autoevolution.example/block'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            self._make_entry(soft_link, title='Soft Article'),
            self._make_entry(block_link, title='Block Article'),
        ]
        articles = {
            soft_link: dict(self.SOFT_ARTICLE),
            block_link: dict(self.BLOCK_ARTICLE),
        }
        mock_fetch_article.side_effect = (
            lambda entry: articles[entry['link']]
        )

        news_bot.job()

        e014 = self._e014_calls(mock_admin)
        self.assertEqual(
            len(e014), 1,
            f"expected exactly one E014; got {mock_admin.call_args_list!r}",
        )
        self.assertIsNotNone(e014[0].kwargs.get('reply_markup'))
        # The E015 hard-block DID fire in this run (otherwise the "other
        # alerts" half of the assertion below would be vacuous).
        others = [c for c in mock_admin.call_args_list if c not in e014]
        self.assertTrue(
            any('[E015]' in (c.args[0] if c.args else '') for c in others),
            f"expected an E015 in the run; got {mock_admin.call_args_list!r}",
        )
        for c in others:
            self.assertIsNone(
                c.kwargs.get('reply_markup'),
                f"non-E014 alert unexpectedly carries buttons: {c!r}",
            )


class TestReviewListener(_IntegrationBase):
    """Task 5 — background review listener + main() wiring.

    The listener is the bot's first inbound Telegram path, so the tests
    stay strictly synchronous: the per-update handler
    (``news_bot._handle_review_update``) is exercised directly with mock
    ``Bot``/``Update`` objects (no threads, no sockets), and the outer
    loop (``news_bot._run_review_listener``) is driven in-thread via the
    ``stop_event`` seam with backoff constants patched to 0.
    """

    ADMIN_ID = 424242

    # -- fixtures ---------------------------------------------------------

    def _make_bot(self):
        """Mock Bot whose API methods are awaitable — the handler builds
        a fresh ``Bot`` per call (cross-event-loop httpx safety), so the
        tests patch ``news_bot.Bot`` to hand this mock back every time."""
        bot = MagicMock()
        bot.get_updates = AsyncMock(return_value=[])
        bot.edit_message_text = AsyncMock()
        bot.answer_callback_query = AsyncMock()
        return bot

    def _make_update(self, data, *, update_id=1, user_id=ADMIN_ID,
                     text='[E014] original alert'):
        update = MagicMock()
        update.update_id = update_id
        cq = update.callback_query
        cq.data = data
        cq.id = 'cbq-1'
        cq.from_user.id = user_id
        cq.message.text = text
        cq.message.chat_id = 777
        cq.message.message_id = 42
        return update

    # -- gate predicate + main() wiring -----------------------------------

    def test_review_listener_starts_when_enabled_and_numeric_admin(self):
        with patch('news_bot.REVIEW_BUTTONS_ENABLED', True), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)), \
                patch('news_bot.threading.Thread') as mock_thread:
            self.assertTrue(news_bot._review_listener_enabled())
            with self.assertLogs('news_bot', level='INFO') as logs:
                result = news_bot._maybe_start_review_listener()

        mock_thread.assert_called_once_with(
            target=news_bot._run_review_listener,
            name='review-listener',
            daemon=True,
        )
        mock_thread.return_value.start.assert_called_once()
        self.assertIsNotNone(result)
        self.assertTrue(
            any('review listener active' in line for line in logs.output))
        # Best-effort startup ping went out.
        self.mock_notify.assert_called_once()

    def test_review_listener_not_started_when_flag_off(self):
        self.mock_notify.reset_mock()
        with patch('news_bot.REVIEW_BUTTONS_ENABLED', False), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)), \
                patch('news_bot.threading.Thread') as mock_thread:
            self.assertFalse(news_bot._review_listener_enabled())
            result = news_bot._maybe_start_review_listener()

        self.assertIsNone(result)
        mock_thread.assert_not_called()
        # Flag off is the normal test-instance state: silent (no ping).
        self.mock_notify.assert_not_called()

    def test_review_listener_not_started_when_bot_token_missing(self):
        """Audit CA-3: flag on + numeric admin + EMPTY bot token must be
        fail-closed at the gate. Without this check the listener thread
        starts and ``Bot(token=None)`` raises every poll — a perpetual
        5s ERROR loop for the process lifetime."""
        self.mock_notify.reset_mock()
        with patch('news_bot.REVIEW_BUTTONS_ENABLED', True), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)), \
                patch('news_bot.TELEGRAM_BOT_TOKEN', ''), \
                patch('news_bot.threading.Thread') as mock_thread:
            self.assertFalse(news_bot._review_listener_enabled())
            with self.assertLogs('news_bot', level='WARNING') as logs:
                result = news_bot._maybe_start_review_listener()

        self.assertIsNone(result)
        mock_thread.assert_not_called()
        # The warning names the actual broken knob, not the admin id.
        self.assertTrue(
            any('TELEGRAM_BOT_TOKEN' in line for line in logs.output),
            f"expected a TELEGRAM_BOT_TOKEN warning; got {logs.output!r}")
        # Operator is told (best-effort) why the buttons are dead.
        self.mock_notify.assert_called_once()

    def test_review_listener_not_started_when_admin_non_numeric(self):
        self.mock_notify.reset_mock()
        # _IntegrationBase already patches TELEGRAM_ADMIN_ID='@admin'
        # (non-numeric) — exactly the fail-closed default shape.
        with patch('news_bot.REVIEW_BUTTONS_ENABLED', True), \
                patch('news_bot.threading.Thread') as mock_thread:
            self.assertFalse(news_bot._review_listener_enabled())
            with self.assertLogs('news_bot', level='WARNING') as logs:
                result = news_bot._maybe_start_review_listener()

        self.assertIsNone(result)
        mock_thread.assert_not_called()
        self.assertTrue(
            any('TELEGRAM_ADMIN_ID' in line for line in logs.output))
        # Operator is told (best-effort) why the buttons are dead.
        self.mock_notify.assert_called_once()

    # -- outer loop resilience --------------------------------------------

    def test_review_listener_error_does_not_propagate(self):
        stop_event = threading.Event()
        calls = []

        def fake_get_updates(*args, **kwargs):
            if not calls:
                calls.append('boom')
                raise RuntimeError('simulated network/DB failure')
            stop_event.set()

            async def _empty():
                return []
            return _empty()

        mock_bot = MagicMock()
        mock_bot.get_updates = fake_get_updates
        with patch('news_bot.Bot', return_value=mock_bot), \
                patch('news_bot.REVIEW_LISTENER_ERROR_BACKOFF_SECONDS', 0), \
                patch('news_bot._review_listener_sleep',
                      wraps=news_bot._review_listener_sleep) as sleep_spy:
            with self.assertLogs('news_bot', level='ERROR') as logs:
                # Must return normally — any escaping exception fails here.
                news_bot._run_review_listener(stop_event=stop_event)

        self.assertEqual(calls, ['boom'])  # error path was exercised
        self.assertTrue(
            any('review listener' in line for line in logs.output))
        # Backoff is pinned (test review round 1): the generic error branch
        # MUST sleep with the error-backoff constant (patched to 0) — a
        # deleted sleep call would busy-loop on a repeating failure.
        sleep_spy.assert_called_once_with(stop_event, 0)

    def test_review_listener_conflict_409_logged_with_backoff(self):
        from telegram.error import Conflict
        stop_event = threading.Event()
        calls = []

        def fake_get_updates(*args, **kwargs):
            if not calls:
                calls.append('conflict')
                raise Conflict('terminated by other getUpdates request')
            stop_event.set()

            async def _empty():
                return []
            return _empty()

        mock_bot = MagicMock()
        mock_bot.get_updates = fake_get_updates
        with patch('news_bot.Bot', return_value=mock_bot), \
                patch('news_bot.REVIEW_LISTENER_CONFLICT_BACKOFF_SECONDS', 0), \
                patch('news_bot._review_listener_sleep',
                      wraps=news_bot._review_listener_sleep) as sleep_spy:
            with self.assertLogs('news_bot', level='ERROR') as logs:
                news_bot._run_review_listener(stop_event=stop_event)

        # Clear operator-facing message: shared token, exactly one listener.
        self.assertTrue(
            any('409' in line and 'one' in line.lower()
                for line in logs.output),
            f"expected a 409/single-listener ERROR line; got {logs.output!r}")
        # Backoff is pinned (test review round 1): the Conflict branch MUST
        # sleep with the CONFLICT backoff constant (patched to 0; the
        # unpatched generic constant is 5, so using the wrong one — or
        # deleting the sleep — fails this assertion).
        sleep_spy.assert_called_once_with(stop_event, 0)

    def test_review_listener_handler_error_advances_offset_and_survives(self):
        stop_event = threading.Event()
        seen_offsets = []
        update = self._make_update('dd:c:tok-loop', update_id=7)

        def fake_get_updates(*args, **kwargs):
            seen_offsets.append(kwargs.get('offset'))
            if len(seen_offsets) > 1:
                stop_event.set()

                async def _empty():
                    return []
                return _empty()

            async def _one():
                return [update]
            return _one()

        mock_bot = MagicMock()
        mock_bot.get_updates = fake_get_updates
        with patch('news_bot.Bot', return_value=mock_bot), \
                patch('news_bot._handle_review_update',
                      side_effect=sqlite3.OperationalError('db locked')), \
                patch('news_bot.REVIEW_LISTENER_ERROR_BACKOFF_SECONDS', 0):
            with self.assertLogs('news_bot', level='ERROR'):
                news_bot._run_review_listener(stop_event=stop_event)

        # The bad update was acked (offset advanced past it) and the loop
        # lived on to poll again — one poisoned update can't wedge the
        # listener or the publish process.
        self.assertEqual(seen_offsets, [None, 8])

    # -- callback_data grammar filter -------------------------------------

    def test_callback_grammar_rejects_malformed_data(self):
        bad_values = [
            None,                          # missing data
            '',                            # empty
            'garbage',                     # no colons
            'dd:c',                        # too few fields
            'dd:c:tok:extra',              # too many fields
            'dd:x:tok',                    # unknown action letter
            'xx:c:tok',                    # wrong prefix
            'dd:cancel:tok',               # full word not allowed on the wire
            'dd:c:',                       # empty token
            'dd:c:' + 'a' * 100,           # oversized (> Telegram 64-byte cap)
            'dd:c:' + 'ю' * 30,            # 35 chars but 65 UTF-8 bytes —
                                           # the cap is BYTES (audit CA-5)
        ]
        with patch('news_bot.Bot') as mock_bot_cls, \
                patch('news_bot.resolve_dedup_callback') as mock_resolve:
            for value in bad_values:
                update = self._make_update(value)
                news_bot._handle_review_update(update)

            # An update with no callback_query at all is likewise a no-op.
            empty_update = MagicMock()
            empty_update.callback_query = None
            news_bot._handle_review_update(empty_update)

        mock_resolve.assert_not_called()
        # Rejected updates never even construct a Bot — zero Telegram I/O.
        mock_bot_cls.assert_not_called()

    def test_keyboard_callback_data_round_trips_through_parser(self):
        """Audit M-1: the ``dd:<c|k>:<token>`` grammar lives as literals
        in two modules (``admin_alerts`` builder, ``news_bot`` parser),
        each pinned by its own unit test — but a tandem edit of the
        builder + its test would ship a mismatch whose failure mode is
        silent by design (parser rejects, listener just advances the
        offset). This round-trip pins the seam: the builder's REAL
        output, with a real ``secrets.token_urlsafe(9)`` token, must be
        accepted by the parser for BOTH buttons."""
        import secrets

        import admin_alerts

        token = secrets.token_urlsafe(9)
        kb = admin_alerts.build_dedup_review_keyboard(token)
        buttons = [b for row in kb.inline_keyboard for b in row]
        self.assertEqual(len(buttons), 2)
        parsed = [
            news_bot._parse_review_callback_data(b.callback_data)
            for b in buttons
        ]
        # Cancel first, keep second — and the exact minted token back out.
        self.assertEqual(parsed, [('cancel', token), ('keep', token)])

    def test_callback_letter_maps_to_full_word(self):
        bot = self._make_bot()
        # Numeric admin matching the presser: the handler's own admin gate
        # (SEC-T5-1, runs BEFORE resolve) must pass so the spy sees the
        # mapped action words.
        with patch('news_bot.Bot', return_value=bot), \
                patch('news_bot.TELEGRAM_ADMIN_ID', '1'), \
                patch('news_bot.resolve_dedup_callback',
                      return_value=(None, "")) as mock_resolve:
            news_bot._handle_review_update(
                self._make_update('dd:c:tok-c', user_id=1))
            news_bot._handle_review_update(
                self._make_update('dd:k:tok-k', user_id=1))
        self.assertEqual(
            [c.args for c in mock_resolve.call_args_list],
            [('cancel', 'tok-c', 1), ('keep', 'tok-k', 1)],
        )

    # -- terminal vs ignored outcomes -------------------------------------

    def test_review_listener_dispatches_admin_cancel(self):
        link = 'https://example.com/listener-cancel'
        _seed_pending_row(link)
        pending_articles_repo.put_review_token('tok-listener-1', link)
        bot = self._make_bot()
        update = self._make_update('dd:c:tok-listener-1')

        with patch('news_bot.Bot', return_value=bot), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)), \
                patch('news_bot.resolve_dedup_callback',
                      wraps=news_bot.resolve_dedup_callback) as spy:
            with self.assertLogs('news_bot', level='INFO') as logs:
                news_bot._handle_review_update(update)

        # Letter mapped to the full word BEFORE the pure resolver ran.
        spy.assert_called_once_with('cancel', 'tok-listener-1', self.ADMIN_ID)
        # Real DB effect: the pending row was skipped, not published.
        self.assertIsNone(pending_articles_repo.get_pending(link))
        self.assertIsNone(pending_articles_repo.get_published(link))
        # Terminal outcome → message edited: status appended, keyboard gone.
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=777,
            message_id=42,
            text='[E014] original alert\n\n✅ Отменено оператором',
            reply_markup=None,
        )
        bot.answer_callback_query.assert_awaited_once_with(
            'cbq-1', text='✅ Отменено оператором')
        # Operator decision logged at INFO: action + link + status.
        decision_lines = [
            line for line in logs.output
            if 'cancel' in line and link in line
            and 'Отменено оператором' in line
        ]
        self.assertEqual(len(decision_lines), 1, logs.output)

    def test_decision_logged_even_when_edit_fails(self):
        """Audit CA-2: by the time the message edit runs, the decision is
        already applied (row skipped, token consumed) — so the
        operator-decision INFO line must be emitted BEFORE the Telegram
        edit/answer calls. A transient ``TelegramError`` on the edit
        must not cost the audit line (user-spec: every operator decision
        lands in the log)."""
        from telegram.error import TelegramError

        link = 'https://example.com/listener-log-first'
        _seed_pending_row(link)
        pending_articles_repo.put_review_token('tok-log-first', link)
        bot = self._make_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=TelegramError('message to edit not found'))
        update = self._make_update('dd:c:tok-log-first')

        with patch('news_bot.Bot', return_value=bot), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)):
            with self.assertLogs('news_bot', level='INFO') as logs:
                # The per-update guard in _run_review_listener owns
                # survival; at the handler seam the error propagates.
                with self.assertRaises(TelegramError):
                    news_bot._handle_review_update(update)

        # State change happened (cancel applied) ...
        self.assertIsNone(pending_articles_repo.get_pending(link))
        # ... and the audit line was written despite the failed edit.
        decision_lines = [
            line for line in logs.output
            if 'operator decision' in line and link in line
            and 'Отменено оператором' in line
        ]
        self.assertEqual(len(decision_lines), 1, logs.output)

    def test_ignored_press_answers_empty_and_never_edits(self):
        link = 'https://example.com/listener-nonadmin'
        _seed_pending_row(link)
        pending_articles_repo.put_review_token('tok-listener-2', link)
        bot = self._make_bot()
        update = self._make_update(
            'dd:c:tok-listener-2', user_id=self.ADMIN_ID + 1)

        with patch('news_bot.Bot', return_value=bot), \
                patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID)), \
                patch('news_bot.pending_repo.get_review_token_link') \
                as mock_link_read:
            news_bot._handle_review_update(update)

        bot.edit_message_text.assert_not_awaited()
        bot.answer_callback_query.assert_awaited_once_with('cbq-1', text='')
        # SEC-T5-1: a non-admin press performs ZERO DB reads — the handler
        # gates on the admin id BEFORE any token lookup.
        mock_link_read.assert_not_called()
        # State untouched: row still pending, token still alive.
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertEqual(
            pending_articles_repo.get_review_token_link('tok-listener-2'),
            link)


# ---------------------------------------------------------------------------
# Intake promo-filter integration tests ([E035], prod incident 2026-07-25).
# ---------------------------------------------------------------------------


class TestPromoIntakeFilter(_PrepPhaseBase):
    """A shop-promo post is dropped at INTAKE — before staging, way before
    translation (zero LLM tokens spent): not in ``pending_articles``, the
    funnel counts it, exactly one [E035] alert fires, and the link is
    pinned in ``processed_news`` so it is not re-fetched daily.

    Mocks the same surface as ``TestCrossSourceDedup`` (``load_feeds`` /
    ``fetch_rss`` / ``fetch_full_article``); the base's generic
    ``send_admin_notification`` silencer is stopped per-test so the
    admin-ping mock can be introspected, and re-started in ``tearDown``.
    """

    PROMO_LINK = ('https://t-hunted.blogspot.com/2026/07/'
                  'hot-wheels-antigos-e-raros-na-loja.html')
    PROMO_TITLE = 'Hot Wheels antigos e raros na loja Universo Hot Wheels'

    def setUp(self):
        super().setUp()
        self.notify_patcher.stop()

    def tearDown(self):
        self.notify_patcher.start()
        super().tearDown()

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_promo_article_dropped_at_intake(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Real-incident fixture end-to-end: promo entry → dropped at
        intake, NOT staged, funnel promo counter incremented, one [E035]
        alert (substring anchor «Отсечена реклама»), link marked
        processed, and NO re-fetch on the next tick."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [{
            'link': self.PROMO_LINK,
            'title': self.PROMO_TITLE,
            'published': '2026-07-25',
            'summary': 'Summary',
        }]
        mock_fetch_article.return_value = {
            'title': self.PROMO_TITLE,
            'subtitle': '',
            'paragraphs': [
                'Em nossa loja Universo Hot Wheels você encontra Hot '
                'Wheels antigos e raros para a sua coleção.',
                'Não perca as novidades desta semana!',
                'Garanta o seu antes que acabe o estoque.',
            ],
            'images': [],
        }

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # Dropped at intake — never staged into pending_articles.
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertIsNone(pending_articles_repo.get_pending(self.PROMO_LINK))

        # Link pinned in processed_news so tomorrow's tick skips it at
        # the b2 filter (no daily re-fetch + re-alert).
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {
                r[0] for r in
                conn.execute('SELECT link FROM processed_news').fetchall()
            }
        finally:
            conn.close()
        self.assertIn(self.PROMO_LINK, processed)

        # Exactly one [E035] alert with the substring anchor + markers.
        e035_calls = [
            c for c in mock_admin.call_args_list
            if '[E035]' in (c.args[0] if c.args else '')
        ]
        self.assertEqual(len(e035_calls), 1)
        msg = e035_calls[0].args[0]
        self.assertIn('Отсечена реклама', msg)
        self.assertIn(self.PROMO_LINK, msg)
        self.assertIn('nossa loja', msg)
        # No dedup alerts fired for this drop.
        for c in mock_admin.call_args_list:
            m = c.args[0] if c.args else ''
            self.assertNotIn('[E014]', m)
            self.assertNotIn('[E015]', m)

        # The drop is diagnosable straight from the logs: an [E035] line
        # naming the link + the funnel line counting promo=1.
        e035_lines = [
            l for l in cm.output
            if '[E035]' in l and self.PROMO_LINK in l
        ]
        self.assertTrue(
            e035_lines,
            "expected an [E035] log line naming the dropped link; got:\n"
            + "\n".join(cm.output),
        )
        funnel_lines = [
            l for l in cm.output if '[funnel]' in l and 'promo=1' in l
        ]
        self.assertTrue(
            funnel_lines,
            "expected the [funnel] line to count promo=1; got:\n"
            + "\n".join(cm.output),
        )

        # Next tick: the processed pin means the entry is filtered at b2 —
        # the article body is NOT fetched again.
        mock_fetch_article.reset_mock()
        news_bot.job()
        mock_fetch_article.assert_not_called()

    @patch('news_bot._is_promo_article', side_effect=RuntimeError('boom'))
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_promo_filter_crash_is_fail_open(
        self, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article, _mock_promo,
    ):
        """Audit SEC-PROMO-1: the filter is wrapped like the dedup gate —
        a crash inside it must NOT kill the tick (``job()`` runs inside a
        bare ``while True`` scheduler, and the crash would land BEFORE
        mark_processed, so the same entry would crash-loop the daemon on
        every restart). Fail-open: the article is treated as not-promo
        and still reaches the queue."""
        link = 'http://example.com/2026/07/ordinary-news.html'
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [{
            'link': link,
            'title': 'Ordinary Hot Wheels news',
            'published': '2026-07-25',
            'summary': 'Summary',
        }]
        mock_fetch_article.return_value = {
            'title': 'Ordinary Hot Wheels news',
            'subtitle': '',
            'paragraphs': ['A new casting was revealed this week.'],
            'images': [],
        }

        news_bot.job()  # must not raise

        # Fail-open: staged as usual, no [E035] fired.
        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        for c in mock_admin.call_args_list:
            self.assertNotIn('[E035]', c.args[0] if c.args else '')


class TestResolveHoldCallback(_IntegrationBase):
    """Every branch of ``news_bot.resolve_hold_callback`` on a real
    tempfile SQLite DB — the [E036] «На утверждение» buttons.

    Same contract as ``resolve_dedup_callback``: ``(status, answer)`` on a
    terminal outcome, ``(None, "")`` on an ignored press.
    """

    ADMIN_ID = 424242

    def setUp(self):
        super().setUp()
        # addCleanup, not a tearDown pair: cleanup is registered the
        # instant the patch starts, so a failure LATER in setUp can never
        # leak a patched module attribute into the rest of the run (the
        # shape that turns one broken test into a cascade of unrelated
        # ones). Same reason everywhere in this feature's classes.
        patcher = patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ----------------------------------------------------------

    def _seed_held(self, link, reason='poster', title='Poster post'):
        pending_articles_repo.insert_pending({
            'link': link,
            'source_name': 't-hunted',
            'feed_url': None,
            'title': title,
            'subtitle': '',
            'paragraphs': ['First paragraph.'],
            'images': [],
            'blocks': None,
            'pub_date': '2026-07-25',
            'hold_reason': reason,
        })

    def _stage_token(self, link, token='tok-hold-1234'):
        pending_articles_repo.put_review_token(
            token, link, kind=pending_articles_repo.REVIEW_TOKEN_KIND_HOLD)
        return token

    def _in_processed_news(self, link):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT 1 FROM processed_news WHERE link=?", (link,),
            ).fetchone() is not None
        finally:
            conn.close()

    # -- the load-bearing guarantee ---------------------------------------

    def test_held_row_is_never_selected_by_the_slot_loop(self):
        """THE guarantee behind «нет ответа = не публикуем»: with no
        button press at all, the slot loop never sees the row. Drives the
        loop's real selection path (head of ``list_pending()``) with the
        channel side-effect stubbed — mirrors
        ``test_cancel_then_slot_publish_does_not_publish``."""
        held = 'https://example.com/held-poster'
        survivor = 'https://example.com/ordinary-news'
        self._seed_held(held)
        _seed_pending_row(survivor, title='Survivor')

        self.assertNotIn(
            held, [r['link'] for r in pending_articles_repo.list_pending()])

        with patch('news_bot._fallback_publish') as mock_pub:
            mock_pub.side_effect = lambda row, via_review=False: (
                pending_articles_repo.skip_pending(row['link']))
            while True:
                rows = pending_articles_repo.list_pending()
                if not rows:
                    break
                news_bot._fallback_publish(rows[0])

        published = [c.args[0]['link'] for c in mock_pub.call_args_list]
        self.assertEqual(published, [survivor])
        # Still parked, still intact — nothing consumed or expired it.
        self.assertIsNotNone(pending_articles_repo.get_pending(held))
        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()], [held])

    # -- approve ----------------------------------------------------------

    def test_approve_releases_the_row_into_the_queue(self):
        link = 'https://example.com/hold-approve'
        self._seed_held(link)
        token = self._stage_token(link)

        status, answer = news_bot.resolve_hold_callback(
            'approve', token, self.ADMIN_ID)

        self.assertEqual(status, "✅ Одобрено — выйдет в ближайший слот")
        self.assertEqual(answer, status)
        self.assertIsNone(pending_articles_repo.get_pending(link)['hold_reason'])
        self.assertIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        # Approval is not a publish.
        self.assertIsNone(pending_articles_repo.get_published(link))
        self.assertFalse(self._in_processed_news(link))
        # Token consumed → a second press is a safe stale no-op.
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))
        self.assertEqual(
            news_bot.resolve_hold_callback('approve', token, self.ADMIN_ID),
            ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела"),
        )

    def test_approve_then_slot_publishes_the_article(self):
        """The other half of the guarantee: once approved, the article
        behaves like any queue member and DOES go out at its slot."""
        link = 'https://example.com/hold-approve-slot'
        self._seed_held(link)
        news_bot.resolve_hold_callback(
            'approve', self._stage_token(link), self.ADMIN_ID)

        with patch('news_bot._fallback_publish') as mock_pub:
            mock_pub.side_effect = lambda row, via_review=False: (
                pending_articles_repo.skip_pending(row['link']))
            rows = pending_articles_repo.list_pending()
            self.assertTrue(rows)
            news_bot._fallback_publish(rows[0])

        self.assertEqual(
            [c.args[0]['link'] for c in mock_pub.call_args_list], [link])

    def test_approve_when_the_row_is_gone_reports_unavailable(self):
        link = 'https://example.com/hold-approve-gone'
        token = self._stage_token(link)
        status, _ = news_bot.resolve_hold_callback(
            'approve', token, self.ADMIN_ID)
        self.assertEqual(status, "⚠️ Статья уже недоступна")
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    def test_approve_on_an_already_released_row_reports_unavailable(self):
        """Two tokens for the same article (re-staged after a restart):
        the second press must not report a fresh approval."""
        link = 'https://example.com/hold-approve-twice'
        self._seed_held(link)
        news_bot.resolve_hold_callback(
            'approve', self._stage_token(link, 'tok-a'), self.ADMIN_ID)
        status, _ = news_bot.resolve_hold_callback(
            'approve', self._stage_token(link, 'tok-b'), self.ADMIN_ID)
        self.assertEqual(status, "⚠️ Статья уже недоступна")

    # -- reject -----------------------------------------------------------

    def test_reject_drops_the_row_and_pins_the_link(self):
        link = 'https://example.com/hold-reject'
        self._seed_held(link)
        token = self._stage_token(link)

        status, answer = news_bot.resolve_hold_callback(
            'reject', token, self.ADMIN_ID)

        self.assertEqual(status, "🚫 Не будет опубликовано")
        self.assertEqual(answer, status)
        self.assertIsNone(pending_articles_repo.get_pending(link))
        self.assertEqual(pending_articles_repo.list_held(), [])
        # Pinned so it never comes back on the next fetch; not a publish.
        self.assertTrue(self._in_processed_news(link))
        self.assertIsNone(pending_articles_repo.get_published(link))
        self.assertIsNone(pending_articles_repo.get_review_token_link(token))

    def test_reject_when_the_row_is_gone_reports_unavailable(self):
        link = 'https://example.com/hold-reject-gone'
        token = self._stage_token(link)
        status, _ = news_bot.resolve_hold_callback(
            'reject', token, self.ADMIN_ID)
        self.assertEqual(status, "⚠️ Статья уже недоступна")

    # -- auth gate --------------------------------------------------------

    def test_non_admin_press_ignored_no_state_change(self):
        link = 'https://example.com/hold-nonadmin'
        self._seed_held(link)
        token = self._stage_token(link)

        for action in ('approve', 'reject'):
            with self.subTest(action=action):
                status, answer = news_bot.resolve_hold_callback(
                    action, token, self.ADMIN_ID + 1)
                self.assertIsNone(status)
                self.assertEqual(answer, "")
        # Still held, token untouched — the admin can still act later.
        self.assertEqual(
            pending_articles_repo.get_pending(link)['hold_reason'], 'poster')
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), link)

    def test_non_numeric_admin_id_fail_closed(self):
        link = 'https://example.com/hold-failclosed'
        self._seed_held(link)
        token = self._stage_token(link)

        with patch('news_bot.TELEGRAM_ADMIN_ID', '@sunny413x'):
            status, answer = news_bot.resolve_hold_callback(
                'approve', token, self.ADMIN_ID)

        self.assertIsNone(status)
        self.assertEqual(answer, "")
        self.assertEqual(
            pending_articles_repo.get_pending(link)['hold_reason'], 'poster')

    # -- token / action robustness ----------------------------------------

    def test_unknown_token_is_stale(self):
        self.assertEqual(
            news_bot.resolve_hold_callback('approve', 'nope', self.ADMIN_ID),
            ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела"),
        )

    def test_unknown_action_keeps_the_token(self):
        link = 'https://example.com/hold-badaction'
        self._seed_held(link)
        token = self._stage_token(link)
        status, _ = news_bot.resolve_hold_callback(
            'explode', token, self.ADMIN_ID)
        self.assertEqual(status, "⚠️ Кнопка устарела")
        # Token NOT burned — a real button can still be pressed.
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token), link)
        self.assertEqual(
            pending_articles_repo.get_pending(link)['hold_reason'], 'poster')


class TestReviewCallbackGrammarsCoexist(_IntegrationBase):
    """Both keyboards ship at once: the [E014] ``dd:`` grammar must keep
    working exactly as before, and the [E036] ``hd:`` grammar must route
    to its own resolver."""

    ADMIN_ID = 424242

    def setUp(self):
        super().setUp()
        patcher = patch('news_bot.TELEGRAM_ADMIN_ID', str(self.ADMIN_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parser_accepts_both_grammars(self):
        self.assertEqual(
            news_bot._parse_review_callback_data('dd:c:tok'), ('cancel', 'tok'))
        self.assertEqual(
            news_bot._parse_review_callback_data('dd:k:tok'), ('keep', 'tok'))
        self.assertEqual(
            news_bot._parse_review_callback_data('hd:a:tok'),
            ('approve', 'tok'))
        self.assertEqual(
            news_bot._parse_review_callback_data('hd:r:tok'), ('reject', 'tok'))

    def test_parser_rejects_cross_grammar_letters_and_junk(self):
        """Strictness is per-prefix: a letter valid in one grammar must
        not be accepted under the other."""
        for bad in (
            'dd:a:tok', 'dd:r:tok',        # hold letters on the dedup prefix
            'hd:c:tok', 'hd:k:tok',        # dedup letters on the hold prefix
            'hd:approve:tok',              # full word on the wire
            'hd:a:', 'hd::tok', ':a:tok',  # empty fields
            'hd:a:tok:extra',              # too many fields
            'xx:a:tok',                    # unknown prefix
            'hd:a:' + 'a' * 100,           # over Telegram's 64-byte cap
            'hd:a:' + 'ю' * 30,            # 35 chars but 65 UTF-8 bytes
            None, 42, b'hd:a:tok',
        ):
            with self.subTest(data=bad):
                self.assertIsNone(news_bot._parse_review_callback_data(bad))

    def test_hold_keyboard_round_trips_through_the_parser(self):
        """Grammar literals live in two modules (builder + parser); pin
        the seam with the builder's REAL output and a real token."""
        import secrets

        import admin_alerts

        token = secrets.token_urlsafe(9)
        buttons = [
            b for row in admin_alerts.build_hold_review_keyboard(
                token).inline_keyboard
            for b in row
        ]
        self.assertEqual(len(buttons), 2)
        self.assertEqual(
            [news_bot._parse_review_callback_data(b.callback_data)
             for b in buttons],
            [('approve', token), ('reject', token)],
        )

    def test_dedup_grammar_still_routes_to_the_dedup_resolver(self):
        """Regression: adding the hold grammar must not steal [E014]
        presses."""
        link = 'https://example.com/still-dedup'
        _seed_pending_row(link)
        pending_articles_repo.put_review_token('tok-dd', link)

        status, _ = news_bot.resolve_dedup_callback(
            'cancel', 'tok-dd', self.ADMIN_ID)
        self.assertEqual(status, "✅ Отменено оператором")
        self.assertIsNone(pending_articles_repo.get_pending(link))

    # -- SEC-CG-2: cross-grammar token confusion --------------------------

    def test_dedup_token_redeemed_by_the_hold_resolver_is_refused(self):
        """Audit SEC-CG-2, direction (a): a token minted for an ordinary
        [E014] row must not let «🚫 Не публиковать» from the HOLD keyboard
        skip a NON-held article. Refused as stale, no state change, and
        the token survives so its own buttons still work."""
        link = 'https://example.com/dedup-token-abuse'
        _seed_pending_row(link)
        pending_articles_repo.put_review_token(
            'tok-x', link,
            kind=pending_articles_repo.REVIEW_TOKEN_KIND_DEDUP)

        for action in ('reject', 'approve'):
            with self.subTest(action=action):
                status, answer = news_bot.resolve_hold_callback(
                    action, 'tok-x', self.ADMIN_ID)
                self.assertEqual(status, "⚠️ Кнопка устарела")
                self.assertEqual(answer, "⚠️ Кнопка устарела")
                # Article untouched — still queued, never skipped.
                self.assertIsNotNone(
                    pending_articles_repo.get_pending(link))
                # Token NOT consumed.
                self.assertEqual(
                    pending_articles_repo.get_review_token_link('tok-x'), link)

        # ...and the legitimate [E014] button still resolves afterwards.
        status, _ = news_bot.resolve_dedup_callback(
            'cancel', 'tok-x', self.ADMIN_ID)
        self.assertEqual(status, "✅ Отменено оператором")

    def test_hold_token_redeemed_by_the_dedup_resolver_is_refused(self):
        """Audit SEC-CG-2, direction (b) — the worse one: «👍 Оставить»
        on a HOLD token used to consume the token with NO state change,
        leaving the held article permanently orphaned (frozen, no live
        button, no re-mint path, only DB surgery). Must be refused
        WITHOUT consuming the token."""
        link = 'https://example.com/hold-token-abuse'
        pending_articles_repo.insert_pending({
            'link': link, 'source_name': 't-hunted', 'feed_url': None,
            'title': 'Poster post', 'subtitle': '',
            'paragraphs': ['p'], 'images': [], 'blocks': None,
            'pub_date': '2026-07-25', 'hold_reason': 'poster',
        })
        pending_articles_repo.put_review_token(
            'tok-y', link, kind=pending_articles_repo.REVIEW_TOKEN_KIND_HOLD)

        for action in ('keep', 'cancel'):
            with self.subTest(action=action):
                status, _ = news_bot.resolve_dedup_callback(
                    action, 'tok-y', self.ADMIN_ID)
                self.assertEqual(status, "⚠️ Кнопка устарела")
                # Still held, token still live.
                self.assertEqual(
                    pending_articles_repo.get_pending(link)['hold_reason'],
                    'poster')
                self.assertEqual(
                    pending_articles_repo.get_review_token_link('tok-y'), link)

        # NOT orphaned: the real [E036] button still releases it.
        status, _ = news_bot.resolve_hold_callback(
            'approve', 'tok-y', self.ADMIN_ID)
        self.assertEqual(status, "✅ Одобрено — выйдет в ближайший слот")
        self.assertIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])

    def test_cross_grammar_refusal_runs_after_the_admin_gate(self):
        """Order matters: a non-admin press is still ignored outright
        (``(None, "")``), never answered with a kind-mismatch message that
        would confirm the token exists."""
        link = 'https://example.com/hold-token-nonadmin'
        pending_articles_repo.put_review_token(
            'tok-z', link, kind=pending_articles_repo.REVIEW_TOKEN_KIND_HOLD)
        self.assertEqual(
            news_bot.resolve_dedup_callback('keep', 'tok-z', self.ADMIN_ID + 1),
            (None, ""),
        )

    def test_handler_dispatches_each_grammar_to_its_own_resolver(self):
        """The listener's dispatch, not just the parser: an ``hd:`` press
        must never land in the dedup resolver (which would answer
        «устарела» and quietly lose the operator's decision)."""
        def _make_update(data, update_id=1):
            upd = MagicMock()
            upd.update_id = update_id
            cq = upd.callback_query
            cq.data = data
            cq.id = 'cbq'
            cq.from_user.id = self.ADMIN_ID
            cq.message.text = 'original alert'
            cq.message.chat_id = 777
            cq.message.message_id = 42
            return upd

        # Fresh Bot per Telegram call (cross-event-loop httpx safety), so
        # patch the class and hand back an awaitable mock — same fixture
        # shape as TestReviewListener.
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.answer_callback_query = AsyncMock()

        with patch('news_bot.Bot', return_value=bot), \
                patch('news_bot.resolve_hold_callback',
                      return_value=("ok", "ok")) as hold_spy, \
                patch('news_bot.resolve_dedup_callback',
                      return_value=("ok", "ok")) as dedup_spy:
            news_bot._handle_review_update(_make_update('hd:a:tok-h'))
            news_bot._handle_review_update(_make_update('dd:c:tok-d', 2))

        hold_spy.assert_called_once_with('approve', 'tok-h', self.ADMIN_ID)
        dedup_spy.assert_called_once_with('cancel', 'tok-d', self.ADMIN_ID)


class TestContentGateIntake(_PrepPhaseBase):
    """The content gate end-to-end through ``news_bot.job()``.

    HOLD: the real 2026-07-25 poster post is STAGED WITH a hold_reason,
    stays out of the publishable queue, and produces exactly one [E036]
    with the approve/reject keyboard.
    DROP: video-review and event entries are rejected at intake like promo
    posts — one [E037], link pinned, no re-fetch next tick.
    """

    POSTER_LINK = ('https://t-hunted.blogspot.com/2026/07/'
                   'as-fotos-do-ultimo-poster-da-hot-wheels.html')
    POSTER_TITLE = 'As fotos do último poster da Hot Wheels 2026'
    POSTER_BODY = {
        'title': POSTER_TITLE,
        'subtitle': '',
        'paragraphs': [
            'Saiu o último poster da Hot Wheels para 2026.',
            'As fotos mostram os carros da linha básica.',
            'Confira todas as imagens abaixo.',
            'Veja também no vídeo abaixo.',
        ],
        'images': ['http://img/%d.jpg' % i for i in range(12)],
    }

    def setUp(self):
        super().setUp()
        self.notify_patcher.stop()
        # Re-arm the base silencer FIRST (LIFO cleanup order means it runs
        # last), so _IntegrationBase.tearDown always has something to stop
        # even if the patch below fails to start.
        self.addCleanup(self.notify_patcher.start)
        # SEC-A8-1: the [E036] send site gates buttons on
        # _review_listener_enabled() (flag + token + NUMERIC admin); the
        # base class patches a non-numeric '@admin'.
        patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '424242')
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _calls_with(mock_admin, code):
        return [c for c in mock_admin.call_args_list
                if code in (c.args[0] if c.args else '')]

    def _feed(self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
              link, title, article):
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [{
            'link': link, 'title': title,
            'published': '2026-07-25', 'summary': 'Summary',
        }]
        mock_fetch_article.return_value = dict(article)

    # -- HOLD -------------------------------------------------------------

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_poster_post_is_staged_but_held(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # Staged — the row EXISTS (unlike a promo drop) and carries the
        # marker list that produced the decision.
        row = pending_articles_repo.get_pending(self.POSTER_LINK)
        self.assertIsNotNone(row)
        self.assertIn('poster', row['hold_reason'])

        # ...but is NOT publishable: invisible to the slot loop's source
        # and absent from the slot-computation count.
        self.assertNotIn(
            self.POSTER_LINK,
            [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()],
            [self.POSTER_LINK])

        # NOT pinned in processed_news: the article is still live in the
        # queue, and a pin would be the "we're done with it" marker.
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {r[0] for r in conn.execute(
                'SELECT link FROM processed_news').fetchall()}
        finally:
            conn.close()
        self.assertNotIn(self.POSTER_LINK, processed)

        # Exactly one [E036] naming the article and the matched markers.
        e036 = self._calls_with(mock_admin, '[E036]')
        self.assertEqual(len(e036), 1, mock_admin.call_args_list)
        msg = e036[0].args[0]
        self.assertIn('На утверждение', msg)
        self.assertIn(self.POSTER_LINK, msg)
        self.assertIn('poster', msg)
        # No genre drop happened.
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])

        # Diagnosable from the logs alone.
        self.assertTrue(
            [l for l in cm.output if '[E036]' in l and self.POSTER_LINK in l],
            "expected an [E036] log line naming the held link",
        )
        self.assertTrue(
            [l for l in cm.output if '[funnel]' in l and 'held=1' in l],
            "expected the [funnel] line to count held=1",
        )

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_hold_alert_carries_the_approve_reject_keyboard(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Gate open: [E036] ships the two buttons, the token resolves to
        the held link, and the advice matches what is attached."""
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        news_bot.job()

        e036 = self._calls_with(mock_admin, '[E036]')
        self.assertEqual(len(e036), 1)
        kb = e036[0].kwargs.get('reply_markup')
        self.assertIsNotNone(kb, "[E036] send is missing reply_markup")
        cds = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertTrue(cds[0].startswith('hd:a:'), cds)
        token = cds[0][len('hd:a:'):]
        self.assertTrue(token)
        self.assertEqual(cds, [f'hd:a:{token}', f'hd:r:{token}'])
        self.assertEqual(
            pending_articles_repo.get_review_token_link(token),
            self.POSTER_LINK)
        # Advice matches reality.
        text = e036[0].args[0]
        self.assertIn('✅ Опубликовать', text)
        self.assertIn('🚫 Не публиковать', text)
        self.assertNotIn('hw_review', text)

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', False)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_gate_closed_holds_the_article_without_buttons(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Flag off: still HELD (the safe direction), but no keyboard, no
        token minted, and the advice must not tell the operator to press
        a button that is not there."""
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        news_bot.job()

        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()],
            [self.POSTER_LINK])
        e036 = self._calls_with(mock_admin, '[E036]')
        self.assertEqual(len(e036), 1)
        self.assertIsNone(e036[0].kwargs.get('reply_markup'))
        self.assertNotIn('нажми', e036[0].args[0])
        self.assertIn('НИКОГДА не опубликуется', e036[0].args[0])
        # No orphan tokens.
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM bot_state "
                "WHERE key LIKE 'review_token:%'").fetchone()[0], 0)
        finally:
            conn.close()

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_held_article_is_reported_in_the_daily_plan_ping(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The held backlog must be visible somewhere — it is excluded
        from «Всего в очереди», and nothing else ever re-surfaces it."""
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        news_bot.job()

        plan = [c.args[0] for c in mock_admin.call_args_list
                if c.args and ('[E008]' in c.args[0] or '[E009]' in c.args[0])]
        self.assertTrue(plan, mock_admin.call_args_list)
        self.assertTrue(
            any('На утверждении: 1' in m for m in plan),
            f"held backlog missing from the daily ping: {plan!r}",
        )

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_held_article_is_not_re_staged_on_the_next_tick(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The held row is not pinned in processed_news, so the b2 filter
        must catch it via ``get_pending`` instead — otherwise the operator
        would get a fresh [E036] every single day."""
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        news_bot.job()
        mock_admin.reset_mock()
        mock_fetch_article.reset_mock()
        news_bot.job()

        mock_fetch_article.assert_not_called()
        self.assertEqual(self._calls_with(mock_admin, '[E036]'), [])
        self.assertEqual(len(pending_articles_repo.list_held()), 1)

    # -- precedence -------------------------------------------------------

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_hold_wins_over_the_genre_drop(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """A title that trips BOTH detectors must be HELD, not dropped —
        the operator gets to decide rather than losing the article."""
        link = 'https://t-hunted.blogspot.com/2026/07/video-poster.html'
        title = 'Vídeo: as fotos do novo poster da Hot Wheels 2026'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title, dict(self.POSTER_BODY, title=title))

        news_bot.job()

        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertEqual(len(self._calls_with(mock_admin, '[E036]')), 1)
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])

    # -- DROP -------------------------------------------------------------

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_unambiguous_video_review_is_dropped_at_intake(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Operator policy «очевидные резать»: only the explicit
        «Vídeo: …» / «Watch: …» headline form is dropped outright."""
        link = 'https://t-hunted.blogspot.com/2026/07/video-caixa-j.html'
        title = 'Vídeo: unboxing da caixa J de 2026 da Hot Wheels'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['Abrimos a caixa.'], 'images': []})

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # Never staged, and pinned so tomorrow's tick skips it at b2.
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertIsNone(pending_articles_repo.get_pending(link))
        self.assertEqual(pending_articles_repo.list_held(), [])
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {r[0] for r in conn.execute(
                'SELECT link FROM processed_news').fetchall()}
        finally:
            conn.close()
        self.assertIn(link, processed)

        e037 = self._calls_with(mock_admin, '[E037]')
        self.assertEqual(len(e037), 1, mock_admin.call_args_list)
        msg = e037[0].args[0]
        self.assertIn('Отсечён жанр', msg)
        self.assertIn('видео-обзор', msg)
        self.assertIn(link, msg)
        self.assertIn('unboxing', msg)
        # A dropped article carries no buttons — nothing to decide.
        self.assertIsNone(e037[0].kwargs.get('reply_markup'))
        self.assertEqual(self._calls_with(mock_admin, '[E036]'), [])

        self.assertTrue(
            [l for l in cm.output if '[funnel]' in l and 'genre=1' in l],
            "expected the [funnel] line to count genre=1",
        )

        # Next tick: the pin means the body is NOT fetched again.
        mock_fetch_article.reset_mock()
        news_bot.job()
        mock_fetch_article.assert_not_called()

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_event_announcement_is_dropped_at_intake(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        link = 'https://t-hunted.blogspot.com/2026/07/convencao-2026.html'
        title = 'Convenção Hot Wheels 2026: datas e ingressos'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['O evento acontece em julho.'],
                    'images': []})

        news_bot.job()

        self.assertIsNone(pending_articles_repo.get_pending(link))
        e037 = self._calls_with(mock_admin, '[E037]')
        self.assertEqual(len(e037), 1)
        self.assertIn('ивент', e037[0].args[0])
        self.assertIn('convenção', e037[0].args[0])

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_convention_exclusive_reveal_publishes_normally(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The critical false-positive guard, end-to-end: a
        convention-exclusive casting reveal — event name in the title,
        event logistics in the BODY — is ordinary model news and must
        reach the publishable queue untouched."""
        link = 'https://example.com/2026/07/convention-datsun.html'
        title = 'Hot Wheels Convention 2026 exclusive Datsun revealed'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': [
                        'The convention will be held in Los Angeles; '
                        'tickets and registration open in March.',
                        'Watch the reveal in the video below.',
                    ],
                    'images': []})

        news_bot.job()

        row = pending_articles_repo.get_pending(link)
        self.assertIsNotNone(row)
        self.assertIsNone(row['hold_reason'])
        self.assertIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(self._calls_with(mock_admin, '[E036]'), [])
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])

    # -- branch → action routing ------------------------------------------

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_ambiguous_video_post_is_staged_held_not_dropped(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Operator policy «спорные спрашивать» (2026-07-25), end to end
        against the REAL map — no patching.

        ``video_np`` is strong evidence but not proof (two review rounds
        found genuine reveals in this exact grammatical shape), so a wrong
        drop here would be unrecoverable. It now takes the same HOLD path
        as a poster: staged, parked, and offered to the operator.
        """
        link = 'https://t-hunted.blogspot.com/2026/07/unboxing-caixa-j.html'
        title = 'Unboxing da caixa J de 2026 da Hot Wheels'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['Abrimos a caixa.'], 'images': []})

        news_bot.job()

        # Staged and PARKED, not dropped: still in the DB, out of the
        # publishable queue, and offered to the operator with buttons.
        row = pending_articles_repo.get_pending(link)
        self.assertIsNotNone(row)
        self.assertIn('unboxing', row['hold_reason'])
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertNotIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()], [link])

        e036 = self._calls_with(mock_admin, '[E036]')
        self.assertEqual(len(e036), 1, mock_admin.call_args_list)
        msg = e036[0].args[0]
        self.assertIn('На утверждение', msg)
        self.assertIn(link, msg)
        # The REASON is legible: this is a suspected video review, NOT a
        # poster dump, and the matched markers say why.
        self.assertIn('видео-обзор', msg)
        self.assertNotIn('постер / каталог / упаковку', msg)
        self.assertIn('unboxing', msg)
        cds = [b.callback_data
               for r in e036[0].kwargs['reply_markup'].inline_keyboard
               for b in r]
        self.assertTrue(cds[0].startswith('hd:a:'), cds)

        # No drop happened: no [E037], and — critically — the link is NOT
        # pinned, so an approved article stays releasable.
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])
        conn = sqlite3.connect(self.db_path)
        try:
            processed = {r[0] for r in conn.execute(
                'SELECT link FROM processed_news').fetchall()}
        finally:
            conn.close()
        self.assertNotIn(link, processed)

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_two_signal_video_post_is_also_held(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The other soft branch (``video_signals``) takes the same path."""
        link = 'https://example.com/2026/07/video-unboxing-2027.html'
        title = 'Check the full video unboxing here for the 2027 lineup'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['Body.'], 'images': []})

        news_bot.job()

        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()], [link])
        self.assertEqual(len(self._calls_with(mock_admin, '[E036]')), 1)
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_a_held_genre_article_publishes_once_approved(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """The whole point of holding instead of dropping: the operator can
        still release it. Unpinned + still staged means «✅ Опубликовать»
        puts it straight back in the queue."""
        link = 'https://t-hunted.blogspot.com/2026/07/unboxing-caixa-k.html'
        title = 'Unboxing da caixa K de 2026 da Hot Wheels'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['Abrimos a caixa.'], 'images': []})

        with patch('news_bot.TELEGRAM_ADMIN_ID', '424242'):
            news_bot.job()
            e036 = self._calls_with(mock_admin, '[E036]')
            cd = e036[0].kwargs['reply_markup'].inline_keyboard[0][0]
            token = cd.callback_data[len('hd:a:'):]
            status, _ = news_bot.resolve_hold_callback('approve', token, 424242)

        self.assertEqual(status, "✅ Одобрено — выйдет в ближайший слот")
        self.assertIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(pending_articles_repo.count_pending(), 1)

    def test_branch_action_map_matches_the_operator_decision(self):
        """Pins the SHIPPED policy table exactly (operator 2026-07-25:
        «очевидные резать, спорные спрашивать»). Re-pointing any branch
        without updating this test — or leaving a re-point half-applied —
        fails here rather than silently changing what the bot publishes."""
        self.assertEqual(
            news_bot._GENRE_BRANCH_ACTION,
            {
                'video_lead': 'drop',     # «Vídeo:» / «Watch:» — explicit
                'video_np': 'hold',       # grammatical position — evidence
                'video_signals': 'hold',  # two lexical signals — evidence
                'event': 'drop',          # name + organisational word
            },
        )

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_funnel_separates_genre_holds_from_genre_drops(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """One tick with BOTH outcomes: a held article must be counted as
        `held_for_review`, never as `dropped_genre` — otherwise the plan
        ping would tell the operator the article is gone when it is
        actually waiting for them."""
        held_link = 'https://t-hunted.blogspot.com/2026/07/unboxing-lote-q.html'
        held_title = 'Unboxing do lote Q de 2026 da Hot Wheels'
        dropped_link = 'https://t-hunted.blogspot.com/2026/07/video-lote-q.html'
        dropped_title = 'Vídeo: abrimos a caixa do lote Q da Hot Wheels'

        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [
            {'link': held_link, 'title': held_title,
             'published': '2026-07-25', 'summary': 's'},
            {'link': dropped_link, 'title': dropped_title,
             'published': '2026-07-25', 'summary': 's'},
        ]
        mock_fetch_article.side_effect = lambda entry: {
            'title': entry['title'], 'subtitle': '',
            'paragraphs': ['Abrimos a caixa.'], 'images': [],
        }

        with self.assertLogs('news_bot', level='INFO') as cm:
            news_bot.job()

        # One of each, attributed to the right counter.
        self.assertTrue(
            [l for l in cm.output
             if '[funnel]' in l and 'genre=1' in l and 'held=1' in l],
            "expected [funnel] to show genre=1 held=1; got:\n"
            + "\n".join(l for l in cm.output if '[funnel]' in l),
        )
        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()],
            [held_link])
        self.assertIsNone(pending_articles_repo.get_pending(dropped_link))
        self.assertEqual(len(self._calls_with(mock_admin, '[E036]')), 1)
        self.assertEqual(len(self._calls_with(mock_admin, '[E037]')), 1)

        # Operator-facing rendering: the held one is NOT in «отсеяно», and
        # the plan ping shows it as awaiting approval.
        plan = [c.args[0] for c in mock_admin.call_args_list
                if c.args and ('[E008]' in c.args[0] or '[E009]' in c.args[0])]
        self.assertTrue(plan)
        self.assertTrue(any('На утверждении: 1' in m for m in plan), plan)
        self.assertTrue(any('отсеяно 1' in m for m in plan), plan)

    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_news_clause_video_headline_reaches_the_queue(
        self, mock_admin, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
    ):
        """Review round 2, R1 end-to-end: «Video of the X leaked online» is
        a reveal story and must publish normally."""
        link = 'https://example.com/2026/07/z06-video-leak.html'
        title = 'Video of the 2027 Corvette Z06 reveal leaked online'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, title,
                   {'title': title, 'subtitle': '',
                    'paragraphs': ['The clip shows the casting.'],
                    'images': []})

        news_bot.job()

        row = pending_articles_repo.get_pending(link)
        self.assertIsNotNone(row)
        self.assertIsNone(row['hold_reason'])
        self.assertIn(
            link, [r['link'] for r in pending_articles_repo.list_pending()])
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])
        self.assertEqual(self._calls_with(mock_admin, '[E036]'), [])

    # -- fail-open --------------------------------------------------------

    @patch('news_bot._hold_for_review_reason', side_effect=RuntimeError('boom'))
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_hold_detector_crash_is_fail_open(
        self, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article, _mock_hold,
    ):
        """A detector crash must not kill the tick (``job()`` runs inside
        a bare ``while True`` scheduler and the crash would land BEFORE
        mark_processed → crash-loop on restart). Fail-open = published as
        usual, NOT silently parked."""
        link = 'http://example.com/2026/07/ordinary.html'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, 'Ordinary Hot Wheels news',
                   {'title': 'Ordinary Hot Wheels news', 'subtitle': '',
                    'paragraphs': ['A new casting was revealed.'],
                    'images': []})

        news_bot.job()  # must not raise

        row = pending_articles_repo.get_pending(link)
        self.assertIsNotNone(row)
        self.assertIsNone(row['hold_reason'])
        self.assertEqual(self._calls_with(mock_admin, '[E036]'), [])

    @patch('news_bot._is_rejected_genre', side_effect=RuntimeError('boom'))
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_genre_detector_crash_is_fail_open(
        self, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article, _mock_genre,
    ):
        link = 'http://example.com/2026/07/ordinary2.html'
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   link, 'Ordinary Hot Wheels news',
                   {'title': 'Ordinary Hot Wheels news', 'subtitle': '',
                    'paragraphs': ['A new casting was revealed.'],
                    'images': []})

        news_bot.job()  # must not raise

        self.assertIsNotNone(pending_articles_repo.get_pending(link))
        self.assertEqual(self._calls_with(mock_admin, '[E037]'), [])

    @patch('news_bot.REVIEW_BUTTONS_ENABLED', True)
    @patch('news_bot.pending_repo.put_review_token',
           side_effect=RuntimeError('storage down'))
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    @patch('news_bot.send_admin_notification')
    def test_token_mint_failure_still_leaves_the_article_held(
        self, mock_admin, mock_load_feeds, mock_fetch_rss,
        mock_fetch_article, _mock_put,
    ):
        """The ping is best-effort; the HOLD is not. A storage fault while
        minting the token must not publish the article by accident — it
        stays parked and shows up in the daily «На утверждении» line."""
        self._feed(mock_load_feeds, mock_fetch_rss, mock_fetch_article,
                   self.POSTER_LINK, self.POSTER_TITLE, self.POSTER_BODY)

        news_bot.job()  # must not raise

        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_held()],
            [self.POSTER_LINK])
        self.assertEqual(pending_articles_repo.count_pending(), 0)


# ---------------------------------------------------------------------------
# source-formatting-parity Task 9 — end-to-end formatting chain (Phase 1,
# t-hunted only).
#
# Every link of the chain is already covered on its own (Task 5 the module,
# Task 7 the parser, Task 8 the guard, Task 6 the render surface). NOTHING
# ran them in sequence, and every wound this feature paid for lived on a
# SEAM: runs surviving the parser but lost in the SQLite JSON round-trip;
# alignment breaking between two lists rather than inside either one; the
# image cap not applying because the block renderer never knew about it.
#
# Harness rules for everything below, each one bought by an incident:
#
#   * ``telegraph_publisher._api_call`` is the mock point, NOT
#     ``publish_article``. Patching ``publish_article`` (as most of the
#     older classes in this file do) means the renderer never runs, no
#     node tree exists, and every assertion about ``strong`` / ``h3`` /
#     ``figure`` would be checking the test's own fixture.
#   * The LLM stand-in calls the REAL ``_llm_common._build_user_message``
#     (request encoding) and the REAL
#     ``_llm_common._patch_text_with_ru_paragraphs`` (response decoding), so
#     the ``**marker**`` round-trip under test is the production one. It
#     does NOT replace only the network hop: standing in for
#     ``transcreate_via_claude`` also skips ``_parse_response``,
#     ``_truncate_paragraphs`` and the caption second pass. That matters
#     once — the paragraph-divergence WARNING lives in ``_parse_response``,
#     so the log-silence test below covers the publish leg, not the whole
#     translation call. ``_parse_response``'s own floors are pinned by
#     ``tests/test_llm_common.py::TestSanityFloorRelaxation``.
#   * The parser is the real ``fetch_t_hunted_article``, reached through
#     the real ``news_bot.fetch_full_article`` domain routing; only the
#     ``requests`` session is replaced. Fixture URLs must therefore stay on
#     ``t-hunted.blogspot.com`` or the parser's SSRF allowlist returns
#     ``None`` and the test "passes" on emptiness.
# ---------------------------------------------------------------------------


T_HUNTED_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'fixtures', 'articles', 't_hunted',
)


def _iter_nodes(nodes):
    """Depth-first walk over a Telegra.ph node tree, yielding dict nodes."""
    for node in nodes:
        if isinstance(node, dict):
            yield node
            yield from _iter_nodes(node.get('children') or [])


def _nodes_with_tag(nodes, tag):
    return [n for n in _iter_nodes(nodes) if n.get('tag') == tag]


def _count_tag(nodes, tag):
    return len(_nodes_with_tag(nodes, tag))


def _node_text(node):
    """Concatenate every string descendant of ``node`` in document order."""
    parts = []

    def walk(children):
        for child in children:
            if isinstance(child, str):
                parts.append(child)
            elif isinstance(child, dict):
                walk(child.get('children') or [])

    walk(node.get('children') or [])
    return ''.join(parts)


def _texts_of_tag(nodes, tag):
    return [_node_text(n) for n in _nodes_with_tag(nodes, tag)]


def _bold_runs(block):
    return [r for r in (block.get('runs') or [])
            if 'bold' in (r.get('formats') or [])]


class _FormattingChainBase(_IntegrationBase):
    """Runs a t-hunted article through parser → queue → LLM → Telegraph.

    ``_stage`` drives the REAL ``news_bot.job()`` with only the RSS feed and
    the parser's HTTP session replaced, so the row in ``pending_articles``
    is produced by production code including ``_blocks_if_aligned``.
    ``_publish`` then calls the real ``_fallback_publish`` on that row and
    captures the node tree handed to ``createPage``.

    ``news_bot._fallback_publish`` is patched for the duration of the
    staging call only — ``job()`` would otherwise run its publish loop
    inside the staging step and the two legs could not be asserted apart.
    """

    LINK = 'https://t-hunted.blogspot.com/2026/01/post.html'
    #: Kept Hot-Wheels-relevant on purpose: ``_is_hot_wheels_relevant``
    #: drops sibling-brand titles before ``fetch_full_article`` is ever
    #: reached, and the article then never enters the chain at all.
    FEED_TITLE = 'Hot Wheels — novidades da semana'

    def setUp(self):
        super().setUp()
        self._start_patch(patch('news_bot.time.sleep'))
        # publish_article raises without a token before it renders anything.
        self._start_patch(
            patch.dict(os.environ, {'TELEGRAPH_ACCESS_TOKEN': 'test-token'}))
        self.published_trees = []   # node trees captured at createPage
        self.llm_payloads = []      # decoded _build_user_message output
        self.ru_results = []        # what the LLM stand-in handed back

    # -- fixtures ----------------------------------------------------------

    @staticmethod
    def fixture_html(name):
        path = os.path.join(T_HUNTED_FIXTURE_DIR, name)
        with open(path, encoding='utf-8') as fh:
            return fh.read()

    @staticmethod
    def build_html(inner, title='Novidades Hot Wheels'):
        """Minimal Blogger page in the shape the parser expects."""
        return (
            f'<html><body><h3 class="post-title">{title}</h3>'
            f'<div class="post-body">{inner}</div></body></html>'
        )

    @staticmethod
    def _http_response(html):
        response = MagicMock()
        response.text = html
        response.content = html.encode('utf-8')
        response.raise_for_status.return_value = None
        return response

    # -- chain legs --------------------------------------------------------

    def _stage(self, html, link=None, feed_title=None):
        """Run the prep phase over one t-hunted entry; return the staged row."""
        link = link or self.LINK
        session = MagicMock()
        session.get.return_value = self._http_response(html)
        entry = {
            'link': link,
            'title': feed_title or self.FEED_TITLE,
            'published': '2026-01-01',
            'summary': 'Summary',
        }
        with patch('t_hunted_source.requests.Session', return_value=session), \
                patch('news_bot.load_feeds',
                      return_value=['https://feed.example/rss']), \
                patch('news_bot.fetch_rss', return_value=[entry]), \
                patch('news_bot._fallback_publish'):
            news_bot.job()
        return pending_articles_repo.get_pending(link)

    def _translator(self, drop_markers=False, ru_prefix='РУ '):
        """LLM stand-in wired through the REAL request/response helpers.

        ``drop_markers=True`` models the AC6 failure: the model returns
        prose with every ``**`` gone. Everything else about the call is
        identical, which is what makes the two paths comparable.
        """
        def translate(row, **_kwargs):
            payload = json.loads(_llm_common._build_user_message(row))
            self.llm_payloads.append(payload)
            ru_paragraphs = []
            for paragraph in payload['paragraphs']:
                text = paragraph.replace('**', '') if drop_markers else paragraph
                # A hand-authored leading bullet stays leading: a translator
                # renders the line, it does not push punctuation inward.
                # This matters — Decision 10's doubling guard strips the
                # bullet off the TRANSLATED text, so a stand-in that buried
                # it mid-string would exercise a case no model produces.
                bullet = ''
                if text.lstrip().startswith('•'):
                    bullet = '• '
                    text = text.lstrip().lstrip('•').lstrip()
                ru_paragraphs.append(bullet + ru_prefix + text)
            result = {
                'title': ru_prefix + (row.get('title') or ''),
                'alts': ['Вариант один', 'Вариант два'],
                'subtitle': ru_prefix + (row.get('subtitle') or ''),
                'paragraphs': ru_paragraphs,
                'blocks': _llm_common._patch_text_with_ru_paragraphs(
                    row.get('blocks'), ru_paragraphs),
            }
            self.ru_results.append(result)
            return result
        return translate

    def _capture_api(self, method, data, session=None):
        if method == 'createPage':
            self.published_trees.append(json.loads(data['content']))
        return {'url': 'https://telegra.ph/test-page', 'path': 'test-page'}

    def _publish(self, row, translate=None):
        """Publish ``row`` for real; the node tree lands in published_trees."""
        with patch('news_bot.transcreate_via_claude',
                   side_effect=translate or self._translator()), \
                patch('news_bot.send_telegraph_teaser', return_value=True), \
                patch('telegraph_publisher._api_call',
                      side_effect=self._capture_api):
            return news_bot._fallback_publish(row)

    def _run_chain(self, html, link=None, translate=None, feed_title=None):
        """Whole chain; returns ``(row, node_tree)``."""
        row = self._stage(html, link=link, feed_title=feed_title)
        self.assertIsNotNone(
            row, 'the article never reached pending_articles — the chain '
                 'under test did not run at all')
        self._publish(row, translate=translate)
        self.assertTrue(
            self.published_trees,
            'createPage was never called — nothing was rendered')
        return row, self.published_trees[-1]

    #: Wipes the tables that make a second run of the same article a no-op
    #: (already processed / already published / already pending). REUSED from
    #: TestCrossSourceDedup by plain-function assignment rather than
    #: subclassing — the same idiom (and the same reason) as
    #: TestDedupReviewButtons above: inheriting would re-register that class's
    #: tests under this one.
    _reset_tables = TestCrossSourceDedup._reset_tables


class TestFormattingChainTHunted(_FormattingChainBase):
    """Scenario 1 — formatting survives parser → queue → LLM → Telegraph.

    Fixtures are real corpus articles, picked by measurement:
    ``johnny-lightning-american-graffiti`` carries 20 PARTIAL bold spans
    inside ordinary paragraphs (the interesting case — a whole-bold
    paragraph would be reclassified as a heading), and
    ``o-que-faz-um-hot-wheels-aumentar-de`` is the 45-paragraph article the
    user-spec was written against, whose 12 subheadings exist ONLY through
    the whole-bold heuristic (t-hunted has zero real ``<h2>/<h3>`` tags).
    """

    BOLD_FIXTURE = 'johnny-lightning-american-graffiti.html'
    HEADING_FIXTURE = 'o-que-faz-um-hot-wheels-aumentar-de.html'

    #: Measured on the frozen corpus fixtures (the same counts Task 7 recorded
    #: in decisions.md). These are PINNED NUMBERS, not derived ones, and that
    #: is the point: a test that reads its expectation out of the very row it
    #: is checking cannot tell "the bold survived" from "the source arrived
    #: with no bold at all" — both come out as ``0 == 0``. That exact hole let
    #: a runs-stripping mutation through on the first pass.
    EXPECTED_BOLD_SPANS = 20
    EXPECTED_HEADINGS = 12

    def _assert_fixture_still_carries_bold(self, row):
        self.assertEqual(
            len([r for b in row['blocks'] for r in _bold_runs(b)]),
            self.EXPECTED_BOLD_SPANS,
            'the staged row no longer carries the bold this fixture was '
            'chosen for — every count below would compare zero against zero')

    def test_bold_run_survives_parser_queue_translation_and_telegraph(self):
        """Asserted at BOTH levels, because the node tree alone is not proof.

        ``_decode_bold_markers`` in the publisher also makes bold out of bare
        ``**`` left sitting in the text, so a page full of ``<strong>`` can
        coexist with a completely dead runs path — the trap the task's own
        list names. The runs on the RU blocks and the tags on the page are
        one behaviour and are checked in one place.
        """
        row, nodes = self._run_chain(self.fixture_html(self.BOLD_FIXTURE))

        self._assert_fixture_still_carries_bold(row)
        source_bold = [r['text'] for b in row['blocks'] for r in _bold_runs(b)]

        # Level 1 — the runs on the blocks handed to the publisher.
        ru_blocks = self.ru_results[-1]['blocks']
        self.assertEqual(
            len([b for b in ru_blocks if _bold_runs(b)]),
            len([b for b in row['blocks'] if _bold_runs(b)]),
            'the RU blocks handed to the publisher lost their bold runs')
        for block in ru_blocks:
            self.assertNotIn(
                '**', block.get('text') or '',
                'a raw marker survived into the block text — the renderer '
                'would bold it by accident and the runs path is untested')

        # Level 2 — the nodes that actually went to createPage.
        strong_texts = _texts_of_tag(nodes, 'strong')
        self.assertEqual(
            len(strong_texts), len(source_bold),
            'every bold span of the source must reach the page as <strong>')
        self.assertEqual(strong_texts, source_bold)

    def test_blocks_round_trip_through_sqlite_with_runs_intact(self):
        """SQLite stores ``blocks`` as JSON text. The round-trip is where
        runs would silently vanish, and ``None`` vs ``[]`` is a live
        distinction downstream (Task 8 drops to NULL, not to an empty list).
        """
        row = self._stage(self.fixture_html(self.BOLD_FIXTURE))

        self.assertIsInstance(row['blocks'], list)
        self.assertNotEqual(
            row['blocks'], [],
            'blocks came back as an empty list — assertFalse would not tell '
            'this apart from None, and the two mean different things here')
        runs = [r for b in row['blocks'] for r in _bold_runs(b)]
        self.assertTrue(runs, 'bold runs did not survive the JSON round-trip')
        for run in runs:
            self.assertIsInstance(run['text'], str)
            self.assertEqual(run['formats'], ['bold'])

    def test_runs_are_encoded_as_bold_markers_in_the_llm_request(self):
        """``_build_user_message`` → ``_encode_format_markers`` is the leg
        that turns runs into something the model can preserve."""
        row, _nodes = self._run_chain(self.fixture_html(self.BOLD_FIXTURE))

        self._assert_fixture_still_carries_bold(row)
        sent = self.llm_payloads[-1]['paragraphs']
        source_bold = [r['text'] for b in row['blocks'] for r in _bold_runs(b)]
        marked = [p for p in sent if '**' in p]
        self.assertEqual(
            len(marked), len([b for b in row['blocks'] if _bold_runs(b)]),
            'the request lost its markers — the model never sees the emphasis')
        for span in source_bold:
            self.assertTrue(
                any(f'**{span}**' in p for p in sent),
                f'bold span {span!r} reached the LLM without its markers')

    def test_heading_block_reaches_telegraph_as_h3(self):
        """t-hunted has no real heading tags; every ``h3`` on the page comes
        from the whole-bold heuristic (Decision 2). 12 of them on this
        article — the user-spec's own measurement.
        """
        row, nodes = self._run_chain(self.fixture_html(self.HEADING_FIXTURE))

        headings = [b for b in row['blocks'] if b.get('type') == 'heading']
        self.assertEqual(
            len(headings), self.EXPECTED_HEADINGS,
            'the heuristic stopped finding the subheadings this article was '
            'measured to have — the h3 count below would then compare zero '
            'against zero')
        self.assertEqual(_count_tag(nodes, 'h3'), len(headings))

    def test_list_item_reaches_telegraph_with_exactly_one_bullet(self):
        """No corpus article carries a list, so the input is synthetic — and
        kept next to the assertion so its shape is readable from here.

        ONE of the two items authors its bullet by hand. That is what makes
        this a test of Decision 10's doubling guard rather than a test that
        the publisher can prepend a character: with a clean source only, the
        guard could be deleted and the count would still be 1.
        """
        row, nodes = self._run_chain(self.build_html(
            '<p>Primeiro parágrafo do post com bastante texto editorial.</p>'
            '<p>Segundo parágrafo do post com bastante texto editorial.</p>'
            '<ul><li>• Primeiro item da lista</li>'
            '<li>Segundo item da lista</li></ul>'
        ))

        list_items = [b for b in row['blocks'] if b.get('type') == 'list_item']
        self.assertEqual(len(list_items), 2)
        self.assertTrue(
            (list_items[0].get('text') or '').startswith('•'),
            'the hand-authored bullet vanished before the publisher saw it, '
            'so the doubling guard is not being exercised')
        bullets = [t for t in _texts_of_tag(nodes, 'p') if '•' in t]
        self.assertEqual(len(bullets), 2)
        for text in bullets:
            self.assertEqual(
                text.count('•'), 1,
                f'bullet doubled in {text!r} — the source bullet was not '
                f'stripped before the publisher prepended its own')

    def test_alignment_guard_does_not_fire_on_a_real_t_hunted_article(self):
        """End-to-end false-positive control for Task 8. A guard that fires
        on ordinary articles switches the whole feature off silently while
        every positive test above stays green.
        """
        with self.assertLogs('news_bot', level='INFO') as logs:
            row, nodes = self._run_chain(self.fixture_html(self.BOLD_FIXTURE))

        self.assertIsNotNone(
            row['blocks'], 'the alignment guard dropped the blocks of a '
                           'normal article — the feature is off in production')
        self.assertEqual(
            [line for line in logs.output if '[align]' in line], [])
        self.assertGreater(_count_tag(nodes, 'strong'), 0)


class TestMarkersLostDegradeSilently(_FormattingChainBase):
    """Scenario 2 — AC6 / Decision 3b: when the model returns no markers the
    article publishes as plain text and the operator hears NOTHING.

    Silence is the requirement, not an omission. The project's alert
    vocabulary already runs E001–E038; a ping per article with no bold
    would train the operator to swipe pings away, and the next real E011 or
    E015 would drown in that noise. An absence assertion is the most
    fragile kind of test there is, so the class also proves the ping path
    is reachable in this very harness.
    """

    FIXTURE = 'johnny-lightning-american-graffiti.html'

    def _assert_the_publish_actually_ran(self, row):
        """Both assertions in this class are assertions of ABSENCE, and an
        absence is trivially true when the code never executed. Verified the
        hard way: stubbing ``_fallback_publish`` out entirely left both of
        them green until this check existed.
        """
        self.assertTrue(
            self.published_trees,
            'createPage was never reached — "no ping" and "no WARNING" are '
            'then statements about code that did not run')
        self.assertIsNotNone(
            pending_articles_repo.get_published(row['link']),
            'the article never reached published_articles — the degraded '
            'publish under test did not complete')

    def test_markers_lost_publishes_plain_text(self):
        """Run the SAME article twice — markers kept, markers lost — and
        compare the two pages.

        The two arms alone are not enough, though, and the gap is specific:
        whole-node loss is INVISIBLE to them, because both arms lose the
        same nodes. So the arms are paired with a containment check over
        ``p`` AND ``h3`` — the ``h3`` half matters, since the whole-bold
        heading heuristic sends some paragraphs there and a ``p``-only check
        reports them as missing. Measured on this fixture: 88 RU paragraphs
        against 90 rendered nodes, nothing dropped.
        """
        lost_row, lost_nodes = self._run_chain(
            self.fixture_html(self.FIXTURE),
            translate=self._translator(drop_markers=True))
        self._reset_tables()
        self.published_trees.clear()
        kept_row, kept_nodes = self._run_chain(self.fixture_html(self.FIXTURE))

        self.assertTrue(
            [b for b in lost_row['blocks'] if _bold_runs(b)],
            'the source had no bold, so losing it proves nothing')
        self.assertEqual(
            _count_tag(lost_nodes, 'strong'), 0,
            'markers were dropped by the model but bold appeared anyway')
        self.assertGreater(_count_tag(kept_nodes, 'strong'), 0)
        for tag in ('p', 'h3'):
            with self.subTest(tag=tag):
                self.assertEqual(
                    _texts_of_tag(lost_nodes, tag),
                    _texts_of_tag(kept_nodes, tag),
                    'the degraded page carries different TEXT than the '
                    'formatted one — degradation is supposed to cost only '
                    'the emphasis')
        self.assertEqual(lost_row['paragraphs'], kept_row['paragraphs'])

        # Whole-node loss slips past the two arms — they would truncate
        # identically. Assert every staged paragraph is actually ON the page.
        rendered = (_texts_of_tag(lost_nodes, 'p')
                    + _texts_of_tag(lost_nodes, 'h3'))
        missing = [p for p in lost_row['paragraphs']
                   if not any(p in text for text in rendered)]
        self.assertEqual(
            missing, [],
            f'{len(missing)} staged paragraph(s) never reached the page')

        self.assertIsNotNone(
            pending_articles_repo.get_published(lost_row['link']))

    def test_markers_lost_send_no_admin_notification(self):
        row = self._stage(self.fixture_html(self.FIXTURE))
        # Staging pings (E0xx funnel alerts) must not be counted as if they
        # came from the publish under test.
        self.mock_notify.reset_mock()

        self._publish(row, translate=self._translator(drop_markers=True))

        self._assert_the_publish_actually_ran(row)
        self.assertEqual(
            self.mock_notify.call_args_list, [],
            'losing the markers pinged the operator — Decision 3b requires '
            'silence, and a per-article ping buries the real alerts')

    def test_markers_lost_positive_control_a_real_alert_still_pings(self):
        """Positive control. Without it, the assertion above is green even
        when the notification code is simply never reached.

        The event is a zombie pending row: the link is already in
        ``published_articles``, so ``_fallback_publish`` short-circuits on
        its idempotency guard and pings. That guard sits at the TOP of the
        function, so this proves the ping path is wired in this harness —
        not that a ping could be raised from inside the translated region.
        The check above is what covers the region itself.
        """
        row = self._stage(self.fixture_html(self.FIXTURE))
        self._publish(row, translate=self._translator(drop_markers=True))
        self.assertIsNotNone(pending_articles_repo.get_published(row['link']))

        pending_articles_repo.insert_pending(row)   # the zombie
        zombie = pending_articles_repo.get_pending(row['link'])
        self.mock_notify.reset_mock()

        self._publish(zombie, translate=self._translator(drop_markers=True))

        self.assertEqual(
            self.mock_notify.call_count, 1,
            'the same harness cannot deliver a ping at all — the silence '
            'asserted by the test above means nothing')

    def test_markers_lost_leave_no_warning_or_error_in_the_log(self):
        """Decision 4 makes the asymmetry deliberate: a desync WARNs, a lost
        marker does not. WARNING stays a signal worth reading."""
        row = self._stage(self.fixture_html(self.FIXTURE))

        with self.assertNoLogs(level='WARNING'):
            self._publish(row, translate=self._translator(drop_markers=True))

        self._assert_the_publish_actually_ran(row)


class TestBoldHeavyArticle(_FormattingChainBase):
    """Scenario 3 — AC7: there is no "too much bold" threshold, and adding
    one later must fail loudly here.

    The fixture is synthetic because no corpus article is bold-heavy. Two
    DISTINCT bold spans per paragraph on purpose: ``_encode_format_markers``
    locates runs with ``str.find``, so two identical spans would collapse
    into one marker and the count assertion would quietly weaken.
    """

    PARAGRAPHS = 12
    #: Two bold spans per paragraph, minus the first paragraph — the subtitle
    #: lift moves it out of ``blocks`` before anything downstream sees it, so
    #: 24 authored ``<b>`` tags arrive as 22 spans. Pinned rather than counted
    #: off the row for the same reason as TestFormattingChainTHunted: an
    #: expectation read out of the object under test cannot tell "every span
    #: survived" from "the spans were merged or lost on the way in". Verified
    #: — coalescing each block's runs into one keeps the ≥0.9 ratio guard
    #: happy and left all three tests green until this constant existed.
    EXPECTED_BOLD_SPANS = 2 * (PARAGRAPHS - 1)

    def _paragraph_source(self, index, bold=True):
        first = f'Lançamento {index} da série Boulevard em escala 1:64'
        second = f'edição limitada número {index} com rodas Real Riders'
        if bold:
            return f'<p><b>{first}</b> e <b>{second}</b>.</p>'
        return f'<p>{first} e {second}.</p>'

    def _html(self, bold=True):
        return self.build_html(''.join(
            self._paragraph_source(i, bold=bold)
            for i in range(self.PARAGRAPHS)))

    def test_bold_heavy_every_span_reaches_telegraph_nodes(self):
        row, nodes = self._run_chain(self._html())

        source_bold = [r['text'] for b in row['blocks'] for r in _bold_runs(b)]
        self.assertEqual(
            len(source_bold), self.EXPECTED_BOLD_SPANS,
            'the staged row does not carry the spans the fixture authors — '
            'the equality below would then compare two equally wrong lists')
        bold_chars = sum(len(s) for s in source_bold)
        total_chars = sum(len(b.get('text') or '') for b in row['blocks'])
        self.assertGreaterEqual(
            bold_chars / total_chars, 0.9,
            'the fixture is not bold-heavy, so it does not exercise AC7')

        self.assertEqual(_texts_of_tag(nodes, 'strong'), source_bold)

    def test_bold_heavy_article_is_not_truncated_or_dropped(self):
        """Same article twice — with and without the ``<b>`` tags. Bold must
        change the markup and nothing else."""
        bold_row, bold_nodes = self._run_chain(self._html())
        self._reset_tables()
        self.published_trees.clear()
        plain_row, plain_nodes = self._run_chain(self._html(bold=False))

        self.assertEqual(
            len([r for b in bold_row['blocks'] for r in _bold_runs(b)]),
            self.EXPECTED_BOLD_SPANS,
            'the bold arm lost or merged spans on the way in — the text '
            'comparison below is blind to that')
        self.assertEqual(len(bold_row['paragraphs']),
                         len(plain_row['paragraphs']))
        self.assertEqual(bold_row['paragraphs'], plain_row['paragraphs'])
        self.assertEqual(
            [t for t in _texts_of_tag(bold_nodes, 'p')],
            [t for t in _texts_of_tag(plain_nodes, 'p')],
            'the bold-heavy article rendered different text than its plain '
            'twin — something dropped or truncated it')
        self.assertEqual(_count_tag(plain_nodes, 'strong'), 0)
        self.assertGreater(_count_tag(bold_nodes, 'strong'), 0)

    def test_bold_heavy_fixture_stays_within_resource_bounds(self):
        """Self-check on the fixture. Past the Decision 8 bounds the runs are
        dropped ON PURPOSE, and the two tests above would then be measuring
        the resource fuse instead of AC7 — passing or failing for a reason
        that has nothing to do with formatting.
        """
        row = self._stage(self._html())

        self.assertEqual(
            len([r for b in row['blocks'] for r in _bold_runs(b)]),
            self.EXPECTED_BOLD_SPANS,
            'checking bounds against a row that lost its spans proves nothing')
        for block in row['blocks']:
            with self.subTest(text=(block.get('text') or '')[:40]):
                text_len = len(block.get('text') or '')
                run_count = len(block.get('runs') or [])
                self.assertLess(text_len, dom_blocks.MAX_TEXT_FOR_RUNS)
                self.assertLess(run_count, dom_blocks.MAX_RUNS_PER_BLOCK)
                # Task 4's request-path bound and the render-path bound are
                # separate constants; the fixture has to clear both.
                self.assertLess(text_len, _llm_common._MAX_TEXT_FOR_RUNS)
                self.assertLess(run_count, _llm_common._MAX_RUNS_PER_BLOCK)
                self.assertLess(
                    text_len, telegraph_publisher._MAX_TEXT_FOR_RUNS)
                self.assertLess(
                    run_count, telegraph_publisher._MAX_RUNS_PER_BLOCK)


class TestPerSourceImageCap(_FormattingChainBase):
    """Scenario 4 — AC9 / Decision 5: the cap is a VALUE carried from
    ``news_bot.SOURCE_IMAGE_LIMITS`` into the block renderer.

    Checking the default alone would also pass against a constant hard-wired
    in the renderer — precisely the defect Decision 5 forbids ("a single
    hard-coded default would violate AC9 for somebody"). Only a second run
    of the same article under a different cap tells the two apart.
    """

    IMAGE_COUNT = 35   # above t-hunted's cap of 30 — no corpus article is
    CONTROL_CAP = 5

    def _html(self, image_count=None):
        count = self.IMAGE_COUNT if image_count is None else image_count
        images = ''.join(
            f'<img src="https://blogger.googleusercontent.com/img/a/'
            f'pic{i}=s1600/photo{i}.jpg" />'
            for i in range(count))
        return self.build_html(
            '<p>Primeiro parágrafo do post com bastante texto editorial.</p>'
            '<p>Segundo parágrafo do post com bastante texto editorial.</p>'
            + images)

    def _publish_with_cap(self, cap, link, image_count=None):
        """Run the whole chain once and return the rendered figure nodes.

        ``cap`` is injected by replacing the per-source table, NOT by
        calling the renderer directly — the point is to prove the value
        travels row → ``_image_limit_for_source`` → ``publish_article``.
        """
        self._reset_tables()
        self.published_trees.clear()
        limits = dict(news_bot.SOURCE_IMAGE_LIMITS)
        limits['t-hunted'] = cap
        with patch('news_bot.SOURCE_IMAGE_LIMITS', limits):
            _row, nodes = self._run_chain(
                self._html(image_count=image_count), link=link)
        return _nodes_with_tag(nodes, 'figure')

    def test_image_limit_is_honoured_by_value_and_a_lower_cap_yields_fewer(self):
        default_cap = news_bot.SOURCE_IMAGE_LIMITS['t-hunted']
        self.assertLess(
            default_cap, self.IMAGE_COUNT,
            'the fixture no longer exceeds the cap, so nothing is capped')

        at_default = self._publish_with_cap(
            default_cap, 'https://t-hunted.blogspot.com/2026/01/cap-default.html')
        at_control = self._publish_with_cap(
            self.CONTROL_CAP,
            'https://t-hunted.blogspot.com/2026/01/cap-control.html')

        self.assertEqual(len(at_default), default_cap)
        self.assertEqual(len(at_control), self.CONTROL_CAP)
        self.assertLess(
            len(at_control), len(at_default),
            'both runs rendered the same number of images — the configured '
            'value is not reaching the renderer, it is hard-coded there')

    def test_image_limit_keeps_the_hero_and_drops_from_the_tail(self):
        """The first figure is what Telegram lifts for the link preview, so
        the cap has to eat the tail, never the head.

        The whole kept SEQUENCE is pinned, not just its first element: a cap
        that kept the hero and then shuffled the rest would satisfy a
        first-element check while reordering the article's photos.
        """
        figures = self._publish_with_cap(
            self.CONTROL_CAP,
            'https://t-hunted.blogspot.com/2026/01/cap-hero.html')

        srcs = [_nodes_with_tag([f], 'img')[0]['attrs']['src'] for f in figures]
        self.assertEqual(len(srcs), self.CONTROL_CAP)
        for position, src in enumerate(srcs):
            self.assertIn(
                f'pic{position}=', src,
                f'figure {position} is not the {position}-th source image — '
                f'the cap reordered or re-anchored the gallery')

    def test_image_limit_that_does_not_bite_truncates_nothing(self):
        """Negative control: a cap that does not bite must not remove
        anything. Without it, "few images" reads as "cap works"."""
        under = self.CONTROL_CAP - 2
        figures = self._publish_with_cap(
            self.CONTROL_CAP,
            'https://t-hunted.blogspot.com/2026/01/cap-under.html',
            image_count=under)

        self.assertEqual(len(figures), under)


class TestChecklistFloorNotNewlyDropped(_FormattingChainBase):
    """Scenario 5 — the two independent floors this feature can disturb.

    Block extraction re-derives the flat ``paragraphs`` list, and both the
    paragraph COUNT and the total text LENGTH feed thresholds:

      * ``news_bot._CHECKLIST_BODY_TEXT_FLOOR`` (500) — intake reject,
        measured on the summed paragraph lengths;
      * the sanity floor in ``_llm_common._parse_response`` — translation
        reject, switched on by ``expected_paragraph_count >= 2``.

    The second one is the 2026-05-31 outage verbatim: all four t-hunted
    slots failed with "paragraphs total content too short" on
    single-paragraph photo-gallery posts. A gallery post that came out of
    block extraction with TWO paragraphs would arm that floor again.
    """

    #: "checklist" in the title arms trigger B. The URL must NOT contain
    #: ``case-contents-checklist`` — that slug is an unconditional trigger
    #: and the test would then be measuring the wrong branch.
    CHECKLIST_TITLE = 'Hot Wheels checklist do mês'
    BORDERLINE_LINK = 'https://t-hunted.blogspot.com/2026/01/checklist-post.html'

    def _borderline_html(self):
        """Title says "checklist", body sits just ABOVE the 500-char floor.

        Seven paragraphs, because the subtitle lift takes the first one out
        of ``paragraphs`` before the floor is measured — six survive and sum
        to ~588 characters. Six paragraphs would leave 490 and the article
        would be dropped for being genuinely short, not for anything this
        feature did.
        """
        return self.build_html(''.join(
            f'<p>Parágrafo {i} com texto editorial suficiente sobre as '
            f'miniaturas e os lançamentos recentes da marca.</p>'
            for i in range(7)
        ), title=self.CHECKLIST_TITLE)

    def _bare_checklist_html(self):
        """A real bare checklist: a one-line intro and model names.

        Deliberately sized so the surviving body lands BETWEEN 50 and 500
        characters. Anything shorter would still be rejected by a floor
        lowered to 50, and the negative control below would stay green while
        the floor it guards had been gutted.
        """
        return self.build_html(
            '<p>Confira a lista completa do case.</p>'
            '<p>Nissan Skyline, Toyota Supra, Honda Civic.</p>'
            '<p>Mazda RX-7, Subaru Impreza, Ford Escort.</p>',
            title=self.CHECKLIST_TITLE)

    def _funnel_checklist_count(self, logs):
        """Read ``checklist=N`` out of the job's ``[funnel]`` summary line.

        The counter sits inside ``dropped(no_article=0,checklist=0,...)``,
        so it is a comma-separated field, not a whitespace-separated token.
        """
        for line in logs.output:
            if '[funnel]' not in line:
                continue
            match = re.search(r'checklist=(\d+)', line)
            self.assertIsNotNone(
                match, f'the [funnel] line lost its checklist counter: {line!r}')
            return int(match.group(1))
        self.fail(f'no [funnel] line in the job log: {logs.output!r}')

    def test_borderline_checklist_article_is_still_staged(self):
        with self.assertLogs('news_bot', level='INFO') as logs:
            row = self._stage(self._borderline_html(),
                              link=self.BORDERLINE_LINK,
                              feed_title=self.CHECKLIST_TITLE)

        self.assertIsNotNone(
            row, 'the borderline checklist article stopped being staged — '
                 'block extraction moved it under the intake floor')
        self.assertEqual(self._funnel_checklist_count(logs), 0)
        self.assertGreater(
            sum(len(p) for p in row['paragraphs']),
            news_bot._CHECKLIST_BODY_TEXT_FLOOR,
            'the fixture drifted below the floor it is supposed to sit above')

    def test_flag_on_and_flag_off_agree_on_the_checklist_verdict(self):
        """Extracting blocks must not change an intake DECISION. The flat
        text is the same in both flag states; only ``blocks`` differs.

        BOTH directions of the verdict are checked. A one-sided invariant is
        what cost this project on 2026-07-28: "stays staged" alone is
        satisfied by an intake that has stopped rejecting anything at all.
        """
        for label, html, expect_staged in (
            ('borderline — stays in', self._borderline_html(), True),
            ('bare — stays out', self._bare_checklist_html(), False),
        ):
            with self.subTest(article=label):
                self._reset_tables()
                on = self._stage(html, link=self.BORDERLINE_LINK,
                                 feed_title=self.CHECKLIST_TITLE)
                self._reset_tables()
                with patch('feature_flags.SOURCE_FORMATTING_ENABLED', False):
                    off = self._stage(html, link=self.BORDERLINE_LINK,
                                      feed_title=self.CHECKLIST_TITLE)

                self.assertEqual(on is not None, expect_staged)
                self.assertEqual(
                    off is not None, expect_staged,
                    'the kill switch changed the intake verdict — turning '
                    'the feature off is supposed to cost formatting, not '
                    'change which articles get published')
                if expect_staged:
                    self.assertEqual(on['paragraphs'], off['paragraphs'])
                    self.assertIsInstance(on['blocks'], list)
                    self.assertIsNone(
                        off['blocks'],
                        'the kill switch did not reach the parser — '
                        'flipping it in an incident would change nothing')

    def test_single_paragraph_gallery_post_stays_single_paragraph(self):
        """The dominant t-hunted format, and the shape of the 2026-05-31
        outage. One paragraph keeps the sanity floor disarmed; two arms it,
        and a thin translation of a photo post strikes the slot."""
        row = self._stage(self.fixture_html(
            'novidades-muito-interessantes-da-m2.html'))

        self.assertEqual(
            len(row['paragraphs']), 1,
            'block extraction changed the paragraph count of a gallery post')
        patchable = [b for b in row['blocks']
                     if b.get('type') in _llm_common._PATCHED_TEXT_BLOCK_TYPES]
        self.assertEqual(len(patchable), 1)
        # The count asserted above IS the input to ``_parse_response``'s
        # sanity floor, which arms at ``expected_paragraph_count >= 2``.
        # What the floor then does at each count is pinned by
        # tests/test_llm_common.py::TestSanityFloorRelaxation — re-asserting
        # it here would pass with this whole chain deleted.

    def test_true_bare_checklist_is_still_dropped(self):
        """Negative control: without it the staging test above is green
        simply because the reject path is broken."""
        with self.assertLogs('news_bot', level='INFO') as logs:
            row = self._stage(self._bare_checklist_html(),
                              link=self.BORDERLINE_LINK,
                              feed_title=self.CHECKLIST_TITLE)

        self.assertIsNone(row)
        self.assertEqual(self._funnel_checklist_count(logs), 1)


if __name__ == '__main__':
    unittest.main()
