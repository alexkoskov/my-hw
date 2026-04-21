#!/usr/bin/env python3
"""Integration tests for Mattel news source end-to-end pipeline."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

import news_bot

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "mattel_news.html"
)


class TestMattelIntegration(unittest.TestCase):
    """Full pipeline: HTTP fixture → job() → SQLite → mocked Telegram."""

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
        self.token_patcher.start()
        self.channel_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.token_patcher.stop()
        self.channel_patcher.stop()
        os.unlink(self.db_path)

    def _make_response(self, text="", raise_exc=None):
        resp = MagicMock(spec=requests.Response)
        resp.text = text
        resp.raise_for_status.side_effect = raise_exc
        if raise_exc is None:
            resp.raise_for_status.return_value = None
        return resp

    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.telegraph_publisher.publish_article", return_value="https://telegra.ph/m")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("news_bot.fetch_mattel_article",
           return_value={"title": "Hot Wheels article", "paragraphs": ["Body."], "images": []})
    @patch("news_bot.transcreate_text", side_effect=lambda t, **k: t)
    @patch("mattel_news_source.requests.get")
    def test_mattel_post_flows_into_telegram_and_db(
        self, mock_get, mock_transcreate, mock_article, mock_feeds, mock_fetch_rss, mock_publish, mock_tg
    ):
        """Mattel HW entry reaches Telegram and is persisted in DB."""
        mock_get.return_value = self._make_response(text=self.fixture_html)
        mock_tg.return_value = True

        news_bot.job()

        mock_tg.assert_called_once()
        called_link = mock_tg.call_args.args[3]
        self.assertIn("corporate.mattel.com/news/", called_link)
        self.assertIn("hot-wheels", called_link)

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT link FROM processed_news WHERE link = ?", (called_link,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("news_bot.send_admin_notification")
    @patch("mattel_news_source.requests.get")
    def test_mattel_http_failure_does_not_crash_job(
        self, mock_get, mock_notify, mock_feeds, mock_fetch_rss, mock_tg
    ):
        """HTTP failure notifies admin and job keeps running (no posts)."""
        mock_get.side_effect = requests.ConnectionError("boom")

        news_bot.job()

        mock_notify.assert_called()
        mock_tg.assert_not_called()

    @patch("news_bot.send_telegraph_teaser")
    @patch("news_bot.telegraph_publisher.publish_article", return_value="https://telegra.ph/m")
    @patch("news_bot.fetch_rss", return_value=[])
    @patch("news_bot.load_feeds", return_value=[])
    @patch("mattel_news_source.requests.get")
    def test_mattel_duplicate_is_not_reposted(self, mock_get, mock_feeds, mock_fetch_rss, mock_publish, mock_tg):
        """Second run with the same fixture does not repost the same article."""
        mock_get.return_value = self._make_response(text=self.fixture_html)
        mock_tg.return_value = True

        article_mock = {"title": "Hot Wheels article", "paragraphs": ["Body."], "images": []}
        with patch("news_bot.fetch_mattel_article", return_value=article_mock):
            with patch("news_bot.transcreate_text", side_effect=lambda t, **k: t):
                news_bot.job()
                self.assertEqual(mock_tg.call_count, 1)
                news_bot.job()
                self.assertEqual(mock_tg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
