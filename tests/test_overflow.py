#!/usr/bin/env python3
"""Integration + unit tests for the overflow fast-track pass in
``news_bot.job()`` and the ``_overflow_fast_track`` helper — Task 10
of manual-review-workflow.

Covers tech-spec §Prep-phase step 4 + Decisions 6 (QUEUE_CAP), 7 (staged
rows never evicted), 9 (telegraph URL reuse), 11 (sanitised last_error),
13 (shared attempt counter):

* Early return when ``len(new_entries) <= slots_free`` — ``_fallback_publish``
  untouched, caller sees all new entries in ``accepted``.
* Eviction picks only ``ru_paragraphs IS NULL`` rows (repo contract of
  ``list_pending_for_eviction``) in ``fetched_at ASC`` order.
* ``_fallback_publish`` path — on success: pending row moves to
  ``published_articles`` with ``via_review=False``; slot frees.
* On ``_fallback_publish`` raise → ``increment_attempt`` bumps the shared
  counter; third strike in any combination → ``move_to_failed``.
* Admin-ping format (byte-for-byte): ``"Queue pressure: auto-published
  {E}, {D} new deferred, {S} staged rows protected"`` + optional suffix
  ``", fast-track failed for {F}"`` when ``fast_track_errors`` non-empty.
* All staged → zero eviction, all new entries deferred, ping cites
  ``staged_protected`` = N.
* Happy-path with new entries fully accepted when within cap → no admin
  ping fired by this pass.

Follows the tempfile-DB + ``_patch_sources`` pattern of
``tests/test_idle_fallback.py`` and ``tests/test_job_prep_phase.py``.
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


def _sample_entry(link='http://example.com/a', title='Example',
                  source='autoevolution', paragraphs=None, images=None,
                  blocks=None, subtitle='Lead'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': subtitle,
        'paragraphs': paragraphs if paragraphs is not None else [
            'First paragraph.', 'Second paragraph.',
        ],
        'images': images if images is not None else [],
        'blocks': blocks,
        'pub_date': '2026-04-01',
    }


def _rss_entry(link, title='Title', source_name='autoevolution'):
    """Shape of entries returned by the SOURCES registry — identical to
    ``_fetch_rss_entries`` output (tech-spec §9.5)."""
    return {
        'link': link,
        'title': title,
        'published': '2026-04-22',
        'summary': '',
        'feed_url': 'http://example.com/feed.xml',
        'source_name': source_name,
    }


def _article_payload(title='Title'):
    """Shape returned by ``fetch_full_article`` for a non-Mattel entry."""
    return {
        'title': title,
        'subtitle': 'Editorial lead',
        'paragraphs': ['Para one.', 'Para two.'],
        'images': ['https://example.com/img.jpg'],
    }


class _OverflowCase(unittest.TestCase):
    """Shared tempfile-DB fixture + admin-credential patches so the
    admin-ping code paths don't short-circuit."""

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

        # Default QUEUE_CAP to a known value so tests don't depend on env.
        self.cap_patcher = patch('news_bot.QUEUE_CAP', 10)
        self.cap_patcher.start()

    def tearDown(self):
        self.cap_patcher.stop()
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _insert_staged(self, **kw):
        """Insert a row and immediately populate its ru_* fields so it's
        ineligible for eviction (Decision 7)."""
        entry = self._insert(**kw)
        repo.update_staged(
            entry['link'],
            'РУ заголовок',
            'РУ лид',
            ['Абзац один.', 'Абзац два.'],
            None,
        )
        return entry

    def _age_fetched(self, link, hours):
        """SQL-UPDATE so ``fetched_at`` is ``-{hours}`` old — lets us
        control the eviction order without Python time-mocks."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET fetched_at = "
                "datetime('now', ? || ' hours') WHERE link = ?",
                (f"-{int(hours)}", link),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_attempt_count(self, link, n):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE pending_articles SET attempt_count = ? WHERE link = ?",
                (int(n), link),
            )
            conn.commit()
        finally:
            conn.close()


# ============================================================================
# Direct ``_overflow_fast_track`` helper tests (no ``job()`` wrapper)
# ============================================================================


class TestOverflowHelper(_OverflowCase):

    def test_overflow_early_return_when_slots_available(self):
        """``slots_free = QUEUE_CAP - count_pending()`` covers all new
        entries → early return ``(new_entries, [])`` and
        ``list_pending_for_eviction`` is never called."""
        # Pre-fill 5 unstaged rows (eviction candidates) + arrival of 3 new.
        for i in range(5):
            self._insert(link=f'http://a/{i}', title=f'A{i}')

        new_entries = [
            _rss_entry('http://new/1', 'N1'),
            _rss_entry('http://new/2', 'N2'),
            _rss_entry('http://new/3', 'N3'),
        ]

        spy = MagicMock(wraps=repo.list_pending_for_eviction)
        notify = MagicMock()
        with patch('news_bot.pending_repo.list_pending_for_eviction', spy), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        spy.assert_not_called()
        self.assertEqual(accepted, new_entries)
        self.assertEqual(errors, [])
        notify.assert_not_called()

    def test_overflow_empty_new_entries_is_noop(self):
        """Empty ``new_entries`` list → early return, no eviction, no ping."""
        for i in range(5):
            self._insert(link=f'http://a/{i}', title=f'A{i}')

        spy = MagicMock(wraps=repo.list_pending_for_eviction)
        notify = MagicMock()
        with patch('news_bot.pending_repo.list_pending_for_eviction', spy), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track([])

        spy.assert_not_called()
        self.assertEqual(accepted, [])
        self.assertEqual(errors, [])
        notify.assert_not_called()

    def test_overflow_evicts_only_unstaged_rows(self):
        """Queue = 10 (5 staged + 5 unstaged), 3 new entries → exactly 3
        unstaged rows (oldest first) go through ``_fallback_publish``;
        staged rows stay in pending untouched."""
        # Seed 5 staged rows, ages 50/49/48/47/46 (older → newer).
        for i, hours in enumerate([50, 49, 48, 47, 46]):
            link = f'http://staged/{i}'
            self._insert_staged(link=link, title=f'Staged {i}')
            self._age_fetched(link, hours)

        # Seed 5 unstaged rows, ages 40/39/38/37/36 (older → newer). These
        # are NEWER than the staged rows but are the only eviction-eligible
        # ones per Decision 7.
        for i, hours in enumerate([40, 39, 38, 37, 36]):
            link = f'http://open/{i}'
            self._insert(link=link, title=f'Open {i}')
            self._age_fetched(link, hours)

        self.assertEqual(repo.count_pending(), 10)

        new_entries = [
            _rss_entry('http://new/1', 'N1'),
            _rss_entry('http://new/2', 'N2'),
            _rss_entry('http://new/3', 'N3'),
        ]

        mock_fallback = MagicMock(return_value=True)
        # Mock move_to_published since we're bypassing the real helper.
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', return_value=True):
            # When _fallback_publish is mocked it doesn't move the row;
            # emulate that side-effect here so count_pending goes down.
            def side(row, via_review=False):
                # Populate ru_title so the NOT NULL constraint on
                # published_articles is satisfied, then move the row.
                repo.update_staged(
                    row['link'],
                    'ru-' + (row.get('title') or ''),
                    '',
                    ['ru-auto'],
                    None,
                )
                repo.move_to_published(
                    row['link'],
                    'https://telegra.ph/fake-01-01',
                    'fake-01-01',
                    via_review=via_review,
                )
                return True
            mock_fallback.side_effect = side

            accepted, errors = news_bot._overflow_fast_track(new_entries)

        # Three OLDEST unstaged rows were evicted: open/0, open/1, open/2.
        self.assertEqual(mock_fallback.call_count, 3)
        evicted_links = [c.args[0]['link'] for c in mock_fallback.call_args_list]
        self.assertEqual(
            evicted_links,
            ['http://open/0', 'http://open/1', 'http://open/2'],
        )

        # via_review=False on each call.
        for c in mock_fallback.call_args_list:
            self.assertFalse(c.kwargs.get('via_review', True))

        # Staged rows still present.
        for i in range(5):
            self.assertIsNotNone(repo.get_pending(f'http://staged/{i}'))

        # All three new entries accepted now that 3 slots freed.
        self.assertEqual(len(accepted), 3)
        self.assertEqual(errors, [])

    def test_overflow_staged_protected_count_in_admin_ping(self):
        """Queue full of staged rows + 3 new → eviction returns 0 rows;
        ``staged_protected=3``, ``deferred=3``, admin ping byte-exact."""
        for i in range(10):
            self._insert_staged(link=f'http://s/{i}', title=f'S{i}')

        new_entries = [
            _rss_entry('http://new/1', 'N1'),
            _rss_entry('http://new/2', 'N2'),
            _rss_entry('http://new/3', 'N3'),
        ]

        notify = MagicMock(return_value=True)
        mock_fallback = MagicMock()
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        mock_fallback.assert_not_called()
        self.assertEqual(accepted, [])
        self.assertEqual(errors, [])

        # Find the overflow ping string. Should carry the Queue-pressure
        # format exactly (Task 10 spec).
        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1,
                         f"Expected exactly one overflow ping, got: {notify.call_args_list}")
        text = overflow_calls[0].args[0]
        self.assertEqual(
            text,
            'Queue pressure: auto-published 0, 3 new deferred, '
            '3 staged rows protected',
        )

    def test_overflow_failure_increments_shared_attempt(self):
        """``_fallback_publish`` raises on an evicted row → ``attempt_count``
        bumped via ``increment_attempt`` (Decision 13 shared counter);
        row stays pending."""
        # Fill to cap with unstaged rows.
        for i in range(10):
            self._insert(link=f'http://o/{i}', title=f'O{i}')
            self._age_fetched(f'http://o/{i}', 40 - i)  # oldest first: o/0

        new_entries = [_rss_entry('http://new/1', 'N1')]

        mock_fallback = MagicMock(side_effect=RuntimeError('boom'))
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', return_value=True):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        # One eviction attempt; the RuntimeError leaves the row pending
        # with attempt_count bumped to 1.
        self.assertEqual(mock_fallback.call_count, 1)
        row = repo.get_pending('http://o/0')
        self.assertIsNotNone(row)
        self.assertEqual(row['attempt_count'], 1)
        self.assertIsNotNone(row['last_error'])
        self.assertIn('boom', row['last_error'])

        # Slot was NOT freed → the new entry is deferred.
        self.assertEqual(accepted, [])
        # fast_track_errors list carries the row's title for the ping.
        self.assertEqual(len(errors), 1)

    def test_overflow_three_strikes_moves_to_failed(self):
        """Row with ``attempt_count=2`` and a failing ``_fallback_publish``
        crosses the 3-strike threshold → ``move_to_failed``; slot freed."""
        for i in range(10):
            self._insert(link=f'http://o/{i}', title=f'O{i}')
            self._age_fetched(f'http://o/{i}', 40 - i)

        # Bump the oldest row to 2 attempts already.
        self._set_attempt_count('http://o/0', 2)

        new_entries = [_rss_entry('http://new/1', 'N1')]

        mock_fallback = MagicMock(side_effect=RuntimeError('third strike'))
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', return_value=True):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        # o/0 moved to failed; no longer in pending.
        self.assertIsNone(repo.get_pending('http://o/0'))
        fail = repo.get_failed('http://o/0')
        self.assertIsNotNone(fail)
        self.assertIn('third strike', fail['last_error'])

        # One slot freed → the new entry is accepted.
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['link'], 'http://new/1')

    def test_overflow_deferred_admin_ping_format(self):
        """Admin ping on deferred-only path (no errors, some eviction
        slots couldn't be freed). Byte-exact match."""
        # Seed: 7 staged + 3 unstaged → cap=10. Five new entries arrive.
        # needed = 5 - 0 = 5; to_evict returns 3 unstaged; staged_protected
        # = 2; after pass queue=7 (staged) + 3 new = 10; deferred = 2.
        for i in range(7):
            self._insert_staged(link=f'http://s/{i}', title=f'S{i}')
        for i in range(3):
            link = f'http://o/{i}'
            self._insert(link=link, title=f'O{i}')
            self._age_fetched(link, 30 - i)

        new_entries = [
            _rss_entry(f'http://new/{i}', f'N{i}') for i in range(5)
        ]

        notify = MagicMock(return_value=True)

        def fallback_side(row, via_review=False):
            # Populate ru_title so the NOT NULL constraint on
            # published_articles is satisfied, then move the row.
            repo.update_staged(
                row['link'],
                'ru-' + (row.get('title') or ''),
                '',
                ['ru-auto'],
                None,
            )
            repo.move_to_published(
                row['link'],
                'https://telegra.ph/fake-01-01',
                'fake-01-01',
                via_review=via_review,
            )
            return True

        with patch('news_bot._fallback_publish', side_effect=fallback_side), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        self.assertEqual(len(accepted), 3)

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1)
        text = overflow_calls[0].args[0]
        self.assertEqual(
            text,
            'Queue pressure: auto-published 3, 2 new deferred, '
            '2 staged rows protected',
        )

    def test_overflow_fast_track_error_in_ping_suffix(self):
        """When ``fast_track_errors`` is non-empty, the ping gets
        ``", fast-track failed for {F}"`` suffix."""
        # Queue full of unstaged, 3 new. First fallback call raises,
        # others succeed.
        for i in range(10):
            link = f'http://o/{i}'
            self._insert(link=link, title=f'O{i}')
            self._age_fetched(link, 40 - i)

        new_entries = [
            _rss_entry(f'http://new/{i}', f'N{i}') for i in range(3)
        ]

        call_count = {'n': 0}

        def fallback_side(row, via_review=False):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError('first fail')
            # Populate ru_title so the NOT NULL constraint on
            # published_articles is satisfied, then move the row.
            repo.update_staged(
                row['link'],
                'ru-' + (row.get('title') or ''),
                '',
                ['ru-auto'],
                None,
            )
            repo.move_to_published(
                row['link'],
                'https://telegra.ph/fake-01-01',
                'fake-01-01',
                via_review=via_review,
            )
            return True

        notify = MagicMock(return_value=True)
        with patch('news_bot._fallback_publish', side_effect=fallback_side), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1)
        text = overflow_calls[0].args[0]
        # One failed, two evicted successfully. needed=3 - 0 = 3;
        # staged_protected=0; 2 slots freed → 2 new accepted; 1 deferred;
        # fast-track errors = 1.
        self.assertEqual(
            text,
            'Queue pressure: auto-published 2, 1 new deferred, '
            '0 staged rows protected, fast-track failed for 1',
        )
        self.assertEqual(len(errors), 1)

    def test_overflow_empty_queue_protected_is_zero(self):
        """Empty queue + flood of new entries: ``staged_protected`` must be
        0, not ``needed - 0``. Regression for the Variant-B QA finding —
        an empty queue means nothing to protect, even if the repo returned
        zero eviction candidates."""
        # Queue EMPTY. 36 new entries arrive; QUEUE_CAP=10 → slots_free=10,
        # needed=26, list_pending_for_eviction() returns []. The pre-fix
        # code reported staged_protected = 26 - 0 = 26 (phantom).
        self.assertEqual(repo.count_pending(), 0)

        new_entries = [
            _rss_entry(f'http://new/{i}', f'N{i}') for i in range(36)
        ]

        notify = MagicMock(return_value=True)
        mock_fallback = MagicMock()
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        mock_fallback.assert_not_called()
        # 10 slots accepted, 26 deferred.
        self.assertEqual(len(accepted), 10)
        self.assertEqual(errors, [])

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1)
        text = overflow_calls[0].args[0]
        # Protected MUST be 0 — no rows existed to be protected.
        self.assertEqual(
            text,
            'Queue pressure: auto-published 0, 26 new deferred, '
            '0 staged rows protected',
        )

    def test_overflow_partial_protection(self):
        """Queue has 10 rows: 4 ru-NULL + 6 ru-staged; needed=6. Repo
        returns 4 candidates → gap of 2 that IS due to protection.
        ``staged_protected`` = 2 (the gap), not 6 (total staged)."""
        # 6 staged rows (older) and 4 unstaged rows (newer).
        for i in range(6):
            self._insert_staged(link=f'http://s/{i}', title=f'S{i}')
        for i in range(4):
            link = f'http://o/{i}'
            self._insert(link=link, title=f'O{i}')
            self._age_fetched(link, 40 - i)

        self.assertEqual(repo.count_pending(), 10)

        # needed = 6 new entries - 0 slots_free = 6; candidates caps at 4.
        new_entries = [
            _rss_entry(f'http://new/{i}', f'N{i}') for i in range(6)
        ]

        def fallback_side(row, via_review=False):
            repo.update_staged(
                row['link'],
                'ru-' + (row.get('title') or ''),
                '',
                ['ru-auto'],
                None,
            )
            repo.move_to_published(
                row['link'],
                'https://telegra.ph/fake-01-01',
                'fake-01-01',
                via_review=via_review,
            )
            return True

        notify = MagicMock(return_value=True)
        with patch('news_bot._fallback_publish', side_effect=fallback_side), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1)
        text = overflow_calls[0].args[0]
        # 4 evicted, 2 new deferred (6 - 4 slots freed), protected = gap = 2.
        self.assertEqual(
            text,
            'Queue pressure: auto-published 4, 2 new deferred, '
            '2 staged rows protected',
        )
        self.assertEqual(len(accepted), 4)

    def test_overflow_full_protection(self):
        """Queue has 10 rows, all ru-staged; needed=6. Repo returns 0
        candidates → gap of 6, all caused by protection. ``staged_protected``
        = 6."""
        for i in range(10):
            self._insert_staged(link=f'http://s/{i}', title=f'S{i}')

        new_entries = [
            _rss_entry(f'http://new/{i}', f'N{i}') for i in range(6)
        ]

        notify = MagicMock(return_value=True)
        mock_fallback = MagicMock()
        with patch('news_bot._fallback_publish', mock_fallback), \
             patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        mock_fallback.assert_not_called()
        self.assertEqual(accepted, [])
        self.assertEqual(errors, [])

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(len(overflow_calls), 1)
        text = overflow_calls[0].args[0]
        self.assertEqual(
            text,
            'Queue pressure: auto-published 0, 6 new deferred, '
            '6 staged rows protected',
        )

    def test_overflow_happy_path_no_ping_when_all_fits(self):
        """When within cap and no eviction needed, the ping is NOT sent
        (AC L97 equivalent — no pressure, no ping)."""
        new_entries = [
            _rss_entry('http://new/1', 'N1'),
        ]

        notify = MagicMock(return_value=True)
        with patch('news_bot.send_admin_notification', notify):
            accepted, errors = news_bot._overflow_fast_track(new_entries)

        overflow_calls = [
            c for c in notify.call_args_list
            if c.args and 'Queue pressure' in c.args[0]
        ]
        self.assertEqual(overflow_calls, [])
        self.assertEqual(accepted, new_entries)


# ============================================================================
# Integration via ``job()`` — overflow pass is wired after filter + before INSERT
# ============================================================================


class TestOverflowInJob(_OverflowCase):

    def _patch_sources(self, entries):
        """Provide a single-fetcher SOURCES that yields the given RSS-shape
        entries. Mimics ``test_job_prep_phase._patch_sources`` plumbing."""
        def rss(notifier=None):
            return entries

        def mattel(notifier=None):
            return []

        return patch('news_bot.SOURCES', [rss, mattel])

    def test_job_overflow_smoke_mixed_queue(self):
        """Smoke scenario from task-10 Verify-smoke: queue pre-filled to
        QUEUE_CAP with a staged/unstaged mix; job() with new entries →
        staged rows survive, unstaged evicted via mocked _fallback_publish."""
        # 4 staged + 6 unstaged = 10 rows.
        for i in range(4):
            self._insert_staged(link=f'http://s/{i}', title=f'S{i}')
        for i in range(6):
            link = f'http://o/{i}'
            self._insert(link=link, title=f'O{i}')
            self._age_fetched(link, 40 - i)

        # Two new RSS entries — will need to evict 2 rows.
        new_rss = [
            _rss_entry('http://new/a', 'New-A', source_name='autoevolution'),
            _rss_entry('http://new/b', 'New-B', source_name='autoevolution'),
        ]

        def fallback_side(row, via_review=False):
            # Populate ru_title so published_articles NOT NULL is satisfied,
            # then move-to-published so the slot actually frees up.
            repo.update_staged(
                row['link'],
                'ru-' + (row.get('title') or ''),
                '',
                ['ru-auto'],
                None,
            )
            repo.move_to_published(
                row['link'],
                f"https://telegra.ph/{row['link'].split('/')[-1]}-01-01",
                f"{row['link'].split('/')[-1]}-01-01",
                via_review=via_review,
            )
            return True

        with self._patch_sources(new_rss), \
             patch('news_bot._fallback_publish', side_effect=fallback_side), \
             patch('news_bot.fetch_full_article',
                   side_effect=lambda e: _article_payload(e.get('title'))), \
             patch('news_bot.send_admin_notification', return_value=True):
            news_bot.job()

        # Queue remains at cap (4 staged survived + 2 new inserted + 4 open
        # left — 2 were evicted).
        self.assertEqual(repo.count_pending(), 10)

        # Staged rows untouched.
        for i in range(4):
            self.assertIsNotNone(repo.get_pending(f'http://s/{i}'))

        # Oldest two unstaged rows evicted.
        self.assertIsNone(repo.get_pending('http://o/0'))
        self.assertIsNone(repo.get_pending('http://o/1'))
        # And they landed in published with via_review=0.
        pub = repo.get_published('http://o/0')
        self.assertIsNotNone(pub)
        self.assertEqual(pub['via_review'], 0)

        # New entries inserted.
        self.assertIsNotNone(repo.get_pending('http://new/a'))
        self.assertIsNotNone(repo.get_pending('http://new/b'))


if __name__ == '__main__':
    unittest.main()
