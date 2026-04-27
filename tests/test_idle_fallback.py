#!/usr/bin/env python3
"""Integration tests for the idle-fallback pass in ``news_bot.job()`` and the
``_fallback_publish`` helper — Task 9 of manual-review-workflow.

Covers tech-spec §9.10 prep-phase steps (1a) heads-up + (1b) overdue
auto-publish plus Decisions 9, 11, 12, 13:

* (1a) ``list_pending_stale`` → single consolidated admin ping
  (Decision 12) → ``mark_notified`` on each row.
* (1b) ``list_notified_overdue`` → ``_fallback_publish`` per row →
  ``move_to_published(via_review=False)``. On failure:
  ``sanitize_error_message`` (Decision 11) → ``increment_attempt``
  (Decision 13); third strike → ``move_to_failed``.
* Decision 9 idempotency: if the pending row already has
  ``telegraph_url``, ``publish_article`` is NOT re-called.
* Empty inputs → no admin ping, no fallback calls.

Follows the tempfile-DB pattern of ``tests/test_hw_review_publish_flow.py``
and ``tests/test_job_prep_phase.py``: allocate a sqlite file, monkeypatch
``news_bot.DB_FILE``, call ``news_bot.init_db()``, populate rows via the
real repo, SQL-UPDATE ``fetched_at`` / ``notified_at`` to simulate time
passage (honours tech-spec Testing Strategy — no Python time-mocks).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
from telegraph_publisher import TelegraphError


def _sample_entry(link='http://example.com/a', title='Example Article',
                  source='autoevolution', paragraphs=None, images=None,
                  blocks=None, subtitle='Lead text'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'First paragraph.', 'Second paragraph.',
        ],
        'images': images if images is not None else ['http://img/1.jpg'],
        'blocks': blocks,
        'pub_date': '2026-04-01',
    }


class _IdleFallbackCase(unittest.TestCase):
    """Shared fixture: tempfile DB + secret-env redaction guards off (tests
    that want to exercise redaction patch the env vars themselves)."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        # Keep admin-notification + teaser env so ``send_admin_notification``
        # isn't a no-op branch when called.
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()

        # Disable the fallback throttle for the existing test suite — these
        # tests exercise multi-row idle fallback flows and would otherwise
        # block on the real ``time.sleep(FALLBACK_THROTTLE_SECONDS)`` call.
        # The dedicated throttle assertions live in ``test_fallback_throttle``.
        self.throttle_patcher = patch('news_bot.FALLBACK_THROTTLE_SECONDS', 0)
        self.throttle_patcher.start()

        # llm-transcreation-and-distributed-publishing Task 7: the
        # primary translation engine is now Claude. These legacy idle-
        # fallback tests assert against Google-Translate behaviour
        # (``transcreate_text``), so we force a per-article
        # ``ClaudeTranscreationError`` to route every row through the
        # per-article Google fallback branch — equivalent to the pre-
        # Task 7 Google-only path. Tests that need a real Claude path
        # live in ``tests/test_fallback_publish_paths.py``.
        from claude_transcreation import ClaudeTranscreationError
        self.claude_patcher = patch(
            'news_bot.transcreate_via_claude',
            side_effect=ClaudeTranscreationError('test stub: per-article'),
        )
        self.claude_patcher.start()
        # Outage state is in a tempfile DB which has no ``bot_state``
        # rows yet — ``is_fallback_active()`` returns False naturally.
        # No patch needed.

    def tearDown(self):
        self.claude_patcher.stop()
        self.throttle_patcher.stop()
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _age_fetched(self, link: str, hours: int):
        """SQL-UPDATE ``fetched_at`` to ``datetime('now', '-{hours} hours')`` —
        tech-spec Testing Strategy recommends this over Python time-mocks
        because SQLite stamps ``CURRENT_TIMESTAMP`` on its own side."""
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

    def _age_notified(self, link: str, hours: int):
        """Same pattern for ``notified_at`` — used to exercise step (1b)."""
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

    def _set_attempt_count(self, link: str, n: int):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET attempt_count = ? WHERE link = ?",
                (int(n), link),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_telegraph_url(self, link: str, url: str, path: str):
        repo.mark_telegraph_published(link, url, path)


# ============================================================================
# Step (1a): idle heads-up
# ============================================================================


class TestHeadsUp(_IdleFallbackCase):

    def test_heads_up_single_ping_batches_all_stale(self):
        """Decision 12: three stale rows → ONE admin-notification call whose
        text carries all three titles comma-separated."""
        self._insert(link='http://a/1', title='First Article')
        self._insert(link='http://a/2', title='Second Article')
        self._insert(link='http://a/3', title='Third Article')
        for link in ('http://a/1', 'http://a/2', 'http://a/3'):
            self._age_fetched(link, 50)

        # Keep sources empty so the rest of job() is a no-op and doesn't
        # send its own admin ping.
        mock_notify = MagicMock(return_value=True)
        with patch('news_bot.send_admin_notification', mock_notify), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        # Exactly one heads-up ping. (Queue ping at step 6 is suppressed
        # for other branches because we want to assert byte-content here —
        # but the queue IS non-empty, so the existing queue-summary ping
        # also fires. Filter by prefix.)
        heads_up_calls = [
            c for c in mock_notify.call_args_list
            if c.args and 'Will auto-publish' in c.args[0]
        ]
        self.assertEqual(len(heads_up_calls), 1,
                         f"Expected one heads-up ping, got: {mock_notify.call_args_list}")
        text = heads_up_calls[0].args[0]
        self.assertIn('First Article', text)
        self.assertIn('Second Article', text)
        self.assertIn('Third Article', text)
        self.assertIn(f"~{news_bot.GRACE_WINDOW_HOURS}h", text)
        self.assertIn('hw_review take N', text)

    def test_heads_up_stamps_notified_at(self):
        """After step (1a), every stale row has ``notified_at IS NOT NULL``."""
        self._insert(link='http://a/1', title='T1')
        self._insert(link='http://a/2', title='T2')
        for link in ('http://a/1', 'http://a/2'):
            self._age_fetched(link, 50)

        with patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        for link in ('http://a/1', 'http://a/2'):
            row = repo.get_pending(link)
            self.assertIsNotNone(row, f"row {link} lost")
            self.assertIsNotNone(row['notified_at'],
                                 f"notified_at still NULL for {link}")

    def test_no_stale_no_idle_ping(self):
        """Empty stale list → no heads-up ping fires. Fresh pending row
        (fetched_at = now) must NOT be picked up."""
        self._insert(link='http://a/fresh', title='Fresh')
        # no _age_fetched — this row is fresh

        mock_notify = MagicMock(return_value=True)
        with patch('news_bot.send_admin_notification', mock_notify), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        heads_up_calls = [
            c for c in mock_notify.call_args_list
            if c.args and 'Will auto-publish' in c.args[0]
        ]
        self.assertEqual(len(heads_up_calls), 0)


# ============================================================================
# Step (1b): overdue auto-publish via _fallback_publish
# ============================================================================


class TestOverdueAutopublish(_IdleFallbackCase):

    def test_overdue_autopublishes_with_via_review_false(self):
        """Row notified 3h ago, ru_paragraphs NULL → _fallback_publish runs;
        ends up in published_articles with via_review=0."""
        entry = self._insert(link='http://a/1', title='Overdue')
        self._age_notified(entry['link'], 3)

        tg_url = 'https://telegra.ph/Overdue-04-23'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        # Row moved to published with via_review=0.
        self.assertIsNone(repo.get_pending(entry['link']))
        pub = repo.get_published(entry['link'])
        self.assertIsNotNone(pub)
        self.assertEqual(pub['via_review'], 0)
        self.assertEqual(pub['telegraph_url'], tg_url)
        # Teaser was called with stored URL + source link (Decision 14).
        # Decision 14 byte-equality: teaser is single-line hashtag for
        # both manual and auto paths. The auto-marker now lives in the
        # Telegra.ph article body (see test_telegraph_publisher
        # TestAutoMarkerInArticleBody) — NOT in the teaser kwargs.
        mock_teaser.assert_called_once_with(tg_url, entry['link'])

    def test_fallback_failure_increments_attempt_count(self):
        """``publish_article`` raises → attempt_count bumped to 1; last_error
        stored; no secret leakage (Decision 11)."""
        entry = self._insert(link='http://a/1', title='Fail-once')
        self._age_notified(entry['link'], 3)

        # Simulate a Telegraph error whose str() embeds the bot token.
        # Decision 11 — sanitize_error_message must redact it.
        with patch.dict(os.environ, {'TELEGRAPH_ACCESS_TOKEN': 'TOP_SECRET_XYZ'}):
            mock_publish = MagicMock(
                side_effect=TelegraphError('boom token=TOP_SECRET_XYZ'),
            )
            mock_teaser = MagicMock(return_value=True)
            with patch('news_bot.telegraph_publisher.publish_article',
                       mock_publish), \
                 patch('news_bot.send_telegraph_teaser', mock_teaser), \
                 patch('news_bot.send_admin_notification', return_value=True), \
                 patch('news_bot.SOURCES', []):
                news_bot.job()

        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row, 'row should still be pending after 1st strike')
        self.assertEqual(row['attempt_count'], 1)
        self.assertIsNotNone(row['last_error'])
        self.assertIn('boom', row['last_error'])
        self.assertNotIn('TOP_SECRET_XYZ', row['last_error'])

    def test_fallback_third_strike_moves_to_failed(self):
        """Row with attempt_count=2 → publish_article fails → attempt_count=3
        → move_to_failed (Decision 13). No pending row remains."""
        entry = self._insert(link='http://a/1', title='Third-strike')
        self._age_notified(entry['link'], 3)
        self._set_attempt_count(entry['link'], 2)

        mock_publish = MagicMock(side_effect=RuntimeError('final boom'))

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', return_value=True), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        self.assertIsNone(repo.get_pending(entry['link']))
        fail = repo.get_failed(entry['link'])
        self.assertIsNotNone(fail)
        self.assertIn('final boom', fail['last_error'])

    def test_telegraph_url_reused_on_fallback_retry(self):
        """Decision 9: if ``row['telegraph_url']`` already set,
        ``publish_article`` is NOT called; teaser uses the saved URL."""
        entry = self._insert(link='http://a/1', title='Retry')
        self._age_notified(entry['link'], 3)
        saved_url = 'https://telegra.ph/Retry-01-01'
        self._set_telegraph_url(entry['link'], saved_url, 'Retry-01-01')

        mock_publish = MagicMock(return_value='SHOULD_NOT_BE_USED')
        mock_teaser = MagicMock(return_value=True)

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        mock_publish.assert_not_called()
        # Teaser is single-line for both paths — no auto_marker kwarg.
        mock_teaser.assert_called_once_with(saved_url, entry['link'])
        self.assertIsNone(repo.get_pending(entry['link']))
        pub = repo.get_published(entry['link'])
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], saved_url)

    def test_fallback_telegram_failure_persists_telegraph_url(self):
        """Teaser returns False → attempt_count bumps, row stays pending with
        telegraph_url populated (next tick reuses per Decision 9)."""
        entry = self._insert(link='http://a/1', title='TG-fail')
        self._age_notified(entry['link'], 3)

        tg_url = 'https://telegra.ph/TG-fail-01-01'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=False)

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertEqual(row['telegraph_url'], tg_url)
        self.assertEqual(row['attempt_count'], 1)

    def test_fallback_isolates_per_row_failures(self):
        """Two overdue rows, one raises — the other still publishes."""
        a = self._insert(link='http://a/1', title='A')
        b = self._insert(link='http://a/2', title='B')
        self._age_notified(a['link'], 3)
        self._age_notified(b['link'], 3)

        def publish_side(**kw):
            if kw.get('source_url') == a['link']:
                raise RuntimeError('row-A boom')
            return 'https://telegra.ph/B-01-01'

        mock_publish = MagicMock(side_effect=publish_side)
        mock_teaser = MagicMock(return_value=True)

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.send_admin_notification', return_value=True), \
             patch('news_bot.SOURCES', []):
            news_bot.job()

        # A still pending with attempt_count=1; B published.
        row_a = repo.get_pending(a['link'])
        self.assertIsNotNone(row_a)
        self.assertEqual(row_a['attempt_count'], 1)

        self.assertIsNone(repo.get_pending(b['link']))
        self.assertIsNotNone(repo.get_published(b['link']))


# ============================================================================
# _fallback_publish unit-level tests (no job() wrapper)
# ============================================================================


class TestFallbackPublishHelper(_IdleFallbackCase):

    def test_helper_calls_transcreate_on_paragraphs(self):
        """``_fallback_publish`` translates each EN paragraph via
        ``transcreate_text`` and passes the RU list into publish_article."""
        entry = self._insert(link='http://a/1', title='EN Title',
                             subtitle='EN lead',
                             paragraphs=['EN one.', 'EN two.'])

        tg_url = 'https://telegra.ph/Trans-01-01'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)
        # Deterministic stub so we can assert the RU list shape.
        mock_trans = MagicMock(side_effect=lambda t, **kw: f"[ru] {t}")

        with patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.transcreate_text', mock_trans):
            row = repo.get_pending(entry['link'])
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        self.assertEqual(mock_publish.call_count, 1)
        kwargs = mock_publish.call_args.kwargs
        # Title translated with is_title=True flag.
        self.assertEqual(kwargs['title'], '[ru] EN Title')
        # Paragraphs translated individually.
        self.assertEqual(kwargs['paragraphs'], ['[ru] EN one.', '[ru] EN two.'])
        # Subtitle translated.
        self.assertEqual(kwargs['subtitle'], '[ru] EN lead')

    def test_helper_cleans_up_preview_html(self):
        """On success, _cleanup_preview_html is invoked with the row's
        preview_html_path. We verify by seeding the path column and
        asserting the file is gone afterwards."""
        entry = self._insert(link='http://a/1', title='T')
        # Create a real tempfile so unlink has something to do.
        pv_fd, pv_path = tempfile.mkstemp(suffix='.html')
        os.close(pv_fd)
        repo.set_preview_path(entry['link'], pv_path)
        self.assertTrue(os.path.exists(pv_path))

        tg_url = 'https://telegra.ph/T-01-01'
        with patch('news_bot.telegraph_publisher.publish_article',
                   MagicMock(return_value=tg_url)), \
             patch('news_bot.send_telegraph_teaser',
                   MagicMock(return_value=True)), \
             patch('news_bot.transcreate_text',
                   MagicMock(side_effect=lambda t, **kw: t)):
            row = repo.get_pending(entry['link'])
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        self.assertFalse(os.path.exists(pv_path),
                         'preview file should have been removed')

    def test_helper_teaser_false_raises(self):
        """When ``send_telegraph_teaser`` returns False, helper must signal
        failure to the caller — either raise or return False — so the
        job()-level wrapper can bump ``attempt_count``. Helper contract:
        returns False without side effects on the pending row beyond
        persisting the telegraph_url."""
        entry = self._insert(link='http://a/1', title='T')

        tg_url = 'https://telegra.ph/T-01-01'
        with patch('news_bot.telegraph_publisher.publish_article',
                   MagicMock(return_value=tg_url)), \
             patch('news_bot.send_telegraph_teaser',
                   MagicMock(return_value=False)), \
             patch('news_bot.transcreate_text',
                   MagicMock(side_effect=lambda t, **kw: t)):
            row = repo.get_pending(entry['link'])
            with self.assertRaises(Exception):
                news_bot._fallback_publish(row, via_review=False)

        # Row still pending with telegraph_url stored (Decision 9).
        row_after = repo.get_pending(entry['link'])
        self.assertIsNotNone(row_after)
        self.assertEqual(row_after['telegraph_url'], tg_url)


if __name__ == '__main__':
    unittest.main()
