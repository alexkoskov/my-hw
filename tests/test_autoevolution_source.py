#!/usr/bin/env python3
"""Unit tests for autoevolution_source."""

from unittest.mock import MagicMock

import pytest

import autoevolution_source
from autoevolution_source import (
    _is_allowed_autoevolution_url,
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
        out = _scrape_article_page("https://www.autoevolution.com/news/x-268757.html", fetcher=fetcher)
        assert out is None

    def test_missing_body_returns_none(self):
        fetcher = lambda url: _fake_response("<html><h1>Only title</h1></html>")
        out = _scrape_article_page("https://www.autoevolution.com/news/x-268757.html", fetcher=fetcher)
        assert out is None

    def test_fetcher_exception_returns_none(self):
        def boom(url):
            raise RuntimeError("network dead")
        out = _scrape_article_page("https://www.autoevolution.com/news/x-268757.html", fetcher=boom)
        assert out is None


class TestHostAllowlist:
    """SSRF guard (CWE-918): the fetcher must reject any URL whose hostname
    isn't exactly ``autoevolution.com`` / ``www.autoevolution.com`` BEFORE any
    fetch — closing the userinfo (``@``) and suffix attacks the substring
    dispatch would otherwise let through."""

    @pytest.mark.parametrize("url", [
        "https://www.autoevolution.com/news/x-1.html",
        "https://autoevolution.com/news/x-1.html",
        "http://www.autoevolution.com/news/x-1.html",
    ])
    def test_allows_canonical_hosts(self, url):
        assert _is_allowed_autoevolution_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://autoevolution.com@169.254.169.254/latest/meta-data/",  # userinfo attack
        "http://autoevolution.com.attacker.example/x-1.html",           # suffix attack
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example/x-1.html",
        "ftp://www.autoevolution.com/x",                                # non-http scheme
        "file:///etc/passwd",
        "",
    ])
    def test_rejects_hostile_or_non_canonical(self, url):
        assert _is_allowed_autoevolution_url(url) is False

    def test_scrape_rejects_hostile_url_without_fetching(self):
        """A hostile URL must be rejected BEFORE the fetcher is ever called —
        even an injected fetcher must not run (no SSRF egress)."""
        calls = []
        def spy_fetcher(url):
            calls.append(url)
            return _fake_response(SAMPLE_ARTICLE_HTML)
        out = _scrape_article_page(
            "http://autoevolution.com@169.254.169.254/latest/meta-data/",
            fetcher=spy_fetcher,
        )
        assert out is None
        assert calls == []  # guard fired before any fetch

    def test_fetch_autoevolution_article_rejects_hostile_link(self):
        """The public entry point degrades safely: a hostile link scrapes to
        None, then falls back to RSS-only enrichment (which performs no fetch
        of the link)."""
        calls = []
        def spy_fetcher(url):
            calls.append(url)
            return _fake_response(SAMPLE_ARTICLE_HTML)
        entry = {
            "link": "http://autoevolution.com.attacker.example/x-1.html",
            "title": "RSS title", "summary": "RSS summary",
            "media_content": [{"url": "https://s1.cdn.example/hero.jpg"}],
        }
        out = fetch_autoevolution_article(entry, fetcher=spy_fetcher)
        assert calls == []  # never fetched the hostile host (SSRF guard)
        # Degrades to RSS-only enrichment — no SSRF, no crash.
        assert out is not None
        assert out["title"] == "RSS title"


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


# ---------------------------------------------------------------------------
# Cloudflare session (2026-08-11). Autoevolution turned on a JS challenge for
# article pages: the old per-article `curl_requests.get(..., impersonate=
# "chrome")` answered 403 + `cf-mitigated: challenge` on every request for
# three days straight. Two things fixed it, measured live — a browser profile
# Cloudflare does not challenge, and ONE session that visits the home page
# first, the way a reader's browser does. These tests pin both, plus the
# retry that re-warms a session whose clearance has gone stale.
# ---------------------------------------------------------------------------

ARTICLE_URL = (
    "https://www.autoevolution.com/news/hot-wheels-chase-car-to-hunt-for-"
    "is-a-rare-porsche-268757.html"
)


def _challenge_response():
    """What Cloudflare actually returned on prod: 403 + the interstitial."""
    resp = _fake_response("<html><head><title>Just a moment...</title></head></html>",
                          status=403)
    resp.headers = {"cf-mitigated": "challenge", "server": "cloudflare"}
    return resp


class _RecordingSession:
    """Stand-in for ``curl_cffi.requests.Session`` — records every GET."""

    def __init__(self, handler, **kwargs):
        self.init_kwargs = kwargs
        self.urls = []
        self.get_kwargs = []
        self.closed = False
        self._handler = handler

    def get(self, url, **kwargs):
        self.urls.append(url)
        # Recorded, not discarded: `allow_redirects=False` is a security
        # control here, and a stub that swallows kwargs cannot notice it going
        # missing.
        self.get_kwargs.append(kwargs)
        return self._handler(url, self)

    def close(self):
        self.closed = True


class _CurlHarness:
    """Replaces the ``curl_requests`` module: hands out recording sessions."""

    def __init__(self):
        self.sessions = []
        self.handler = lambda url, session: _fake_response(SAMPLE_ARTICLE_HTML)

    def Session(self, **kwargs):  # noqa: N802 — mirrors curl_cffi's own API
        session = _RecordingSession(lambda u, s: self.handler(u, s), **kwargs)
        self.sessions.append(session)
        return session

    @property
    def article_gets(self):
        """Every article URL fetched, warm-up hits excluded."""
        return [u for s in self.sessions for u in s.urls
                if u != autoevolution_source._WARMUP_URL]


@pytest.fixture
def curl(monkeypatch):
    """Swap curl_cffi for the harness and clear the module-level session.

    The reset matters both ways: a session left over from another test would
    hide a missing warm-up, and one left behind by these tests would leak a
    fake into whatever runs next.
    """
    monkeypatch.setattr(autoevolution_source, "_CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(autoevolution_source, "_session", None)
    harness = _CurlHarness()
    monkeypatch.setattr(autoevolution_source, "curl_requests", harness)
    yield harness
    autoevolution_source.reset_session()


class TestCloudflareSession:
    def test_visits_home_page_before_the_first_article(self, curl):
        out = _scrape_article_page(ARTICLE_URL)

        assert out["title"] == "Hot Wheels Chase Car"
        # Order is the whole point — clearance must be earned before the article.
        assert curl.sessions[0].urls == [autoevolution_source._WARMUP_URL, ARTICLE_URL]

    def test_warms_up_once_and_reuses_the_session_across_articles(self, curl):
        for n in (1, 2, 3):
            _scrape_article_page(f"https://www.autoevolution.com/news/story-{n}-2687{n}.html")

        assert len(curl.sessions) == 1, "a fresh session per article throws the clearance away"
        assert curl.sessions[0].urls.count(autoevolution_source._WARMUP_URL) == 1
        assert len(curl.article_gets) == 3

    #: Profiles measured against a live blocked article on 2026-08-11 — every
    #: one of these was answered with 403 + `cf-mitigated: challenge`.
    CHALLENGED_PROFILES = (
        "chrome", "chrome136", "chrome142", "chrome146", "safari260",
    )

    def test_impersonates_a_profile_this_site_does_not_challenge(self, curl):
        _scrape_article_page(ARTICLE_URL)

        # The session must actually carry the constant...
        assert curl.sessions[0].init_kwargs["impersonate"] == \
            autoevolution_source._IMPERSONATE_PROFILE
        # ...and the constant must not be one of the profiles that caused the
        # three-day outage. Asserting `!= "chrome"` was not enough: `chrome146`
        # is a different string and was 403 for those same three days.
        assert autoevolution_source._IMPERSONATE_PROFILE not in self.CHALLENGED_PROFILES

    def test_never_follows_redirects(self, curl):
        # `allow_redirects=False` is an SSRF control, not tuning: the allowlist
        # validates ONE url, so a followed 30x would reach an arbitrary host
        # through a guard that already passed. Covers the warm-up too — it is a
        # request the old per-article code never made.
        _scrape_article_page(ARTICLE_URL)

        assert curl.sessions[0].urls == [autoevolution_source._WARMUP_URL, ARTICLE_URL]
        assert all(kw.get("allow_redirects") is False
                   for kw in curl.sessions[0].get_kwargs)

    def test_rebuilds_the_session_and_retries_once_when_challenged(self, curl):
        # A clearance cookie expires between daily ticks, so the first article
        # of a tick can be challenged even though the profile is fine.
        seen = {"challenged": False}

        def handler(url, session):
            if url == ARTICLE_URL and not seen["challenged"]:
                seen["challenged"] = True
                return _challenge_response()
            return _fake_response(SAMPLE_ARTICLE_HTML)

        curl.handler = handler
        out = _scrape_article_page(ARTICLE_URL)

        assert out["title"] == "Hot Wheels Chase Car"
        assert len(curl.sessions) == 2, "the stale session must be thrown away, not reused"
        assert curl.sessions[1].urls == [autoevolution_source._WARMUP_URL, ARTICLE_URL]

    def test_gives_up_after_the_retry_is_challenged_too(self, curl):
        # A bare 403 with no cf-mitigated header: the code keys on the status
        # alone, because that header is not guaranteed. Must not loop — the
        # caller falls back to the RSS-only path.
        curl.handler = lambda url, session: _fake_response("", status=403)

        assert _scrape_article_page(ARTICLE_URL) is None
        assert len(curl.sessions) == 2
        assert len(curl.article_gets) == 2

    @pytest.mark.parametrize("status", [404, 500, 503])
    def test_does_not_burn_a_second_request_on_a_non_challenge_error(self, curl, status):
        # Only 403 means "re-warm and try again". A missing page or a dead
        # backend must cost one request, not two — doubling every failure
        # against a source that is already unhappy is how a soft problem
        # becomes a hard one.
        curl.handler = lambda url, session: _fake_response("", status=status)

        assert _scrape_article_page(ARTICLE_URL) is None
        assert len(curl.sessions) == 1
        assert len(curl.article_gets) == 1

    def test_a_dead_home_page_does_not_stop_the_article_fetch(self, curl):
        # Warm-up is an optimisation, not a precondition. Before the retry
        # existed a raising warm-up would have cost the whole tick.
        def handler(url, session):
            if url == autoevolution_source._WARMUP_URL:
                raise ConnectionError("home page down")
            return _fake_response(SAMPLE_ARTICLE_HTML)

        curl.handler = handler
        out = _scrape_article_page(ARTICLE_URL)

        assert out["title"] == "Hot Wheels Chase Car"

    def test_a_broken_connection_does_not_poison_the_rest_of_the_tick(self, curl):
        # One shared session means one shared failure mode: a dead socket on
        # article 1 must not be inherited by articles 2..N. The per-request
        # code this replaced could not have that problem.
        second = "https://www.autoevolution.com/news/story-two-268758.html"
        boom = {"done": False}

        def handler(url, session):
            if url == ARTICLE_URL and not boom["done"]:
                boom["done"] = True
                raise ConnectionError("connection reset by peer")
            return _fake_response(SAMPLE_ARTICLE_HTML)

        curl.handler = handler

        assert _scrape_article_page(ARTICLE_URL) is None
        assert _scrape_article_page(second)["title"] == "Hot Wheels Chase Car"
        # Rebuilt rather than reused, and the new one is warmed like any other.
        assert len(curl.sessions) == 2
        assert curl.sessions[1].urls == [autoevolution_source._WARMUP_URL, second]

    def test_ssrf_guard_still_runs_before_any_session_is_built(self, curl):
        # The allowlist must reject BEFORE egress — including the warm-up hit,
        # which is a request the guard never saw in the old per-article design.
        assert _scrape_article_page("https://www.autoevolution.com.attacker.example/news/x-1.html") is None
        assert curl.sessions == []

    def test_missing_curl_cffi_returns_none_without_touching_the_network(self, curl, monkeypatch):
        monkeypatch.setattr(autoevolution_source, "_CURL_CFFI_AVAILABLE", False)

        assert _scrape_article_page(ARTICLE_URL) is None
        assert curl.sessions == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
