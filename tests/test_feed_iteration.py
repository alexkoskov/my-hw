#!/usr/bin/env python3
"""Tests for feed iteration + error isolation via the prep-phase ``job()``.

After manual-review-workflow Task 6, ``job()`` iterates the ``SOURCES``
registry instead of the raw feed list, and the ``process_new_articles``
helper is gone. These tests therefore patch through ``load_feeds`` and
``fetch_rss`` (which ``_fetch_rss_entries`` calls) and assert staging
via ``pending_articles_repo`` rather than against an auto-publish path.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import job, RSS_URL
import news_bot
import pending_articles_repo


class TestJobIteration(unittest.TestCase):
    """Test ``job()`` iteration over multiple feeds via ``SOURCES``."""

    def setUp(self):
        # Tempfile DB so inserts / counts are isolated per test.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.admin_patcher = patch('news_bot.send_admin_notification')
        self.mock_admin = self.admin_patcher.start()
        # Keep Mattel silent unless a test changes it.
        self.mattel_patcher = patch('news_bot.fetch_mattel_news', return_value=[])
        self.mattel_patcher.start()
        # Default: no full-article fetch network traffic.
        self.fetch_article_patcher = patch(
            'news_bot.fetch_full_article',
            return_value={'title': 'T', 'subtitle': '',
                          'paragraphs': ['Body'], 'images': []},
        )
        self.mock_fetch_article = self.fetch_article_patcher.start()
        # Task 8 added the distributed-publish loop to ``job()``; without
        # neutering ``time.sleep`` and ``_fallback_publish`` these prep-phase
        # tests would block on slot waits. Wave 7 (Task 11) reshapes them;
        # here we only keep them green.
        self.sleep_patcher = patch('news_bot.time.sleep')
        self.sleep_patcher.start()
        self.fallback_patcher = patch('news_bot._fallback_publish')
        self.fallback_patcher.start()

    def tearDown(self):
        self.fallback_patcher.stop()
        self.sleep_patcher.stop()
        self.fetch_article_patcher.stop()
        self.mattel_patcher.stop()
        self.admin_patcher.stop()
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    def test_job_iterates_over_feeds(self, mock_fetch, mock_load):
        """``job()`` fetches every feed returned by ``load_feeds``."""
        feed_urls = [
            'https://example.com/feed1.xml',
            'https://example.com/feed2.xml',
            'https://example.com/feed3.xml',
        ]
        mock_load.return_value = feed_urls
        mock_fetch.side_effect = [
            [{'link': f'http://example.com/a{i}', 'title': f'Title{i}'}]
            for i in range(1, 4)
        ]

        job()

        mock_load.assert_called_once_with()
        self.assertEqual(mock_fetch.call_count, 3)
        expected_calls = [call(url) for url in feed_urls]
        mock_fetch.assert_has_calls(expected_calls, any_order=False)
        # Three distinct links staged.
        self.assertEqual(pending_articles_repo.count_pending(), 3)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    def test_error_isolation(self, mock_fetch, mock_load):
        """If one feed returns empty (helper caught the exception) the
        others still stage entries."""
        feed_urls = ['http://good1.xml', 'http://bad.xml', 'http://good2.xml']
        mock_load.return_value = feed_urls

        def fetch_side_effect(url):
            if url == 'http://bad.xml':
                return []  # fetch_rss swallowed the exception
            return [{'link': f'http://example.com/{url}', 'title': 'Title'}]
        mock_fetch.side_effect = fetch_side_effect

        job()

        self.assertEqual(mock_fetch.call_count, 3)
        # Two good feeds → two staged rows; bad feed contributes nothing.
        self.assertEqual(pending_articles_repo.count_pending(), 2)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    def test_no_global_limit(self, mock_fetch, mock_load):
        """The old ``limit=3`` cap on ``process_new_articles`` is gone —
        every accepted entry is staged. Hard-cap enforcement is no longer
        part of the prep phase: the distributed-publish loop carries
        excess rows over to the next day instead."""
        feed_urls = ['http://feed1.xml', 'http://feed2.xml']
        mock_load.return_value = feed_urls
        entries = [
            {'link': f'http://example.com/a{i}', 'title': f'Title{i}'}
            for i in range(5)
        ]
        mock_fetch.return_value = entries

        job()

        # Deduped within a tick by PRIMARY KEY; 5 unique links from each of
        # two feeds → 5 unique links total (feeds return the same list).
        self.assertEqual(pending_articles_repo.count_pending(), 5)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    def test_empty_feeds_list_falls_back(self, mock_fetch, mock_load):
        """Empty ``load_feeds`` → ``_fetch_rss_entries`` falls back to
        ``RSS_URL``."""
        mock_load.return_value = []
        mock_fetch.return_value = []

        job()

        mock_load.assert_called_once()
        mock_fetch.assert_called_once_with(RSS_URL)
        self.assertEqual(pending_articles_repo.count_pending(), 0)


if __name__ == '__main__':
    unittest.main()
