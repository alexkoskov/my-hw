#!/usr/bin/env python3
"""Unit tests for telegraph_publisher."""

import json
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
        nodes = tp._build_content("", ["para one"], ["img1.jpg"], None)
        assert nodes[0]["tag"] == "figure"
        assert nodes[0]["children"][0]["attrs"]["src"] == "img1.jpg"
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
        images = ["hero.jpg", "mid.jpg", "extra.jpg"]
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
            ["hero.jpg"],
            "http://src",
        )
        # [figure(hero), p(italic "💬 «subtitle»"), hr, p(body), p(footer)]
        tags = [n["tag"] for n in nodes]
        assert tags == ["figure", "p", "hr", "p", "p"]
        # Decorated subtitle paragraph
        subtitle_p = nodes[1]
        italic_child = subtitle_p["children"][0]
        assert italic_child["tag"] == "i"
        assert italic_child["children"] == ["💬 «Forgive me father»"]

    def test_empty_subtitle_skips_lead_and_hr(self):
        nodes = tp._build_content("", ["body"], ["hero.jpg"], None)
        # No hr when subtitle is empty
        assert all(n["tag"] != "hr" for n in nodes)


class TestBuildContentFromBlocks:
    def test_image_caption_becomes_figcaption(self):
        blocks = [
            {"type": "image", "src": "hero.jpg", "caption": "Photo: Mattel"},
            {"type": "paragraph", "text": "Body."},
            {"type": "image", "src": "inline.jpg", "caption": "Photo: Lamley Group"},
        ]
        nodes = tp._build_content_from_blocks("", blocks, None)
        # Hero figure has figcaption
        hero_children = nodes[0]["children"]
        assert hero_children[0]["tag"] == "img"
        assert hero_children[0]["attrs"]["src"] == "hero.jpg"
        assert hero_children[1] == {"tag": "figcaption", "children": ["Photo: Mattel"]}
        # Inline figure also has its caption
        inline_figure = nodes[2]
        assert inline_figure["tag"] == "figure"
        assert inline_figure["children"][1] == {"tag": "figcaption", "children": ["Photo: Lamley Group"]}

    def test_image_without_caption_has_no_figcaption(self):
        blocks = [{"type": "image", "src": "hero.jpg"}]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes[0]["children"] == [{"tag": "img", "attrs": {"src": "hero.jpg"}}]

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
            {"type": "image", "src": "img1.jpg"},
            {"type": "paragraph", "text": "p2"},
            {"type": "image", "src": "img2.jpg"},
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
            images=["img1.jpg"],
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
            {"type": "image", "src": "hero.jpg", "caption": "cap"},
            {"type": "paragraph", "text": "body"},
        ]
        subtitle = "sub"
        source_url = "https://src"
        preview = tp.preview_nodes(
            title="t",
            paragraphs=["ignored"],
            images=["ignored.jpg"],
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
        # Nested <img> with the hero src
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
                {"type": "image", "src": "hero.jpg", "caption": "cap"},
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
