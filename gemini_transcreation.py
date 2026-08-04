"""Google Gemini wrapper — alternate translation engine.

Mirror of ``claude_transcreation.py`` with the same public API:
``transcreate_via_claude``, ``health_check``, ``ClaudeOutageError``,
``ClaudeTranscreationError``, ``is_outage_error``, ``is_per_article_error``.
The "Claude" prefix on exceptions is preserved for backward compatibility
with news_bot.py and existing tests; semantics are provider-agnostic.

Selected via ``LLM_PROVIDER=gemini`` in the environment (dispatcher
lives in ``llm_transcreation.py``).

System prompt (``ux-guidelines.md``) and JSON envelope are identical
to the Claude path — quality bar matches.

Uses the modern ``google-genai`` SDK (the older ``google.generativeai``
package is deprecated upstream).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from _llm_common import (
    ClaudeOutageError,
    ClaudeTranscreationError,
    _ACCOUNT_LEVEL_STATUS_CODES,
    _JSON_ENVELOPE,
    _PARAGRAPH_MAX_CHARS,
    _TITLE_EMOJIS,
    _apply_emoji_safety_net,
    _build_system_prompt,
    _build_user_message,
    _is_mostly_russian,
    _parse_response as _parse_response_common,
    _patch_text_with_ru_paragraphs,
    _resolve_prompt_path,
    _strip_json_fence,
    _truncate_paragraphs,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module constants                                                            #
# --------------------------------------------------------------------------- #

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

_PROMPT_PATH = _resolve_prompt_path(_MODULE_DIR)

_PROMPT_CACHE: dict = {"mtime": None, "body": None, "path": None}

#: Output token cap. 30K — long unboxing posts were truncating at 8K.
#: See openrouter_transcreation for rationale.
_DEFAULT_MAX_TOKENS = 30000
_DEFAULT_MODEL = "gemini-2.5-flash-lite"

#: Lazily-instantiated singleton client.
_DEFAULT_CLIENT: Optional["genai.Client"] = None


def _parse_response(text, expected_paragraph_count, expected_block_count):
    return _parse_response_common(
        text, expected_paragraph_count, expected_block_count,
        engine_name="Gemini", log=logger,
    )


# --------------------------------------------------------------------------- #
# Prompt loading (identical to claude_transcreation)                          #
# --------------------------------------------------------------------------- #


def _load_prompt(path: str = _PROMPT_PATH) -> str:
    candidate = path
    if not os.path.isfile(candidate):
        flat_fallback = os.path.join(_MODULE_DIR, "ux-guidelines.md")
        if not os.path.isfile(flat_fallback):
            raise FileNotFoundError(
                f"ux-guidelines.md not found at {path!r} or flat fallback "
                f"{flat_fallback!r}"
            )
        candidate = flat_fallback

    mtime = os.path.getmtime(candidate)
    if (
        _PROMPT_CACHE.get("path") == candidate
        and _PROMPT_CACHE.get("mtime") == mtime
        and _PROMPT_CACHE.get("body")
    ):
        return _PROMPT_CACHE["body"]

    with open(candidate, "r", encoding="utf-8") as fh:
        body = fh.read()
    if not body.strip():
        raise FileNotFoundError(
            f"ux-guidelines.md at {candidate!r} is empty — treating as missing"
        )

    _PROMPT_CACHE["path"] = candidate
    _PROMPT_CACHE["mtime"] = mtime
    _PROMPT_CACHE["body"] = body
    return body


# --------------------------------------------------------------------------- #
# Variant B+ second-pass: translate captions/text in EN-fallback blocks       #
# --------------------------------------------------------------------------- #


_BLOCK_TRANSLATE_SYSTEM = (
    "You are a Hot Wheels diecast collector blog translator. "
    "Translate each numbered English caption/text below into natural, "
    "idiomatic Russian suitable for a Telegram channel. "
    "Brand and model names (Hot Wheels, Mattel, Nissan GT-R, Bugatti, "
    "Matchbox, etc.) stay in English. Keep tone enthusiastic but factual. "
    "Call the collectible itself «машинка» — never «миниатюра», "
    "«фигурка», «моделька» or «изделие». Exception: a real character "
    "figure (a person or creature) stays «фигурка». "
    'Return strictly JSON: {"translations": ["ru1", "ru2", ...]} — same '
    "count and order as the input."
)

_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading")


def _translate_block_strings(
    blocks: list,
    client: "genai.Client",
    model: str,
    *,
    max_tokens: int = 30000,
    skip_patched_text: bool = True,
) -> list:
    """Variant B+ second-pass: translate block ``text`` / ``caption`` fields.

    See ``openrouter_transcreation._translate_block_strings`` for full doc.
    Always returns a list — keeps EN blocks on any failure.
    """
    if not isinstance(blocks, list) or not blocks:
        return blocks

    items: list[tuple[int, str, str]] = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        for field in ("text", "caption"):
            if (skip_patched_text and field == "text"
                    and btype in _PATCHED_TEXT_BLOCK_TYPES):
                continue
            v = b.get(field)
            if isinstance(v, str) and v.strip():
                items.append((i, field, v))

    if not items:
        return blocks

    en_lines = [f"{n+1}. {it[2]}" for n, it in enumerate(items)]
    user_msg = "\n".join(en_lines)

    try:
        response = client.models.generate_content(
            model=model,
            contents=user_msg,
            config=genai_types.GenerateContentConfig(
                system_instruction=_BLOCK_TRANSLATE_SYSTEM,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        text = response.text
        parsed = json.loads(text or "{}")
        translations = parsed.get("translations")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "block-caption second-pass failed: %s; keeping EN captions",
            type(exc).__name__,
        )
        return blocks

    if not isinstance(translations, list) or len(translations) != len(items):
        logger.warning(
            "block-caption translation count mismatch: expected %d, got %s; "
            "keeping EN captions",
            len(items),
            len(translations) if isinstance(translations, list) else type(translations).__name__,
        )
        return blocks

    result = [dict(b) if isinstance(b, dict) else b for b in blocks]
    for (idx, field, _en), ru in zip(items, translations):
        if isinstance(ru, str) and ru.strip():
            result[idx][field] = ru
    logger.info(
        "block-caption second-pass success: translated %d strings across %d blocks",
        len(items), len(blocks),
    )
    return result


# --------------------------------------------------------------------------- #
# Exception classification (Gemini-specific mapping)                          #
# --------------------------------------------------------------------------- #


#: 4xx codes that mean "the API/account is unavailable", not "this article is
#: bad". google-genai raises one ``ClientError`` for every 4xx, so the split
#: that the other engines get for free from their SDK's exception classes has
#: to be made here by code:
#:
#:   401/403/404/409/429 — the codes OpenAI/Anthropic give a dedicated class
#:                         (Authentication/PermissionDenied/NotFound/Conflict/
#:                         RateLimit), all already held by the other engines.
#:   499 cancelled       — Gemini-only, documented at
#:                         https://ai.google.dev/gemini-api/docs/api-errors as
#:                         "client cancelled the request". Transport-level: the
#:                         google-genai twin of ``APITimeoutError`` and of the
#:                         408 in the shared set.
#:   402/407/408         — the shared ``_ACCOUNT_LEVEL_STATUS_CODES``.
_CLIENT_OUTAGE_CODES = (
    frozenset({401, 403, 404, 409, 429, 499}) | _ACCOUNT_LEVEL_STATUS_CODES
)


def _classify_exception(exc: BaseException) -> Exception:
    """Map a Gemini SDK exception to ``ClaudeOutageError`` / ``ClaudeTranscreationError``.

    google-genai exception hierarchy:
        APIError (base)
          ├── ClientError  — 4xx responses
          └── ServerError  — 5xx responses

    Mapping:
        ClientError (401/403 auth, 404 model-not-found, 429 quota, and
                     ``_ACCOUNT_LEVEL_STATUS_CODES``)        → outage
        ClientError (any other 4xx — 400, 422, 413, 451, …)  → per-article
        ServerError (any 5xx)                                → outage
        UnknownApiResponseError                              → outage
        Generic APIError                                     → outage (conservative)
    """
    if isinstance(exc, (ClaudeTranscreationError, ClaudeOutageError)):
        return exc

    if isinstance(exc, genai_errors.ClientError):
        # ``ClientError`` exposes the status as ``.code`` and has NO
        # ``.status_code`` (verified on google-genai 1.73.1) — which is exactly
        # why this branch tests the shared set by hand rather than calling
        # ``_is_account_level_status``, whose ``getattr(exc, "status_code")``
        # would silently return False for every code here.
        code = getattr(exc, "code", None)
        if code in _CLIENT_OUTAGE_CODES:
            return ClaudeOutageError(f"{type(exc).__name__}({code}): {exc}")
        # Everything else 4xx is per-article. This default was flipped from
        # outage on 2026-08-04 to match the three SDK-based engines: an outage
        # HOLDS the row and ``news_bot.job()`` re-reads ``list_pending()[0]``
        # every slot, so an article-specific code (413 payload too large, 451
        # legal) used to pin that row to the queue head and stop the channel.
        return ClaudeTranscreationError(f"{type(exc).__name__}({code}): {exc}")

    if isinstance(exc, genai_errors.ServerError):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, genai_errors.UnknownApiResponseError):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, genai_errors.APIError):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    return exc  # type: ignore[return-value]


def is_outage_error(exc: BaseException) -> bool:
    return isinstance(exc, ClaudeOutageError)


def is_per_article_error(exc: BaseException) -> bool:
    return isinstance(exc, ClaudeTranscreationError)


# --------------------------------------------------------------------------- #
# Client lifecycle                                                            #
# --------------------------------------------------------------------------- #


def _get_default_client() -> "genai.Client":
    """Lazily build the singleton ``genai.Client`` using ``GEMINI_API_KEY``.

    Module import must succeed without ``GEMINI_API_KEY`` set (so test
    collection and ``news_bot`` startup health checks don't fail). The real
    client is built only on the first actual ``transcreate_via_claude`` call
    that does not pass an explicit ``client=`` argument.
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY env var not set")
        _DEFAULT_CLIENT = genai.Client(api_key=api_key)
    return _DEFAULT_CLIENT


# --------------------------------------------------------------------------- #
# Main entry point                                                            #
# --------------------------------------------------------------------------- #


def transcreate_via_claude(  # name kept for backward compat
    article: dict,
    *,
    prompt_path: str = _PROMPT_PATH,
    model: Optional[str] = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout_s: int = 60,
    client: Optional["genai.Client"] = None,
) -> dict:
    """Transcreate one article via Gemini.

    Public API matches ``claude_transcreation.transcreate_via_claude`` for
    drop-in dispatcher compatibility. ``client`` parameter accepts an
    injected ``genai.Client`` instance for tests.
    """
    paragraphs_in = article.get("paragraphs") or []
    blocks_in = article.get("blocks")
    expected_paragraph_count = len(paragraphs_in)
    expected_block_count = len(blocks_in) if isinstance(blocks_in, list) else None

    if model is None:
        model = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL)

    prompt_body = _load_prompt(prompt_path)
    system_prompt = _build_system_prompt(prompt_body)
    user_msg = _build_user_message(article)

    if client is None:
        try:
            client = _get_default_client()
        except RuntimeError as exc:
            raise ClaudeOutageError(str(exc)) from exc

    started = time.monotonic()
    try:
        response = client.models.generate_content(
            model=model,
            contents=user_msg,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
    except (ClaudeOutageError, ClaudeTranscreationError):
        raise
    except Exception as exc:
        raise _classify_exception(exc) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        text = response.text
    except (AttributeError, ValueError) as exc:
        # ``response.text`` raises ValueError if response was blocked/empty.
        raise ClaudeTranscreationError(
            f"Gemini response shape unexpected or blocked: {exc}"
        ) from exc

    if not text:
        raise ClaudeTranscreationError("Gemini response.text is empty")

    # Token + latency observability (AC19).
    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    logger.info(
        "gemini_transcreation: model=%s input_tokens=%s output_tokens=%s "
        "latency_ms=%s",
        model,
        input_tokens,
        output_tokens,
        latency_ms,
    )

    parsed = _parse_response(text, expected_paragraph_count, expected_block_count)
    parsed["title"] = _apply_emoji_safety_net(parsed["title"])
    parsed["paragraphs"] = _truncate_paragraphs(parsed["paragraphs"])

    # Variant B fallback: if model didn't return matching blocks, use
    # the article's original EN blocks for structural completeness.
    if expected_block_count and not parsed.get("blocks"):
        parsed["blocks"] = _patch_text_with_ru_paragraphs(
            blocks_in, parsed["paragraphs"],
        )
        logger.info(
            "Using original EN blocks (count=%d, paragraph text spliced "
            "from main response)", expected_block_count,
        )
        parsed["blocks"] = _translate_block_strings(parsed["blocks"], client, model)

    return parsed


# --------------------------------------------------------------------------- #
# Health check                                                                #
# --------------------------------------------------------------------------- #


def health_check(client: Optional["genai.Client"] = None) -> bool:
    """Cheap probe: returns True iff Gemini reachable AND ux-guidelines.md
    loadable. Never raises."""
    try:
        _load_prompt()
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: prompt load failed: %s", type(exc).__name__)
        return False

    try:
        if client is None:
            client = _get_default_client()
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: client init failed: %s", type(exc).__name__)
        return False

    try:
        client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", _DEFAULT_MODEL),
            contents="ping",
            config=genai_types.GenerateContentConfig(max_output_tokens=10),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: probe failed: %s", type(exc).__name__)
        return False
