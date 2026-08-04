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

import httpx  # noqa: E402 — transitive dep of openai, used to build real SDK errors
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


#: Client used only to reach the SDK's status-error dispatch. Never issues a
#: request — ``_make_status_error_from_response`` is pure.
_ERROR_CLIENT = openai.OpenAI(api_key="test-key-not-used",
                              base_url="https://openrouter.ai/api/v1")


def _make_openai_error(error_class, message: str = "test"):
    err = error_class.__new__(error_class)
    err.message = message
    err.args = (message,)
    err.code = None
    err.status_code = getattr(error_class, "status_code", None)
    err.response = None
    return err


def _make_status_error(status_code: int, message: str = "status error"):
    """Build the exception the SDK itself would raise for ``status_code``.

    Goes through ``_make_status_error_from_response`` — the SDK's own dispatch —
    rather than calling a constructor directly, so the test double matches
    production in BOTH respects that matter here: the class (402/407/408/413/451
    have no dedicated class and come back as a bare ``APIStatusError``, which is
    why the classifier has to read ``.status_code``) and the message shape
    (``"Error code: 402 - {...}"`` — the SDK prepends the code, the server body
    does not contain it).
    """
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    body = json.dumps({"error": {"code": status_code, "message": message}})
    response = httpx.Response(
        status_code, request=request, content=body.encode(),
        headers={"content-type": "application/json"},
    )
    return _ERROR_CLIENT._make_status_error_from_response(response)


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

    def test_payment_required_is_outage(self):
        """402 «Insufficient credits» — an empty OpenRouter balance is an
        account-level problem, not a problem with THIS article.

        Incident 2026-07-14: two articles were struck out three times each and
        moved to ``failed_articles`` because 402 fell into the bare
        ``APIStatusError`` catch-all → per-article. Holding is the only outcome
        that survives a top-up.
        """
        client = _make_client_returning(_make_status_error(402, "Insufficient credits"))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError) as ctx:
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

        # The wrap must carry the cause forward: the `[hold]` log line in
        # news_bot is the only record of WHY an article was held, and it can
        # only print what this message contains.
        self.assertIn("402", str(ctx.exception))
        self.assertIn("Insufficient credits", str(ctx.exception))

    def test_proxy_auth_and_server_timeout_are_outage(self):
        """407 (proxy auth) and 408 (server-side timeout) are transport-level,
        exactly like the 401/403/APITimeoutError cases already held."""
        for code in (407, 408):
            with self.subTest(status_code=code):
                client = _make_client_returning(_make_status_error(code))
                with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeOutageError):
                        openrouter_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )

    def test_unknown_status_stays_per_article(self):
        """Regression guard for the conservative default.

        413 (payload too large) and 451 (legal) are genuinely about THIS
        article. Classifying them as an outage would hold the row at the queue
        head — and ``job()`` re-reads ``list_pending()[0]`` every slot, so the
        same poisoned row would block every later publish forever. Losing one
        article after 3 strikes is the cheaper failure.
        """
        for code in (413, 451):
            with self.subTest(status_code=code):
                client = _make_client_returning(_make_status_error(code))
                with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
                    with self.assertRaises(ClaudeTranscreationError):
                        openrouter_transcreation.transcreate_via_claude(
                            SAMPLE_ARTICLE, client=client
                        )


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

    def test_skip_patched_text_default_skips_paragraph_block_text(self):
        """``skip_patched_text=True`` (default in the variant-B+ flow)
        skips the ``text`` field on lead/paragraph/heading blocks
        because those are already RU after _patch_text_with_ru_paragraphs.
        Only ``caption`` (and text on other block types) hits the API."""
        blocks = [
            {"type": "paragraph", "text": "Already RU paragraph."},
            {"type": "image", "src": "https://x/a.jpg", "caption": "EN cap"},
            {"type": "lead", "text": "Already RU lead."},
        ]
        # Mock returns 1 translation — only the image caption.
        translations_json = json.dumps({"translations": ["RU cap"]})
        client = _make_client_returning(_make_response(translations_json))
        out = openrouter_transcreation._translate_block_strings(
            blocks, client, "m",
        )
        # Only caption translated; paragraph + lead text untouched.
        self.assertEqual(out[0]["text"], "Already RU paragraph.")
        self.assertEqual(out[1]["caption"], "RU cap")
        self.assertEqual(out[2]["text"], "Already RU lead.")
        # The user_msg sent to GPT contained ONLY the caption.
        called = client.chat.completions.create.call_args.kwargs
        user_msg = called["messages"][1]["content"]
        self.assertIn("EN cap", user_msg)
        self.assertNotIn("Already RU", user_msg)

    def test_skip_patched_text_false_translates_all_text_fields(self):
        """``skip_patched_text=False`` preserves the legacy contract —
        all text/caption fields are sent for translation regardless of
        block type. Used by tests that don't run the patch beforehand."""
        blocks = [
            {"type": "paragraph", "text": "Para text."},
            {"type": "image", "src": "https://x/a.jpg", "caption": "Cap"},
        ]
        translations_json = json.dumps({"translations": ["RU para", "RU cap"]})
        client = _make_client_returning(_make_response(translations_json))
        out = openrouter_transcreation._translate_block_strings(
            blocks, client, "m", skip_patched_text=False,
        )
        self.assertEqual(out[0]["text"], "RU para")
        self.assertEqual(out[1]["caption"], "RU cap")

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


class TestGetRemainingCredits(unittest.TestCase):
    """``get_remaining_credits`` is a best-effort monitoring probe: it returns the
    USD balance (total_credits - total_usage) or None (no key / error / unlimited),
    and never raises."""

    @staticmethod
    def _resp(status=200, payload=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload if payload is not None else {}
        return r

    def test_remaining_is_credits_minus_usage(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False), \
             patch("openrouter_transcreation.requests.get",
                   return_value=self._resp(200, {"data": {"total_credits": 10.0, "total_usage": 7.5}})) as g:
            out = openrouter_transcreation.get_remaining_credits()
        self.assertAlmostEqual(out, 2.5)
        args, kwargs = g.call_args
        self.assertTrue(args[0].endswith("/credits"))
        self.assertIn("Authorization", kwargs["headers"])
        self.assertNotIn("sk-or-test", args[0])  # key is in the header, not the URL

    def test_no_key_returns_none_without_request(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "OPEN_ROUTER_API_KEY": ""}, clear=False), \
             patch("openrouter_transcreation.requests.get") as g:
            out = openrouter_transcreation.get_remaining_credits()
        self.assertIsNone(out)
        g.assert_not_called()

    def test_non_200_returns_none(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False), \
             patch("openrouter_transcreation.requests.get", return_value=self._resp(500, {})):
            self.assertIsNone(openrouter_transcreation.get_remaining_credits())

    def test_network_error_returns_none(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False), \
             patch("openrouter_transcreation.requests.get", side_effect=Exception("boom")):
            self.assertIsNone(openrouter_transcreation.get_remaining_credits())

    def test_non_https_base_url_skips_without_leaking_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test",
                                     "OPENROUTER_BASE_URL": "http://evil.example/api/v1"}, clear=False), \
             patch("openrouter_transcreation.requests.get") as g:
            out = openrouter_transcreation.get_remaining_credits()
        self.assertIsNone(out)
        g.assert_not_called()  # key never egresses over cleartext

    def test_unlimited_or_bad_shape_returns_none(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False), \
             patch("openrouter_transcreation.requests.get",
                   return_value=self._resp(200, {"data": {"total_credits": None, "total_usage": 0}})):
            self.assertIsNone(openrouter_transcreation.get_remaining_credits())


if __name__ == "__main__":
    unittest.main()


class TestErrorEnvelopeIn200(unittest.TestCase):
    """OpenRouter can answer HTTP 200 with an error ENVELOPE in the body
    instead of a status code — a gateway shape the SDK cannot classify,
    because to the SDK it is a perfectly successful response.

    Before this the envelope reached the ``response.choices[0]`` read, raised
    ``ClaudeTranscreationError('response shape unexpected')`` — a per-article
    strike — and an out-of-credits 402 delivered this way lost the article
    after three of them. Exactly the 2026-07-14 failure, through a door the
    status-code fix did not cover.
    """

    def setUp(self):
        openrouter_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}

    @staticmethod
    def _envelope_response(code, message="Insufficient credits"):
        """A 200 whose body carries ``error`` and no usable ``choices`` —
        what the SDK hands back for a gateway-level error envelope."""
        response = MagicMock()
        response.choices = []
        response.error = {"code": code, "message": message}
        return response

    def test_account_level_envelope_is_an_outage(self):
        client = _make_client_returning(self._envelope_response(402))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeOutageError) as ctx:
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)
        # The cause must survive into the message — it is what the [hold] log
        # line and [E038] print.
        self.assertIn("402", str(ctx.exception))
        self.assertIn("Insufficient credits", str(ctx.exception))

    def test_article_level_envelope_stays_per_article(self):
        """Same conservative default as the status-code path: an envelope code
        that is about THIS article must not hold the queue head."""
        client = _make_client_returning(self._envelope_response(413, "too large"))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError):
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)

    def test_envelope_without_a_code_stays_per_article(self):
        response = MagicMock()
        response.choices = []
        response.error = {"message": "something went wrong"}
        client = _make_client_returning(response)
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            with self.assertRaises(ClaudeTranscreationError) as ctx:
                openrouter_transcreation.transcreate_via_claude(SAMPLE_ARTICLE, client=client)
        self.assertIn("something went wrong", str(ctx.exception))

    def test_a_healthy_response_is_untouched(self):
        """Regression guard: the envelope check must not intercept a normal
        response. ``MagicMock`` answers every attribute, so a naive
        ``getattr(response, 'error')`` would swallow every success."""
        client = _make_client_returning(_make_response(SAMPLE_VALID_JSON))
        with patch.object(openrouter_transcreation, "_load_prompt", return_value="X"):
            result = openrouter_transcreation.transcreate_via_claude(
                SAMPLE_ARTICLE, client=client)
        self.assertIn("title", result)
