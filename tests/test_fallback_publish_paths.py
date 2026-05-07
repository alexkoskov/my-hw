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

    def test_fallback_publish_per_article_failure_re_raises_immediately(self):
        """``ClaudeTranscreationError`` → re-raise so the slot loop
        counts a strike. NO inline retry, NO Google fallback, NO
        admin ping from _fallback_publish itself.

        Operator decision: GPT translates everything; per-article
        hiccups go through the slot-level 3-strike retry (each retry
        on a fresh slot ≥ MIN_INTERVAL_MINUTES later) instead of the
        old inline retry-with-sleep loop. Inline retries blocked the
        slot for 10+ min synchronously and stalled publish-loop pacing.

        Asserts:
        * transcreate_via_claude was called exactly ONCE
        * transcreate_text (Google) was NEVER called
        * publish_article was NEVER called (translation failed)
        * record_outage_event was NEVER called
        * ClaudeTranscreationError propagates to caller
        """
        entry = self._insert(
            link='http://a/2',
            title='EN Title 2',
            subtitle='EN sub 2',
            paragraphs=['EN one.', 'EN two.'],
        )
        row = repo.get_pending(entry['link'])

        mock_claude = MagicMock(
            side_effect=ClaudeTranscreationError('malformed JSON'),
        )
        mock_google = MagicMock(side_effect=AssertionError(
            "transcreate_text (Google) must NOT fire on per-article failure"
        ))
        mock_record = MagicMock(side_effect=AssertionError(
            "record_outage_event must NOT fire on per-article failure"
        ))
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article must NOT fire when translation failed"
        ))

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.outage_state.record_outage_event', mock_record), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish):
            with self.assertRaises(ClaudeTranscreationError):
                news_bot._fallback_publish(row, via_review=False)

        # Single attempt — no inline retries (regression guard).
        self.assertEqual(mock_claude.call_count, 1)


class TestGoogleEnglishGuard(_FallbackPublishPathsCase):
    """When Google Translate returns the source verbatim (403 / blocked
    call), ``_google_translate`` raises ``GoogleTranslationError`` so
    the slot loop strikes the article instead of publishing EN body
    content with an RU-emoji title."""

    def test_pure_english_google_output_raises(self):
        entry = self._insert(
            link='http://a/eng-leak',
            paragraphs=['First English paragraph.', 'Second one.'],
        )
        row = repo.get_pending(entry['link'])

        # Google identity stub — returns the EN input verbatim, mimicking
        # a 403 / blocked translate call.
        mock_google = MagicMock(side_effect=lambda t, **k: t)
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article must NOT fire when Google returned EN-only",
        ))
        # Force the already-in-fallback path so we exercise the Google
        # branch directly without going through Claude.
        with patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=True), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish):
            with self.assertRaises(news_bot.GoogleTranslationError):
                news_bot._fallback_publish(row, via_review=False)


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
        mock_google = MagicMock(side_effect=lambda t, **kw: f"[ру] {t}")

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
        self.assertEqual(kwargs['title'], '[ру] EN T3')
        self.assertEqual(kwargs['paragraphs'], ['[ру] EN p1.'])
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
        mock_google = MagicMock(side_effect=lambda t, **kw: f"[ру] {t}")

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


# ============================================================================
# Idempotency-guard tests (publish-idempotency-fix Task 3, T1–T5)
# ============================================================================


class TestIdempotencyGuard(_FallbackPublishPathsCase):
    """Guard at top of ``_fallback_publish``: if the link is already in
    ``published_articles`` (zombie pending row), short-circuit BEFORE any
    LLM/Telegraph/Telegram side-effect, ping the admin, and clean up via
    ``skip_pending``. See news_bot.py ~line 985 (commit c1a8076).
    """

    def _pre_stage_published(self, link, *, ru_title='РУ',
                             telegraph_url='https://telegra.ph/OLD-URL',
                             telegraph_path='OLD-URL'):
        """Insert a row into ``published_articles`` via raw SQL.

        Pattern from ``tests/test_hw_review_publish_flow.py:271``. We need
        raw SQL (not ``move_to_published``) because the latter DELETEs the
        pending row, and the whole point of the zombie scenario is to have
        the link present in BOTH tables simultaneously.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link, 'EN', ru_title, telegraph_url, telegraph_path,
                 'autoevolution', 0),
            )
            conn.commit()
        finally:
            conn.close()

    def _processed_news_has(self, link):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_news WHERE link=?", (link,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ---- T1 ----------------------------------------------------------------

    def test_skip_if_link_already_published_claude_path(self):
        """Default Claude path: zombie row + already-published link →
        guard fires before any side-effect. Asserts cleanup, return True,
        and the ``[idempotency-guard]`` INFO marker (user-spec AC10).
        """
        link = 'http://a/zombie-claude'
        entry = self._insert(link=link, title='EN T-zombie')
        self._pre_stage_published(link)
        row = repo.get_pending(link)

        # Side-effects MUST NOT fire — wire each as AssertionError.
        mock_claude = MagicMock(side_effect=AssertionError(
            "transcreate_via_claude must NOT fire when guard short-circuits"
        ))
        mock_google = MagicMock(side_effect=AssertionError(
            "transcreate_text must NOT fire when guard short-circuits"
        ))
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article must NOT fire when guard short-circuits"
        ))
        mock_mark = MagicMock(side_effect=AssertionError(
            "mark_telegraph_published must NOT fire when guard short-circuits"
        ))
        mock_teaser = MagicMock(side_effect=AssertionError(
            "send_telegraph_teaser must NOT fire when guard short-circuits"
        ))
        mock_update = MagicMock(side_effect=AssertionError(
            "update_staged must NOT fire when guard short-circuits"
        ))
        mock_move = MagicMock(side_effect=AssertionError(
            "move_to_published must NOT fire when guard short-circuits"
        ))
        mock_notify = MagicMock(return_value=True)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.update_staged', mock_update), \
             patch('news_bot.pending_repo.move_to_published', mock_move), \
             patch('news_bot.send_admin_notification', mock_notify):
            with self.assertLogs('news_bot', level='INFO') as logs:
                ok = news_bot._fallback_publish(row, via_review=False)

        # Return path.
        self.assertTrue(ok)

        # Negative side-effect asserts (positive proof of dominator-position
        # semantics: guard fires before LLM, Telegraph, and bookkeeping).
        mock_claude.assert_not_called()
        mock_google.assert_not_called()
        mock_publish.assert_not_called()
        mock_mark.assert_not_called()
        mock_teaser.assert_not_called()
        mock_update.assert_not_called()
        mock_move.assert_not_called()

        # Cleanup: pending row removed, link in processed_news.
        self.assertIsNone(repo.get_pending(link))
        self.assertTrue(self._processed_news_has(link))

        # AC10 log marker — exactly one INFO entry containing both the
        # marker and the link.
        marker_lines = [
            line for line in logs.output
            if line.startswith('INFO')
            and '[idempotency-guard]' in line
            and link in line
        ]
        self.assertEqual(len(marker_lines), 1,
                         f"expected exactly one [idempotency-guard] INFO line "
                         f"with the link; got {logs.output!r}")

        # Original entry dict was used (sanity).
        self.assertEqual(entry['link'], link)

    # ---- T2 ----------------------------------------------------------------

    def test_skip_if_link_already_published_outage_shortcut_path(self):
        """``is_fallback_active() == True`` shortcut (line 1045) MUST be
        guarded too: the zombie row never reaches the Google branch.
        Regression test against any refactor that places the guard AFTER
        the outage shortcut.
        """
        link = 'http://a/zombie-outage'
        self._insert(link=link, title='EN T-zombie-outage')
        self._pre_stage_published(link)
        row = repo.get_pending(link)

        mock_claude = MagicMock(side_effect=AssertionError(
            "transcreate_via_claude must NOT fire on outage-shortcut guard skip"
        ))
        mock_google = MagicMock(side_effect=AssertionError(
            "transcreate_text must NOT fire on outage-shortcut guard skip"
        ))
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article must NOT fire on outage-shortcut guard skip"
        ))
        mock_mark = MagicMock(side_effect=AssertionError(
            "mark_telegraph_published must NOT fire on outage-shortcut guard skip"
        ))
        mock_teaser = MagicMock(side_effect=AssertionError(
            "send_telegraph_teaser must NOT fire on outage-shortcut guard skip"
        ))
        mock_update = MagicMock(side_effect=AssertionError(
            "update_staged must NOT fire on outage-shortcut guard skip"
        ))
        mock_move = MagicMock(side_effect=AssertionError(
            "move_to_published must NOT fire on outage-shortcut guard skip"
        ))
        mock_notify = MagicMock(return_value=True)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=True), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.update_staged', mock_update), \
             patch('news_bot.pending_repo.move_to_published', mock_move), \
             patch('news_bot.send_admin_notification', mock_notify):
            with self.assertLogs('news_bot', level='INFO') as logs:
                ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        mock_claude.assert_not_called()
        mock_google.assert_not_called()  # Google path also blocked.
        mock_publish.assert_not_called()
        mock_mark.assert_not_called()
        mock_teaser.assert_not_called()
        mock_update.assert_not_called()
        mock_move.assert_not_called()

        self.assertIsNone(repo.get_pending(link))
        self.assertTrue(self._processed_news_has(link))

        marker_lines = [
            line for line in logs.output
            if '[idempotency-guard]' in line and link in line
        ]
        self.assertGreaterEqual(len(marker_lines), 1)

    # ---- T3 ----------------------------------------------------------------

    def test_skip_if_link_already_published_no_telegraph_url(self):
        """Zombie pending row whose ``telegraph_url`` is NULL: guard must
        still short-circuit. Specifically asserts the Telegraph CREATE
        branch (``publish_article``) is NEVER called — the guard sits
        BEFORE Telegraph-create, not just before Telegraph-reuse.
        """
        link = 'http://a/zombie-no-tg'
        # _sample_entry / insert_pending leave telegraph_url=NULL by default.
        self._insert(link=link, title='EN T-zombie-no-tg')
        self._pre_stage_published(link)
        row = repo.get_pending(link)
        # Sanity: pending row really has telegraph_url=NULL.
        self.assertIsNone(row.get('telegraph_url'))

        mock_claude = MagicMock(side_effect=AssertionError(
            "transcreate_via_claude must NOT fire on no-tg-url guard skip"
        ))
        mock_google = MagicMock(side_effect=AssertionError(
            "transcreate_text must NOT fire on no-tg-url guard skip"
        ))
        mock_publish = MagicMock(side_effect=AssertionError(
            "publish_article (Telegraph CREATE) must NOT fire on guard skip"
        ))
        mock_mark = MagicMock(side_effect=AssertionError(
            "mark_telegraph_published must NOT fire on no-tg-url guard skip"
        ))
        mock_teaser = MagicMock(side_effect=AssertionError(
            "send_telegraph_teaser must NOT fire on no-tg-url guard skip"
        ))
        mock_update = MagicMock(side_effect=AssertionError(
            "update_staged must NOT fire on no-tg-url guard skip"
        ))
        mock_move = MagicMock(side_effect=AssertionError(
            "move_to_published must NOT fire on no-tg-url guard skip"
        ))
        mock_notify = MagicMock(return_value=True)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.update_staged', mock_update), \
             patch('news_bot.pending_repo.move_to_published', mock_move), \
             patch('news_bot.send_admin_notification', mock_notify):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        # Specific assertion this test owns: Telegraph CREATE was not called.
        mock_publish.assert_not_called()
        # Plus the rest, for completeness.
        mock_claude.assert_not_called()
        mock_google.assert_not_called()
        mock_mark.assert_not_called()
        mock_teaser.assert_not_called()
        mock_update.assert_not_called()
        mock_move.assert_not_called()

        self.assertIsNone(repo.get_pending(link))
        self.assertTrue(self._processed_news_has(link))

    # ---- T4 ----------------------------------------------------------------

    def test_admin_ping_fires_when_guard_skips(self):
        """Guard must dispatch exactly one admin notification with the
        canonical ``"⚠️ Skipped re-publish of "`` prefix and the link."""
        link = 'http://a/zombie-ping'
        self._insert(link=link, title='EN T-zombie-ping')
        self._pre_stage_published(link)
        row = repo.get_pending(link)

        mock_claude = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_google = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_publish = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_mark = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_teaser = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_update = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_move = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_notify = MagicMock(return_value=True)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.update_staged', mock_update), \
             patch('news_bot.pending_repo.move_to_published', mock_move), \
             patch('news_bot.send_admin_notification', mock_notify):
            ok = news_bot._fallback_publish(row, via_review=False)

        self.assertTrue(ok)
        mock_notify.assert_called_once()
        # ``send_admin_notification(text)`` — first positional arg is the
        # message body. Use assertIn (not assertEqual) per task hint —
        # exact wording may evolve (instance prefix, emoji variants).
        ping_text = mock_notify.call_args.args[0]
        self.assertIn("⚠️ Skipped re-publish of ", ping_text)
        self.assertIn(link, ping_text)

    # ---- T5 ----------------------------------------------------------------

    def test_guard_continues_when_admin_ping_returns_false(self):
        """``send_admin_notification`` returned ``False`` (Telegram down,
        no credentials, etc.). Guard MUST still:
          * call ``skip_pending`` (cleanup runs regardless),
          * return ``True`` (never strike the slot loop just because the
            admin channel is broken),
          * emit a WARNING describing the failed admin ping.
        """
        link = 'http://a/zombie-ping-false'
        self._insert(link=link, title='EN T-zombie-ping-false')
        self._pre_stage_published(link)
        row = repo.get_pending(link)

        mock_claude = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_google = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_publish = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_mark = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_teaser = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_update = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        mock_move = MagicMock(side_effect=AssertionError("guard must short-circuit"))
        # Ping returns False — function must NOT raise, must NOT return False.
        mock_notify = MagicMock(return_value=False)

        with patch('news_bot.transcreate_via_claude', mock_claude), \
             patch('news_bot.transcreate_text', mock_google), \
             patch('news_bot.outage_state.is_fallback_active',
                   return_value=False), \
             patch('news_bot.telegraph_publisher.publish_article',
                   mock_publish), \
             patch('news_bot.pending_repo.mark_telegraph_published',
                   mock_mark), \
             patch('news_bot.send_telegraph_teaser', mock_teaser), \
             patch('news_bot.pending_repo.update_staged', mock_update), \
             patch('news_bot.pending_repo.move_to_published', mock_move), \
             patch('news_bot.send_admin_notification', mock_notify):
            with self.assertLogs('news_bot', level='WARNING') as logs:
                ok = news_bot._fallback_publish(row, via_review=False)

        # Returned True even though ping failed.
        self.assertTrue(ok)
        # Cleanup ran (skip_pending was invoked) — proven via DB state.
        self.assertIsNone(repo.get_pending(link))
        self.assertTrue(self._processed_news_has(link))
        # WARNING log entry mentioning the failed admin ping.
        warn_lines = [
            line for line in logs.output
            if line.startswith('WARNING') and 'admin ping' in line and link in line
        ]
        self.assertGreaterEqual(len(warn_lines), 1,
                                f"expected WARNING about failed admin ping; "
                                f"got {logs.output!r}")


if __name__ == '__main__':
    unittest.main()
