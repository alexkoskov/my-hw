#!/usr/bin/env python3
"""
Integration tests for the RSS news bot pipeline.
Uses mock RSS feeds (via patched feedparser) and mock Telegram API.
"""
import unittest
from unittest.mock import patch, MagicMock, call
import sqlite3
import tempfile
import os

import news_bot


class TestIntegration(unittest.TestCase):
    """Integration tests for the full pipeline."""

    def setUp(self):
        # Create a temporary database file
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        # Patch DB_FILE before any database function is called
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        # Initialize the database
        news_bot.init_db()
        # Mock the telegram credentials to avoid real API calls
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.token_patcher.start()
        self.channel_patcher.start()

    def tearDown(self):
        # Stop all patches
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        # Remove temporary database file
        os.unlink(self.db_path)

    def _create_mock_entry(self, link, title='Test Article', published='2025-01-01'):
        """Helper to create a mock RSS entry as returned by feedparser."""
        return {
            'link': link,
            'title': title,
            'published': published,
            'summary': 'Summary',
        }

    @patch('news_bot.send_to_telegram')
    @patch('news_bot.summarize_text_with_limit')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_full_pipeline_with_multiple_feeds(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate_text,
        mock_summarize_text_with_limit,
        mock_send_to_telegram,
    ):
        """
        Test that the pipeline processes articles from multiple feeds,
        respects the global limit, and calls Telegram with expected arguments.
        """
        # Configure mocks
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        # Feed 1 returns two entries
        mock_fetch_rss.side_effect = [
            [   # first feed
                self._create_mock_entry('http://example.com/article1'),
                self._create_mock_entry('http://example.com/article2'),
            ],
            [   # second feed
                self._create_mock_entry('http://example.com/article3'),
            ],
        ]
        # All articles are fetchable
        mock_fetch_article.return_value = {
            'title': 'Article Title',
            'text': 'Article content with several sentences. Second sentence.',
            'images': ['http://example.com/image.jpg'],
        }
        mock_transcreate_text.return_value = 'Translated text'
        mock_summarize_text_with_limit.return_value = 'Summarized content'
        mock_send_to_telegram.return_value = True

        # Run the job
        news_bot.job()

        # Verify load_feeds was called
        mock_load_feeds.assert_called_once()

        # Verify fetch_rss called for each feed URL
        self.assertEqual(mock_fetch_rss.call_count, 2)
        mock_fetch_rss.assert_has_calls([
            call('http://example.com/feed1.xml'),
            call('http://example.com/feed2.xml'),
        ])

        # fetch_article should be called for each unique entry (3 articles)
        self.assertEqual(mock_fetch_article.call_count, 3)
        # transcreate_text called twice per article (title + text), summarize_text_with_limit once
        self.assertEqual(mock_transcreate_text.call_count, 6)
        self.assertEqual(mock_summarize_text_with_limit.call_count, 3)

        # send_to_telegram should be called for each article (global limit is 3, we have 3)
        self.assertEqual(mock_send_to_telegram.call_count, 3)
        # Check that send_to_telegram received expected arguments (simplified)
        mock_send_to_telegram.assert_has_calls([
            call(
                'Translated text',
                'Translated text',
                ['http://example.com/image.jpg'],
                'http://example.com/article1',
            ),
            call(
                'Translated text',
                'Translated text',
                ['http://example.com/image.jpg'],
                'http://example.com/article2',
            ),
            call(
                'Translated text',
                'Translated text',
                ['http://example.com/image.jpg'],
                'http://example.com/article3',
            ),
        ], any_order=True)

        # Verify articles are marked as processed in the database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT link FROM processed_news')
        processed_links = {row[0] for row in cursor.fetchall()}
        conn.close()
        expected_links = {
            'http://example.com/article1',
            'http://example.com/article2',
            'http://example.com/article3',
        }
        self.assertEqual(processed_links, expected_links)

    @patch('news_bot.send_to_telegram')
    @patch('news_bot.summarize_text_with_limit')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_duplicate_skipping(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate_text,
        mock_summarize_text_with_limit,
        mock_send_to_telegram,
    ):
        """
        Ensure duplicate entries (same link) are not processed twice.
        """
        # Two feeds, both contain the same article link
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [self._create_mock_entry('http://example.com/article1')],
            [self._create_mock_entry('http://example.com/article1')],  # duplicate
        ]
        mock_fetch_article.return_value = {
            'title': 'Title',
            'text': 'Content',
            'images': []
        }
        mock_transcreate_text.return_value = 'Translated text'
        mock_summarize_text_with_limit.return_value = 'Summary'
        mock_send_to_telegram.return_value = True

        news_bot.job()

        # fetch_article should be called only once (duplicate skipped)
        self.assertEqual(mock_fetch_article.call_count, 1)
        # send_to_telegram should be called only once
        self.assertEqual(mock_send_to_telegram.call_count, 1)

    @patch('news_bot.send_to_telegram')
    @patch('news_bot.summarize_text_with_limit')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_error_isolation(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate_text,
        mock_summarize_text_with_limit,
        mock_send_to_telegram,
    ):
        """
        If one feed fails, the other feeds are still processed.
        """
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        # First feed returns empty list (simulating error), second returns entries
        mock_fetch_rss.side_effect = [
            [],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {
            'title': 'Title',
            'text': 'Content',
            'images': []
        }
        mock_transcreate_text.return_value = 'Translated text'
        mock_summarize_text_with_limit.return_value = 'Summary'
        mock_send_to_telegram.return_value = True

        # Job should not crash; error should be logged
        news_bot.job()

        # fetch_rss called for both feeds
        self.assertEqual(mock_fetch_rss.call_count, 2)
        # fetch_article called for the successful feed's entry
        self.assertEqual(mock_fetch_article.call_count, 1)
        # send_to_telegram called once
        self.assertEqual(mock_send_to_telegram.call_count, 1)


if __name__ == '__main__':
    unittest.main()