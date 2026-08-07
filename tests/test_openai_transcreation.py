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

import httpx  # noqa: E402 — transitive dep of openai, used to build real SDK errors
import openai  # noqa: E402
import _llm_common  # noqa: E402
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


#: Client used only to reach the SDK's status-error dispatch. Never issues a
#: request — ``_make_status_error_from_response`` is pure.
_ERROR_CLIENT = openai.OpenAI(api_key="test-key-not-used")


def _make_status_error(status_code: int, message: str = "status error"):
    """Build the exception the SDK itself would raise for ``status_code``.

    Goes through the SDK's own dispatch rather than a direct constructor call,
    so both the class and the ``.status_code`` the classifier reads come from
    the same code path production uses.
    """
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = json.dumps({"error": {"code": status_code, "message": message}})
    response = httpx.Response(
        status_code, request=request, content=body.encode(),
        headers={"content-type": "application/json"},
    )
    return _ERROR_CLIENT._make_status_error_from_response(response)


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

    def test_account_level_status_codes_are_outage(self):
        """402 (payment required), 407 (proxy auth), 408 (server-side timeout).

        The SDK has no dedicated class for any of them, so all three land in
        the bare ``APIStatusError`` catch-all. None is about THIS article — see
        the 2026-07-14 OpenRouter incident recorded in the sibling engine test.
        """
        for code in (402, 407, 408):
            with self.subTest(status_code=code):
                client = _make_client_returning(_make_status_error(code))
                with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeOutageError):
                        openai_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )

    def test_unknown_status_stays_per_article(self):
        """Conservative default: 413/451 are article-specific, and an outage
        would wedge the row at the queue head instead of striking it out."""
        for code in (413, 451):
            with self.subTest(status_code=code):
                client = _make_client_returning(_make_status_error(code))
                with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeTranscreationError):
                        openai_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )


class TestResponseValidation(unittest.TestCase):
    def setUp(self):
        openai_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    def test_malformed_json_is_per_article(self):
        client = _make_client_returning(_make_response("not json {{"))
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openai_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_paragraph_count_mismatch_is_accepted_with_warning(self):
        """Count divergence is accepted per the relaxed contract — Telegraph
        renders <p> nodes without a structural 1:1 mapping requirement.
        A WARNING is logged for operator visibility. Paragraph kept long
        enough (>=30 chars) so the separate too-short floor is not hit."""
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
        with patch.object(openai_transcreation, "_load_prompt", return_value="X"):
            with self.assertLogs('openai_transcreation', level='WARNING') as cm:
                out = openai_transcreation.transcreate_via_claude(
                    SAMPLE_ARTICLE, client=client,
                )
        self.assertEqual(len(out["paragraphs"]), 1)
        self.assertTrue(any(
            'paragraph count diverges' in r.message and 'expected 2' in r.message
            for r in cm.records
        ))


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
        out = openai_transcreation._translate_block_strings(blocks, client, "m")
        self.assertEqual(out[0]["text"], "Уже переведённый пункт списка.")
        self.assertEqual(out[1]["caption"], "RU cap")
        called = client.chat.completions.create.call_args.kwargs
        user_msg = called["messages"][1]["content"]
        self.assertIn("EN cap", user_msg)
        self.assertNotIn("Уже переведённый пункт списка.", user_msg)

    def test_patched_types_include_list_item_and_match_shared(self):
        """Anti-drift guard: compare the whole tuple with the shared one,
        so a type added to ``_llm_common`` and forgotten here also fails."""
        self.assertEqual(
            openai_transcreation._PATCHED_TEXT_BLOCK_TYPES,
            _llm_common._PATCHED_TEXT_BLOCK_TYPES,
        )


if __name__ == "__main__":
    unittest.main()
