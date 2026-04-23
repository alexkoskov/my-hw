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
    'fetched_at':    {'type': 'TIMESTAMP', 'notnull': 1, 'dflt_value': 'CURRENT_TIMESTAMP',     'pk': 0},
    'notified_at':   {'type': 'TIMESTAMP', 'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'attempt_count': {'type': 'INTEGER',   'notnull': 1, 'dflt_value': '0',                     'pk': 0},
    'last_error':    {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
    'pub_date':      {'type': 'TEXT',      'notnull': 0, 'dflt_value': None,                    'pk': 0},
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

    def test_list_pending_orders_by_fetched_at_asc(self):
        self._insert_with_ts('http://a/1', '2026-04-01 00:00:00')
        self._insert_with_ts('http://a/2', '2026-04-03 00:00:00')
        self._insert_with_ts('http://a/3', '2026-04-02 00:00:00')

        rows = repo.list_pending()
        self.assertEqual([r['link'] for r in rows],
                         ['http://a/1', 'http://a/3', 'http://a/2'])

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


# ---------------- SQL audit ----------------

class TestSqlAudit(unittest.TestCase):
    """Grep-style audit: repo source must not hand-roll SQL with f-strings / %
    formatting around SQL keywords."""

    def test_parameterized_queries_only(self):
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'pending_articles_repo.py',
        )
        with open(src_path, 'r', encoding='utf-8') as f:
            source = f.read()

        # Very narrow check: look for f-string or %-format patterns that
        # appear immediately adjacent to SQL keywords. False positives are
        # acceptable at this level — the intent is a smoke net, not a parser.
        import re
        sql_keywords = r'(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|VALUES)'
        forbidden_patterns = [
            # f"... SELECT ... {expr} ..."
            re.compile(rf"f['\"][^'\"]*{sql_keywords}[^'\"]*\{{[^}}]+\}}", re.IGNORECASE),
            # "... WHERE x = " + var
            re.compile(rf"['\"][^'\"]*{sql_keywords}[^'\"]*['\"]\s*\+\s*\w", re.IGNORECASE),
            # "... WHERE x = %s" % var
            re.compile(rf"['\"][^'\"]*{sql_keywords}[^'\"]*%s[^'\"]*['\"]", re.IGNORECASE),
        ]
        for pat in forbidden_patterns:
            m = pat.search(source)
            self.assertIsNone(
                m,
                f"parameterized-query rule violated: pattern {pat.pattern!r} "
                f"matched {m.group(0) if m else None!r} in pending_articles_repo.py",
            )


if __name__ == '__main__':
    unittest.main()
