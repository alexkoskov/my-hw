"""Mocked unit tests for openrouter_transcreation.

OpenRouter is OpenAI-compatible — most behavior mirrors openai_transcreation.
This file focuses on OpenRouter-specific concerns: base_url plumbing,
multi-model passthrough, env-var naming.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai  # noqa: E402
import openrouter_transcreation  # noqa: E402
from _llm_common import ClaudeOutageError, ClaudeTranscreationError  # noqa: E402


SAMPLE_ARTICLE = {
    "source_name": "lamley",
    "title": "Hot Wheels Premium F1 — first models hit shelves",
    "subtitle": "After 20 years",
    "paragraphs": [
        "First paragraph in English.",
        "Second paragraph.",
    ],
    "blocks": None,
}

SAMPLE_VALID_JSON = json.dumps({
    "title": "🏎️ Hot Wheels Premium F1 — первые модели на полках",
    "alts": ["Альт 1", "Альт 2", "Альт 3"],
    "subtitle": "Спустя 20 лет",
    "paragraphs": [
        "Первый абзац перевода с реальной длиной.",
        "Второй абзац с подробностями о релизе модели.",
    ],
    "blocks": None,
})


def _make_response(text: str, model: str = "openai/gpt-4o-mini"):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    response.model = model
    return response


def _make_client_returning(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.chat.completions.create.side_effect = response_or_exc
    else:
        client.chat.completions.create.return_value = response_or_exc
    return client


def _make_openai_error(error_class, message: str = "test"):
    err = error_class.__new__(error_class)
    err.message = message
    err.args = (message,)
    err.code = None
    err.status_code = getattr(error_class, "status_code", None)
    err.response = None
    return err


class TestSuccessPath(unittest.TestCase):
    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_happy_path_returns_valid_dict(self):
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            result = openrouter_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client,
            )
        self.assertEqual(len(result["paragraphs"]), 2)
        self.assertTrue(result["title"].startswith("🏎️"))

    def test_model_param_forwarded(self):
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            openrouter_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client, model="anthropic/claude-haiku-4-5",
            )
        called_with = client.chat.completions.create.call_args.kwargs
        self.assertEqual(called_with["model"], "anthropic/claude-haiku-4-5")

    def test_model_from_env_var(self):
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            with patch.dict(os.environ, {"OPENROUTER_MODEL": "google/gemini-2.5-flash"}):
                openrouter_transcreation.transcreate_via_claude(
                    SAMPLE_ARTICLE, client=client,
                )
        called_with = client.chat.completions.create.call_args.kwargs
        self.assertEqual(called_with["model"], "google/gemini-2.5-flash")


class TestEnglishGuard(unittest.TestCase):
    """Reject responses where paragraphs came back in English (model
    silently skipped the translation step). The article must enter the
    3-strike retry flow rather than surface in the channel as EN."""

    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_en_only_paragraphs_rejected(self):
        en_payload = json.loads(SAMPLE_VALID_JSON)
        en_payload["paragraphs"] = [
            "First English paragraph that is long enough to pass the floor.",
            "Second English paragraph that is also long.",
        ]
        client = _make_client_returning(_make_response(json.dumps(en_payload)))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            with self.assertRaises(ClaudeTranscreationError) as ctx:
                openrouter_transcreation.transcreate_via_claude(
                    SAMPLE_ARTICLE, client=client,
                )
        self.assertIn("English", str(ctx.exception))

    def test_brand_heavy_russian_paragraphs_accepted(self):
        """Real-world: Russian translation legitimately keeps brand names
        in Latin (Hot Wheels, Nissan GT-R). 30% threshold must accept this."""
        ru_payload = json.loads(SAMPLE_VALID_JSON)
        ru_payload["paragraphs"] = [
            "Hot Wheels представил новую модель Nissan GT-R на выставке.",
            "В новой коллекции Bugatti и Porsche будут эксклюзивами.",
        ]
        client = _make_client_returning(_make_response(json.dumps(ru_payload)))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            result = openrouter_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client,
            )
        self.assertEqual(len(result["paragraphs"]), 2)

    def test_helper_threshold_boundary(self):
        # 50/50 mix should pass (≥30% Cyrillic)
        self.assertTrue(openrouter_transcreation._is_mostly_russian(
            ["Hello мир hello мир"],
        ))
        # All English → fail
        self.assertFalse(openrouter_transcreation._is_mostly_russian(
            ["Hello world"],
        ))
        # Empty paragraphs → True (other validators handle empties)
        self.assertTrue(openrouter_transcreation._is_mostly_russian([]))
        self.assertTrue(openrouter_transcreation._is_mostly_russian(["  "]))


class TestExceptionClassification(unittest.TestCase):
    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_rate_limit_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.RateLimitError))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_auth_error_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.AuthenticationError))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_bad_request_is_per_article(self):
        client = _make_client_returning(_make_openai_error(openai.BadRequestError))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)


class TestClientLifecycle(unittest.TestCase):
    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}
        openrouter_transcreation._DEFAULT_CLIENT = None

    def test_client_uses_openrouter_base_url(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch.object(openai, "OpenAI") as mock_openai:
                openrouter_transcreation._get_default_client()
                mock_openai.assert_called_once()
                kwargs = mock_openai.call_args.kwargs
                self.assertEqual(kwargs["api_key"], "test-key")
                self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")

    def test_client_respects_custom_base_url_override(self):
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_BASE_URL": "https://custom.example/v1",
        }):
            with patch.object(openai, "OpenAI") as mock_openai:
                openrouter_transcreation._get_default_client()
                kwargs = mock_openai.call_args.kwargs
                self.assertEqual(kwargs["base_url"], "https://custom.example/v1")

    def test_missing_api_key_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ.pop("OPEN_ROUTER_API_KEY", None)
            with self.assertRaises(RuntimeError):
                openrouter_transcreation._get_default_client()

    def test_alias_env_var_accepted(self):
        """OPEN_ROUTER_API_KEY (with underscore) should also work."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ["OPEN_ROUTER_API_KEY"] = "test-alias-key"
            with patch.object(openai, "OpenAI") as mock_openai:
                openrouter_transcreation._get_default_client()
                kwargs = mock_openai.call_args.kwargs
                self.assertEqual(kwargs["api_key"], "test-alias-key")
            # Clean up
            os.environ.pop("OPEN_ROUTER_API_KEY", None)


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}
        openrouter_transcreation._DEFAULT_CLIENT = None

    def test_health_check_false_when_prompt_missing(self):
        with patch.object(openrouter_transcreation, "_load_prompt",
                          side_effect=FileNotFoundError("missing")):
            self.assertFalse(openrouter_transcreation.health_check())

    def test_health_check_false_when_no_api_key(self):
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENROUTER_API_KEY", None)
                os.environ.pop("OPEN_ROUTER_API_KEY", None)
                self.assertFalse(openrouter_transcreation.health_check())

    def test_health_check_true_on_successful_probe(self):
        client = _make_client_returning(_make_response("pong"))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            self.assertTrue(openrouter_transcreation.health_check(client=client))


class TestVariantBPlus(unittest.TestCase):
    """Variant B+: second-pass translation of EN block captions/text after
    main response returned ``blocks: null``."""

    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_empty_blocks_short_circuit(self):
        client = _make_client_returning(_make_response('{"translations": []}'))
        out = openrouter_transcreation._translate_block_strings([], client, "m")
        self.assertEqual(out, [])
        client.chat.completions.create.assert_not_called()

    def test_blocks_with_no_text_or_caption_short_circuit(self):
        blocks = [{"type": "image", "src": "https://x/a.jpg"},
                  {"type": "video", "src": "https://x/v.mp4"}]
        client = MagicMock()
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out, blocks)
        client.chat.completions.create.assert_not_called()

    def test_successful_translation_spliced_back(self):
        blocks = [
            {"type": "image", "src": "https://x/a.jpg", "caption": "Front view"},
            {"type": "p", "text": "More cars coming."},
            {"type": "image", "src": "https://x/b.jpg", "caption": "Rear view"},
        ]
        translations_json = json.dumps({
            "translations": ["Вид спереди", "Скоро ещё машины.", "Вид сзади"],
        })
        client = _make_client_returning(_make_response(translations_json))
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["caption"], "Вид спереди")
        self.assertEqual(out[1]["text"], "Скоро ещё машины.")
        self.assertEqual(out[2]["caption"], "Вид сзади")
        # src URLs preserved
        self.assertEqual(out[0]["src"], "https://x/a.jpg")
        self.assertEqual(out[2]["src"], "https://x/b.jpg")
        # original blocks not mutated
        self.assertEqual(blocks[0]["caption"], "Front view")

    def test_count_mismatch_keeps_en_blocks(self):
        blocks = [{"type": "image", "caption": "Front"},
                  {"type": "image", "caption": "Rear"}]
        # Returns only 1 translation for 2 items
        translations_json = json.dumps({"translations": ["Спереди"]})
        client = _make_client_returning(_make_response(translations_json))
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["caption"], "Front")
        self.assertEqual(out[1]["caption"], "Rear")

    def test_api_exception_keeps_en_blocks(self):
        blocks = [{"type": "image", "caption": "Front"}]
        client = _make_client_returning(
            _make_openai_error(openai.RateLimitError, "boom"),
        )
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["caption"], "Front")

    def test_invalid_json_keeps_en_blocks(self):
        blocks = [{"type": "image", "caption": "Front"}]
        client = _make_client_returning(_make_response("not json at all"))
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["caption"], "Front")

    def test_translations_not_list_keeps_en_blocks(self):
        blocks = [{"type": "image", "caption": "Front"}]
        client = _make_client_returning(_make_response('{"translations": "oops"}'))
        out = openrouter_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["caption"], "Front")

    def test_patch_text_with_ru_paragraphs_splices_in_order(self):
        """``_patch_text_with_ru_paragraphs`` must replace text in
        lead/paragraph/heading blocks in occurrence order with the RU
        paragraphs from the main response, leaving image/video blocks
        and their captions untouched."""
        blocks_in = [
            {"type": "lead", "text": "Lead in EN."},
            {"type": "image", "src": "https://x/a.jpg", "caption": "Front EN"},
            {"type": "paragraph", "text": "Body 1 EN."},
            {"type": "paragraph", "text": "Body 2 EN."},
            {"type": "video", "src": "https://x/v"},  # no text
            {"type": "image", "src": "https://x/b.jpg", "caption": "Rear EN"},
        ]
        ru_paragraphs = ["Лид RU.", "Тело 1 RU.", "Тело 2 RU."]
        out = openrouter_transcreation._patch_text_with_ru_paragraphs(
            blocks_in, ru_paragraphs,
        )
        self.assertEqual(out[0]["text"], "Лид RU.")
        self.assertEqual(out[2]["text"], "Тело 1 RU.")
        self.assertEqual(out[3]["text"], "Тело 2 RU.")
        # Image/video blocks left untouched
        self.assertEqual(out[1]["caption"], "Front EN")
        self.assertEqual(out[5]["caption"], "Rear EN")
        self.assertEqual(out[4]["src"], "https://x/v")
        # Original blocks not mutated
        self.assertEqual(blocks_in[0]["text"], "Lead in EN.")

    def test_full_flow_keeps_paragraphs_ru_when_second_pass_fails(self):
        """The exact regression that hit production 2026-04-29: long
        unboxing article, main GPT call returns RU paragraphs but no
        blocks; second-pass exception → blocks must still publish with
        RU paragraph text (spliced from main response). Captions stay
        EN as a controlled degradation."""
        article = dict(SAMPLE_ARTICLE)
        article["blocks"] = [
            {"type": "lead", "text": "Lead body EN."},
            {"type": "paragraph", "text": "Body para 1 EN."},
            {"type": "image", "src": "https://x/img.jpg", "caption": "Cap EN"},
        ]
        main_payload = json.loads(SAMPLE_VALID_JSON)
        main_payload["blocks"] = None
        # Different paragraphs from SAMPLE_VALID_JSON's defaults — verify
        # they actually get spliced in.
        main_payload["paragraphs"] = [
            "Лид-абзац на русском с длиной достаточной.",
            "Второй абзац перевода с подробностями.",
        ]
        main_response = _make_response(json.dumps(main_payload))
        # Second-pass call raises (simulates B+ failure)
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            main_response,
            _make_openai_error(openai.RateLimitError, "boom"),
        ]
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            result = openrouter_transcreation.transcreate_via_claude(
                article, client=client,
            )
        # Paragraphs must be RU (spliced from main response).
        self.assertEqual(
            result["blocks"][0]["text"],
            "Лид-абзац на русском с длиной достаточной.",
        )
        self.assertEqual(
            result["blocks"][1]["text"],
            "Второй абзац перевода с подробностями.",
        )
        # Caption stays EN — controlled degradation when B+ fails.
        self.assertEqual(result["blocks"][2]["caption"], "Cap EN")

    def test_full_flow_main_null_blocks_triggers_second_pass(self):
        """End-to-end: main response returns blocks=null with mismatched count
        → variant B fills with EN blocks → variant B+ second-pass translates."""
        article_with_blocks = dict(SAMPLE_ARTICLE)
        article_with_blocks["blocks"] = [
            {"type": "image", "src": "https://x/a.jpg", "caption": "Hot Wheels GT-R"},
            {"type": "image", "src": "https://x/b.jpg", "caption": "Bugatti reveal"},
        ]
        # Main response: paragraphs OK, blocks=null
        main_payload = json.loads(SAMPLE_VALID_JSON)
        main_payload["blocks"] = None
        main_response = _make_response(json.dumps(main_payload))
        # Second-pass response: translations matching items
        second_response = _make_response(json.dumps({
            "translations": ["Hot Wheels GT-R", "Релиз Bugatti"],
        }))
        client = MagicMock()
        client.chat.completions.create.side_effect = [main_response, second_response]
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="STUB"):
            result = openrouter_transcreation.transcreate_via_claude(
                article_with_blocks, client=client,
            )
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(len(result["blocks"]), 2)
        self.assertEqual(result["blocks"][1]["caption"], "Релиз Bugatti")
        # src URLs preserved
        self.assertEqual(result["blocks"][0]["src"], "https://x/a.jpg")


if __name__ == "__main__":
    unittest.main()
