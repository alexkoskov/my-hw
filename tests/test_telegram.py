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
        # Body is the source brand hashtag + the static `#news` tag (no URL
        # to the page — tap INSTANT VIEW on the preview card to read).
        self.assertEqual(kwargs['text'], '#autoevolution #news')
        preview = kwargs['link_preview_options']
        self.assertEqual(preview.url, 'https://telegra.ph/X')
        self.assertTrue(preview.show_above_text)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_mattel_teaser_appends_news_tag(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://corporate.mattel.com/news/x',
        )
        self.assertTrue(ok)
        kwargs = mock_bot.send_message.await_args.kwargs
        self.assertEqual(kwargs['text'], '#mattel #news')

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_lamley_teaser_appends_news_tag(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://lamleygroup.com/2026/04/05/post/',
        )
        self.assertTrue(ok)
        kwargs = mock_bot.send_message.await_args.kwargs
        self.assertEqual(kwargs['text'], '#lamleygroup #news')

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_unknown_source_does_not_emit_bare_news_tag(self, mock_bot_class):
        """Edge case: empty/malformed source_url makes `_source_hashtag`
        return the bare `#` (no label). The teaser must NOT emit just
        `#news` alone — fall back to the legacy bare hashtag instead."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='',
        )
        self.assertTrue(ok)
        kwargs = mock_bot.send_message.await_args.kwargs
        self.assertNotEqual(kwargs['text'], '#news')
        self.assertNotIn('#news', kwargs['text'])

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
    def test_iv_preview_only_layout(self, mock_bot_class):
        """``send_telegraph_teaser`` sends ONE message:
          - text = hashtag line (#source #news), NOT the raw URL.
          - link_preview_options.url carries the Telegraph URL.
          - show_above_text=True puts the IV card ABOVE the tags.
          - prefer_large_media=True forces the IV preview to render
            with a full-width image instead of a small thumbnail.
        No ``send_photo`` — the IV card is the only visual.
        """
        mock_bot = MagicMock()
        mock_bot.send_photo = AsyncMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot
        with self.assertLogs('news_bot', level='INFO') as cm:
            ok = send_telegraph_teaser(
                telegraph_url='https://telegra.ph/X',
                source_url='https://example.com/a',
            )
        self.assertTrue(ok)
        mock_bot.send_photo.assert_not_called()
        mock_bot.send_message.assert_awaited_once()

        msg_kwargs = mock_bot.send_message.await_args.kwargs
        self.assertEqual(msg_kwargs['text'], '#example #news')
        self.assertNotIn('https://telegra.ph/X', msg_kwargs['text'])
        preview = msg_kwargs['link_preview_options']
        self.assertEqual(preview.url, 'https://telegra.ph/X')
        self.assertTrue(preview.show_above_text)
        self.assertTrue(preview.prefer_large_media)

        self.assertTrue(any(
            'Posted to Telegram: https://telegra.ph/X' in r.message
            for r in cm.records
        ))


if __name__ == '__main__':
    unittest.main()
