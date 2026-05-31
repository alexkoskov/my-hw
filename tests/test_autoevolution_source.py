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
<p>The rare Porsche is finally here. See <a href="https://mattel.com/rlc">Red Line Club</a> for details.</p>
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
<div class="newsgal2 posrel"><div class="vslide">
  <a href="https://s1.cdn.example/images/news/gallery/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche_1.jpg"><img
    src="https://s1.cdn.example/images/news-gallery-130x/hot-wheels-chase-thumb_1.jpg"
    data-description="Photo credits: Mattel" /></a>
  <a href="https://s1.cdn.example/images/news/gallery/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche_2.jpg"><img
    src="https://s1.cdn.example/images/news-gallery-130x/hot-wheels-chase-thumb_2.jpg"
    data-description="Photo credits: Mattel" /></a>
  <a href="https://s1.cdn.example/images/news/gallery/hot-wheels-chase-car-to-hunt-for-is-a-rare-porsche_3.jpg"><img
    src="https://s1.cdn.example/images/news-gallery-130x/hot-wheels-chase-thumb_3.jpg"
    data-description="Photo credits: Mattel" /></a>
  <a href="https://s1.cdn.example/images/news/gallery/different-story_5.jpg"><img
    src="https://s1.cdn.example/images/news-gallery-130x/different-thumb_5.jpg" /></a>
</div></div>
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
        # with inline image + heading + video in their positions. Gallery
        # photos appended at the end. Sibling story links and placeholders
        # are filtered out.
        types = [b["type"] for b in out["blocks"]]
        assert types == [
            "image",        # hero (ch_pic.mainpic)
            "lead",         # bold intro
            "paragraph",    # "The rare Porsche..."
            "paragraph",    # "Production run..."
            "image",        # inline gallery image (body _1)
            "heading",      # "Why this matters"
            "paragraph",    # "Collectors have waited..."
            "video",        # YouTube embed
            # Gallery at page bottom: _1 skipped (same URL as inline),
            # _2 and _3 appended; sibling-story _5 filtered by slug.
            "image",        # gallery _2
            "image",        # gallery _3
        ]
        # Gallery images carry the data-description as caption
        assert out["blocks"][-2]["caption"] == "Photo credits: Mattel"
        assert out["blocks"][-1]["caption"] == "Photo credits: Mattel"
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

        # Paragraph with an inline external link carries a `runs` list so the
        # link survives translation and reaches Telegraph as a real <a>.
        porsche_para = out["blocks"][2]
        assert porsche_para["type"] == "paragraph"
        assert porsche_para["runs"] == [
            {"text": "The rare Porsche is finally here. See "},
            {"text": "Red Line Club", "href": "https://mattel.com/rlc"},
            {"text": " for details."},
        ]

        # Back-compat flat lists still populated
        assert "Why this matters" in out["paragraphs"]
        assert "Bold intro paragraph from the editor." in out["paragraphs"]
        # Hero + inline body image + 2 gallery images (dedup removed duplicate _1).
        assert len(out["images"]) == 4

    def test_extracts_heading_nested_inside_paragraph(self):
        """Regression for 2026-05-13: autoevolution article 269773 wrapped
        every section title in `<h2 class="bold dispblock">` *nested
        inside* `<p>` (invalid HTML, but consistent across their CMS).
        Without the detach pass these headings disappeared into the
        paragraph text — e.g. block emitted `"BMW M1 Procar Here's a
        tough question…"` instead of separate heading + paragraph."""
        html = """
        <html><body>
        <h1>Test title</h1>
        <div class="newstext">
        <p><h2 class="bold dispblock mgtop_20 mgbot_10">BMW M1 Procar</h2>
        Here's a tough question for you about the BMW.</p>
        <p>Standalone paragraph without nested heading.</p>
        <p><h3>Inline H3 Section</h3>Body for the h3 section.</p>
        </div>
        </body></html>
        """
        fetcher = lambda url: _fake_response(html)
        out = _scrape_article_page(
            "https://www.autoevolution.com/news/x-100.html",
            fetcher=fetcher,
        )
        assert out is not None
        text_blocks = [(b["type"], b.get("text"), b.get("level"))
                       for b in out["blocks"]
                       if b["type"] in ("paragraph", "heading")]
        # DOM order: h2 + its paragraph, plain paragraph, h3 + its paragraph.
        assert text_blocks == [
            ("heading", "BMW M1 Procar", 2),
            ("paragraph", "Here's a tough question for you about the BMW.", None),
            ("paragraph", "Standalone paragraph without nested heading.", None),
            ("heading", "Inline H3 Section", 3),
            ("paragraph", "Body for the h3 section.", None),
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
        # Back-compat flat text now includes inline link text
        assert any(
            "The rare Porsche is finally here." in p and "Red Line Club" in p
            for p in out["paragraphs"]
        )

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

    def test_returns_none_when_scrape_fails_and_no_rss_images(self):
        # Reproduces incident 2026-05-31: autoevolution Boulevard Mix was
        # 403'd on scrape, fell back to RSS-only enrichment, but the RSS
        # entry carried no media_thumbnail / media_content. The article was
        # staged with images=[] and published to Telegraph the next morning
        # without a hero, so the Telegram teaser had no preview image.
        # Fix: defer (return None) when scrape fails AND RSS has no hero —
        # news_bot skips the entry without marking processed, so next tick
        # retries from scratch (autoevolution 403's are single-tick spikes).
        def failing(url):
            raise RuntimeError("403")
        entry = {
            "link": "https://www.autoevolution.com/news/boulevard-mix-270739.html",
            "title": "Boulevard Mix",
            "summary": "Short RSS summary.",
            # NO media_thumbnail / media_content — the failure pattern.
        }
        out = fetch_autoevolution_article(entry, fetcher=failing)
        assert out is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
