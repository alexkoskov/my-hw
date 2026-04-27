#!/usr/bin/env python3
"""Integration tests for the prep-only shape of ``news_bot.job()``.

After Task 6 of manual-review-workflow, ``job()`` must:
* Iterate the ``SOURCES`` registry (Task 5) — no direct feed-URL loop.
* Filter against both ``processed_news`` AND ``pending_articles``.
* Call ``fetch_full_article`` for each accepted entry and stage the
  result via ``pending_articles_repo.insert_pending``.
* Compose the admin-ping via ``build_admin_ping(rows)`` and send it
  ONLY when the resulting queue is non-empty.
* Make ZERO Telegraph-publish / channel-teaser calls during prep.
* Leave NO trace of ``process_new_articles`` in the module.

``job()`` also reads ``QUEUE_CAP`` / ``IDLE_TIMEOUT_HOURS`` /
``GRACE_WINDOW_HOURS`` as env-overridable module constants — those
overrides are exercised here too.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo


class PrepPhaseBase(unittest.TestCase):
    """Shared tempfile-DB scaffolding for prep-phase tests."""

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

        # Disable the auto-fallback throttle to keep multi-row prep flows
        # snappy; the throttle is exercised in ``test_fallback_throttle``.
        self.throttle_patcher = patch('news_bot.FALLBACK_THROTTLE_SECONDS', 0)
        self.throttle_patcher.start()

        # Task 8 turned ``job()`` into fetch+stage PLUS a distributed-
        # publish loop. The prep-only invariants exercised here still hold
        # (no Telegraph publish during the staging phase, queue counts,
        # admin-ping suppression on N=0), but we must keep the loop from
        # actually sleeping or auto-publishing during these tests. Two
        # patches do the work:
        #   * ``time.sleep`` → no-op (window slot waits would otherwise
        #     stall the test runner for hours).
        #   * ``_fallback_publish`` → no-op (would otherwise try to call
        #     Telegram / Telegraph; the prep tests assert those calls
        #     don't happen DURING the staging phase, but the publish loop
        #     fires after staging — see new test file
        #     ``test_job_distributed_publish.py``).
        self.sleep_patcher = patch('news_bot.time.sleep')
        self.sleep_patcher.start()
        self.publish_patcher = patch('news_bot._fallback_publish')
        self.publish_patcher.start()
        # Force fallback-inactive so the loop tries the Claude path —
        # which is also patched here as a safety net for any stragglers.
        self.outage_patcher = patch(
            'news_bot.outage_state.is_fallback_active', return_value=False,
        )
        self.outage_patcher.start()

    def tearDown(self):
        self.outage_patcher.stop()
        self.publish_patcher.stop()
        self.sleep_patcher.stop()
        self.throttle_patcher.stop()
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    @staticmethod
    def _rss_entry(link, title='Title', source_name='autoevolution'):
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
            'subtitle': 'Editorial lead',
            'paragraphs': ['First paragraph.', 'Second paragraph.'],
            'images': ['https://example.com/img.jpg'],
        }


def _patch_sources(rss_return=None, mattel_return=None,
                   rss_side_effect=None, mattel_side_effect=None):
    """Return a ``patch`` context that swaps ``SOURCES`` with callables
    built from the arguments. ``job()`` iterates ``SOURCES``, so
    patching the module attribute on the individual fetchers would be
    a no-op — the list was captured at module-load time."""
    def rss(notifier=None):
        if rss_side_effect is not None:
            if isinstance(rss_side_effect, BaseException) or (
                isinstance(rss_side_effect, type)
                and issubclass(rss_side_effect, BaseException)
            ):
                raise rss_side_effect
            if callable(rss_side_effect):
                return rss_side_effect(notifier=notifier)
        return rss_return if rss_return is not None else []

    def mattel(notifier=None):
        if mattel_side_effect is not None:
            if isinstance(mattel_side_effect, BaseException) or (
                isinstance(mattel_side_effect, type)
                and issubclass(mattel_side_effect, BaseException)
            ):
                raise mattel_side_effect
            if callable(mattel_side_effect):
                return mattel_side_effect(notifier=notifier)
        return mattel_return if mattel_return is not None else []

    return patch('news_bot.SOURCES', [rss, mattel])


class TestPrepDoesNotPublish(PrepPhaseBase):
    """AC: prep-phase makes 0 ``send_telegraph_teaser`` and 0
    ``publish_article`` calls — accepted rows land in
    ``pending_articles`` instead."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article')
    def test_prep_phase_does_not_publish(
        self, mock_fetch_article, mock_publish, mock_teaser, mock_admin,
    ):
        mock_fetch_article.side_effect = lambda e: self._article_payload(
            title=e.get('title') or ''
        )

        with _patch_sources(
            rss_return=[
                self._rss_entry('http://example.com/a1', 'Autoevo 1',
                                source_name='autoevolution'),
                self._rss_entry('http://example.com/a2', 'Lamley 1',
                                source_name='lamley'),
            ],
            mattel_return=[
                {
                    'link': 'http://corporate.mattel.com/news/m1',
                    'title': 'Mattel 1',
                    'feed_url': 'http://corporate.mattel.com/news/',
                    'source_name': 'mattel',
                },
            ],
        ):
            news_bot.job()

        # No publish-side work during prep.
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()

        # All three entries staged.
        self.assertEqual(pending_articles_repo.count_pending(), 3)
        # And each carries its source_name as passed by the fetcher.
        rows = pending_articles_repo.list_pending()
        self.assertEqual(
            sorted(r['source_name'] for r in rows),
            ['autoevolution', 'lamley', 'mattel'],
        )


class TestAdminPing(PrepPhaseBase):
    """AC: admin-ping uses ``build_admin_ping`` byte-for-byte and is
    suppressed when the post-prep queue is empty."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article')
    def test_admin_ping_plan_of_day_sent(
        self, mock_fetch_article, mock_publish, mock_teaser, mock_admin,
    ):
        """After Task 8 the prep-tick admin ping is the plan-of-day
        summary (``Зафетчил X новых, в очереди M, расписание сегодня:
        HH:MM, …; carry-over: K``), not the legacy
        ``build_admin_ping(rows)`` format. We assert the new
        contract — the byte-exact format check belongs in
        ``test_job_distributed_publish``."""
        mock_fetch_article.return_value = self._article_payload()

        with _patch_sources(rss_return=[
            self._rss_entry('http://example.com/a1', 'T1',
                            source_name='autoevolution'),
            self._rss_entry('http://example.com/a2', 'T2',
                            source_name='autoevolution'),
        ]):
            news_bot.job()

        plan_calls = [
            c for c in mock_admin.call_args_list
            if c.args and isinstance(c.args[0], str)
            and 'Зафетчил' in c.args[0]
            and 'расписание' in c.args[0]
        ]
        self.assertEqual(
            len(plan_calls), 1,
            msg=f"Expected one plan-of-day ping; got: "
                f"{mock_admin.call_args_list}",
        )

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article')
    def test_admin_ping_suppressed_on_empty_queue(
        self, mock_fetch_article, mock_publish, mock_teaser, mock_admin,
    ):
        with _patch_sources():  # all sources return []
            news_bot.job()

        # No pending row → no ping string. Any admin-notification that
        # happened (e.g. source warning) must NOT be the ping string.
        # Easiest invariant: `build_admin_ping([])` is None, so no call can
        # pass that string.
        self.assertIsNone(news_bot.build_admin_ping([]))

        # Nothing in the queue, so there's no matching "N ждут review" call.
        for call_args in mock_admin.call_args_list:
            if not call_args.args:
                continue
            arg0 = call_args.args[0]
            if isinstance(arg0, str):
                self.assertNotIn('ждут review', arg0)


class TestFiltering(PrepPhaseBase):
    """AC: entries already in ``processed_news`` OR ``pending_articles``
    are filtered out — no duplicate INSERTs."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article')
    def test_entries_in_processed_news_are_skipped(
        self, mock_fetch_article, mock_publish, mock_teaser, mock_admin,
    ):
        # Seed processed_news with a link that will come back from the feed.
        news_bot.mark_processed(
            'http://example.com/old', 'Old title', '2024-12-01'
        )

        mock_fetch_article.side_effect = lambda e: self._article_payload(
            title=e.get('title') or ''
        )

        with _patch_sources(rss_return=[
            self._rss_entry('http://example.com/old', 'Old title'),
            self._rss_entry('http://example.com/new', 'New title'),
        ]):
            news_bot.job()

        # Only the new link got staged; fetch_full_article called once.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        staged = pending_articles_repo.list_pending()[0]
        self.assertEqual(staged['link'], 'http://example.com/new')
        self.assertEqual(mock_fetch_article.call_count, 1)

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article')
    def test_entries_already_pending_are_skipped(
        self, mock_fetch_article, mock_publish, mock_teaser, mock_admin,
    ):
        # Seed pending directly.
        pending_articles_repo.insert_pending({
            'link': 'http://example.com/p',
            'source_name': 'autoevolution',
            'title': 'Already staged',
            'paragraphs': ['existing'],
            'images': [],
        })
        self.assertEqual(pending_articles_repo.count_pending(), 1)

        mock_fetch_article.side_effect = lambda e: self._article_payload()

        with _patch_sources(rss_return=[
            self._rss_entry('http://example.com/p', 'Already staged'),
            self._rss_entry('http://example.com/fresh', 'Fresh'),
        ]):
            news_bot.job()

        # One pre-existing + one fresh → two rows total; fetch called once.
        self.assertEqual(pending_articles_repo.count_pending(), 2)
        self.assertEqual(mock_fetch_article.call_count, 1)


class TestFetchArticleFailures(PrepPhaseBase):
    """Existing behaviour preserved: when ``fetch_full_article`` returns
    ``None`` or a payload with empty ``paragraphs``, the entry is skipped
    without an INSERT (matches the legacy ``process_new_articles`` guard)."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.fetch_full_article', return_value=None)
    def test_none_payload_skipped(self, mock_fetch, mock_admin):
        with _patch_sources(rss_return=[
            self._rss_entry('http://example.com/x'),
        ]):
            news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 0)

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.fetch_full_article')
    def test_empty_paragraphs_skipped(self, mock_fetch, mock_admin):
        mock_fetch.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': [], 'images': [],
        }

        with _patch_sources(rss_return=[
            self._rss_entry('http://example.com/x'),
        ]):
            news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 0)


class TestSourceErrorIsolation(PrepPhaseBase):
    """One source raising doesn't abort the whole prep pass — other
    sources are still consumed."""

    @patch('news_bot.send_admin_notification')
    @patch('news_bot.fetch_full_article')
    def test_source_exception_isolated(self, mock_fetch, mock_admin):
        mock_fetch.return_value = {
            'title': 'M', 'subtitle': '', 'paragraphs': ['p'], 'images': [],
        }

        with _patch_sources(
            rss_side_effect=RuntimeError('rss boom'),
            mattel_return=[
                {
                    'link': 'http://corporate.mattel.com/news/m',
                    'title': 'M',
                    'source_name': 'mattel',
                },
            ],
        ):
            news_bot.job()

        # Mattel row still staged.
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        # Admin got SOME notification about the RSS failure.
        self.assertTrue(mock_admin.called)


class TestEnvOverridableConstants(unittest.TestCase):
    """AC: ``IDLE_TIMEOUT_HOURS`` / ``GRACE_WINDOW_HOURS`` / ``QUEUE_CAP``
    are module constants seeded from env, with explicit int defaults."""

    def test_defaults(self):
        # Re-import in a clean env so we see the defaults.
        # (Do NOT leave the module swapped — other tests rely on the live one.)
        saved = {
            k: os.environ.pop(k, None)
            for k in ('IDLE_TIMEOUT_HOURS', 'GRACE_WINDOW_HOURS', 'QUEUE_CAP')
        }
        try:
            importlib.reload(news_bot)
            self.assertEqual(news_bot.IDLE_TIMEOUT_HOURS, 48)
            self.assertEqual(news_bot.GRACE_WINDOW_HOURS, 2)
            self.assertEqual(news_bot.QUEUE_CAP, 10)
            self.assertIsInstance(news_bot.IDLE_TIMEOUT_HOURS, int)
            self.assertIsInstance(news_bot.GRACE_WINDOW_HOURS, int)
            self.assertIsInstance(news_bot.QUEUE_CAP, int)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(news_bot)

    def test_env_overrides_applied(self):
        saved = {
            k: os.environ.get(k)
            for k in ('IDLE_TIMEOUT_HOURS', 'GRACE_WINDOW_HOURS', 'QUEUE_CAP')
        }
        os.environ['IDLE_TIMEOUT_HOURS'] = '72'
        os.environ['GRACE_WINDOW_HOURS'] = '5'
        os.environ['QUEUE_CAP'] = '20'
        try:
            importlib.reload(news_bot)
            self.assertEqual(news_bot.IDLE_TIMEOUT_HOURS, 72)
            self.assertEqual(news_bot.GRACE_WINDOW_HOURS, 5)
            self.assertEqual(news_bot.QUEUE_CAP, 20)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(news_bot)


class TestProcessNewArticlesRemoved(unittest.TestCase):
    """AC: ``process_new_articles`` is fully removed from the module
    surface — no function, no attribute."""

    def test_no_attribute(self):
        self.assertFalse(
            hasattr(news_bot, 'process_new_articles'),
            msg="process_new_articles must be deleted from news_bot module",
        )


class TestCronScheduleDailyMSK(unittest.TestCase):
    """AC (Task 8 / Decisions 2 + 4): cron runs once daily at 12:00 МСК
    via TZ-aware ``schedule.every().day.at("12:00", tz=pytz.timezone(
    "Europe/Moscow"))``. Replaces the legacy 12-hour cron from manual-
    review-workflow. The schedule lives in ``main()``; we assert the
    source text to avoid actually running the scheduler."""

    def test_main_uses_daily_msk_schedule(self):
        import inspect
        src = inspect.getsource(news_bot.main)
        self.assertIn('every().day.at(', src,
                      msg="main() must register the daily 12:00 МСК cron")
        self.assertIn('12:00', src)
        self.assertIn('Europe/Moscow', src)
        # And the old 12-hour cadence must be gone.
        self.assertNotIn('every(12).hours', src,
                         msg="legacy 12-hour cron line must be removed")


if __name__ == '__main__':
    unittest.main()
