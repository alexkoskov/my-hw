"""Mocked unit tests for gemini_transcreation.

Mirror the structure of test_claude_transcreation.py but use google-genai
exception types and response shapes.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _llm_common  # noqa: E402
import gemini_transcreation  # noqa: E402
from _llm_common import ClaudeOutageError, ClaudeTranscreationError  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402


SAMPLE_ARTICLE = {
    "source_name": "lamley",
    "title": "Hot Wheels Premium F1 — first models hit shelves",
    "subtitle": "After 20 years",
    "paragraphs": [
        "First paragraph in English about the F1 release.",
        "Second paragraph with more details.",
    ],
    "blocks": None,
}

SAMPLE_VALID_JSON = json.dumps({
    "title": "🏎️ Hot Wheels Premium F1 — первые модели на полках",
    "alts": ["Альт 1", "Альт 2", "Альт 3"],
    "subtitle": "Спустя 20 лет ожидания",
    "paragraphs": [
        "Первый абзац на русском про релиз F1.",
        "Второй абзац с деталями.",
    ],
    "blocks": None,
})


def _make_response(text: str, input_tokens: int = 100, output_tokens: int = 50):
    """Build a mock Gemini response with expected attribute shape."""
    response = MagicMock()
    response.text = text
    response.usage_metadata = MagicMock(
        prompt_token_count=input_tokens,
        candidates_token_count=output_tokens,
    )
    return response


def _make_client_returning(response_or_exc):
    """Build a mock client whose models.generate_content returns/raises the given value."""
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.models.generate_content.side_effect = response_or_exc
    else:
        client.models.generate_content.return_value = response_or_exc
    return client


def _make_client_error(status_code: int, message: str = "test error"):
    """Build a real google-genai ``ClientError`` with the given status code.

    Uses the actual constructor ``(code, response_json, response=None)`` — no
    HTTP layer needed. It must NOT be faked with ``__new__`` + hand-set
    attributes: a real ``ClientError`` exposes ``.code`` and has **no**
    ``.status_code`` (google-genai 1.73.1), so a double that invents
    ``.status_code`` would keep these tests green while production sent every
    4xx down the wrong branch.
    """
    return genai_errors.ClientError(
        status_code, {"error": {"code": status_code, "message": message}}, None
    )


def _make_server_error(status_code: int = 500, message: str = "server error"):
    err = genai_errors.ServerError.__new__(genai_errors.ServerError)
    err.code = status_code
    err.args = (message,)
    return err


class TestSuccessPath(unittest.TestCase):
    def setUp(self):
        gemini_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_happy_path_returns_valid_dict(self):
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="STUB PROMPT"):
            result = gemini_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client,
            )
        self.assertIn("title", result)
        self.assertTrue(result["title"].startswith("🏎️"))
        self.assertEqual(len(result["alts"]), 3)
        self.assertEqual(len(result["paragraphs"]), 2)


class TestExceptionClassification(unittest.TestCase):
    def setUp(self):
        gemini_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_rate_limit_429_is_outage(self):
        client = _make_client_returning(_make_client_error(429, "quota exceeded"))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_auth_401_is_outage(self):
        client = _make_client_returning(_make_client_error(401, "unauth"))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_bad_request_400_is_per_article(self):
        client = _make_client_returning(_make_client_error(400, "invalid arg"))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_server_error_is_outage(self):
        client = _make_client_returning(_make_server_error(500))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_account_level_codes_are_outage(self):
        """402/407/408 — same shared set the SDK-based engines use. google-genai
        raises one ``ClientError`` for every 4xx, so the split is by code."""
        for code in (402, 407, 408):
            with self.subTest(code=code):
                client = _make_client_returning(_make_client_error(code, "account-level"))
                with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeOutageError):
                        gemini_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )

    def test_other_client_codes_are_per_article(self):
        """Default flipped 2026-08-04: unlisted 4xx are per-article, matching the
        other three engines. 413/451 are about THIS article — as an outage they
        would pin the row to the queue head and stop the channel."""
        for code in (413, 414, 451):
            with self.subTest(code=code):
                client = _make_client_returning(_make_client_error(code, "article-level"))
                with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeTranscreationError):
                        gemini_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )

    def test_named_class_codes_stay_outage(self):
        """Guard for the flipped default. Every code the OpenAI/Anthropic SDKs
        give a dedicated class to must stay on the outage side here too, or the
        engines stop agreeing: 404 bad model name, 409 conflict (their
        ``ConflictError``), 403 no access.

        499 is Gemini-only — «client cancelled the request», the google-genai
        twin of ``APITimeoutError``. It was an outage before the default flip
        and must not be swept into per-article by it.
        """
        for code in (403, 404, 409, 499):
            with self.subTest(code=code):
                client = _make_client_returning(_make_client_error(code, "api-level"))
                with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeOutageError):
                        gemini_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )

    def test_classifier_handles_already_wrapped(self):
        """If the SDK somehow returns our own exception type, do not re-wrap."""
        original = ClaudeOutageError("already wrapped")
        result = gemini_transcreation._classify_exception(original)
        self.assertIs(result, original)


class TestResponseValidation(unittest.TestCase):
    def setUp(self):
        gemini_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_malformed_json_is_per_article(self):
        client = _make_client_returning(_make_response("not json {{"))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_paragraph_count_mismatch_is_accepted_with_warning(self):
        """Count divergence (model merged 2 paragraphs into 1) is accepted
        per the relaxed contract — Telegraph renders <p> nodes without
        a structural 1:1 mapping requirement. A WARNING is logged for
        operator visibility, but the call returns successfully. Paragraph
        kept long enough (>=30 chars) so the separate too-short floor is
        not hit."""
        bad_json = json.dumps({
            "title": "🚀 Title",
            "alts": ["a", "b"],
            "subtitle": "sub",
            "paragraphs": [
                "Один абзац, в который модель сложила оба исходных абзаца ради читабельности.",
            ],
            "blocks": None,
        })
        client = _make_client_returning(_make_response(bad_json))
        import logging
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertLogs('gemini_transcreation', level='WARNING') as cm:
                out = gemini_transcreation.transcreate_via_claude(
                    SAMPLE_ARTICLE, client=client,
                )
        self.assertEqual(len(out["paragraphs"]), 1)
        self.assertTrue(any(
            'paragraph count diverges' in r.message and 'expected 2' in r.message
            for r in cm.records
        ))

    def test_missing_title_is_per_article(self):
        bad_json = json.dumps({
            "title": "",
            "alts": ["a", "b"],
            "subtitle": "sub",
            "paragraphs": ["p1", "p2"],
            "blocks": None,
        })
        client = _make_client_returning(_make_response(bad_json))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)


class TestEmojiSafetyNet(unittest.TestCase):
    def test_existing_emoji_preserved(self):
        result = gemini_transcreation._apply_emoji_safety_net("🏆 Title with emoji")
        self.assertEqual(result, "🏆 Title with emoji")

    def test_missing_emoji_added_by_keyword(self):
        result = gemini_transcreation._apply_emoji_safety_net("Гонки на скорости")
        self.assertTrue(result.startswith("🏎️"))

    def test_unknown_falls_back_to_fire(self):
        result = gemini_transcreation._apply_emoji_safety_net("Случайный заголовок")
        self.assertTrue(result.startswith("🔥"))


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        gemini_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}
        gemini_transcreation._DEFAULT_CLIENT = None

    def test_health_check_false_when_prompt_missing(self):
        with patch.object(gemini_transcreation, "_load_prompt", side_effect=FileNotFoundError("missing")):
            self.assertFalse(gemini_transcreation.health_check())

    def test_health_check_false_when_no_api_key(self):
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GEMINI_API_KEY", None)
                self.assertFalse(gemini_transcreation.health_check())

    def test_health_check_true_on_successful_probe(self):
        client = _make_client_returning(_make_response("pong"))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            self.assertTrue(gemini_transcreation.health_check(client=client))

    def test_health_check_false_on_client_error(self):
        client = _make_client_returning(_make_server_error(500))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            self.assertFalse(gemini_transcreation.health_check(client=client))


class TestLLMTranscreationDispatcher(unittest.TestCase):
    """Verify dispatcher selects correct engine: explicit LLM_PROVIDER first,
    then auto-select by API-key presence in priority order
    openai → claude → gemini."""

    _ENV_KEYS = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")

    def setUp(self):
        # Snapshot then clear all relevant env vars so each test starts clean.
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        # Restore env exactly to its pre-test state.
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        import llm_transcreation
        importlib.reload(llm_transcreation)

    def _reload_and_get_engine(self):
        import importlib
        import llm_transcreation
        importlib.reload(llm_transcreation)
        return llm_transcreation._engine.__name__

    def test_no_keys_no_provider_defaults_to_claude(self):
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_only_gemini_key_auto_selects_gemini(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.assertEqual(self._reload_and_get_engine(), "gemini_transcreation")

    def test_only_anthropic_key_auto_selects_claude(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_only_openai_key_auto_selects_openai(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        self.assertEqual(self._reload_and_get_engine(), "openai_transcreation")

    def test_all_three_keys_priority_picks_openai(self):
        os.environ["OPENAI_API_KEY"] = "a"
        os.environ["ANTHROPIC_API_KEY"] = "b"
        os.environ["GEMINI_API_KEY"] = "c"
        self.assertEqual(self._reload_and_get_engine(), "openai_transcreation")

    def test_claude_and_gemini_priority_picks_claude(self):
        os.environ["ANTHROPIC_API_KEY"] = "a"
        os.environ["GEMINI_API_KEY"] = "b"
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_explicit_provider_overrides_auto_selection(self):
        # Only Gemini key present, but LLM_PROVIDER=claude → use claude
        os.environ["GEMINI_API_KEY"] = "a"
        os.environ["LLM_PROVIDER"] = "claude"
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_explicit_provider_gemini(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        self.assertEqual(self._reload_and_get_engine(), "gemini_transcreation")

    def test_explicit_provider_openai(self):
        os.environ["LLM_PROVIDER"] = "openai"
        self.assertEqual(self._reload_and_get_engine(), "openai_transcreation")

    def test_explicit_provider_openrouter(self):
        os.environ["LLM_PROVIDER"] = "openrouter"
        self.assertEqual(self._reload_and_get_engine(), "openrouter_transcreation")

    def test_only_openrouter_key_auto_selects_openrouter(self):
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        self.assertEqual(self._reload_and_get_engine(), "openrouter_transcreation")

    def test_openrouter_lowest_priority_when_others_present(self):
        os.environ["OPENROUTER_API_KEY"] = "a"
        os.environ["GEMINI_API_KEY"] = "b"
        # gemini wins over openrouter (gemini is direct provider, higher priority)
        self.assertEqual(self._reload_and_get_engine(), "gemini_transcreation")

    def test_unknown_provider_falls_back_to_auto_selection(self):
        # Unknown provider + Gemini key → gemini (auto-selection kicks in)
        os.environ["LLM_PROVIDER"] = "garbage"
        os.environ["GEMINI_API_KEY"] = "a"
        self.assertEqual(self._reload_and_get_engine(), "gemini_transcreation")

    def test_unknown_provider_no_keys_defaults_to_claude(self):
        os.environ["LLM_PROVIDER"] = "garbage"
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_empty_string_key_treated_as_unset(self):
        os.environ["GEMINI_API_KEY"] = ""
        os.environ["ANTHROPIC_API_KEY"] = "  "  # whitespace also counts as unset
        self.assertEqual(self._reload_and_get_engine(), "claude_transcreation")

    def test_exception_class_identity_shared(self):
        """ClaudeOutageError from each engine IS the one in _llm_common."""
        from _llm_common import ClaudeOutageError as CommonOutage
        import claude_transcreation
        import gemini_transcreation
        import openai_transcreation
        import openrouter_transcreation
        import llm_transcreation
        self.assertIs(claude_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(gemini_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(openai_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(openrouter_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(llm_transcreation.ClaudeOutageError, CommonOutage)


class TestVariantBPlus(unittest.TestCase):
    """Second-pass block-string translation. Duplicated per engine on
    purpose: ``_translate_block_strings`` is physically copied into all
    four modules, so a test against one proves nothing about the other
    three — that is exactly how the gemini classifier bug survived."""

    def test_list_item_text_skipped_by_second_pass(self):
        """``list_item`` text is already RU (the SHARED tuple lists it, so
        ``_patch_text_with_ru_paragraphs`` fills it), so the second pass
        must skip it instead of re-translating RU→RU."""
        blocks = [
            {"type": "list_item", "text": "Уже переведённый пункт списка."},
            {"type": "image", "src": "https://x/a.jpg", "caption": "EN cap"},
        ]
        translations_json = json.dumps({"translations": ["RU cap"]})
        client = _make_client_returning(_make_response(translations_json))
        out = gemini_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["text"], "Уже переведённый пункт списка.")
        self.assertEqual(out[1]["caption"], "RU cap")
        contents = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("EN cap", contents)
        self.assertNotIn("Уже переведённый пункт списка.", contents)

    def test_patched_types_include_list_item_and_match_shared(self):
        """Anti-drift guard: compare the whole tuple with the shared one,
        so a type added to ``_llm_common`` and forgotten here also fails."""
        self.assertEqual(
            gemini_transcreation._PATCHED_TEXT_BLOCK_TYPES,
            _llm_common._PATCHED_TEXT_BLOCK_TYPES,
        )


if __name__ == "__main__":
    unittest.main()
