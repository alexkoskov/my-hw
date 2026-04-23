#!/usr/bin/env python3
"""Unit tests for the ux-guidelines reminder surfaced in `hw_review`.

Ad-hoc workflow-enforcement layer added after live QA found that a test
translation shipped without the transcreation system prompt loaded. The
reminder must:

* appear in `list` output whenever the pending queue is non-empty
  (AFTER the numbered rows and any ``⚠️`` failed-articles footer);
* stay quiet on an empty pending queue (no `stage N` follows from
  nothing-to-do, and no `stage` follows from failed-only either —
  `retry` is the next action there);
* fire as the FIRST line of stderr on every `hw_review stage` call,
  before any stdin read or validation, because `stage` is the gate
  where Russian text is persisted.

Shares the tempfile-DB scaffold from ``test_hw_review_cli.py`` —
self-contained so a single-file pytest invocation also works.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
import hw_review


GUIDE_PATH = '.claude/skills/project-knowledge/references/ux-guidelines.md'


def _sample_entry(link='http://example.com/a', title='Example',
                  source='autoevolution'):
    return {
        'link': link,
        'source_name': source,
        'feed_url': None,
        'title': title,
        'subtitle': 'Lead',
        'paragraphs': ['First paragraph.', 'Second paragraph.'],
        'images': ['http://img/1.jpg'],
        'blocks': None,
        'pub_date': '2026-04-01',
    }


class _CliCase(unittest.TestCase):
    """Fresh tempfile SQLite + captured stdout/stderr per test."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _run(self, argv, stdin_bytes=None):
        fake_stdin = None
        if stdin_bytes is not None:
            fake_stdin = MagicMock()
            fake_stdin.buffer = io.BytesIO(stdin_bytes)

        with patch('sys.stdout', self.stdout), \
             patch('sys.stderr', self.stderr):
            if fake_stdin is not None:
                with patch('sys.stdin', fake_stdin):
                    return hw_review.main(argv)
            return hw_review.main(argv)

    def _insert(self, **kw):
        entry = _sample_entry(**kw)
        self.assertTrue(repo.insert_pending(entry))
        return entry

    def _insert_failed(self, link='http://fail/1', title='Failed One',
                       source='mattel', error='boom'):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, last_error) "
                "VALUES (?, ?, ?, ?, ?)",
                (link, title, source, '[]', error),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Constant shape
# ---------------------------------------------------------------------------


class TestReminderConstant(unittest.TestCase):
    """Guard against silent drift of the guide path — that token is the
    whole point of the reminder."""

    def test_reminder_mentions_ux_guidelines(self):
        self.assertIn('ux-guidelines', hw_review._STAGE_GUIDE_REMINDER)

    def test_reminder_contains_exact_guide_path(self):
        self.assertIn(GUIDE_PATH, hw_review._STAGE_GUIDE_REMINDER)


# ---------------------------------------------------------------------------
# `list` surface
# ---------------------------------------------------------------------------


class TestListReminder(_CliCase):

    def test_list_nonempty_queue_shows_reminder_after_rows(self):
        self._insert(link='http://a/1', title='Only One')
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn(GUIDE_PATH, out)
        self.assertIn('ux-guidelines', out)
        # Row comes first, reminder after.
        row_pos = out.find('Only One')
        reminder_pos = out.find(GUIDE_PATH)
        self.assertGreaterEqual(row_pos, 0)
        self.assertGreater(reminder_pos, row_pos,
                           'reminder must appear AFTER the row listing')

    def test_list_empty_queue_hides_reminder(self):
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('queue is empty', out)
        self.assertNotIn(GUIDE_PATH, out)
        self.assertNotIn('ux-guidelines', out)

    def test_list_failed_only_hides_reminder(self):
        """Pending empty + failed non-empty → no `stage` follows, so no
        reminder. Retry is the next action here."""
        self._insert_failed(link='http://fail/1', title='Broken')
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        # Footer is present but reminder is not.
        self.assertIn('⚠️', out)
        self.assertIn('Broken', out)
        self.assertNotIn(GUIDE_PATH, out)

    def test_list_reminder_after_failed_footer(self):
        """When both pending and failed are non-empty, reminder still
        lands after the ⚠️ footer — users reading top-to-bottom see
        warnings first, enforcement last."""
        self._insert(link='http://a/1', title='Active One')
        self._insert_failed(link='http://fail/1', title='Broken')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        footer_pos = out.find('⚠️')
        reminder_pos = out.find(GUIDE_PATH)
        self.assertGreaterEqual(footer_pos, 0)
        self.assertGreater(reminder_pos, footer_pos,
                           'reminder must appear AFTER the failed footer')


# ---------------------------------------------------------------------------
# `stage` surface
# ---------------------------------------------------------------------------


class TestStageReminder(_CliCase):

    def _payload(self):
        return json.dumps(
            {'ru_paragraphs': ['Первый.'], 'ru_blocks': None},
            ensure_ascii=False,
        ).encode('utf-8')

    def test_stage_success_emits_reminder_first_on_stderr(self):
        entry = self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=self._payload(),
        )
        self.assertEqual(rc, 0, self.stderr.getvalue())
        err = self.stderr.getvalue()
        # First non-empty line carries the reminder.
        first_line = err.splitlines()[0] if err else ''
        self.assertIn('ux-guidelines', first_line,
                      f'first stderr line should carry reminder, got: {first_line!r}')
        self.assertIn(GUIDE_PATH, err)
        # Stage success is silent on stdout (unchanged invariant).
        self.assertEqual(self.stdout.getvalue(), '')
        # Row was actually staged despite the reminder prelude.
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['ru_paragraphs'], ['Первый.'])

    def test_stage_validation_error_still_emits_reminder_first(self):
        """Even when stage bails on invalid JSON, the reminder was already
        printed — first line of stderr, before any validation step."""
        self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=b'{not json',
        )
        self.assertEqual(rc, 1)
        err = self.stderr.getvalue()
        lines = err.splitlines()
        self.assertGreater(len(lines), 0)
        self.assertIn('ux-guidelines', lines[0])
        # The original validation-error message also surfaces, AFTER the
        # reminder (which spans two lines).
        self.assertIn('invalid JSON', err)
        reminder_pos = err.find(GUIDE_PATH)
        err_pos = err.find('invalid JSON')
        self.assertLess(reminder_pos, err_pos,
                        'reminder must precede validation error')

    def test_stage_out_of_range_still_emits_reminder_first(self):
        """Reminder comes before `_resolve_pending` too — so an
        out-of-range index never suppresses the nudge."""
        rc = self._run(
            ['stage', '99', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=self._payload(),
        )
        self.assertEqual(rc, 1)
        err = self.stderr.getvalue()
        lines = err.splitlines()
        self.assertGreater(len(lines), 0)
        self.assertIn('ux-guidelines', lines[0])
        self.assertIn('index out of range', err)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
