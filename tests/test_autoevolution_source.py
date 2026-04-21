#!/usr/bin/env python3
"""Unit tests for autoevolution_source."""

from unittest.mock import MagicMock

import pytest

from autoevolution_source import (
    _scrape_article_page,
    enrich_entry,
    fetch_autoevolution_article,
)


SAMPLE_ARTICLE_HTML = """
<html><body>
<h1>Hot Wheels Chase Car</h1>
<div class="mgtop_10 mgbot_10 fsz19">Editorial lead about the rare Porsche.</div>
<div class="newstext">
<p>The rare Porsche is finally here.</p>
<p>Production run details follow.</p>
<h2>Why this matters</h2>
<p>Collectors have waited months.</p>
</div>
<img src="https://s1.cdn.example/images/news/hot-wheels-chase-268757-7.jpg" />
<img src="https://s1.cdn.example/images/editors/avatar.jpg" />
<img src="https://s1.cdn.example/images/news-gallery-130x/hot-wheels-chase-thumbnail_1.jpg" />
<a href="https://s1.cdn.example/images/news/hot-wheels-chase-268757_1.jpg">gallery</a>
<a href="https://s1.cdn.example/images/news/different-story-268800-1.jpg">sibling</a>
</body></html>
"""


def _fake_response(text, status=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    return resp


class TestEnrichEntry:
    def test_basic_entry_with_summary_and_images(self):
        entry = {
            "title": "Hot Wheels Pickup",
            "summary": "A cool new pickup truck.",
            "media_content": [{"url": "https://s1.cdn.example/1.jpg", "medium": "image"}],
            "media_thumbnail": [{"url": "https://s1.cdn.example/2.jpg"}],
        }
        out = enrich_entry(entry)
        assert out["title"] == "Hot Wheels Pickup"
        assert out["paragraphs"] == ["A cool new pickup truck."]
        assert out["images"] == [
            "https://s1.cdn.example/1.jpg",
            "https://s1.cdn.example/2.jpg",
        ]

    def test_strips_continue_reading_link(self):
        entry = {
            "title": "T",
            "summary": 'Body text here. (<a href="https://example.com">continue reading...</a>)',
        }
        out = enrich_entry(entry)
        assert "continue reading" not in out["paragraphs"][0]
        assert "<a" not in out["paragraphs"][0]
        assert out["paragraphs"][0].startswith("Body text here.")

    def test_html_entities_decoded(self):
        entry = {"title": "T", "summary": "Ford &amp; Chevy"}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["Ford & Chevy"]

    def test_multiple_paragraphs_split_on_double_newline(self):
        entry = {"title": "T", "summary": "First paragraph.\n\nSecond paragraph."}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["First paragraph.", "Second paragraph."]

    def test_dedupes_images_across_media_fields(self):
        entry = {
            "title": "T",
            "summary": "x",
            "media_content": [{"url": "https://a.jpg"}],
            "media_thumbnail": [{"url": "https://a.jpg"}],
        }
        out = enrich_entry(entry)
        assert out["images"] == ["https://a.jpg"]

    def test_no_summary_falls_back_to_title(self):
        entry = {"title": "Just a title"}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["Just a title"]

    def test_returns_none_for_empty_entry(self):
        assert enrich_entry({}) is None

    def test_rss_output_has_empty_subtitle(self):
        # RSS has no subtitle — must return '' so publish_article skips the lead.
        out = enrich_entry({"title": "t", "summary": "Some body."})
        assert out["subtitle"] == ""


class TestScrapeArticlePage:
    def test_parses_title_subtitle_paragraphs_article_images(self):
        fetcher = lambda url: _fake_response(SAMPLE_ARTICLE_HTML)
        out = _scrape_article_page(
            "https://www.autoevolution.com/news/hot-wheels-chase-268757.html",
            fetcher=fetcher,
        )
        assert out["title"] == "Hot Wheels Chase Car"
        assert out["subtitle"] == "Editorial lead about the rare Porsche."
        assert out["paragraphs"] == [
            "The rare Porsche is finally here.",
            "Production run details follow.",
            "Why this matters",
            "Collectors have waited months.",
        ]
        # Only images matching article ID 268757 should be kept
        assert out["images"] == [
            "https://s1.cdn.example/images/news/hot-wheels-chase-268757-7.jpg",
            "https://s1.cdn.example/images/news/hot-wheels-chase-268757_1.jpg",
        ]

    def test_http_error_returns_none(self):
        fetcher = lambda url: _fake_response("", status=403)
        out = _scrape_article_page("https://x-268757.html", fetcher=fetcher)
        assert out is None

    def test_missing_body_returns_none(self):
        fetcher = lambda url: _fake_response("<html><h1>Only title</h1></html>")
        out = _scrape_article_page("https://x-268757.html", fetcher=fetcher)
        assert out is None

    def test_fetcher_exception_returns_none(self):
        def boom(url):
            raise RuntimeError("network dead")
        out = _scrape_article_page("https://x-268757.html", fetcher=boom)
        assert out is None


class TestFetchAutoevolutionArticle:
    def test_uses_scrape_when_successful(self):
        fetcher = lambda url: _fake_response(SAMPLE_ARTICLE_HTML)
        entry = {"link": "https://www.autoevolution.com/news/hw-268757.html",
                 "title": "RSS title", "summary": "RSS summary"}
        out = fetch_autoevolution_article(entry, fetcher=fetcher)
        assert out["title"] == "Hot Wheels Chase Car"
        assert "The rare Porsche is finally here." in out["paragraphs"]

    def test_falls_back_to_rss_when_scrape_fails(self):
        def failing(url):
            raise RuntimeError("down")
        entry = {
            "link": "https://www.autoevolution.com/news/hw-268757.html",
            "title": "RSS title",
            "summary": "Short RSS summary.",
            "media_thumbnail": [{"url": "https://s1.cdn.example/thumb.jpg"}],
        }
        out = fetch_autoevolution_article(entry, fetcher=failing)
        assert out["title"] == "RSS title"
        assert out["paragraphs"] == ["Short RSS summary."]
        assert out["images"] == ["https://s1.cdn.example/thumb.jpg"]

    def test_scrape_without_images_uses_rss_images(self):
        # Article page has no img matching its ID
        html = '<html><h1>T</h1><div class="newstext"><p>Body.</p></div></html>'
        fetcher = lambda url: _fake_response(html)
        entry = {
            "link": "https://www.autoevolution.com/news/x-999.html",
            "media_content": [{"url": "https://s1.cdn.example/rss.jpg"}],
        }
        out = fetch_autoevolution_article(entry, fetcher=fetcher)
        assert out["paragraphs"] == ["Body."]
        assert out["images"] == ["https://s1.cdn.example/rss.jpg"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
