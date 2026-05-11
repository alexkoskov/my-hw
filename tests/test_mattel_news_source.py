#!/usr/bin/env python3
"""Unit tests for mattel_news_source (RSC flight-payload parser)."""

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
from tests.fixtures.mattel_flight_builder import (
    _make_flight_article,
    _make_flight_listing,
)


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


def _hw_entry(**overrides):
    """Default HW listing-shape entry; overrides win."""
    base = {
        "handle": "hot-wheels-legends-tour-2026",
        "title": "Hot Wheels Legends Tour Returns",
        "date": "2026-04-13",
        "excerpt": "Editorial lead text",
        "seo_description": "SEO desc",
        "thumbnail": {"url": "https://example.com/thumb.jpg"},
        "url": "https://corporate.mattel.com/news/hot-wheels-legends-tour-2026",
        "download_media": [],
    }
    base.update(overrides)
    return base


def _non_hw_entry(handle="masters-of-the-universe-promo", title="MOTU Promo"):
    return {
        "handle": handle,
        "title": title,
        "date": "2026-04-09",
        "excerpt": "",
        "seo_description": "",
        "thumbnail": {"url": "https://example.com/x.jpg"},
        "url": f"https://corporate.mattel.com/news/{handle}",
        "download_media": [],
    }


# ---------------------------------------------------------------------------
# Pure-helper tests (parser-agnostic; bodies preserved 1:1)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _extract_entries — operates on the listing flight payload
# ---------------------------------------------------------------------------


class TestExtractEntries:
    def test_extracts_entries_from_synthetic_flight(self):
        e1 = _hw_entry(handle="hot-wheels-a", title="HW A")
        e2 = _non_hw_entry(handle="other-b", title="B")
        e3 = _non_hw_entry(handle="other-c", title="C")
        html = _make_flight_listing([e1, e2, e3])
        entries = _extract_entries(html)
        assert isinstance(entries, list)
        assert len(entries) == 3
        # Field-level shape preserved end-to-end
        for raw in entries:
            assert "handle" in raw
            assert "title" in raw
            assert "date" in raw

    def test_missing_flight_payload_raises(self):
        with pytest.raises(MattelNewsError, match="flight payload not found"):
            _extract_entries("<html><body>no data</body></html>")

    def test_invalid_json_raises(self):
        # Build a flight push whose envelope contains malformed JSON inside
        # article2.entries.
        broken_payload = '{"props":{"pageProps":{"page":{"data":{"state":{"article2":{"entries":[{ broken json }]}}}}}}}'
        from tests.fixtures.mattel_flight_builder import _push
        html = (
            "<html><body>"
            + _push("6:" + broken_payload)
            + "</body></html>"
        )
        with pytest.raises(MattelNewsError, match="invalid JSON in entries array"):
            _extract_entries(html)

    def test_missing_article2_anchor_raises(self):
        # Push exists but contains no article2.entries anchor at all.
        from tests.fixtures.mattel_flight_builder import _push
        html = (
            "<html><body>"
            + _push('6:{"props":{"pageProps":{"page":{}}}}')
            + "</body></html>"
        )
        with pytest.raises(MattelNewsError, match="article2.entries not found"):
            _extract_entries(html)


# ---------------------------------------------------------------------------
# fetch_mattel_news
# ---------------------------------------------------------------------------


class TestFetchMattelNews:
    def test_success_filters_hotwheels_only(self):
        html = _make_flight_listing(
            [
                _hw_entry(),
                _non_hw_entry(handle="motu-x", title="MOTU x"),
                _non_hw_entry(handle="barbie-y", title="Barbie y"),
            ]
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        entries = fetch_mattel_news(session=session)

        assert len(entries) == 1
        entry = entries[0]
        assert "hot-wheels" in entry["link"].lower()
        assert entry["link"].startswith(ARTICLE_URL_PREFIX)
        assert entry["feed_url"] == NEWS_URL
        assert entry["published_parsed"] is not None
        # AC2: exactly 5 keys
        assert set(entry.keys()) == {
            "link", "title", "summary", "published_parsed", "feed_url"
        }

    def test_listing_with_no_hotwheels_returns_empty_without_notifier(self):
        # AC3: 3 non-HW entries → [] AND notifier NOT called.
        html = _make_flight_listing(
            [
                _non_hw_entry(handle="motu-a", title="MOTU A"),
                _non_hw_entry(handle="barbie-b", title="Barbie B"),
                _non_hw_entry(handle="masters-c", title="Masters C"),
            ]
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_not_called()

    def test_http_error_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(
            raise_exc=requests.HTTPError("500 server error")
        )
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        msg = notifier.call_args[0][0]
        assert "[E020]" in msg
        assert "Mattel" in msg
        # Sanitised: no raw exception string
        assert "500 server error" not in msg

    def test_connection_error_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()

    def test_missing_flight_payload_returns_empty_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(text="<html></html>")
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "flight payload not found" in notifier.call_args[0][0]

    def test_invalid_json_returns_empty_and_notifies(self):
        from tests.fixtures.mattel_flight_builder import _push
        broken = '{"props":{"pageProps":{"page":{"data":{"state":{"article2":{"entries":[{bad}]}}}}}}}'
        bad_html = "<html><body>" + _push("6:" + broken) + "</body></html>"
        session = MagicMock()
        session.get.return_value = _make_response(text=bad_html)
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "invalid JSON in entries array" in notifier.call_args[0][0]

    def test_missing_article2_anchor_returns_empty_and_notifies(self):
        from tests.fixtures.mattel_flight_builder import _push
        html = (
            "<html><body>"
            + _push('6:{"props":{"pageProps":{"page":{}}}}')
            + "</body></html>"
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        entries = fetch_mattel_news(session=session, notifier=notifier)

        assert entries == []
        notifier.assert_called_once()
        assert "article2.entries not found" in notifier.call_args[0][0]

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

    def test_uses_allow_redirects_false(self):
        # Decision 8: redirects must be disabled to avoid CDN-edge surprises.
        html = _make_flight_listing([_hw_entry()])
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        fetch_mattel_news(session=session)

        _, kwargs = session.get.call_args
        assert kwargs.get("allow_redirects") is False


# ---------------------------------------------------------------------------
# fetch_mattel_article
# ---------------------------------------------------------------------------


class TestFetchMattelArticle:
    def test_parses_paragraphs_and_uses_thumbnail_only(self):
        # Image policy lock from patterns.md: only thumbnail surfaces;
        # ``download_media`` press-kit assets are dropped.
        entry = _hw_entry(
            handle="hw-x",
            title="Sample Mattel Article",
            excerpt="Editorial lead text",
            thumbnail={"url": "https://example.com/thumb.jpg"},
            download_media=[
                {"url": "https://example.com/media.png"},
                {"url": "https://example.com/hi-res.jpg"},
            ],
        )
        body = "<p>A paragraph.</p><p>Second.</p><ul><li>Bullet</li></ul>"
        html = _make_flight_article(entry, body_html=body)
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x", session=session
        )

        assert out["title"] == "Sample Mattel Article"
        assert out["subtitle"] == "Editorial lead text"
        assert out["paragraphs"] == ["A paragraph.", "Second.", "Bullet"]
        assert out["images"] == ["https://example.com/thumb.jpg"]

    def test_no_thumbnail_yields_empty_images_regardless_of_download_media(self):
        entry = _hw_entry(
            handle="hw-x",
            thumbnail=None,
            download_media=[{"url": "https://example.com/press.jpg"}],
        )
        # Strip thumbnail entirely to exercise the absent-thumbnail branch.
        del entry["thumbnail"]
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x", session=session
        )

        assert out["images"] == []

    def test_missing_excerpt_yields_empty_subtitle(self):
        entry = _hw_entry(handle="hw-x", excerpt="")
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x", session=session
        )

        assert out["subtitle"] == ""

    def test_dict_form_excerpt_extracts_text_field(self):
        # AC6: excerpt may be a dict {"text": "..."} on some entries.
        entry = _hw_entry(handle="hw-x", excerpt={"text": "Dict-form lead"})
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x", session=session
        )

        assert out["subtitle"] == "Dict-form lead"

    def test_http_error_returns_none_and_notifies(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()
        # Sanitised: no raw exception text
        assert "boom" not in notifier.call_args[0][0]

    def test_oversized_response_returns_none(self):
        session = MagicMock()
        resp = _make_response(text="ok")
        resp.content = b"x" * (MAX_RESPONSE_SIZE + 1)
        session.get.return_value = resp
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()
        assert "too large" in notifier.call_args[0][0].lower()

    def test_missing_payload_returns_none_and_notifies(self):
        session = MagicMock()
        session.get.return_value = _make_response(text="<html></html>")
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()

    def test_article_entry_not_found_returns_none_with_readable_error(self):
        # The flight has an article2.entries section but the requested handle
        # is not in it (e.g., 200-with-no-data CDN edge condition).
        entry = _hw_entry(handle="some-other-handle")
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/missing-handle",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()
        msg = notifier.call_args[0][0]
        assert "article entry not found" in msg

    def test_body_split_across_multiple_flight_chunks(self):
        # AC8: body content split across N pushes should reconstruct in
        # order with no gaps or duplicates.
        entry = _hw_entry(handle="hw-x")
        body = "<p>Alpha.</p><p>Beta.</p><p>Gamma.</p>"
        html = _make_flight_article(entry, body_html=body, body_chunks=3)
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is not None
        assert out["paragraphs"] == ["Alpha.", "Beta.", "Gamma."]
        notifier.assert_not_called()

    def test_body_absent_returns_dict_no_notifier(self):
        # AC9 part 1: entry without body field → paragraphs=[] without notifier.
        entry = _hw_entry(handle="hw-x")
        html = _make_flight_article(entry, body_html=None)
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is not None
        assert out["paragraphs"] == []
        notifier.assert_not_called()

    def test_body_truncated_returns_empty_paragraphs_no_notifier(self):
        # AC9 part 2: advertised hex-length > available content → empty
        # paragraphs (content-empty path), no notifier.
        entry = _hw_entry(handle="hw-x")
        html = _make_flight_article(
            entry, body_html="<p>x</p>", truncate=True
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x",
            session=session,
            notifier=notifier,
        )

        assert out is not None
        assert out["paragraphs"] == []
        notifier.assert_not_called()

    def test_article_falls_back_to_url_field_when_handle_mismatch(self):
        # AC7: if handle ≠ URL slug but the entry's url field equals link, lookup wins.
        entry = _hw_entry(
            handle="some-internal-slug-that-doesnt-match",
            url="https://corporate.mattel.com/news/canonical-link",
        )
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/canonical-link", session=session
        )

        assert out is not None
        assert out["paragraphs"] == ["x"]

    def test_uses_allow_redirects_false(self):
        entry = _hw_entry(handle="hw-x")
        html = _make_flight_article(entry, body_html="<p>x</p>")
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        fetch_mattel_article(
            "https://corporate.mattel.com/news/hw-x", session=session
        )

        _, kwargs = session.get.call_args
        assert kwargs.get("allow_redirects") is False


# ---------------------------------------------------------------------------
# SSRF guard (Decision 8 / ES10)
# ---------------------------------------------------------------------------


class TestSsrfGuard:
    def test_rejects_link_outside_article_url_prefix(self):
        # Decision 8 / ES10: any link not matching ARTICLE_URL_PREFIX is
        # rejected BEFORE any HTTP call. The notifier message must NOT
        # echo the malicious URL.
        session = MagicMock()
        notifier = MagicMock()

        out = fetch_mattel_article(
            "https://evil.example.com/news/foo",
            session=session,
            notifier=notifier,
        )

        assert out is None
        # No HTTP call was issued.
        session.get.assert_not_called()
        notifier.assert_called_once()
        msg = notifier.call_args[0][0]
        assert "[E023]" in msg
        assert "Mattel" in msg
        assert "evil.example.com" not in msg


# ---------------------------------------------------------------------------
# Anti-drift snapshot smokes (Decision 7) — guarded on /tmp snapshots
# ---------------------------------------------------------------------------


def test_live_listing_snapshot_parses_without_notifier():
    snapshot = "/tmp/mattel_news.html"
    if not os.path.exists(snapshot):
        pytest.skip("live listing snapshot not available")
    with open(snapshot, encoding="utf-8") as f:
        html = f.read()
    session = MagicMock()
    session.get.return_value = _make_response(text=html)
    notifier = MagicMock()

    result = fetch_mattel_news(session=session, notifier=notifier)

    assert isinstance(result, list)
    notifier.assert_not_called()


def test_live_article_snapshot_parses_without_notifier():
    snapshot = "/tmp/mattel_article.html"
    if not os.path.exists(snapshot):
        pytest.skip("live article snapshot not available")
    with open(snapshot, encoding="utf-8") as f:
        html = f.read()
    session = MagicMock()
    session.get.return_value = _make_response(text=html)
    notifier = MagicMock()

    # The live HALO snapshot is the canonical link.
    link = (
        "https://corporate.mattel.com/news/"
        "engage-for-good-names-mattel-2026-halo-corporation-of-the-year"
    )
    result = fetch_mattel_article(link, session=session, notifier=notifier)

    assert result is not None
    assert isinstance(result, dict)
    assert isinstance(result.get("paragraphs"), list)
    notifier.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
