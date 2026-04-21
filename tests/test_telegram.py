#!/usr/bin/env python3
"""Unit tests for the minimal Telegram channel-post format (send_telegraph_teaser).

Covers the locked format documented in work/telegraph-pipeline/post-format.md —
a single `🔗 [{domain}]({url})` line + `LinkPreviewOptions(show_above_text=True)`
for the Telegraph preview card.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import _source_hashtag, send_telegraph_teaser
from telegram.error import TelegramError


class TestSourceHashtag(unittest.TestCase):
    def test_autoevolution(self):
        self.assertEqual(
            _source_hashtag("https://www.autoevolution.com/news/x.html"),
            "#autoevolution",
        )

    def test_mattel_corporate(self):
        self.assertEqual(
            _source_hashtag("https://corporate.mattel.com/news/x"),
            "#mattel",
        )

    def test_lamley(self):
        self.assertEqual(
            _source_hashtag("https://lamleygroup.com/2026/04/05/post/"),
            "#lamleygroup",
        )

    def test_strips_www_prefix(self):
        self.assertEqual(_source_hashtag("https://www.example.com/"), "#example")


class TestSendTelegraphTeaser(unittest.TestCase):

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_sends_source_hashtag_with_link_preview(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://autoevolution.com/news/article.html',
        )

        self.assertTrue(ok)
        mock_bot_class.assert_called_once_with(token='test_token')
        mock_bot.send_message.assert_awaited_once()
        kwargs = mock_bot.send_message.await_args.kwargs
        self.assertEqual(kwargs['chat_id'], '@channel')
        self.assertEqual(kwargs['parse_mode'], 'Markdown')
        # Body is a single hashtag for the source brand (no URL to the page —
        # tap INSTANT VIEW on the preview card to read).
        self.assertEqual(kwargs['text'], '#autoevolution')
        preview = kwargs['link_preview_options']
        self.assertEqual(preview.url, 'https://telegra.ph/X')
        self.assertTrue(preview.show_above_text)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', None)
    @patch('news_bot.TELEGRAM_CHANNEL_ID', None)
    @patch('news_bot.Bot')
    def test_missing_credentials_returns_false(self, mock_bot_class):
        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://example.com/a',
        )
        self.assertFalse(ok)
        mock_bot_class.assert_not_called()

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_telegram_error_returns_false(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=TelegramError('boom'))
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://example.com/a',
        )
        self.assertFalse(ok)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_logs_success(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot
        with self.assertLogs('news_bot', level='INFO') as cm:
            ok = send_telegraph_teaser(
                telegraph_url='https://telegra.ph/X',
                source_url='https://example.com/a',
            )
        self.assertTrue(ok)
        self.assertTrue(any('Posted to Telegram: https://telegra.ph/X' in r.message for r in cm.records))


if __name__ == '__main__':
    unittest.main()
