#!/usr/bin/env python3
"""Unit tests for mattel_news_source."""

import json
import os
import time
from unittest.mock import MagicMock

import pytest
import requests

from mattel_news_source import (
    ARTICLE_URL_PREFIX,
    MAX_RESPONSE_SIZE,
    NEWS_URL,
    MattelNewsError,
    _build_entry,
    _extract_entries,
    _is_hotwheels,
    fetch_mattel_article,
    fetch_mattel_news,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "mattel_news.html"
)


@pytest.fixture(scope="module")
def fixture_html():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return f.read()


def _make_response(text="", status_code=200, raise_exc=None):
    resp = MagicMock(spec=requests.Response)
    resp.text = text
    resp.content = text.encode("utf-8") if text else b""
    resp.status_code = status_code
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestIsHotwheels:
    def test_matches_title(self):
        assert _is_hotwheels({"title": "New Hot Wheels Legends Tour", "handle": ""})

    def test_matches_handle(self):
        assert _is_hotwheels({"title": "Unrelated", "handle": "2026-hot-wheels-legends"})

    def test_case_insensitive(self):
        assert _is_hotwheels({"title": "HOT WHEELS News", "handle": ""})

    def test_does_not_match_unrelated(self):
        assert not _is_hotwheels({"title": "Barbie update", "handle": "barbie-news"})

    def test_missing_fields(self):
        assert not _is_hotwheels({})


class TestBuildEntry:
    def test_builds_all_fields(self):
        raw = {
            "handle": "example-slug",
            "title": "Example",
            "excerpt": "Some excerpt",
            "date": "2026-04-13",
        }
        entry = _build_entry(raw)
        assert entry["link"] == ARTICLE_URL_PREFIX + "example-slug"
        assert entry["title"] == "Example"
        assert entry["summary"] == "Some excerpt"
        assert entry["published_parsed"] == time.strptime("2026-04-13", "%Y-%m-%d")
        assert entry["feed_url"] == NEWS_URL

    def test_empty_excerpt_falls_back_to_title(self):
        raw = {"handle": "x", "title": "Title Only", "excerpt": ""}
        assert _build_entry(raw)["summary"] == "Title Only"

    def test_missing_handle_returns_none(self):
        assert _build_entry({"title": "x"}) is None

    def test_missing_title_returns_none(self):
        assert _build_entry({"handle": "x"}) is None

    def test_invalid_date_keeps_entry(self):
        entry = _build_entry({"handle": "x", "title": "y", "date": "not-a-date"})
        assert entry is not None
        assert entry["published_parsed"] is None


class TestExtractEntries:
    def test_extracts_from_fixture(self, fixture_html):
        entries = _extract_entries(fixture_html)
        assert isinstance(entries, list)
        assert len(entries) >= 1
        # At least one real entry should have the key fields
        assert any("title" in e and "handle" in e for e in entries)

    def test_missing_next_data_raises(self):
        with pytest.raises(MattelNewsError, match="__NEXT_DATA__"):
            _extract_entries("<html><body>no data</body></html>")

    def test_invalid_json_raises(self):
        bad = '<script id="__NEXT_DATA__" type="application/json">{ invalid }</script>'
        with pytest.raises(MattelNewsError, match="Invalid"):
            _extract_entries(bad)

    def test_missing_entries_path_raises(self):
        payload = json.dumps({"props": {"pageProps": {"page": {}}}})
        html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
        with pytest.raises(MattelNewsError, match="not found"):
            _extract_entries(html)


class TestFetchMattelNews:
    def test_success_filters_hotwheels_only(self, fixture_html):
        session = MagicMock()
        session.get.return_value = _make_response(text=fixture_html)

        entries = fetch_mattel_news(session=session)

        assert len(entries) == 1
        entry = entries[0]
        assert "hot wheels" in entry["title"].lower() or "hot-wheels" in entry["link"].lower()
        assert entry["link"].startswith(ARTICLE_URL_PREFIX)
        assert entry["feed_url"] == NEWS_URL
        assert entry["published_parsed"] is not None

    def test_http_error_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(
            raise_exc=requests.HTTPError("500 server error")
        )
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "HTTP error" in notifier.call_args[0][0]

    def test_connection_error_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()

    def test_missing_next_data_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(text="<html></html>")
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "parsing error" in notifier.call_args[0][0]

    def test_invalid_json_returns_empty_and_notifies(self):
        bad_html = (
            '<script id="__NEXT_DATA__" type="application/json">{bad json}</script>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=bad_html)
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()

    def test_missing_entries_path_returns_empty_and_notifies(self):
        payload = json.dumps({"props": {"pageProps": {"page": {}}}})
        html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()

    def test_notifier_failure_does_not_raise(self):
        session = MagicMock()
        session.get.return_value = _make_response(text="<html></html>")
        notifier = MagicMock(side_effect=RuntimeError("notifier broken"))

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()

    def test_oversized_response_returns_empty_and_notifies(self):
        session = MagicMock()
        response = _make_response(text="ok")
        response.content = b"x" * (MAX_RESPONSE_SIZE + 1)
        session.get.return_value = response
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "too large" in notifier.call_args[0][0].lower()

    def test_no_notifier_is_ok(self):
        session = MagicMock()
        session.get.return_value = _make_response(text="<html></html>")

        entries = fetch_mattel_news(session=session)

        assert entries == []


class TestFetchMattelArticle:
    def _article_page(self, body_html='<p>A paragraph.</p><p>Second.</p><ul><li>Bullet</li></ul>',
                       thumb_url='https://images.example/thumb.jpg',
                       download_media=None, excerpt='Editorial lead text'):
        article = {
            'title': 'Sample Mattel Article',
            'body': body_html,
            'excerpt': excerpt,
            'download_media': download_media or [],
        }
        payload = {
            'props': {
                'pageProps': {
                    'contentArticle': {
                        'thumbnail': {'url': thumb_url} if thumb_url else {},
                        'result': article,
                    }
                }
            }
        }
        html = (
            f'<html><body>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
            f'</body></html>'
        )
        return html

    def test_parses_paragraphs_and_images(self):
        session = MagicMock()
        session.get.return_value = _make_response(
            text=self._article_page(download_media=[{'url': 'https://images.example/media.png'}]),
        )
        out = fetch_mattel_article('https://corporate.mattel.com/news/x', session=session)
        assert out['title'] == 'Sample Mattel Article'
        assert out['subtitle'] == 'Editorial lead text'
        assert out['paragraphs'] == ['A paragraph.', 'Second.', 'Bullet']
        assert out['images'] == [
            'https://images.example/thumb.jpg',
            'https://images.example/media.png',
        ]

    def test_missing_excerpt_yields_empty_subtitle(self):
        session = MagicMock()
        session.get.return_value = _make_response(text=self._article_page(excerpt=''))
        out = fetch_mattel_article('https://corporate.mattel.com/news/x', session=session)
        assert out['subtitle'] == ''

    def test_http_error_returns_none_and_notifies(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError('boom')
        notifier = MagicMock()
        out = fetch_mattel_article('https://x', session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()

    def test_missing_next_data_returns_none_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(text='<html></html>')
        notifier = MagicMock()
        out = fetch_mattel_article('https://x', session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()

    def test_oversized_response_returns_none(self):
        session = MagicMock()
        resp = _make_response(text='ok')
        resp.content = b'x' * (MAX_RESPONSE_SIZE + 1)
        session.get.return_value = resp
        notifier = MagicMock()
        out = fetch_mattel_article('https://x', session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()

    def test_null_content_article_returns_none_with_readable_error(self):
        # Next.js 404 pages return {'contentArticle': null} in __NEXT_DATA__.
        # The fetcher must surface a readable admin notification, not a
        # cryptic "'NoneType' object is not subscriptable".
        session = MagicMock()
        payload = {'props': {'pageProps': {'contentArticle': None}}}
        html = (
            '<html><body>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
            '</body></html>'
        )
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()
        out = fetch_mattel_article('https://x', session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()
        (msg,), _ = notifier.call_args
        assert 'contentArticle is null' in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
