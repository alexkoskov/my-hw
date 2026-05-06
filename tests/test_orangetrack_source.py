#!/usr/bin/env python3
"""Unit tests for orangetrack_source.

Mirrors the lamley/autoevolution test conventions: inline ``SAMPLE_*_HTML``
constants, ~30+ unit tests in topical classes (TestPrimaryPath,
TestSSRFAllowlist, TestFallbackPath, TestWPBlockDriftMitigation,
TestYouTubeEmbedWrapping, TestOrangetrackPingAggregator).

Network calls are avoided via MagicMock-injected ``requests.get`` and
monkeypatched ``feedparser.parse``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

import orangetrack_source as ots
from orangetrack_source import (
    OrangetrackPingAggregator,
    _is_allowed_orangetrack_url,
    _safe_for_ping,
    _video_embed_url,
    fetch_orangetrack_article,
)


# ---------------------------------------------------------------------------
# Inline sample HTML constants (lamley pattern)
# ---------------------------------------------------------------------------

SAMPLE_STANDARD_HTML = """
<p>The first paragraph introduces the casting.</p>
<p>The second paragraph explains its rarity.</p>
<figure class="wp-block-image"><img src="https://orangetrackdiecast.com/wp-content/uploads/img1.jpg?w=1024" srcset="https://orangetrackdiecast.com/wp-content/uploads/img1.jpg?w=300 300w, https://orangetrackdiecast.com/wp-content/uploads/img1.jpg?w=600 600w, https://orangetrackdiecast.com/wp-content/uploads/img1.jpg?w=1024 1024w" /><figcaption>An image caption</figcaption></figure>
<p>A third paragraph after the image. Read details at <a href="https://orangetrackdiecast.com/post-x">this post</a>.</p>
"""

SAMPLE_WITH_VIDEO_HTML = """
<p>Watch the unboxing below.</p>
<p><iframe src="https://www.youtube.com/embed/abc123XYZ?feature=oembed" frameborder="0"></iframe></p>
<p>Don't forget to subscribe.</p>
"""

SAMPLE_WITH_H5_HTML = """
<p>The intro paragraph.</p>
<h5>Case A: 1995 Honda NSX</h5>
<p>Details about the case A casting.</p>
<h5>Case B: 1969 Dodge Charger</h5>
<p>Details about the case B casting.</p>
"""

SAMPLE_AFFILIATE_HTML = """
<p>The first lead paragraph.</p>
<p>*QUICK LINK!* Buy from 1 Stop Diecast now.</p>
<p>The body paragraph mentions you might want to buy this on launch day.</p>
"""

SAMPLE_VIDEO_ONLY_HTML = """
<p><iframe src="https://www.youtube.com/embed/abc123XYZ" frameborder="0"></iframe></p>
"""

SAMPLE_NON_ASCII_HTML = """
<p>Mañana — the new "Honda NSX" lands. It's a beauty.</p>
"""

SAMPLE_BAD_SCHEMES_HTML = """
<p>Click <a href="javascript:alert(1)">here</a> for details.</p>
<p>Or visit <a href="//evil.example/x">our site</a> for more.</p>
<figure><img src="data:image/svg+xml,%3Csvg/%3E" /></figure>
<figure><img src="file:///etc/passwd" /></figure>
<figure><img src="https://orangetrackdiecast.com/wp-content/uploads/safe.jpg" /></figure>
"""

SAMPLE_GALLERY_HTML = """
<p>Image gallery below.</p>
<figure>
  <img src="https://orangetrackdiecast.com/wp-content/uploads/g1.jpg?w=300" />
  <figure><img src="https://orangetrackdiecast.com/wp-content/uploads/g2.jpg?w=600" /></figure>
  <figure><img src="https://orangetrackdiecast.com/wp-content/uploads/g3.jpg?w=1024" /></figure>
</figure>
<figure><img src="https://orangetrackdiecast.com/wp-content/uploads/g1.jpg?w=600" /></figure>
"""


def _make_entry(content_html: str, link: str = "https://orangetrackdiecast.com/post-x", title: str = "Sample Title"):
    return {
        "link": link,
        "title": title,
        "content": [{"value": content_html}],
        "summary": "",
        "published": "Mon, 01 Jan 2025 00:00:00 +0000",
    }


# ---------------------------------------------------------------------------
# TestPrimaryPath — content:encoded happy paths
# ---------------------------------------------------------------------------


class TestPrimaryPath:
    def test_standard_post_with_paragraphs_and_image(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_STANDARD_HTML))
        assert out is not None
        # Subtitle is empty by design — preserves alignment between
        # `paragraphs` and the paragraph-type entries in `blocks` so the
        # `_patch_text_with_ru_paragraphs` fallback splices RU translations
        # one-to-one without leaving trailing blocks in English.
        # See SESSION-2026-05-06.md.
        assert out["subtitle"] == ""
        # First paragraph stays in body (Telegraph just won't render the
        # italic `💬 «…»` lead).
        assert "The first paragraph introduces the casting." in out["paragraphs"]
        # Body paragraphs include the rest too.
        assert "The second paragraph explains its rarity." in out["paragraphs"]
        assert any("third paragraph" in p for p in out["paragraphs"])
        # Image present in flat list and in blocks.
        assert any("img1.jpg" in i for i in out["images"])
        types = [b["type"] for b in out["blocks"]]
        assert "paragraph" in types
        assert "image" in types
        # Paragraphs and blocks-paragraphs counts must match — this is
        # the alignment invariant the fix protects.
        block_paragraph_count = sum(1 for t in types if t == "paragraph")
        assert len(out["paragraphs"]) == block_paragraph_count

    def test_post_with_video_block(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_WITH_VIDEO_HTML))
        assert out is not None
        types = [b["type"] for b in out["blocks"]]
        # Order: paragraph, video, paragraph
        assert types[0] == "paragraph"
        assert "video" in types
        # Video src wrapped through Telegra.ph proxy.
        video = next(b for b in out["blocks"] if b["type"] == "video")
        assert video["src"].startswith("https://telegra.ph/embed/youtube?url=")
        # Flat paragraphs do NOT include the video.
        assert all("iframe" not in p for p in out["paragraphs"])

    def test_h5_goes_to_blocks_only(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_WITH_H5_HTML))
        assert out is not None
        types = [b["type"] for b in out["blocks"]]
        assert "heading" in types
        # h5 captured at level 5 in blocks.
        h5_blocks = [b for b in out["blocks"] if b["type"] == "heading"]
        assert all(b["level"] == 5 for b in h5_blocks)
        assert any("Case A" in b["text"] for b in h5_blocks)
        # Heading text NOT in flat paragraphs (Decision 15).
        assert all("Case A" not in p for p in out["paragraphs"])
        assert all("Case B" not in p for p in out["paragraphs"])

    def test_affiliate_standalone_short_paragraph_filtered(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_AFFILIATE_HTML))
        assert out is not None
        # The "*QUICK LINK!* Buy from 1 Stop Diecast now." standalone para
        # should be stripped via boilerplate_filter.
        assert all("QUICK LINK" not in p for p in out["paragraphs"])
        assert "QUICK LINK" not in (out["subtitle"] or "")
        # Inline reference to "buy" inside a real paragraph SURVIVES.
        assert any("buy this on launch" in p for p in out["paragraphs"])

    def test_video_only_synthesizes_paragraphs_from_title(self):
        entry = _make_entry(SAMPLE_VIDEO_ONLY_HTML, title="Video Only Post")
        out = fetch_orangetrack_article(entry)
        assert out is not None
        # Synthesized fallback so the gating field at news_bot.py:1510 passes.
        assert out["paragraphs"] == ["Video Only Post"]
        types = [b["type"] for b in out["blocks"]]
        assert "video" in types

    def test_non_ascii_title_preserved(self):
        # Curly quotes, em-dash, non-ASCII letters survive.
        out = fetch_orangetrack_article(_make_entry(SAMPLE_NON_ASCII_HTML))
        assert out is not None
        # Body text retains non-ASCII characters.
        full_text = (out["subtitle"] or "") + " " + " ".join(out["paragraphs"])
        assert "Mañana" in full_text or "ñ" in full_text

    def test_unsafe_href_dropped_keeps_anchor_text(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_BAD_SCHEMES_HTML))
        assert out is not None
        # Anchor text "here" / "our site" survive in body text.
        full_text = (out["subtitle"] or "") + " " + " ".join(out["paragraphs"])
        assert "here" in full_text or "our site" in full_text
        # No anchor block carries an unsafe href.
        for blk in out["blocks"]:
            for run in blk.get("runs", []) or []:
                if "href" in run:
                    assert run["href"].startswith(("http://", "https://", "mailto:"))

    def test_unsafe_image_src_dropped(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_BAD_SCHEMES_HTML))
        assert out is not None
        # No data: / file: src in any image block.
        for img_url in out["images"]:
            assert img_url.startswith(("http://", "https://"))
        # The single safe image survives.
        assert any("safe.jpg" in i for i in out["images"])

    def test_image_dedup_by_url_pre_query(self):
        out = fetch_orangetrack_article(_make_entry(SAMPLE_GALLERY_HTML))
        assert out is not None
        # g1.jpg appears twice (with ?w=300 and ?w=600) — must dedup
        # by URL pre-`?`.
        assert sum(1 for i in out["images"] if "/g1.jpg" in i) == 1
        # All three distinct gallery files surface.
        assert any("/g1.jpg" in i for i in out["images"])
        assert any("/g2.jpg" in i for i in out["images"])
        assert any("/g3.jpg" in i for i in out["images"])

    def test_image_limit_capped_at_10(self):
        many_imgs = "".join(
            f'<figure><img src="https://orangetrackdiecast.com/wp-content/uploads/x{i}.jpg" /></figure>'
            for i in range(20)
        )
        html = "<p>Lead.</p>" + many_imgs
        out = fetch_orangetrack_article(_make_entry(html))
        assert out is not None
        assert len(out["images"]) <= 10

    def test_empty_content_returns_none_with_no_link(self):
        # No content:encoded, no link, no fallback path → None.
        entry = {"link": "", "title": "", "content": []}
        out = fetch_orangetrack_article(entry)
        assert out is None

    def test_multiple_iframes_in_one_paragraph(self):
        # Tech-spec line 365: "Multiple <iframe> in same paragraph block →
        # blocks list contains 2 video entries in order". Regression guard
        # against the parser emitting only the first iframe.
        html = (
            "<p>Watch:</p>"
            "<p>"
            "<iframe src='https://www.youtube.com/embed/A123abcXYZ'></iframe>"
            "<iframe src='https://www.youtube.com/embed/B456defABC'></iframe>"
            "</p>"
        )
        out = fetch_orangetrack_article(_make_entry(html))
        assert out is not None
        videos = [b for b in out["blocks"] if b["type"] == "video"]
        assert len(videos) == 2
        # Both wrapped via Telegra.ph proxy.
        for v in videos:
            assert v["src"].startswith("https://telegra.ph/embed/youtube?url=")
        # Order preserved: A123 comes before B456.
        assert "A123abcXYZ" in videos[0]["src"]
        assert "B456defABC" in videos[1]["src"]


# ---------------------------------------------------------------------------
# TestSSRFAllowlist — _is_allowed_orangetrack_url + entry-level guard
# ---------------------------------------------------------------------------


class TestSSRFAllowlist:
    def test_apex_host_allowed(self):
        assert _is_allowed_orangetrack_url("https://orangetrackdiecast.com/post") is True

    def test_www_host_allowed(self):
        assert _is_allowed_orangetrack_url("https://www.orangetrackdiecast.com/post") is True

    def test_http_scheme_allowed(self):
        # http (not just https) accepted — recipe doesn't restrict.
        assert _is_allowed_orangetrack_url("http://orangetrackdiecast.com/post") is True

    def test_subdomain_attack_rejected(self):
        # Substring-based dispatcher would route this to orangetrack —
        # allowlist closes the hole.
        assert _is_allowed_orangetrack_url(
            "https://orangetrackdiecast.com.attacker.example/payload"
        ) is False

    def test_cloud_metadata_ip_rejected(self):
        assert _is_allowed_orangetrack_url(
            "http://169.254.169.254/latest/meta-data/"
        ) is False

    def test_javascript_scheme_rejected(self):
        assert _is_allowed_orangetrack_url("javascript:alert(1)") is False

    def test_data_scheme_rejected(self):
        assert _is_allowed_orangetrack_url("data:text/html,<script>x</script>") is False

    def test_malformed_url_rejected(self):
        assert _is_allowed_orangetrack_url("not a url") is False

    def test_scheme_relative_rejected(self):
        assert _is_allowed_orangetrack_url("//evil.example/x") is False

    def test_empty_string_rejected(self):
        assert _is_allowed_orangetrack_url("") is False

    def test_none_rejected(self):
        assert _is_allowed_orangetrack_url(None) is False

    def test_fallback_path_allowlist_called(self):
        # Entry with link not in allowlist + content:encoded missing →
        # notifier called with ART_FALLBACK_HOST_REJECTED, returns None,
        # requests.get NOT called.
        events = []
        entry = {
            "link": "https://attacker.example/x",
            "title": "Bad",
            "content": [],
            "summary": "",
        }
        with patch("orangetrack_source.requests.get") as mock_get:
            out = fetch_orangetrack_article(
                entry, notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        assert mock_get.call_count == 0
        assert events == [("ART_FALLBACK_HOST_REJECTED", "https://attacker.example/x")]

    def test_entry_level_guard_in_news_bot_fetcher(self, monkeypatch):
        # _fetch_orangetrack_entries should reject poisoned-link entries via
        # the ENTRY_HOST_REJECTED code BEFORE parsing.
        import news_bot

        class FakeEntry(dict):
            pass

        fake_parsed = type("P", (), {
            "entries": [
                {"link": "https://attacker.example/x", "title": "T", "summary": ""},
            ],
            "bozo": 0,
            "status": 200,
            "get": lambda self, k, d=None: 200 if k == "status" else d,
        })()
        monkeypatch.setattr("feedparser.parse", lambda url: fake_parsed)

        sent = []
        monkeypatch.setattr(
            news_bot, "send_admin_notification", lambda msg: sent.append(msg),
        )

        out = news_bot._fetch_orangetrack_entries(notifier=None)
        assert out == []
        # Aggregator emit fires once with the ENTRY_HOST_REJECTED line.
        assert len(sent) == 1
        assert "ENTRY_HOST_REJECTED" in sent[0]


# ---------------------------------------------------------------------------
# TestFallbackPath — bounded-stream HTTP scrape
# ---------------------------------------------------------------------------


def _make_streaming_response(status=200, body: bytes = b"", chunks=None, raise_on_get=None):
    """Return a MagicMock requests.Response that supports iter_content and close."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.encoding = "utf-8"
    resp.headers = {}
    if chunks is None:
        # Default chunking: 1 chunk per kilobyte.
        chunks_list = [body[i:i + 1024] for i in range(0, len(body), 1024)] or [b""]
    else:
        chunks_list = list(chunks)
    resp.iter_content = MagicMock(return_value=iter(chunks_list))
    resp.close = MagicMock()
    return resp


class TestFallbackPath:
    def _entry_no_content(self, link="https://orangetrackdiecast.com/post-x"):
        return {"link": link, "title": "T", "content": [], "summary": ""}

    def test_200_success_silent(self):
        body = SAMPLE_STANDARD_HTML.encode("utf-8")
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.return_value = _make_streaming_response(200, body)
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is not None
        # Successful primary/fallback path is silent — no notifier events.
        assert events == []

    def test_503_emits_notifier(self):
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.return_value = _make_streaming_response(503, b"bad gateway")
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_HTTP_503" in codes

    def test_404_emits_notifier(self):
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.return_value = _make_streaming_response(404, b"not found")
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_HTTP_404" in codes

    def test_timeout_emits_notifier(self):
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.side_effect = requests.Timeout("timed out")
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_TIMEOUT" in codes

    def test_redirect_does_not_call_get_twice(self):
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.return_value = _make_streaming_response(302, b"")
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_REDIRECT_302" in codes
        # Critical: only one GET issued (allow_redirects=False).
        assert mock_get.call_count == 1
        # And the call passed allow_redirects=False.
        _, kwargs = mock_get.call_args
        assert kwargs.get("allow_redirects") is False
        assert kwargs.get("stream") is True

    def test_body_too_large_cuts_off(self):
        # 6 MB body via chunks; iter_content cuts off after 5 MB cap.
        chunk = b"x" * 1024  # 1 KB
        chunks = [chunk] * (6 * 1024)  # 6 MB total
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            resp = _make_streaming_response(200, b"", chunks=chunks)
            mock_get.return_value = resp
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_TOO_LARGE" in codes

    def test_parse_empty_emits_notifier(self):
        # 200 + empty/unusable body → ART_FALLBACK_PARSE_EMPTY.
        events = []
        with patch("orangetrack_source.requests.get") as mock_get:
            mock_get.return_value = _make_streaming_response(200, b"<html></html>")
            out = fetch_orangetrack_article(
                self._entry_no_content(),
                notifier=lambda c, l: events.append((c, l)),
            )
        assert out is None
        codes = [e[0] for e in events]
        assert "ART_FALLBACK_PARSE_EMPTY" in codes


# ---------------------------------------------------------------------------
# TestWPBlockDriftMitigation — minimal HTML (no wp-block-* classes)
# ---------------------------------------------------------------------------


class TestWPBlockDriftMitigation:
    def test_minimal_html_without_wp_block_classes_parses(self):
        # No wp-block-* classes anywhere — parser must walk by tag.
        minimal = """
        <p>First para no class.</p>
        <p>Second para no class.</p>
        <figure><img src="https://orangetrackdiecast.com/x.jpg" /></figure>
        """
        out = fetch_orangetrack_article(_make_entry(minimal))
        assert out is not None
        # Subtitle stays empty by design (see TestPrimaryPath note).
        assert out["subtitle"] == ""
        # Both paragraphs in body — first is no longer extracted as subtitle.
        assert "First para no class." in out["paragraphs"]
        assert "Second para no class." in out["paragraphs"]
        assert any("x.jpg" in i for i in out["images"])


# ---------------------------------------------------------------------------
# TestYouTubeEmbedWrapping
# ---------------------------------------------------------------------------


class TestYouTubeEmbedWrapping:
    def test_youtube_com_embed_wrapped(self):
        out = _video_embed_url("https://www.youtube.com/embed/abc123XYZ?feature=oembed")
        assert out is not None
        assert out.startswith("https://telegra.ph/embed/youtube?url=")
        assert "abc123XYZ" in out or "abc123" in out

    def test_m_youtube_com_wrapped(self):
        out = _video_embed_url("https://m.youtube.com/watch?v=abcdefg")
        assert out is not None
        assert out.startswith("https://telegra.ph/embed/youtube?url=")

    def test_youtu_be_wrapped(self):
        out = _video_embed_url("https://youtu.be/abcdefghijk")
        assert out is not None
        assert out.startswith("https://telegra.ph/embed/youtube?url=")

    def test_youtube_nocookie_wrapped(self):
        out = _video_embed_url("https://www.youtube-nocookie.com/embed/abc123XYZ")
        assert out is not None
        assert out.startswith("https://telegra.ph/embed/youtube?url=")

    def test_vimeo_not_wrapped(self):
        out = _video_embed_url("https://vimeo.com/123456")
        # Decision 8: vimeo not in YouTube allowlist for this feature.
        assert out is None

    def test_attacker_url_with_youtube_substring_rejected(self):
        # Hostname allowlist gate blocks this even though the URL contains
        # 'youtube.com/embed/abc' as a substring.
        out = _video_embed_url(
            "https://attacker.example/redirect?u=https://youtube.com/embed/abc123"
        )
        assert out is None

    def test_iframe_in_html_routes_through_allowlist(self):
        # Attacker iframe inside content:encoded does NOT produce a video
        # block.
        html = """
        <p>Watch:</p>
        <p><iframe src="https://attacker.example/youtube.com/embed/abc"></iframe></p>
        """
        out = fetch_orangetrack_article(_make_entry(html))
        assert out is not None
        types = [b["type"] for b in out["blocks"]]
        assert "video" not in types


# ---------------------------------------------------------------------------
# TestOrangetrackPingAggregator
# ---------------------------------------------------------------------------


class TestOrangetrackPingAggregator:
    def test_empty_aggregator(self):
        a = OrangetrackPingAggregator("test")
        assert a.is_empty() is True
        assert a.format_summary() == ""

    def test_emit_noop_when_empty(self):
        a = OrangetrackPingAggregator("test")
        sent = []
        a.emit(lambda msg: sent.append(msg))
        assert sent == []

    def test_three_events_grouping(self):
        a = OrangetrackPingAggregator("test")
        a.add("FEED_HTTP_503", "https://orangetrackdiecast.com/feed/")
        a.add("ART_FALLBACK_HTTP_404", "https://orangetrackdiecast.com/a")
        a.add("ART_FALLBACK_HTTP_404", "https://orangetrackdiecast.com/b")
        out = a.format_summary()
        assert out.startswith("[test] orangetrack: 3 issues this tick")
        assert "FEED_HTTP_503" in out
        assert "ART_FALLBACK_HTTP_404" in out
        # FEED_* group comes before ART_*.
        assert out.index("FEED_HTTP_503") < out.index("ART_FALLBACK_HTTP_404")

    def test_dedup_same_code_same_link(self):
        a = OrangetrackPingAggregator("test")
        a.add("FEED_HTTP_503", "https://x/y")
        a.add("FEED_HTTP_503", "https://x/y")
        out = a.format_summary()
        assert "(2×)" in out
        # Header reflects total events fired (2 add() calls), not just
        # distinct (code, link) pairs — operator-severity semantic.
        assert "2 issues this tick" in out

    def test_distinct_status_codes_separate_lines(self):
        a = OrangetrackPingAggregator("test")
        a.add("FEED_HTTP_503", "https://x/y")
        a.add("FEED_HTTP_429", "https://x/y")
        out = a.format_summary()
        assert "FEED_HTTP_503" in out
        assert "FEED_HTTP_429" in out
        # Alphabetical within FEED_*: 429 before 503.
        assert out.index("FEED_HTTP_429") < out.index("FEED_HTTP_503")

    def test_category_ordering_feed_entry_art(self):
        a = OrangetrackPingAggregator(None)
        # Add in reverse category order to verify sort kicks in.
        a.add("ART_FALLBACK_TIMEOUT", "https://x/a")
        a.add("ENTRY_HOST_REJECTED", "https://x/b")
        a.add("FEED_TIMEOUT", "https://x/c")
        out = a.format_summary()
        assert out.index("FEED_TIMEOUT") < out.index("ENTRY_HOST_REJECTED")
        assert out.index("ENTRY_HOST_REJECTED") < out.index("ART_FALLBACK_TIMEOUT")

    def test_alphabetical_within_category(self):
        a = OrangetrackPingAggregator("test")
        a.add("ART_FALLBACK_TIMEOUT", "https://x/a")
        a.add("ART_FALLBACK_HOST_REJECTED", "https://x/b")
        out = a.format_summary()
        # HOST_REJECTED < TIMEOUT alphabetically.
        assert out.index("ART_FALLBACK_HOST_REJECTED") < out.index("ART_FALLBACK_TIMEOUT")

    def test_instance_label_set(self):
        a = OrangetrackPingAggregator("test")
        a.add("FEED_HTTP_503", "https://x/y")
        out = a.format_summary()
        assert out.startswith("[test] orangetrack:")

    def test_instance_label_empty(self):
        a = OrangetrackPingAggregator("")
        a.add("FEED_HTTP_503", "https://x/y")
        out = a.format_summary()
        assert not out.startswith("[")
        assert out.startswith("orangetrack:")

    def test_instance_label_none(self):
        a = OrangetrackPingAggregator(None)
        a.add("FEED_HTTP_503", "https://x/y")
        out = a.format_summary()
        assert not out.startswith("[")

    def test_per_code_link_cap_50(self):
        a = OrangetrackPingAggregator("test")
        for i in range(60):
            a.add("FEED_HTTP_503", f"https://orangetrackdiecast.com/post-{i}")
        out = a.format_summary()
        # Truncation marker present — not all 60 links visible.
        assert "more truncated" in out

    def test_header_count_includes_truncated_overflow(self):
        # 60 distinct (code, link) pairs of the same code → only 50 stored
        # in the bullet's link list, but the header MUST reflect all 60
        # events that fired so the operator's severity assessment is honest.
        # Per-code count must also include the truncated tail.
        a = OrangetrackPingAggregator("test")
        for i in range(60):
            a.add("FEED_HTTP_503", f"https://orangetrackdiecast.com/post-{i}")
        out = a.format_summary()
        # Header reports the TRUE total (events fired), not stored count.
        assert "60 issues this tick" in out
        # Per-bucket count also reflects the truncated overflow.
        assert "FEED_HTTP_503 (60×)" in out
        # Truncation marker for the link list itself stays at the tail.
        assert "10 more truncated" in out

    def test_total_event_cap_500_silent(self):
        from orangetrack_source import _MAX_TOTAL_EVENTS
        a = OrangetrackPingAggregator("test")
        # 600 distinct codes — past 500 cap, additional adds are silent
        # no-ops (no raise, no storage).
        for i in range(600):
            a.add(f"FEED_HTTP_{i}", f"https://x/{i}")
        out = a.format_summary()
        # Storage cap is bound: only 500 events were stored (litmus: if the
        # _MAX_TOTAL_EVENTS guard at orangetrack_source.py is removed, this
        # assertion fails because _total_calls would reach 600).
        assert a._total_calls == _MAX_TOTAL_EVENTS  # 500
        distinct_stored = sum(len(b) for b in a._events.values())
        assert distinct_stored == _MAX_TOTAL_EVENTS  # 500
        # Header reflects the TRUE event volume (600), not just stored
        # count — operator severity signal must not be muted by the
        # internal storage cap.
        assert "600 issues this tick" in out
        # Format summary still emits — must not raise.
        assert "issues this tick" in out

    def test_summary_truncated_at_3500_chars(self):
        a = OrangetrackPingAggregator("test")
        # Long crafted links to overflow the 3500-char output limit.
        long_link_template = "https://orangetrackdiecast.com/" + "a" * 100
        for i in range(200):
            a.add(f"FEED_HTTP_5{i:02d}", f"{long_link_template}/{i}")
        out = a.format_summary()
        assert len(out) <= 3500

    def test_control_char_sanitization(self):
        a = OrangetrackPingAggregator("test")
        # Newline-injection attempt — must not produce a fake summary line.
        crafted = "https://x/\n[prod] orangetrack: 0 issues this tick"
        a.add("FEED_HTTP_503", crafted)
        out = a.format_summary()
        # Original [test] header still on line 1.
        assert out.split("\n")[0] == "[test] orangetrack: 1 issues this tick"
        # Newline injection neutralized — output structure has only TWO
        # lines (header + one bullet), not three (header + bullet +
        # injected fake header).
        assert out.count("\n") == 1
        # No raw newline survived in the rendered output.
        # The injected fake-header text gets concatenated onto the
        # bullet line as plain text rather than starting a new line.
        lines = out.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("[test] orangetrack:")
        assert lines[1].lstrip().startswith("• FEED_HTTP_503")

    def test_emit_swallows_send_fn_error(self, caplog):
        a = OrangetrackPingAggregator("test")
        a.add("FEED_HTTP_503", "https://x/y")

        def boom(msg):
            raise RuntimeError("send failed")

        # Must not propagate — only log.
        with caplog.at_level(logging.ERROR, logger="orangetrack_source"):
            a.emit(boom)
        # Some error-level message logged.
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("send_fn raised" in r.getMessage() or "swallowing" in r.getMessage()
                   for r in errors)


# ---------------------------------------------------------------------------
# TestDispatcherIntegration — news_bot.fetch_full_article orangetrack branch
# ---------------------------------------------------------------------------


class TestDispatcherIntegration:
    """Verifies the orangetrack pass-through branch in
    ``news_bot.fetch_full_article`` (news_bot.py:1281-1298).

    The branch is the contract that ``_fetch_orangetrack_entries`` upstream
    pre-populates body fields and ``fetch_full_article`` is then a
    zero-HTTP pass-through. Without these tests the branch can be deleted
    or swapped for a fallback HTTP fetch with no test failure (M1 in
    test-audit.md).
    """

    def test_dispatcher_routes_orangetrack_apex_passthrough(self):
        import news_bot
        entry = {
            "link": "https://orangetrackdiecast.com/post-x",
            "title": "T",
            "subtitle": "S",
            "paragraphs": ["Body para 1", "Body para 2"],
            "images": ["https://orangetrackdiecast.com/img.jpg"],
            "blocks": [{"type": "paragraph", "text": "Body para 1"}],
        }
        with patch("orangetrack_source.requests.get") as mock_get:
            out = news_bot.fetch_full_article(entry)
        assert out is not None
        assert out["title"] == "T"
        assert out["subtitle"] == "S"
        assert out["paragraphs"] == ["Body para 1", "Body para 2"]
        assert out["images"] == ["https://orangetrackdiecast.com/img.jpg"]
        assert out["blocks"] == [{"type": "paragraph", "text": "Body para 1"}]
        # Zero HTTP — pass-through must NOT issue any GET.
        assert mock_get.call_count == 0

    def test_dispatcher_routes_orangetrack_www_passthrough(self):
        import news_bot
        entry = {
            "link": "https://www.orangetrackdiecast.com/post-y",
            "title": "T2",
            "subtitle": "",
            "paragraphs": ["www-host paragraph"],
            "images": [],
            "blocks": None,
        }
        with patch("orangetrack_source.requests.get") as mock_get:
            out = news_bot.fetch_full_article(entry)
        assert out is not None
        assert out["paragraphs"] == ["www-host paragraph"]
        assert mock_get.call_count == 0

    def test_dispatcher_orangetrack_subdomain_attack_does_not_route(self):
        # Defense-in-depth: dispatcher's substring `'orangetrackdiecast.com'
        # in domain` would route this to the orangetrack branch — but the
        # entry here lacks pre-populated paragraphs (a malicious upstream
        # entry would have been rejected by _fetch_orangetrack_entries'
        # ENTRY_HOST_REJECTED guard and never reached here with body
        # fields). Pass-through MUST return None — body fields from the
        # entry must NOT be returned.
        import news_bot
        entry = {
            "link": "https://orangetrackdiecast.com.attacker.example/x",
            "title": "Bad",
            # No 'paragraphs' field at all.
        }
        with patch("orangetrack_source.requests.get") as mock_get:
            out = news_bot.fetch_full_article(entry)
        assert out is None
        assert mock_get.call_count == 0

    def test_dispatcher_orangetrack_without_pre_populated_paragraphs_returns_none(self):
        # Safety check at news_bot.py:1287-1291 — entry without paragraphs
        # field returns None rather than synthesizing an empty article.
        import news_bot
        entry = {
            "link": "https://orangetrackdiecast.com/post-z",
            "title": "T",
            # No 'paragraphs' key.
        }
        with patch("orangetrack_source.requests.get") as mock_get:
            out = news_bot.fetch_full_article(entry)
        assert out is None
        assert mock_get.call_count == 0


# ---------------------------------------------------------------------------
# Smoke: _safe_for_ping
# ---------------------------------------------------------------------------


class TestSafeForPing:
    def test_strips_newline(self):
        assert "\n" not in _safe_for_ping("hello\nworld")

    def test_strips_carriage_return(self):
        assert "\r" not in _safe_for_ping("hello\rworld")

    def test_strips_tab(self):
        assert "\t" not in _safe_for_ping("hello\tworld")

    def test_truncates_long_input(self):
        s = "x" * 1000
        out = _safe_for_ping(s)
        assert len(out) <= 200

    def test_handles_none(self):
        # Defensive: None becomes empty string.
        out = _safe_for_ping(None)
        assert out == ""
