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
import time
import unittest
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
        self.assertEqual(prefixed[0], 'http://x/a')
        self.assertIsNone(bare)


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


class TestConcurrentWriters(_TmpDbCase):
    """Carry-forward from tech-spec (Integration tests / Risks «database is
    locked»): publish-loop writer and cancel writer hit the same DB file at
    the same time — under busy_timeout the second writer WAITS for the lock
    instead of failing.

    Control (not run, to keep the test deterministic): with
    ``timeout=0`` (busy_timeout=0) on the cancel writer's connection, the
    same scenario raises ``sqlite3.OperationalError: database is locked``
    the instant its first write statement hits the RESERVED lock held by
    the publish-loop transaction.
    """

    def test_two_writers_no_database_locked(self):
        entry = _sample_entry(link='http://c/concurrent')
        repo.insert_pending(entry)

        errors = []
        writer_started = threading.Event()

        def cancel_writer():
            # Models the listener thread handling a "skip" button press
            # while the publish loop is mid-transaction.
            writer_started.set()
            try:
                repo.skip_pending(entry['link'])
            except Exception as exc:  # noqa: BLE001 — recorded for assert
                errors.append(exc)

        # Publish-loop writer: open transaction holding the write lock
        # (BEGIN IMMEDIATE acquires RESERVED immediately, like the
        # multi-statement move_to_* transactions do on first write).
        locker = sqlite3.connect(self.db_path)
        try:
            locker.execute("BEGIN IMMEDIATE")
            locker.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('test_publish_loop_marker', 'busy'),
            )

            t = threading.Thread(target=cancel_writer)
            t.start()
            self.assertTrue(writer_started.wait(timeout=5.0))
            # Give the cancel writer time to reach the locked write and
            # park inside the busy handler (well under the 5 s budget).
            time.sleep(0.3)
            locker.commit()  # release the lock — writer must now proceed
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), 'cancel writer did not finish')
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


# ---------------- SQL audit ----------------

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
