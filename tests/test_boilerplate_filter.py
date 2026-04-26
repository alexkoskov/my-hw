#!/usr/bin/env python3
"""Unit tests for boilerplate_filter.

Covers the pure helpers (``is_boilerplate`` / ``filter_boilerplate``) plus
per-parser integration: feeding synthetic article HTML into each source
parser and asserting that UI-boilerplate paragraphs ("Share on Facebook",
"Tweet", etc.) are stripped before the parser returns.
"""

from unittest.mock import MagicMock

import pytest

from boilerplate_filter import filter_boilerplate, is_boilerplate


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
        # >80 chars; even though it begins with "Share on Facebook"
        # we treat it as real prose because the bound is exceeded.
        long_text = (
            "Share on Facebook with all your friends and family for the rest "
            "of your life and even beyond, share share share."
        )
        assert len(long_text) > 80
        assert is_boilerplate(long_text) is False


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
