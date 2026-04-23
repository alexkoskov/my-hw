#!/usr/bin/env python3
"""Unit tests for `hw_review.py` — CLI surface (Task 7).

Five subcommands under test: `list`, `show`, `stage`, `skip`, `preview`.
Matches the TDD anchor in `work/manual-review-workflow/tasks/7.md`.

Follows the tempfile-DB pattern from `tests/test_integration.py:22-28`:
allocate a .db file, monkeypatch `news_bot.DB_FILE`, `news_bot.init_db()` so
both the CLI's own `pending_articles_repo` calls and test-side sqlite3
connections target the same on-disk file.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import pending_articles_repo as repo
import hw_review


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------


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


class _CliCase(unittest.TestCase):
    """Shared setUp/tearDown: tempfile DB, minimal env patches, captured IO."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)
        self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        # Capture stdout / stderr per call via argparse main(argv=list, ...).
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _run(self, argv, stdin_bytes=None):
        """Invoke `hw_review.main` with captured stdout / stderr.

        `stdin_bytes`, if given, is pushed onto a fake `sys.stdin` object whose
        `.buffer.read(n)` returns up to n of those bytes.
        """
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

    def _stage(self, link, ru_title='РУ заголовок', ru_subtitle='РУ лид',
               ru_paragraphs=None, ru_blocks=None):
        repo.update_staged(
            link,
            ru_title,
            ru_subtitle,
            ru_paragraphs if ru_paragraphs is not None else ['Первый.', 'Второй.'],
            ru_blocks,
        )


# ------------------------------------------------------------------
# `list`
# ------------------------------------------------------------------


class TestListCommand(_CliCase):

    def test_list_empty_prints_marker(self):
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        self.assertIn('queue is empty', self.stdout.getvalue())
        # No warning footer when failed table is empty.
        self.assertNotIn('⚠️', self.stdout.getvalue())

    def test_list_renders_numbered_rows(self):
        self._insert(link='http://a/1', title='First', source='autoevolution')
        self._insert(link='http://a/2', title='Second', source='mattel')
        self._insert(link='http://a/3', title='Third', source='lamley')

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('1.', out)
        self.assertIn('2.', out)
        self.assertIn('3.', out)
        # SOURCE_EMOJI appears per row.
        self.assertIn(news_bot.SOURCE_EMOJI['autoevolution'], out)
        self.assertIn(news_bot.SOURCE_EMOJI['mattel'], out)
        self.assertIn(news_bot.SOURCE_EMOJI['lamley'], out)
        self.assertIn('First', out)
        self.assertIn('Second', out)
        self.assertIn('Third', out)

    def test_list_appends_failed_footer_when_queue_empty(self):
        # Insert straight into failed_articles (no public helper needed).
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, last_error) "
                "VALUES (?, ?, ?, ?, ?)",
                ('http://fail/1', 'Failed One', 'mattel', '[]', 'boom'),
            )
            conn.commit()
        finally:
            conn.close()

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('queue is empty', out)
        self.assertIn('⚠️', out)
        self.assertIn('1', out)
        self.assertIn('Failed One', out)

    def test_list_appends_failed_footer_when_queue_nonempty(self):
        self._insert(link='http://a/1', title='Active One')
        self._insert(link='http://a/2', title='Active Two')
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, last_error) "
                "VALUES (?, ?, ?, ?, ?)",
                ('http://fail/1', 'Broken', 'lamley', '[]', 'err'),
            )
            conn.commit()
        finally:
            conn.close()

        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('Active One', out)
        self.assertIn('Active Two', out)
        self.assertIn('⚠️', out)
        self.assertIn('Broken', out)

    def test_list_no_failed_footer_when_empty_failed(self):
        self._insert(link='http://a/1', title='Only One')
        rc = self._run(['list'])
        self.assertEqual(rc, 0)
        self.assertNotIn('⚠️', self.stdout.getvalue())


# ------------------------------------------------------------------
# `show`
# ------------------------------------------------------------------


class TestShowCommand(_CliCase):

    def test_show_out_of_range_returns_exit_1(self):
        self._insert(link='http://a/1', title='Only One')
        rc = self._run(['show', '99'])
        self.assertEqual(rc, 1)
        self.assertIn('index out of range', self.stderr.getvalue())

    def test_show_zero_index_out_of_range(self):
        self._insert(link='http://a/1', title='Only One')
        rc = self._run(['show', '0'])
        self.assertEqual(rc, 1)
        self.assertIn('index out of range', self.stderr.getvalue())

    def test_show_prints_all_fields(self):
        entry = self._insert(link='http://a/1', title='EN Title')
        self._stage(entry['link'],
                    ru_title='РУ Заголовок',
                    ru_subtitle='РУ Подзаг',
                    ru_paragraphs=['Первый.', 'Второй.'])
        repo.mark_telegraph_published(
            entry['link'], 'https://telegra.ph/EN-01', 'EN-01',
        )
        # Inject last_error + attempt_count via public helper.
        repo.increment_attempt(entry['link'], error='some error')

        rc = self._run(['show', '1'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('EN Title', out)
        self.assertIn('РУ Заголовок', out)
        self.assertIn('РУ Подзаг', out)
        self.assertIn('Первый', out)
        self.assertIn('https://telegra.ph/EN-01', out)
        self.assertIn('some error', out)
        self.assertIn('http://a/1', out)

    def test_show_marks_null_vs_filled_ru_fields(self):
        # ru_* all NULL — NULL markers should appear.
        self._insert(link='http://a/1', title='EN Title')
        rc = self._run(['show', '1'])
        self.assertEqual(rc, 0)
        out = self.stdout.getvalue()
        self.assertIn('NULL', out)


# ------------------------------------------------------------------
# `stage` / validator
# ------------------------------------------------------------------


class TestStageCommand(_CliCase):

    def _valid_payload_bytes(self, ru_blocks=None):
        payload = {'ru_paragraphs': ['Первый.', 'Второй.'], 'ru_blocks': ru_blocks}
        return json.dumps(payload, ensure_ascii=False).encode('utf-8')

    def test_stage_happy_path(self):
        entry = self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=self._valid_payload_bytes(),
        )
        self.assertEqual(rc, 0, self.stderr.getvalue())
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['ru_title'], 'РУ')
        self.assertEqual(row['ru_subtitle'], 'Лид')
        self.assertEqual(row['ru_paragraphs'], ['Первый.', 'Второй.'])
        self.assertIsNone(row['ru_blocks'])
        # Happy-path stage is silent on stdout.
        self.assertEqual(self.stdout.getvalue(), '')

    def test_stage_rejects_stdin_over_256kib(self):
        entry = self._insert(link='http://a/1', title='EN')
        # 256 KiB + 1 byte of 'a' — slurp limit is 256 KiB + 1 (262145), so
        # sending that exact byte-count triggers the rejection branch.
        huge = b'a' * 262145
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=huge,
        )
        self.assertEqual(rc, 1)
        self.assertIn('stdin too large', self.stderr.getvalue())
        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['ru_paragraphs'])

    def test_stage_rejects_invalid_json(self):
        self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=b'{not json',
        )
        self.assertEqual(rc, 1)
        self.assertIn('invalid JSON', self.stderr.getvalue())

    def test_stage_rejects_depth_over_3(self):
        self._insert(link='http://a/1', title='EN')
        # Depth 4: top dict > list > dict > list > dict → forbidden.
        evil = {
            'ru_paragraphs': [
                {'k': {'nested': {'too': 'deep'}}},
            ],
            'ru_blocks': None,
        }
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps(evil).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_unknown_top_key(self):
        self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [], 'ru_blocks': None, 'extra': 1,
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_non_list_ru_paragraphs(self):
        self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': 'строка', 'ru_blocks': None,
            }, ensure_ascii=False).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_non_dict_block(self):
        self._insert(link='http://a/1', title='EN', blocks=[{'type': 'paragraph', 'text': 'x'}])
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [], 'ru_blocks': [1, 2],
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_unknown_block_type(self):
        self._insert(link='http://a/1', title='EN', blocks=[{'type': 'paragraph', 'text': 'x'}])
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [],
                'ru_blocks': [{'type': 'script', 'text': 'evil'}],
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_unknown_block_key(self):
        self._insert(link='http://a/1', title='EN', blocks=[{'type': 'paragraph', 'text': 'x'}])
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [],
                'ru_blocks': [{'type': 'paragraph', 'text': 'ok', 'evil': 'x'}],
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_string_field_over_10kib(self):
        self._insert(link='http://a/1', title='EN')
        too_long = 'x' * 10241
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [too_long], 'ru_blocks': None,
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('staging rejected', self.stderr.getvalue())

    def test_stage_rejects_ru_blocks_null_when_en_has_blocks(self):
        self._insert(
            link='http://a/1', title='EN',
            blocks=[{'type': 'paragraph', 'text': 'en'}],
        )
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': ['Первый'], 'ru_blocks': None,
            }, ensure_ascii=False).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('ru_blocks required', self.stderr.getvalue())

    def test_stage_rejects_ru_blocks_present_when_en_null(self):
        self._insert(link='http://a/1', title='EN', blocks=None)
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': ['Первый'],
                'ru_blocks': [{'type': 'paragraph', 'text': 'ok'}],
            }, ensure_ascii=False).encode('utf-8'),
        )
        self.assertEqual(rc, 1)
        self.assertIn('ru_blocks must be null', self.stderr.getvalue())

    def test_stage_accepts_valid_blocks_parity(self):
        """EN has blocks, payload has matching RU blocks → happy path."""
        entry = self._insert(
            link='http://a/1', title='EN',
            blocks=[{'type': 'paragraph', 'text': 'en'}],
        )
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': ['Первый'],
                'ru_blocks': [{'type': 'paragraph', 'text': 'ок'}],
            }, ensure_ascii=False).encode('utf-8'),
        )
        self.assertEqual(rc, 0, self.stderr.getvalue())
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['ru_blocks'], [{'type': 'paragraph', 'text': 'ок'}])

    def test_stage_accepts_empty_ru_paragraphs(self):
        """Edge case §9.13 п.1: empty list is staged (NOT NULL)."""
        entry = self._insert(link='http://a/1', title='EN')
        rc = self._run(
            ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=json.dumps({
                'ru_paragraphs': [], 'ru_blocks': None,
            }).encode('utf-8'),
        )
        self.assertEqual(rc, 0, self.stderr.getvalue())
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['ru_paragraphs'], [])

    def test_stage_reports_vanished_row(self):
        """Between list and stage the row was deleted by another flow."""
        self._insert(link='http://a/1', title='EN')

        # Patch update_staged to return False, simulating a vanished row.
        with patch('pending_articles_repo.update_staged', return_value=False):
            rc = self._run(
                ['stage', '1', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
                stdin_bytes=self._valid_payload_bytes(),
            )
        self.assertEqual(rc, 1)
        self.assertIn('row no longer pending', self.stderr.getvalue())

    def test_stage_out_of_range(self):
        rc = self._run(
            ['stage', '99', '--ru-title', 'РУ', '--ru-subtitle', 'Лид'],
            stdin_bytes=self._valid_payload_bytes(),
        )
        self.assertEqual(rc, 1)
        self.assertIn('index out of range', self.stderr.getvalue())


# ------------------------------------------------------------------
# `skip`
# ------------------------------------------------------------------


class TestSkipCommand(_CliCase):

    def test_skip_without_staged_ru_no_prompt(self):
        entry = self._insert(link='http://a/1', title='EN')
        # No ru staged. No stdin should be consumed.
        with patch('builtins.input') as mock_input:
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        mock_input.assert_not_called()
        # Row removed from pending; link in processed_news.
        self.assertIsNone(repo.get_pending(entry['link']))
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT link FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_skip_with_staged_ru_prompts_confirms(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('builtins.input', return_value='y'):
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        self.assertIsNone(repo.get_pending(entry['link']))

    def test_skip_cancelled_on_n_answer(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('builtins.input', return_value='N'):
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        self.assertIn('skip cancelled', self.stdout.getvalue())
        # Row still in pending (unchanged).
        self.assertIsNotNone(repo.get_pending(entry['link']))

    def test_skip_cancelled_on_empty_answer(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('builtins.input', return_value=''):
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(repo.get_pending(entry['link']))

    def test_skip_cancelled_on_garbage_answer(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('builtins.input', return_value='maybe'):
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(repo.get_pending(entry['link']))

    def test_skip_cancelled_on_eof(self):
        """Non-tty stdin → EOFError → treat as cancel."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('builtins.input', side_effect=EOFError):
            rc = self._run(['skip', '1'])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(repo.get_pending(entry['link']))


# ------------------------------------------------------------------
# `preview`
# ------------------------------------------------------------------


class TestPreviewCommand(_CliCase):

    def setUp(self):
        super().setUp()
        # Redirect CACHE_DIR at module level to a tempdir so tests don't
        # pollute the user's real ~/.cache/hw-review/.
        self.cache_tmp = tempfile.mkdtemp(prefix='hw-review-test-')
        self.cache_patcher = patch('hw_review.CACHE_DIR', Path(self.cache_tmp).resolve())
        self.cache_patcher.start()

    def tearDown(self):
        self.cache_patcher.stop()
        # Clean up any files left behind.
        for p in Path(self.cache_tmp).glob('*'):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            os.rmdir(self.cache_tmp)
        except OSError:
            pass
        super().tearDown()

    def test_preview_precondition_no_staged_ru(self):
        entry = self._insert(link='http://a/1', title='EN')
        with patch('webbrowser.open') as mock_open:
            rc = self._run(['preview', '1'])
        self.assertEqual(rc, 1)
        self.assertIn('nothing to preview', self.stderr.getvalue())
        mock_open.assert_not_called()
        # No file created.
        self.assertEqual(list(Path(self.cache_tmp).glob('*.html')), [])
        # preview_html_path still NULL.
        row = repo.get_pending(entry['link'])
        self.assertIsNone(row['preview_html_path'])

    def test_preview_writes_html_inside_cache_dir(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('webbrowser.open', return_value=True) as mock_open:
            rc = self._run(['preview', '1'])
        self.assertEqual(rc, 0, self.stderr.getvalue())

        out = self.stdout.getvalue().strip()
        path = Path(out)
        self.assertTrue(path.exists(), f"preview file not created at {path}")
        self.assertEqual(path.parent.resolve(), Path(self.cache_tmp).resolve())
        body = path.read_text(encoding='utf-8')
        self.assertIn('<!DOCTYPE html>', body)
        self.assertIn('Content-Security-Policy', body)

        # preview_html_path persisted.
        row = repo.get_pending(entry['link'])
        self.assertEqual(row['preview_html_path'], str(path))

        mock_open.assert_called_once()

    def test_preview_no_open_flag_skips_browser(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('webbrowser.open') as mock_open:
            rc = self._run(['preview', '1', '--no-open'])
        self.assertEqual(rc, 0, self.stderr.getvalue())
        mock_open.assert_not_called()
        path = self.stdout.getvalue().strip()
        self.assertTrue(Path(path).exists())

    def test_preview_calls_webbrowser_by_default(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('webbrowser.open', return_value=True) as mock_open:
            rc = self._run(['preview', '1'])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_open.call_count, 1)
        arg = mock_open.call_args[0][0]
        self.assertTrue(arg.startswith('file://'))

    def test_preview_path_guard_escape_aborts(self):
        """Simulate a resolve() that escapes the cache dir: abort + cleanup."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        real_resolve = Path.resolve
        evil_path = Path('/tmp/evil.html')

        def fake_resolve(self, *a, **kw):
            # Keep CACHE_DIR resolution intact; only swap for the temp file.
            resolved = real_resolve(self, *a, **kw)
            if resolved.suffix == '.html' and resolved.parent != evil_path.parent:
                return evil_path
            return resolved

        with patch.object(Path, 'resolve', fake_resolve), \
             patch('webbrowser.open') as mock_open:
            rc = self._run(['preview', '1'])
        self.assertEqual(rc, 1)
        self.assertIn('preview path escaped cache dir', self.stderr.getvalue())
        mock_open.assert_not_called()

    def test_preview_on_empty_webbrowser_still_exits_zero(self):
        """`webbrowser.open` returns False (headless) → still exit 0."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])
        with patch('webbrowser.open', return_value=False):
            rc = self._run(['preview', '1'])
        self.assertEqual(rc, 0)
        self.assertTrue(self.stdout.getvalue().strip())

    def test_preview_removes_old_file_on_rerun(self):
        """Running preview twice → old file is deleted, new one replaces it."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        with patch('webbrowser.open', return_value=True):
            rc1 = self._run(['preview', '1', '--no-open'])
        self.assertEqual(rc1, 0)
        first_path = self.stdout.getvalue().strip()
        self.assertTrue(Path(first_path).exists())

        # Reset IO and re-run.
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        with patch('webbrowser.open', return_value=True):
            rc2 = self._run(['preview', '1', '--no-open'])
        self.assertEqual(rc2, 0, self.stderr.getvalue())
        second_path = self.stdout.getvalue().strip()
        self.assertTrue(Path(second_path).exists())
        self.assertNotEqual(first_path, second_path)
        # First file was cleaned up.
        self.assertFalse(Path(first_path).exists())


# ------------------------------------------------------------------
# `_validate_stage_payload` unit tests (direct, independent of stdin plumbing)
# ------------------------------------------------------------------


class TestValidator(unittest.TestCase):

    def test_valid_minimal(self):
        hw_review._validate_stage_payload(
            {'ru_paragraphs': [], 'ru_blocks': None},
        )

    def test_missing_top_key(self):
        with self.assertRaises(ValueError):
            hw_review._validate_stage_payload({'ru_paragraphs': []})

    def test_valid_block_types(self):
        hw_review._validate_stage_payload({
            'ru_paragraphs': [],
            'ru_blocks': [
                {'type': 'paragraph', 'text': 'p'},
                {'type': 'lead', 'text': 'l'},
                {'type': 'heading', 'text': 'h', 'level': 3},
                {'type': 'image', 'src': 'https://x/y.jpg', 'caption': 'c'},
                {'type': 'video', 'src': 'https://x/v', 'caption': ''},
            ],
        })

    def test_heading_level_must_be_3_or_4(self):
        with self.assertRaises(ValueError):
            hw_review._validate_stage_payload({
                'ru_paragraphs': [],
                'ru_blocks': [{'type': 'heading', 'text': 'x', 'level': 5}],
            })

    def test_non_string_paragraph(self):
        with self.assertRaises(ValueError):
            hw_review._validate_stage_payload({
                'ru_paragraphs': [123],
                'ru_blocks': None,
            })

    def test_paragraphs_list_over_100(self):
        with self.assertRaises(ValueError):
            hw_review._validate_stage_payload({
                'ru_paragraphs': ['x'] * 101,
                'ru_blocks': None,
            })


# ------------------------------------------------------------------
# argv smoke
# ------------------------------------------------------------------


class TestArgvSmoke(_CliCase):

    def test_main_no_args_raises_systemexit_nonzero(self):
        # argparse.exit(2) on a missing required subcommand raises SystemExit
        # with a non-zero status. That's the idiomatic argparse contract —
        # we don't swallow it so the shell sees the real exit-code.
        with self.assertRaises(SystemExit) as cm:
            self._run([])
        self.assertNotEqual(cm.exception.code, 0)

    def test_list_subcommand_smoke(self):
        # Smoke: the `list` subcommand parses and runs.
        rc = self._run(['list'])
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
