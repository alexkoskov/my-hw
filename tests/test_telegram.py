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

    @patch('news_bot._fetch_telegraph_og_image', return_value=None)
    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_logs_success_fallback_path(self, mock_bot_class, _mock_og):
        """When the Telegraph article has no og:image, send_telegraph_teaser
        degrades to the single-message preview-only flow and logs
        'Posted to Telegram (fallback): ...'."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot
        with self.assertLogs('news_bot', level='INFO') as cm:
            ok = send_telegraph_teaser(
                telegraph_url='https://telegra.ph/X',
                source_url='https://example.com/a',
            )
        self.assertTrue(ok)
        self.assertTrue(any(
            'Posted to Telegram (fallback): https://telegra.ph/X' in r.message
            for r in cm.records
        ))

    @patch('news_bot._fetch_telegraph_og_image', return_value='https://cdn.telegra.ph/file/img.jpg')
    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'test_token')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@channel')
    @patch('news_bot.Bot')
    def test_variant_c_sends_photo_then_link(self, mock_bot_class, _mock_og):
        """Happy path: og:image found → variant C sends:
          1. ``send_photo`` with the lead image (NO caption — clean hero).
          2. ``send_message`` whose visible text is the hashtag line and
             whose ``LinkPreviewOptions(show_above_text=True)`` renders
             the INSTANT VIEW preview ABOVE the tags. Raw URL must NOT
             appear as visible text."""
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
        mock_bot.send_photo.assert_awaited_once()
        mock_bot.send_message.assert_awaited_once()

        photo_kwargs = mock_bot.send_photo.await_args.kwargs
        self.assertEqual(photo_kwargs['photo'], 'https://cdn.telegra.ph/file/img.jpg')
        # Photo must NOT carry a caption (tags moved to the second message).
        self.assertNotIn('caption', photo_kwargs)

        msg_kwargs = mock_bot.send_message.await_args.kwargs
        # Visible text is the hashtag line, NOT the raw Telegraph URL.
        self.assertEqual(msg_kwargs['text'], '#example #news')
        self.assertNotIn('https://telegra.ph/X', msg_kwargs['text'])
        # Preview above text + URL passed via options (so URL is hidden).
        preview = msg_kwargs['link_preview_options']
        self.assertEqual(preview.url, 'https://telegra.ph/X')
        self.assertTrue(preview.show_above_text)

        self.assertTrue(any(
            'variant C' in r.message for r in cm.records
        ))


if __name__ == '__main__':
    unittest.main()
