#!/usr/bin/env python3
"""Unit tests for telegraph_publisher."""

import json
import logging
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import requests

import telegraph_publisher as tp


def _make_response(json_body, status=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


class TestApiCall:
    def test_success(self):
        session = MagicMock()
        session.post.return_value = _make_response({"ok": True, "result": {"x": 1}})
        out = tp._api_call("someMethod", {"a": "b"}, session=session)
        assert out == {"x": 1}
        session.post.assert_called_once()
        url = session.post.call_args[0][0]
        assert url.endswith("/someMethod")

    def test_api_error_raises(self):
        session = MagicMock()
        session.post.return_value = _make_response({"ok": False, "error": "boom"})
        with pytest.raises(tp.TelegraphError, match="boom"):
            tp._api_call("m", {}, session=session)


class TestCreateAccount:
    def test_returns_token(self):
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"access_token": "tok-123"}}
        )
        assert tp.create_account(session=session) == "tok-123"


class TestEnsureAccessToken:
    def test_returns_env_token_without_api_call(self, monkeypatch):
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "cached-token")
        session = MagicMock()
        assert tp.ensure_access_token(session=session) == "cached-token"
        session.post.assert_not_called()

    def test_creates_and_persists_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n")
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"access_token": "new-token"}}
        )
        token = tp.ensure_access_token(env_path=str(env_file), session=session)
        assert token == "new-token"
        assert os.environ[tp.ENV_TOKEN_KEY] == "new-token"
        contents = env_file.read_text()
        assert "FOO=bar" in contents
        assert f"{tp.ENV_TOKEN_KEY}=new-token" in contents

    def test_updates_existing_env_entry(self, monkeypatch, tmp_path):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(f"FOO=bar\n{tp.ENV_TOKEN_KEY}=old-token\nBAZ=qux\n")
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"access_token": "fresh"}}
        )
        tp.ensure_access_token(env_path=str(env_file), session=session)
        contents = env_file.read_text()
        assert f"{tp.ENV_TOKEN_KEY}=fresh" in contents
        assert "old-token" not in contents
        assert "FOO=bar" in contents
        assert "BAZ=qux" in contents


class TestBuildContent:
    def test_hero_image_first(self):
        nodes = tp._build_content("", ["para one"], ["https://cdn.example.com/img1.jpg"], None)
        assert nodes[0]["tag"] == "figure"
        assert nodes[0]["children"][0]["attrs"]["src"] == "https://cdn.example.com/img1.jpg"
        assert nodes[1]["tag"] == "p"

    def test_source_link_at_end(self):
        nodes = tp._build_content("", ["para"], [], "http://src")
        # Footer is a plain <p> (no <aside> — breaks IV clickability).
        last = nodes[-1]
        assert last["tag"] == "p"
        anchor = last["children"][1]
        assert anchor["tag"] == "a"
        assert anchor["attrs"]["href"] == "http://src"

    def test_images_interleaved_every_third_paragraph(self):
        paragraphs = [f"p{i}" for i in range(6)]
        images = ["https://cdn.example.com/hero.jpg", "https://cdn.example.com/mid.jpg", "https://cdn.example.com/extra.jpg"]
        nodes = tp._build_content("", paragraphs, images, None)
        # [figure(hero), p0, p1, p2, figure(mid), p3, p4, p5, figure(extra)]
        tags = [n["tag"] for n in nodes]
        assert tags == ["figure", "p", "p", "p", "figure", "p", "p", "p", "figure"]

    def test_no_images(self):
        nodes = tp._build_content("", ["a", "b"], [], None)
        assert all(n["tag"] == "p" for n in nodes)

    def test_subtitle_adds_decorated_lead_and_hr(self):
        nodes = tp._build_content(
            "Forgive me father",
            ["body para"],
            ["https://cdn.example.com/hero.jpg"],
            "http://src",
        )
        # [figure(hero), p(italic "💬 «subtitle»"), hr, p(body), p(footer)]
        tags = [n["tag"] for n in nodes]
        assert tags == ["figure", "p", "hr", "p", "p"]
        subtitle_p = nodes[1]
        italic_child = subtitle_p["children"][0]
        assert italic_child["tag"] == "i"
        assert italic_child["children"] == ["💬 «Forgive me father»"]

    def test_empty_subtitle_skips_lead_and_hr(self):
        nodes = tp._build_content("", ["body"], ["https://cdn.example.com/hero.jpg"], None)
        # No hr when subtitle is empty
        assert all(n["tag"] != "hr" for n in nodes)


class TestBuildContentFromBlocks:
    def test_image_caption_becomes_figcaption(self):
        blocks = [
            {"type": "image", "src": "https://cdn.example.com/hero.jpg", "caption": "Photo: Mattel"},
            {"type": "paragraph", "text": "Body."},
            {"type": "image", "src": "https://cdn.example.com/inline.jpg", "caption": "Photo: Lamley Group"},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        hero_children = nodes[0]["children"]
        assert hero_children[0]["tag"] == "img"
        assert hero_children[0]["attrs"]["src"] == "https://cdn.example.com/hero.jpg"
        assert hero_children[1] == {"tag": "figcaption", "children": ["Photo: Mattel"]}
        inline_figure = nodes[2]
        assert inline_figure["tag"] == "figure"
        assert inline_figure["children"][1] == {"tag": "figcaption", "children": ["Photo: Lamley Group"]}

    def test_image_without_caption_has_no_figcaption(self):
        blocks = [{"type": "image", "src": "https://cdn.example.com/hero.jpg"}]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes[0]["children"] == [{"tag": "img", "attrs": {"src": "https://cdn.example.com/hero.jpg"}}]

    def test_video_block_becomes_iframe(self):
        blocks = [
            {"type": "paragraph", "text": "text"},
            {"type": "video", "src": "https://telegra.ph/embed/youtube?url=..."},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        iframe = nodes[-1]
        assert iframe["tag"] == "iframe"
        assert iframe["attrs"]["src"].startswith("https://telegra.ph/embed/")

    def test_runs_are_metadata_not_rendered_inline(self):
        """Phase 1 keeps href metadata in runs but does not emit `<a>` nodes.
        Phase 2 (cross-article linking) will consume the runs."""
        blocks = [
            {
                "type": "paragraph",
                "text": "See Red Line Club for more.",
                "runs": [
                    {"text": "See "},
                    {"text": "Red Line Club", "href": "https://mattel.com/rlc"},
                    {"text": " for more."},
                ],
            }
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        p_node = nodes[0]
        assert p_node == {"tag": "p", "children": ["See Red Line Club for more."]}

    def test_block_order_preserved_except_hero_promotion(self):
        blocks = [
            {"type": "paragraph", "text": "p1"},
            {"type": "image", "src": "https://cdn.example.com/img1.jpg"},
            {"type": "paragraph", "text": "p2"},
            {"type": "image", "src": "https://cdn.example.com/img2.jpg"},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        # First image promoted to hero; remaining blocks in original order
        tags = [n["tag"] for n in nodes]
        assert tags == ["figure", "p", "p", "figure"]


class TestPublishArticle:
    def test_success(self, monkeypatch):
        """publish_article does a single createPage call and returns the URL."""
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "tok")
        session = MagicMock()
        session.post.return_value = _make_response({
            "ok": True,
            "result": {
                "url": "https://telegra.ph/Test-04-20",
                "path": "Test-04-20",
            },
        })
        url = tp.publish_article(
            title="Заголовок",
            paragraphs=["Абзац 1.", "Абзац 2."],
            images=["https://cdn.example.com/img1.jpg"],
            source_url="http://source",
            session=session,
        )
        assert url == "https://telegra.ph/Test-04-20"
        assert session.post.call_count == 1
        assert session.post.call_args[0][0].endswith("/createPage")
        # Источник footer is the last <p> with the source link
        content = json.loads(session.post.call_args[1]["data"]["content"])
        footer = content[-1]
        assert footer["tag"] == "p"
        assert footer["children"][0] == "Источник: "
        assert footer["children"][1]["attrs"]["href"] == "http://source"

    def test_no_token_raises(self, monkeypatch):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        with pytest.raises(tp.TelegraphError, match="not set"):
            tp.publish_article("t", ["p"], [])

    def test_explicit_token_overrides_env(self, monkeypatch):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        session = MagicMock()
        session.post.return_value = _make_response({
            "ok": True,
            "result": {"url": "https://telegra.ph/X", "path": "X"},
        })
        tp.publish_article(
            title="T", paragraphs=["p"], images=[],
            access_token="explicit", session=session,
        )
        assert session.post.call_args[1]["data"]["access_token"] == "explicit"


class TestAutoMarkerInArticleBody:
    """Auto-fallback marker is rendered as a plain `<p>` paragraph node
    inside the Telegra.ph article, IMMEDIATELY before the `Источник:`
    footer, only when ``publish_article`` is called with
    ``auto_marker=True``. The manual-review path defaults to False and
    produces a tree with no marker — the channel teaser stays a clean
    single-line hashtag for both paths.

    Marker text is byte-pinned to ``↳ автоперевод`` (U+21B3 + space +
    Russian "autotranslation"). Plain `<p>`, no `<i>`/`<b>` wrap —
    unobtrusive but visible.
    """

    MARKER_TEXT = "↳ автоперевод"

    def _publish_capture(self, monkeypatch, **kwargs):
        """Helper: invoke publish_article with a mocked session and return
        the parsed `content` array sent to createPage."""
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "tok")
        session = MagicMock()
        session.post.return_value = _make_response({
            "ok": True,
            "result": {"url": "https://telegra.ph/X", "path": "X"},
        })
        tp.publish_article(session=session, **kwargs)
        return json.loads(session.post.call_args[1]["data"]["content"])

    def test_auto_fallback_telegraph_includes_marker_before_source_footer(
        self, monkeypatch,
    ):
        """``auto_marker=True`` (auto-fallback path) → the marker `<p>` is
        the second-to-last node, immediately before the `Источник:`
        footer."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["body para 1", "body para 2"],
            images=["https://cdn.example.com/hero.jpg"],
            source_url="https://example.com/src",
            subtitle="Лид",
            auto_marker=True,
        )

        # Footer is last; marker is the node directly before it.
        footer = content[-1]
        assert footer["tag"] == "p"
        assert footer["children"][0] == "Источник: "
        marker = content[-2]
        assert marker == {"tag": "p", "children": [self.MARKER_TEXT]}

    def test_manual_review_telegraph_does_not_include_marker(
        self, monkeypatch,
    ):
        """``auto_marker`` defaults to False (manual-review path) → no
        ``автоперевод`` substring anywhere in the node tree."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["body"],
            images=["https://cdn.example.com/hero.jpg"],
            source_url="https://example.com/src",
            subtitle="Лид",
        )

        # Walk the tree, collect every string, assert no marker text.
        collected: list[str] = []

        def _walk(node):
            if isinstance(node, str):
                collected.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(content)
        joined = "".join(collected)
        assert "автоперевод" not in joined
        assert "↳" not in joined

    def test_marker_codepoint_is_u21b3(self, monkeypatch):
        """Pin the arrow byte-for-byte to U+21B3 ('↳'). Pinning the
        codepoint protects against editor-driven character drift
        (e.g. someone replaces it with U+2192 '→'). Asserted on the
        Telegraph node tree (not the channel teaser — see
        Decision 14: teaser is single-line hashtag for both paths)."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["p"],
            images=[],
            source_url="https://example.com/src",
            auto_marker=True,
        )
        marker = content[-2]
        text = marker["children"][0]
        assert text == self.MARKER_TEXT
        assert "↳" in text  # U+21B3 LOWERWARDS ARROW WITH TIP RIGHTWARDS
        # Reject look-alike arrows.
        for bad in ("→", "↪", "↱", "->"):
            assert bad not in text

    def test_auto_marker_default_is_false(self, monkeypatch):
        """publish_article without ``auto_marker`` (manual-review path
        ``hw_review.cmd_publish`` style) yields no marker."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["p"],
            images=[],
            source_url="https://example.com/src",
        )
        # Footer is the last node; node before it must NOT be the marker.
        if len(content) >= 2:
            prev = content[-2]
            assert prev != {"tag": "p", "children": [self.MARKER_TEXT]}

    def test_marker_renders_as_plain_paragraph_no_decoration(
        self, monkeypatch,
    ):
        """No italic/bold wrap — children is a flat ``[str]``."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["p"],
            images=[],
            source_url="https://example.com/src",
            auto_marker=True,
        )
        marker = content[-2]
        assert marker["tag"] == "p"
        # Single string child, no nested tags (i, b, etc.).
        assert len(marker["children"]) == 1
        assert isinstance(marker["children"][0], str)

    def test_auto_marker_works_on_blocks_path(self, monkeypatch):
        """``auto_marker=True`` injects the marker on the blocks path too
        (Mattel/Lamley/RSS use blocks). Position must remain immediately
        before the `Источник:` footer."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=None,
            images=None,
            source_url="https://example.com/src",
            blocks=[
                {"type": "image", "src": "https://cdn.example.com/hero.jpg"},
                {"type": "paragraph", "text": "body"},
            ],
            auto_marker=True,
        )
        footer = content[-1]
        assert footer["children"][0] == "Источник: "
        marker = content[-2]
        assert marker == {"tag": "p", "children": ["↳ автоперевод"]}

    def test_auto_marker_without_source_url_appends_at_end(
        self, monkeypatch,
    ):
        """If the article has no source_url (no footer node), the marker
        still appears — at the end of the node tree. Defensive — current
        code paths always pass source_url, but the function shouldn't
        crash if a future caller skips it."""
        content = self._publish_capture(
            monkeypatch,
            title="T",
            paragraphs=["p"],
            images=[],
            source_url=None,
            auto_marker=True,
        )
        # Last node should be the marker since there's no footer.
        assert content[-1] == {"tag": "p", "children": ["↳ автоперевод"]}


class TestPreviewNodes:
    """Tests for the public preview_nodes wrapper.

    preview_nodes is the offline mirror of the node tree that
    publish_article would upload to Telegraph's createPage. It must never
    touch the network and must not require TELEGRAPH_ACCESS_TOKEN.
    """

    def test_returns_list_of_dicts(self):
        nodes = tp.preview_nodes(title="t", paragraphs=["p1"])
        assert isinstance(nodes, list)
        assert len(nodes) > 0
        for node in nodes:
            assert isinstance(node, dict)
            assert "tag" in node

    def test_flat_path_matches_build_content(self):
        paragraphs = ["p1", "p2"]
        images = ["https://x/1.jpg"]
        source_url = "https://src"
        subtitle = "sub"
        preview = tp.preview_nodes(
            title="t",
            paragraphs=paragraphs,
            images=images,
            source_url=source_url,
            subtitle=subtitle,
        )
        direct = tp._build_content(subtitle, paragraphs, images, source_url)
        assert preview == direct

    def test_blocks_path_matches_build_content_from_blocks(self):
        blocks = [
            {"type": "image", "src": "https://cdn.example.com/hero.jpg", "caption": "cap"},
            {"type": "paragraph", "text": "body"},
        ]
        subtitle = "sub"
        source_url = "https://src"
        preview = tp.preview_nodes(
            title="t",
            paragraphs=["ignored"],
            images=["https://cdn.example.com/ignored.jpg"],
            source_url=source_url,
            subtitle=subtitle,
            blocks=blocks,
        )
        direct = tp._build_content_from_blocks(subtitle, blocks, source_url)
        assert preview == direct

    def test_no_network_call(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("no network allowed in preview_nodes")

        monkeypatch.setattr(tp, "_api_call", _boom)
        # Also guard requests.post in case someone bypasses _api_call.
        monkeypatch.setattr(tp.requests, "post", _boom)
        nodes = tp.preview_nodes(
            title="t",
            paragraphs=["p1", "p2"],
            images=["https://x/1.jpg"],
            source_url="https://src",
            subtitle="sub",
        )
        assert isinstance(nodes, list)

    def test_no_token_required(self, monkeypatch):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        nodes = tp.preview_nodes(title="t", paragraphs=["p"])
        assert isinstance(nodes, list)
        assert len(nodes) > 0

    def test_hero_image_figure_first(self):
        nodes = tp.preview_nodes(
            title="t",
            paragraphs=["body"],
            images=["https://cdn/1.jpg"],
        )
        assert nodes[0]["tag"] == "figure"
        inner = nodes[0]["children"][0]
        assert inner["tag"] == "img"
        assert inner["attrs"]["src"] == "https://cdn/1.jpg"

    def test_empty_paragraphs_and_no_blocks(self):
        # No source_url → _build_content returns [] for empty paragraphs; no exception.
        nodes = tp.preview_nodes(title="t")
        assert isinstance(nodes, list)

        # With source_url, only the footer is emitted.
        nodes_with_footer = tp.preview_nodes(title="t", source_url="https://src")
        assert isinstance(nodes_with_footer, list)
        assert len(nodes_with_footer) == 1
        assert nodes_with_footer[0]["tag"] == "p"

    def test_title_not_in_nodes(self):
        title = "UniqueTitleMarker12345"
        nodes = tp.preview_nodes(
            title=title,
            paragraphs=["body paragraph"],
            images=["https://x/1.jpg"],
            source_url="https://src",
            subtitle="subtitle text",
        )

        def _walk(node):
            """Yield every string found inside any node's children or attrs."""
            if isinstance(node, str):
                yield node
                return
            if isinstance(node, dict):
                for v in node.values():
                    yield from _walk(v)
                return
            if isinstance(node, list):
                for item in node:
                    yield from _walk(item)

        for text in _walk(nodes):
            assert title not in text, f"title leaked into node tree: {text!r}"

    def test_empty_blocks_falls_back_to_flat(self):
        """Empty blocks list is falsy; preview_nodes must take the flat path,
        matching publish_article's `if blocks:` branch."""
        preview = tp.preview_nodes(
            title="t",
            paragraphs=["p1"],
            images=["https://x/1.jpg"],
            source_url="https://src",
            subtitle="",
            blocks=[],
        )
        direct = tp._build_content("", ["p1"], ["https://x/1.jpg"], "https://src")
        assert preview == direct

    def test_parity_with_publish_article_payload(self, monkeypatch):
        """The node list uploaded by publish_article equals preview_nodes output
        for the same inputs — the whole point of exposing the wrapper."""
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "tok")
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"url": "https://telegra.ph/X", "path": "X"}}
        )
        kwargs = dict(
            title="Заголовок",
            paragraphs=["Абзац 1.", "Абзац 2."],
            images=["https://x/hero.jpg"],
            source_url="https://src",
            subtitle="Лид",
        )
        tp.publish_article(session=session, **kwargs)
        uploaded = json.loads(session.post.call_args[1]["data"]["content"])
        preview = tp.preview_nodes(**kwargs)
        assert preview == uploaded

    def test_parity_with_publish_article_blocks_path(self, monkeypatch):
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "tok")
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"url": "https://telegra.ph/Y", "path": "Y"}}
        )
        kwargs = dict(
            title="T",
            paragraphs=None,
            images=None,
            source_url="https://src",
            subtitle="Sub",
            blocks=[
                {"type": "image", "src": "https://cdn.example.com/hero.jpg", "caption": "cap"},
                {"type": "paragraph", "text": "body"},
                {"type": "heading", "text": "Header", "level": 3},
            ],
        )
        tp.publish_article(session=session, **kwargs)
        uploaded = json.loads(session.post.call_args[1]["data"]["content"])
        preview = tp.preview_nodes(**kwargs)
        assert preview == uploaded

    def test_unicode_and_html_entity_passthrough(self):
        """Cyrillic, emoji and HTML-entity-like text pass through without mangling."""
        paragraphs = ["Привет 🚗 <b>&amp;</b>", "Второй абзац 💬"]
        nodes = tp.preview_nodes(
            title="Заголовок",
            paragraphs=paragraphs,
            subtitle="Лид 🎉",
        )
        # Collect every string in the tree
        collected: list[str] = []

        def _walk(node):
            if isinstance(node, str):
                collected.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(nodes)
        assert "Привет 🚗 <b>&amp;</b>" in collected
        assert "Второй абзац 💬" in collected
        assert "💬 «Лид 🎉»" in collected


SOURCE_URL = "https://orangetrackdiecast.com/article"


class TestRenderParagraphWithRuns:
    """Direct unit tests for ``_render_paragraph_with_runs`` helper.

    Inline links are intentionally NOT rendered (2026-05-13 product
    decision — subscribers reading the Russian translation should not
    be hyperlinked to English source pages mid-prose; Telegra.ph page
    still carries the «Источник: …» footer with the original URL).
    href metadata is preserved in `runs` for the future cross-article-
    linking feature (dormant). Inline formats (bold/italic/underline/
    strikethrough) DO render — see ``TestInlineFormats``.
    """

    def test_same_site_href_not_rendered(self):
        text = "A B C"
        runs = [{"text": "B", "href": "https://orangetrackdiecast.com/x"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_external_href_not_rendered(self):
        text = "See Mattel for more"
        runs = [{"text": "Mattel", "href": "https://mattel.com/news"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_run_text_not_in_paragraph_text_falls_through(self):
        text = "Plain paragraph body"
        runs = [{"text": "Ferrari", "href": "https://orangetrackdiecast.com/f"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_run_with_empty_text_skipped(self):
        text = "ABCDE"
        runs = [{"text": "", "href": "https://orangetrackdiecast.com/x"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_run_with_none_href_skipped(self):
        text = "ABCDE"
        runs = [{"text": "BCD", "href": None}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_run_with_malformed_href_skipped(self):
        text = "ABCDE"
        runs = [{"text": "BCD", "href": "not a url://!!"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]


@pytest.mark.parametrize("runs", [[], None])
def test_no_runs_renders_plain_text(runs):
    text = "Some plain paragraph text"
    children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
    assert children == [text]


class TestInlineFormats:
    """Inline format wrapping (Decisions 11-12 — orangetrack-inline-formats):
    `formats` metadata in runs maps to <strong>/<i>/<u>/<s> Telegraph nodes.
    Color classes upstream → bold. <a> dominates formats (a > strong nesting).
    """

    def test_bold_run_wraps_in_strong(self):
        text = "This is bold text in a sentence"
        runs = [{"text": "bold text", "formats": ["bold"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "This is ",
            {"tag": "strong", "children": ["bold text"]},
            " in a sentence",
        ]

    def test_italic_run_wraps_in_i(self):
        text = "This is italic phrase here"
        runs = [{"text": "italic phrase", "formats": ["italic"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "This is ",
            {"tag": "i", "children": ["italic phrase"]},
            " here",
        ]

    def test_underline_run_wraps_in_u(self):
        text = "Look at the underlined word now"
        runs = [{"text": "underlined word", "formats": ["underline"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "Look at the ",
            {"tag": "u", "children": ["underlined word"]},
            " now",
        ]

    def test_strikethrough_run_wraps_in_s(self):
        text = "Old crossed out content removed"
        runs = [{"text": "crossed out", "formats": ["strikethrough"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "Old ",
            {"tag": "s", "children": ["crossed out"]},
            " content removed",
        ]

    def test_combined_bold_italic_outer_strong_inner_i(self):
        """Multiple formats nest: bold outer, italic inner (per _FORMAT_TAGS order)."""
        text = "Both formats applied here"
        runs = [{"text": "formats applied", "formats": ["bold", "italic"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        # _FORMAT_TAGS order: bold > italic — bold is outermost, italic inside.
        assert children == [
            "Both ",
            {"tag": "strong", "children": [
                {"tag": "i", "children": ["formats applied"]},
            ]},
            " here",
        ]

    def test_same_site_href_with_bold_drops_link_keeps_format(self):
        """2026-05-13: links no longer render. href is silently dropped;
        the bold format wrapper is preserved (text still appears bold,
        just not hyperlinked)."""
        text = "Visit Mercedes-Benz today"
        runs = [{
            "text": "Mercedes-Benz",
            "href": "https://orangetrackdiecast.com/mercedes",
            "formats": ["bold"],
        }]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "Visit ",
            {"tag": "strong", "children": ["Mercedes-Benz"]},
            " today",
        ]

    def test_external_link_with_bold_drops_link_keeps_format(self):
        """External href dropped (per AC7), but bold format preserved."""
        text = "See external resource here"
        runs = [{
            "text": "external resource",
            "href": "https://other-site.com/x",
            "formats": ["bold"],
        }]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "See ",
            {"tag": "strong", "children": ["external resource"]},
            " here",
        ]

    def test_run_with_only_formats_no_href_renders(self):
        """Run with formats but no href at all renders the format wrapping."""
        text = "Plain bold word here"
        runs = [{"text": "bold word", "formats": ["bold"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            "Plain ",
            {"tag": "strong", "children": ["bold word"]},
            " here",
        ]

    def test_unknown_format_silently_ignored(self):
        """Unknown format markers (defensive) are filtered out."""
        text = "Mixed signal example shown"
        runs = [{"text": "Mixed signal", "formats": ["bold", "rainbow"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [
            {"tag": "strong", "children": ["Mixed signal"]},
            " example shown",
        ]

    def test_format_run_text_not_in_paragraph_dropped(self):
        """If run.text not found in block.text, format is silently dropped."""
        text = "Russian translated paragraph"
        runs = [{"text": "BoldEnglishWord", "formats": ["bold"]}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_overlapping_format_runs_first_wins(self):
        """Overlapping spans: first wins, second appears plain (Decision 5)."""
        text = "ABCDE"
        runs = [
            {"text": "ABCD", "formats": ["bold"]},
            {"text": "BCDE", "formats": ["italic"]},
        ]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        # First run wins: "ABCD" wrapped in bold, "E" plain. Second's "BCDE"
        # span overlaps, dropped — text still appears via segments.
        assert children == [
            {"tag": "strong", "children": ["ABCD"]},
            "E",
        ]

    def test_run_with_empty_formats_list_no_href_skipped(self):
        """Run with empty formats AND no href contributes nothing."""
        text = "Just plain text here"
        runs = [{"text": "plain text", "formats": []}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]


class TestSchemesAndDomainEdges:
    """Scheme filtering and domain normalisation edge cases (Decision 4)."""

    def test_mailto_scheme_dropped(self):
        text = "Email admin for help"
        runs = [{"text": "admin", "href": "mailto:admin@orangetrackdiecast.com"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_javascript_scheme_dropped(self):
        text = "Click here for fun"
        runs = [{"text": "Click", "href": "javascript:alert(1)"}]
        children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]

    def test_empty_netloc_dropped_when_source_also_empty(self):
        text = "Email admin for help"
        runs = [{"text": "admin", "href": "mailto:x@orangetrackdiecast.com"}]
        children = tp._render_paragraph_with_runs(text, runs, "")
        assert children == [text]

    def test_no_special_handling_for_same_site_href(self):
        # 2026-05-13: same-site links no longer rendered. www-prefix
        # normalisation in `_is_same_site` is still tested in isolation
        # (dormant feature, ready for cross-article linking), but at the
        # paragraph-render level the href is always dropped.
        text = "See Ferrari news"
        runs = [{"text": "Ferrari", "href": "https://orangetrackdiecast.com/f"}]
        children = tp._render_paragraph_with_runs(
            text, runs, "https://www.orangetrackdiecast.com/article",
        )
        assert children == [text]


class TestListItemRendering:
    """list_item block rendering through _build_content_from_blocks."""

    def test_list_item_renders_with_bullet_prefix(self):
        blocks = [{"type": "list_item", "text": "Ferrari", "runs": []}]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes == [{"tag": "p", "children": ["• ", "Ferrari"]}]

    def test_list_item_with_href_run_renders_plain_text(self):
        # 2026-05-13: inline links no longer rendered. The bullet prefix
        # and plain item text are preserved; the href is silently
        # dropped (still kept in `runs` metadata for the dormant cross-
        # article linking feature).
        blocks = [{
            "type": "list_item",
            "text": "Ferrari news",
            "runs": [{"text": "Ferrari", "href": "https://orangetrackdiecast.com/f"}],
        }]
        nodes = tp._build_content_from_blocks("", blocks, SOURCE_URL)
        assert nodes[0] == {
            "tag": "p",
            "children": ["• ", "Ferrari news"],
        }

    def test_list_item_strips_leading_bullet_in_text(self):
        # Decision 10 — bullet-doubling guard: "• " must not appear twice
        blocks = [{"type": "list_item", "text": "• Ferrari", "runs": []}]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes == [{"tag": "p", "children": ["• ", "Ferrari"]}]


class TestHeadingRendering:
    """heading block rendering through _build_content_from_blocks."""

    def test_heading_block_renders_h3(self):
        blocks = [{"type": "heading", "level": 3, "text": "Section"}]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes == [{"tag": "h3", "children": ["Section"]}]

    def test_heading_block_with_href_run_renders_plain_text(self):
        # 2026-05-13: inline links no longer rendered. The heading
        # type/level/text are preserved; href in runs is silently dropped.
        blocks = [{
            "type": "heading",
            "level": 3,
            "text": "About Ferrari today",
            "runs": [{"text": "Ferrari", "href": "https://orangetrackdiecast.com/f"}],
        }]
        nodes = tp._build_content_from_blocks("", blocks, SOURCE_URL)
        assert nodes[0] == {
            "tag": "h3",
            "children": ["About Ferrari today"],
        }


class TestDoSBounds:
    """DoS-bound fall-through (Decision 10) with WARNING log assertion."""

    def test_dos_bound_skips_helper_on_huge_text(self, caplog):
        text = "A" * (tp._MAX_TEXT_FOR_RUNS + 1)
        runs = [{"text": "A", "href": "https://orangetrackdiecast.com/x"}]
        with caplog.at_level(logging.WARNING, logger="telegraph_publisher"):
            children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]
        assert any(
            "DoS bound" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_dos_bound_skips_helper_on_too_many_runs(self, caplog):
        text = "Some short text"
        runs = [
            {"text": "x", "href": "https://orangetrackdiecast.com/x"}
            for _ in range(tp._MAX_RUNS_PER_BLOCK + 1)
        ]
        with caplog.at_level(logging.WARNING, logger="telegraph_publisher"):
            children = tp._render_paragraph_with_runs(text, runs, SOURCE_URL)
        assert children == [text]
        assert any(
            "DoS bound" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Literal `**` markers must never reach a reader (prod complaint 2026-07-28).
#
# The LLM is primed to emit `**bold**` on EVERY article — the system prompt
# explains the markers unconditionally — but before this fix the decode step
# ran ONLY on the variant-B block-patch path. Every other route published the
# raw asterisks: a flat-`paragraphs` source, a model that returned its own
# `blocks`, the page title, an image caption. Rendering is where all routes
# converge, so that is where the guarantee is made and where it is pinned.
# ---------------------------------------------------------------------------


def _flatten(node):
    """Yield every string in a node tree (order not significant)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for child in node.get("children") or []:
            yield from _flatten(child)
        attrs = node.get("attrs") or {}
        for value in attrs.values():
            if isinstance(value, str):
                yield value
    elif isinstance(node, list):
        for item in node:
            yield from _flatten(item)


def _strong_texts(nodes):
    """Every text wrapped in a <strong> node, anywhere in the tree."""
    found = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("tag") == "strong":
                found.extend(_flatten(n))
            for child in n.get("children") or []:
                walk(child)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(nodes)
    return found


class TestBoldMarkerDecoding:
    def test_prod_case_whole_paragraph_marked_becomes_bold(self):
        # The literal text the operator saw on the published page.
        nodes = tp._build_content(
            subtitle="",
            paragraphs=["**Все ли старые Hot Wheels ценны?**"],
            images=[],
            source_url="https://example.com/a",
        )
        assert "Все ли старые Hot Wheels ценны?" in _strong_texts(nodes)
        assert not any("**" in s for s in _flatten(nodes))

    def test_flat_paragraph_partial_span_becomes_bold(self):
        nodes = tp._build_content(
            subtitle="",
            paragraphs=["Обычный текст с **выделенным** куском."],
            images=[],
            source_url="https://example.com/a",
        )
        assert _strong_texts(nodes) == ["выделенным"]
        # The surrounding prose must stay plain — the same backwards-bleed
        # invariant the orangetrack walker regressed on.
        assert "Обычный текст с " in list(_flatten(nodes))

    def test_flat_subtitle_markers_stripped(self):
        nodes = tp._build_content(
            subtitle="Лид с **выделением**",
            paragraphs=["body"],
            images=[],
            source_url="https://example.com/a",
        )
        assert not any("**" in s for s in _flatten(nodes))
        assert any("Лид с выделением" in s for s in _flatten(nodes))

    @pytest.mark.parametrize("block,expected_bold", [
        ({"type": "paragraph", "text": "Блок с **жирным**."}, "жирным"),
        ({"type": "heading", "text": "**Заголовок**", "level": 3}, "Заголовок"),
        ({"type": "list_item", "text": "• пункт с **жирным**"}, "жирным"),
    ])
    def test_blocks_path_decodes_markers(self, block, expected_bold):
        nodes = tp._build_content_from_blocks(
            subtitle="", blocks=[block], source_url="https://example.com/a",
        )
        assert expected_bold in _strong_texts(nodes)
        assert not any("**" in s for s in _flatten(nodes))

    def test_image_caption_markers_stripped(self):
        # Captions come from a SEPARATE second LLM pass, so they carry markers
        # independently of the body.
        nodes = tp._build_content_from_blocks(
            subtitle="",
            blocks=[{"type": "image", "src": "https://x/i.jpg",
                     "caption": "Подпись **жирная**"}],
            source_url="https://example.com/a",
        )
        assert not any("**" in s for s in _flatten(nodes))
        assert any("Подпись жирная" in s for s in _flatten(nodes))

    def test_existing_runs_win_over_llm_markers(self):
        """Source formatting outranks the model's guesses.

        When a block already carries `runs` (a source that preserves markup),
        those runs are kept as-is and the stray markers are merely stripped —
        we do not let the model's opinion override the author's.
        """
        nodes = tp._build_content_from_blocks(
            subtitle="",
            blocks=[{
                "type": "paragraph",
                "text": "Настоящий жирный и **догадка** модели.",
                "runs": [{"text": "Настоящий жирный", "formats": ["bold"]}],
            }],
            source_url="https://example.com/a",
        )
        assert _strong_texts(nodes) == ["Настоящий жирный"]
        assert not any("**" in s for s in _flatten(nodes))

    def test_unbalanced_marker_is_stripped_not_published(self):
        nodes = tp._build_content(
            subtitle="",
            paragraphs=["Непарная **звёздочка тут."],
            images=[],
            source_url="https://example.com/a",
        )
        assert not any("**" in s for s in _flatten(nodes))
        assert any("Непарная звёздочка тут." in s for s in _flatten(nodes))
        # Nothing to bold — an unplaceable emphasis is dropped, not guessed at.
        assert _strong_texts(nodes) == []

    def test_page_title_markers_stripped(self):
        """The title is plain text on Telegraph AND is what the Telegram link
        preview shows — the most visible leak of all."""
        captured = {}

        def fake_api_call(method, data, session=None):
            captured.update(data)
            return {"url": "https://telegra.ph/x"}

        with patch.object(tp, "_api_call", fake_api_call):
            tp.publish_article(
                title="**Жирный заголовок**",
                paragraphs=["body"],
                images=[],
                source_url="https://example.com/a",
                access_token="tok",
            )
        assert captured["title"] == "Жирный заголовок"
        assert "**" not in captured["content"]

    def test_no_literal_markers_survive_anywhere(self):
        """Belt-and-braces sweep over a marker-heavy article: not a single `**`
        may appear in the produced node tree, whichever route built it."""
        nodes = tp._build_content(
            subtitle="**Лид**",
            paragraphs=["**Заголовок раздела**", "Текст с **выделением**.",
                        "Непарная ** тут", "Без разметки"],
            images=["https://x/1.jpg"],
            source_url="https://example.com/a",
        )
        assert not any("**" in s for s in _flatten(nodes))

    def test_text_without_markers_is_untouched(self):
        """Fast path: an article with no markers must render exactly as before
        — plain strings, no runs machinery, no behaviour change."""
        nodes = tp._build_content(
            subtitle="Обычный лид",
            paragraphs=["Просто текст."],
            images=[],
            source_url="https://example.com/a",
        )
        body = [n for n in nodes if n.get("tag") == "p"]
        assert any(n["children"] == ["Просто текст."] for n in body)


# --------------------------------------------------------------------------- #
# Task 6 — per-source image cap on the block path (AC9)                       #
# --------------------------------------------------------------------------- #


def _figures(nodes):
    return [n for n in nodes if isinstance(n, dict) and n.get("tag") == "figure"]


def _iframes(nodes):
    return [n for n in nodes if isinstance(n, dict) and n.get("tag") == "iframe"]


def _img_srcs(nodes):
    out = []
    for fig in _figures(nodes):
        for child in fig.get("children", []):
            if isinstance(child, dict) and child.get("tag") == "img":
                out.append(child["attrs"]["src"])
    return out


def _image_blocks(count, start=0):
    return [
        {"type": "image", "src": f"https://cdn.example.com/x{i}.jpg"}
        for i in range(start, start + count)
    ]


class TestImageLimitOnBlocksPath:
    """The per-source caps only ever sliced the flat ``images`` list, which
    this path ignores entirely. Measured on 14 real articles: all four lamley
    posts blow past their limit of 10 (14, 41, 48, 50)."""

    def test_image_limit_caps_total_figures_including_hero(self):
        nodes = tp._build_content_from_blocks(
            "", _image_blocks(20), None, image_limit=10,
        )
        assert len(_figures(nodes)) == 10
        # The hero is the first image and counts toward the limit.
        assert _img_srcs(nodes)[0] == "https://cdn.example.com/x0.jpg"

    def test_image_limit_drops_from_the_tail(self):
        nodes = tp._build_content_from_blocks(
            "", _image_blocks(20), None, image_limit=3,
        )
        assert _img_srcs(nodes) == [
            "https://cdn.example.com/x0.jpg",
            "https://cdn.example.com/x1.jpg",
            "https://cdn.example.com/x2.jpg",
        ]

    def test_image_limit_none_means_unlimited(self):
        """Protects hw_review and any third-party caller that does not pass
        the argument — their behaviour must not change."""
        nodes = tp._build_content_from_blocks("", _image_blocks(20), None)
        assert len(_figures(nodes)) == 20

    @pytest.mark.parametrize("limit, expected", [(0, 0), (1, 1), (5, 5)])
    def test_image_limit_boundaries_are_literal(self, limit, expected):
        """0 must never be read as "unlimited" — that silently discards the
        setting."""
        nodes = tp._build_content_from_blocks(
            "", _image_blocks(20), None, image_limit=limit,
        )
        assert len(_figures(nodes)) == expected

    def test_image_limit_ignores_video_blocks(self):
        blocks = _image_blocks(10) + [
            {"type": "video", "src": f"https://telegra.ph/embed/youtube?url=v{i}"}
            for i in range(3)
        ]
        nodes = tp._build_content_from_blocks("", blocks, None, image_limit=10)
        assert len(_figures(nodes)) == 10
        assert len(_iframes(nodes)) == 3

    def test_image_limit_is_threaded_through_preview_nodes(self):
        nodes = tp.preview_nodes(
            "T", blocks=_image_blocks(20), image_limit=4,
        )
        assert len(_figures(nodes)) == 4

    def test_image_limit_is_threaded_through_publish_article(self):
        captured = {}

        def fake_api_call(method, data, session=None):
            captured["content"] = json.loads(data["content"])
            return {"url": "https://telegra.ph/x"}

        with patch.object(tp, "_api_call", side_effect=fake_api_call), \
                patch.object(tp, "ensure_access_token", return_value="tok"):
            tp.publish_article(
                "T", blocks=_image_blocks(20), image_limit=6,
            )
        assert len(_figures(captured["content"])) == 6

    @pytest.mark.parametrize(
        "source_limit, expected", [(30, 30), (10, 10)],
        ids=["t-hunted-30", "lamley-10"],
    )
    def test_image_limit_per_source_30_and_10(self, source_limit, expected):
        """A single hardcoded 10 anywhere in the chain MUST fail this: the
        same 35-image article renders differently per source."""
        nodes = tp._build_content_from_blocks(
            "", _image_blocks(35), None, image_limit=source_limit,
        )
        assert len(_figures(nodes)) == expected


class TestImageSrcValidation:
    """The publisher is the one point every source and every path funnels
    through. The per-source pickers trust ``startswith("http")`` — which lets
    ``httpx://evil/x.jpg`` past — and autoevolution's gallery branch checks no
    scheme at all."""

    @pytest.mark.parametrize(
        "src",
        ["javascript:alert(1)", "data:image/svg+xml;base64,AAA",
         "file:///etc/passwd", "httpx://evil.example/x.jpg",
         "//cdn.example.com/x.jpg", "images/x.jpg", "", "   "],
        ids=["javascript", "data", "file", "httpx-lookalike",
             "protocol-relative", "relative", "empty", "blank"],
    )
    def test_unsafe_scheme_image_block_dropped(self, src):
        blocks = [
            {"type": "image", "src": src},
            {"type": "paragraph", "text": "Body.", "runs": []},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert _figures(nodes) == []

    def test_https_http_and_uppercase_scheme_preserved(self):
        """Negative control for the one-sided-invariant lesson of 2026-07-28:
        without it, a policy that dropped EVERYTHING would pass above."""
        for src in ("https://a.example/x.jpg", "http://a.example/x.jpg",
                    "HTTPS://a.example/x.jpg",
                    "https://a.example/x.jpg?w=1024",
                    "https://a.example/фото.jpg"):
            nodes = tp._build_content_from_blocks(
                "", [{"type": "image", "src": src}], None,
            )
            assert _img_srcs(nodes) == [src], src

    def test_unsafe_hero_promotes_next_valid_image(self):
        blocks = [
            {"type": "image", "src": "javascript:alert(1)"},
            {"type": "image", "src": "https://cdn.example.com/real.jpg"},
            {"type": "image", "src": "https://cdn.example.com/second.jpg"},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert _img_srcs(nodes) == [
            "https://cdn.example.com/real.jpg",
            "https://cdn.example.com/second.jpg",
        ]

    def test_invalid_images_do_not_consume_limit_slots(self):
        """Order of operations: drop by scheme FIRST, cap SECOND. Otherwise
        junk eats a live image's slot."""
        blocks = (
            [{"type": "image", "src": "javascript:alert(1)"}] * 3
            + _image_blocks(5)
        )
        nodes = tp._build_content_from_blocks("", blocks, None, image_limit=3)
        assert _img_srcs(nodes) == [
            "https://cdn.example.com/x0.jpg",
            "https://cdn.example.com/x1.jpg",
            "https://cdn.example.com/x2.jpg",
        ]

    def test_image_block_without_src_is_skipped_not_raised(self):
        blocks = [
            {"type": "image", "caption": "orphan"},
            {"type": "paragraph", "text": "Body.", "runs": []},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)  # must not raise
        assert _figures(nodes) == []

    def test_flat_path_drops_unsafe_src(self):
        nodes = tp._build_content(
            "", ["para"],
            ["javascript:alert(1)", "https://cdn.example.com/ok.jpg"],
            None,
        )
        assert _img_srcs(nodes) == ["https://cdn.example.com/ok.jpg"]

    def test_video_iframe_src_validated(self):
        blocks = [
            {"type": "video", "src": "javascript:alert(1)"},
            {"type": "video",
             "src": "https://telegra.ph/embed/youtube?url=https%3A%2F%2Fx"},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert [n["attrs"]["src"] for n in _iframes(nodes)] == [
            "https://telegra.ph/embed/youtube?url=https%3A%2F%2Fx"
        ]

    def test_publisher_policy_matches_preview_renderer(self):
        """Guard against a SECOND, diverging policy. The publisher keeps its
        own copy rather than importing preview_renderer (the CLI layer must
        not be a dependency of the production path) — so the copies have to be
        proven equal, not assumed equal."""
        import preview_renderer

        urls = [
            "https://a.example/x.jpg", "http://a.example/x.jpg",
            "HTTPS://a.example/x.jpg", "  https://a.example/x.jpg",
            "javascript:alert(1)", "data:image/png;base64,AAA",
            "file:///etc/passwd", "httpx://evil/x.jpg",
            "//cdn/x.jpg", "images/x.jpg", "", "   ",
            "https://a.example/x.jpg?w=1024&h=2",
        ]
        for url in urls:
            mine = tp._is_safe_media_url(url)
            theirs = bool(preview_renderer._SAFE_URL_RE.match(url.strip()))
            assert mine == theirs, f"policies disagree on {url!r}"
