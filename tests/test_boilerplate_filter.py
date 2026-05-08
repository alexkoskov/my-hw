#!/usr/bin/env python3
"""Unit tests for boilerplate_filter.

Covers the pure helpers (``is_boilerplate`` / ``filter_boilerplate``) plus
per-parser integration: feeding synthetic article HTML into each source
parser and asserting that UI-boilerplate paragraphs ("Share on Facebook",
"Tweet", etc.) are stripped before the parser returns.
"""

from unittest.mock import MagicMock

import pytest

from boilerplate_filter import (
    _MAX_BOILERPLATE_LEN,
    _PLUG_PLATFORMS,
    filter_blocks,
    filter_boilerplate,
    is_boilerplate,
)


# ---------------------------------------------------------------------------
# Pure-filter positive cases (must be classified as boilerplate)
# ---------------------------------------------------------------------------


class TestIsBoilerplatePositive:
    @pytest.mark.parametrize(
        "text",
        [
            "Share on Facebook",
            "share on twitter",
            "Share on X",
            "Share on LinkedIn",
            "Share on Pinterest",
            "Share via WhatsApp",
            "Share to Reddit",
            "Tweet",
            "Pin it",
            "Pin on Pinterest",
            "Email this",
            "Email this article",
            "Copy link",
            "Copy URL",
            "Copy article URL",
            "Subscribe",
            "Subscribe to our newsletter",
            "Subscribe to newsletter",
            "Follow us on Instagram",
            "Follow us on Facebook",
            "Related articles",
            "Related posts",
            "See also",
            "You may also like",
            "Read more",
            "Read more:",
            "Tags: HotWheels, Mattel, Cars",
            "Filed under: news",
            "Categories: cars",
            "Comments",
            "Comment",
            # Affiliate / "Quick Link" promo lines (orangetrack-source).
            "*QUICK LINK!* Buy from 1 Stop Diecast now.",
            "QUICK LINK! Buy from 1 Stop Diecast now.",
            "Quick link: order from CollectorClub today.",
            "Buy now from 1 Stop Diecast",
            "Buy from CollectorZone.",
        ],
    )
    def test_english_patterns_filtered(self, text):
        assert is_boilerplate(text) is True, f"expected boilerplate: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "Поделиться на Facebook",
            "Поделиться в Telegram",
            "Поделиться через WhatsApp",
            "Твитнуть",
            "Подписаться",
            "Подписаться на рассылку",
            "Подписаться на нашу рассылку",
            "Читайте также",
            "Смотрите также",
            "Тэги: машинки",
            "Теги: hotwheels",
            "Категории: новости",
            "Метки: тест",
            "Комментарии",
            "Комментарий",
        ],
    )
    def test_russian_patterns_filtered(self, text):
        assert is_boilerplate(text) is True, f"expected boilerplate: {text!r}"


# ---------------------------------------------------------------------------
# Pure-filter negative cases (must be PRESERVED — real content)
# ---------------------------------------------------------------------------


class TestIsBoilerplateNegative:
    @pytest.mark.parametrize(
        "text",
        [
            # Long sentences containing trigger words but in inline context.
            "This article describes how Mattel's Hot Wheels brand uses Facebook for marketing campaigns.",
            "The Hot Wheels collector world has many tweet-worthy moments these days, including the new launch of the chase car.",
            "Mattel collaborated with Subaru on a special Hot Wheels edition.",
            "The brand has a long history of partnerships with car manufacturers.",
            # Short non-boilerplate phrases (still preserved — different shape).
            "Hot Wheels Legends Tour 2026.",
            "Mattel announced today.",
            "New chase car released.",
            # Affiliate-like words inline are NOT filtered (Decision 12 —
            # only standalone short promo lines).
            "I'd buy that for $1",
            "The collector loves to shop for rare castings on weekends.",
        ],
    )
    def test_real_content_preserved(self, text):
        assert is_boilerplate(text) is False, f"unexpectedly filtered: {text!r}"

    def test_empty_string_not_boilerplate(self):
        # Empty/whitespace returns False — filter_boilerplate keeps them
        # (callers usually drop empties earlier; we don't claim ownership).
        assert is_boilerplate("") is False
        assert is_boilerplate("   ") is False


# ---------------------------------------------------------------------------
# Length threshold: long content with trigger phrase is NOT filtered.
# ---------------------------------------------------------------------------


class TestLengthThreshold:
    def test_long_paragraph_with_trigger_preserved(self):
        # Long content that begins with "Share on Facebook" — treated as
        # real prose because the length exceeds the bound.
        long_text = (
            "Share on Facebook with all your friends and family for the rest "
            "of your life and even beyond, share share share, please share, "
            "share more, even more, share share share share share."
        )
        assert len(long_text) > _MAX_BOILERPLATE_LEN
        assert is_boilerplate(long_text) is False


# ---------------------------------------------------------------------------
# Affiliate-line length-bound boundary cases (orangetrack-source).
# ---------------------------------------------------------------------------


class TestAffiliateLengthBound:
    def test_affiliate_at_120_chars_filtered(self):
        # Build an affiliate-shape line of EXACTLY 120 chars — must be
        # caught (`_MAX_BOILERPLATE_LEN` is inclusive).
        prefix = "Buy now from "
        # Pad with 'a's to hit exactly 120 chars total.
        line = prefix + "a" * (_MAX_BOILERPLATE_LEN - len(prefix))
        assert len(line) == _MAX_BOILERPLATE_LEN
        assert is_boilerplate(line) is True

    def test_affiliate_at_121_chars_preserved(self):
        # 121 chars → over the threshold, treated as real prose even
        # though the prefix matches an affiliate pattern.
        prefix = "Buy now from "
        line = prefix + "a" * (_MAX_BOILERPLATE_LEN + 1 - len(prefix))
        assert len(line) == _MAX_BOILERPLATE_LEN + 1
        assert is_boilerplate(line) is False

    def test_quick_link_at_120_chars_filtered(self):
        # *QUICK LINK!* shape, padded to 120 chars total.
        prefix = "*QUICK LINK!* Buy "
        line = prefix + "a" * (_MAX_BOILERPLATE_LEN - len(prefix) - len(" from x"))
        line = line + " from x"
        # Pad to exactly 120 if needed.
        if len(line) < _MAX_BOILERPLATE_LEN:
            line = line + "x" * (_MAX_BOILERPLATE_LEN - len(line))
        line = line[:_MAX_BOILERPLATE_LEN]
        assert len(line) == _MAX_BOILERPLATE_LEN
        assert is_boilerplate(line) is True

    def test_redos_safety_long_buy_pattern(self):
        # Pathological-shape input that would catastrophically backtrack
        # in a vulnerable regex. Must complete fast (<100ms).
        import time
        s = "a" * 60 + "buy" * 20 + " shop now"
        t0 = time.monotonic()
        out = is_boilerplate(s)
        elapsed = time.monotonic() - t0
        # Shape doesn't match Aff1/Aff2 anchored at start; expected False.
        assert out is False
        assert elapsed < 0.1, f"is_boilerplate too slow: {elapsed:.3f}s"


class TestQuickLinkVerbGateRemoved:
    """Aff1 dropped its (buy|order|grab|shop) verb gate 2026-05-08 after
    «*QUICK LINK!* Find the 2026 Fast & Furious ... case on eBay» slipped
    through. Prefix alone is now sufficient."""

    def test_quick_link_find_on_ebay_filtered(self):
        # Real-world case from prod 2026-05-08: «find» wasn't in the verb
        # list, ad slipped through, got translated, hit the channel.
        line = "*QUICK LINK!* Find the 2026 Fast & Furious case on eBay"
        assert is_boilerplate(line) is True

    def test_quick_link_no_verb_at_all_filtered(self):
        # Bare prefix is sufficient signal.
        line = "Quick link: Hot Wheels Mainline 2026 Treasure Hunts"
        assert is_boilerplate(line) is True

    def test_quick_link_with_buy_still_filtered(self):
        # Existing behaviour preserved — verb-having lines still match.
        line = "*QUICK LINK!* Buy the case on eBay now"
        assert is_boilerplate(line) is True

    def test_aff3_find_on_ebay_filtered(self):
        # New Aff3 — direct «Find ... on eBay» CTA without QUICK LINK prefix.
        line = "Find the Modern Classics S case on eBay"
        assert is_boilerplate(line) is True

    def test_aff3_find_at_amazon_filtered(self):
        line = "Find the 2026 Pop Culture set at Amazon"
        assert is_boilerplate(line) is True

    def test_aff3_find_in_random_context_preserved(self):
        # «find ... on the map» / «find ... on hand» — not affiliate; should
        # NOT match (no allowlisted store name follows). Length-bounded.
        line = "Find the rare casting on the back of the case wall"
        assert is_boilerplate(line) is False


class TestRussianAffiliateDefenseInDepth:
    """RU-side defense-in-depth (added 2026-05-08): post-LLM filter pass
    in news_bot._fallback_publish runs `is_boilerplate` on RU paragraphs
    too. These patterns activate when an EN affiliate variant slips
    through the parser-side filter and the LLM translates it."""

    def test_bystryaya_ssylka_filtered(self):
        # The exact RU shape that reached prod 2026-05-08.
        line = "БЫСТРАЯ ССЫЛКА! Найти кейс 2026 Fast & Furious на eBay"
        assert is_boilerplate(line) is True

    def test_bystraya_ssylka_lowercase_filtered(self):
        line = "Быстрая ссылка: Hot Wheels Mainline"
        assert is_boilerplate(line) is True

    def test_naiti_na_ebay_filtered(self):
        # «Найти ... на eBay» CTA shape.
        line = "Найти кейс Car Culture на eBay"
        assert is_boilerplate(line) is True

    def test_kupit_na_amazon_filtered(self):
        line = "Купить набор 2026 Pop Culture на Amazon"
        assert is_boilerplate(line) is True

    def test_normal_russian_prose_preserved(self):
        # No false positives on regular content prose mentioning «найти» /
        # «купить» without an allowlisted store keyword.
        line = "Можно найти этот редкий вариант в коллекциях фанатов"
        assert is_boilerplate(line) is False


# ---------------------------------------------------------------------------
# Author social-media plug patterns (variant A of the author-plug-filter
# feature). Standalone-paragraph plugs from authors of source articles,
# distinct from the corporate / UI-button shapes above.
# ---------------------------------------------------------------------------


class TestAuthorPlugPositive:
    """Each of the 10 supported platforms must be caught in at least one
    pattern shape."""

    @pytest.mark.parametrize("platform", _PLUG_PLATFORMS)
    def test_follow_me_on_each_platform(self, platform):
        assert is_boilerplate(f"Follow me on {platform.capitalize()}") is True

    @pytest.mark.parametrize("platform", _PLUG_PLATFORMS)
    def test_parenthesised_with_handle(self, platform):
        assert is_boilerplate(
            f"(follow me on {platform.capitalize()} @diecast215)"
        ) is True

    @pytest.mark.parametrize("platform", _PLUG_PLATFORMS)
    def test_platform_colon_handle(self, platform):
        assert is_boilerplate(f"{platform.capitalize()}: @diecast215") is True

    @pytest.mark.parametrize(
        "text",
        [
            "@diecast215",
            "@hot_wheels_collector",
            "@a1B_2",
        ],
    )
    def test_orphan_handle(self, text):
        assert is_boilerplate(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Subscribe to my channel",
            "Subscribe to my newsletter",
            "Subscribe to my YouTube",
            "Subscribe to my Patreon",
        ],
    )
    def test_subscribe_to_my_feed(self, text):
        assert is_boilerplate(text) is True

    def test_check_us_on_instagram(self):
        # A1 covers "check" alongside "follow" and "subscribe to".
        assert is_boilerplate("Check us on Instagram") is True

    def test_subscribe_to_us_on_youtube(self):
        # A1 with "subscribe to ... us on <platform>".
        assert is_boilerplate("Subscribe to us on YouTube") is True


class TestAuthorPlugNegative:
    """Real content / corporate plugs / journalistic refs MUST NOT be caught
    by variant-A. (Inline-plug stripping is variant B's job, not this layer.)"""

    def test_corporate_mattel_plug_passes(self):
        # AC16 — corporate "Follow Mattel on ..." is intentionally out of
        # scope (separate future feature). A1 anchors on me|us; Mattel
        # naming structurally cannot match.
        assert is_boilerplate(
            "Follow Mattel on Instagram, X, and Facebook"
        ) is False

    def test_real_content_with_instagram_mention(self):
        # Long inline content mentioning a platform — preserved.
        assert is_boilerplate(
            "The collector posted his find to Instagram and gathered 50K likes."
        ) is False

    def test_journalistic_paren_no_handle(self):
        # Standalone parenthesised reference WITHOUT @handle. A2 requires
        # @handle, so this doesn't match.
        assert is_boilerplate("(see photos on Instagram)") is False

    def test_bare_at_word_too_short(self):
        # A4 needs \w{2,30}; single-char or empty handles don't match.
        assert is_boilerplate("@a") is False

    def test_short_random_word_starting_with_at(self):
        # @-prefix alone isn't enough — must match \w{2,30}, but a
        # known-good real word also passes. Negative is the boundary case.
        assert is_boilerplate("@@") is False


# ---------------------------------------------------------------------------
# filter_boilerplate behaviour on a paragraph list
# ---------------------------------------------------------------------------


class TestFilterBoilerplate:
    def test_drops_only_boilerplate_lines(self):
        paragraphs = [
            "Mattel announced the new Hot Wheels Legends Tour today.",
            "Share on Facebook",
            "The collection includes ten unique castings.",
            "Tweet",
            "Subscribe",
            "Read more about the lineup at the press release.",
        ]
        out = filter_boilerplate(paragraphs)
        assert out == [
            "Mattel announced the new Hot Wheels Legends Tour today.",
            "The collection includes ten unique castings.",
            "Read more about the lineup at the press release.",
        ]

    def test_preserves_order(self):
        out = filter_boilerplate(["a", "Tweet", "b", "Share on Facebook", "c"])
        assert out == ["a", "b", "c"]

    def test_handles_iterables(self):
        gen = (p for p in ["x", "Tweet", "y"])
        out = filter_boilerplate(gen)
        assert out == ["x", "y"]

    def test_empty_list(self):
        assert filter_boilerplate([]) == []


# ---------------------------------------------------------------------------
# Per-parser integration: lamley
# ---------------------------------------------------------------------------


def _make_response(text="", status=200):
    import requests
    resp = MagicMock(spec=requests.Response)
    resp.text = text
    resp.status_code = status
    resp.content = text.encode("utf-8")
    resp.raise_for_status.return_value = None
    return resp


class TestLamleyIntegration:
    def test_share_paragraphs_stripped(self):
        import lamley_source

        html = """
        <html><body>
        <h1 class="entry-title">Hot Wheels Chase Spotted</h1>
        <article><div class="entry-content">
            <p>The first lead paragraph (becomes subtitle).</p>
            <p>Mattel announced a new variant today.</p>
            <p>Share on Facebook</p>
            <p>Tweet</p>
            <p>Collectors have been waiting for years.</p>
            <p>Subscribe to our newsletter</p>
        </div></article>
        </body></html>
        """
        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        out = lamley_source.fetch_lamley_article(
            "http://lamleygroup.com/x", session=session
        )
        assert out is not None
        assert "Mattel announced a new variant today." in out["paragraphs"]
        assert "Collectors have been waiting for years." in out["paragraphs"]
        # Boilerplate paragraphs MUST be filtered:
        assert "Share on Facebook" not in out["paragraphs"]
        assert "Tweet" not in out["paragraphs"]
        assert "Subscribe to our newsletter" not in out["paragraphs"]


# ---------------------------------------------------------------------------
# Per-parser integration: mattel
# ---------------------------------------------------------------------------


class TestMattelIntegration:
    def test_share_paragraphs_stripped(self):
        from mattel_news_source import fetch_mattel_article
        from tests.fixtures.mattel_flight_builder import _make_flight_article

        body_html = (
            "<p>Mattel announced the new Hot Wheels Legends Tour.</p>"
            "<p>Share on Facebook</p>"
            "<p>Tweet</p>"
            "<p>Collectors are excited about this release.</p>"
            "<p>Subscribe</p>"
        )
        entry = {
            "handle": "hot-wheels-legends-tour",
            "title": "Hot Wheels Legends Tour",
            "date": "2026-04-13",
            "excerpt": "Editorial lead",
            "thumbnail": {"url": "https://example.com/thumb.jpg"},
            "url": "https://corporate.mattel.com/news/hot-wheels-legends-tour",
        }
        html = _make_flight_article(entry, body_html=body_html)

        session = MagicMock()
        session.get.return_value = _make_response(text=html)
        out = fetch_mattel_article(
            "https://corporate.mattel.com/news/hot-wheels-legends-tour",
            session=session,
        )
        assert out is not None
        assert "Mattel announced the new Hot Wheels Legends Tour." in out["paragraphs"]
        assert "Collectors are excited about this release." in out["paragraphs"]
        # Boilerplate gone:
        assert "Share on Facebook" not in out["paragraphs"]
        assert "Tweet" not in out["paragraphs"]
        assert "Subscribe" not in out["paragraphs"]


# ---------------------------------------------------------------------------
# Per-parser integration: autoevolution (paragraphs AND blocks)
# ---------------------------------------------------------------------------


class TestAutoevolutionIntegration:
    def _scrape(self, html):
        from autoevolution_source import _scrape_article_page

        def fetcher(url):
            resp = MagicMock()
            resp.text = html
            resp.status_code = 200
            return resp

        return _scrape_article_page(
            "https://autoevolution.com/news/hot-wheels-chase-12345.html",
            fetcher=fetcher,
        )

    def test_share_paragraphs_stripped_from_paragraphs_and_blocks(self):
        html = """
        <html><body>
        <h1>Hot Wheels Chase Car</h1>
        <div class="newstext">
        <p>The rare Porsche is finally here.</p>
        <p>Share on Facebook</p>
        <p>Tweet</p>
        <p>Production run details follow.</p>
        <p>Subscribe to our newsletter</p>
        <p>Related articles</p>
        </div>
        </body></html>
        """
        out = self._scrape(html)
        assert out is not None
        # Flat paragraphs filtered:
        assert "The rare Porsche is finally here." in out["paragraphs"]
        assert "Production run details follow." in out["paragraphs"]
        assert "Share on Facebook" not in out["paragraphs"]
        assert "Tweet" not in out["paragraphs"]
        assert "Subscribe to our newsletter" not in out["paragraphs"]
        assert "Related articles" not in out["paragraphs"]
        # Structured blocks filtered too:
        block_texts = [b.get("text", "") for b in out["blocks"]]
        assert "The rare Porsche is finally here." in block_texts
        assert "Production run details follow." in block_texts
        assert "Share on Facebook" not in block_texts
        assert "Tweet" not in block_texts
        assert "Subscribe to our newsletter" not in block_texts
        assert "Related articles" not in block_texts


# ---------------------------------------------------------------------------
# Pure-helper tests for filter_blocks
# ---------------------------------------------------------------------------


class TestFilterBlocks:
    """``filter_blocks`` — same UI-junk strip as ``filter_boilerplate``
    but for the structured-block representation used by autoevolution
    (lead / paragraph / image / video). Media-bearing blocks are
    preserved regardless of short captions (visual content beats
    label heuristic)."""

    def test_drops_pure_text_boilerplate_block(self):
        blocks = [
            {"type": "paragraph", "text": "Real article paragraph."},
            {"type": "paragraph", "text": "Share on Facebook"},
            {"type": "paragraph", "text": "Subscribe to our newsletter"},
            {"type": "paragraph", "text": "Another real paragraph."},
        ]
        out = filter_blocks(blocks)
        assert len(out) == 2
        assert out[0]["text"] == "Real article paragraph."
        assert out[1]["text"] == "Another real paragraph."

    def test_keeps_image_block_with_short_caption(self):
        blocks = [
            {"type": "image", "src": "https://cdn/x.jpg",
             "caption": "1995 Honda NSX"},
            {"type": "image", "src": "https://cdn/y.jpg",
             "caption": "Toyota AE86 Trueno"},
        ]
        out = filter_blocks(blocks)
        assert len(out) == 2

    def test_keeps_video_block_without_caption(self):
        blocks = [
            {"type": "video", "src": "https://youtube.com/watch?v=abc"},
        ]
        out = filter_blocks(blocks)
        assert len(out) == 1

    def test_drops_image_with_boilerplate_caption_and_no_text(self):
        blocks = [
            {"type": "image", "src": "https://cdn/banner.jpg",
             "caption": "Subscribe to our newsletter"},
        ]
        out = filter_blocks(blocks)
        assert out == []

    def test_keeps_image_with_boilerplate_caption_if_text_present(self):
        blocks = [
            {"type": "image", "src": "https://cdn/x.jpg",
             "caption": "Share on Facebook",
             "text": "This is the actual story body about the Porsche."},
        ]
        out = filter_blocks(blocks)
        assert len(out) == 1

    def test_drops_non_dict_entries(self):
        blocks = [
            {"type": "paragraph", "text": "Real para"},
            "not-a-dict",
            None,
            42,
        ]
        out = filter_blocks(blocks)
        assert len(out) == 1

    def test_preserves_order(self):
        blocks = [
            {"type": "paragraph", "text": "First."},
            {"type": "paragraph", "text": "Share on Facebook"},
            {"type": "paragraph", "text": "Second."},
            {"type": "paragraph", "text": "Subscribe"},
            {"type": "paragraph", "text": "Third."},
        ]
        out = filter_blocks(blocks)
        assert [b["text"] for b in out] == ["First.", "Second.", "Third."]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
