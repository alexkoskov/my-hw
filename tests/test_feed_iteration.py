#!/usr/bin/env python3
"""
Unit tests for feed iteration and error isolation in job().
"""

import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import job, RSS_URL


class TestJobIteration(unittest.TestCase):
    """Test job() iteration over multiple feeds."""

    def setUp(self):
        # Mock logger to capture logs
        self.logger_mock = MagicMock()
        self.logger_patch = patch('news_bot.logger', self.logger_mock)
        self.logger_patch.start()
        # Mock Mattel source so tests stay offline
        self.mattel_patch = patch('news_bot.fetch_mattel_news', return_value=[])
        self.mattel_patch.start()

    def tearDown(self):
        self.logger_patch.stop()
        self.mattel_patch.stop()

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.filter_new_entries')
    @patch('news_bot.process_new_articles')
    def test_job_iterates_over_feeds(self, mock_process, mock_filter, mock_fetch, mock_load):
        """job() should call fetch_rss for each feed URL from load_feeds."""
        # Arrange
        feed_urls = [
            'https://example.com/feed1.xml',
            'https://example.com/feed2.xml',
            'https://example.com/feed3.xml'
        ]
        mock_load.return_value = feed_urls

        # Mock entries for each feed
        mock_fetch.side_effect = [
            [{'link': 'link1', 'title': 'Title1'}],
            [{'link': 'link2', 'title': 'Title2'}],
            [{'link': 'link3', 'title': 'Title3'}]
        ]
        mock_filter.return_value = []  # no new entries after filtering
        mock_process.return_value = 0

        # Act
        job()

        # Assert
        # load_feeds called once
        mock_load.assert_called_once_with()
        # fetch_rss called for each URL
        self.assertEqual(mock_fetch.call_count, 3)
        expected_calls = [call(url) for url in feed_urls]
        mock_fetch.assert_has_calls(expected_calls, any_order=False)
        # filter_new_entries called once with aggregated entries (including feed_url)
        aggregated_entries = [
            {'link': 'link1', 'title': 'Title1', 'feed_url': 'https://example.com/feed1.xml'},
            {'link': 'link2', 'title': 'Title2', 'feed_url': 'https://example.com/feed2.xml'},
            {'link': 'link3', 'title': 'Title3', 'feed_url': 'https://example.com/feed3.xml'}
        ]
        mock_filter.assert_called_once_with(aggregated_entries)
        # process_new_articles called with filtered entries and limit=3
        mock_process.assert_called_once_with([], limit=3)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.filter_new_entries')
    @patch('news_bot.process_new_articles')
    def test_error_isolation(self, mock_process, mock_filter, mock_fetch, mock_load):
        """If one feed fails, others are still processed."""
        feed_urls = ['http://good1.xml', 'http://bad.xml', 'http://good2.xml']
        mock_load.return_value = feed_urls

        # Simulate fetch_rss returning empty list for the failing feed (because it catches exception)
        def fetch_side_effect(url):
            if url == 'http://bad.xml':
                return []  # fetch_rss caught an exception and returns empty list
            return [{'link': f'link_{url}', 'title': 'Title'}]
        mock_fetch.side_effect = fetch_side_effect
        mock_filter.return_value = []
        mock_process.return_value = 0

        job()

        # fetch_rss should have been called for all three URLs
        self.assertEqual(mock_fetch.call_count, 3)
        # filter_new_entries should have received entries from the two successful feeds
        # (the failing feed returns empty list)
        aggregated_entries = [
            {'link': 'link_http://good1.xml', 'title': 'Title', 'feed_url': 'http://good1.xml'},
            {'link': 'link_http://good2.xml', 'title': 'Title', 'feed_url': 'http://good2.xml'}
        ]
        mock_filter.assert_called_once_with(aggregated_entries)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.filter_new_entries')
    @patch('news_bot.process_new_articles')
    def test_global_limit(self, mock_process, mock_filter, mock_fetch, mock_load):
        """Global limit (limit=3) is applied across all feeds."""
        feed_urls = ['http://feed1.xml', 'http://feed2.xml']
        mock_load.return_value = feed_urls
        # Each feed returns 5 entries
        entries = [{'link': f'link{i}', 'title': f'Title{i}'} for i in range(5)]
        mock_fetch.return_value = entries
        # Assume all entries are new
        mock_filter.return_value = entries * 2  # 10 entries total
        mock_process.return_value = 3  # processed 3 articles

        job()

        # process_new_articles should be called with limit=3
        mock_process.assert_called_once_with(entries * 2, limit=3)

    @patch('news_bot.load_feeds')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.filter_new_entries')
    @patch('news_bot.process_new_articles')
    def test_empty_feeds_list_falls_back(self, mock_process, mock_filter, mock_fetch, mock_load):
        """If load_feeds returns empty list, fallback to RSS_URL."""
        mock_load.return_value = []  # empty list -> fallback
        # fetch_rss should be called with RSS_URL
        mock_fetch.return_value = []
        mock_filter.return_value = []
        mock_process.return_value = 0

        job()

        # load_feeds called
        mock_load.assert_called_once()
        # fetch_rss called with RSS_URL
        mock_fetch.assert_called_once_with(RSS_URL)


if __name__ == '__main__':
    unittest.main()