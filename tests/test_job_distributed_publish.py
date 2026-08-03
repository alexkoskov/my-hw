#!/usr/bin/env python3
"""Tests for the distributed-publish refactor of ``news_bot.job()`` (Task 8).

Covers:

* **Crash-loop guard (Decision 9).** ``job()`` reads ``MAX(published_at)`` and
  sleeps until ``last_published + MIN_INTERVAL_MINUTES`` if the gap is too
  small. Defends against systematic container restarts producing
  burst-publishes.
* **Cron change (Decision 2 + 4).** ``main()`` registers a TZ-aware fixed-time
  daily cron (``schedule.every().day.at("12:00", tz=pytz.timezone(
  "Europe/Moscow"))``) — verifying that ``schedule==1.2.1`` accepts a
  ``pytz.BaseTzInfo`` and rejects ``zoneinfo.ZoneInfo``.
* **Startup health checks (Decision 14).** ``main()`` calls
  ``claude_transcreation.health_check()`` and pings + flips
  ``outage_state.set_fallback_active(True)`` on ``False``; warns on
  ``TZ != 'Europe/Moscow'``.
* **Distributed-publish loop (Decision 15 + tech-spec §How-it-works step 7).**
  ``job()`` after the fetch+insert phase calls ``compute_fixed_slots`` and
  iterates the result, sleeping until each slot, then publishing via
  ``_fallback_publish`` (or a Google-only path if ``is_fallback_active()``).
  Window-end guard breaks the loop on slots beyond the window.

ANTHROPIC_API_KEY is NOT in ``.env`` per project Constraints — every test
mocks ``claude_transcreation.health_check`` and ``transcreate_via_claude``.
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
import pending_articles_repo
import outage_state
from claude_transcreation import ClaudeOutageError, ClaudeTranscreationError


MSK = pytz.timezone("Europe/Moscow")


# ---------------------------------------------------------------------------
# Shared scaffolding — tempfile DB + safe env-var stubs.
# ---------------------------------------------------------------------------

class _JobBase(unittest.TestCase):
    """Tempfile DB + token/channel/admin patches; init schema once per test.

    Tests inherit and add their own per-test patches (sources, sleep, claude,
    schedule, etc.). The base intentionally does NOT patch ``time.sleep`` —
    crash-loop-guard tests assert sleep arguments and need the real binding.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # -- helpers --------------------------------------------------------

    def _seed_published(self, link, ts_iso):
        """Insert a row into ``published_articles`` with explicit ``published_at``.

        ``ts_iso`` is a naive UTC ISO string (the column default uses SQLite
        ``CURRENT_TIMESTAMP`` which is UTC). The crash-loop guard interprets
        the column as UTC-naive on read.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link, 'T', 'РУ T', 'https://telegra.ph/x', 'autoevolution',
                 0, ts_iso),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _entry(link, title='Title', source_name='autoevolution'):
        return {
            'link': link,
            'title': title,
            'published': '2025-01-01',
            'summary': '',
            'feed_url': 'http://example.com/feed.xml',
            'source_name': source_name,
        }

    @staticmethod
    def _article_payload(title='Title'):
        return {
            'title': title,
            'subtitle': 'Lead',
            'paragraphs': ['p1.', 'p2.'],
            'images': [],
        }


def _patch_sources_empty():
    """Replace ``SOURCES`` with two no-op fetchers — used by tests that
    do NOT care about the fetch phase but want a deterministic empty
    pending insert step."""
    def _empty(notifier=None):
        return []
    return patch('news_bot.SOURCES', [_empty, _empty])


def _patch_sources_returning(rss_entries):
    """Single-source ``SOURCES`` patch returning ``rss_entries`` from the
    first fetcher (RSS-equivalent) and nothing from the second."""
    def _rss(notifier=None):
        return list(rss_entries)

    def _mattel(notifier=None):
        return []

    return patch('news_bot.SOURCES', [_rss, _mattel])


# ---------------------------------------------------------------------------
# Crash-loop guard tests (Decision 9)
# ---------------------------------------------------------------------------

class TestCrashLoopGuard(_JobBase):
    """``job()`` enters the crash-loop guard early — BEFORE fetching sources.

    Asserts the ``time.sleep`` argument is approximately
    ``MIN_INTERVAL_MINUTES * 60 - elapsed_seconds`` when the most recent
    publish is younger than the interval; zero (no sleep on guard) when
    the gap is wide enough; and that ``MAX IS NULL`` is tolerated.
    """

    def _sleep_args(self, mock_sleep):
        """Return the sleep argument from the FIRST positional time.sleep
        call. ``job()`` may also sleep inside the publish loop — but on an
        empty pending pool it will not enter the loop, so the FIRST call
        is the guard."""
        if not mock_sleep.call_args_list:
            return None
        return mock_sleep.call_args_list[0].args[0]

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    def test_sleeps_when_last_published_recent(self, mock_sleep, _mock_admin):
        # Seed published row 5 minutes ago (UTC-naive — matches column).
        five_min_ago = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=5)
        self._seed_published(
            'https://example.com/recent',
            five_min_ago.strftime('%Y-%m-%d %H:%M:%S'),
        )

        with _patch_sources_empty():
            news_bot.job()

        # Expected: (MIN_INTERVAL_MINUTES * 60) - 5*60 seconds, ± 5%.
        self.assertTrue(
            mock_sleep.called,
            "time.sleep must be called by the crash-loop guard",
        )
        first_sleep = self._sleep_args(mock_sleep)
        self.assertIsNotNone(first_sleep)
        expected = news_bot.MIN_INTERVAL_MINUTES * 60 - 5 * 60
        self.assertGreater(first_sleep, expected * 0.95)
        self.assertLess(first_sleep, expected * 1.05)

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    def test_no_sleep_when_last_published_old(self, mock_sleep, _mock_admin):
        # Seed publish older than the guard threshold so no sleep fires.
        gap_minutes = news_bot.MIN_INTERVAL_MINUTES + 10
        long_ago = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=gap_minutes)
        self._seed_published(
            'https://example.com/old',
            long_ago.strftime('%Y-%m-%d %H:%M:%S'),
        )

        with _patch_sources_empty():
            news_bot.job()

        # Empty pending → loop won't run → no time.sleep call from job() at
        # all. (Other module sleeps — e.g. inside _fallback_publish — are
        # unreachable on empty pending.)
        self.assertEqual(mock_sleep.call_count, 0,
                         f"unexpected sleep calls: {mock_sleep.call_args_list!r}")

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    def test_no_sleep_when_no_published_rows(self, mock_sleep, _mock_admin):
        # No seed — published_articles is empty.
        with _patch_sources_empty():
            news_bot.job()
        # Guard reads MAX(published_at) → NULL → skip; nothing else sleeps.
        self.assertEqual(mock_sleep.call_count, 0)


# ---------------------------------------------------------------------------
# Cron registration tests (Decision 2 + 4)
# ---------------------------------------------------------------------------

class TestCronRegistration(unittest.TestCase):
    """``main()`` registers a TZ-aware fixed-time cron via the live
    ``schedule`` library — no fake-clock substitution.

    The first test instantiates the schedule entry the same way ``main()``
    does and asserts no exception. The second documents the limitation
    that ``schedule==1.2.1`` does NOT accept a stdlib ``zoneinfo`` object.
    """

    def test_schedule_at_accepts_pytz_timezone(self):
        import schedule
        s = schedule.Scheduler()
        # Should not raise.
        job = s.every().day.at("12:00", tz=pytz.timezone("Europe/Moscow")).do(
            lambda: None
        )
        self.assertIsNotNone(job)
        # Cleanup so we don't leak entries into other tests using the
        # global default scheduler.
        s.clear()

    def test_schedule_at_rejects_zoneinfo(self):
        import schedule
        from zoneinfo import ZoneInfo
        s = schedule.Scheduler()
        with self.assertRaises(schedule.ScheduleValueError):
            s.every().day.at("12:00", tz=ZoneInfo("Europe/Moscow")).do(
                lambda: None
            )
        s.clear()

    def test_main_registers_tz_aware_daily_cron(self):
        """``main()`` registers the daily tick at 10:00 МСК.

        We assert the source rather than running ``main()`` (which loops
        forever). This complements ``test_schedule_at_accepts_pytz_timezone``
        — together they prove the registration line both *exists* and
        *executes* without ``ScheduleValueError``.

        The assertion matches the actual ``schedule.every().day.at(...)``
        CALL, not a bare substring. Until 2026-08-03 this test asserted
        ``'12:00' in src``, which was satisfied by a stale ``12:00`` in the
        docstring while the real registration said ``10:00`` — so it would
        have stayed green through any change to the publish time. Keep the
        regex anchored to the call.
        """
        import inspect
        import re
        src = inspect.getsource(news_bot.main)
        m = re.search(
            r'schedule\.every\(\)\.day\.at\(\s*"(\d{2}:\d{2})"\s*,\s*tz=',
            src,
        )
        self.assertIsNotNone(
            m, "no schedule.every().day.at(\"HH:MM\", tz=...) call in main()"
        )
        self.assertEqual(
            m.group(1), "10:00",
            "daily tick must fire at 10:00 МСК — the first publish slot",
        )
        self.assertIn('Europe/Moscow', src)
        # Old 12-hour cadence must be removed.
        self.assertNotIn('every(12).hours', src)


# ---------------------------------------------------------------------------
# Startup health-check tests (Decision 14)
# ---------------------------------------------------------------------------

class TestMainHealthChecks(unittest.TestCase):
    """``main()`` runs two startup health checks BEFORE registering cron:

    1. ``claude_transcreation.health_check()`` — on ``False``, admin-ping +
       ``outage_state.set_fallback_active(True)``.
    2. ``os.getenv('TZ') == 'Europe/Moscow'`` — on mismatch, admin warning ping.

    Neither blocks startup. We mock the schedule + main loop to prevent
    ``main()`` from actually running forever.
    """

    def setUp(self):
        # Tempfile DB so init_db doesn't touch the real file.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self._db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self._db_patcher.start()

    def tearDown(self):
        self._db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _run_main_once(self, **extra_patches):
        """Helper: run ``main()`` with the cron-loop neutralised so the
        function returns after registration + first ``job()`` invocation.

        ``schedule.run_pending`` and the ``while True`` loop are short-
        circuited by raising ``SystemExit`` from the first ``time.sleep``
        call inside the loop. The first ``job()`` call still happens.
        """
        sleep_calls = {'count': 0}

        def fake_sleep(secs):
            # The loop sleep is the first sleep AFTER job() returns. We
            # detect the transition by counting calls; the first call
            # from inside main()'s "while True" gets us out.
            sleep_calls['count'] += 1
            if sleep_calls['count'] >= 1:
                raise SystemExit('break-main-loop')

        # Patch in deps. ``job`` is replaced with a no-op so we don't run
        # the full prep flow (which is exercised separately).
        ctx_patches = [
            patch('news_bot.job'),
            patch('news_bot.telegraph_publisher.ensure_access_token'),
            patch('news_bot.schedule'),
            patch('news_bot.time.sleep', side_effect=fake_sleep),
        ]
        for name, p in extra_patches.items():
            ctx_patches.append(p)

        started = []
        try:
            for p in ctx_patches:
                started.append(p.start())
            try:
                news_bot.main()
            except SystemExit:
                pass
        finally:
            for p in ctx_patches:
                p.stop()

    @patch.dict(os.environ, {'TZ': 'Europe/Moscow'})
    @patch('news_bot.outage_state.set_fallback_active')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.claude_transcreation.health_check', return_value=False)
    def test_main_pings_admin_when_health_check_returns_false(
        self, _mock_health, mock_admin, mock_set_fallback,
    ):
        self._run_main_once()
        # Some admin-ping was sent that mentions Claude probe failure.
        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        self.assertTrue(
            any('Claude' in m for m in ping_msgs),
            f"expected a Claude-probe ping, got: {ping_msgs!r}",
        )
        # Hold-and-wait: startup no longer flips to a Google-only day — it
        # just warns; job() will attempt the LLM each slot and hold instead.
        mock_set_fallback.assert_not_called()

    @patch.dict(os.environ, {'TZ': 'Europe/Moscow'})
    @patch('news_bot.outage_state.set_fallback_active')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.claude_transcreation.health_check', return_value=True)
    def test_main_no_admin_ping_when_health_check_returns_true(
        self, _mock_health, mock_admin, mock_set_fallback,
    ):
        self._run_main_once()
        # No Claude-probe ping; no fallback-active flip.
        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        self.assertFalse(
            any('Claude probe failed' in m for m in ping_msgs),
            f"unexpected Claude-probe ping: {ping_msgs!r}",
        )
        # set_fallback_active(True) must NOT be called when healthy.
        for c in mock_set_fallback.call_args_list:
            if c.args:
                self.assertNotEqual(c.args[0], True,
                                    "set_fallback_active(True) on healthy probe")

    @patch.dict(os.environ, {'TZ': 'UTC'})
    @patch('news_bot.outage_state.set_fallback_active')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.claude_transcreation.health_check', return_value=True)
    def test_main_warns_on_tz_mismatch(
        self, _mock_health, mock_admin, _mock_set_fallback,
    ):
        self._run_main_once()
        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        self.assertTrue(
            any('TZ' in m or 'timezone' in m.lower() for m in ping_msgs),
            f"expected a TZ-warning ping, got: {ping_msgs!r}",
        )

    @patch.dict(os.environ, {'TZ': 'Europe/Moscow'})
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.claude_transcreation.health_check', return_value=True)
    @patch('news_bot._maybe_start_review_listener')
    def test_main_wires_review_listener(
        self, mock_listener, _mock_health, _mock_admin,
    ):
        """Audit M-2: pin the feature's ONLY production activation path.

        The review-listener gate function is thoroughly tested at its own
        seam, but nothing pinned the ``_maybe_start_review_listener()``
        call inside ``main()`` — deleting that line left the whole suite
        green (mutation-verified in the test audit) while shipping a fully
        dead feature (buttons render flag-on, nothing ever serves them).
        This spy makes that mutation fail.
        """
        self._run_main_once()
        mock_listener.assert_called_once_with()


# ---------------------------------------------------------------------------
# Distributed-publish loop tests (Decisions 15 + tech-spec §How-it-works step 7)
# ---------------------------------------------------------------------------

class _DistribLoopBase(_JobBase):
    """Adds time-frozen ``datetime.now(MSK)`` patching so ``compute_fixed_slots``
    is exercised against a deterministic ``now``."""

    # 10:00 МСК — the morning fixed slot. At this tick all three fixed slots
    # (10:00/15:00/19:30) are eligible, so a 3-article queue yields 3 publishes
    # (compute_fixed_slots, operator pacing 2026-06-13).
    FROZEN_NOW = MSK.localize(dt.datetime(2026, 4, 27, 10, 0, 0))

    def setUp(self):
        super().setUp()
        # Freeze ``datetime.now(msk_tz)`` inside news_bot. We patch the
        # whole ``datetime`` module attribute on news_bot so every call
        # via ``datetime.now(...)`` returns the frozen MSK time.
        # The simplest approach: patch news_bot._now_msk if introduced;
        # otherwise rely on patching ``news_bot.datetime``.
        self._dt_patcher = patch('news_bot.datetime')
        mock_datetime = self._dt_patcher.start()
        # ``datetime.now(tz)`` → frozen value.
        mock_datetime.now.return_value = self.FROZEN_NOW
        # Ensure other constructors still work via the real datetime.
        mock_datetime.utcnow.side_effect = lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        # Pass through real classes for ``isinstance`` / arithmetic.
        mock_datetime.timedelta = dt.timedelta
        mock_datetime.timezone = dt.timezone
        mock_datetime.datetime = dt.datetime
        # Allow ``datetime.combine``, ``datetime(...)`` etc. used elsewhere
        # in news_bot to fall through to the real implementation if needed.
        mock_datetime.combine = dt.datetime.combine

    def tearDown(self):
        self._dt_patcher.stop()
        super().tearDown()


class TestDistributedPublishLoop(_DistribLoopBase):
    """End-to-end-ish tests that fetch is mocked, claude is mocked,
    ``_fallback_publish`` is mocked, and we assert slot-by-slot behaviour."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_publishes_three_articles_at_expected_slots(
        self, mock_fetch_article, mock_publish, mock_sleep, _mock_admin,
    ):
        # 3 incoming RSS entries, 3 article payloads.
        mock_fetch_article.side_effect = lambda e: self._article_payload(
            title=e.get('title') or '',
        )

        with _patch_sources_returning([
            self._entry('http://example.com/a1', 'T1'),
            self._entry('http://example.com/a2', 'T2'),
            self._entry('http://example.com/a3', 'T3'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        # Exactly 3 _fallback_publish calls (one per slot).
        self.assertEqual(mock_publish.call_count, 3)
        # ``_fallback_publish`` was called with via_review=False.
        for c in mock_publish.call_args_list:
            self.assertEqual(c.kwargs.get('via_review'), False)

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_outage_active_routes_via_google(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
    ):
        """When ``outage_state.is_fallback_active() == True`` the loop still
        routes through ``_fallback_publish``; ``_fallback_publish`` itself
        short-circuits to Google internally on the active-fallback flag.
        Test verifies that the publish loop fires for the slot even with the
        flag set (TR-2)."""
        mock_fetch_article.side_effect = lambda e: self._article_payload()

        with _patch_sources_returning([
            self._entry('http://example.com/g1', 'T1'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=True):
                news_bot.job()

        # Slot fired exactly once and through the unified entry-point.
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(
            mock_publish.call_args.kwargs.get('via_review'), False,
        )

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_first_strike_increments_attempt(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
    ):
        """One unexpected exception on a single slot bumps attempt_count
        to 1 and writes the (sanitised) error to last_error. Full
        end-to-end 3-strikes coverage lives in ``test_three_strikes_*``
        below — this test isolates the first-strike side-effect."""
        mock_fetch_article.side_effect = lambda e: self._article_payload()
        mock_publish.side_effect = RuntimeError('boom')

        with _patch_sources_returning([
            self._entry('http://example.com/x1', 'T1'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        rows = pending_articles_repo.list_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempt_count'], 1)
        self.assertIn('boom', (rows[0].get('last_error') or ''))

    @patch('news_bot.pending_repo.increment_attempt')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_outage_holds_row_without_strike_or_publish(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
        mock_increment,
    ):
        """Hold-and-wait slot-loop invariant: a ``ClaudeOutageError`` from
        ``_fallback_publish`` must NOT strike the row (no increment_attempt),
        must NOT count a publish, and must leave the row in pending with
        attempt_count still 0 — so it carries over and retries the LLM.
        This is the symmetric counterpart to
        ``test_first_strike_increments_attempt`` for the outage branch."""
        mock_fetch_article.side_effect = lambda e: self._article_payload()
        mock_publish.side_effect = ClaudeOutageError('429 overloaded')

        with _patch_sources_returning([
            self._entry('http://example.com/hold', 'T1'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        # Outage path is NOT a strike — the article waits, untouched.
        mock_increment.assert_not_called()
        rows = pending_articles_repo.list_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempt_count'], 0)
        # Nothing was published.
        self.assertIsNone(
            pending_articles_repo.get_published('http://example.com/hold')
        )

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_three_strikes_moves_to_failed(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
    ):
        """Three consecutive unexpected exceptions for the SAME row across
        three job() runs end with the row in ``failed_articles`` and
        nothing in ``pending_articles``. Defends the 3-strikes contract
        end-to-end (TR-1)."""
        # Pre-seed one row directly so the fetch phase does not duplicate it.
        pending_articles_repo.insert_pending({
            'link': 'http://example.com/x1',
            'source_name': 'autoevolution',
            'title': 'T1',
            'paragraphs': ['p'],
            'images': [],
        })
        mock_fetch_article.side_effect = lambda e: self._article_payload()
        mock_publish.side_effect = RuntimeError('boom')

        # Run job() three times — each run picks up the SAME oldest row
        # and bumps the attempt counter. After the third run move_to_failed
        # fires.
        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()
                news_bot.job()
                news_bot.job()

        # Row no longer pending; row IS in failed.
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        failed = pending_articles_repo.get_failed('http://example.com/x1')
        self.assertIsNotNone(failed)
        self.assertIn('boom', (failed.get('last_error') or ''))

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    def test_admin_ping_suppressed_on_zero_pending(
        self, _mock_publish, _mock_sleep, mock_admin,
    ):
        # No entries, no insert; admin should NOT receive a "plan-of-day" ping.
        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        # No multi-line plan-of-day ping when N=0 — only the quiet-day
        # «🟢 Бот сработал, новых статей нет.» single-line ping should fire.
        self.assertFalse(
            any(('План на сегодня' in m) or ('Принято свежих' in m) or
                ('schedule' in m.lower()) or ('queued' in m.lower())
                for m in ping_msgs),
            f"unexpected plan-of-day ping on N=0: {ping_msgs!r}",
        )

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_backlog_warning_at_threshold(
        self, mock_fetch_article, _mock_publish, _mock_sleep, mock_admin,
    ):
        # Pre-seed 51 pending rows (above BACKLOG_WARNING_THRESHOLD=50).
        # Use direct insert_pending to skip fetch_full_article overhead.
        for i in range(51):
            pending_articles_repo.insert_pending({
                'link': f'http://example.com/p{i}',
                'source_name': 'autoevolution',
                'title': f'T{i}',
                'paragraphs': ['x'],
                'images': [],
            })
        mock_fetch_article.side_effect = lambda e: self._article_payload()

        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        ping_msgs = [c.args[0] for c in mock_admin.call_args_list if c.args]
        # Expect at least one ping mentioning the backlog (>50) state.
        self.assertTrue(
            any(('backlog' in m.lower()) or ('очеред' in m.lower())
                or ('queue' in m.lower())
                for m in ping_msgs),
            f"expected backlog warning ping, got: {ping_msgs!r}",
        )


class TestWindowEndGuard(_DistribLoopBase):
    """Decision 15: if a slot is past ``window_end`` (e.g. previous publish
    overran), the loop must ``break``."""

    # We need a frozen "now" inside the window to seed slots near 19:30,
    # then synthesise a degenerate case where compute_fixed_slots returns
    # a slot beyond 20:00. Easiest: monkey-patch compute_fixed_slots to
    # return a synthetic slot list including one outside the window, and
    # assert _fallback_publish is NOT called for the over-window slot.

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.compute_fixed_slots')
    def test_window_end_guard_breaks_loop(
        self, mock_compute, mock_fetch_article, mock_publish, _mock_sleep,
        _mock_admin,
    ):
        # Two slots inside, one slot past window_end. compute_fixed_slots
        # would never produce that organically, but the loop's window-end
        # guard insurance must catch it.
        slot_a = MSK.localize(dt.datetime(2026, 4, 27, 13, 0))
        slot_b = MSK.localize(dt.datetime(2026, 4, 27, 14, 0))
        slot_c = MSK.localize(dt.datetime(2026, 4, 27, 21, 0))  # past end
        mock_compute.return_value = ([slot_a, slot_b, slot_c], 0)

        mock_fetch_article.side_effect = lambda e: self._article_payload()

        with _patch_sources_returning([
            self._entry('http://example.com/w1', 'T1'),
            self._entry('http://example.com/w2', 'T2'),
            self._entry('http://example.com/w3', 'T3'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        # Only the two in-window slots produce publish calls.
        self.assertEqual(mock_publish.call_count, 2,
                         f"expected 2 publishes (slot 3 past window), got "
                         f"{mock_publish.call_count}")


# ---------------------------------------------------------------------------
# In-slot publish retry (operator decision 2026-06-17)
# ---------------------------------------------------------------------------

class TestPublishWithRetries(unittest.TestCase):
    """``_publish_with_retries`` retries a transient publish failure within
    the same slot (spaced ``PUBLISH_RETRY_DELAY_SECONDS`` apart) so a one-off
    network blip (e.g. Telegra.ph read timeout) does not cost the article its
    slot until the next day. Returns ('published'|'held'|'failed', last_err).
    """

    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    def test_transient_failure_then_success(self, mock_pub, mock_sleep):
        # Fail once (network timeout), then succeed on the retry.
        mock_pub.side_effect = [RuntimeError('api.telegra.ph read timeout'), None]
        outcome, err = news_bot._publish_with_retries({'link': 'x'}, 2, 2)
        self.assertEqual(outcome, 'published')
        self.assertIsNone(err)
        self.assertEqual(mock_pub.call_count, 2)
        # Exactly one retry-delay sleep happened before the successful retry.
        mock_sleep.assert_called_once_with(news_bot.PUBLISH_RETRY_DELAY_SECONDS)

    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    def test_all_attempts_fail_returns_failed_with_last_error(self, mock_pub, mock_sleep):
        boom = RuntimeError('still timing out')
        mock_pub.side_effect = boom
        outcome, err = news_bot._publish_with_retries({'link': 'x'}, 1, 1)
        self.assertEqual(outcome, 'failed')
        self.assertIs(err, boom)
        # 1 initial attempt + PUBLISH_RETRY_ATTEMPTS retries.
        self.assertEqual(mock_pub.call_count, news_bot.PUBLISH_RETRY_ATTEMPTS + 1)
        self.assertEqual(mock_sleep.call_count, news_bot.PUBLISH_RETRY_ATTEMPTS)

    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    def test_outage_returns_held_without_retry(self, mock_pub, mock_sleep):
        # An LLM outage is NOT a transient publish failure — hold immediately,
        # never retry (retrying would re-translate and waste tokens).
        mock_pub.side_effect = ClaudeOutageError('429 overloaded')
        outcome, err = news_bot._publish_with_retries({'link': 'x'}, 1, 1)
        self.assertEqual(outcome, 'held')
        mock_pub.assert_called_once()
        mock_sleep.assert_not_called()

    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    def test_per_article_failure_not_retried(self, mock_pub, mock_sleep):
        # A per-article LLM problem is deterministic — strike immediately,
        # never retry (would re-translate to the same bad result).
        boom = ClaudeTranscreationError('malformed JSON')
        mock_pub.side_effect = boom
        outcome, err = news_bot._publish_with_retries({'link': 'x'}, 1, 1)
        self.assertEqual(outcome, 'failed')
        self.assertIs(err, boom)
        mock_pub.assert_called_once()
        mock_sleep.assert_not_called()


class TestSlotLoopTransientRetry(_DistribLoopBase):
    """job()-level: a transient publish failure that recovers on retry must
    NOT strike the row (no increment_attempt) and must count as published.

    Inherits ``_DistribLoopBase`` (not ``_JobBase``) so ``datetime.now(MSK)``
    is frozen at 10:00 МСК — without it the slot loop's 20:00 window-end
    guard exits before publishing when the suite runs in the evening, making
    these tests time-of-day flaky."""

    @patch('news_bot.pending_repo.increment_attempt')
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_transient_failure_recovers_in_slot_no_strike(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
        mock_increment,
    ):
        mock_fetch_article.side_effect = lambda e: self._article_payload()
        # First publish attempt times out; the in-slot retry succeeds.
        mock_publish.side_effect = [RuntimeError('telegra.ph timeout'), None]

        with _patch_sources_returning([
            self._entry('http://example.com/retry', 'T1'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        # Recovered on retry → no strike.
        mock_increment.assert_not_called()
        self.assertEqual(mock_publish.call_count, 2)

    @patch('news_bot.pending_repo.increment_attempt', return_value=1)
    @patch('news_bot.send_admin_notification')
    @patch('news_bot.time.sleep')
    @patch('news_bot._fallback_publish')
    @patch('news_bot.fetch_full_article')
    def test_exhausted_retries_strikes_once_not_per_retry(
        self, mock_fetch_article, mock_publish, _mock_sleep, _mock_admin,
        mock_increment,
    ):
        """A transient failure that never recovers must strike the row
        EXACTLY ONCE for the slot (not once per in-slot retry)."""
        mock_fetch_article.side_effect = lambda e: self._article_payload()
        mock_publish.side_effect = RuntimeError('telegra.ph timeout')

        with _patch_sources_returning([
            self._entry('http://example.com/dead', 'T1'),
        ]):
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        # 1 initial + PUBLISH_RETRY_ATTEMPTS retries, but only ONE strike.
        self.assertEqual(
            mock_publish.call_count, news_bot.PUBLISH_RETRY_ATTEMPTS + 1)
        mock_increment.assert_called_once()


# ---------------------------------------------------------------------------
# Channel-silence alert (2026-06-23)
# ---------------------------------------------------------------------------

class TestDrySpellAlert(_JobBase):
    """job() sends an [E017] admin warning when nothing has been published
    for DRY_SPELL_ALERT_DAYS+ days — a louder signal than the daily [E009]
    'no news', to catch a prolonged dry spell (over-strict filter, dead
    source, server-network trouble)."""

    def _admin_msgs(self, mock_admin):
        return [c.args[0] for c in mock_admin.call_args_list if c.args]

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_pings_when_channel_silent_for_days(self, mock_admin, _mock_sleep):
        old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
               - dt.timedelta(days=5))
        self._seed_published('https://example.com/old', old.strftime('%Y-%m-%d %H:%M:%S'))

        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        msgs = self._admin_msgs(mock_admin)
        self.assertTrue(any('[E017]' in m for m in msgs),
                        f"expected an [E017] channel-silent ping, got {msgs!r}")

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_no_ping_when_recent_publish(self, mock_admin, _mock_sleep):
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        self._seed_published('https://example.com/fresh', now.strftime('%Y-%m-%d %H:%M:%S'))

        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        msgs = self._admin_msgs(mock_admin)
        self.assertFalse(any('[E017]' in m for m in msgs),
                         f"unexpected [E017] ping with a recent publish: {msgs!r}")

    @patch('news_bot.time.sleep')
    @patch('news_bot.send_admin_notification')
    def test_no_ping_when_never_published(self, mock_admin, _mock_sleep):
        # Fresh DB — published_articles empty → no false alarm.
        with _patch_sources_empty():
            with patch('news_bot.outage_state.is_fallback_active',
                       return_value=False):
                news_bot.job()

        msgs = self._admin_msgs(mock_admin)
        self.assertFalse(any('[E017]' in m for m in msgs),
                         f"unexpected [E017] ping on a never-published bot: {msgs!r}")


if __name__ == '__main__':
    unittest.main()
