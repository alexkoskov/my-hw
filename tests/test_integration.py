#!/usr/bin/env python3
"""Integration tests for the news bot pipeline.

Pipeline: feed → `fetch_full_article` (dispatched per source) →
translate paragraphs → publish Telegraph → post channel teaser.
Telegraph publishing and Telegram posting are mocked; DB writes are
exercised for real.
"""
import unittest
from unittest.mock import patch
import sqlite3
import tempfile
import os

import news_bot


class TestIntegration(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.token_patcher.start()
        self.channel_patcher.start()
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
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_full_pipeline_with_multiple_feeds(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [
                self._create_mock_entry('http://example.com/article1'),
                self._create_mock_entry('http://example.com/article2'),
            ],
            [self._create_mock_entry('http://example.com/article3')],
        ]
        mock_fetch_article.return_value = {
            'title': 'Article Title',
            'subtitle': 'Editorial lead',
            'paragraphs': ['First paragraph.', 'Second paragraph.'],
            'images': ['http://example.com/image.jpg'],
        }
        mock_transcreate.side_effect = lambda t, **k: f'RU:{t}'
        mock_publish.return_value = 'https://telegra.ph/page'
        mock_send_teaser.return_value = True

        news_bot.job()

        mock_load_feeds.assert_called_once()
        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 3)
        self.assertEqual(mock_publish.call_count, 3)
        first = mock_publish.call_args_list[0]
        self.assertEqual(first.kwargs['title'], 'RU:Article Title')
        self.assertEqual(first.kwargs['subtitle'], 'RU:Editorial lead')
        self.assertEqual(first.kwargs['paragraphs'],
                         ['RU:First paragraph.', 'RU:Second paragraph.'])
        self.assertEqual(first.kwargs['images'], ['http://example.com/image.jpg'])
        self.assertEqual(first.kwargs['source_url'], 'http://example.com/article1')

        # New signature: send_telegraph_teaser(telegraph_url, source_url)
        self.assertEqual(mock_send_teaser.call_count, 3)
        for call_args in mock_send_teaser.call_args_list:
            args = call_args.args
            self.assertEqual(args[0], 'https://telegra.ph/page')
            self.assertTrue(args[1].startswith('http://example.com/article'))

        conn = sqlite3.connect(self.db_path)
        processed = {r[0] for r in conn.execute('SELECT link FROM processed_news').fetchall()}
        conn.close()
        self.assertEqual(processed, {
            'http://example.com/article1',
            'http://example.com/article2',
            'http://example.com/article3',
        })

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.transcreate_text')
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_duplicate_skipping(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [self._create_mock_entry('http://example.com/article1')],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': []
        }
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
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_error_isolation(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        mock_load_feeds.return_value = [
            'http://example.com/feed1.xml',
            'http://example.com/feed2.xml',
        ]
        mock_fetch_rss.side_effect = [
            [],
            [self._create_mock_entry('http://example.com/article1')],
        ]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': []
        }
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
    @patch('news_bot.fetch_full_article')
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_telegraph_failure_skips_teaser_and_db(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_transcreate, mock_publish, mock_send_teaser,
    ):
        from telegraph_publisher import TelegraphError
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._create_mock_entry('http://example.com/article1')]
        mock_fetch_article.return_value = {
            'title': 'T', 'subtitle': '', 'paragraphs': ['Body.'], 'images': []
        }
        mock_transcreate.side_effect = lambda t, **k: t
        mock_publish.side_effect = TelegraphError('API down')
        mock_send_teaser.return_value = True

        news_bot.job()

        mock_send_teaser.assert_not_called()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute('SELECT 1 FROM processed_news').fetchall()
        conn.close()
        self.assertEqual(rows, [])

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article', return_value=None)
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_no_article_data_skips_publish(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_publish, mock_send_teaser,
    ):
        """If fetch_full_article returns None, nothing is published."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._create_mock_entry('http://example.com/article1')]

        news_bot.job()

        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
