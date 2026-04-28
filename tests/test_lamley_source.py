#!/usr/bin/env python3
"""Unit tests for lamley_source.fetch_lamley_article."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import lamley_source


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Replace ``lamley_source.time.sleep`` with a recording no-op so unit
    tests never actually wait. Tests that want to inspect sleep arguments
    can patch ``lamley_source.time.sleep`` themselves on top of this."""
    monkeypatch.setattr(lamley_source.time, "sleep", lambda s: None)
    # Reset throttle state so each test starts from a clean baseline.
    monkeypatch.setattr(lamley_source, "_last_request_time", 0.0)
    yield


def _make_response(text="", status=200, raise_exc=None, content=None):
    resp = MagicMock(spec=requests.Response)
    resp.text = text
    resp.status_code = status
    resp.content = content if content is not None else text.encode("utf-8")
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return resp


SAMPLE_HTML = """
<html>
<body>
<h1 class="entry-title">Sample Hot Wheels Post</h1>
<article>
<div class="entry-content">
<p>First paragraph of the post.</p>
<p>Second paragraph with more detail.</p>
<ul><li>Bullet one</li><li>Bullet two</li></ul>
<h3>A heading</h3>
<img src="https://cdn.example.com/img1.jpg?resize=1024" />
<img src="https://cdn.example.com/img1.jpg?resize=500" />
<img src="https://cdn.example.com/img2.jpg" />
<img src="/local/relative.jpg" />
</div>
</article>
</body>
</html>
"""


class TestFetchLamleyArticle:
    def test_parses_title_subtitle_paragraphs_images(self):
        session = MagicMock()
        session.get.return_value = _make_response(text=SAMPLE_HTML)
        out = lamley_source.fetch_lamley_article("http://lamleygroup.com/x", session=session)
        assert out["title"] == "Sample Hot Wheels Post"
        # First body paragraph is lifted out as subtitle; no duplicate in body.
        assert out["subtitle"] == "First paragraph of the post."
        assert "First paragraph of the post." not in out["paragraphs"]
        assert "Second paragraph with more detail." in out["paragraphs"]
        assert "Bullet one" in out["paragraphs"]
        assert "A heading" in out["paragraphs"]
        # Dedup by base URL, skip relative
        assert out["images"] == [
            "https://cdn.example.com/img1.jpg?resize=1024",
            "https://cdn.example.com/img2.jpg",
        ]

    def test_http_error_returns_none_and_notifies(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        notifier = MagicMock()
        out = lamley_source.fetch_lamley_article("http://x", session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()

    def test_missing_body_returns_none(self):
        session = MagicMock()
        session.get.return_value = _make_response(
            text="<html><body><h1>Only title</h1></body></html>"
        )
        notifier = MagicMock()
        out = lamley_source.fetch_lamley_article("http://x", session=session, notifier=notifier)
        assert out is None
        notifier.assert_called_once()

    def test_image_limit_applied(self):
        imgs = "\n".join(
            f'<img src="https://cdn.example/img{i}.jpg" />' for i in range(20)
        )
        html = f'<html><body><article><div class="entry-content">{imgs}</div></article></body></html>'
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        out = lamley_source.fetch_lamley_article("http://x", session=session)
        assert len(out["images"]) == lamley_source.IMAGE_LIMIT


class TestRateLimitHandling:
    """Tests for the 429-retry + module throttle introduced after a live
    burst tripped lamleygroup.com's WordPress rate-limiter."""

    def test_429_then_success_retries_and_returns_data(self, monkeypatch):
        """First response 429 → sleep ``Retry-After`` → second response 200."""
        session = MagicMock()
        first = _make_response(status=429)
        first.headers = {"Retry-After": "3"}
        session.get.side_effect = [first, _make_response(text=SAMPLE_HTML)]
        sleep_calls = []
        monkeypatch.setattr(
            lamley_source.time, "sleep", lambda s: sleep_calls.append(s),
        )
        out = lamley_source.fetch_lamley_article(
            "http://lamleygroup.com/x", session=session,
        )
        assert out is not None
        assert out["title"] == "Sample Hot Wheels Post"
        assert session.get.call_count == 2
        # Retry-After=3 was honoured (3.0 should appear in sleep calls).
        assert 3.0 in sleep_calls

    def test_429_without_retry_after_uses_default(self, monkeypatch):
        """Missing Retry-After → fall back to default delay."""
        session = MagicMock()
        first = _make_response(status=429)
        first.headers = {}
        session.get.side_effect = [first, _make_response(text=SAMPLE_HTML)]
        sleep_calls = []
        monkeypatch.setattr(
            lamley_source.time, "sleep", lambda s: sleep_calls.append(s),
        )
        out = lamley_source.fetch_lamley_article("http://x", session=session)
        assert out is not None
        assert lamley_source._DEFAULT_RETRY_AFTER_S in sleep_calls

    def test_429_twice_gives_up_via_raise_for_status(self):
        """Two 429s in a row → second .raise_for_status() throws → notifier fires."""
        session = MagicMock()
        first = _make_response(status=429)
        first.headers = {"Retry-After": "1"}
        second = _make_response(
            status=429,
            raise_exc=requests.HTTPError("429 too many"),
        )
        second.headers = {"Retry-After": "1"}
        session.get.side_effect = [first, second]
        notifier = MagicMock()
        out = lamley_source.fetch_lamley_article(
            "http://x", session=session, notifier=notifier,
        )
        assert out is None
        notifier.assert_called_once()
        assert session.get.call_count == 2

    def test_retry_after_parser_caps_huge_values(self):
        """A maliciously huge Retry-After value should be capped (not block
        the bot for hours)."""
        assert lamley_source._parse_retry_after("999999") == 120.0
        assert (
            lamley_source._parse_retry_after("not a number")
            == lamley_source._DEFAULT_RETRY_AFTER_S
        )
        assert (
            lamley_source._parse_retry_after(None)
            == lamley_source._DEFAULT_RETRY_AFTER_S
        )

    def test_throttle_enforces_min_interval(self, monkeypatch):
        """Two back-to-back ``_throttle_wait`` calls should sleep on the
        second one (within ``_MIN_REQUEST_INTERVAL_S``)."""
        sleep_calls = []
        monkeypatch.setattr(
            lamley_source.time, "sleep", lambda s: sleep_calls.append(s),
        )
        # First call sets _last_request_time to "now"; no sleep needed.
        lamley_source._throttle_wait()
        sleep_calls.clear()
        # Second call immediately after: elapsed is ~0, must sleep close
        # to the configured interval.
        lamley_source._throttle_wait()
        assert sleep_calls, "Expected throttle to sleep on back-to-back call"
        assert sleep_calls[0] > 0
        assert sleep_calls[0] <= lamley_source._MIN_REQUEST_INTERVAL_S


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
