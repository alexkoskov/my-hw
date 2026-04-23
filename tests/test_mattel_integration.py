#!/usr/bin/env python3
"""Integration tests for the Mattel source end-to-end via the prep phase.

After manual-review-workflow Task 6, ``news_bot.job()`` stages Mattel
entries into ``pending_articles`` and does NOT publish to Telegraph or
Telegram. These tests verify the staging path end-to-end: the HTTP
fixture is parsed by the real Mattel source, routed through the
``SOURCES`` registry, filtered against the dedup tables, and inserted
into the queue.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

import news_bot
import pending_articles_repo

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "mattel_news.html"
)


class TestMattelIntegration(unittest.TestCase):
    """Full pipeline: HTTP fixture → job() → SQLite (pending_articles)."""

    def setUp(self):
        with open(FIXTURE_PATH, encoding="utf-8") as f:
            self.fixture_html = f.read()

        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        self.db_patcher = patch("news_bot.DB_FILE", self.db_path)
        self.db_patcher.start()
        news_bot.init_db()

        self.token_patcher = patch("news_bot.TELEGRAM_BOT_TOKEN", "mock_token")
        self.channel_patcher = patch("news_bot.TELEGRAM_CHANNEL_ID", "@mock_channel")
        self.admin_patcher = patch("news_bot.TELEGRAM_ADMIN_ID", "@admin")
        self.token_patcher.start()
        self.channel_patcher.start()
        self.admin_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        self.admin_patcher.stop()
        os.unlink(self.db_path)

    def _make_response(self, text="", raise_exc=None):
        resp = MagicMock(spec=requests.Response)
        resp.text = text
        resp.raise_for_status.side_effect = raise_exc
        if raise_exc is None:
            resp.raise_for_status.return_value = None
        return resp

    @patch("news_bot.send_admin_notification")
    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.telegraph_publisher.publish_article")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("news_bot.fetch_mattel_article",
           return_value={"title": "Hot Wheels article", "subtitle": "",
                         "paragraphs": ["Body."], "images": []})
    @patch("news_bot.transcreate_text", side_effect=lambda t, **k: t)
    @patch("mattel_news_source.requests.get")
    def test_mattel_post_flows_into_pending_queue(
        self, mock_get, mock_transcreate, mock_article, mock_feeds,
        mock_fetch_rss, mock_publish, mock_tg, mock_admin,
    ):
        """Mattel HW entry is staged into ``pending_articles`` with
        ``source_name='mattel'`` and is NOT sent to Telegram or Telegraph."""
        mock_get.return_value = self._make_response(text=self.fixture_html)

        news_bot.job()

        # Prep-phase never publishes.
        mock_publish.assert_not_called()
        mock_tg.assert_not_called()
        mock_transcreate.assert_not_called()

        # Exactly one Hot-Wheels Mattel row was staged.
        rows = pending_articles_repo.list_pending()
        self.assertEqual(len(rows), 1, f"unexpected rows: {rows!r}")
        row = rows[0]
        self.assertEqual(row['source_name'], 'mattel')
        self.assertIn('corporate.mattel.com/news/', row['link'])
        self.assertIn('hot-wheels', row['link'])

        # No row in processed_news yet — moves there only on operator publish.
        conn = sqlite3.connect(self.db_path)
        try:
            rows_processed = conn.execute(
                "SELECT link FROM processed_news"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows_processed, [])

    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("news_bot.send_admin_notification")
    @patch("mattel_news_source.requests.get")
    def test_mattel_http_failure_does_not_crash_job(
        self, mock_get, mock_notify, mock_feeds, mock_fetch_rss, mock_tg
    ):
        """HTTP failure notifies admin and job keeps running (no staging,
        no publish)."""
        mock_get.side_effect = requests.ConnectionError("boom")

        news_bot.job()

        mock_notify.assert_called()
        mock_tg.assert_not_called()
        self.assertEqual(pending_articles_repo.count_pending(), 0)

    @patch("news_bot.send_admin_notification")
    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.telegraph_publisher.publish_article")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("mattel_news_source.requests.get")
    def test_mattel_duplicate_is_not_restaged(
        self, mock_get, mock_feeds, mock_fetch_rss, mock_publish,
        mock_tg, mock_admin,
    ):
        """Second tick with the same fixture does not create a second
        pending row — the PRIMARY KEY on ``pending_articles.link``
        rejects the duplicate."""
        mock_get.return_value = self._make_response(text=self.fixture_html)

        article_mock = {"title": "Hot Wheels article", "subtitle": "",
                        "paragraphs": ["Body."], "images": []}
        with patch("news_bot.fetch_mattel_article", return_value=article_mock):
            with patch("news_bot.transcreate_text", side_effect=lambda t, **k: t):
                news_bot.job()
                self.assertEqual(pending_articles_repo.count_pending(), 1)
                mock_tg.assert_not_called()
                mock_publish.assert_not_called()

                news_bot.job()
                self.assertEqual(pending_articles_repo.count_pending(), 1)
                mock_tg.assert_not_called()
                mock_publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
