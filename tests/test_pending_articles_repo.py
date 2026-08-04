#!/usr/bin/env python3
"""Unit tests for pending_articles_repo DAO.

Covers schema init, CRUD, JSON serialization (ensure_ascii=False),
transactional moves (pending -> published / failed / processed,
failed -> pending), and filter helpers.

Tests follow `tests/test_integration.py` tempfile pattern:
allocate a .db file, monkeypatch `news_bot.DB_FILE` to point at it.
This lets repo functions that open their own connection see the
same DB as test-opened `sqlite3.connect` cursors.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo


# ---- Expected PRAGMA table_info schemas (migration test) -----------

# PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
EXPECTED_PENDING = {
    'link':          {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 1},
    'source_name':   {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                    'pk': 0},
    'feed_url':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'title':         {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                    'pk': 0},
    'subtitle':      {'type': 'TEXT',      'notnull': 1, 'dflt_value': "''",                    'pk': 0},
    'paragraphs':    {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                    'pk': 0},
    'images':        {'type': 'TEXT',      'notnull': 1, 'dflt_value': "'[]'",                  'pk': 0},
    'blocks':        {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'ru_title':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'ru_subtitle':   {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'ru_paragraphs': {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'ru_blocks':     {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'telegraph_url': {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'telegraph_path':{'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'preview_html_path': {'type': 'TEXT',  'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'fetched_at':    {'type': 'TIMESTAMP', 'notnull': 1, 'dflt_value': 'CURRENT_TIMESTAMP',     'pk': 0},
    'notified_at':   {'type': 'TIMESTAMP', 'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'attempt_count': {'type': 'INTEGER',   'notnull': 1, 'dflt_value': '0',                     'pk': 0},
    'last_error':    {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'pub_date':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    # Migration 2026-06-XX (cross-source-dedup, Decision 11).
    'model_fingerprint': {'type': 'TEXT',  'notnull': 0, 'dflt_value': None,                    'pk': 0},
    # Migration 2026-07-25 (content-gate): NULL = publishable, non-NULL =
    # held for operator approval and invisible to list_pending/count_pending.
    'hold_reason':   {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    # dedup defer (2026-07-28) — timed sibling of hold_reason; NULL = publishable now.
    'publish_after': {'type': 'TIMESTAMP', 'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'hold_count':    {'type': 'INTEGER',   'notnull': 0, 'dflt_value': None,                    'pk': 0},
}

EXPECTED_PUBLISHED = {
    'link':           {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 1},
    'title':          {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'ru_title':       {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'telegraph_url':  {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'telegraph_path': {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'source_name':    {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'published_at':   {'type': 'TIMESTAMP', 'notnull': 1, 'dflt_value': 'CURRENT_TIMESTAMP', 'pk': 0},
    'via_review':     {'type': 'INTEGER',   'notnull': 1, 'dflt_value': None,                'pk': 0},
    # Migration 2026-06-XX (cross-source-dedup, Decision 11).
    'model_fingerprint': {'type': 'TEXT',  'notnull': 0, 'dflt_value': None,                'pk': 0},
}

EXPECTED_FAILED = {
    'link':                {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 1},
    'title':               {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'source_name':         {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'paragraphs':          {'type': 'TEXT',      'notnull': 1, 'dflt_value': None,                'pk': 0},
    'images':              {'type': 'TEXT',      'notnull': 1, 'dflt_value': "'[]'",              'pk': 0},
    'blocks':              {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'subtitle':            {'type': 'TEXT',      'notnull': 1, 'dflt_value': "''",                'pk': 0},
    'pub_date':            {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'feed_url':            {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'last_error':          {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'failed_at':           {'type': 'TIMESTAMP', 'notnull': 1, 'dflt_value': 'CURRENT_TIMESTAMP', 'pk': 0},
    'original_fetched_at': {'type': 'TIMESTAMP', 'notnull': 0, 'dflt_value': None,                'pk': 0},
    # Migration 2026-04-30: preserved across the failed/retry boundary so
    # ``retry_from_failed`` doesn't create an orphan Telegraph page.
    'telegraph_url':       {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
    'telegraph_path':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                'pk': 0},
}


def _sample_entry(link='http://example.com/a', source='mattel',
                  paragraphs=None, images=None, blocks=None):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': 'Sample Title',
        'subtitle': '',
        'paragraphs': ['p1', 'p2'] if paragraphs is None else paragraphs,
        'images': [] if images is None else images,
        'blocks': blocks,
        'pub_date': '2026-04-23',
    }


class _TmpDbCase(unittest.TestCase):
    """Base class: sets up tempfile DB + init_schema + patches news_bot.DB_FILE."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db_patcher = patch.object(news_bot, 'DB_FILE', self.db_path)
        self.db_patcher.start()
        # Initialize processed_news (existing table) and the three new tables.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS processed_news '
                '(link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, '
                'processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
            )
            conn.commit()
            repo.init_schema(conn)
        finally:
            conn.close()

    def tearDown(self):
        self.db_patcher.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _conn(self):
        return sqlite3.connect(self.db_path)


# ---------------- Schema / migration tests ----------------

class TestSchema(unittest.TestCase):

    def test_init_schema_creates_three_tables(self):
        conn = sqlite3.connect(':memory:')
        try:
            repo.init_schema(conn)
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name"
            ).fetchall()
            names = {r[0] for r in rows}
            self.assertIn('pending_articles', names)
            self.assertIn('published_articles', names)
            self.assertIn('failed_articles', names)
        finally:
            conn.close()

    def test_init_schema_is_idempotent(self):
        conn = sqlite3.connect(':memory:')
        try:
            repo.init_schema(conn)
            # Seed one row into each table so we can detect a destructive re-init.
            conn.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs) VALUES (?, ?, ?, ?)",
                ('http://x/1', 'mattel', 't', '[]'),
            )
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, via_review) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ('http://x/2', 't', 'т', 'http://tg/', 'mattel', 1),
            )
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs) VALUES (?, ?, ?, ?)",
                ('http://x/3', 't', 'mattel', '[]'),
            )
            conn.commit()

            # Second call must not raise and must not clobber data.
            repo.init_schema(conn)

            for table in ('pending_articles', 'published_articles', 'failed_articles'):
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(n, 1, f"{table} row count changed on re-init")
        finally:
            conn.close()

    def test_pragma_table_info_matches_spec(self):
        conn = sqlite3.connect(':memory:')
        try:
            repo.init_schema(conn)

            for table, expected in (
                ('pending_articles', EXPECTED_PENDING),
                ('published_articles', EXPECTED_PUBLISHED),
                ('failed_articles', EXPECTED_FAILED),
            ):
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                # (cid, name, type, notnull, dflt_value, pk)
                actual = {
                    r[1]: {
                        'type': r[2],
                        'notnull': r[3],
                        'dflt_value': r[4],
                        'pk': r[5],
                    }
                    for r in rows
                }
                self.assertEqual(
                    set(actual.keys()), set(expected.keys()),
                    f"{table} column set mismatch",
                )
                for col, spec in expected.items():
                    self.assertEqual(
                        actual[col], spec,
                        f"{table}.{col} schema mismatch: got {actual[col]}",
                    )
        finally:
            conn.close()


# ---------------- insert / get round-trip tests ----------------

class TestInsertGet(_TmpDbCase):

    def test_insert_pending_serializes_json(self):
        entry = _sample_entry(paragraphs=['a', 'b'], images=['http://i/1'],
                              blocks=[{'type': 'paragraph', 'text': 'x'}])
        self.assertTrue(repo.insert_pending(entry))

        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertEqual(row['paragraphs'], ['a', 'b'])
        self.assertEqual(row['images'], ['http://i/1'])
        self.assertEqual(row['blocks'], [{'type': 'paragraph', 'text': 'x'}])
        # ru_* NULL at insert
        self.assertIsNone(row['ru_title'])
        self.assertIsNone(row['ru_paragraphs'])
        self.assertIsNone(row['ru_blocks'])

    def test_insert_pending_preserves_cyrillic(self):
        entry = _sample_entry()
        entry['title'] = 'Привет'
        entry['subtitle'] = 'Подзаголовок «ёлки»'
        entry['paragraphs'] = ['Привет мир', 'Ещё один абзац']
        self.assertTrue(repo.insert_pending(entry))

        # Read raw TEXT from SQLite — if ensure_ascii=False was NOT used,
        # paragraphs would contain \uXXXX escapes.
        with self._conn() as c:
            raw_title, raw_paragraphs = c.execute(
                "SELECT title, paragraphs FROM pending_articles WHERE link=?",
                (entry['link'],),
            ).fetchone()
        self.assertEqual(raw_title, 'Привет')
        self.assertIn('Привет мир', raw_paragraphs)
        self.assertNotIn('\\u', raw_paragraphs)

    def test_insert_pending_duplicate_returns_false(self):
        entry = _sample_entry()
        self.assertTrue(repo.insert_pending(entry))
        # Second insert with same link: False, no exception propagated.
        self.assertFalse(repo.insert_pending(entry))

    def test_get_pending_returns_none_for_missing(self):
        self.assertIsNone(repo.get_pending('http://nope/'))

    def test_get_published_returns_none_for_missing(self):
        self.assertIsNone(repo.get_published('http://nope/'))

    def test_get_failed_returns_none_for_missing(self):
        self.assertIsNone(repo.get_failed('http://nope/'))

    def test_blocks_empty_list_vs_null_distinguished(self):
        # blocks=None → NULL in DB → None back
        e1 = _sample_entry(link='http://a/1', blocks=None)
        repo.insert_pending(e1)
        # blocks=[] → '[]' JSON → [] back
        e2 = _sample_entry(link='http://a/2', blocks=[])
        repo.insert_pending(e2)

        r1 = repo.get_pending('http://a/1')
        r2 = repo.get_pending('http://a/2')
        self.assertIsNone(r1['blocks'])
        self.assertEqual(r2['blocks'], [])


# ---------------- list / sort / filter tests ----------------

class TestListsAndFilters(_TmpDbCase):

    def _insert_with_ts(self, link, fetched_at, notified_at=None,
                       ru_paragraphs=None):
        """Backdoor: insert row and explicitly overwrite timestamps and ru state."""
        entry = _sample_entry(link=link)
        repo.insert_pending(entry)
        with self._conn() as c:
            c.execute(
                "UPDATE pending_articles "
                "SET fetched_at=?, notified_at=?, ru_paragraphs=? WHERE link=?",
                (fetched_at, notified_at,
                 json.dumps(ru_paragraphs, ensure_ascii=False) if ru_paragraphs is not None else None,
                 link),
            )
            c.commit()

    def test_list_pending_orders_today_first_then_backlog_oldest_first(self):
        """Two-tier ordering: today's fresh batch publishes first
        (in the order it was fetched), then carry-over from earlier
        days drains oldest-first.

        Inserts use SQLite ``datetime('now', ...)`` so timestamps are
        anchored to whatever date the test runs on — same anchor that
        ``list_pending`` uses internally via ``date('now')``.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-2 minutes'))",
                ('http://today/early', 'mattel', 't', '[]'),
            )
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-1 minute'))",
                ('http://today/late', 'mattel', 't', '[]'),
            )
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-1 day'))",
                ('http://yesterday', 'mattel', 't', '[]'),
            )
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-5 days'))",
                ('http://very-old', 'mattel', 't', '[]'),
            )
            c.commit()

        rows = repo.list_pending()
        self.assertEqual(
            [r['link'] for r in rows],
            [
                # Today's fresh batch (oldest-fetched within today first)
                'http://today/early',
                'http://today/late',
                # Backlog drained oldest-first
                'http://very-old',
                'http://yesterday',
            ],
        )

    def test_list_pending_stale_filters(self):
        # Use SQLite datetime() to compute timestamps relative to "now",
        # which matches the repo's own filter logic.
        with self._conn() as c:
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-50 hours'), NULL)",
                ('http://stale/1', 'mattel', 't', '[]'),
            )
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-50 hours'), datetime('now', '-10 hours'))",
                ('http://notified/1', 'mattel', 't', '[]'),
            )
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-1 hour'), NULL)",
                ('http://fresh/1', 'mattel', 't', '[]'),
            )
            c.commit()

        stale = repo.list_pending_stale(48)
        stale_links = {r['link'] for r in stale}
        self.assertIn('http://stale/1', stale_links)
        self.assertNotIn('http://notified/1', stale_links)  # already notified
        self.assertNotIn('http://fresh/1', stale_links)     # too young

    def test_list_notified_overdue_filters(self):
        with self._conn() as c:
            # Overdue & not staged → should match.
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-50 hours'), datetime('now', '-3 hours'))",
                ('http://overdue/1', 'mattel', 't', '[]'),
            )
            # Overdue but staged (ru_paragraphs NOT NULL) → should NOT match.
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at, ru_paragraphs) "
                "VALUES (?, ?, ?, ?, datetime('now', '-50 hours'), datetime('now', '-3 hours'), ?)",
                ('http://staged-overdue/1', 'mattel', 't', '[]', '["абзац"]'),
            )
            # Within grace window → should NOT match.
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, fetched_at, notified_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-50 hours'), datetime('now', '-30 minutes'))",
                ('http://grace/1', 'mattel', 't', '[]'),
            )
            c.commit()

        overdue = repo.list_notified_overdue(2)
        overdue_links = {r['link'] for r in overdue}
        self.assertIn('http://overdue/1', overdue_links)
        self.assertNotIn('http://staged-overdue/1', overdue_links)
        self.assertNotIn('http://grace/1', overdue_links)

    def test_list_pending_for_eviction_excludes_staged(self):
        # Staged rows protected per Decision 7.
        self._insert_with_ts('http://ev/1', '2026-04-01 00:00:00')
        self._insert_with_ts('http://ev/2', '2026-04-02 00:00:00',
                             ru_paragraphs=['абзац'])
        self._insert_with_ts('http://ev/3', '2026-04-03 00:00:00')

        rows = repo.list_pending_for_eviction()
        links = [r['link'] for r in rows]
        self.assertEqual(links, ['http://ev/1', 'http://ev/3'])

    def test_list_failed_orders_by_failed_at_desc(self):
        # Insert three failed rows with explicit failed_at.
        with self._conn() as c:
            c.executemany(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, failed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ('http://f/1', 't', 'mattel', '[]', '2026-04-01 00:00:00'),
                    ('http://f/2', 't', 'mattel', '[]', '2026-04-03 00:00:00'),
                    ('http://f/3', 't', 'mattel', '[]', '2026-04-02 00:00:00'),
                ],
            )
            c.commit()

        rows = repo.list_failed()
        self.assertEqual([r['link'] for r in rows],
                         ['http://f/2', 'http://f/3', 'http://f/1'])

    def test_count_pending(self):
        self.assertEqual(repo.count_pending(), 0)
        repo.insert_pending(_sample_entry(link='http://a/1'))
        repo.insert_pending(_sample_entry(link='http://a/2'))
        self.assertEqual(repo.count_pending(), 2)
        # count_pending changes after a move_to_published. Row must be
        # staged first so `published_articles.ru_title` NOT NULL passes.
        repo.update_staged(
            'http://a/1',
            ru_title='Т',
            ru_subtitle='',
            ru_paragraphs=['абзац'],
            ru_blocks=None,
        )
        repo.move_to_published(
            'http://a/1',
            telegraph_url='http://tg/a1',
            telegraph_path='a1',
            via_review=True,
        )
        self.assertEqual(repo.count_pending(), 1)


# ---------------- update / mutation tests ----------------

class TestMutations(_TmpDbCase):

    def test_update_staged_rejects_if_row_left_pending(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        with self._conn() as c:
            c.execute("DELETE FROM pending_articles WHERE link=?",
                      (entry['link'],))
            c.commit()
        ok = repo.update_staged(
            entry['link'],
            ru_title='т',
            ru_subtitle='',
            ru_paragraphs=['абзац'],
            ru_blocks=None,
        )
        self.assertFalse(ok)

    def test_update_staged_success(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        ok = repo.update_staged(
            entry['link'],
            ru_title='Т',
            ru_subtitle='подзаголовок',
            ru_paragraphs=['абзац один', 'абзац два'],
            ru_blocks=None,
        )
        self.assertTrue(ok)
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['ru_title'], 'Т')
        self.assertEqual(row['ru_subtitle'], 'подзаголовок')
        self.assertEqual(row['ru_paragraphs'], ['абзац один', 'абзац два'])
        self.assertIsNone(row['ru_blocks'])

    def test_mark_notified_and_clear_notified(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        self.assertIsNone(repo.get_pending(entry['link'])['notified_at'])
        repo.mark_notified(entry['link'])
        self.assertIsNotNone(repo.get_pending(entry['link'])['notified_at'])
        repo.clear_notified(entry['link'])
        self.assertIsNone(repo.get_pending(entry['link'])['notified_at'])

    def test_increment_attempt_returns_new_count(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        self.assertEqual(repo.increment_attempt(entry['link'], 'err1'), 1)
        self.assertEqual(repo.increment_attempt(entry['link'], 'err2'), 2)
        self.assertEqual(repo.increment_attempt(entry['link'], 'err3'), 3)

    def test_increment_attempt_stores_last_error(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        repo.increment_attempt(entry['link'], 'boom')
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['last_error'], 'boom')
        self.assertEqual(row['attempt_count'], 1)
        # None overrides too.
        repo.increment_attempt(entry['link'], None)
        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['last_error'])

    def test_mark_telegraph_published_sets_url_and_path(self):
        entry = _sample_entry()
        repo.insert_pending(entry)
        repo.mark_telegraph_published(
            entry['link'],
            telegraph_url='https://telegra.ph/Test-01-01',
            telegraph_path='Test-01-01',
        )
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['telegraph_url'], 'https://telegra.ph/Test-01-01')
        self.assertEqual(row['telegraph_path'], 'Test-01-01')

    def test_set_preview_path_writes_and_clears_column(self):
        entry = _sample_entry()
        repo.insert_pending(entry)

        # Initial value is NULL.
        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['preview_html_path'])

        # Write an absolute path.
        repo.set_preview_path(entry['link'], '/home/vscode/.cache/hw-review/hw-abc.html')
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['preview_html_path'], '/home/vscode/.cache/hw-review/hw-abc.html')

        # Clear it (publish/skip flow will use this).
        repo.set_preview_path(entry['link'], None)
        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['preview_html_path'])

    def test_set_preview_path_on_missing_link_is_noop(self):
        # No row — must not raise.
        repo.set_preview_path('http://no.such/link', '/tmp/whatever.html')


# ---------------- transactional move tests ----------------

class TestMoves(_TmpDbCase):

    def _stage(self, entry):
        repo.update_staged(
            entry['link'],
            ru_title='РуТ',
            ru_subtitle='',
            ru_paragraphs=['абзац'],
            ru_blocks=None,
        )

    def test_move_to_published_atomic(self):
        entry = _sample_entry(link='http://m/1')
        repo.insert_pending(entry)
        self._stage(entry)

        repo.move_to_published(
            entry['link'],
            telegraph_url='https://telegra.ph/M-04-23',
            telegraph_path='M-04-23',
            via_review=True,
        )

        # pending gone
        self.assertIsNone(repo.get_pending(entry['link']))
        # published created
        pub = repo.get_published(entry['link'])
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], 'https://telegra.ph/M-04-23')
        self.assertEqual(pub['telegraph_path'], 'M-04-23')
        self.assertEqual(pub['via_review'], 1)
        self.assertEqual(pub['title'], entry['title'])
        self.assertEqual(pub['ru_title'], 'РуТ')
        self.assertEqual(pub['source_name'], entry['source_name'])
        # processed_news has link
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        self.assertIsNotNone(row)

    def test_move_to_published_via_review_false(self):
        entry = _sample_entry(link='http://m/2')
        repo.insert_pending(entry)
        self._stage(entry)

        repo.move_to_published(
            entry['link'],
            telegraph_url='https://telegra.ph/auto',
            telegraph_path='auto',
            via_review=False,
        )

        pub = repo.get_published(entry['link'])
        self.assertEqual(pub['via_review'], 0)

    def test_move_to_published_missing_row_warns_and_dozapis_published(self):
        """Audit CA-1b: the pending row can vanish between the Telegram
        teaser send and ``move_to_published`` (operator cancel racing an
        in-flight publish — ``skip_pending`` deleted it). The teaser IS in
        the channel at that point, so a silent no-op would leave
        ``published_articles`` without a row for a post that exists —
        skewing the fingerprint window, E017 and E034. Contract: WARNING
        log + defensive dozapis of the published row from the explicit
        args (title recovered from ``processed_news``, where the skip
        stamped it).
        """
        link = 'http://m/ghost'
        entry = _sample_entry(link=link)
        entry['title'] = 'Ghost Title'
        repo.insert_pending(entry)
        # The racing cancel: row leaves pending, title lands in
        # processed_news.
        repo.skip_pending(link)
        self.assertIsNone(repo.get_pending(link))

        with self.assertLogs('pending_articles_repo', level='WARNING') as logs:
            repo.move_to_published(
                link,
                telegraph_url='https://telegra.ph/ghost',
                telegraph_path='ghost',
                via_review=False,
            )

        self.assertTrue(
            any('move_to_published' in line and link in line
                for line in logs.output),
            f"expected a move_to_published WARNING; got {logs.output!r}")
        # The completed publish is NEVER absent from published_articles.
        pub = repo.get_published(link)
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], 'https://telegra.ph/ghost')
        self.assertEqual(pub['telegraph_path'], 'ghost')
        self.assertEqual(pub['via_review'], 0)
        # Title recovered from the processed_news stamp left by the skip.
        self.assertEqual(pub['title'], 'Ghost Title')

    def test_move_to_published_missing_row_no_processed_news_uses_link(self):
        """Dozapis fallback: no pending row AND no processed_news stamp
        (row never existed) — the defensive insert still writes a row,
        using the link for the NOT NULL title columns."""
        link = 'http://m/ghost-bare'
        with self.assertLogs('pending_articles_repo', level='WARNING'):
            repo.move_to_published(
                link,
                telegraph_url='https://telegra.ph/ghost-bare',
                telegraph_path='ghost-bare',
                via_review=True,
            )
        pub = repo.get_published(link)
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], 'https://telegra.ph/ghost-bare')
        self.assertEqual(pub['title'], link)
        self.assertEqual(pub['via_review'], 1)

    def test_move_to_published_idempotent_on_duplicate_link(self):
        # Contract (Task 2): a second move_to_published with the same link
        # must NOT raise IntegrityError on the published_articles UNIQUE/PK,
        # and must preserve the FIRST publication's values
        # (INSERT OR IGNORE — not INSERT OR REPLACE).
        link = 'http://m/idem'

        # Setup A: first pending row + first publish (via_review=False).
        entry = _sample_entry(link=link)
        repo.insert_pending(entry)
        self._stage(entry)

        repo.move_to_published(
            link,
            telegraph_url='https://telegra.ph/first',
            telegraph_path='first',
            via_review=False,
        )

        # Pre-conditions for the second call:
        self.assertIsNone(repo.get_pending(link))
        first_pub = repo.get_published(link)
        self.assertIsNotNone(first_pub)
        self.assertEqual(first_pub['telegraph_url'], 'https://telegra.ph/first')
        self.assertEqual(first_pub['telegraph_path'], 'first')
        self.assertEqual(first_pub['via_review'], 0)

        # Re-stage: pending PK on `link` was deleted by the first call.
        # Insert a fresh pending row with the SAME link via raw SQL.
        # ru_title must be a non-empty string — published_articles.ru_title
        # is NOT NULL, and step 0 of move_to_published copies it from pending.
        with self._conn() as c:
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, ru_title, paragraphs) "
                "VALUES (?, ?, ?, ?, ?)",
                (link, 'mattel', 'title2', 'РуТ2', '[]'),
            )
            c.commit()

        # Second call: must NOT raise IntegrityError thanks to INSERT OR IGNORE.
        repo.move_to_published(
            link,
            telegraph_url='https://telegra.ph/second',
            telegraph_path='second',
            via_review=True,
        )

        # Exactly one published row for this link.
        with self._conn() as c:
            n_pub = c.execute(
                "SELECT COUNT(*) FROM published_articles WHERE link=?",
                (link,),
            ).fetchone()[0]
        self.assertEqual(n_pub, 1)

        # Values are still those of the FIRST publication (litmus for
        # accidental INSERT OR REPLACE — that would have flipped these to
        # 'second'/'second'/1).
        pub = repo.get_published(link)
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], 'https://telegra.ph/first')
        self.assertEqual(pub['telegraph_path'], 'first')
        self.assertEqual(pub['via_review'], 0)

        # Pending was cleaned up by step 3 of the second call.
        self.assertIsNone(repo.get_pending(link))
        with self._conn() as c:
            n_pending = c.execute(
                "SELECT COUNT(*) FROM pending_articles"
            ).fetchone()[0]
        self.assertEqual(n_pending, 0)

    def test_move_to_published_rollback_on_error(self):
        entry = _sample_entry(link='http://m/rollback')
        repo.insert_pending(entry)
        self._stage(entry)

        # Inject a fault: the second INSERT inside move_to_published
        # (INSERT OR IGNORE INTO processed_news) must raise mid-transaction.
        # We wrap sqlite3.connect so every execute() call is counted on the
        # wrapping connection; the N-th one raises. The repo uses
        # conn.execute() exclusively, so a single counter on the conn covers
        # both SELECT and INSERT paths.
        real_connect = sqlite3.connect

        class WrappingConn:
            def __init__(self, inner):
                self._inner = inner
                self._n = 0

            def execute(self, sql, params=()):
                self._n += 1
                # Inside move_to_published the execute sequence is:
                #   1) SELECT title, ru_title, ...
                #   2) INSERT INTO published_articles ...
                #   3) INSERT OR IGNORE INTO processed_news ...
                #   4) DELETE FROM pending_articles ...
                # We raise at call 3 to land "inside the transaction" —
                # after published_articles has been written but before
                # processed_news/DELETE — so rollback must revert all.
                if self._n == 3:
                    raise sqlite3.OperationalError(
                        'simulated failure on 3rd exec'
                    )
                return self._inner.execute(sql, params)

            def commit(self):
                return self._inner.commit()

            def rollback(self):
                return self._inner.rollback()

            def close(self):
                return self._inner.close()

            def __getattr__(self, item):
                return getattr(self._inner, item)

        def fake_connect(path, *a, **kw):
            return WrappingConn(real_connect(path, *a, **kw))

        with patch.object(repo.sqlite3, 'connect', side_effect=fake_connect):
            with self.assertRaises(sqlite3.OperationalError):
                repo.move_to_published(
                    entry['link'],
                    telegraph_url='https://telegra.ph/r',
                    telegraph_path='r',
                    via_review=True,
                )

        # pending row still present; published/processed_news empty for this link.
        self.assertIsNotNone(repo.get_pending(entry['link']))
        self.assertIsNone(repo.get_published(entry['link']))
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        self.assertIsNone(row)

    def test_move_to_failed_copies_en_fields(self):
        entry = _sample_entry(
            link='http://f/copy',
            paragraphs=['EN p1', 'EN p2'],
            images=['http://i/1'],
            blocks=[{'type': 'paragraph', 'text': 'x'}],
        )
        entry['subtitle'] = 'sub en'
        entry['feed_url'] = 'http://feed/'
        repo.insert_pending(entry)

        # Read original fetched_at for forensic comparison.
        with self._conn() as c:
            original_fetched_at = c.execute(
                "SELECT fetched_at FROM pending_articles WHERE link=?",
                (entry['link'],),
            ).fetchone()[0]

        repo.move_to_failed(entry['link'], last_error='boom')

        self.assertIsNone(repo.get_pending(entry['link']))
        failed = repo.get_failed(entry['link'])
        self.assertIsNotNone(failed)
        self.assertEqual(failed['title'], entry['title'])
        self.assertEqual(failed['source_name'], entry['source_name'])
        self.assertEqual(failed['paragraphs'], ['EN p1', 'EN p2'])
        self.assertEqual(failed['images'], ['http://i/1'])
        self.assertEqual(failed['blocks'], [{'type': 'paragraph', 'text': 'x'}])
        self.assertEqual(failed['subtitle'], 'sub en')
        self.assertEqual(failed['feed_url'], 'http://feed/')
        self.assertEqual(failed['pub_date'], entry['pub_date'])
        self.assertEqual(failed['last_error'], 'boom')
        self.assertEqual(failed['original_fetched_at'], original_fetched_at)

    def test_skip_pending_writes_processed_not_published(self):
        entry = _sample_entry(link='http://skip/1')
        repo.insert_pending(entry)
        self._stage(entry)

        repo.skip_pending(entry['link'])

        self.assertIsNone(repo.get_pending(entry['link']))
        self.assertIsNone(repo.get_published(entry['link']))
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        self.assertIsNotNone(row)

    def test_retry_from_failed_resets_attempt_count_and_fetched_at(self):
        # Build a row directly in failed_articles.
        with self._conn() as c:
            c.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, images, blocks, subtitle, "
                " pub_date, feed_url, last_error, failed_at, original_fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    'http://r/1', 't', 'mattel',
                    json.dumps(['p1'], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    None, '', '2026-01-01', None, 'boom',
                    '2026-01-01 00:00:00', '2025-12-30 00:00:00',
                ),
            )
            c.commit()

        ok = repo.retry_from_failed('http://r/1')
        self.assertTrue(ok)

        # Failed row removed.
        self.assertIsNone(repo.get_failed('http://r/1'))
        # Pending row created with reset counters + fresh fetched_at + ru_* NULL.
        row = repo.get_pending('http://r/1')
        self.assertIsNotNone(row)
        self.assertEqual(row['attempt_count'], 0)
        self.assertIsNone(row['ru_title'])
        self.assertIsNone(row['ru_paragraphs'])
        self.assertIsNone(row['ru_blocks'])
        self.assertIsNone(row['notified_at'])
        self.assertIsNone(row['telegraph_url'])
        self.assertIsNone(row['telegraph_path'])
        self.assertIsNone(row['last_error'])
        # fetched_at must differ from original 2025-12-30 timestamp.
        self.assertNotEqual(row['fetched_at'], '2025-12-30 00:00:00')

    def test_retry_from_failed_preserves_telegraph_url_and_path(self):
        """When a Telegraph page was already published before the row
        flunked into failed (e.g. the article translated and uploaded
        but the Telegram-teaser step failed), ``retry_from_failed``
        must restore ``telegraph_url`` / ``telegraph_path`` so the
        retry re-uses the existing Telegraph page (Decision 9
        idempotency) rather than creating an orphan duplicate."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, images, blocks, "
                " subtitle, pub_date, feed_url, last_error, failed_at, "
                " original_fetched_at, telegraph_url, telegraph_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    'http://r/preserve', 't', 'mattel',
                    json.dumps(['p1'], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    None, '', '2026-01-01', None,
                    'telegram_send failed', '2026-01-01 00:00:00',
                    '2025-12-30 00:00:00',
                    'https://telegra.ph/Saved-04-29',
                    'Saved-04-29',
                ),
            )
            c.commit()

        ok = repo.retry_from_failed('http://r/preserve')
        self.assertTrue(ok)

        row = repo.get_pending('http://r/preserve')
        self.assertIsNotNone(row)
        self.assertEqual(row['telegraph_url'], 'https://telegra.ph/Saved-04-29')
        self.assertEqual(row['telegraph_path'], 'Saved-04-29')
        # attempt_count still resets — only Telegraph fields are carried over.
        self.assertEqual(row['attempt_count'], 0)

    def test_move_to_failed_carries_telegraph_url_when_set(self):
        """A pending row that already has ``telegraph_url`` populated
        (Telegraph published, downstream step crashed) carries the URL
        into ``failed_articles`` so a later ``retry_from_failed``
        round-trip can preserve it."""
        repo.insert_pending(_sample_entry(link='http://m/preserve'))
        repo.mark_telegraph_published(
            'http://m/preserve',
            'https://telegra.ph/Mid-04-30',
            'Mid-04-30',
        )
        repo.move_to_failed('http://m/preserve', 'simulated late failure')

        failed = repo.get_failed('http://m/preserve')
        self.assertIsNotNone(failed)
        self.assertEqual(failed['telegraph_url'], 'https://telegra.ph/Mid-04-30')
        self.assertEqual(failed['telegraph_path'], 'Mid-04-30')

    def test_retry_from_failed_returns_false_when_link_already_pending(self):
        # Defensive: a link present in BOTH pending and failed is anomalous,
        # but retry_from_failed must not blow up or drop the failed row.
        entry = _sample_entry(link='http://r/collide')
        repo.insert_pending(entry)
        with self._conn() as c:
            c.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs) VALUES (?, ?, ?, ?)",
                ('http://r/collide', 't', 'mattel', '[]'),
            )
            c.commit()

        ok = repo.retry_from_failed('http://r/collide')
        self.assertFalse(ok)
        # Failed row still present — not silently dropped.
        self.assertIsNotNone(repo.get_failed('http://r/collide'))

    def test_retry_from_failed_missing_link_returns_false(self):
        self.assertFalse(repo.retry_from_failed('http://does/not/exist'))


# ---------------- Cross-source dedup: schema-pin sanity ----------------

class TestCrossSourceDedupSchemaPin(unittest.TestCase):
    """Pin the dedup-related schema additions on both tables (Decision 11)."""

    def test_expected_pending_includes_model_fingerprint(self):
        self.assertIn('model_fingerprint', EXPECTED_PENDING)
        self.assertEqual(
            EXPECTED_PENDING['model_fingerprint'],
            {'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 0},
        )

    def test_expected_published_includes_model_fingerprint(self):
        self.assertIn('model_fingerprint', EXPECTED_PUBLISHED)
        self.assertEqual(
            EXPECTED_PUBLISHED['model_fingerprint'],
            {'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 0},
        )


# ---------------- Cross-source dedup: insert_pending fingerprint ----------------

class TestInsertPendingFingerprint(_TmpDbCase):
    """Cover the ``insert_pending`` extension (entry['model_fingerprint'])
    introduced for cross-source-dedup (Decisions 5, 11).
    """

    def test_insert_pending_with_fingerprint_roundtrip(self):
        """A dict fingerprint round-trips through insert/get as a dict —
        pins ``_PENDING_JSON_COLS`` registration against typos."""
        fp = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        entry = _sample_entry(link='http://fp/round')
        entry['model_fingerprint'] = fp
        self.assertTrue(repo.insert_pending(entry))

        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertEqual(row['model_fingerprint'], fp)
        # CRITICAL: must be a dict not a str — that's what guards
        # _PENDING_JSON_COLS membership.
        self.assertIsInstance(row['model_fingerprint'], dict)

    def test_pending_json_cols_registers_model_fingerprint(self):
        """Direct low-cost guard (Task 8 audit M2): the literal string
        ``'model_fingerprint'`` must be an element of the ``_PENDING_JSON_COLS``
        tuple. The roundtrip test above pins behavior, but this catches a
        deleted tuple element even if that test is ever mocked/skipped."""
        self.assertIn('model_fingerprint', repo._PENDING_JSON_COLS)

    def test_insert_pending_with_empty_fingerprint_roundtrip(self):
        """The computed-empty fingerprint ``{strict:[], brands:[]}`` is
        distinct from NULL (Decision 5) — must round-trip as the empty
        dict-shape, NOT as None."""
        fp = {'strict': [], 'brands': []}
        entry = _sample_entry(link='http://fp/empty')
        entry['model_fingerprint'] = fp
        self.assertTrue(repo.insert_pending(entry))

        row = repo.get_pending(entry['link'])
        self.assertEqual(row['model_fingerprint'], fp)
        # Distinct from NULL — must not be None.
        self.assertIsNotNone(row['model_fingerprint'])

    def test_insert_pending_without_fingerprint_backward_compat(self):
        """Existing callers that don't set ``model_fingerprint`` in entry —
        ``insert_pending`` must store NULL and not raise."""
        entry = _sample_entry(link='http://fp/none')
        # No 'model_fingerprint' key at all.
        self.assertNotIn('model_fingerprint', entry)
        self.assertTrue(repo.insert_pending(entry))

        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['model_fingerprint'])


# ---------------- Cross-source dedup: recent fingerprint queries ----------------

class TestListRecentFingerprints(_TmpDbCase):
    """Cover ``list_recent_pending_fingerprints`` /
    ``list_recent_published_fingerprints`` — window filtering + JSON deserialisation.
    """

    def test_list_recent_pending_fingerprints_window(self):
        """Seed 4 rows: 2 inside the 7-day window, 2 older. Only the
        in-window rows are returned, with model_fingerprint deserialised
        to dict."""
        # Two inside-window rows, two outside-window (>7 days old).
        fp_a = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        fp_b = {'strict': ['subaru legacy gt'], 'brands': ['subaru']}
        fp_c = {'strict': ['ford mustang'], 'brands': ['ford']}
        fp_d = {'strict': ['honda civic'], 'brands': ['honda']}

        for link, fp in (
            ('http://fp/in1', fp_a),
            ('http://fp/in2', fp_b),
            ('http://fp/old1', fp_c),
            ('http://fp/old2', fp_d),
        ):
            entry = _sample_entry(link=link)
            entry['model_fingerprint'] = fp
            repo.insert_pending(entry)

        # Push old rows out of the 7-day window via direct UPDATE.
        with self._conn() as c:
            c.execute(
                "UPDATE pending_articles SET fetched_at=datetime('now', '-10 days') "
                "WHERE link IN (?, ?)",
                ('http://fp/old1', 'http://fp/old2'),
            )
            c.commit()

        with self._conn() as c:
            rows = repo.list_recent_pending_fingerprints(c, days=7)

        links = {r['link'] for r in rows}
        self.assertEqual(links, {'http://fp/in1', 'http://fp/in2'})
        # JSON deserialised to dict.
        for r in rows:
            self.assertIsInstance(r['model_fingerprint'], dict)
            self.assertIn('strict', r['model_fingerprint'])
            self.assertIn('brands', r['model_fingerprint'])

    def test_list_recent_published_fingerprints_window(self):
        """Same window filter applied to ``published_articles`` via
        ``published_at``."""
        fp_in = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        fp_out = {'strict': ['ford mustang'], 'brands': ['ford']}

        # Insert directly into published_articles to bypass the
        # pending → published transition for this targeted unit test.
        with self._conn() as c:
            c.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, model_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ('http://pub/in', 't', 'т', 'http://tg/in', 'mattel', 1,
                 json.dumps(fp_in, ensure_ascii=False)),
            )
            c.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, model_fingerprint, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-10 days'))",
                ('http://pub/old', 't', 'т', 'http://tg/old', 'mattel', 1,
                 json.dumps(fp_out, ensure_ascii=False)),
            )
            c.commit()

        with self._conn() as c:
            rows = repo.list_recent_published_fingerprints(c, days=7)

        links = {r['link'] for r in rows}
        self.assertEqual(links, {'http://pub/in'})
        self.assertEqual(rows[0]['model_fingerprint'], fp_in)
        self.assertIsInstance(rows[0]['model_fingerprint'], dict)

    def test_list_recent_pending_fingerprints_custom_days(self):
        """The ``days`` parameter is honoured — narrower window excludes more rows."""
        fp = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        for link, days_old in (
            ('http://w/today', 0),
            ('http://w/2d', 2),
            ('http://w/5d', 5),
        ):
            entry = _sample_entry(link=link)
            entry['model_fingerprint'] = fp
            repo.insert_pending(entry)
            with self._conn() as c:
                c.execute(
                    "UPDATE pending_articles SET fetched_at=datetime('now', ?) "
                    "WHERE link=?",
                    (f"-{days_old} days", link),
                )
                c.commit()

        with self._conn() as c:
            wide = repo.list_recent_pending_fingerprints(c, days=7)
            narrow = repo.list_recent_pending_fingerprints(c, days=3)
        self.assertEqual({r['link'] for r in wide},
                         {'http://w/today', 'http://w/2d', 'http://w/5d'})
        self.assertEqual({r['link'] for r in narrow},
                         {'http://w/today', 'http://w/2d'})


# ---------------- Cross-source dedup: update_published_fingerprint ----------------

class TestUpdatePublishedFingerprint(_TmpDbCase):
    """Cover ``update_published_fingerprint`` (backfill script write path)."""

    def _insert_pub(self, link, fingerprint=None):
        fp_text = json.dumps(fingerprint, ensure_ascii=False) if fingerprint is not None else None
        with self._conn() as c:
            c.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, source_name, "
                " via_review, model_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link, 't', 'т', 'http://tg/' + link[-3:], 'mattel', 1, fp_text),
            )
            c.commit()

    def test_update_published_fingerprint_writes_dict(self):
        """Writing a dict fingerprint persists as JSON; read-back returns dict."""
        self._insert_pub('http://upf/1', fingerprint=None)
        fp = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        with self._conn() as c:
            repo.update_published_fingerprint(c, 'http://upf/1', fp)
            c.commit()

        with self._conn() as c:
            row = c.execute(
                "SELECT model_fingerprint FROM published_articles WHERE link=?",
                ('http://upf/1',),
            ).fetchone()
        # Raw stored value is JSON-encoded TEXT.
        self.assertIsNotNone(row[0])
        self.assertEqual(json.loads(row[0]), fp)

    def test_update_published_fingerprint_overwrites_existing(self):
        """A previously-stored fingerprint is overwritten on re-write."""
        old_fp = {'strict': ['ford mustang'], 'brands': ['ford']}
        new_fp = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        self._insert_pub('http://upf/2', fingerprint=old_fp)
        with self._conn() as c:
            repo.update_published_fingerprint(c, 'http://upf/2', new_fp)
            c.commit()
        with self._conn() as c:
            raw = c.execute(
                "SELECT model_fingerprint FROM published_articles WHERE link=?",
                ('http://upf/2',),
            ).fetchone()[0]
        self.assertEqual(json.loads(raw), new_fp)


# ---------------- Cross-source dedup: rate-limit helpers ----------------

class TestPairRateLimit(_TmpDbCase):
    """Cover ``is_pair_rate_limited`` / ``mark_pair_pinged`` (Decision 6,
    soft-flag per-pair rate-limit).
    """

    def test_pair_rate_limit_unset_returns_false(self):
        """No prior ``mark_pair_pinged`` → not rate-limited."""
        with self._conn() as c:
            self.assertFalse(
                repo.is_pair_rate_limited(c, 'http://a/new', 'http://a/old')
            )

    def test_pair_rate_limit_within_window_true(self):
        """mark → check immediately → True."""
        with self._conn() as c:
            repo.mark_pair_pinged(c, 'http://b/new', 'http://b/old')
            c.commit()
        with self._conn() as c:
            self.assertTrue(
                repo.is_pair_rate_limited(c, 'http://b/new', 'http://b/old',
                                          window_days=7)
            )

    def test_pair_rate_limit_after_window_false(self):
        """mark → fast-forward >7d via direct bot_state UPDATE → False."""
        with self._conn() as c:
            repo.mark_pair_pinged(c, 'http://c/new', 'http://c/old')
            c.commit()
        # Backdate the value to >7 days ago — direct UPDATE on bot_state row
        # is shorter than mocking ``datetime.now`` and follows the
        # outage_state test pattern.
        old_iso = '2020-01-01T00:00:00+00:00'
        with self._conn() as c:
            c.execute(
                "UPDATE bot_state SET value=? WHERE key=?",
                (old_iso,
                 'softflag_pair:http://c/new\nhttp://c/old'),
            )
            c.commit()
        with self._conn() as c:
            self.assertFalse(
                repo.is_pair_rate_limited(c, 'http://c/new', 'http://c/old',
                                          window_days=7)
            )

    def test_pair_rate_limit_independent_pairs(self):
        """mark(A,B) does NOT rate-limit check(C,D) — pair keys are
        independent."""
        with self._conn() as c:
            repo.mark_pair_pinged(c, 'http://A/new', 'http://A/old')
            c.commit()
        with self._conn() as c:
            self.assertFalse(
                repo.is_pair_rate_limited(c, 'http://C/new', 'http://D/old')
            )

    def test_pair_rate_limit_corrupted_timestamp_does_not_block(self):
        """If ``bot_state`` has a malformed value at the pair key (manual
        edit, format drift), ``is_pair_rate_limited`` returns False + logs
        a warning — must NOT raise (parity with outage_state._parse_dt)."""
        key = 'softflag_pair:http://d/new\nhttp://d/old'
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                (key, 'not-a-timestamp'),
            )
            c.commit()
        with self._conn() as c:
            # Must NOT raise; treat corrupted as "no last ping" → False.
            self.assertFalse(
                repo.is_pair_rate_limited(c, 'http://d/new', 'http://d/old')
            )


class TestDedupDegradedRateLimit(_TmpDbCase):
    """Cover ``is_dedup_degraded_rate_limited`` / ``mark_dedup_degraded_pinged``
    (Decision 6, global 1-hour window)."""

    def test_degraded_rate_limit_unset_returns_false(self):
        with self._conn() as c:
            self.assertFalse(repo.is_dedup_degraded_rate_limited(c))

    def test_degraded_rate_limit_within_hour_true(self):
        with self._conn() as c:
            repo.mark_dedup_degraded_pinged(c)
            c.commit()
        with self._conn() as c:
            self.assertTrue(
                repo.is_dedup_degraded_rate_limited(c, window_hours=1)
            )

    def test_degraded_rate_limit_after_hour_false(self):
        with self._conn() as c:
            repo.mark_dedup_degraded_pinged(c)
            c.commit()
        # Backdate the row to >1h ago — direct UPDATE for clarity.
        old_iso = '2020-01-01T00:00:00+00:00'
        with self._conn() as c:
            c.execute(
                "UPDATE bot_state SET value=? WHERE key=?",
                (old_iso, 'dedup_degraded_last_pinged_at'),
            )
            c.commit()
        with self._conn() as c:
            self.assertFalse(
                repo.is_dedup_degraded_rate_limited(c, window_hours=1)
            )

    def test_degraded_rate_limit_corrupted_timestamp_does_not_block(self):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('dedup_degraded_last_pinged_at', 'corrupted'),
            )
            c.commit()
        with self._conn() as c:
            self.assertFalse(repo.is_dedup_degraded_rate_limited(c))


# ---------------- Review-token store (dedup-review-buttons Task 1) ----------


class TestReviewTokenStore(_TmpDbCase):
    """Cover ``put_review_token`` / ``get_review_token_link`` /
    ``delete_review_token`` — the token→link mapping behind the [E014]
    review buttons (callback_data is capped at 64 bytes, so a short token
    stands in for the full article URL). Backed by the existing
    ``bot_state`` table under the ``review_token:`` key prefix — no new
    table / migration.
    """

    def test_put_get_roundtrip(self):
        repo.put_review_token('tok', 'http://x/a')
        self.assertEqual(repo.get_review_token_link('tok'), 'http://x/a')

    def test_get_unknown_token_returns_none(self):
        self.assertIsNone(repo.get_review_token_link('never-stored'))

    def test_delete_removes_token(self):
        repo.put_review_token('tok', 'http://x/a')
        repo.delete_review_token('tok')
        self.assertIsNone(repo.get_review_token_link('tok'))

    def test_delete_unknown_token_is_noop(self):
        # Must not raise — idempotent delete (double-click on a button,
        # bot restart between put and delete, etc.).
        repo.delete_review_token('nope')

    def test_put_overwrites_existing(self):
        repo.put_review_token('tok', 'http://x/first')
        repo.put_review_token('tok', 'http://x/second')
        self.assertEqual(repo.get_review_token_link('tok'), 'http://x/second')

    def test_token_stored_under_prefixed_key(self):
        repo.put_review_token('tok', 'http://x/a')
        with self._conn() as c:
            prefixed = c.execute(
                "SELECT value FROM bot_state WHERE key=?",
                ('review_token:tok',),
            ).fetchone()
            bare = c.execute(
                "SELECT value FROM bot_state WHERE key=?",
                ('tok',),
            ).fetchone()
        self.assertIsNotNone(prefixed)
        # Value carries the kind (SEC-CG-2), key layout unchanged.
        self.assertEqual(prefixed[0], 'dedup|http://x/a')
        self.assertIsNone(bare)


class TestReviewTokenKinds(_TmpDbCase):
    """Audit SEC-CG-2 — tokens record WHICH keyboard minted them.

    The store is one flat namespace and the listener dispatches by action
    word, so without a kind a token from one keyboard can be redeemed by
    the other resolver. Encoded in the VALUE (``<kind>|<link>``) rather
    than the key, so no migration and no janitor are needed: values
    written before this change have no prefix and read back as ``dedup``,
    which is what they are — the hold keyboard did not exist then.
    """

    def test_kind_roundtrips(self):
        repo.put_review_token('t1', 'http://x/a',
                              kind=repo.REVIEW_TOKEN_KIND_HOLD)
        self.assertEqual(repo.get_review_token('t1'),
                         (repo.REVIEW_TOKEN_KIND_HOLD, 'http://x/a'))

    def test_default_kind_is_dedup(self):
        """Back-compat for every existing call site."""
        repo.put_review_token('t2', 'http://x/b')
        self.assertEqual(repo.get_review_token('t2'),
                         (repo.REVIEW_TOKEN_KIND_DEDUP, 'http://x/b'))

    def test_legacy_bare_link_value_reads_as_dedup(self):
        """A token minted before this change (raw link, no prefix) must
        still resolve sanely rather than becoming an unusable button."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?)",
                ('review_token:legacy', 'http://x/legacy'),
            )
            c.commit()
        self.assertEqual(repo.get_review_token('legacy'),
                         (repo.REVIEW_TOKEN_KIND_DEDUP, 'http://x/legacy'))
        self.assertEqual(repo.get_review_token_link('legacy'),
                         'http://x/legacy')

    def test_a_link_containing_the_separator_is_not_mangled(self):
        """Split on the FIRST separator and only for a KNOWN kind, so a
        URL with a pipe in the query string round-trips intact."""
        link = 'http://x/a?q=1|2|3'
        repo.put_review_token('t3', link, kind=repo.REVIEW_TOKEN_KIND_HOLD)
        self.assertEqual(repo.get_review_token('t3'),
                         (repo.REVIEW_TOKEN_KIND_HOLD, link))

    def test_unknown_prefix_is_treated_as_a_legacy_link(self):
        """A value whose prefix is not a known kind is a link, not a
        kind — never silently reinterpreted."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?)",
                ('review_token:weird', 'ftp|http://x/c'),
            )
            c.commit()
        self.assertEqual(repo.get_review_token('weird'),
                         (repo.REVIEW_TOKEN_KIND_DEDUP, 'ftp|http://x/c'))

    def test_unknown_token_returns_none(self):
        self.assertIsNone(repo.get_review_token('never-stored'))

    def test_put_rejects_an_unknown_kind(self):
        """Fail loudly at the mint site rather than writing a token no
        resolver will ever accept."""
        with self.assertRaises(ValueError):
            repo.put_review_token('t4', 'http://x/d', kind='banana')
        self.assertIsNone(repo.get_review_token('t4'))

    def test_get_review_token_link_is_kind_agnostic(self):
        """The listener's decision log records what was acted on, whatever
        keyboard it came from."""
        repo.put_review_token('t5', 'http://x/e',
                              kind=repo.REVIEW_TOKEN_KIND_HOLD)
        self.assertEqual(repo.get_review_token_link('t5'), 'http://x/e')

    def test_kinds_are_distinct(self):
        self.assertNotEqual(repo.REVIEW_TOKEN_KIND_DEDUP,
                            repo.REVIEW_TOKEN_KIND_HOLD)


class TestConnectBusyTimeout(_TmpDbCase):
    """Pin the busy-timeout contract on ``repo._connect()`` (5000 ms).

    The feature introduces a second concurrent writer (the callback
    listener thread calling ``skip_pending`` while the publish loop holds
    a write lock), so ``_connect()`` must wait out short lock windows
    instead of failing with ``database is locked``. Set via the
    ``timeout=5.0`` parameter of ``sqlite3.connect`` — deliberately NOT
    via a ``PRAGMA busy_timeout`` execute() inside ``_connect()``, which
    would shift the execute() counter in the fault-injection test
    ``test_move_to_published_rollback_on_error``.
    """

    def test_connect_sets_busy_timeout(self):
        conn = repo._connect()
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 5000)

    def test_connect_passes_explicit_timeout_parameter(self):
        """Call-spy regression guard (test-review round 1, HIGH).

        The PRAGMA check above also passes on a bare ``sqlite3.connect``
        because the stdlib default ``timeout`` is already 5.0 — so it pins
        the *behavior* but cannot catch someone reverting the explicit
        parameter. This spy asserts the code line itself: ``_connect()``
        must invoke ``sqlite3.connect(<DB_FILE>, timeout=5.0)`` explicitly,
        keeping the 5 s contract in our code rather than in a stdlib
        default that could drift.
        """
        calls = []
        real_connect = sqlite3.connect

        def spy_connect(path, *args, **kwargs):
            calls.append({'path': path, 'args': args, 'kwargs': kwargs})
            return real_connect(path, *args, **kwargs)

        with patch.object(repo.sqlite3, 'connect', side_effect=spy_connect):
            conn = repo._connect()
            conn.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['path'], self.db_path)
        self.assertEqual(calls[0]['kwargs'].get('timeout'), 5.0)


class TestConcurrentWriters(_TmpDbCase):
    """Carry-forward from tech-spec (Integration tests / Risks «database is
    locked»): publish-loop writer and cancel writer hit the same DB file at
    the same time — under busy_timeout the second writer WAITS for the lock
    instead of failing. The negative counterpart (what the failure mode
    looks like WITHOUT a busy-timeout) is
    ``test_zero_timeout_control_raises_database_locked``.
    """

    def test_two_writers_no_database_locked(self):
        entry = _sample_entry(link='http://c/concurrent')
        repo.insert_pending(entry)

        errors = []
        write_reached = threading.Event()
        real_connect = sqlite3.connect

        class SignallingConn:
            """Delegates to the real connection; sets ``write_reached``
            immediately before the first WRITE statement of skip_pending
            (its opening SELECT is lock-free under RESERVED). The main
            thread holds the lock until this event fires, so the cancel
            writer provably reaches its blocking write WHILE the lock is
            held — deterministic contention, no sleep-based timing
            (test-review round 1, MEDIUM)."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=()):
                if 'INSERT OR IGNORE INTO processed_news' in sql:
                    write_reached.set()
                return self._inner.execute(sql, params)

            def __getattr__(self, item):
                return getattr(self._inner, item)

        def signalling_connect(path, *a, **kw):
            return SignallingConn(real_connect(path, *a, **kw))

        def cancel_writer():
            # Models the listener thread handling a "skip" button press
            # while the publish loop is mid-transaction.
            try:
                repo.skip_pending(entry['link'])
            except Exception as exc:  # noqa: BLE001 — recorded for assert
                errors.append(exc)

        # Publish-loop writer: open transaction holding the write lock
        # (BEGIN IMMEDIATE acquires RESERVED immediately, like the
        # multi-statement move_to_* transactions do on first write).
        # Created BEFORE the connect patch so it uses a plain connection.
        locker = real_connect(self.db_path)
        try:
            locker.execute("BEGIN IMMEDIATE")
            locker.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('test_publish_loop_marker', 'busy'),
            )

            with patch.object(repo.sqlite3, 'connect',
                              side_effect=signalling_connect):
                t = threading.Thread(target=cancel_writer)
                t.start()
                # Event fires just before the writer's blocking INSERT;
                # the lock is still held here (commit comes only after
                # the wait returns), so contention is guaranteed.
                self.assertTrue(
                    write_reached.wait(timeout=10.0),
                    'cancel writer never reached its write statement',
                )
                locker.commit()  # release — writer must now proceed
                t.join(timeout=10.0)
                self.assertFalse(t.is_alive(),
                                 'cancel writer did not finish')
        finally:
            locker.close()

        self.assertEqual(
            errors, [],
            f'cancel writer raised instead of waiting out the lock: {errors}',
        )
        # The cancel write went through: pending row gone, dedup stamped.
        self.assertIsNone(repo.get_pending(entry['link']))
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        self.assertIsNotNone(row)

    def test_zero_timeout_control_raises_database_locked(self):
        """Negative control (test-review round 1, LOW): demonstrate the
        exact failure mode the busy-timeout protects against. A connection
        with ``timeout=0`` (busy_timeout=0 — no busy handler) attempting a
        write while another connection holds RESERVED fails IMMEDIATELY
        with ``database is locked``. Single-threaded and lock-release-free
        until teardown, so fully deterministic and fast."""
        locker = sqlite3.connect(self.db_path)
        zero_timeout = sqlite3.connect(self.db_path, timeout=0)
        try:
            locker.execute("BEGIN IMMEDIATE")
            locker.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('test_publish_loop_marker', 'busy'),
            )
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                zero_timeout.execute("BEGIN IMMEDIATE")
            self.assertIn('database is locked', str(ctx.exception))
            locker.rollback()
        finally:
            zero_timeout.close()
            locker.close()


# ---------------- SQL audit ----------------

class TestHoldState(_TmpDbCase):
    """``hold_reason`` — the content-gate hold state (2026-07-25).

    A held row lives in ``pending_articles`` exactly like any other row,
    but it is INVISIBLE to the publishable queue: ``list_pending`` (the
    slot loop's row source) and ``count_pending`` (the slot-computation
    and backlog-warning input) both filter it out. That single SQL
    predicate is the load-bearing guarantee behind the operator's rule
    «нет ответа = не публикуем»: with no approval the row simply never
    reaches the publish path, forever, with no timer involved.
    """

    def _insert_held(self, link, reason='poster', fetched_at=None):
        with self._conn() as c:
            if fetched_at is None:
                c.execute(
                    "INSERT INTO pending_articles "
                    "(link, source_name, title, paragraphs, hold_reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (link, 't-hunted', 'held title', '[]', reason),
                )
            else:
                c.execute(
                    "INSERT INTO pending_articles "
                    "(link, source_name, title, paragraphs, hold_reason, "
                    " fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (link, 't-hunted', 'held title', '[]', reason,
                     fetched_at),
                )
            c.commit()

    # -- schema -----------------------------------------------------------

    def test_column_add_is_idempotent_on_an_existing_db(self):
        """The ALTER runs on every ``init_schema`` call — including on the
        live prod DB, which already has rows and (after the first run)
        already has the column. Re-running must neither raise nor clobber."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs, hold_reason) "
                "VALUES ('http://x/keepme', 'mattel', 't', '[]', 'poster')"
            )
            conn.commit()
            repo.init_schema(conn)
            repo.init_schema(conn)
            row = conn.execute(
                "SELECT hold_reason FROM pending_articles WHERE link=?",
                ('http://x/keepme',),
            ).fetchone()
            self.assertEqual(row[0], 'poster')
        finally:
            conn.close()

    def test_legacy_rows_get_null_hold_reason(self):
        """Prod-safety: rows written before the migration must come back as
        publishable (NULL), never as accidentally-held."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO pending_articles "
                "(link, source_name, title, paragraphs) "
                "VALUES ('http://legacy/1', 'mattel', 't', '[]')"
            )
            c.commit()
        row = repo.get_pending('http://legacy/1')
        self.assertIsNone(row['hold_reason'])
        self.assertEqual(
            [r['link'] for r in repo.list_pending()], ['http://legacy/1'])

    # -- insert -----------------------------------------------------------

    def test_insert_pending_persists_hold_reason(self):
        entry = _sample_entry(link='http://hold/insert')
        entry['hold_reason'] = 'poster, url:poster'
        self.assertTrue(repo.insert_pending(entry))
        self.assertEqual(
            repo.get_pending('http://hold/insert')['hold_reason'],
            'poster, url:poster',
        )

    def test_insert_pending_without_hold_reason_stays_null(self):
        entry = _sample_entry(link='http://hold/plain')
        self.assertTrue(repo.insert_pending(entry))
        self.assertIsNone(repo.get_pending('http://hold/plain')['hold_reason'])

    # -- queue visibility -------------------------------------------------

    def test_list_pending_excludes_held_rows(self):
        repo.insert_pending(_sample_entry(link='http://free/1'))
        self._insert_held('http://held/1')
        self.assertEqual(
            [r['link'] for r in repo.list_pending()], ['http://free/1'])

    def test_count_pending_excludes_held_rows(self):
        """Slot computation (``compute_fixed_slots(N=count_pending())``) and
        the backlog warning both read this — a held row must not buy the
        day an extra publish slot it can never fill."""
        repo.insert_pending(_sample_entry(link='http://free/2'))
        self._insert_held('http://held/2')
        self._insert_held('http://held/3')
        self.assertEqual(repo.count_pending(), 1)

    def test_get_pending_still_sees_a_held_row(self):
        """``get_pending`` is the by-PK accessor used by the intake
        duplicate guard and both button resolvers — it must keep seeing
        held rows, otherwise the same article would be re-staged daily."""
        self._insert_held('http://held/4')
        self.assertIsNotNone(repo.get_pending('http://held/4'))

    def test_list_pending_stale_excludes_held_rows(self):
        """The 48h «залежалась» heads-up must not nag forever about an
        article that is deliberately parked awaiting approval."""
        self._insert_held(
            'http://held/stale', fetched_at="2020-01-01 00:00:00")
        self.assertEqual(repo.list_pending_stale(48), [])

    def test_list_notified_overdue_excludes_held_rows(self):
        """Uniform rule: no repo helper that feeds a publish path may hand
        out a held row (this pool is dormant, but the rule holds)."""
        self._insert_held('http://held/overdue')
        with self._conn() as c:
            c.execute(
                "UPDATE pending_articles SET notified_at=? WHERE link=?",
                ("2020-01-01 00:00:00", 'http://held/overdue'),
            )
            c.commit()
        self.assertEqual(repo.list_notified_overdue(2), [])

    def test_list_pending_for_eviction_excludes_held_rows(self):
        """Overflow fast-track drains the publishable queue; a held row is
        not part of it and must not be evicted behind the operator's back."""
        self._insert_held('http://held/evict')
        self.assertEqual(repo.list_pending_for_eviction(), [])

    # -- list_held --------------------------------------------------------

    def test_list_held_returns_only_held_rows_oldest_first(self):
        repo.insert_pending(_sample_entry(link='http://free/3'))
        self._insert_held('http://held/new', fetched_at="2026-07-25 10:00:00")
        self._insert_held('http://held/old', fetched_at="2026-07-01 10:00:00")
        self.assertEqual(
            [r['link'] for r in repo.list_held()],
            ['http://held/old', 'http://held/new'],
        )

    def test_list_held_deserializes_json_columns(self):
        entry = _sample_entry(link='http://held/json', paragraphs=['a', 'b'])
        entry['hold_reason'] = 'catálogo'
        repo.insert_pending(entry)
        row = repo.list_held()[0]
        self.assertEqual(row['paragraphs'], ['a', 'b'])
        self.assertEqual(row['hold_reason'], 'catálogo')

    def test_list_held_is_empty_when_nothing_is_held(self):
        repo.insert_pending(_sample_entry(link='http://free/4'))
        self.assertEqual(repo.list_held(), [])

    # -- clear_hold -------------------------------------------------------

    def test_clear_hold_releases_the_row_into_the_queue(self):
        self._insert_held('http://held/approve')
        self.assertTrue(repo.clear_hold('http://held/approve'))
        self.assertIsNone(repo.get_pending('http://held/approve')['hold_reason'])
        self.assertEqual(
            [r['link'] for r in repo.list_pending()], ['http://held/approve'])
        self.assertEqual(repo.count_pending(), 1)
        self.assertEqual(repo.list_held(), [])

    def test_clear_hold_returns_false_when_the_row_is_gone(self):
        """Distinguishes «одобрено» from «статья уже недоступна» in the
        button resolver without a second round-trip."""
        self.assertFalse(repo.clear_hold('http://never/existed'))

    def test_clear_hold_returns_false_when_the_row_is_not_held(self):
        """Already approved (or never held): nothing to release. Keeps a
        double press from reporting a fresh approval."""
        repo.insert_pending(_sample_entry(link='http://free/5'))
        self.assertFalse(repo.clear_hold('http://free/5'))

    def test_clear_hold_touches_only_the_named_row(self):
        self._insert_held('http://held/a')
        self._insert_held('http://held/b')
        repo.clear_hold('http://held/a')
        self.assertEqual([r['link'] for r in repo.list_held()],
                         ['http://held/b'])

    # -- reject path ------------------------------------------------------

    def test_skip_pending_removes_a_held_row_and_pins_it(self):
        """«🚫 Не публиковать» reuses ``skip_pending`` — it must work on a
        held row too (DELETE + processed_news pin so it never comes back)."""
        self._insert_held('http://held/reject')
        repo.skip_pending('http://held/reject')
        self.assertIsNone(repo.get_pending('http://held/reject'))
        self.assertEqual(repo.list_held(), [])
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_news WHERE link=?",
                ('http://held/reject',),
            ).fetchone()
        self.assertIsNotNone(row)


class TestColumnMigrationHardening(unittest.TestCase):
    """Audit SEC-CG-1 — the idempotent column migration must VERIFY, not
    assume.

    The old shape (`try: ALTER / except sqlite3.OperationalError: pass`)
    could not distinguish "already migrated" from "the ALTER failed":
    a writer holding an IMMEDIATE lock past the busy timeout raises
    `database is locked`, which is ALSO an OperationalError. The migration
    would report success with the column absent — and because
    `list_pending` / `count_pending` / `insert_pending` name the migrated
    columns unconditionally, every subsequent tick would die on
    `no such column` with nothing pointing at the real cause.
    """

    def _fresh(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        return conn

    def _locked_db_missing_hold_reason(self):
        """A REAL locked database, not a mocked exception: migrate a
        tempfile DB, drop ``hold_reason`` back off it, then have a second
        connection hold an IMMEDIATE (write) lock. Reads still work — so
        the existence check succeeds — but the ALTER needs EXCLUSIVE and
        genuinely raises ``database is locked`` once the busy timeout
        expires. Returns the connection init_schema should choke on."""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        migrating = sqlite3.connect(path, timeout=0.1)
        self.addCleanup(migrating.close)
        repo.init_schema(migrating)
        migrating.execute(
            "ALTER TABLE pending_articles DROP COLUMN hold_reason")
        migrating.commit()

        blocker = sqlite3.connect(path, timeout=0.1)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('x', '1')")
        self.addCleanup(blocker.rollback)
        return migrating, path

    #: The migration entry under test, read from the live tuple.
    HOLD_MIGRATION = ('pending_articles', 'hold_reason',
                      "ALTER TABLE pending_articles ADD COLUMN hold_reason TEXT")

    def test_hold_migration_entry_is_the_live_one(self):
        """The lock tests below drive ``_ensure_column`` with this triple;
        pin it against the real table so they can't test a fossil."""
        self.assertIn(self.HOLD_MIGRATION, repo._COLUMN_MIGRATIONS)

    def test_locked_db_during_alter_raises_instead_of_silently_passing(self):
        """THE finding, reproduced against a REAL lock (not a mocked
        exception): the ALTER genuinely raises `database is locked`, which
        is an OperationalError just like `duplicate column name` — and it
        must NOT be mistaken for an already-migrated no-op.

        Drives ``_ensure_column`` rather than ``init_schema`` because the
        preceding ``CREATE TABLE IF NOT EXISTS`` statements need the write
        lock too and would fail first; ``init_schema``'s propagation is
        covered by ``test_verification_catches_an_alter_that_did_not_take``.
        """
        migrating, _path = self._locked_db_missing_hold_reason()

        with self.assertRaises(repo.SchemaMigrationError) as ctx:
            repo._ensure_column(migrating, *self.HOLD_MIGRATION)

        # The error names the column so the operator can act on it.
        self.assertIn('hold_reason', str(ctx.exception))
        # And the column really is still absent — i.e. had this been
        # swallowed, every subsequent tick would die on `no such column`.
        self.assertFalse(repo._column_exists(
            migrating, 'pending_articles', 'hold_reason'))

    def test_locked_db_failure_is_logged_before_it_is_raised(self):
        migrating, _path = self._locked_db_missing_hold_reason()
        with self.assertLogs('pending_articles_repo', level='ERROR') as cm:
            with self.assertRaises(repo.SchemaMigrationError):
                repo._ensure_column(migrating, *self.HOLD_MIGRATION)
        self.assertTrue(
            any('schema migration failed' in line for line in cm.output),
            cm.output,
        )

    def test_a_swallowed_lock_would_break_the_publish_path(self):
        """Why this is HIGH and not cosmetic: with the column absent, the
        every-tick queries that name it fail. Pinning the consequence
        keeps the severity argument honest if anyone relaxes the guard."""
        migrating, path = self._locked_db_missing_hold_reason()
        with self.assertRaises(repo.SchemaMigrationError):
            repo._ensure_column(migrating, *self.HOLD_MIGRATION)

        with patch.object(news_bot, 'DB_FILE', path):
            for call in (repo.count_pending, repo.list_pending, repo.list_held):
                with self.subTest(call=call.__name__):
                    with self.assertRaises(sqlite3.OperationalError) as ctx:
                        call()
                    self.assertIn('hold_reason', str(ctx.exception))

    def test_duplicate_column_race_is_still_tolerated(self):
        """The one benign OperationalError: another process added the
        column between our check and our ALTER. Simulated by forcing the
        existence check to report 'missing' on an already-migrated DB, so
        the real ALTER raises a real `duplicate column name`."""
        conn = self._fresh()
        repo.init_schema(conn)
        with patch.object(repo, '_column_exists', return_value=False):
            repo.init_schema(conn)  # must not raise
        # And the DB is still intact.
        self.assertTrue(repo._column_exists(
            conn, 'pending_articles', 'hold_reason'))

    def test_alter_is_skipped_entirely_when_the_column_exists(self):
        """Idempotent path stays cheap and side-effect-free: a second
        init_schema issues no ALTER at all. ``sqlite3.Connection.execute``
        is read-only, so spy through a thin proxy."""
        conn = self._fresh()
        repo.init_schema(conn)
        seen = []

        class _SpyConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                seen.append(sql)
                return self._inner.execute(sql, *args, **kwargs)

            def commit(self):
                return self._inner.commit()

        repo.init_schema(_SpyConn(conn))
        self.assertEqual(
            [s for s in seen if s.strip().upper().startswith('ALTER')], [])

    def test_verification_catches_an_alter_that_did_not_take(self):
        """Belt-and-braces: ALTER reports success but the column is not
        there → fatal, never trusted."""
        conn = self._fresh()

        class _LyingConn:
            """ALTER silently does nothing; everything else is real."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith('ALTER'):
                    return None
                return self._inner.execute(sql, *args, **kwargs)

            def commit(self):
                return self._inner.commit()

        with self.assertRaises(repo.SchemaMigrationError) as ctx:
            repo.init_schema(_LyingConn(conn))
        self.assertIn('missing after a successful ALTER', str(ctx.exception))

    def test_every_migration_entry_is_verifiable(self):
        """Structural invariant: the (table, column) pair that drives the
        existence check must match the DDL literal, or the check would
        silently guard the wrong column."""
        for table, column, ddl in repo._COLUMN_MIGRATIONS:
            with self.subTest(column=f'{table}.{column}'):
                self.assertIn(f'ALTER TABLE {table} ', ddl)
                self.assertIn(f'ADD COLUMN {column} ', ddl)

    def test_all_migrated_columns_exist_after_init_on_a_fresh_db(self):
        conn = self._fresh()
        repo.init_schema(conn)
        for table, column, _ddl in repo._COLUMN_MIGRATIONS:
            with self.subTest(column=f'{table}.{column}'):
                self.assertTrue(repo._column_exists(conn, table, column))


class TestSqlAudit(unittest.TestCase):
    """Grep-style audit: repo source must not hand-roll SQL with f-strings / %
    formatting around SQL keywords."""

    def test_parameterized_queries_only(self):
        # Scan every SQL-bearing first-party file. `backfill_fingerprints.py`
        # was added (test-audit L-2): the security audit had to hand-verify its
        # static-literal `json_extract(...,'$.pairs')` predicate because this
        # net previously skipped it — now it is inside the automated scope.
        import re
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sql_source_files = [
            'pending_articles_repo.py',
            'backfill_fingerprints.py',
        ]

        # Very narrow check: look for f-string or %-format patterns that
        # appear immediately adjacent to SQL keywords. False positives are
        # acceptable at this level — the intent is a smoke net, not a parser.
        sql_keywords = r'(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|VALUES)'
        forbidden_patterns = [
            # f"... SELECT ... {expr} ..."
            re.compile(rf"f['\"][^'\"]*{sql_keywords}[^'\"]*\{{[^}}]+\}}", re.IGNORECASE),
            # "... WHERE x = " + var
            re.compile(rf"['\"][^'\"]*{sql_keywords}[^'\"]*['\"]\s*\+\s*\w", re.IGNORECASE),
            # "... WHERE x = %s" % var
            re.compile(rf"['\"][^'\"]*{sql_keywords}[^'\"]*%s[^'\"]*['\"]", re.IGNORECASE),
        ]
        for fname in sql_source_files:
            src_path = os.path.join(repo_root, fname)
            with open(src_path, 'r', encoding='utf-8') as f:
                source = f.read()
            for pat in forbidden_patterns:
                m = pat.search(source)
                self.assertIsNone(
                    m,
                    f"parameterized-query rule violated: pattern {pat.pattern!r} "
                    f"matched {m.group(0) if m else None!r} in {fname}",
                )


if __name__ == '__main__':
    unittest.main()


pending_articles_repo = repo


class TestPublishAfterDefer(unittest.TestCase):
    """``publish_after`` — the timed sibling of ``hold_reason`` (2026-07-28).

    A hold waits FOREVER for the operator; a defer expires by itself. That
    asymmetry is deliberate and is the operator's rule per gate: «нет ответа =
    не публикуем» for the content gate, «нет ответа = публикуем» for a
    suspected duplicate.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig_db = news_bot.DB_FILE
        news_bot.DB_FILE = self.tmp.name
        news_bot.init_db()

    def tearDown(self):
        news_bot.DB_FILE = self._orig_db
        os.unlink(self.tmp.name)

    def _stage(self, link, publish_after=None):
        return pending_articles_repo.insert_pending({
            'link': link,
            'source_name': 't-hunted',
            'title': 'T',
            'paragraphs': ['body'],
            'publish_after': publish_after,
        })

    def _offset(self, hours):
        return (
            datetime.now(timezone.utc) + timedelta(hours=hours)
        ).strftime('%Y-%m-%d %H:%M:%S')

    def test_future_timestamp_hides_row_from_the_queue(self):
        self._stage('http://x/deferred', self._offset(24))
        self.assertEqual(pending_articles_repo.count_pending(), 0)
        self.assertEqual(pending_articles_repo.list_pending(), [])

    def test_deferred_row_is_still_staged_and_findable(self):
        """Withheld from the QUEUE, not from the database.

        ``get_pending`` must keep seeing it or the intake duplicate guard
        would re-stage the same article every tick, and both button resolvers
        look rows up by link — the cancel button has to work while the row is
        deferred, which is the entire point of the delay.
        """
        self._stage('http://x/deferred', self._offset(24))
        row = pending_articles_repo.get_pending('http://x/deferred')
        self.assertIsNotNone(row)
        self.assertIsNotNone(row['publish_after'])
        self.assertFalse(pending_articles_repo.insert_pending({
            'link': 'http://x/deferred', 'source_name': 't-hunted',
            'title': 'T', 'paragraphs': ['body'],
        }), "duplicate guard must still reject a re-stage of a deferred row")

    def test_elapsed_timestamp_returns_the_row_to_the_queue(self):
        """Silence publishes — this is the auto-release the operator chose."""
        self._stage('http://x/ripe', self._offset(-1))
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_pending()],
            ['http://x/ripe'],
        )

    def test_null_is_publishable_now(self):
        """Every pre-migration row has NULL here and must stay publishable."""
        self._stage('http://x/plain', None)
        self.assertEqual(pending_articles_repo.count_pending(), 1)

    def test_deferred_row_is_not_evicted_by_the_overflow_drain(self):
        """Evicting a deferred row would destroy a decision the operator has
        not made yet — the same argument ``list_pending_for_eviction`` already
        makes for held rows."""
        self._stage('http://x/deferred', self._offset(24))
        self._stage('http://x/plain', None)
        links = [r['link'] for r in
                 pending_articles_repo.list_pending_for_eviction()]
        self.assertEqual(links, ['http://x/plain'])

    def test_defer_and_hold_are_independent(self):
        """A row can be both; either one alone must keep it out of the queue."""
        pending_articles_repo.insert_pending({
            'link': 'http://x/both', 'source_name': 't-hunted', 'title': 'T',
            'paragraphs': ['body'], 'hold_reason': 'poster',
            'publish_after': self._offset(-1),
        })
        self.assertEqual(pending_articles_repo.count_pending(), 0,
                         "elapsed defer must not release a HELD row")


class TestHoldCounter(unittest.TestCase):
    """``hold_count`` — how many times in a row the slot loop HELD this row.

    Separate from ``attempt_count`` on purpose. A strike moves the row toward
    ``failed_articles``; a hold must never do that (hold-and-wait exists so an
    LLM outage costs nothing). But an unbounded hold pins the row to the queue
    head — ``news_bot.job()`` re-reads ``list_pending()[0]`` every slot — so it
    needs its own bound, with its own counter.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig_db = news_bot.DB_FILE
        news_bot.DB_FILE = self.tmp.name
        news_bot.init_db()

    def tearDown(self):
        news_bot.DB_FILE = self._orig_db
        os.unlink(self.tmp.name)

    def _stage(self, link='http://x/held'):
        pending_articles_repo.insert_pending({
            'link': link, 'source_name': 't-hunted', 'title': 'T',
            'paragraphs': ['body'],
        })
        return link

    def test_counts_up_from_a_legacy_null(self):
        """The column is added by migration as nullable, so every row that
        existed before it reads NULL — which must behave as 0, not blow up a
        ``NULL + 1`` into NULL and silently disable the cap forever."""
        link = self._stage()
        self.assertIsNone(pending_articles_repo.get_pending(link)['hold_count'])
        self.assertEqual(pending_articles_repo.increment_hold(link), 1)
        self.assertEqual(pending_articles_repo.increment_hold(link), 2)
        self.assertEqual(pending_articles_repo.get_pending(link)['hold_count'], 2)

    def test_missing_row_returns_zero(self):
        """A row can vanish between the slot loop reading it and the hold being
        recorded (manual review publishes it). Returning 0 keeps the caller
        below any cap instead of raising inside an error path."""
        self.assertEqual(
            pending_articles_repo.increment_hold('http://x/gone'), 0)

    def test_hold_does_not_touch_attempt_count(self):
        """The invariant the whole hold-and-wait design rests on: holding an
        article must never move it toward ``failed_articles``."""
        link = self._stage()
        for _ in range(5):
            pending_articles_repo.increment_hold(link)
        self.assertEqual(
            pending_articles_repo.get_pending(link)['attempt_count'], 0)

    def test_defer_publish_takes_the_row_out_of_the_queue(self):
        """What actually unblocks the channel: after the defer the row is gone
        from ``list_pending``, so the next slot reads a DIFFERENT ``rows[0]``."""
        stuck = self._stage('http://x/stuck')
        nxt = self._stage('http://x/next')
        until = (datetime.now(timezone.utc) + timedelta(hours=24)
                 ).strftime('%Y-%m-%d %H:%M:%S')

        pending_articles_repo.defer_publish(stuck, until)

        self.assertEqual(
            [r['link'] for r in pending_articles_repo.list_pending()], [nxt])
        self.assertEqual(
            pending_articles_repo.get_pending(stuck)['publish_after'], until)

    def test_defer_publish_keeps_the_counter(self):
        """Resetting on defer would give a known-bad row a fresh full cap every
        time it came back — wedging the head for another N slots per cycle.
        Keeping the count means it steps aside after ONE hold from then on.
        """
        link = self._stage()
        for _ in range(6):
            pending_articles_repo.increment_hold(link)
        pending_articles_repo.defer_publish(
            link, (datetime.now(timezone.utc) + timedelta(hours=24)
                   ).strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(pending_articles_repo.increment_hold(link), 7)

    def test_defer_publish_reports_whether_a_row_was_touched(self):
        """The caller pings [E038] «отложена, вернётся сама» on True. If the
        review listener deleted the row a microsecond earlier, that sentence
        would be a lie — so a miss must be distinguishable from a hit."""
        link = self._stage()
        self.assertTrue(
            pending_articles_repo.defer_publish(link, '2030-01-01 00:00:00'))
        self.assertFalse(
            pending_articles_repo.defer_publish('http://x/gone',
                                                '2030-01-01 00:00:00'))

    def test_counter_is_per_row(self):
        """Holding one article must not age any other — otherwise a single bad
        row would push the whole queue over the cap with it."""
        a = self._stage('http://x/a')
        b = self._stage('http://x/b')
        for _ in range(3):
            pending_articles_repo.increment_hold(a)
        self.assertEqual(pending_articles_repo.get_pending(a)['hold_count'], 3)
        self.assertIsNone(pending_articles_repo.get_pending(b)['hold_count'])

    # -- reset on recovery ------------------------------------------------

    def test_reset_clears_rows_below_the_cap_only(self):
        """What makes the counter mean "in a row". A working LLM proves the
        holds were global — except for rows that already crossed the cap: they
        keep the marker so they yield on their FIRST hold next window instead
        of blocking for another full cap."""
        incidental = self._stage('http://x/incidental')
        proven = self._stage('http://x/proven')
        for _ in range(3):
            pending_articles_repo.increment_hold(incidental)
        for _ in range(6):
            pending_articles_repo.increment_hold(proven)

        cleared = pending_articles_repo.reset_hold_counts_below(6)

        self.assertEqual(cleared, 1)
        self.assertEqual(
            pending_articles_repo.get_pending(incidental)['hold_count'], 0)
        self.assertEqual(
            pending_articles_repo.get_pending(proven)['hold_count'], 6)

    def test_reset_is_a_noop_when_nothing_is_counted(self):
        self._stage()
        self.assertEqual(pending_articles_repo.reset_hold_counts_below(6), 0)

    # -- deferred backlog -------------------------------------------------

    def test_count_deferred_counts_only_future_defers(self):
        """``count_pending`` excludes deferred rows, and job() sizes the day's
        slots ONCE from it — so this number is what stops a fully-deferred tick
        from computing zero slots and skipping the day."""
        self._stage('http://x/now')
        future = self._stage('http://x/future')
        past = self._stage('http://x/past')
        pending_articles_repo.defer_publish(
            future, (datetime.now(timezone.utc) + timedelta(hours=24)
                     ).strftime('%Y-%m-%d %H:%M:%S'))
        pending_articles_repo.defer_publish(
            past, (datetime.now(timezone.utc) - timedelta(hours=1)
                   ).strftime('%Y-%m-%d %H:%M:%S'))

        self.assertEqual(pending_articles_repo.count_deferred(), 1)
        # An elapsed defer is publishable again and counts as queue, not backlog.
        self.assertEqual(pending_articles_repo.count_pending(), 2)
