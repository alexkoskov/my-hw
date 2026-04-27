#!/usr/bin/env python3
"""Tests for the auto-marker / single-line teaser invariants.

Two invariants pinned here, both surviving the Wave-5 deletion of the
legacy throttle / overflow / idle-fallback machinery:

* The ``↳ автоперевод`` paragraph node lives inside the Telegra.ph
  article body (immediately before the ``Источник:`` footer), NOT in
  the channel teaser — see
  ``test_telegraph_publisher.TestAutoMarkerInArticleBody``. The
  channel teaser is single-line for both manual and auto paths
  (Decision 14 of manual-review-workflow — byte-equality at the
  visible-feed level).
* The wiring contract: ``_fallback_publish`` forwards
  ``auto_marker=True`` through to ``publish_article`` for the
  auto-fallback path (``via_review=False``); ``hw_review.cmd_publish``
  must NOT pass the flag (defaults False, no marker).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_entry(link='http://example.com/a', title='Example',
                  source='autoevolution'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': 'Lead',
        'paragraphs': ['Para one.', 'Para two.'],
        'images': [],
        'blocks': None,
        'pub_date': '2026-04-01',
    }


# ---------------------------------------------------------------------------
# Channel teaser is single-line for BOTH paths
# ---------------------------------------------------------------------------


class TestTeaserAlwaysSingleLine(unittest.TestCase):
    """Decision 14 (manual-review-workflow tech-spec) is preserved at
    the visible-channel-feed level: the teaser body is byte-identical
    for manual and auto paths — a single hashtag line. The auto-marker
    moved INTO the Telegra.ph article body (see
    ``test_telegraph_publisher.TestAutoMarkerInArticleBody``)."""

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_manual_teaser_is_single_line(self, mock_bot_class):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        ok = news_bot.send_telegraph_teaser(
            telegraph_url='https://telegra.ph/X',
            source_url='https://autoevolution.com/news/x.html',
        )
        self.assertTrue(ok)

        text = mock_bot.send_message.await_args.kwargs['text']
        self.assertEqual(text, '#autoevolution #news')
        self.assertNotIn('\n', text)
        self.assertNotIn('автоперевод', text)

    @patch('news_bot.TELEGRAM_BOT_TOKEN', 'tok')
    @patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
    @patch('news_bot.Bot')
    def test_teaser_does_not_accept_auto_marker_kwarg(self, mock_bot_class):
        """``send_telegraph_teaser`` no longer takes ``auto_marker`` —
        the marker moved to the Telegra.ph body. Passing it is a TypeError
        (or accepted but ignored — pin the contract: rejected)."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot_class.return_value = mock_bot

        with self.assertRaises(TypeError):
            news_bot.send_telegraph_teaser(
                telegraph_url='https://telegra.ph/X',
                source_url='https://autoevolution.com/news/x.html',
                auto_marker=True,
            )


# ---------------------------------------------------------------------------
# _fallback_publish forwards auto_marker=True to publish_article
# (NOT to the teaser)
# ---------------------------------------------------------------------------


class TestFallbackPublishPassesAutoMarkerToPublishArticle(unittest.TestCase):
    """Inside ``_fallback_publish`` (via_review=False) the
    ``publish_article`` call must propagate ``auto_marker=True`` so
    the Telegra.ph node tree gets the marker paragraph before the
    Источник footer. The channel teaser must NOT receive the kwarg —
    Decision 14 byte-equality at the visible-feed level.

    After Task 8 the idle-overdue path in ``job()`` was deleted, so the
    test now drives ``_fallback_publish`` directly with a fixture row
    rather than going through ``job()``. The contract under test is
    unchanged: kwargs forwarded one way, not the other.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
        self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
        self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()

        # Pin Claude transcreation to a deterministic mock return so the
        # test does not depend on Anthropic credentials or live network.
        # ``news_bot.transcreate_via_claude`` is the local rebinding — see
        # Task 11 patch-target-pinning note in the feature tech-spec.
        self.claude_patcher = patch(
            'news_bot.transcreate_via_claude',
            return_value={
                'title': 'РУ заголовок',
                'subtitle': 'РУ лид',
                'paragraphs': ['РУ абзац 1.', 'РУ абзац 2.'],
                'blocks': None,
            },
        )
        self.claude_patcher.start()

        # Force fallback-inactive so we go through the Claude branch.
        self.outage_patcher = patch(
            'news_bot.outage_state.is_fallback_active', return_value=False,
        )
        self.outage_patcher.start()

    def tearDown(self):
        self.outage_patcher.stop()
        self.claude_patcher.stop()
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_fallback_publish_passes_auto_marker_true_to_publish_article(self):
        # Fixture row: shape mirrors what
        # ``pending_articles_repo.get_pending`` returns to ``job()``'s
        # distributed-publish loop.
        entry = _sample_entry(link='http://example.com/a', title='Example')
        self.assertTrue(repo.insert_pending(entry))
        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)

        mock_teaser = MagicMock(return_value=True)
        mock_publish = MagicMock(return_value='https://telegra.ph/X')

        with patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.send_admin_notification', return_value=True):
            news_bot._fallback_publish(row, via_review=False)

        # publish_article must have been called with auto_marker=True
        # for the auto-fallback path.
        self.assertEqual(mock_publish.call_count, 1)
        kwargs = mock_publish.call_args.kwargs
        self.assertTrue(
            kwargs.get('auto_marker') is True,
            f'expected auto_marker=True forwarded to publish_article, '
            f'got kwargs={kwargs!r}',
        )

        # Teaser was called WITHOUT the marker kwarg (single-line).
        teaser_kwargs = mock_teaser.call_args.kwargs
        self.assertNotIn('auto_marker', teaser_kwargs,
                         'teaser must NOT receive auto_marker — '
                         'the marker lives in the Telegraph body now')


if __name__ == '__main__':
    unittest.main()
