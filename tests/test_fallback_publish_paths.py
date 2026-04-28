#!/usr/bin/env python3
"""Tests for the dual-path translation contract in ``_fallback_publish``.

Task 7 of ``llm-transcreation-and-distributed-publishing`` introduces a
two-tier translation engine in step 1 of ``_fallback_publish``:

* **Primary (Claude):** ``claude_transcreation.transcreate_via_claude``
  is called when ``outage_state.is_fallback_active() == False``.
* **Per-article fallback:** on ``ClaudeTranscreationError`` for THIS
  article only, fall back to the existing ``transcreate_text`` (Google)
  path. The outage state machine is NOT advanced (Decision 5).
* **API-level outage:** on ``ClaudeOutageError`` the state machine has
  already been advanced inside ``claude_transcreation``. The function
  publishes THIS slot's article via Google (degraded mode) AND then
  re-raises ``ClaudeOutageError`` so the upstream ``job()`` loop (Task 8)
  can route subsequent slots through Google without strike-counting.
* **Already-in-fallback shortcut:** when ``is_fallback_active() == True``
  on entry, Claude is NOT called — the row goes straight to Google.

Steps 2–5 (Telegraph publish + ``mark_telegraph_published`` BEFORE
Telegram teaser + ``move_to_published(via_review=False)`` + preview
cleanup) are unchanged per Decision 9 idempotency from
manual-review-workflow. Both branches assert:

* ``via_review=False`` is passed through to ``move_to_published``.
* ``auto_marker=True`` is passed to ``telegraph_publisher.publish_article``
  (computed as ``not via_review`` inside the helper).
* ``mark_telegraph_published`` is invoked BEFORE
  ``send_telegraph_teaser`` — Decision 9 idempotency anchor.

Test layout follows the tempfile-DB pattern of
``tests/test_idle_fallback.py`` (``_IdleFallbackCase`` fixture pattern):
allocate a sqlite file, monkeypatch ``news_bot.DB_FILE``, call
``init_db()``, populate rows via the real repo. Order assertions are
made via ``MagicMock`` ``manager.attach_mock`` (Decision 9 anchor) and
via ``call_order`` lists captured by ``side_effect`` callbacks
(``assert_called_before`` does not exist in ``unittest.mock``).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
from claude_transcreation import (
    ClaudeOutageError,
    ClaudeTranscreationError,
)


def _sample_entry(link='http://example.com/a', title='Example Article',
                  source='autoevolution', paragraphs=None, images=None,
                  blocks=None, subtitle='Lead text'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'First paragraph.', 'Second paragraph.',
        ],
        'images': images if images is not None else ['http://img/1.jpg'],
        'blocks': blocks,
        'pub_date': '2026-04-01',
    }


def _claude_dict(title='🚀 RU Title', subtitle='RU subtitle',
                 paragraphs=None, alts=None, blocks=None):
    """Mimic the dict returned by ``claude_transcreation.transcreate_via_claude``."""
    return {
        'title': title,
        'alts': alts if alts is not None else ['alt 1', 'alt 2'],
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'RU one.', 'RU two.',
        ],
        'blocks': blocks,
    }


class _FallbackPublishPathsCase(unittest.TestCase):
    """Shared fixture: tempfile DB + admin/teaser env vars patched in.

    Monkeypatches ``outage_state.is_fallback_active`` to ``False`` by
    default — individual tests override it when they need the
    already-in-fallback shortcut path.
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


# ============================================================================
# Required tests (TDD anchors)
# ============================================================================


class TestClaudePath(_FallbackPublishPathsCase):

    def test_fallback_publish_claude_path(self):
        """Claude success → ``transcreate_text`` is NOT called; Telegraph
        receives the Claude dict's RU fields; ``mark_telegraph_published``
        runs BEFORE ``send_telegraph_teaser``; ``move_to_published`` is
        called with ``via_review=False``; ``auto_marker=True`` is passed
        to ``publish_article``.
        """
        entry = self._insert(
            link='http://a/1',
            title='EN Title',
            subtitle='EN subtitle',
            paragraphs=['EN one.', 'EN two.'],
        )
        row = repo.get_pending(entry['link'])

        claude_response = _claude_dict(
            title='🚀 RU Title',
            subtitle='RU subtitle',
            paragraphs=['RU one.', 'RU two.'],
        )

        tg_url = 'https://telegra.ph/Claude-04-27'
        # Manager + attach_mock: lets us inspect the global call order
        # across multiple mocks. ``assert_called_before`` does NOT exist
        # in unittest.mock, so we read ``manager.mock_calls`` instead.
        manager = MagicMock()
        mock_claude = MagicMock(return_value=claude_response)
        mock_google = MagicMock(side_effect=AssertionError(
            "Google transcreate_text must NOT be called on the Claude path"
        ))
        mock_publish = MagicMock(return_value=tg_url)
        mock_mark = MagicMock()
        mock_teaser = MagicMock(return_value=True)
        mock_move = MagicMock()
        manager.attach_mock(mock_claude, 'claude')
        manager.attach_mock(mock_publish, 'publish')
        manager.attach_mock(mock_mark, 'mark')
        manager.attach_mock(mock_teaser, 'teaser')
        manager.attach_mock(mock_move, 'move')

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.move_to_published', mock_move):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        # Claude was the engine, Google never invoked.
        mock_claude.assert_called_once()
        mock_google.assert_not_called()

        # Telegraph received Claude RU fields. Claude success → auto_marker
        # MUST be False (no ↳ автоперевод marker for LLM-translated posts;
        # marker now reserved for actual Google-fallback degradations).
        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        self.assertEqual(kwargs['title'], '🚀 RU Title')
        self.assertEqual(kwargs['subtitle'], 'RU subtitle')
        self.assertEqual(kwargs['paragraphs'], ['RU one.', 'RU two.'])
        self.assertEqual(kwargs['source_url'], entry['link'])
        self.assertFalse(kwargs.get('auto_marker'),
                         f"auto_marker must be False on Claude-success path, "
                         f"got {kwargs.get('auto_marker')!r}")

        # Decision 9: mark BEFORE teaser. Read manager.mock_calls and
        # confirm 'mark' index < 'teaser' index. ``mock_calls`` carries
        # ``call.<name>(...)`` entries — ``c[0]`` is the name string.
        names = [c[0] for c in manager.mock_calls]
        self.assertIn('mark', names)
        self.assertIn('teaser', names)
        self.assertLess(names.index('mark'), names.index('teaser'),
                        f"mark_telegraph_published must run BEFORE teaser; got {names}")

        # mark_telegraph_published got the link + Telegraph URL.
        mock_mark.assert_called_once()
        mark_args = mock_mark.call_args.args
        self.assertEqual(mark_args[0], entry['link'])
        self.assertEqual(mark_args[1], tg_url)

        # Teaser sent with the persisted URL.
        mock_teaser.assert_called_once_with(tg_url, entry['link'])

        # Final move with via_review=False.
        mock_move.assert_called_once()
        self.assertEqual(mock_move.call_args.kwargs.get('via_review'), False)


class TestGoogleFallbackPath(_FallbackPublishPathsCase):

    def test_fallback_publish_per_article_failure_retries_then_google(self):
        """``ClaudeTranscreationError`` 3x in a row → admin ping +
        Google fallback for THIS article (variant X' per operator).

        Slot block is bounded by ``_LLM_PER_ARTICLE_RETRY_INTERVAL_S``
        (production: 5 min × 2 retries = 10 min; tests: 0 via conftest).
        After 3 GPT failures the article is still published (through
        Google) so the channel does not lose content; the
        ``↳ автоперевод`` marker on the Telegraph body signals quality
        degradation. Outage state machine is NOT advanced.

        Asserts:
        * transcreate_via_claude was called 3 times (initial + 2 retries)
        * transcreate_text (Google) was called for title + subtitle + paragraphs
        * send_admin_notification was called once with a 3x-failure ping
        * publish_article got auto_marker=True
        * record_outage_event was NOT called
        * the row was moved to published_articles
        """
        entry = self._insert(
            link='http://a/2',
            title='EN Title 2',
            subtitle='EN sub 2',
            paragraphs=['EN one.', 'EN two.'],
        )
        row = repo.get_pending(entry['link'])

        # 3 failures in a row.
        mock_claude = MagicMock(side_effect=[
            ClaudeTranscreationError('malformed JSON #1'),
            ClaudeTranscreationError('malformed JSON #2'),
            ClaudeTranscreationError('malformed JSON #3'),
        ])
        mock_google = MagicMock(side_effect=lambda t, **kw: f"[g] {t}")
        mock_record = MagicMock(side_effect=AssertionError(
            "record_outage_event must NOT fire on per-article failure"
        ))
        mock_admin = MagicMock(return_value=True)

        tg_url = 'https://telegra.ph/Google-after-3-fails-04-28'
        mock_publish = MagicMock(return_value=tg_url)
        mock_mark = MagicMock()
        mock_teaser = MagicMock(return_value=True)
        mock_move = MagicMock()

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.outage_state.record_outage_event', mock_record), \
             patch('news_bot.send_admin_notification', mock_admin), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.move_to_published', mock_move):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        # 3 attempts to GPT.
        self.assertEqual(mock_claude.call_count, 3)
        # Google fired after all 3 GPT failures.
        self.assertGreaterEqual(mock_google.call_count, 4)  # title + subtitle + 2 paragraphs
        # Admin ping fired exactly once with the 3-failure summary.
        mock_admin.assert_called_once()
        ping_text = mock_admin.call_args.args[0]
        self.assertIn('GPT перевод не удался', ping_text)
        # Telegraph published with auto_marker=True (Google fallback was used).
        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        self.assertEqual(kwargs['title'], '[g] EN Title 2')
        self.assertTrue(kwargs.get('auto_marker'))
        # Row was moved to published.
        mock_move.assert_called_once()


# ============================================================================
# Optional but important tests — already-in-fallback shortcut + outage re-raise
# ============================================================================


class TestAlreadyInFallback(_FallbackPublishPathsCase):

    def test_fallback_publish_already_in_fallback(self):
        """``is_fallback_active() == True`` on entry → Claude is NOT called;
        translation goes through Google directly. Downstream chain
        unchanged (Decision 9 ordering)."""
        entry = self._insert(
            link='http://a/3',
            title='EN T3',
            subtitle='EN s3',
            paragraphs=['EN p1.'],
        )
        row = repo.get_pending(entry['link'])

        mock_claude = MagicMock(side_effect=AssertionError(
            "Claude must NOT be called when fallback is already active"
        ))
        mock_google = MagicMock(side_effect=lambda t, **kw: f"[g] {t}")

        tg_url = 'https://telegra.ph/Skip-Claude-04-27'
        mock_publish = MagicMock(return_value=tg_url)
        mock_mark = MagicMock()
        mock_teaser = MagicMock(return_value=True)
        mock_move = MagicMock()

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=True), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.move_to_published', mock_move):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        mock_claude.assert_not_called()
        # Google fired for title + subtitle + paragraph.
        self.assertGreaterEqual(mock_google.call_count, 3)

        mock_publish.assert_called_once()
        kwargs = mock_publish.call_args.kwargs
        self.assertEqual(kwargs['title'], '[g] EN T3')
        self.assertEqual(kwargs['paragraphs'], ['[g] EN p1.'])
        self.assertTrue(kwargs.get('auto_marker'))

        mock_move.assert_called_once()
        self.assertEqual(mock_move.call_args.kwargs.get('via_review'), False)


class TestOutageDegradedThenReraises(_FallbackPublishPathsCase):

    def test_fallback_publish_outage_error_degraded_then_reraises(self):
        """``ClaudeOutageError`` from Claude → ``record_outage_event``
        advances the state machine, admin ping is dispatched, Google
        fallback fires for THIS publication, Steps 2–5 execute fully,
        and THEN ``_fallback_publish`` re-raises ``ClaudeOutageError``
        so ``job()`` (Task 8) can advance its slot-counting loop without
        treating this as a strike.

        Critical contract for Task 8: the article DOES get published
        (degraded via Google) AND the upstream loop still hears the
        outage signal.
        """
        entry = self._insert(
            link='http://a/4',
            title='EN T4',
            subtitle='EN s4',
            paragraphs=['EN p4.'],
        )
        row = repo.get_pending(entry['link'])

        mock_claude = MagicMock(side_effect=ClaudeOutageError(
            'RateLimitError: 429',
        ))
        # Google still translates — degraded mode publishes anyway.
        mock_google = MagicMock(side_effect=lambda t, **kw: f"[g] {t}")

        # Outage state machine + admin ping mocks. ``record_outage_event``
        # returns a dict with ``pings_to_send`` (admin Telegram messages
        # to dispatch) — assert ``send_admin_notification`` is called
        # with each ping.
        mock_record = MagicMock(return_value={
            'state': 'ping_1_sent',
            'pings_to_send': ['⚠️ Claude API недоступна. ...'],
            'fallback_now': False,
        })
        mock_notify = MagicMock(return_value=True)

        tg_url = 'https://telegra.ph/Outage-04-27'
        manager = MagicMock()
        mock_publish = MagicMock(return_value=tg_url)
        mock_mark = MagicMock()
        mock_teaser = MagicMock(return_value=True)
        mock_move = MagicMock()
        manager.attach_mock(mock_claude, 'claude')
        manager.attach_mock(mock_record, 'record')
        manager.attach_mock(mock_notify, 'notify')
        manager.attach_mock(mock_google, 'google')
        manager.attach_mock(mock_publish, 'publish')
        manager.attach_mock(mock_mark, 'mark')
        manager.attach_mock(mock_teaser, 'teaser')
        manager.attach_mock(mock_move, 'move')

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.outage_state.record_outage_event', mock_record), \
             patch('news_bot.send_admin_notification', mock_notify), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.move_to_published', mock_move):
            with self.assertRaises(ClaudeOutageError):
                news_bot._fallback_publish(row, via_review=False)

        # State machine advanced + admin ping fired.
        mock_record.assert_called_once()
        # The single ping returned by record_outage_event was dispatched.
        notify_payloads = [c.args[0] for c in mock_notify.call_args_list
                           if c.args]
        self.assertIn('⚠️ Claude API недоступна. ...', notify_payloads)

        # Critical: Steps 2–5 ran BEFORE the re-raise. The article must
        # have been published in degraded mode.
        mock_publish.assert_called_once()
        mock_mark.assert_called_once()
        mock_teaser.assert_called_once_with(tg_url, entry['link'])
        mock_move.assert_called_once()

        # via_review=False, auto_marker=True preserved.
        kwargs = mock_publish.call_args.kwargs
        self.assertTrue(kwargs.get('auto_marker'))
        self.assertEqual(mock_move.call_args.kwargs.get('via_review'), False)

        # Decision 9 ordering still holds in degraded mode.
        names = [c[0] for c in manager.mock_calls]
        self.assertLess(names.index('mark'), names.index('teaser'),
                        f"mark must run BEFORE teaser even in degraded mode; got {names}")
        # State-machine update + admin ping happened BEFORE Telegraph
        # publish (so operator knows about outage even if publish later
        # fails for unrelated reasons).
        self.assertLess(names.index('record'), names.index('publish'),
                        f"record_outage_event must precede Telegraph publish; got {names}")


if __name__ == '__main__':
    unittest.main()
