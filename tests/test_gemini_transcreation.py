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
    """Build a google-genai ClientError with the given status code."""
    # ClientError accepts (code, response_json, response) per signature; minimum:
    # use a mock with the .code attribute that _classify_exception reads.
    err = genai_errors.ClientError.__new__(genai_errors.ClientError)
    err.code = status_code
    err.status_code = status_code
    err.args = (message,)
    return err


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

    def test_paragraph_count_mismatch_is_per_article(self):
        bad_json = json.dumps({
            "title": "🚀 Title",
            "alts": ["a", "b"],
            "subtitle": "sub",
            "paragraphs": ["only one"],  # input had 2
            "blocks": None,
        })
        client = _make_client_returning(_make_response(bad_json))
        with patch.object(gemini_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                gemini_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

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

    _ENV_KEYS = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")

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
        import llm_transcreation
        self.assertIs(claude_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(gemini_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(openai_transcreation.ClaudeOutageError, CommonOutage)
        self.assertIs(llm_transcreation.ClaudeOutageError, CommonOutage)


if __name__ == "__main__":
    unittest.main()
