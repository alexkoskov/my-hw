#!/usr/bin/env python3
"""
Unit tests for database functions (init_db, is_processed, mark_processed).
"""

import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
import re
import subprocess
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import init_db, is_processed, mark_processed, DB_FILE


class TestDatabaseFunctions(unittest.TestCase):
    """Test database functions."""

    @patch('news_bot.sqlite3.connect')
    def test_init_db_creates_table(self, mock_connect):
        """init_db still owns the processed_news DDL.

        After manual-review-workflow Task 6, ``init_db`` also delegates the
        three new tables (``pending_articles`` / ``published_articles`` /
        ``failed_articles``) to ``pending_articles_repo.init_schema``, which
        issues further DDL on the same connection. Hence this test asserts
        the ``processed_news`` DDL appears *among* the execute calls rather
        than being the single/last one.
        """
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        init_db()

        mock_connect.assert_called_once_with(DB_FILE)

        # Collect every execute-call across both the cursor and the raw
        # connection (``pending_articles_repo.init_schema`` uses
        # ``conn.execute(...)``).
        all_sql_calls = list(mock_cursor.execute.call_args_list)
        all_sql_calls += list(mock_conn.execute.call_args_list)
        sqls = [c.args[0] for c in all_sql_calls if c.args]

        def _norm(sql):
            import re
            return re.sub(r'\s+', ' ', sql.strip())

        self.assertTrue(
            any(_norm(s).startswith(
                'CREATE TABLE IF NOT EXISTS processed_news') for s in sqls),
            msg=f"processed_news DDL not found in execute calls: {sqls}",
        )
        # Commit happens at least once (may be twice: once inside
        # ``init_schema``, once at the end of ``init_db``).
        self.assertTrue(mock_conn.commit.called)
        mock_conn.close.assert_called_once()

    @patch('news_bot.sqlite3.connect')
    def test_init_db_also_creates_pending_tables(self, mock_connect):
        """init_db delegates DDL for the three new tables to the repo."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        init_db()

        all_sql_calls = list(mock_cursor.execute.call_args_list)
        all_sql_calls += list(mock_conn.execute.call_args_list)
        sqls = [c.args[0] for c in all_sql_calls if c.args]

        def _has(name):
            return any(
                f'CREATE TABLE IF NOT EXISTS {name}' in sql for sql in sqls
            )

        self.assertTrue(_has('pending_articles'),
                        msg=f"pending_articles DDL missing: {sqls}")
        self.assertTrue(_has('published_articles'),
                        msg=f"published_articles DDL missing: {sqls}")
        self.assertTrue(_has('failed_articles'),
                        msg=f"failed_articles DDL missing: {sqls}")

    @patch('news_bot.sqlite3.connect')
    def test_is_processed_true(self, mock_connect):
        """is_processed returns True when link exists."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)

        result = is_processed('http://example.com/article')
        self.assertTrue(result)
        mock_cursor.execute.assert_called_once_with(
            "SELECT 1 FROM processed_news WHERE link = ?",
            ('http://example.com/article',)
        )
        mock_conn.close.assert_called_once()

    @patch('news_bot.sqlite3.connect')
    def test_is_processed_false(self, mock_connect):
        """is_processed returns False when link not found."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None

        result = is_processed('http://example.com/article')
        self.assertFalse(result)

    @patch('news_bot.sqlite3.connect')
    def test_mark_processed_inserts(self, mock_connect):
        """mark_processed inserts link, title, pub_date."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        mark_processed('http://example.com/article', 'Test Title', '2025-01-01')

        mock_cursor.execute.assert_called_once_with(
            "INSERT INTO processed_news (link, title, pub_date) VALUES (?, ?, ?)",
            ('http://example.com/article', 'Test Title', '2025-01-01')
        )
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch('news_bot.sqlite3.connect')
    def test_mark_processed_logs(self, mock_connect):
        """mark_processed logs debug message."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        with self.assertLogs('news_bot', level='DEBUG') as cm:
            mark_processed('http://example.com/article', 'Title', 'date')
        self.assertTrue(any('Marked as processed' in record.message for record in cm.records))


class TestDBFileEnvConfig(unittest.TestCase):
    """``DB_FILE`` must be configurable via the ``DB_FILE`` environment variable.

    The Docker+VPN container mounts the persistent DB at ``/data/news.db`` and
    sets ``DB_FILE=/data/news.db`` in ``.env``; the bot MUST honor it, otherwise
    it starts on an empty ephemeral ``/app/news.db`` and re-floods the channel
    with backlog (and loses all state on every redeploy). The NL/systemd + test
    default must stay ``news.db``.

    ``DB_FILE`` is resolved once at import time (module-level ``os.getenv``), so a
    fresh interpreter is the faithful way to observe what a started container
    sees — hence the subprocess (no in-process reload side effects).
    """

    _PROG = (
        "import news_bot, sys; "
        "sys.stdout.write('<<DBFILE:%s>>' % news_bot.DB_FILE)"
    )

    def _imported_db_file(self, env_overrides):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env.pop("DB_FILE", None)          # clean inherited process-env slate
        # repo on PYTHONPATH so the child imports news_bot from a scratch cwd —
        # the child's news_bot.DB_FILE then reflects ONLY what we pass here.
        # (news_bot's import-time load_dotenv resolves .env next to the module;
        # the repo never commits a DB_FILE there — it's container-injected,
        # .dockerignore'd and gitignored — so unset/default stays deterministic.)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (repo_root, env.get("PYTHONPATH", "")) if p)
        env.update(env_overrides)
        with tempfile.TemporaryDirectory() as scratch:
            try:
                result = subprocess.run(
                    [sys.executable, "-c", self._PROG],
                    cwd=scratch, env=env, capture_output=True, text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"news_bot import timed out after 60s: {exc}")
        self.assertEqual(result.returncode, 0,
                         msg=f"news_bot import failed: {result.stderr}")
        match = re.search(r"<<DBFILE:(.*?)>>", result.stdout)
        self.assertIsNotNone(
            match, msg=f"DB_FILE marker not found in stdout: {result.stdout!r}")
        return match.group(1)

    def test_defaults_to_news_db_when_unset(self):
        """No ``DB_FILE`` env → historical default ``news.db``."""
        self.assertEqual(self._imported_db_file({}), "news.db")

    def test_honors_db_file_env_override(self):
        """``DB_FILE`` env set → the bot uses it (container's mounted volume)."""
        self.assertEqual(
            self._imported_db_file({"DB_FILE": "/data/news.db"}),
            "/data/news.db",
        )

    def test_strips_surrounding_whitespace(self):
        """A stray space in .env (``DB_FILE=/data/news.db ``) must not silently
        open a different path — the value is ``.strip()``ed."""
        self.assertEqual(
            self._imported_db_file({"DB_FILE": "  /data/news.db  "}),
            "/data/news.db",
        )

    def test_empty_db_file_falls_back_to_default(self):
        """A blank ``DB_FILE=`` in .env resolves to "" and sqlite3.connect("")
        opens a throwaway temp DB (empty state → flood) — must fall back."""
        self.assertEqual(self._imported_db_file({"DB_FILE": ""}), "news.db")

    def test_whitespace_only_db_file_falls_back_to_default(self):
        """A whitespace-only ``DB_FILE=   `` strips to "" — must fall back too."""
        self.assertEqual(self._imported_db_file({"DB_FILE": "   "}), "news.db")


if __name__ == '__main__':
    unittest.main()
