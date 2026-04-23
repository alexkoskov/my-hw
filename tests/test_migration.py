#!/usr/bin/env python3
"""Migration tests for ``news_bot.init_db``.

After Task 6 of manual-review-workflow, ``news_bot.init_db`` is expected to
create four tables:

* ``processed_news``    (existing — owned by news_bot's own DDL)
* ``pending_articles``   (delegated to ``pending_articles_repo.init_schema``)
* ``published_articles`` (delegated to ``pending_articles_repo.init_schema``)
* ``failed_articles``    (delegated to ``pending_articles_repo.init_schema``)

These tests use a tempfile SQLite DB (NOT ``:memory:``) because the repo
opens its own short-lived connections via ``news_bot.DB_FILE`` — a
``:memory:`` connection would be unreachable from the repo side.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot


# Expected column schema for ``pending_articles`` (PRAGMA table_info rows are
# `(cid, name, type, notnull, dflt_value, pk)`). Mirrors
# ``tests/test_pending_articles_repo.py::EXPECTED_PENDING`` so a future column
# drift trips both tests.
EXPECTED_PENDING_COLUMNS = {
    'link':          {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 1},
    'source_name':   {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'feed_url':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'title':         {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'subtitle':      {'type': 'TEXT',      'notnull': 1, 'dflt_value': "''",                'pk': 0},
    'paragraphs':    {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'images':        {'type': 'TEXT',      'notnull': 1, 'dflt_value': "'[]'",              'pk': 0},
    'blocks':        {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'ru_title':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'ru_subtitle':   {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'ru_paragraphs': {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'ru_blocks':     {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'telegraph_url': {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'telegraph_path':{'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'preview_html_path': {'type': 'TEXT',  'notnull': 0, 'dflt_value': None,                'pk': 0},
    'fetched_at':    {'type': 'TIMESTAMP', 'notnull': 1, 'dflt_value': 'CURRENT_TIMESTAMP', 'pk': 0},
    'notified_at':   {'type': 'TIMESTAMP', 'notnull': 0, 'dflt_value': None,                'pk': 0},
    'attempt_count': {'type': 'INTEGER',   'notnull': 1, 'dflt_value': '0',                 'pk': 0},
    'last_error':    {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'pub_date':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
}


class TestMigration(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            conn.close()
        return sorted(r[0] for r in rows)

    def _pragma_columns(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        finally:
            conn.close()
        return {
            row[1]: {
                'type': row[2],
                'notnull': row[3],
                'dflt_value': row[4],
                'pk': row[5],
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # AC: init_db creates all four tables
    # ------------------------------------------------------------------

    def test_all_tables_created(self):
        """After ``init_db``, all four expected tables exist."""
        news_bot.init_db()

        tables = self._tables()
        self.assertIn('processed_news', tables)
        self.assertIn('pending_articles', tables)
        self.assertIn('published_articles', tables)
        self.assertIn('failed_articles', tables)

    def test_processed_news_schema_unchanged(self):
        """``processed_news`` DDL is still owned by news_bot (Decision 2 —
        schema unchanged). Column names pinned to guard against drift."""
        news_bot.init_db()

        cols = self._pragma_columns('processed_news')
        self.assertEqual(set(cols), {'link', 'title', 'pub_date', 'processed_at'})
        self.assertEqual(cols['link']['pk'], 1)

    # ------------------------------------------------------------------
    # AC: pending_articles column schema matches tech-spec
    # ------------------------------------------------------------------

    def test_pending_articles_has_expected_columns(self):
        """``pending_articles`` has all 19 tech-spec columns with correct
        types / NOT-NULL / defaults / PK flags."""
        news_bot.init_db()

        actual = self._pragma_columns('pending_articles')
        self.assertEqual(
            set(actual), set(EXPECTED_PENDING_COLUMNS),
            msg="pending_articles column set diverged from tech-spec",
        )
        for col, expected in EXPECTED_PENDING_COLUMNS.items():
            self.assertEqual(
                actual[col], expected,
                msg=f"pending_articles.{col} diverged: {actual[col]} != {expected}",
            )

    # ------------------------------------------------------------------
    # AC: init_db is idempotent
    # ------------------------------------------------------------------

    def test_init_db_idempotent(self):
        """Second call on the same DB raises nothing and does not alter
        row counts in the existing tables."""
        news_bot.init_db()

        # Seed one row per table so row-count drift is observable.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO processed_news (link, title, pub_date) "
                "VALUES ('http://x', 't', '2025-01-01')"
            )
            conn.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs) "
                "VALUES ('http://y', 'mattel', 't', '[]')"
            )
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs) "
                "VALUES ('http://z', 't', 'mattel', '[]')"
            )
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, via_review) "
                "VALUES ('http://w', 't', 'ru', 'https://t.ph/x', 'mattel', 1)"
            )
            conn.commit()
        finally:
            conn.close()

        # Second call must be a no-op.
        try:
            news_bot.init_db()
        except Exception as exc:  # pragma: no cover - would be a regression
            self.fail(f"Second init_db() raised: {exc!r}")

        # Row counts unchanged.
        conn = sqlite3.connect(self.db_path)
        try:
            for table in (
                'processed_news', 'pending_articles',
                'published_articles', 'failed_articles',
            ):
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(
                    count, 1,
                    msg=f"Row count in {table} changed on second init_db()",
                )
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
