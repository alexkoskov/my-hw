#!/usr/bin/env python3
"""Integration tests for the news-bot prep phase.

After manual-review-workflow Task 6, ``news_bot.job()`` stages articles into
the ``pending_articles`` queue and no longer publishes directly to Telegraph
or Telegram. These tests exercise the staging path end-to-end with per-source
fetchers mocked and the DB exercised for real.
"""
import unittest
from unittest.mock import patch
import sqlite3
import tempfile
import os

import news_bot
import pending_articles_repo


class TestIntegration(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()
        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@mock_channel')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()
        # Silence admin notifications by default — individual tests can
        # stop this patch and introspect the mock if they care.
        self.notify_patcher = patch('news_bot.send_admin_notification')
        self.mock_notify = self.notify_patcher.start()
        # Mattel source returns nothing unless a test overrides it.
        self.mattel_patcher = patch('news_bot.fetch_mattel_news', return_value=[])
        self.mattel_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        self.notify_patcher.stop()
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
        """Prep-phase stages every accepted entry into ``pending_articles``
        and makes zero Telegraph / Telegram calls."""
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

        # Prep-phase invariants (Decision 10).
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()
        mock_transcreate.assert_not_called()

        # Every article landed in pending_articles.
        self.assertEqual(pending_articles_repo.count_pending(), 3)
        rows = pending_articles_repo.list_pending()
        staged_links = {r['link'] for r in rows}
        self.assertEqual(staged_links, {
            'http://example.com/article1',
            'http://example.com/article2',
            'http://example.com/article3',
        })
        # EN title / subtitle / paragraphs copied through — no GT applied.
        row = rows[0]
        self.assertEqual(row['title'], 'Article Title')
        self.assertEqual(row['subtitle'], 'Editorial lead')
        self.assertEqual(row['paragraphs'], ['First paragraph.', 'Second paragraph.'])

        # processed_news is untouched in prep-phase — dedup moves to
        # ``published_articles`` via the operator flow, not the cron tick.
        conn = sqlite3.connect(self.db_path)
        processed = conn.execute('SELECT link FROM processed_news').fetchall()
        conn.close()
        self.assertEqual(processed, [])

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
        """A link that appears on two feeds inside one tick lands in
        pending exactly once — via the PRIMARY KEY UNIQUE guard."""
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

        news_bot.job()

        self.assertEqual(pending_articles_repo.count_pending(), 1)
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()

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
        """An empty feed doesn't abort the second one — staged rows
        come only from the successful feed."""
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

        news_bot.job()

        self.assertEqual(mock_fetch_rss.call_count, 2)
        self.assertEqual(mock_fetch_article.call_count, 1)
        self.assertEqual(pending_articles_repo.count_pending(), 1)
        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()

    @patch('news_bot.send_telegraph_teaser')
    @patch('news_bot.telegraph_publisher.publish_article')
    @patch('news_bot.fetch_full_article', return_value=None)
    @patch('news_bot.fetch_rss')
    @patch('news_bot.load_feeds')
    def test_no_article_data_skips_publish(
        self, mock_load_feeds, mock_fetch_rss, mock_fetch_article,
        mock_publish, mock_send_teaser,
    ):
        """If ``fetch_full_article`` returns ``None``, nothing is staged —
        matches the existing skip rule carried over from the removed
        ``process_new_articles`` helper."""
        mock_load_feeds.return_value = ['http://example.com/feed1.xml']
        mock_fetch_rss.return_value = [self._create_mock_entry('http://example.com/article1')]

        news_bot.job()

        mock_publish.assert_not_called()
        mock_send_teaser.assert_not_called()
        self.assertEqual(pending_articles_repo.count_pending(), 0)


if __name__ == '__main__':
    unittest.main()
