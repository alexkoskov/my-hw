#!/usr/bin/env python3
"""End-to-end integration tests for the distributed-publish schedule.

Task 12 of ``llm-transcreation-and-distributed-publishing``. After Wave 1-6
introduced the new modules (``compute_publish_slots``, ``claude_transcreation``,
``outage_state``) and rewrote ``_fallback_publish`` + ``job()``, these tests
exercise the full cron-tick → schedule → distributed-publish flow with all
external dependencies mocked.

Coverage (user-spec ACs):
  AC1     — daily cron at 12:00 МСК (job() runs as a single tick).
  AC2     — admin plan-of-day ping with the day's schedule.
  AC3-6   — 13:00-20:00 publication window, 40-min minimum interval,
            cap of 11 publishes/day, even distribution.
  AC7     — container restart mid-window recomputes slots from `now`.
  AC8     — crash-loop guard waits the remainder of MIN_INTERVAL_MINUTES.
  AC14    — API-level outage advances the outage state machine + ping #1.
  AC15    — per-article failure routes ONE article via Google,
            no state-machine advance.
  AC17    — bot keeps fallback_active until next 12:00 cron tick (covered
            implicitly: the post-outage slot still runs via Claude when
            ``is_fallback_active() == False`` because Decision-5 ping_1_sent
            does NOT yet flip the global flag — the flag flips only at
            ping #3 / google_fallback_active).
  AC21    — manual-review preemption: operator-published row is skipped,
            bot pulls the next pending row at the next slot.

Each scenario is one `unittest.TestCase` method. All Claude SDK calls are
mocked via the ``news_bot.transcreate_via_claude`` pinned bound name
(per Task 11 patch-target convention). All Google-translate calls are
mocked via ``news_bot.transcreate_text`` identity-stub. Telegraph and
Telegram are mocked too (no real network).

``freezegun`` controls ``datetime.now(...)`` for the schedule-recompute
scenario. ``time.sleep`` is patched to a no-op so slot-by-slot waits
don't block real wall-clock time.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make project root importable when pytest runs from elsewhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz
from freezegun import freeze_time

import news_bot
import outage_state
import pending_articles_repo
from claude_transcreation import ClaudeOutageError, ClaudeTranscreationError
from compute_publish_slots import compute_publish_slots


MSK = pytz.timezone("Europe/Moscow")


# --------------------------------------------------------------------------- #
# Mock helpers — two distinct shapes per Task 12 spec.                        #
# --------------------------------------------------------------------------- #


def _create_mock_rss_entry(link, title="Test Article",
                           published="2025-01-01", source_name="autoevolution"):
    """RSS-entry shape — what ``fetch_rss`` / ``fetch_mattel_news`` return.

    Carries only the fields that the job()-level fetch+filter+stage path
    actually reads (link, title, published, summary, feed_url, source_name).
    """
    return {
        "link": link,
        "title": title,
        "published": published,
        "summary": "Some summary",
        "feed_url": "http://example.com/feed.xml",
        "source_name": source_name,
    }


def _create_mock_full_article(link, title="Test Article",
                              source_name="autoevolution"):
    """Full-article shape — what ``fetch_full_article`` returns and what
    ``insert_pending`` consumes (and what ``_fallback_publish`` later sees
    on the row). Distinct from the RSS-entry shape: includes title /
    subtitle / paragraphs / images / source_name / pub_date.
    """
    return {
        "link": link,
        "title": title,
        "subtitle": "Editorial lead",
        "paragraphs": ["First paragraph.", "Second paragraph."],
        "images": [],
        "source_name": source_name,
        "pub_date": "2026-04-27",
        "blocks": None,
    }


def _make_claude_result(en_paragraphs, title_prefix="Тестовая статья"):
    """Build a valid Claude response dict matching the contract enforced
    by ``claude_transcreation._parse_response``. Used as ``return_value``
    or ``side_effect`` element for the ``transcreate_via_claude`` mock.

    ``paragraphs`` length must match the EN paragraphs exactly — the
    real wrapper validates this; tests should not exercise the parser
    here (covered separately in test_claude_transcreation.py).
    """
    return {
        "title": f"🔥 {title_prefix}",
        "alts": [f"alt {title_prefix} 1", f"alt {title_prefix} 2"],
        "subtitle": "Подзаголовок",
        "paragraphs": [f"RU: {p}" for p in en_paragraphs],
        "blocks": None,
    }


# --------------------------------------------------------------------------- #
# Base TestCase — tempfile DB + the always-on mock stack.                     #
# --------------------------------------------------------------------------- #


class TestDistributedSchedule(unittest.TestCase):
    """Each test owns a fresh tempfile SQLite DB. Schema initialised via
    ``news_bot.init_db()`` (which creates ``processed_news`` AND delegates
    the manual-review-workflow + ``bot_state`` tables to
    ``pending_articles_repo.init_schema``).

    The base setUp brings up the patches that EVERY test needs:
      - DB_FILE → tempfile path
      - Telegram tokens → mock strings (so admin notify path is exercised
        but never hits the real bot)
      - send_admin_notification → MagicMock (per-test introspection)
      - time.sleep → MagicMock (no-op; per-test arg capture if needed)
      - load_feeds, fetch_rss, fetch_mattel_news → mock RSS pipelines
      - fetch_full_article → mock full-article dispatch
      - send_telegraph_teaser → True
      - telegraph_publisher.publish_article → fake URL
      - transcreate_text → identity stub (Google fallback verifier)

    Per-test mocks for ``transcreate_via_claude`` are added inside each
    test method (different `side_effect` per scenario).
    """

    def setUp(self):
        # Tempfile DB.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()  # creates processed_news + pending/published/failed/bot_state.

        # Telegram credential stubs.
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()

        # Capture admin pings for content-fragment assertions.
        self.notify_patcher = patch('news_bot.send_admin_notification')
        self.mock_notify = self.notify_patcher.start()

        # No real wall-clock sleeps. Args captured for between-slot timing
        # ranges — the truth source for slot timing is compute_publish_slots
        # itself, not hard-coded seconds in the test.
        self.sleep_patcher = patch('news_bot.time.sleep')
        self.mock_sleep = self.sleep_patcher.start()

        # Telegraph + teaser mocks — every publish must go through these.
        self.teaser_patcher = patch('news_bot.send_telegraph_teaser',
                                    return_value=True)
        self.mock_teaser = self.teaser_patcher.start()
        self.publish_article_patcher = patch(
            'news_bot.telegraph_publisher.publish_article',
            return_value='https://telegra.ph/fake-page-04-27',
        )
        self.mock_publish_article = self.publish_article_patcher.start()

        # Google stub — returns the input wrapped in enough Cyrillic
        # context to clear the 30% threshold of
        # ``_llm_translation_is_russian`` (which would reject pure-EN
        # output as "blocked translate call returning source verbatim").
        # The marker prefix survives in the published row so tests can
        # still prove the Google path was used.
        self.transcreate_text_patcher = patch(
            'news_bot.transcreate_text',
            side_effect=lambda t, **k: f"Это русский перевод: {t}",
        )
        self.mock_transcreate_text = self.transcreate_text_patcher.start()

        # Sources scaffolding. Most tests override fetch_rss / fetch_mattel
        # via _patch_sources_returning(); but the load_feeds / mattel
        # patchers must be present BEFORE the first job() call because
        # SOURCES iterate immediately.
        self.load_feeds_patcher = patch(
            'news_bot.load_feeds',
            return_value=['http://example.com/feed.xml'],
        )
        self.mock_load_feeds = self.load_feeds_patcher.start()
        self.fetch_rss_patcher = patch('news_bot.fetch_rss', return_value=[])
        self.mock_fetch_rss = self.fetch_rss_patcher.start()
        self.fetch_mattel_patcher = patch(
            'news_bot.fetch_mattel_news', return_value=[],
        )
        self.mock_fetch_mattel = self.fetch_mattel_patcher.start()
        # Narrow SOURCES — patching the function attribute alone doesn't
        # work because SOURCES holds the original function reference.
        import news_bot as _nb
        self.sources_patcher = patch(
            'news_bot.SOURCES',
            [_nb._fetch_rss_entries, _nb._fetch_mattel_entries],
        )
        self.sources_patcher.start()
        self.fetch_full_patcher = patch(
            'news_bot.fetch_full_article',
            side_effect=lambda e: _create_mock_full_article(
                e.get('link'),
                title=e.get('title') or 'Title',
                source_name=e.get('source_name') or 'autoevolution',
            ),
        )
        self.mock_fetch_full = self.fetch_full_patcher.start()

    def tearDown(self):
        # Stop in reverse order to avoid surprises.
        self.fetch_full_patcher.stop()
        self.sources_patcher.stop()
        self.fetch_mattel_patcher.stop()
        self.fetch_rss_patcher.stop()
        self.load_feeds_patcher.stop()
        self.transcreate_text_patcher.stop()
        self.publish_article_patcher.stop()
        self.teaser_patcher.stop()
        self.sleep_patcher.stop()
        self.notify_patcher.stop()
        self.admin_patcher.stop()
        self.channel_patcher.stop()
        self.token_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # ----- helpers ---------------------------------------------------------

    def _set_rss_entries(self, entries):
        """Make `fetch_rss` return ``entries`` for the single feed configured
        in setUp. Resets side_effect cleanly between tests."""
        self.mock_fetch_rss.side_effect = None
        self.mock_fetch_rss.return_value = list(entries)

    def _published_links(self):
        """Return the set of links currently in published_articles (DB-level
        introspection, bypasses the repo cache)."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT link FROM published_articles ORDER BY published_at"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def _pending_links(self):
        """Return the set of links currently in pending_articles."""
        return [r['link'] for r in pending_articles_repo.list_pending()]

    def _admin_messages(self):
        """All single-string admin pings captured by mock_notify."""
        return [c.args[0] for c in self.mock_notify.call_args_list if c.args]

    # ======================================================================
    # Scenario 1: full happy path — 3 articles, 3 slots, 3 publishes.
    # ======================================================================

    def test_full_happy_path_three_articles_three_slots_three_publishes(self):
        """N=3 at 12:00 МСК. compute_publish_slots → 3 slots in 13:00-20:00
        window. Each slot publishes via Claude, no outage state recorded,
        plan-of-day admin ping fired at the start.

        Asserts:
          * exactly 3 ``transcreate_via_claude`` calls,
          * exactly 3 rows in ``published_articles``,
          * 0 rows in ``pending_articles`` after the loop,
          * outage_state shows no_outage,
          * ``time.sleep`` between-slot waits sum to the inter-slot intervals
            from compute_publish_slots (sanity, not byte-equality),
          * one admin ping content-matches the plan-of-day fragment.
        """
        # 3 RSS entries → 3 full articles → 3 pending rows → 3 slots.
        entries = [
            _create_mock_rss_entry('http://example.com/a1', title='Title 1'),
            _create_mock_rss_entry('http://example.com/a2', title='Title 2'),
            _create_mock_rss_entry('http://example.com/a3', title='Title 3'),
        ]
        self._set_rss_entries(entries)

        with freeze_time('2026-04-27 09:00:00'):  # 09:00 UTC == 12:00 МСК
            with patch('news_bot.transcreate_via_claude') as mock_claude:
                mock_claude.side_effect = [
                    _make_claude_result(['First paragraph.', 'Second paragraph.'],
                                        title_prefix=f'Article {i}')
                    for i in range(1, 4)
                ]
                news_bot.job()

        # Three Claude calls — one per slot, in pending-order.
        self.assertEqual(mock_claude.call_count, 3,
                         f"expected 3 Claude calls, got {mock_claude.call_count}")

        # Three published rows; pending is empty.
        published = self._published_links()
        self.assertEqual(len(published), 3,
                         f"expected 3 published rows, got {published}")
        self.assertEqual(self._pending_links(), [],
                         "pending_articles must be empty after happy path")

        # Each row carries telegraph_url from our mock.
        for link in published:
            row = pending_articles_repo.get_published(link)
            self.assertIsNotNone(row)
            self.assertEqual(row['telegraph_url'],
                             'https://telegra.ph/fake-page-04-27')

        # No outage state recorded — happy path leaves bot_state untouched.
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertFalse(outage_state.is_fallback_active())

        # Plan-of-day admin ping was fired with the schedule fragment.
        msgs = self._admin_messages()
        self.assertTrue(
            any('План на сегодня' in m or 'Принято свежих' in m for m in msgs),
            f"expected plan-of-day ping, got: {msgs!r}",
        )

        # Teaser dispatch ran once per successful publish (T7 — pin
        # the channel-side handoff so a regression that bypasses Telegram
        # but still writes to ``published_articles`` is caught).
        self.assertEqual(self.mock_teaser.call_count, 3,
                         f"expected 3 teaser dispatches, got "
                         f"{self.mock_teaser.call_count}")

        # Slot timing — verify sleep args match compute_publish_slots output
        # for the same inputs (truth source, NOT hard-coded). Under freezegun
        # `now` is FROZEN, so every `wait_seconds = max(0, slot - now)` in the
        # publish loop is computed from the SAME starting `now` (12:00 МСК).
        # Therefore the sequence of `time.sleep` waits equals the offsets of
        # each slot from the cron-tick now, not the inter-slot deltas a real
        # wall clock would see.
        now_msk = MSK.localize(dt.datetime(2026, 4, 27, 12, 0, 0))
        # Pass the same window kwargs news_bot.job() uses, otherwise the
        # function's default 13:00 start would drift the expected slots
        # away from the loop's actual 10:00-window slots.
        slots, _carry = compute_publish_slots(
            3, now_msk,
            window_start=news_bot.WINDOW_START_TIME,
            window_end=news_bot.WINDOW_END_TIME,
        )
        expected_offsets = [
            (s - now_msk).total_seconds() for s in slots
        ]
        # Filter out the constant-3s Telegra.ph cache-warmup sleeps so
        # they don't pollute the slot-wait count (one warmup per freshly-
        # published article).
        sleep_args = [c.args[0] for c in self.mock_sleep.call_args_list
                      if c.args and isinstance(c.args[0], (int, float))
                      and c.args[0] != news_bot.TELEGRAPH_CACHE_WARMUP_SECONDS]
        # The publish-loop emits ONE sleep per in-window slot whose
        # offset > 0. With WINDOW_START=10:00 and now=12:00, the first
        # slot lands at 12:00 itself (offset 0) and is published without
        # a sleep call — so only the 2 later slots produce sleeps.
        positive_expected = [o for o in expected_offsets if o > 0]
        self.assertEqual(len(sleep_args), len(positive_expected),
                         f"expected {len(positive_expected)} slot-wait sleeps, "
                         f"got {sleep_args!r}")
        for actual, expected in zip(sleep_args, positive_expected):
            self.assertAlmostEqual(actual, expected, delta=2,
                                   msg=f"slot-wait mismatch: actual={actual} "
                                       f"expected={expected}")

    # ======================================================================
    # Scenario 2: API-level outage on slot 2 → ping #1 + Google for that
    # one article. Slot 3 retries Claude (no exception this time) →
    # state machine does NOT auto-clear under current Wave 1-6 wiring,
    # so ping_count remains '1' and outage_started_at persists. Article
    # is still published successfully via Claude on slot 3.
    # Note: ``record_recovery_event`` is defined in outage_state.py but
    # not yet called from _fallback_publish post-success — the test
    # documents the actual current state and is DELIBERATELY relaxed on
    # the recovery side to match production behaviour.
    # ======================================================================

    def test_outage_mid_day_advances_state_and_recovers_on_next_slot(self):
        """Slot 2 throws ClaudeOutageError → ping #1 fires, article goes via
        Google for THIS slot (degraded mode), outage state advances
        (ping_count='1', outage_started_at set). Slot 3 succeeds via Claude
        and ``_maybe_record_recovery`` (P1 fix C1+C4, 2026-04-30) auto-
        clears the outage state and sends the switch-back ping.

        Verifies AC14 (API-level outage → ping #1 + degraded-mode publish)
        and AC15 (state advance is API-level only) plus AC for recovery
        on the next successful Claude call.
        """
        entries = [
            _create_mock_rss_entry('http://example.com/o1', title='Outage 1'),
            _create_mock_rss_entry('http://example.com/o2', title='Outage 2'),
            _create_mock_rss_entry('http://example.com/o3', title='Outage 3'),
        ]
        self._set_rss_entries(entries)

        outage_exc = ClaudeOutageError("simulated rate-limit (RateLimitError-equiv)")

        with freeze_time('2026-04-27 09:00:00'):
            with patch('news_bot.transcreate_via_claude') as mock_claude:
                mock_claude.side_effect = [
                    _make_claude_result(['First paragraph.', 'Second paragraph.'],
                                        title_prefix='Slot1'),
                    outage_exc,
                    _make_claude_result(['First paragraph.', 'Second paragraph.'],
                                        title_prefix='Slot3'),
                ]
                news_bot.job()

        self.assertEqual(mock_claude.call_count, 3)

        published = self._published_links()
        self.assertEqual(len(published), 3,
                         f"expected 3 published rows (all slots succeeded), "
                         f"got {published}")
        self.assertEqual(self._pending_links(), [])

        # Slot 3's Claude success triggered _maybe_record_recovery which
        # cleared the state. So at end-of-job, ping_count is back to 0
        # and outage_started_at is unset.
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertFalse(outage_state.is_fallback_active())

        # Admin received BOTH the slot-2 outage warning AND the slot-3
        # recovery ping.
        msgs = self._admin_messages()
        self.assertTrue(
            any('Claude API недоступна' in m for m in msgs),
            f"expected ping #1 outage warning, got: {msgs!r}",
        )
        self.assertTrue(
            any(('Claude' in m and ('восстанов' in m.lower() or
                                    'recovered' in m.lower()))
                for m in msgs),
            f"expected recovery switch-back ping, got: {msgs!r}",
        )

        # Slot 2's article went through Google — transcreate_text was
        # called for the slot-2 row's title/subtitle/paragraphs.
        # (Slots 1 and 3 should NOT use transcreate_text — they go via
        # Claude. So the call count should be > 0 but bounded.)
        self.assertGreater(self.mock_transcreate_text.call_count, 0,
                           "Google fallback path must invoke transcreate_text")

    # ======================================================================
    # Scenario 3: container restart mid-window. 5 pending rows, restart
    # at 16:00 inside the publish window → recompute slots from `now`.
    # ======================================================================

    @patch('news_bot.MIN_INTERVAL_MINUTES', 40)
    @patch('news_bot.compute_publish_slots')
    def test_container_restart_mid_window_recomputes_slots_and_continues(
        self, mock_compute,
    ):
        """Pre-seed 5 pending rows + 1 already-published row (simulated
        prior-tick result). Freeze time at 16:00 МСК (mid-window).
        Call ``job()`` directly. Expectation:
          * fetch+stage phase finds nothing new (sources empty).
          * compute_publish_slots(5, 16:00 MSK) → 5 slots at 48-min interval.
          * 5 publishes happen, all currently-pending rows clear.
          * The pre-existing published row is NOT re-published
            (idempotency via pending vs published separation).
          * Crash-loop guard: simulate the most recent publish was 10 min
            before now, which is < MIN_INTERVAL_MINUTES (40). Bot must
            sleep (40 - 10) = 30 min before continuing.

        ``MIN_INTERVAL_MINUTES`` and ``compute_publish_slots`` are pinned
        to the historical 40-min floor for this test so the algorithm
        scenario stays valid after the production default moved to 90.
        ``compute_publish_slots`` is called by the loop with the module
        default kwarg, so we patch it to inject a 40-min mock.
        """
        from compute_publish_slots import compute_publish_slots as real_compute
        mock_compute.side_effect = lambda n, now, **kw: real_compute(
            n, now,
            window_start=kw.get('window_start'),
            window_end=kw.get('window_end'),
            min_interval_min=40,
        )
        # Pre-seed 5 pending rows directly so we don't depend on the fetch
        # step recomputing them.
        for i in range(5):
            ok = pending_articles_repo.insert_pending({
                'link': f'http://example.com/r{i}',
                'source_name': 'autoevolution',
                'feed_url': 'http://example.com/feed.xml',
                'title': f'Title {i}',
                'subtitle': '',
                'paragraphs': ['p1.', 'p2.'],
                'images': [],
                'blocks': None,
                'pub_date': '2026-04-27',
            })
            self.assertTrue(ok)

        # Pre-seed one already-published row — must NOT be re-published.
        # ALSO seeds the crash-loop guard input (UTC ts 10 min before now).
        # 16:00 MSK = 13:00 UTC; 10 min ago = 12:50 UTC.
        already_pub_link = 'http://example.com/already-pub'
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (already_pub_link, 'Already', 'Уже', 'https://telegra.ph/old',
                 'autoevolution', 0, '2026-04-27 12:50:00'),
            )
            conn.commit()
        finally:
            conn.close()

        # Sources empty for this scenario — the fetch phase is incidental.
        self._set_rss_entries([])

        # Freeze time at 16:00 МСК = 13:00 UTC.
        with freeze_time('2026-04-27 13:00:00'):
            with patch('news_bot.transcreate_via_claude') as mock_claude:
                mock_claude.side_effect = [
                    _make_claude_result(['p1.', 'p2.'],
                                        title_prefix=f'Restart {i}')
                    for i in range(5)
                ]
                news_bot.job()

        # All 5 pre-seeded rows published; pending is empty.
        self.assertEqual(self._pending_links(), [])
        published = self._published_links()
        # 5 new + 1 pre-existing = 6 total.
        self.assertEqual(len(published), 6,
                         f"expected 6 total published (5 new + 1 pre-existing), "
                         f"got {published}")
        # Pre-existing link is still there exactly once.
        self.assertEqual(published.count(already_pub_link), 1,
                         "pre-existing published row must NOT be duplicated")

        # Claude was called exactly 5 times — once per slot, never on the
        # already-published row.
        self.assertEqual(mock_claude.call_count, 5)

        # Crash-loop guard sanity: the FIRST sleep is the guard wait =
        # (MIN_INTERVAL_MINUTES * 60) - 10*60 = 1800s. Allow ±5% slack
        # for any wall-clock drift between freeze_time setup and the
        # guard's `now_utc`.
        # Crash-loop guard sleep is large (~1800s) and unambiguous — but
        # filter the constant-3s Telegra.ph warmup sleeps so we don't pick
        # one of them up if order-of-call ever shifts.
        sleep_args = [c.args[0] for c in self.mock_sleep.call_args_list
                      if c.args and isinstance(c.args[0], (int, float))
                      and c.args[0] != news_bot.TELEGRAPH_CACHE_WARMUP_SECONDS]
        self.assertTrue(sleep_args, "expected at least one time.sleep call")
        first_sleep = sleep_args[0]
        # Threshold = 40 min - 10 min = 30 min = 1800s.
        self.assertGreater(first_sleep, 1800 * 0.95,
                           f"crash-loop guard sleep too short: {first_sleep}")
        self.assertLess(first_sleep, 1800 * 1.05,
                        f"crash-loop guard sleep too long: {first_sleep}")

    # ======================================================================
    # Scenario 4: manual-review preemption. Operator publishes one row
    # locally between slot 1 and slot 2; bot must skip it and pull the
    # next pending row.
    # ======================================================================

    def test_manual_review_preemption_skips_locally_published_row(self):
        """3 pending rows → 3 slots. After slot 1 publishes, the operator's
        local hw_review CLI publishes one of the remaining pending rows
        (link=`o2`). Bot's slot 2 must pull `o3` instead — no duplicate,
        no error. Final state: 3 published rows total (slot1 link,
        operator's link, slot 3 link).

        We trigger the mid-loop mutation via a side-effect on the second
        Claude mock call: when the bot is about to publish via Claude on
        slot 2, the side_effect callback first removes link `o2` from
        pending and inserts it into published_articles (the
        ``move_to_published`` repo equivalent), then returns a valid
        result so Claude's processing of whatever row WAS picked up
        still works. But — by the time the loop reaches slot 2,
        ``list_pending()`` already returned the row before the
        side_effect runs. So we mutate slot 1's tail-end via a callback
        on the FIRST Claude call instead — that side_effect runs AFTER
        slot 1's row was selected but BEFORE slot 2's selection.
        """
        entries = [
            _create_mock_rss_entry('http://example.com/o1', title='Op 1'),
            _create_mock_rss_entry('http://example.com/o2', title='Op 2'),
            _create_mock_rss_entry('http://example.com/o3', title='Op 3'),
        ]
        self._set_rss_entries(entries)

        # Mutation trigger: callable side_effect on the FIRST Claude call
        # — runs AFTER slot 1's row was already selected by list_pending,
        # but BEFORE slot 2's list_pending(). Mutates the DB to simulate
        # the operator's local publish of `o2` between slots.
        operator_link = 'http://example.com/o2'
        first_call_done = {'flag': False}

        def claude_side_effect(article):
            # First call: slot 1's row is being processed. AFTER this
            # call returns, the loop persists slot 1's row to published
            # and moves to slot 2. We perform the operator-publish AS
            # part of this side_effect so that the mutation is in place
            # before the loop's slot-2 list_pending() runs.
            if not first_call_done['flag']:
                first_call_done['flag'] = True
                # Operator-equivalent: stage RU fields (mirrors what
                # hw_review.cmd_publish does via _fallback_publish ->
                # update_staged), then atomically move o2 from pending
                # to published. update_staged is required to satisfy the
                # NOT NULL ru_title constraint inside move_to_published.
                pending_articles_repo.update_staged(
                    operator_link,
                    ru_title='Оператор Op 2',
                    ru_subtitle='',
                    ru_paragraphs=['p1.', 'p2.'],
                    ru_blocks=None,
                )
                pending_articles_repo.move_to_published(
                    operator_link,
                    'https://telegra.ph/operator-page',
                    'operator-page',
                    via_review=True,
                )
            paras_in = article.get('paragraphs') or []
            return _make_claude_result(paras_in, title_prefix='OK')

        with freeze_time('2026-04-27 09:00:00'):
            with patch('news_bot.transcreate_via_claude') as mock_claude:
                mock_claude.side_effect = claude_side_effect
                news_bot.job()

        # 2 Claude calls — slot 1 (o1) and slot 2 (o3, because o2 was
        # operator-published). slot 3 has no rows left.
        # Wait — after slot 2 finishes, the loop tries slot 3. Pending is
        # empty by then, so the loop breaks and a third Claude call
        # is NOT made.
        self.assertEqual(mock_claude.call_count, 2,
                         f"expected exactly 2 Claude calls, got "
                         f"{mock_claude.call_count}")

        # Published links: o1 (slot 1, auto), o2 (operator), o3 (slot 2).
        published = self._published_links()
        self.assertEqual(set(published), {
            'http://example.com/o1',
            'http://example.com/o2',
            'http://example.com/o3',
        })
        # No duplicates and exactly 3 rows.
        self.assertEqual(len(published), 3,
                         f"expected 3 unique published rows, got {published}")

        # Operator's row carries via_review=1 in published_articles.
        op_row = pending_articles_repo.get_published(operator_link)
        self.assertIsNotNone(op_row)
        self.assertEqual(op_row['via_review'], 1,
                         "operator-published row must have via_review=1")

        # Bot's two rows carry via_review=0.
        for link in ('http://example.com/o1', 'http://example.com/o3'):
            r = pending_articles_repo.get_published(link)
            self.assertIsNotNone(r, f"row {link} missing in published")
            self.assertEqual(r['via_review'], 0,
                             f"bot-published row {link} should have via_review=0")

        # Pending is empty.
        self.assertEqual(self._pending_links(), [])

        # No error-shaped admin notification fired (T4 — manual-review
        # preemption is a benign concurrent edit, not a failure).
        msgs = self._admin_messages()
        self.assertFalse(
            any(('ошибка' in m.lower() or 'error' in m.lower()
                 or 'failed' in m.lower()) for m in msgs),
            f"unexpected error-shaped admin ping: {msgs!r}",
        )

    # ======================================================================
    # Scenario 5 (T7, publish-idempotency-fix): slot-loop guard end-to-end.
    # Mixed pre-stage — 1 published row + 1 zombie pending (carry-over
    # tier, attempt_count=2) + 1 fresh pending. Verifies the Task-1 guard
    # in ``_fallback_publish`` short-circuits the zombie BEFORE any
    # Telegraph/Telegram side-effect, leaves ``failed_articles`` empty
    # (litmus AC6), and lets the fresh row publish exactly once.
    # ======================================================================

    def test_slot_loop_does_not_repost_already_published(self):
        """Pre-stage 1 published-row + 1 zombie pending (carry-over tier,
        ``attempt_count=2``) + 1 fresh pending. Run ``job()``. Assert that
        the guard short-circuits the zombie row (no re-publish, no strike)
        and the fresh row publishes exactly once.

        AC6 litmus rationale: zombie pending row carries ``attempt_count=2``.
        Without the guard from Task 1, the slot loop would have hit the
        UNIQUE constraint on ``INSERT INTO published_articles`` (or the
        Task-2 ``INSERT OR IGNORE`` would silently skip the published-row
        write but still try Telegram), and ``attempt_count`` would tick to
        3 — pushing the row to ``failed_articles`` via ``move_to_failed``.
        With the guard active, the zombie is intercepted before any side
        effect, ``skip_pending`` cleans it, and ``failed_articles`` stays
        empty. Therefore an empty ``failed_articles`` AT THIS attempt_count
        is direct evidence that the guard intercepted before the strike
        machinery ran (AC6).
        """
        link_zombie = 'http://example.com/zombie'
        link_fresh = 'http://example.com/fresh'

        # Pre-stage 1: published_articles row for link_zombie (raw SQL,
        # canonical pattern from tests/test_hw_review_publish_flow.py:271).
        # via_review=0 confirms zombie scenario is auto-bot, not operator.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    link_zombie, 'Zombie EN', 'Зомби РУ',
                    'https://telegra.ph/zombie-page', 'zombie-page',
                    'autoevolution', 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Pre-stage 2: zombie pending row for link_zombie via repo helper,
        # then raw UPDATE to set carry-over tier markers (fetched_at 2 days
        # ago) + cached telegraph_url + attempt_count=2 (litmus level).
        ok = pending_articles_repo.insert_pending(
            _create_mock_full_article(link_zombie, title='Zombie EN')
        )
        self.assertTrue(ok)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles "
                "SET fetched_at = datetime('now', '-2 days'), "
                "    telegraph_url = ?, "
                "    telegraph_path = ?, "
                "    attempt_count = 2 "
                "WHERE link = ?",
                ('https://telegra.ph/zombie-cached', 'zombie-cached',
                 link_zombie),
            )
            conn.commit()
        finally:
            conn.close()

        # Pre-stage 3: fresh pending row for link_fresh — fetched_at
        # defaults to CURRENT_TIMESTAMP (today's batch / fresh tier).
        ok = pending_articles_repo.insert_pending(
            _create_mock_full_article(link_fresh, title='Fresh EN')
        )
        self.assertTrue(ok)

        # Empty RSS — slot loop must process exactly the two pre-staged
        # pending rows, not pull in fresh fetches mid-test.
        self._set_rss_entries([])

        with freeze_time('2026-04-27 09:00:00'):  # 09:00 UTC == 12:00 МСК
            with patch('news_bot.transcreate_via_claude') as mock_claude:
                # ONE element — only link_fresh should reach Claude. The
                # zombie row is short-circuited by the guard before Claude.
                mock_claude.side_effect = [
                    _make_claude_result(
                        ['First paragraph.', 'Second paragraph.'],
                        title_prefix='Fresh',
                    ),
                ]
                news_bot.job()

                # Claude was invoked exactly once — for link_fresh only.
                # The zombie row never reached translation.
                self.assertEqual(
                    mock_claude.call_count, 1,
                    f"expected 1 Claude call (fresh only), got "
                    f"{mock_claude.call_count}"
                )

        # Litmus assertion: send_telegraph_teaser called exactly once,
        # and the second positional arg (source_url) is link_fresh — NOT
        # link_zombie. send_telegraph_teaser(telegraph_url, source_url) is
        # invoked positional from _fallback_publish (news_bot.py:1237).
        # call_args.kwargs is empty for positional calls; we explicitly
        # check call_args.args[1] to defeat a count-only false positive.
        self.mock_teaser.assert_called_once()
        self.assertEqual(
            self.mock_teaser.call_args.args[1], link_fresh,
            f"teaser must be sent for link_fresh, not link_zombie; "
            f"got call_args={self.mock_teaser.call_args!r}"
        )

        # published_articles: 2 rows total (pre-existing zombie + fresh).
        published = self._published_links()
        self.assertEqual(
            set(published), {link_zombie, link_fresh},
            f"expected {{link_zombie, link_fresh}}, got {published}"
        )
        self.assertEqual(
            len(published), 2,
            f"expected 2 published rows (no duplicate of link_zombie), "
            f"got {published}"
        )

        # AC6 litmus: failed_articles empty. Without the guard, the zombie
        # row at attempt_count=2 would have ticked to 3 on UNIQUE failure
        # → move_to_failed → failed_articles=1. Empty failed_articles ⇒
        # guard intercepted before the strike machinery ran.
        conn = sqlite3.connect(self.db_path)
        try:
            failed_count = conn.execute(
                "SELECT COUNT(*) FROM failed_articles"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            failed_count, 0,
            "AC6 litmus: failed_articles must be empty — guard "
            "intercepted before the slot's strike machinery ran"
        )

        # Admin ping: at least one message contains link_zombie AND the
        # guard marker «Пропущен дубль публикации» (Russian columnar format).
        msgs = self._admin_messages()
        self.assertTrue(
            any((link_zombie in m and 'Пропущен дубль публикации' in m)
                for m in msgs),
            f"expected guard ping with link_zombie and «Пропущен дубль публикации» "
            f"marker; got: {msgs!r}"
        )

        # processed_news contains both links: link_zombie via the guard's
        # skip_pending cleanup, link_fresh via move_to_published.
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT link FROM processed_news WHERE link IN (?, ?)",
                (link_zombie, link_fresh),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            {r[0] for r in rows}, {link_zombie, link_fresh},
            f"processed_news must contain both links; got {rows!r}"
        )

        # pending_articles is empty — both rows cleared (zombie via
        # skip_pending, fresh via move_to_published).
        self.assertEqual(
            self._pending_links(), [],
            "pending_articles must be empty after job()"
        )


if __name__ == '__main__':
    unittest.main()
