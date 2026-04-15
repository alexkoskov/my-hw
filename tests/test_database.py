#!/usr/bin/env python3
"""
Unit tests for database functions (init_db, is_processed, mark_processed).
"""

import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import init_db, is_processed, mark_processed, DB_FILE


class TestDatabaseFunctions(unittest.TestCase):
    """Test database functions."""

    @patch('news_bot.sqlite3.connect')
    def test_init_db_creates_table(self, mock_connect):
        """init_db creates processed_news table if not exists."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        init_db()

        mock_connect.assert_called_once_with(DB_FILE)
        # Check that execute was called with the correct SQL (ignore whitespace differences)
        args, kwargs = mock_cursor.execute.call_args
        self.assertEqual(len(args), 1)
        sql = args[0]
        # Normalize whitespace: replace newlines with space, collapse multiple spaces
        import re
        normalized = re.sub(r'\s+', ' ', sql.strip())
        expected = 'CREATE TABLE IF NOT EXISTS processed_news (link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
        self.assertEqual(normalized, expected)
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

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


if __name__ == '__main__':
    unittest.main()