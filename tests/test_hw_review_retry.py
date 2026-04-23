#!/usr/bin/env python3
"""Unit + integration tests for ``hw_review retry N`` and the failed-footer
behaviour of ``hw_review list`` (Task 10 of manual-review-workflow).

Subcommand contract (tech-spec §Review+publish, user-spec AC L71–L72):

* ``retry N`` — N is a 1-based index into ``list_failed()`` (ORDER BY
  ``failed_at DESC``, matching the footer's rendering order). Calls
  ``pending_articles_repo.retry_from_failed(link)`` which resets the
  row back to ``pending_articles`` with ``attempt_count=0`` and fresh
  ``fetched_at``. On out-of-range N → stderr ``"out of range"`` + exit 1.

* ``list`` — always appends a ``⚠️`` footer with count + titles +
  retry hint when ``list_failed()`` is non-empty, regardless of whether
  the main queue is empty or not (Decision 8, AC L71).

Footer format (Decision 8, byte-for-byte):
  ``"⚠️ {K} неопубликованных в failed: [title1, title2, ...]. hw_review
  retry N чтобы переподнять."``

Follows the tempfile-DB pattern of ``tests/test_hw_review_take.py`` and
``tests/test_hw_review_cli.py``.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
import hw_review


def _sample_entry(link='http://example.com/a', title='Example',
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


class _RetryCase(unittest.TestCase):
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

    def _insert_pending(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _seed_failed(self, link, title, last_error='boom', failed_at=None):
        """Insert directly into ``failed_articles`` so we don't depend on
        the pending→failed move logic. ``failed_at`` lets tests pin the
        ORDER BY ``failed_at DESC`` ordering deterministically."""
        conn = sqlite3.connect(self.db_path)
        try:
            if failed_at is None:
                conn.execute(
                    "INSERT INTO failed_articles "
                    "(link, title, source_name, paragraphs, images, "
                    " subtitle, last_error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (link, title, 'autoevolution',
                     '["p1.", "p2."]', '[]', 'Lead', last_error),
                )
            else:
                conn.execute(
                    "INSERT INTO failed_articles "
                    "(link, title, source_name, paragraphs, images, "
                    " subtitle, last_error, failed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (link, title, 'autoevolution',
                     '["p1.", "p2."]', '[]', 'Lead', last_error, failed_at),
                )
            conn.commit()
        finally:
            conn.close()


# ============================================================================
# retry happy path
# ============================================================================


class TestRetryHappyPath(_RetryCase):

    def test_retry_moves_row_from_failed_to_pending(self):
        """Two failed rows; ``retry 1`` moves the newest (failed_at DESC)
        back into pending with ``attempt_count=0`` and a fresh
        ``fetched_at``."""
        # Earlier failed_at → lower index position in DESC ordering.
        self._seed_failed('http://f/old', 'Old Failure',
                          failed_at='2026-04-20 10:00:00')
        self._seed_failed('http://f/new', 'Fresh Failure',
                          failed_at='2026-04-22 10:00:00')

        rc = self._run(['retry', '1'])
        self.assertEqual(rc, 0, self.stderr.getvalue())

        # Newest (by failed_at DESC) went back to pending.
        self.assertIsNone(repo.get_failed('http://f/new'))
        row = repo.get_pending('http://f/new')
        self.assertIsNotNone(row)
        self.assertEqual(row['attempt_count'], 0)
        self.assertIsNotNone(row['fetched_at'])
        # ru_* fields clear by default (never existed on failed).
        self.assertIsNone(row['ru_paragraphs'])
        # Unrelated failed row still there.
        self.assertIsNotNone(repo.get_failed('http://f/old'))

    def test_retry_prints_title_on_success(self):
        """Stdout carries a human-friendly confirmation with the title."""
        self._seed_failed('http://f/1', 'Big Title')

        rc = self._run(['retry', '1'])
        self.assertEqual(rc, 0)
        # Task-file says ``"Restored: {title}"``.
        self.assertIn('Big Title', self.stdout.getvalue())

    def test_retry_selects_index_2_correctly(self):
        """``retry 2`` resolves to the second item in ``list_failed()``
        ordering — the one with the OLDER ``failed_at``."""
        self._seed_failed('http://f/older', 'Older',
                          failed_at='2026-04-20 10:00:00')
        self._seed_failed('http://f/newer', 'Newer',
                          failed_at='2026-04-22 10:00:00')

        rc = self._run(['retry', '2'])
        self.assertEqual(rc, 0)

        # Index 2 of DESC ordering = older row.
        self.assertIsNotNone(repo.get_pending('http://f/older'))
        self.assertIsNone(repo.get_failed('http://f/older'))
        # Newer row untouched.
        self.assertIsNotNone(repo.get_failed('http://f/newer'))


# ============================================================================
# retry edge cases
# ============================================================================


class TestRetryOutOfRange(_RetryCase):

    def test_retry_index_above_count(self):
        """N > len(failed) → exit 1, stderr cites out-of-range."""
        self._seed_failed('http://f/1', 'Only one')

        rc = self._run(['retry', '5'])
        self.assertEqual(rc, 1)
        err = self.stderr.getvalue().lower()
        self.assertIn('out of range', err)
        # Row untouched.
        self.assertIsNotNone(repo.get_failed('http://f/1'))

    def test_retry_zero_index(self):
        """N=0 → exit 1, out-of-range."""
        self._seed_failed('http://f/1', 'Only one')

        rc = self._run(['retry', '0'])
        self.assertEqual(rc, 1)
        self.assertIn('out of range', self.stderr.getvalue().lower())

    def test_retry_negative_index(self):
        """N=-1 → exit 1, out-of-range; no mutation."""
        self._seed_failed('http://f/1', 'Only one')

        rc = self._run(['retry', '-1'])
        self.assertEqual(rc, 1)
        self.assertIn('out of range', self.stderr.getvalue().lower())

    def test_retry_empty_failed_queue(self):
        """No failed rows → any N → exit 1."""
        rc = self._run(['retry', '1'])
        self.assertEqual(rc, 1)
        self.assertIn('out of range', self.stderr.getvalue().lower())


class TestRetryNoop(_RetryCase):
    """``retry_from_failed`` has defensive guards: if the link is already
    in pending (e.g. operator-race) it returns False rather than raising.
    CLI should still exit 0 and log — the row's terminal state is
    operator-observable via ``hw_review list``."""

    def test_retry_when_already_in_pending(self):
        """Simulate a race: seed BOTH failed and pending with the same
        link (defensive repo-level guard). ``retry_from_failed`` returns
        False; CLI should not crash."""
        # Pending row exists already.
        self._insert_pending(link='http://f/1', title='Raced')
        # And an orphan failed row sits under the same link.
        self._seed_failed('http://f/1', 'Raced Failure')

        rc = self._run(['retry', '1'])
        # Task-file AC says "exit 0 on success, 1 on out-of-range".
        # The defensive False path is not explicitly covered — accept either
        # exit code as long as stderr is clean (no traceback).
        self.assertIn(rc, (0, 1))
        self.assertNotIn('Traceback', self.stderr.getvalue())


# ============================================================================
# list failed-footer (Decision 8)
# ============================================================================


class TestListFooter(_RetryCase):

    def test_list_no_footer_when_failed_empty(self):
        """No failed rows → no ``⚠️`` in stdout."""
        self._insert_pending(link='http://a/1', title='A')
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        self.assertNotIn('⚠️', self.stdout.getvalue())

    def test_list_footer_when_pending_empty(self):
        """Pending empty + failed non-empty → ``queue is empty`` line AND
        ``⚠️`` footer with correct count + title."""
        self._seed_failed('http://f/1', 'Failed Once')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('queue is empty', out)
        self.assertIn('⚠️', out)
        self.assertIn('1 неопубликованных', out)
        self.assertIn('Failed Once', out)
        self.assertIn('hw_review retry N', out)
        self.assertIn('чтобы переподнять', out)

    def test_list_footer_when_pending_nonempty(self):
        """Both pending and failed populated → numbered pending rows AND
        the footer in the same stdout."""
        self._insert_pending(link='http://a/1', title='Active')
        self._seed_failed('http://f/1', 'Dead Letter')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('1.', out)
        self.assertIn('Active', out)
        self.assertIn('⚠️', out)
        self.assertIn('Dead Letter', out)

    def test_list_footer_count_and_title_order(self):
        """K=3 failed rows → footer cites ``3 неопубликованных`` and lists
        all three titles in the same order as ``list_failed()`` (DESC by
        failed_at) — so operator's ``retry N`` indices match."""
        self._seed_failed('http://f/1', 'Title-One',
                          failed_at='2026-04-20 10:00:00')
        self._seed_failed('http://f/2', 'Title-Two',
                          failed_at='2026-04-21 10:00:00')
        self._seed_failed('http://f/3', 'Title-Three',
                          failed_at='2026-04-22 10:00:00')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('3 неопубликованных', out)
        # DESC ordering → Title-Three appears first, Title-One last.
        idx_three = out.find('Title-Three')
        idx_two = out.find('Title-Two')
        idx_one = out.find('Title-One')
        self.assertTrue(idx_three != -1 and idx_two != -1 and idx_one != -1)
        self.assertLess(idx_three, idx_two)
        self.assertLess(idx_two, idx_one)

    def test_list_footer_format_exact(self):
        """Pin the footer format byte-for-byte (Decision 8)."""
        self._seed_failed('http://f/1', 'Alpha')
        self._seed_failed('http://f/2', 'Beta',
                          failed_at='2026-04-25 10:00:00')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn(
            '⚠️ 2 неопубликованных в failed: [Beta, Alpha]. '
            'hw_review retry N чтобы переподнять.',
            out,
        )


# ============================================================================
# retry → list integration (indices line up)
# ============================================================================


class TestRetryListIntegration(_RetryCase):

    def test_retry_footer_index_matches(self):
        """Operator sees ``⚠️ 2 неопубликованных: [B, A]`` then runs
        ``retry 1`` → picks up 'B' (the DESC-first one), not 'A'."""
        self._seed_failed('http://f/a', 'Alpha',
                          failed_at='2026-04-20 10:00:00')
        self._seed_failed('http://f/b', 'Beta',
                          failed_at='2026-04-22 10:00:00')

        # First: list → capture footer order.
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        # Beta before Alpha.
        self.assertLess(out.find('Beta'), out.find('Alpha'))

        # Fresh buffers for the retry call.
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        rc = self._run(['retry', '1'])
        self.assertEqual(rc, 0, self.stderr.getvalue())

        # Index 1 = Beta (the DESC-first).
        self.assertIsNotNone(repo.get_pending('http://f/b'))
        self.assertIsNone(repo.get_failed('http://f/b'))
        # Alpha untouched.
        self.assertIsNotNone(repo.get_failed('http://f/a'))


if __name__ == '__main__':
    unittest.main()
