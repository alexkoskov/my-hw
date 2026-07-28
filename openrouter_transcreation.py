"""OpenRouter wrapper — multi-model gateway via one API key.

OpenRouter (https://openrouter.ai) proxies many LLM providers (Anthropic,
OpenAI, Google, Meta, Mistral, DeepSeek, etc.) behind one OpenAI-compatible
API. The model name selects the underlying provider/model, e.g.:

    openai/gpt-4o-mini
    anthropic/claude-haiku-4-5
    google/gemini-2.5-flash
    meta-llama/llama-3.3-70b-instruct:free

Mirrors the public API of the other engines so the dispatcher can swap
providers transparently:
    transcreate_via_claude, health_check,
    ClaudeOutageError, ClaudeTranscreationError,
    is_outage_error, is_per_article_error.

Selected via ``LLM_PROVIDER=openrouter`` OR auto-selected by the dispatcher
when ``OPENROUTER_API_KEY`` is the only LLM key present.

Configuration:
    OPENROUTER_API_KEY   required
    OPENROUTER_MODEL     default ``openai/gpt-4o-mini``;
                         use ``provider/model[:variant]`` form per
                         https://openrouter.ai/models
    OPENROUTER_BASE_URL  optional, defaults to https://openrouter.ai/api/v1
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import openai
import requests

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

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module constants                                                            #
# --------------------------------------------------------------------------- #

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

_PROMPT_PATH = _resolve_prompt_path(_MODULE_DIR)

_PROMPT_CACHE: dict = {"mtime": None, "body": None, "path": None}

#: Output token cap. 30000 is generous — typical articles fit in 4-8K,
#: long autoevolution unboxing posts (50+ blocks) can hit ~16-20K of
#: Russian output. Going to 30K removes the "Unterminated string" JSON
#: truncation failure mode entirely; cost impact is bounded by actual
#: response size, not the cap.
_DEFAULT_MAX_TOKENS = 30000

#: Default model spec; "provider/model" form. Override via OPENROUTER_MODEL.
#: gpt-5.4-mini balances quality and cost — ~$0.03 per typical article
#: (vs ~$0.24 for gpt-5.5), good enough for transcreation per ux-guidelines.
_DEFAULT_MODEL = "openai/gpt-5.4-mini"

#: Env var names accepted for the API key. Canonical first, alias second.
_API_KEY_ENV_VARS = ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")


def _resolve_api_key() -> Optional[str]:
    """Return the first non-empty value from ``_API_KEY_ENV_VARS``."""
    for name in _API_KEY_ENV_VARS:
        v = os.getenv(name, "").strip()
        if v:
            return v
    return None

#: OpenRouter API base URL (OpenAI-compatible).
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: Account-balance endpoint (relative to the base URL). Returns
#: ``{"data": {"total_credits": float, "total_usage": float}}``; remaining USD =
#: total_credits - total_usage. This reflects the ACCOUNT balance (distinct from
#: ``/auth/key`` which is the key's own limit). Confirm live:
#:   curl -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/credits
_CREDITS_PATH = "/credits"
_BALANCE_TIMEOUT_S = 15


def get_remaining_credits() -> Optional[float]:
    """Best-effort probe of the remaining OpenRouter balance in USD.

    Returns ``total_credits - total_usage``, or ``None`` when it can't be
    determined — no API key, network/HTTP/JSON error, or an uncapped account
    (``total_credits`` null). NEVER raises: this backs a monitoring ping
    (``news_bot._maybe_alert_openrouter_balance``) that must not break the tick.
    The key travels in the ``Authorization`` header, never in the URL/logs.
    """
    key = _resolve_api_key()
    if not key:
        return None
    base = os.getenv("OPENROUTER_BASE_URL", "").strip() or _DEFAULT_BASE_URL
    url = base.rstrip("/") + _CREDITS_PATH
    if not url.lower().startswith("https://"):
        # Refuse to send the Bearer key over cleartext if OPENROUTER_BASE_URL is
        # misconfigured to http:// — skip the probe rather than leak the key.
        logger.warning("OpenRouter credits check skipped: non-https base URL")
        return None
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=_BALANCE_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning("OpenRouter credits check: HTTP %s", resp.status_code)
            return None
        data = (resp.json() or {}).get("data") or {}
        total = data.get("total_credits")
        used = data.get("total_usage")
        if total is None or used is None:
            return None  # uncapped account or unexpected shape → no signal
        return float(total) - float(used)
    except Exception as exc:
        # Terse, type-only log — never surface the token or a stack that might
        # embed the header. The caller treats None as "unknown, skip".
        logger.warning("OpenRouter credits check failed: %s", type(exc).__name__)
        return None

#: Lazily-instantiated singleton client.
_DEFAULT_CLIENT: Optional["openai.OpenAI"] = None


def _parse_response(text, expected_paragraph_count, expected_block_count):
    return _parse_response_common(
        text, expected_paragraph_count, expected_block_count,
        engine_name="OpenRouter", log=logger,
    )


# --------------------------------------------------------------------------- #
# Prompt loading (identical to other engines)                                 #
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

#: Block types whose ``text`` field is filled in by
#: ``_patch_text_with_ru_paragraphs`` from the main response. The
#: second-pass translator skips the ``text`` of these types to avoid
#: re-translating already-RU paragraphs (one wasted API call per long
#: article).
_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading")


def _translate_block_strings(
    blocks: list,
    client: "openai.OpenAI",
    model: str,
    *,
    timeout_s: int = 60,
    max_tokens: int = 30000,
    skip_patched_text: bool = True,
) -> list:
    """Second-pass translation of block ``text`` / ``caption`` fields.

    Used by variant B+ when the main response returned ``blocks: null`` and
    we fell back to the article's original EN blocks. We send a flat numbered
    list of the EN strings to a small focused call and splice the Russian
    translations back, preserving block structure (``src`` URLs etc.).

    ``skip_patched_text=True`` (default in the variant-B+ flow) skips the
    ``text`` field on ``lead`` / ``paragraph`` / ``heading`` blocks because
    those are already filled in with RU strings by
    ``_patch_text_with_ru_paragraphs``; without the skip we'd ask the
    model to "translate" already-RU text, wasting tokens. Tests that
    call this helper directly (without a prior patch) should pass
    ``skip_patched_text=False`` for the legacy contract.

    Always returns a list — on any failure, returns ``blocks`` unchanged
    so the article still publishes (with EN captions, same as plain B).
    """
    if not isinstance(blocks, list) or not blocks:
        return blocks

    items: list[tuple[int, str, str]] = []  # (block_idx, field, en_text)
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
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _BLOCK_TRANSLATE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout_s,
        )
        text = response.choices[0].message.content
        parsed = json.loads(text or "{}")
        translations = parsed.get("translations")
    except Exception as exc:  # noqa: BLE001 — keep EN on any failure
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
    """Map an OpenAI-SDK exception (raised against the OpenRouter base URL)
    to our outage / per-article axis. Same mapping as openai_transcreation
    since OpenRouter is OpenAI-compatible."""
    if isinstance(exc, (ClaudeTranscreationError, ClaudeOutageError)):
        return exc

    if isinstance(exc, (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.NotFoundError,
        openai.InternalServerError,
        openai.ConflictError,
    )):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, (
        openai.BadRequestError,
        openai.UnprocessableEntityError,
    )):
        return ClaudeTranscreationError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, openai.APIStatusError):
        return ClaudeTranscreationError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, openai.APIError):
        return ClaudeOutageError(f"{type(exc).__name__}: {exc}")

    return exc  # type: ignore[return-value]


def is_outage_error(exc: BaseException) -> bool:
    return isinstance(exc, ClaudeOutageError)


def is_per_article_error(exc: BaseException) -> bool:
    return isinstance(exc, ClaudeTranscreationError)


# --------------------------------------------------------------------------- #
# Client lifecycle                                                            #
# --------------------------------------------------------------------------- #


def _get_default_client() -> "openai.OpenAI":
    """Lazily build an ``openai.OpenAI`` client pointed at OpenRouter's base URL.

    Accepts the API key from either ``OPENROUTER_API_KEY`` (canonical, per
    OpenRouter docs) or ``OPEN_ROUTER_API_KEY`` (alias — typo-tolerance for
    operators who add the underscore).
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY (or alias OPEN_ROUTER_API_KEY) env var not set"
            )
        base_url = (
            os.getenv("OPENROUTER_BASE_URL")
            or os.getenv("OPEN_ROUTER_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        _DEFAULT_CLIENT = openai.OpenAI(api_key=api_key, base_url=base_url)
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
    client: Optional["openai.OpenAI"] = None,
) -> dict:
    """Transcreate one article via OpenRouter.

    The ``model`` parameter is forwarded as-is to OpenRouter (e.g.
    ``"openai/gpt-4o-mini"``, ``"anthropic/claude-haiku-4-5"``,
    ``"google/gemini-2.5-flash"``).
    """
    paragraphs_in = article.get("paragraphs") or []
    blocks_in = article.get("blocks")
    expected_paragraph_count = len(paragraphs_in)
    expected_block_count = len(blocks_in) if isinstance(blocks_in, list) else None

    if model is None:
        model = os.getenv("OPENROUTER_MODEL", _DEFAULT_MODEL)

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
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout_s,
        )
    except (ClaudeOutageError, ClaudeTranscreationError):
        raise
    except openai.OpenAIError as exc:
        raise _classify_exception(exc) from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        text = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ClaudeTranscreationError(
            f"OpenRouter response shape unexpected: {exc}"
        ) from exc

    if not text:
        raise ClaudeTranscreationError("OpenRouter response.content is empty")

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    response_model = getattr(response, "model", model)
    logger.info(
        "openrouter_transcreation: model=%s input_tokens=%s output_tokens=%s "
        "latency_ms=%s",
        response_model,
        input_tokens,
        output_tokens,
        latency_ms,
    )

    parsed = _parse_response(text, expected_paragraph_count, expected_block_count)
    parsed["title"] = _apply_emoji_safety_net(parsed["title"])
    parsed["paragraphs"] = _truncate_paragraphs(parsed["paragraphs"])

    # Variant B: if the model didn't return matching blocks (the
    # ``_parse_response`` softener nulled them out for us), fall back
    # to the article's original EN blocks so galleries / videos /
    # image structure survive on the Telegraph page. autoevolution_source
    # already runs ``filter_blocks`` on these so no ad / social-share
    # content leaks through.
    if expected_block_count and not parsed.get("blocks"):
        # Variant B: substitute the original EN blocks, but FIRST patch
        # paragraph/lead/heading text with the RU translations the main
        # call already produced — so the article body is always Russian
        # even if the second-pass call fails on a long article.
        parsed["blocks"] = _patch_text_with_ru_paragraphs(
            blocks_in, parsed["paragraphs"],
        )
        logger.info(
            "Using original EN blocks (count=%d, paragraph text spliced "
            "from main response)", expected_block_count,
        )
        # Variant B+: focused second pass to translate captions (and any
        # remaining text). Captions stay EN as a controlled degradation
        # if this fails; paragraphs are already RU from the patch above.
        parsed["blocks"] = _translate_block_strings(
            parsed["blocks"], client, model, timeout_s=timeout_s,
        )

    return parsed


# --------------------------------------------------------------------------- #
# Health check                                                                #
# --------------------------------------------------------------------------- #


def health_check(client: Optional["openai.OpenAI"] = None) -> bool:
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
        # max_tokens=200 (not 10) because OpenRouter routes some models
        # through reasoning-style providers (GPT-5 family, Claude thinking
        # mode, DeepSeek R1, etc.) which need budget for an internal CoT
        # token allocation BEFORE producing visible output. Tiny budgets
        # round-trip a 400 BadRequestError on those routes. 200 is enough
        # to fit a reasoning preamble + a one-word reply across all
        # providers; cost is negligible (~$0.001 per probe).
        client.chat.completions.create(
            model=os.getenv("OPENROUTER_MODEL", _DEFAULT_MODEL),
            messages=[
                {"role": "system", "content": "You are a health probe."},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=200,
            timeout=15,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: probe failed: %s: %s", type(exc).__name__, exc)
        return False
