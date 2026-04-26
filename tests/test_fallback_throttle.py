#!/usr/bin/env python3
"""Tests for the fallback-throttle + auto-marker feature.

Two-part feature:

* Part A — ``FALLBACK_THROTTLE_SECONDS`` (default 3600 = 1h) inserts a
  ``time.sleep`` between sequential ``_fallback_publish`` calls in both
  the overflow fast-track loop (`_overflow_fast_track`) and the idle-
  fallback loop (job() step 1b). Skip-first: 1 publish in a batch = no
  wait; N publishes = (N-1) waits.

* Part B — auto-marker line ``↳ автоперевод`` (U+21B3 + space +
  Russian "autotranslation") appears as a SECOND line under the source
  hashtag in ``send_telegraph_teaser`` when ``auto_marker=True``. The
  manual-review path (``hw_review publish``) calls the function
  without the flag → single-line teaser unchanged.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo


# ---------------------------------------------------------------------------
# Constants — Part A: throttle env-var
# ---------------------------------------------------------------------------


class TestFallbackThrottleConstant(unittest.TestCase):
    """``FALLBACK_THROTTLE_SECONDS`` is module-level, env-overridable,
    defaults to 3600 (60 min)."""

    def test_default_value_is_3600(self):
        # Re-import with the env var unset.
        env = {k: v for k, v in os.environ.items()
               if k != 'FALLBACK_THROTTLE_SECONDS'}
        with patch.dict(os.environ, env, clear=True):
            importlib.reload(news_bot)
            self.assertEqual(news_bot.FALLBACK_THROTTLE_SECONDS, 3600)
        # Restore default module state for the rest of the suite.
        importlib.reload(news_bot)

    def test_env_override_parses_as_int(self):
        with patch.dict(os.environ, {'FALLBACK_THROTTLE_SECONDS': '120'}):
            importlib.reload(news_bot)
            self.assertEqual(news_bot.FALLBACK_THROTTLE_SECONDS, 120)
        importlib.reload(news_bot)


# ---------------------------------------------------------------------------
# Shared tempfile-DB fixture (mirrors test_overflow / test_idle_fallback)
# ---------------------------------------------------------------------------


def _sample_entry(link='http://example.com/a', title='Example',
                  source='autoevolution'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': 'Lead',
        'paragraphs': ['Para one.', 'Para two.'],
        'images': [],
        'blocks': None,
        'pub_date': '2026-04-01',
    }


class _ThrottleCase(unittest.TestCase):

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

        self.cap_patcher = patch('news_bot.QUEUE_CAP', 10)
        self.cap_patcher.start()

        # Pin throttle to a known integer so assertions are stable.
        self.throttle_patcher = patch('news_bot.FALLBACK_THROTTLE_SECONDS', 3600)
        self.throttle_patcher.start()

    def tearDown(self):
        self.throttle_patcher.stop()
        self.cap_patcher.stop()
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _age_fetched(self, link, hours):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET fetched_at = "
                "datetime('now', ? || ' hours') WHERE link = ?",
                (f"-{int(hours)}", link),
            )
            conn.commit()
        finally:
            conn.close()

    def _age_notified(self, link, hours):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET notified_at = "
                "datetime('now', ? || ' hours') WHERE link = ?",
                (f"-{int(hours)}", link),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Part A: overflow fast-track throttle
# ---------------------------------------------------------------------------


class TestOverflowThrottle(_ThrottleCase):

    def test_overflow_single_evict_no_sleep(self):
        """Pool=11, 1 unstaged old → exactly 1 ``_fallback_publish`` call,
        zero ``time.sleep`` calls (skip-first pattern)."""
        # 10 staged rows (protected) + 1 unstaged → pool 11.
        for i in range(10):
            entry = _sample_entry(link=f'http://staged/{i}', title=f'S{i}')
            self.assertTrue(repo.insert_pending(entry))
            repo.update_staged(entry['link'], 'РУ', '', ['абз'], None)
        self._insert(link='http://open/0', title='O0')
        self._age_fetched('http://open/0', 100)

        # No new entries → pool would shrink, force overflow with 1 new.
        # Actually pool = 11 (10 staged + 1 unstaged) + 1 new = 12; cap 10.
        # excess = 2. 1 unstaged evicts, 1 new auto-pubs. We want a single
        # _fallback_publish — so just give 0 new entries and reduce cap.
        # Easier: cap=10, 11 pending+1 new=12, excess=2 → 1 old + 1 new.
        # That's 2 fallback calls. To hit "exactly 1" use cap=11+1 - 1 = 11
        # actually we want pool > cap by 1.
        with patch('news_bot.QUEUE_CAP', 11):
            new_entries = [{
                'link': 'http://new/x', 'title': 'X', 'published': '',
                'summary': '', 'feed_url': None,
                'source_name': 'autoevolution',
            }]

            mock_fallback = MagicMock(return_value=True)
            mock_sleep = MagicMock()

            def side(row, via_review=False):
                repo.update_staged(row['link'], 'ru', '', ['p'], None)
                repo.move_to_published(
                    row['link'], 'https://telegra.ph/x', 'x',
                    via_review=via_review,
                )
                return True
            mock_fallback.side_effect = side

            with patch('news_bot._fallback_publish', mock_fallback), \
                 patch('news_bot.time.sleep', mock_sleep), \
                 patch('news_bot.send_admin_notification', return_value=True):
                news_bot._overflow_fast_track(new_entries)

            self.assertEqual(mock_fallback.call_count, 1,
                             'exactly one fallback publish expected')
            self.assertEqual(mock_sleep.call_count, 0,
                             'skip-first: single publish must not sleep')

    def test_overflow_three_evicts_two_sleeps(self):
        """3 sequential ``_fallback_publish`` calls → exactly 2
        ``time.sleep(FALLBACK_THROTTLE_SECONDS)`` calls (N-1 pattern)."""
        # 10 unstaged rows aged differently; cap=10, 3 new → excess=3.
        for i in range(10):
            self._insert(link=f'http://open/{i}', title=f'O{i}')
            self._age_fetched(f'http://open/{i}', 100 - i)

        new_entries = [
            {'link': f'http://new/{i}', 'title': f'N{i}', 'published': '',
             'summary': '', 'feed_url': None,
             'source_name': 'autoevolution'} for i in range(3)
        ]

        mock_fallback = MagicMock(return_value=True)
        mock_sleep = MagicMock()

        def side(row, via_review=False):
            repo.update_staged(row['link'], 'ru', '', ['p'], None)
            repo.move_to_published(
                row['link'], 'https://telegra.ph/x', 'x',
                via_review=via_review,
            )
            return True
        mock_fallback.side_effect = side

        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.time.sleep', mock_sleep), \
             patch('news_bot.send_admin_notification', return_value=True):
            news_bot._overflow_fast_track(new_entries)

        self.assertEqual(mock_fallback.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2,
                         '3 publishes → exactly 2 sleeps (skip-first)')
        # Each sleep must use the configured throttle value.
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args[0], 3600)


# ---------------------------------------------------------------------------
# Part A: idle-fallback throttle (job() step 1b)
# ---------------------------------------------------------------------------


class TestIdleFallbackThrottle(_ThrottleCase):

    def test_idle_single_overdue_no_sleep(self):
        """Exactly 1 overdue row → 1 fallback, 0 sleeps."""
        entry = self._insert(link='http://idle/a', title='Idle A')
        self._age_notified(entry['link'], 5)

        mock_fallback = MagicMock(return_value=True)
        mock_sleep = MagicMock()

        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.time.sleep', mock_sleep), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        self.assertEqual(mock_fallback.call_count, 1)
        # job() may call time.sleep elsewhere — assert NONE used the
        # throttle value for the idle batch.
        throttle_sleeps = [
            c for c in mock_sleep.call_args_list
            if c.args and c.args[0] == 3600
        ]
        self.assertEqual(len(throttle_sleeps), 0,
                         'single overdue must not trigger throttle sleep')

    def test_idle_three_overdue_two_throttle_sleeps(self):
        """3 overdue rows → 3 fallback calls, exactly 2 throttle sleeps."""
        for i in range(3):
            entry = self._insert(link=f'http://idle/{i}', title=f'I{i}')
            self._age_notified(entry['link'], 5 + i)

        mock_fallback = MagicMock(return_value=True)
        mock_sleep = MagicMock()

        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.time.sleep', mock_sleep), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        self.assertEqual(mock_fallback.call_count, 3)
        throttle_sleeps = [
            c for c in mock_sleep.call_args_list
            if c.args and c.args[0] == 3600
        ]
        self.assertEqual(len(throttle_sleeps), 2,
                         '3 overdues → 2 throttle sleeps (skip-first)')


# ---------------------------------------------------------------------------
# Part B: auto-marker in teaser
# ---------------------------------------------------------------------------


class TestAutoMarkerTeaser(unittest.TestCase):
    """``send_telegraph_teaser(..., auto_marker=True)`` adds a second
    line ``↳ автоперевод``; the manual path (no flag / False) is
    unchanged single-line."""

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_manual_teaser_is_single_line(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = news_bot.send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://autoevolution.com/news/x.html',
        )
        self.assertTrue(ok)

        text = mock_bot.send_message.await_args.kwargs['text']
        self.assertEqual(text, '#autoevolution',
                         'manual path: single-line teaser unchanged')
        self.assertNotIn('\n', text)
        self.assertNotIn('автоперевод', text)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_auto_marker_appends_second_line(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = news_bot.send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://autoevolution.com/news/x.html',
            auto_marker=True,
        )
        self.assertTrue(ok)

        text = mock_bot.send_message.await_args.kwargs['text']
        # Two lines exactly: hashtag, then marker.
        self.assertEqual(text, '#autoevolution\n↳ автоперевод')

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_marker_codepoint_is_u21b3(self, mock_bot_class):
        """Pin the arrow byte-for-byte to U+21B3 ('↳'). Pinning the
        codepoint protects against editor-driven character drift
        (e.g. someone replaces it with U+2192 '→')."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        news_bot.send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://corporate.mattel.com/news/x',
            auto_marker=True,
        )
        text = mock_bot.send_message.await_args.kwargs['text']
        # Expected exact bytes (UTF-8): '#mattel\n↳ автоперевод'.
        self.assertIn('↳', text)
        # Reject look-alike arrows.
        for bad in ('→', '↪', '↱', '->'):
            self.assertNotIn(bad, text)
        # Marker line is the SECOND line.
        lines = text.split('\n')
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], '#mattel')
        self.assertEqual(lines[1], '↳ автоперевод')

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_auto_marker_default_false(self, mock_bot_class):
        """Calling ``send_telegraph_teaser`` without ``auto_marker`` (as
        ``hw_review.cmd_publish`` does) defaults to single-line."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        news_bot.send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://lamleygroup.com/post/',
        )
        text = mock_bot.send_message.await_args.kwargs['text']
        self.assertEqual(text, '#lamleygroup')


# ---------------------------------------------------------------------------
# Part B: integration — _fallback_publish forwards auto_marker=True
# ---------------------------------------------------------------------------


class TestFallbackPublishPassesAutoMarker(_ThrottleCase):

    def test_fallback_calls_teaser_with_auto_marker_true(self):
        """Inside ``_fallback_publish`` (via_review=False) the teaser
        invocation must propagate ``auto_marker=True``."""
        entry = self._insert(link='http://idle/a', title='Idle A')
        self._age_notified(entry['link'], 5)

        mock_teaser = MagicMock(return_value=True)
        mock_publish = MagicMock(return_value='https://telegra.ph/X')

        with patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        # send_telegraph_teaser must have been called with auto_marker=True
        # for the auto-fallback path.
        self.assertEqual(mock_teaser.call_count, 1)
        kwargs = mock_teaser.call_args.kwargs
        self.assertTrue(
            kwargs.get('auto_marker') is True,
            f'expected auto_marker=True, got kwargs={kwargs!r} '
            f'args={mock_teaser.call_args.args!r}',
        )


if __name__ == '__main__':
    unittest.main()
