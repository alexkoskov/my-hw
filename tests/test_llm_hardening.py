"""Publication-boundary regressions for structured LLM output and secrets."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import _llm_common as common
import claude_transcreation
import gemini_transcreation
import openai_transcreation
import openrouter_transcreation
import telegraph_publisher


ENGINES = [
    claude_transcreation,
    gemini_transcreation,
    openai_transcreation,
    openrouter_transcreation,
]
RU = [
    "Первый русский абзац сохраняет сведения о новой машинке.",
    "Второй русский абзац содержит остальные подробности релиза.",
]


def payload(paragraphs=RU, blocks=None):
    return {
        "title": "🏎️ Коллекционеры встречают новый релиз",
        "alts": ["Первый заголовок", "Второй заголовок"],
        "subtitle": "Подробности коллекции",
        "paragraphs": paragraphs,
        "blocks": blocks,
    }


def source_article():
    return {
        "source_name": "test",
        "title": "New collectible",
        "paragraphs": ["Original introduction.", "Original second paragraph."],
        "images": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        "blocks": [
            {"type": "paragraph", "text": "Original introduction."},
            {"type": "image", "src": "https://example.com/a.jpg"},
            {"type": "paragraph", "text": "Original second paragraph."},
            {"type": "image", "src": "https://example.com/b.jpg"},
        ],
    }


def invoke(engine, article, body, monkeypatch, *, caption_failure=False):
    text = json.dumps(body, ensure_ascii=False)
    response = SimpleNamespace(
        text=text,
        content=[SimpleNamespace(text=text)],
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=None,
        usage_metadata=None,
        model="test-model",
        error=None,
    )
    client = MagicMock()
    client.messages.create.return_value = response
    client.models.generate_content.return_value = response
    client.chat.completions.create.return_value = response
    if caption_failure:
        for method in (client.messages.create, client.models.generate_content,
                       client.chat.completions.create):
            method.side_effect = [response, RuntimeError("caption service unavailable")]
    monkeypatch.setattr(engine, "_load_prompt", lambda _path: "TEST PROMPT")
    return engine.transcreate_via_claude(article, client=client)


@pytest.mark.parametrize("blocks", ["not blocks", {"bad": "shape"}, [None] * 4,
                                    [{"type": "paragraph", "text": "short"}]])
def test_rejected_model_blocks_are_actually_cleared(blocks):
    parsed = common._parse_response(json.dumps(payload(blocks=blocks)), 2, 4)
    assert parsed["blocks"] is None


@pytest.mark.parametrize("engine", ENGINES, ids=lambda module: module.__name__)
@pytest.mark.parametrize("blocks", [
    [{"type": "paragraph", "text": "truncated model response"}],
    [{"type": "paragraph", "text": "hallucinated model response"}] * 4,
])
def test_render_uses_all_translated_text_and_original_gallery(engine, blocks, monkeypatch):
    article = source_article()
    result = invoke(engine, article, payload(blocks=blocks), monkeypatch)
    nodes = telegraph_publisher.preview_nodes(
        **{key: result[key] for key in ("title", "paragraphs", "subtitle", "blocks")},
        images=article["images"],
    )
    rendered = json.dumps(nodes, ensure_ascii=False)
    assert all(paragraph in rendered for paragraph in RU)
    assert all(url in rendered for url in article["images"])
    assert "Original" not in rendered
    assert "model response" not in rendered


@pytest.mark.parametrize("engine", ENGINES, ids=lambda module: module.__name__)
@pytest.mark.parametrize("paragraphs", [RU[:1], RU + ["Третий русский абзац."],
                                         [RU[0], "  "]])
def test_unalignable_structured_translation_is_not_published(engine, paragraphs, monkeypatch):
    with pytest.raises(common.ClaudeTranscreationError):
        invoke(engine, source_article(), payload(paragraphs=paragraphs), monkeypatch)


@pytest.mark.parametrize("engine", ENGINES, ids=lambda module: module.__name__)
@pytest.mark.parametrize("caption_failure", [False, True])
def test_single_intro_gallery_can_drop_intro_without_leaving_english(
    engine, caption_failure, monkeypatch,
):
    article = source_article()
    article["paragraphs"] = article["paragraphs"][:1]
    article["blocks"] = [block for index, block in enumerate(article["blocks"]) if index != 2]
    article["blocks"][1]["caption"] = "Front view"
    result = invoke(engine, article, payload(paragraphs=[]), monkeypatch,
                    caption_failure=caption_failure)
    nodes = telegraph_publisher.preview_nodes(
        title=result["title"], paragraphs=result["paragraphs"],
        blocks=result["blocks"], images=article["images"],
    )
    rendered = json.dumps(nodes, ensure_ascii=False)
    assert all(url in rendered for url in article["images"])
    assert "Original introduction" not in rendered
    assert result["paragraphs"] == []


def test_empty_intro_without_visual_payload_is_not_a_gallery_exception():
    with pytest.raises(common.ClaudeTranscreationError):
        common._patch_text_with_ru_paragraphs(
            [{"type": "paragraph", "text": "Original introduction."}], [],
        )


@pytest.mark.parametrize("engine", ENGINES, ids=lambda module: module.__name__)
def test_flat_segmentation_remains_tolerant_and_ignores_unsolicited_structure(engine, monkeypatch):
    article = source_article()
    article["blocks"] = None
    paragraphs = RU + ["Третий абзац перевода сохраняется целиком."]
    result = invoke(engine, article, payload(paragraphs, blocks=[{"type": "paragraph"}]),
                    monkeypatch)
    nodes = telegraph_publisher.preview_nodes(
        title=result["title"], paragraphs=result["paragraphs"], blocks=result["blocks"],
    )
    rendered = json.dumps(nodes, ensure_ascii=False)
    assert all(paragraph in rendered for paragraph in paragraphs)


@pytest.mark.parametrize("code", [401, 403, 404, 409, 429, 500, 503, 599])
@pytest.mark.parametrize("as_string", [False, True])
def test_http200_known_outage_envelope_is_not_an_article_strike(code, as_string, monkeypatch):
    response = SimpleNamespace(error={"code": str(code) if as_string else code,
                                      "message": "Provider unavailable"})
    client = MagicMock()
    client.chat.completions.create.return_value = response
    monkeypatch.setattr(openrouter_transcreation, "_load_prompt", lambda _path: "TEST PROMPT")
    with pytest.raises(common.ClaudeOutageError):
        openrouter_transcreation.transcreate_via_claude(source_article(), client=client)


@pytest.mark.parametrize("code", [400, 413, 422, 451, None, True, "unknown"])
def test_unknown_or_article_envelope_preserves_conservative_policy(code):
    error = SimpleNamespace(error={"code": code, "message": "Bad article"})
    parsed_code, message = common._error_envelope(error)
    assert isinstance(common._classify_error_envelope(parsed_code, message, "Test"),
                      common.ClaudeTranscreationError)
