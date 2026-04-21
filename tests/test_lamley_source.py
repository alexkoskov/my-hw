#!/usr/bin/env python3
"""Unit tests for lamley_source.fetch_lamley_article."""

from unittest.mock import MagicMock

import pytest
import requests

import lamley_source


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
