#!/usr/bin/env python3
"""Tests for the hold-and-wait outage contract in ``_fallback_publish``.

Operator decision (2026-06-11): when the LLM transcreation engine is
unavailable (API-level outage), the bot must NOT auto-publish a
low-quality Google machine translation. Instead it HOLDS the article in
``pending_articles`` and retries the LLM on the next slot/day until it
recovers. This replaces the previous degraded-mode Google fallback.

These tests pin the new contract:

* ``ClaudeOutageError`` → ``record_outage_event`` still fires (operator
  pings), Google ``transcreate_text`` is NEVER called, ``publish_article``
  is NEVER called, the error re-raises, and the row stays in pending.
* ``is_fallback_active() == True`` no longer short-circuits to Google —
  the LLM is still attempted (so recovery can be detected).

Per-article ``ClaudeTranscreationError`` behaviour (3-strike → failed) is
unchanged and covered by ``tests/test_fallback_publish_paths.py``.

Fixture mirrors ``tests/test_fallback_publish_paths.py``: tempfile DB,
patched env vars, rows inserted via the real repo.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
from claude_transcreation import ClaudeOutageError


def _sample_entry(link='http://example.com/a', title='Example Article',
                  source='autoevolution', paragraphs=None, subtitle='Lead'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'First paragraph.', 'Second paragraph.',
        ],
        'images': ['http://img/1.jpg'],
        'blocks': None,
        'pub_date': '2026-04-01',
    }


def _claude_dict():
    return {
        'title': '🚀 RU Title',
        'alts': ['alt 1', 'alt 2'],
        'subtitle': 'RU subtitle',
        'paragraphs': ['RU one.', 'RU two.'],
        'blocks': None,
    }


class _HoldCase(unittest.TestCase):
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

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry


class TestOutageHolds(_HoldCase):

    def test_outage_holds_article_no_google_no_publish(self):
        """``ClaudeOutageError`` → hold: outage event recorded (pings),
        Google never called, nothing published, error re-raised, and the
        row remains in ``pending_articles`` for a later retry."""
        entry = self._insert(link='http://a/hold-1')
        row = repo.get_pending(entry['link'])

        mock_claude = MagicMock(side_effect=ClaudeOutageError('429 overloaded'))
        mock_google = MagicMock(side_effect=AssertionError(
            "Google transcreate_text MUST NOT be called on an LLM outage"
        ))
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article MUST NOT be called when the article is held"
        ))
        # Two pings queued this tick — assert BOTH are dispatched (pins the
        # "send every ping in pings_to_send" loop, not just the first).
        mock_record = MagicMock(return_value={
            'pings_to_send': ['[E010] первый пинг', '[E011] второй пинг'],
        })
        mock_notify = MagicMock(return_value=True)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.outage_state.record_outage_event', mock_record), \
             patch('news_bot.send_admin_notification', mock_notify), \
             patch('news_bot.telegraph_publisher.publish_article', mock_publish):
            with self.assertRaises(ClaudeOutageError):
                news_bot._fallback_publish(row, via_review=False)

        mock_claude.assert_called_once()
        mock_google.assert_not_called()
        mock_publish.assert_not_called()
        # Operator notification protocol still advances on outage, and every
        # queued ping is dispatched.
        mock_record.assert_called_once()
        notify_payloads = [c.args[0] for c in mock_notify.call_args_list if c.args]
        self.assertEqual(notify_payloads,
                         ['[E010] первый пинг', '[E011] второй пинг'])
        # Article is NOT lost — it stays in pending for the next slot/day.
        self.assertIsNotNone(repo.get_pending(entry['link']))

    def test_hold_logs_the_cause(self):
        """The ``[hold]`` log line is the ONLY place the cause of a hold is
        recorded, so it is a contract, not a nicety.

        A held row never reaches ``increment_attempt`` (no ``last_error``) and
        never enters the ``[E034]`` recap — both are 'failed'-branch only. The
        remaining operator signal, ``[E010]/[E011]/[E012]``, is generic
        «LLM недоступна» and cannot distinguish an empty OpenRouter balance
        (402) from a dead network. Drop the cause from this line and an
        out-of-credits outage becomes invisible in the journal — which is what
        made the 2026-07-14 loss hard to attribute in the first place.

        The secret in the payload pins the second half of the contract: the
        cause must reach the journal THROUGH ``sanitize_error_message``, not by
        raw interpolation.
        """
        entry = self._insert(link='http://a/hold-cause')
        row = repo.get_pending(entry['link'])

        secret = 'sk-or-v1-hold-cause-canary'
        mock_claude = MagicMock(side_effect=ClaudeOutageError(
            f'APIStatusError: 402 Insufficient credits (key {secret})'
        ))
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': secret}), \
             patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', MagicMock()), \
             patch('news_bot.outage_state.is_fallback_active', return_value=False), \
             patch('news_bot.outage_state.record_outage_event',
                   MagicMock(return_value={'pings_to_send': []})), \
             patch('news_bot.send_admin_notification', MagicMock(return_value=True)), \
             patch('news_bot.telegraph_publisher.publish_article', MagicMock()):
            with self.assertLogs('news_bot', level='WARNING') as logs:
                with self.assertRaises(ClaudeOutageError):
                    news_bot._fallback_publish(row, via_review=False)

        hold_lines = [ln for ln in logs.output if '[hold]' in ln]
        self.assertEqual(len(hold_lines), 1, logs.output)
        # The diagnostic itself — what an operator greps for.
        self.assertIn('402', hold_lines[0])
        self.assertIn('Insufficient credits', hold_lines[0])
        # …and it went through the sanitiser on the way.
        self.assertNotIn(secret, hold_lines[0])
        self.assertIn('[REDACTED]', hold_lines[0])

    def test_fallback_active_does_not_route_to_google(self):
        """A previously-set ``is_fallback_active() == True`` must NOT
        short-circuit to Google. The LLM is still attempted so a recovery
        is detected; on LLM success the article publishes normally."""
        entry = self._insert(link='http://a/hold-2')
        row = repo.get_pending(entry['link'])

        mock_claude = MagicMock(return_value=_claude_dict())
        mock_google = MagicMock(side_effect=AssertionError(
            "Google transcreate_text MUST NOT be called even when "
            "is_fallback_active() == True"
        ))
        mock_recovery = MagicMock(return_value={})

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=True), \
             patch('news_bot.outage_state.record_recovery_event',
                   mock_recovery), \
             patch('news_bot.telegraph_publisher.publish_article',
                   MagicMock(return_value='https://telegra.ph/x')), \
             patch('news_bot.pending_repo.mark_telegraph_published', MagicMock()), \
             patch('news_bot.send_telegraph_teaser', MagicMock(return_value=True)), \
             patch('news_bot.pending_repo.move_to_published', MagicMock()):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        mock_claude.assert_called_once()
        mock_google.assert_not_called()
        # Recovery IS detected on the successful LLM call (the stale outage
        # state would otherwise never clear).
        mock_recovery.assert_called_once()


if __name__ == '__main__':
    unittest.main()
