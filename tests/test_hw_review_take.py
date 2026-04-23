#!/usr/bin/env python3
"""Unit tests for ``hw_review take N`` (Task 9 of manual-review-workflow).

Subcommand contract (tech-spec §Review+publish, user-spec AC L66–L67):

* Happy: row in pending with ``notified_at != NULL`` → ``clear_notified``
  called, stdout carries ``"notification cleared"``, exit 0.
* Already left pending: row in ``published_articles`` or
  ``failed_articles`` → stderr ``"already auto-published"`` (AC L67
  wording), exit 1, no DB mutation.
* Out of range ``N`` → stderr without traceback, exit 1.
* ``clear_notified`` touches only ``notified_at`` — ``attempt_count``,
  ``last_error``, ``ru_*``, ``telegraph_url`` are untouched.

Follows the tempfile-DB pattern of ``tests/test_hw_review_cli.py``.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
import hw_review


def _sample_entry(link='http://example.com/a', title='Example Article',
                  source='autoevolution'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': 'Lead',
        'paragraphs': ['p1.', 'p2.'],
        'images': [],
        'blocks': None,
        'pub_date': '2026-04-01',
    }


class _TakeCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _run(self, argv):
        with patch('sys.stdout', self.stdout), patch('sys.stderr', self.stderr):
            return hw_review.main(argv)

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _mark_notified_ago(self, link: str, minutes: int):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET notified_at = "
                "datetime('now', ? || ' minutes') WHERE link = ?",
                (f"-{int(minutes)}", link),
            )
            conn.commit()
        finally:
            conn.close()


class TestTakeHappy(_TakeCase):

    def test_take_clears_notified_at_during_grace(self):
        entry = self._insert(link='http://a/1', title='T1')
        self._mark_notified_ago(entry['link'], 30)

        # Sanity: notified_at is non-null before.
        before = repo.get_pending(entry['link'])
        self.assertIsNotNone(before['notified_at'])

        rc = self._run(['take', '1'])
        self.assertEqual(rc, 0, self.stderr.getvalue())
        self.assertIn('notification cleared', self.stdout.getvalue().lower())

        after = repo.get_pending(entry['link'])
        self.assertIsNotNone(after, 'row must still be pending')
        self.assertIsNone(after['notified_at'],
                          'notified_at must be cleared')

    def test_take_preserves_other_columns(self):
        """``clear_notified`` touches only ``notified_at`` —
        ``attempt_count``, ``last_error``, ``ru_*``, ``telegraph_url``
        stay put."""
        entry = self._insert(link='http://a/1', title='Preserve')
        self._mark_notified_ago(entry['link'], 15)
        repo.update_staged(entry['link'], 'РУ', 'РУ лид', ['Абзац'], None)
        repo.increment_attempt(entry['link'], 'prior-err')
        repo.mark_telegraph_published(entry['link'],
                                      'https://telegra.ph/x-01-01',
                                      'x-01-01')

        before = repo.get_pending(entry['link'])
        rc = self._run(['take', '1'])
        self.assertEqual(rc, 0)
        after = repo.get_pending(entry['link'])

        self.assertIsNone(after['notified_at'])
        # Every other column retained.
        for col in ('ru_title', 'ru_subtitle', 'ru_paragraphs',
                    'attempt_count', 'last_error', 'telegraph_url',
                    'telegraph_path', 'title', 'paragraphs'):
            self.assertEqual(after[col], before[col],
                             f"column {col} mutated: {before[col]!r} -> {after[col]!r}")


class TestTakeAfterTerminal(_TakeCase):

    def test_take_after_autopublish_returns_exit_1_with_error(self):
        """Row is in list_pending (snapshot captured before move), but by
        the time cmd_take re-reads via get_pending, the row has been
        auto-published. cmd_take must report the terminal state from
        ``get_published`` and exit 1 without mutating anything."""
        entry = self._insert(link='http://a/1', title='Gone')
        self._mark_notified_ago(entry['link'], 30)

        # Stage RU fields so move_to_published's NOT NULL ru_title is
        # satisfied when we force the move during the test.
        repo.update_staged(entry['link'], 'РУ заголовок', 'РУ лид',
                           ['Абзац'], None)
        repo.mark_telegraph_published(entry['link'],
                                      'https://telegra.ph/g-01-01',
                                      'g-01-01')

        real_list = repo.list_pending()  # capture BEFORE move

        def fake_list_pending():
            return real_list

        def fake_get_pending(link):
            # Race: the row has vanished from pending by the time
            # cmd_take re-reads. Force the move here so
            # ``get_published`` returns a real row in the branch under
            # test.
            if repo.get_published(link) is None:
                repo.move_to_published(link,
                                       'https://telegra.ph/g-01-01',
                                       'g-01-01',
                                       via_review=False)
            return None

        with patch.object(hw_review.repo, 'list_pending',
                          side_effect=fake_list_pending), \
             patch.object(hw_review.repo, 'get_pending',
                          side_effect=fake_get_pending):
            rc = self._run(['take', '1'])

        self.assertEqual(rc, 1)
        self.assertIn('already auto-published', self.stderr.getvalue().lower())
        pub = repo.get_published(entry['link'])
        self.assertIsNotNone(pub)

    def test_take_when_in_failed(self):
        """Row moved to failed_articles → take exits 1 with a clean
        error and no mutations."""
        entry = self._insert(link='http://a/1', title='Fail')
        self._mark_notified_ago(entry['link'], 30)

        real_list = repo.list_pending()

        def fake_list_pending():
            return real_list

        def fake_get_pending(link):
            repo.move_to_failed(link, 'prior failure')
            return None

        with patch.object(hw_review.repo, 'list_pending',
                          side_effect=fake_list_pending), \
             patch.object(hw_review.repo, 'get_pending',
                          side_effect=fake_get_pending):
            rc = self._run(['take', '1'])

        self.assertEqual(rc, 1)
        err = self.stderr.getvalue().lower()
        # Accept either "already auto-published" (generic evicted message)
        # or an explicit "failed" mention — tech-spec only requires clean
        # stderr + exit 1, and the task-file one-liner uses the L67 phrasing.
        self.assertTrue('already auto-published' in err or 'failed' in err,
                        f"unexpected stderr: {err}")

        failed = repo.get_failed(entry['link'])
        self.assertIsNotNone(failed)


class TestTakeOutOfRange(_TakeCase):

    def test_take_out_of_range_index(self):
        self._insert(link='http://a/1', title='A')
        self._insert(link='http://a/2', title='B')
        # queue has 2 rows; take 99 is out of range

        rc = self._run(['take', '99'])
        self.assertEqual(rc, 1)
        err = self.stderr.getvalue()
        self.assertIn('index out of range', err.lower())
        # No Python traceback in stderr.
        self.assertNotIn('Traceback', err)

    def test_take_zero_index(self):
        self._insert(link='http://a/1', title='A')
        rc = self._run(['take', '0'])
        self.assertEqual(rc, 1)
        self.assertIn('index out of range', self.stderr.getvalue().lower())

    def test_take_empty_queue(self):
        rc = self._run(['take', '1'])
        self.assertEqual(rc, 1)
        self.assertIn('index out of range', self.stderr.getvalue().lower())


class TestTakeNotNotified(_TakeCase):
    """Row in pending but ``notified_at IS NULL``: per task-file the
    implementation may either (a) refuse with stderr and exit 1, or
    (b) proceed idempotently with a gentle message. We assert it does
    NOT crash, runs in under the same happy-path pattern, and the row
    remains pending with ``notified_at IS NULL``.
    """

    def test_take_row_never_notified(self):
        entry = self._insert(link='http://a/1', title='Never-notified')
        # no _mark_notified_ago — row's notified_at is NULL

        rc = self._run(['take', '1'])
        # Accept either exit code per task-file flexibility; check
        # DB state + clean stderr.
        self.assertIn(rc, (0, 1))
        self.assertNotIn('Traceback', self.stderr.getvalue())

        after = repo.get_pending(entry['link'])
        self.assertIsNotNone(after)
        self.assertIsNone(after['notified_at'])


if __name__ == '__main__':
    unittest.main()
