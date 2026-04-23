#!/usr/bin/env python3
"""Tests for `hw_review publish N` (Task 8).

Covers the idempotency contract (Decision 9 — Telegraph-URL reuse on retry),
the channel-post hashtag wiring (Decision 14 — pass `row['link']`, not
`source_name`, into `send_telegraph_teaser`), the three-state vanished-row
matrix, the preview-file cleanup, and the Telegraph-only failure-leaves-row
contract.

Follows the tempfile-DB pattern already used by `tests/test_hw_review_cli.py`:
allocate a sqlite file, monkeypatch `news_bot.DB_FILE`, call `news_bot.init_db()`,
use the real `pending_articles_repo` to populate rows. External network
dependencies (`publish_article`, `send_telegraph_teaser`) are mocked at the
module-under-test level (`hw_review.publish_article` /
`hw_review.send_telegraph_teaser`) because `hw_review.py` imports those names
directly — patching the import alias is the correct seam.
"""
import io
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
from telegraph_publisher import TelegraphError


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


class _PublishCase(unittest.TestCase):
    """Shared tempfile-DB fixture, captured IO, helpers."""

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

    def _run(self, argv):
        with patch('sys.stdout', self.stdout), patch('sys.stderr', self.stderr):
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
# Happy path
# ------------------------------------------------------------------


class TestPublishHappyPath(_PublishCase):

    def test_publish_happy_path(self):
        entry = self._insert(link='http://a/1', title='EN Title',
                             source='autoevolution')
        self._stage(entry['link'])

        tg_url = 'https://telegra.ph/EN-Title-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 0, self.stderr.getvalue())
        self.assertIn(tg_url, self.stdout.getvalue())

        # publish_article and teaser each called exactly once.
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_teaser.call_count, 1)

        # Row removed from pending, present in published with via_review=1.
        self.assertIsNone(repo.get_pending(entry['link']))
        pub = repo.get_published(entry['link'])
        self.assertIsNotNone(pub)
        self.assertEqual(pub['telegraph_url'], tg_url)
        self.assertEqual(pub['via_review'], 1)

        # processed_news stamped.
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT link FROM processed_news WHERE link=?",
                (entry['link'],),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)


# ------------------------------------------------------------------
# Retry idempotency (Decision 9)
# ------------------------------------------------------------------


class TestPublishRetryIdempotency(_PublishCase):

    def test_publish_retry_reuses_telegraph_url(self):
        """First run: teaser returns False → pending retained with telegraph_url.
        Second run: teaser returns True → row published. publish_article
        called exactly ONCE across both runs."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        tg_url = 'https://telegra.ph/EN-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        # Two-shot teaser: False first, True second.
        mock_teaser = MagicMock(side_effect=[False, True])

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc1 = self._run(['publish', '1'])

        self.assertEqual(rc1, 1)
        self.assertIn('telegram send failed', self.stderr.getvalue())
        # Pending row retained, telegraph_url populated.
        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertEqual(row['telegraph_url'], tg_url)
        self.assertTrue(row['telegraph_path'])  # non-empty path

        # Second run — reset captured IO, same mocks.
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc2 = self._run(['publish', '1'])

        self.assertEqual(rc2, 0, self.stderr.getvalue())
        self.assertIn(tg_url, self.stdout.getvalue())

        # CRITICAL: publish_article was NOT called a second time.
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_teaser.call_count, 2)

        # Second-run teaser call used the stored URL (the same one from run 1).
        _, second_call = mock_teaser.call_args_list
        self.assertEqual(second_call[0][0], tg_url)

        # Row is now published.
        self.assertIsNone(repo.get_pending(entry['link']))
        self.assertIsNotNone(repo.get_published(entry['link']))

    def test_publish_retry_after_teaser_exception(self):
        """Same idempotency when first teaser call raises."""
        from telegram.error import TelegramError as _TGError
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        tg_url = 'https://telegra.ph/EN-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(side_effect=[_TGError('boom'), True])

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc1 = self._run(['publish', '1'])

        self.assertEqual(rc1, 1)
        self.assertIn('telegram send failed', self.stderr.getvalue())
        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertEqual(row['telegraph_url'], tg_url)

        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc2 = self._run(['publish', '1'])

        self.assertEqual(rc2, 0, self.stderr.getvalue())
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_teaser.call_count, 2)
        self.assertIsNotNone(repo.get_published(entry['link']))


# ------------------------------------------------------------------
# Vanished-row three-state matrix
# ------------------------------------------------------------------


class TestPublishVanishedRow(_PublishCase):
    """When pending is empty for the given index — the CLI must bail before any
    external call with a clean one-line stderr cite of the current state."""

    def test_publish_out_of_range(self):
        """Nothing in pending at all → generic index-out-of-range branch."""
        mock_publish = MagicMock()
        mock_teaser = MagicMock()
        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])
        self.assertEqual(rc, 1)
        self.assertIn('out of range', self.stderr.getvalue())
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()

    def test_publish_vanished_row_already_published(self):
        """Link appears at position 1, but between list() and publish the row
        was moved to published_articles — CLI must detect that via
        `get_published` and cite the stored URL."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        # Concurrent move: insert a published row with matching link; remove
        # from pending directly to simulate race.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry['link'], 'EN', 'РУ',
                    'https://telegra.ph/OLD-URL', 'OLD-URL',
                    'autoevolution', 1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Insert a second pending row so `publish 1` still resolves to an index.
        # We'll resolve index 1 by patching list_pending to return the vanished link.
        with patch('pending_articles_repo.list_pending') as mock_list:
            mock_list.return_value = [{
                'link': entry['link'], 'source_name': 'autoevolution',
                'title': 'EN', 'fetched_at': '2026-04-20 10:00:00',
                'blocks': None, 'paragraphs': [],
            }]
            # Delete pending row directly — race simulation.
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM pending_articles WHERE link=?",
                             (entry['link'],))
                conn.commit()
            finally:
                conn.close()

            mock_publish = MagicMock()
            mock_teaser = MagicMock()
            with patch('hw_review.publish_article', mock_publish), \
                 patch('hw_review.send_telegraph_teaser', mock_teaser):
                rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        err = self.stderr.getvalue()
        self.assertIn('already published', err)
        self.assertIn('https://telegra.ph/OLD-URL', err)
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()

    def test_publish_vanished_row_in_failed(self):
        entry = self._insert(link='http://a/1', title='EN')

        # Insert into failed_articles + remove from pending.
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO failed_articles "
                "(link, title, source_name, paragraphs, last_error) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry['link'], 'EN', 'autoevolution', '[]', 'oh no'),
            )
            conn.commit()
        finally:
            conn.close()

        with patch('pending_articles_repo.list_pending') as mock_list:
            mock_list.return_value = [{
                'link': entry['link'], 'source_name': 'autoevolution',
                'title': 'EN', 'fetched_at': '2026-04-20 10:00:00',
                'blocks': None, 'paragraphs': [],
            }]
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM pending_articles WHERE link=?",
                             (entry['link'],))
                conn.commit()
            finally:
                conn.close()

            mock_publish = MagicMock()
            mock_teaser = MagicMock()
            with patch('hw_review.publish_article', mock_publish), \
                 patch('hw_review.send_telegraph_teaser', mock_teaser):
                rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        err = self.stderr.getvalue()
        self.assertIn('failed', err)
        self.assertIn('oh no', err)
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()

    def test_publish_vanished_row_not_found(self):
        entry = self._insert(link='http://a/1', title='EN')

        with patch('pending_articles_repo.list_pending') as mock_list:
            mock_list.return_value = [{
                'link': entry['link'], 'source_name': 'autoevolution',
                'title': 'EN', 'fetched_at': '2026-04-20 10:00:00',
                'blocks': None, 'paragraphs': [],
            }]
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM pending_articles WHERE link=?",
                             (entry['link'],))
                conn.commit()
            finally:
                conn.close()

            mock_publish = MagicMock()
            mock_teaser = MagicMock()
            with patch('hw_review.publish_article', mock_publish), \
                 patch('hw_review.send_telegraph_teaser', mock_teaser):
                rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        self.assertIn('not found', self.stderr.getvalue())
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()


# ------------------------------------------------------------------
# Not-staged precondition
# ------------------------------------------------------------------


class TestPublishPrecondition(_PublishCase):

    def test_publish_rejects_unstaged_row(self):
        """ru_paragraphs IS NULL → exit 1, no external calls."""
        entry = self._insert(link='http://a/1', title='EN')
        # Deliberately do NOT stage.

        mock_publish = MagicMock()
        mock_teaser = MagicMock()
        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        self.assertIn('nothing to publish', self.stderr.getvalue())
        mock_publish.assert_not_called()
        mock_teaser.assert_not_called()
        # Row unchanged.
        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertIsNone(row['ru_paragraphs'])


# ------------------------------------------------------------------
# Telegraph-only failure
# ------------------------------------------------------------------


class TestPublishTelegraphFailure(_PublishCase):

    def test_publish_telegraph_failure_leaves_row_intact(self):
        """publish_article raises TelegraphError → exit 1, pending row
        untouched (telegraph_url still NULL), no teaser call."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        mock_publish = MagicMock(side_effect=TelegraphError('telegraph down'))
        mock_teaser = MagicMock()

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        mock_teaser.assert_not_called()

        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertIsNone(row['telegraph_url'])

    def test_publish_requests_exception_leaves_row_intact(self):
        """requests.RequestException path — same contract."""
        import requests
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        mock_publish = MagicMock(side_effect=requests.RequestException('net'))
        mock_teaser = MagicMock()

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 1)
        mock_teaser.assert_not_called()
        row = repo.get_pending(entry['link'])
        self.assertIsNotNone(row)
        self.assertIsNone(row['telegraph_url'])


# ------------------------------------------------------------------
# Hashtag wiring (Decision 14)
# ------------------------------------------------------------------


class TestPublishHashtagWiring(_PublishCase):

    def test_publish_passes_source_url_to_teaser_for_hashtag(self):
        """Decision 14 guard: send_telegraph_teaser must be called with the
        article's real source URL (row['link']), so the hashtag continues to
        derive from it via _source_hashtag()."""
        entry = self._insert(link='https://lamleygroup.com/post/foo',
                             title='EN', source='lamley')
        self._stage(entry['link'])

        tg_url = 'https://telegra.ph/EN-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 0, self.stderr.getvalue())

        # Teaser called with (telegraph_url, row['link']).
        self.assertEqual(mock_teaser.call_count, 1)
        call = mock_teaser.call_args
        # Accept positional or keyword forms; check both args equal.
        args = call.args if call.args else (call.kwargs.get('telegraph_url'),
                                            call.kwargs.get('source_url'))
        self.assertEqual(args[0], tg_url)
        self.assertEqual(args[1], entry['link'])

        # Sanity spot-check: _source_hashtag on the passed URL yields
        # #lamleygroup (not #lamley — the internal source_name).
        self.assertEqual(news_bot._source_hashtag(args[1]), '#lamleygroup')

    def test_publish_url_identity_happy_and_retry(self):
        """Both runs of a retry pass the SAME URL to the teaser."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        tg_url = 'https://telegra.ph/Consistent-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(side_effect=[False, True])

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            self._run(['publish', '1'])
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()
            self._run(['publish', '1'])

        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_teaser.call_count, 2)
        first_url = mock_teaser.call_args_list[0][0][0]
        second_url = mock_teaser.call_args_list[1][0][0]
        self.assertEqual(first_url, tg_url)
        self.assertEqual(second_url, tg_url)


# ------------------------------------------------------------------
# Preview-file cleanup
# ------------------------------------------------------------------


class TestPublishPreviewCleanup(_PublishCase):

    def test_publish_deletes_local_preview_file(self):
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        # Write a real tempfile and pin it on the row.
        fd, tmppath = tempfile.mkstemp(suffix='.html')
        os.close(fd)
        with open(tmppath, 'w') as f:
            f.write('<html>preview</html>')
        self.assertTrue(os.path.exists(tmppath))
        repo.set_preview_path(entry['link'], tmppath)

        tg_url = 'https://telegra.ph/EN-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 0, self.stderr.getvalue())
        self.assertFalse(os.path.exists(tmppath),
                         'preview file should be deleted after publish')

    def test_publish_missing_preview_file_is_tolerated(self):
        """preview_html_path points at a file that's already gone (e.g. after a
        re-preview) — publish must not crash."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        repo.set_preview_path(entry['link'], '/tmp/does-not-exist-hw.html')

        tg_url = 'https://telegra.ph/EN-04-22'
        mock_publish = MagicMock(return_value=tg_url)
        mock_teaser = MagicMock(return_value=True)

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 0, self.stderr.getvalue())


# ------------------------------------------------------------------
# Retry path — verify teaser uses the PREVIOUSLY-STORED URL
# ------------------------------------------------------------------


class TestPublishRetryRestoresStoredUrl(_PublishCase):

    def test_retry_uses_previously_stored_url_even_if_publish_would_return_new(self):
        """Simulate a row that already has telegraph_url set (e.g. from a
        previous failed send). The second publish invocation must reuse the
        stored URL verbatim and NOT call publish_article."""
        entry = self._insert(link='http://a/1', title='EN')
        self._stage(entry['link'])

        stored_url = 'https://telegra.ph/STORED-04-20'
        stored_path = 'STORED-04-20'
        repo.mark_telegraph_published(entry['link'], stored_url, stored_path)

        # publish_article would return a DIFFERENT URL if called — we assert
        # it's never called.
        mock_publish = MagicMock(return_value='https://telegra.ph/NEW-URL')
        mock_teaser = MagicMock(return_value=True)

        with patch('hw_review.publish_article', mock_publish), \
             patch('hw_review.send_telegraph_teaser', mock_teaser):
            rc = self._run(['publish', '1'])

        self.assertEqual(rc, 0, self.stderr.getvalue())
        mock_publish.assert_not_called()
        # Teaser got the STORED url.
        self.assertEqual(mock_teaser.call_args[0][0], stored_url)

        # Published row records the stored URL.
        pub = repo.get_published(entry['link'])
        self.assertEqual(pub['telegraph_url'], stored_url)
        self.assertEqual(pub['telegraph_path'], stored_path)


if __name__ == '__main__':
    unittest.main()
