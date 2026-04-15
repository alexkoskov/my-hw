#!/usr/bin/env python3
"""
Unit tests for Telegram posting (send_to_telegram).
"""

import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import send_to_telegram
from telegram.error import TelegramError


class TestSendToTelegram(unittest.TestCase):
    """Test send_to_telegram function."""

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_successful_message_without_images(self, mock_bot_class):
        """send_to_telegram sends message via bot when no images."""
        mock_bot = MagicMock()
        mock_bot_class.return_value = mock_bot

        result = send_to_telegram(
            title='Test Title',
            summary='Test summary.',
            images=[],
            original_link='https://example.com'
        )

        self.assertTrue(result)
        mock_bot_class.assert_called_once_with(token='test_token')
        mock_bot.send_message.assert_called_once_with(
            chat_id='@channel',
            text='*Test Title*\n\nTest summary.\n\nИсточник: [читать оригинал](https://example.com)',
            parse_mode='Markdown'
        )

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_successful_message_with_images(self, mock_bot_class):
        """send_to_telegram sends photo with caption when images present."""
        mock_bot = MagicMock()
        mock_bot_class.return_value = mock_bot

        images = ['http://example.com/image1.jpg', 'http://example.com/image2.jpg']
        result = send_to_telegram(
            title='Test Title',
            summary='Test summary.',
            images=images,
            original_link='https://example.com'
        )

        self.assertTrue(result)
        mock_bot_class.assert_called_once_with(token='test_token')
        mock_bot.send_photo.assert_called_once_with(
            chat_id='@channel',
            photo='http://example.com/image1.jpg',
            caption='*Test Title*\n\nTest summary.\n\nИсточник: [читать оригинал](https://example.com)',
            parse_mode='Markdown'
        )
        # Ensure send_message not called
        mock_bot.send_message.assert_not_called()

    @patch('news_bot.TELEGRAM_BOT_TOKEN', None)
    @patch('news_bot.TELEGRAM_CHANNEL_ID', None)
    @patch('news_bot.Bot')
    def test_missing_credentials(self, mock_bot_class):
        """send_to_telegram returns False and logs error if credentials missing."""
        result = send_to_telegram('Title', 'Summary', [], 'link')
        self.assertFalse(result)
        mock_bot_class.assert_not_called()

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_telegram_error_returns_false(self, mock_bot_class):
        """If Telegram API raises TelegramError, function returns False."""
        mock_bot = MagicMock()
        mock_bot.send_message.side_effect = TelegramError('Telegram error')
        mock_bot_class.return_value = mock_bot

        result = send_to_telegram('Title', 'Summary', [], 'link')
        self.assertFalse(result)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_logging_on_success(self, mock_bot_class):
        """Successful post logs info."""
        mock_bot = MagicMock()
        mock_bot_class.return_value = mock_bot

        with self.assertLogs('news_bot', level='INFO') as cm:
            result = send_to_telegram('Title', 'Summary', [], 'link')
        self.assertTrue(result)
        self.assertTrue(any('Posted to Telegram: Title' in record.message for record in cm.records))

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_logging_on_error(self, mock_bot_class):
        """Telegram error logs error."""
        mock_bot = MagicMock()
        mock_bot.send_message.side_effect = TelegramError('Telegram error')
        mock_bot_class.return_value = mock_bot

        with self.assertLogs('news_bot', level='ERROR') as cm:
            result = send_to_telegram('Title', 'Summary', [], 'link')
        self.assertFalse(result)
        self.assertTrue(any('Telegram error' in record.message for record in cm.records))


if __name__ == '__main__':
    unittest.main()