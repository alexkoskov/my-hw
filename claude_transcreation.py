"""Anthropic SDK wrapper — Claude transcreation for the auto-fallback path.

This module is the primary translation engine for the
``llm-transcreation-and-distributed-publishing`` feature. It loads
``ux-guidelines.md`` once (cached by mtime), composes the system prompt
+ a small JSON envelope, and calls Claude (Haiku 4.5 by default) to
transcreate one English article into a Russian dict mirroring the
manual-review (`hw_review`) path's quality bar.

Per-article failures (refusal, malformed JSON, schema mismatch, 4xx)
raise ``ClaudeTranscreationError`` — caller falls back to Google Translate
for THAT article only. API-level failures (network, 429, 5xx, auth, model
404) raise ``ClaudeOutageError`` — caller advances the outage state machine
and Google-fallback persists across slots.

Public API
----------
* ``transcreate_via_claude(article)`` — main entry point.
* ``health_check()`` — non-raising bool probe for recovery state.
* ``ClaudeOutageError``, ``ClaudeTranscreationError`` — exception types.
* ``is_outage_error(exc)``, ``is_per_article_error(exc)`` — classifier helpers.

Decisions implemented
---------------------
* Decision 5: SDK exception classification (API-level vs per-article).
* Decision 6: System prompt = ``ux-guidelines.md`` verbatim + JSON envelope;
  model = Haiku 4.5; ``max_retries=2`` (SDK default); no streaming;
  no prompt caching.
* Decision 8: Flat-path fallback for ``ux-guidelines.md`` on server.
* Decision 13: ``max_tokens=8000``; per-paragraph defensive 4000-char cap;
  paragraph-count validation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module constants                                                            #
# --------------------------------------------------------------------------- #

from _llm_common import (
    ClaudeOutageError,
    ClaudeTranscreationError,
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

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

#: Primary location of ``ux-guidelines.md`` — the project-knowledge subdir.
_PROMPT_PATH = _resolve_prompt_path(_MODULE_DIR)

#: Cache keyed by ``(resolved_path, mtime)``. Reload on file change OR when
#: a different on-disk file resolves (e.g. flat fallback after subdir miss).
_PROMPT_CACHE: dict = {"mtime": None, "body": None, "path": None}

#: Output token cap. 30K — see openrouter_transcreation for rationale
#: (long autoevolution unboxing posts were truncating at 8K).
_DEFAULT_MAX_TOKENS = 30000

#: Default model name (Decision 6); overridable via ``ANTHROPIC_MODEL`` env.
_DEFAULT_MODEL = "claude-haiku-4-5"

#: Lazily-instantiated singleton client (only built on first real call).
_DEFAULT_CLIENT: Optional["anthropic.Anthropic"] = None


def _parse_response(text, expected_paragraph_count, expected_block_count):
    return _parse_response_common(
        text, expected_paragraph_count, expected_block_count,
        engine_name="Claude", log=logger,
    )


# --------------------------------------------------------------------------- #
# Prompt loading                                                              #
# --------------------------------------------------------------------------- #


def _load_prompt(path: str = _PROMPT_PATH) -> str:
    """Load ``ux-guidelines.md`` body, with subdir → flat-path fallback.

    Cache by mtime: re-read only when the file's mtime changes.
    Per Decision 8, on a server that received the bundle via flat ``scp``,
    the file lands at ``<module dir>/ux-guidelines.md`` — try that location
    if the original subdir path is missing.

    Empty file is treated as missing (raises ``FileNotFoundError``).
    """
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
    'Return strictly JSON: {"translations": ["ru1", "ru2", ...]} — same '
    "count and order as the input."
)

_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading")


def _translate_block_strings(
    blocks: list,
    client: "anthropic.Anthropic",
    model: str,
    *,
    timeout_s: int = 60,
    max_tokens: int = 30000,
    skip_patched_text: bool = True,
) -> list:
    """Variant B+ second-pass: translate block ``text`` / ``caption`` fields.

    Used after the variant B fallback substitutes the article's original EN
    blocks. Sends a flat numbered list of EN strings to a focused call,
    splices Russian translations back. On any failure, returns ``blocks``
    unchanged (B fallback remains correct).
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
    user_msg = (
        "Translate these numbered English blog captions/text fragments into "
        "Russian and return JSON.\n\n" + "\n".join(en_lines)
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_BLOCK_TRANSLATE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            timeout=timeout_s,
        )
        text = response.content[0].text  # type: ignore[index]
        parsed = json.loads(_strip_json_fence(text or "{}"))
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
# Exception classification                                                    #
# --------------------------------------------------------------------------- #


def _classify_exception(exc: BaseException) -> Exception:
    """Map an anthropic SDK exception to ``ClaudeOutageError`` /
    ``ClaudeTranscreationError`` per Decision 5.

    API-level (advance outage state machine):
        APIConnectionError, APITimeoutError, RateLimitError,
        InternalServerError, AuthenticationError, PermissionDeniedError,
        NotFoundError.

    Per-article (single-article Google fallback, no state change):
        BadRequestError, UnprocessableEntityError, APIStatusError (other),
        ClaudeTranscreationError (parse / schema).
    """
    # APITimeoutError extends APIConnectionError — order matters but both
    # map to outage so the order is moot here.
    if isinstance(exc, (
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
    )):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, (
        anthropic.BadRequestError,
        anthropic.UnprocessableEntityError,
    )):
        return ClaudeTranscreationError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, anthropic.APIStatusError):
        # Conservative default for unrecognized status codes — don't escalate
        # to outage state machine on a novel code.
        return ClaudeTranscreationError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, ClaudeTranscreationError):
        return exc
    if isinstance(exc, ClaudeOutageError):
        return exc

    # Anything else (including bare anthropic.APIError) — treat as outage so
    # admin sees a ping rather than the channel silently degrading.
    if isinstance(exc, anthropic.APIError):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    # Truly unexpected — re-raise without re-wrapping.
    return exc  # type: ignore[return-value]


def is_outage_error(exc: BaseException) -> bool:
    """Public classifier: True iff ``exc`` should advance the outage state
    machine. Used by ``_fallback_publish`` (Wave 3, task 7)."""
    return isinstance(exc, ClaudeOutageError)


def is_per_article_error(exc: BaseException) -> bool:
    """Public classifier: True iff ``exc`` is a per-article Claude failure
    (single-article Google fallback, no state change)."""
    return isinstance(exc, ClaudeTranscreationError)


# --------------------------------------------------------------------------- #
# Client lifecycle                                                            #
# --------------------------------------------------------------------------- #


def _get_default_client() -> "anthropic.Anthropic":
    """Lazily build the singleton ``anthropic.Anthropic`` client.

    Module import must succeed without ``ANTHROPIC_API_KEY`` set (so test
    collection and ``news_bot`` startup health checks don't fail). The real
    client is built only on the first actual ``transcreate_via_claude`` call
    that does not pass an explicit ``client=`` argument.
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = anthropic.Anthropic()
    return _DEFAULT_CLIENT


# --------------------------------------------------------------------------- #
# Main entry point                                                            #
# --------------------------------------------------------------------------- #


def transcreate_via_claude(
    article: dict,
    *,
    prompt_path: str = _PROMPT_PATH,
    model: Optional[str] = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout_s: int = 60,
    client: Optional["anthropic.Anthropic"] = None,
) -> dict:
    """Transcreate one article via Claude.

    Parameters
    ----------
    article: dict
        Keys: ``source_name`` (str), ``title`` (str), ``subtitle`` (str),
        ``paragraphs`` (list[str]), ``blocks`` (list[dict] | None).
    prompt_path: str
        Override for the ``ux-guidelines.md`` location (defaults to subdir
        path; the loader applies flat-path fallback automatically).
    model: str, optional
        Defaults to ``ANTHROPIC_MODEL`` env or ``claude-haiku-4-5``.
    max_tokens: int
        Output cap. Default 8000 (Decision 13).
    timeout_s: int
        Per-call timeout (forwarded to SDK).
    client: anthropic.Anthropic, optional
        Injectable for tests. ``None`` → lazy module-level singleton.

    Returns
    -------
    dict with keys ``title``, ``alts``, ``subtitle``, ``paragraphs``, ``blocks``.

    Raises
    ------
    ClaudeOutageError
        On API-level errors (advances outage state machine).
    ClaudeTranscreationError
        On per-article failures (Google-fallback for THIS article only).
    """
    paragraphs_in = article.get("paragraphs") or []
    blocks_in = article.get("blocks")
    expected_paragraph_count = len(paragraphs_in)
    expected_block_count = len(blocks_in) if isinstance(blocks_in, list) else None

    if model is None:
        model = os.getenv("ANTHROPIC_MODEL", _DEFAULT_MODEL)
    if client is None:
        client = _get_default_client()

    # Build messages (raises FileNotFoundError if ux-guidelines.md missing —
    # startup health check in news_bot.main() catches this; downstream
    # callers treat it as a deploy-misconfig, not an outage).
    prompt_body = _load_prompt(prompt_path)
    system_prompt = _build_system_prompt(prompt_body)
    user_msg = _build_user_message(article)

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            timeout=timeout_s,
        )
    except (ClaudeOutageError, ClaudeTranscreationError):
        # Already classified — don't wrap again.
        raise
    except anthropic.APIError as exc:
        raise _classify_exception(exc) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    # Extract text — Claude returns a list of content blocks; we expect
    # the first to be a text block. Defensive on shape variation.
    try:
        text = response.content[0].text  # type: ignore[index]
    except (AttributeError, IndexError, TypeError) as exc:
        raise ClaudeTranscreationError(
            f"Claude response shape unexpected: {exc}"
        ) from exc

    # Token + latency observability (AC19).
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    response_model = getattr(response, "model", model)
    logger.info(
        "claude_transcreation: model=%s input_tokens=%s output_tokens=%s "
        "latency_ms=%s",
        response_model,
        input_tokens,
        output_tokens,
        latency_ms,
    )

    # Parse + validate.
    parsed = _parse_response(text, expected_paragraph_count, expected_block_count)

    # Post-pass: emoji safety net + per-paragraph defensive truncation.
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
        parsed["blocks"] = _translate_block_strings(
            parsed["blocks"], client, model, timeout_s=timeout_s,
        )

    return parsed


# --------------------------------------------------------------------------- #
# Health check                                                                #
# --------------------------------------------------------------------------- #


def health_check(client: Optional["anthropic.Anthropic"] = None) -> bool:
    """Cheap probe: returns True iff Claude appears reachable AND
    ``ux-guidelines.md`` is loadable. Never raises.

    Used by:
      * Outage recovery probe (Wave 2 task 5).
      * Startup health check (Decision 14).
    """
    # 1. Prompt presence — non-network check first, fast.
    try:
        _load_prompt()
    except Exception as exc:  # noqa: BLE001 — health check, never raise
        logger.info("health_check: prompt load failed: %s", type(exc).__name__)
        return False

    # 2. Lightweight ping. We try to build a default client only if no client
    # was supplied; failure to build (e.g. missing ANTHROPIC_API_KEY) → False.
    try:
        if client is None:
            client = _get_default_client()
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: client init failed: %s", type(exc).__name__)
        return False

    try:
        # 10-token probe — minimal cost, exercises auth + network + model.
        client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", _DEFAULT_MODEL),
            max_tokens=10,
            system="You are a health probe.",
            messages=[{"role": "user", "content": "ping"}],
            timeout=15,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — health check, never raise
        logger.info(
            "health_check: probe failed: %s",
            type(exc).__name__,
        )
        return False
