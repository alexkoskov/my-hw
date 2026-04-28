"""Mocked unit tests for openai_transcreation.

Mirror the structure of test_claude_transcreation.py and
test_gemini_transcreation.py but use openai exception types.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai  # noqa: E402
import openai_transcreation  # noqa: E402
from _llm_common import ClaudeOutageError, ClaudeTranscreationError  # noqa: E402


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


def _make_response(text: str, prompt_tokens: int = 100, completion_tokens: int = 50):
    """Build a mock OpenAI ChatCompletion-shaped response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    response.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    response.model = "gpt-4o-mini"
    return response


def _make_client_returning(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.chat.completions.create.side_effect = response_or_exc
    else:
        client.chat.completions.create.return_value = response_or_exc
    return client


def _make_openai_error(error_class, message: str = "test"):
    """Construct an OpenAI SDK exception (no real HTTP request, no body)."""
    err = error_class.__new__(error_class)
    err.message = message
    err.args = (message,)
    # Minimal attributes the SDK exception classes typically expose
    err.code = None
    err.status_code = getattr(error_class, "status_code", None)
    err.response = None
    return err


class TestSuccessPath(unittest.TestCase):
    def setUp(self):
        openai_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_happy_path_returns_valid_dict(self):
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(openai_transcreation, "_load_prompt", return_value="STUB"):
            result = openai_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client,
            )
        self.assertIn("title", result)
        self.assertTrue(result["title"].startswith("🏎️"))
        self.assertEqual(len(result["alts"]), 3)
        self.assertEqual(len(result["paragraphs"]), 2)


class TestExceptionClassification(unittest.TestCase):
    def setUp(self):
        openai_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_rate_limit_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.RateLimitError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_auth_error_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.AuthenticationError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_connection_error_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.APIConnectionError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_internal_server_error_is_outage(self):
        client = _make_client_returning(_make_openai_error(openai.InternalServerError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_bad_request_is_per_article(self):
        client = _make_client_returning(_make_openai_error(openai.BadRequestError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_unprocessable_entity_is_per_article(self):
        client = _make_client_returning(_make_openai_error(openai.UnprocessableEntityError))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_classifier_passes_through_already_wrapped(self):
        """If the SDK somehow returns our own exception type, do not re-wrap."""
        original = ClaudeOutageError("already wrapped")
        result = openai_transcreation._classify_exception(original)
        self.assertIs(result, original)


class TestResponseValidation(unittest.TestCase):
    def setUp(self):
        openai_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_malformed_json_is_per_article(self):
        client = _make_client_returning(_make_response("not json {{"))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_paragraph_count_mismatch_is_per_article(self):
        bad_json = json.dumps({
            "title": "🚀 Title",
            "alts": ["a", "b"],
            "subtitle": "sub",
            "paragraphs": ["only one"],
            "blocks": None,
        })
        client = _make_client_returning(_make_response(bad_json))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        openai_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}
        openai_transcreation._DEFAULT_CLIENT = None

    def test_health_check_false_when_prompt_missing(self):
        with patch.object(openai_transcreation, "_load_prompt", side_effect=FileNotFoundError("missing")):
            self.assertFalse(openai_transcreation.health_check())

    def test_health_check_false_when_no_api_key(self):
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_API_KEY", None)
                self.assertFalse(openai_transcreation.health_check())

    def test_health_check_true_on_successful_probe(self):
        client = _make_client_returning(_make_response("pong"))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            self.assertTrue(openai_transcreation.health_check(client=client))


class TestEmojiSafetyNet(unittest.TestCase):
    def test_existing_emoji_preserved(self):
        result = openai_transcreation._apply_emoji_safety_net("🏆 Title")
        self.assertEqual(result, "🏆 Title")

    def test_missing_emoji_added_by_keyword(self):
        result = openai_transcreation._apply_emoji_safety_net("Гонки на скорости")
        self.assertTrue(result.startswith("🏎️"))


if __name__ == "__main__":
    unittest.main()
