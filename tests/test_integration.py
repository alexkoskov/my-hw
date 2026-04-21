#!/usr/bin/env python3
"""Integration tests for the RSS news bot pipeline.

The pipeline is: fetch RSS → translate → publish Telegraph → post channel teaser.
Telegraph publishing and Telegram posting are mocked; DB writes are exercised."""
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
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()
        # Telegram credentials
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.token_patcher.start()
        self.channel_patcher.start()
        # Mattel source stays offline
        self.mattel_patcher = patch('news_bot.fetch_mattel_news', return_value=[])
        self.mattel_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.mattel_patcher.stop()
        os.unlink(self.db_path)

    def _create_mock_entry(self, link, title='Test Article', published='2025-01-01'):
        return {
            'link': link,
            'title': title,
            'published': published,
            'summary': 'Summary',
        }

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_full_pipeline_with_multiple_feeds(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate,
        mock_publish,
        mock_send_teaser,
    ):
        """Pipeline translates body, publishes to Telegraph, posts teaser per article."""
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [
                self._create_mock_entry('http://example.com/article1'),
                self._create_mock_entry('http://example.com/article2'),
            ],
            [
                self._create_mock_entry('http://example.com/article3'),
            ],
        ]
        mock_fetch_article.return_value = {
            'title': 'Article Title',
            'text': 'First paragraph.\nSecond paragraph.',
            'images': ['http://example.com/image.jpg'],
        }
        mock_transcreate.side_effect = lambda t, **k: f'RU:{t}'
        mock_publish.return_value = 'https://telegra.ph/page'
        mock_send_teaser.return_value = True

        news_bot.job()

        mock_load_feeds.assert_called_once()
        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 3)
        # publish_article called once per article with translated inputs
        self.assertEqual(mock_publish.call_count, 3)
        first_call = mock_publish.call_args_list[0]
        self.assertEqual(first_call.kwargs['title'], 'RU:Article Title')
        self.assertEqual(first_call.kwargs['paragraphs'],
                         ['RU:First paragraph.', 'RU:Second paragraph.'])
        self.assertEqual(first_call.kwargs['images'], ['http://example.com/image.jpg'])
        self.assertEqual(first_call.kwargs['source_url'], 'http://example.com/article1')

        # Teaser sent with Telegraph URL and source
        self.assertEqual(mock_send_teaser.call_count, 3)
        for call_args in mock_send_teaser.call_args_list:
            args = call_args.args
            self.assertEqual(args[0], 'RU:Article Title')  # title
            self.assertEqual(args[2], 'https://telegra.ph/page')  # telegraph_url
            self.assertTrue(args[3].startswith('http://example.com/article'))

        # DB persistence
        conn = sqlite3.connect(self.db_path)
        processed_links = {row[0] for row in conn.execute('SELECT link FROM processed_news').fetchall()}
        conn.close()
        self.assertEqual(processed_links, {
            'http://example.com/article1',
            'http://example.com/article2',
            'http://example.com/article3',
        })

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_duplicate_skipping(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate,
        mock_publish,
        mock_send_teaser,
    ):
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [self._create_mock_entry('http://example.com/article1')],
            [self._create_mock_entry('http://example.com/article1')],  # dup
        ]
        mock_fetch_article.return_value = {'title': 'T', 'text': 'Body.', 'images': []}
        mock_transcreate.side_effect = lambda t, **k: t
        mock_publish.return_value = 'https://telegra.ph/x'
        mock_send_teaser.return_value = True

        news_bot.job()

        self.assertEqual(mock_fetch_article.call_count, 1)
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_send_teaser.call_count, 1)

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_error_isolation(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate,
        mock_publish,
        mock_send_teaser,
    ):
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {'title': 'T', 'text': 'Body.', 'images': []}
        mock_transcreate.side_effect = lambda t, **k: t
        mock_publish.return_value = 'https://telegra.ph/x'
        mock_send_teaser.return_value = True

        news_bot.job()

        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 1)
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_send_teaser.call_count, 1)

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_telegraph_failure_skips_teaser_and_db_mark(
        self,
        mock_load_feeds,
        mock_fetch_rss,
        mock_fetch_article,
        mock_transcreate,
        mock_publish,
        mock_send_teaser,
    ):
        """If Telegraph publish fails, teaser is skipped and article is NOT marked processed."""
        from telegraph_publisher import TelegraphError
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._create_mock_entry('http://example.com/article1')]
        mock_fetch_article.return_value = {'title': 'T', 'text': 'Body.', 'images': []}
        mock_transcreate.side_effect = lambda t, **k: t
        mock_publish.side_effect = TelegraphError('API down')
        mock_send_teaser.return_value = True

        news_bot.job()

        mock_send_teaser.assert_not_called()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute('SELECT 1 FROM processed_news').fetchall()
        conn.close()
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
