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
        last = nodes[-1]
        assert last["tag"] == "p"
        # source link paragraph has "Источник: " italic + link
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
        # [figure(hero), p(italic "💬 «subtitle»"), hr, p(body), p(source)]
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

    def test_paragraph_runs_emit_inline_links(self):
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
        assert p_node["tag"] == "p"
        assert p_node["children"] == [
            "See ",
            {"tag": "a", "attrs": {"href": "https://mattel.com/rlc"}, "children": ["Red Line Club"]},
            " for more.",
        ]

    def test_lead_runs_wrap_inline_children_in_bold(self):
        blocks = [{
            "type": "lead",
            "runs": [
                {"text": "Visit "},
                {"text": "Hot Wheels", "href": "https://hotwheels.com/"},
                {"text": " site."},
            ],
        }]
        nodes = tp._build_content_from_blocks("", blocks, None)
        assert nodes[0]["tag"] == "p"
        bold = nodes[0]["children"][0]
        assert bold["tag"] == "b"
        assert bold["children"][1] == {
            "tag": "a",
            "attrs": {"href": "https://hotwheels.com/"},
            "children": ["Hot Wheels"],
        }

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
        monkeypatch.setenv(tp.ENV_TOKEN_KEY, "tok")
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"url": "https://telegra.ph/Test-04-20"}}
        )
        url = tp.publish_article(
            title="Заголовок",
            paragraphs=["Абзац 1.", "Абзац 2."],
            images=["img1.jpg"],
            source_url="http://source",
            session=session,
        )
        assert url == "https://telegra.ph/Test-04-20"
        call_data = session.post.call_args[1]["data"]
        assert call_data["title"] == "Заголовок"
        assert call_data["access_token"] == "tok"
        content = json.loads(call_data["content"])
        assert content[0]["tag"] == "figure"
        # Source link appended
        assert content[-1]["children"][1]["attrs"]["href"] == "http://source"

    def test_no_token_raises(self, monkeypatch):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        with pytest.raises(tp.TelegraphError, match="not set"):
            tp.publish_article("t", ["p"], [])

    def test_explicit_token_overrides_env(self, monkeypatch):
        monkeypatch.delenv(tp.ENV_TOKEN_KEY, raising=False)
        session = MagicMock()
        session.post.return_value = _make_response(
            {"ok": True, "result": {"url": "https://telegra.ph/X"}}
        )
        tp.publish_article(
            title="T", paragraphs=["p"], images=[],
            access_token="explicit", session=session,
        )
        assert session.post.call_args[1]["data"]["access_token"] == "explicit"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
