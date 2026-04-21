#!/usr/bin/env python3
"""Unit tests for Telegram teaser posting."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import make_teaser, send_telegraph_teaser
from telegram.error import TelegramError


class TestMakeTeaser(unittest.TestCase):
    def test_short_text_returned_whole(self):
        assert make_teaser("One sentence.", max_chars=300) == "One sentence."

    def test_caps_at_max_chars_on_sentence_boundary(self):
        text = "First sentence is short. " + ("x " * 200) + "Last one."
        out = make_teaser(text, max_chars=50)
        # Should stop after the first sentence since the second would exceed cap
        assert out == "First sentence is short."

    def test_takes_multiple_sentences_when_they_fit(self):
        text = "Один. Два. Три."
        out = make_teaser(text, max_chars=300)
        assert out == "Один. Два. Три."

    def test_empty_text(self):
        assert make_teaser("") == ""


class TestSendTelegraphTeaser(unittest.TestCase):

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_sends_message_with_link_preview(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            title='Title',
            teaser='Short teaser text.',
            telegraph_url='https://telegra.ph/X',
            source_url='https://source.example/article',
        )

        self.assertTrue(ok)
        mock_bot_class.assert_called_once_with(token='test_token')
        mock_bot.send_message.assert_awaited_once()
        kwargs = mock_bot.send_message.await_args.kwargs
        self.assertEqual(kwargs['chat_id'], '@channel')
        self.assertEqual(kwargs['parse_mode'], 'Markdown')
        self.assertIn('*Title*', kwargs['text'])
        self.assertIn('Short teaser text.', kwargs['text'])
        self.assertIn('[Читать полностью](https://telegra.ph/X)', kwargs['text'])
        self.assertIn('[Источник](https://source.example/article)', kwargs['text'])
        preview = kwargs['link_preview_options']
        self.assertEqual(preview.url, 'https://telegra.ph/X')
        self.assertTrue(preview.show_above_text)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', None)
    @patch('news_bot.TELEGRAM_CHANNEL_ID', None)
    @patch('news_bot.Bot')
    def test_missing_credentials_returns_false(self, mock_bot_class):
        ok = send_telegraph_teaser('T', 'Te', 'https://telegra.ph/X', 'https://s')
        self.assertFalse(ok)
        mock_bot_class.assert_not_called()

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_telegram_error_returns_false(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=TelegramError('boom'))
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser('T', 'Te', 'https://telegra.ph/X', 'https://s')
        self.assertFalse(ok)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_logs_success(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot
        with self.assertLogs('news_bot', level='INFO') as cm:
            ok = send_telegraph_teaser('Title', 'Te', 'https://telegra.ph/X', 'https://s')
        self.assertTrue(ok)
        self.assertTrue(any('Posted to Telegram: Title' in r.message for r in cm.records))


if __name__ == '__main__':
    unittest.main()
