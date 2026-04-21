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
<div class="ch_pic mainpic"><a class="fullimg"
  href="https://s1.cdn.example/images/news/hot-wheels-chase-268757-7.jpg"><picture><img
  src="https://s1.cdn.example/images/news/hot-wheels-chase-268757-7.jpg" /></picture></a>
  <div class="ch_pic_crd">Photo: Mattel</div></div>
<div class="newstext">
<div class="sanscond mgtop_20 fsz22 bold">Bold intro paragraph from the editor.</div>
<div class="mgtop_20"><img src="https://s1.cdn.example/_img/g_news.png" /></div>
<p>The rare Porsche is finally here.</p>
<p>Production run details follow.</p>
<p><div class="ch_pic mgbot_20"><a class="fullimg"
  href="https://s1.cdn.example/images/news/gallery/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche_1.jpg"><img
  src="https://s1.cdn.example/images/news-gallery-860x/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche-thumbnail_1.jpg" /></a>
  <div class="ch_pic_crd">Photo: Lamley Group</div></div></p>
<h2>Why this matters</h2>
<p>Collectors have waited months.</p>
<p><a href="https://youtu.be/abc123"><img
  src="https://img.youtube.com/vi/abc123/hqdefault.jpg" /></a></p>
<div class="ad ad300x250 ad-intext">ads here, ignore</div>
<div class="clearfix"></div>
</div>
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
    def test_parses_title_subtitle_and_ordered_blocks(self):
        fetcher = lambda url: _fake_response(SAMPLE_ARTICLE_HTML)
        out = _scrape_article_page(
            "https://www.autoevolution.com/news/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche-268757.html",
            fetcher=fetcher,
        )
        assert out["title"] == "Hot Wheels Chase Car"
        assert out["subtitle"] == "Editorial lead about the rare Porsche."

        # Blocks preserve DOM order: hero prepended first, lead, then body
        # with inline image + heading + video in their positions. Sibling
        # story links and the placeholder spinner are filtered out.
        types = [b["type"] for b in out["blocks"]]
        assert types == [
            "image",        # hero (ch_pic)
            "lead",         # bold intro
            "paragraph",    # "The rare Porsche..."
            "paragraph",    # "Production run..."
            "image",        # inline gallery image
            "heading",      # "Why this matters"
            "paragraph",    # "Collectors have waited..."
            "video",        # YouTube embed
        ]
        # Hero is the first image + caption from div.ch_pic_crd
        hero = out["blocks"][0]
        assert hero["src"] == (
            "https://s1.cdn.example/images/news/hot-wheels-chase-268757-7.jpg"
        )
        assert hero["caption"] == "Photo: Mattel"
        # Inline image uses the <a href> (full-size gallery) + its own caption
        inline = out["blocks"][4]
        assert inline["src"] == (
            "https://s1.cdn.example/images/news/gallery/"
            "hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche_1.jpg"
        )
        assert inline["caption"] == "Photo: Lamley Group"
        # YouTube URL wrapped into the Telegra.ph proxy form (raw URLs get
        # stripped by Telegraph, breaking Instant View).
        assert out["blocks"][7]["src"] == (
            "https://telegra.ph/embed/youtube?url="
            "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabc123"
        )

        # Back-compat flat lists still populated
        assert "Why this matters" in out["paragraphs"]
        assert "Bold intro paragraph from the editor." in out["paragraphs"]
        assert len(out["images"]) == 2

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
