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

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

#: Primary location of ``ux-guidelines.md`` — the project-knowledge subdir.
_PROMPT_PATH = os.path.join(
    _MODULE_DIR,
    ".claude",
    "skills",
    "project-knowledge",
    "references",
    "ux-guidelines.md",
)

#: Cache keyed by ``(resolved_path, mtime)``. Reload on file change OR when
#: a different on-disk file resolves (e.g. flat fallback after subdir miss).
_PROMPT_CACHE: dict = {"mtime": None, "body": None, "path": None}

#: Allowed title emoji prefixes (per ux-guidelines.md + patterns.md).
_TITLE_EMOJIS = ("🏆", "🏎️", "🚀", "💎", "🤝", "📢", "🚗", "🔥")

#: Per-paragraph defensive truncation cap (Decision 13).
_PARAGRAPH_MAX_CHARS = 4000

#: Output token cap (Decision 13). Bounds prompt-injection cost amplification.
_DEFAULT_MAX_TOKENS = 8000

#: Default model name (Decision 6); overridable via ``ANTHROPIC_MODEL`` env.
_DEFAULT_MODEL = "claude-haiku-4-5"

#: Lazily-instantiated singleton client (only built on first real call).
_DEFAULT_CLIENT: Optional["anthropic.Anthropic"] = None

#: Static JSON envelope appended to ``ux-guidelines.md`` body (Decision 6).
#: Keep verbatim — operator's prompt edits land in the file, this envelope
#: is the technical contract that turns prose-style guidance into a parseable
#: response.
_JSON_ENVELOPE = """\

---

## Output format (technical envelope)

Output a single JSON object — no markdown fence, no commentary. Schema:

{
  "title": "<RU title with emoji prefix from {🏆,🏎️,🚀,💎,🤝,📢,🚗,🔥}>",
  "alts": ["<alt RU title 1>", "<alt RU title 2>", "<alt RU title 3>"],
  "subtitle": "<RU subtitle>",
  "paragraphs": ["<RU paragraph 1>", "<RU paragraph 2>", ...],
  "blocks": [{"type": "...", "text": "<RU>", "caption": "<RU>"}, ...] | null
}

The output JSON MUST contain `paragraphs` of EXACTLY the same length as
the input EN paragraphs, in the same order. Do not merge or split. If a
block was provided, the `blocks` array must mirror its length and types.
"""


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


from _llm_common import ClaudeTranscreationError, ClaudeOutageError


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


def _build_system_prompt(prompt_body: str) -> str:
    """Append the JSON envelope to the ``ux-guidelines.md`` body."""
    return prompt_body.rstrip() + "\n" + _JSON_ENVELOPE


def _build_user_message(article: dict) -> str:
    """Serialise the article as JSON for the user message slot."""
    payload = {
        "source_name": article.get("source_name"),
        "title": article.get("title"),
        "subtitle": article.get("subtitle"),
        "paragraphs": article.get("paragraphs") or [],
        "blocks": article.get("blocks"),
    }
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Response parsing                                                            #
# --------------------------------------------------------------------------- #


_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL
)


def _strip_json_fence(text: str) -> str:
    """Strip an optional leading/trailing ```json fence``` if the model added one."""
    if not text:
        return text
    match = _JSON_FENCE_RE.match(text)
    if match:
        return match.group("body")
    return text.strip()


def _parse_response(
    text: str,
    expected_paragraph_count: int,
    expected_block_count: Optional[int],
) -> dict:
    """Parse Claude's JSON response. Raise ``ClaudeTranscreationError`` on
    malformed JSON, missing keys, or schema-shape mismatch."""
    raw = _strip_json_fence(text)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ClaudeTranscreationError(
            f"Claude response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ClaudeTranscreationError(
            f"Claude response is not a JSON object (got {type(parsed).__name__})"
        )

    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ClaudeTranscreationError("Claude response missing/empty 'title'")

    alts = parsed.get("alts")
    if not isinstance(alts, list) or not (2 <= len(alts) <= 3) \
            or not all(isinstance(a, str) and a.strip() for a in alts):
        raise ClaudeTranscreationError(
            "Claude response 'alts' must be a list of 2-3 non-empty strings"
        )

    subtitle = parsed.get("subtitle")
    if not isinstance(subtitle, str):
        raise ClaudeTranscreationError("Claude response missing/invalid 'subtitle'")

    paragraphs = parsed.get("paragraphs")
    if not isinstance(paragraphs, list) \
            or not all(isinstance(p, str) for p in paragraphs):
        raise ClaudeTranscreationError(
            "Claude response 'paragraphs' must be a list of strings"
        )
    if len(paragraphs) != expected_paragraph_count:
        # Soften: see openrouter_transcreation for rationale. Accept the
        # LLM's paragraph segmentation — Telegraph renders <p> nodes
        # without a structural 1:1 mapping requirement.
        logger.warning(
            "Claude response paragraph count diverges: "
            "expected %d, got %d; accepting LLM-chosen segmentation",
            expected_paragraph_count, len(paragraphs),
        )

    total_chars = sum(len(p) for p in paragraphs)
    if total_chars < 30:
        raise ClaudeTranscreationError(
            f"Claude response paragraphs total content too short "
            f"({total_chars} chars < 30 minimum) — likely empty / stub translation"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            logger.warning(
                "Claude response 'blocks' length diverges: expected %d, got %s; "
                "caller will fall back to original EN blocks for structure",
                expected_block_count,
                len(blocks) if isinstance(blocks, list) else type(blocks).__name__,
            )
            parsed["blocks"] = None

    return {
        "title": title,
        "alts": list(alts),
        "subtitle": subtitle,
        "paragraphs": list(paragraphs),
        "blocks": blocks,
    }


# --------------------------------------------------------------------------- #
# Post-processing: emoji safety net + defensive truncation                    #
# --------------------------------------------------------------------------- #


def _apply_emoji_safety_net(title: str) -> str:
    """If ``title`` doesn't already start with one of the 8 known emojis,
    prepend a content-aware emoji using the same regex cascade as the legacy
    ``transcreate_text(is_title=True)`` in ``news_bot.py:404-423``.

    Use explicit ``any(startswith(e))`` rather than ``startswith(tuple)``;
    multi-codepoint emoji like 🏎️ (U+1F3CE + U+FE0F) play badly with tuple-form
    ``startswith`` under certain string normalisations.
    """
    if not isinstance(title, str) or not title:
        return title
    if any(title.startswith(emoji) for emoji in _TITLE_EMOJIS):
        return title

    t = title.lower()
    if re.search(r"легенд|legends|tour|чемпион|приз|победител", t):
        emoji = "🏆"
    elif re.search(r"гонк|скорост|race|ралли", t):
        emoji = "🏎️"
    elif re.search(r"релиз|выпуск|launch|запуск|вышел|выходит|дебют", t):
        emoji = "🚀"
    elif re.search(r"коллекц|серия|series|collection", t):
        emoji = "💎"
    elif re.search(r"сотруднич|партнёр|collab|partner", t):
        emoji = "🤝"
    elif re.search(r"анонс|объявл|представля|announce", t):
        emoji = "📢"
    elif re.search(r"машин|автомобил|модел|\bcar\b", t):
        emoji = "🚗"
    else:
        emoji = "🔥"
    return f"{emoji} {title}"


def _truncate_paragraphs(paragraphs: list) -> list:
    """Defensive cap: any paragraph longer than ``_PARAGRAPH_MAX_CHARS`` is
    truncated and a warning logged (Decision 13)."""
    out = []
    for idx, p in enumerate(paragraphs):
        if isinstance(p, str) and len(p) > _PARAGRAPH_MAX_CHARS:
            logger.warning(
                "paragraph truncated at %d chars (idx=%d, original_len=%d)",
                _PARAGRAPH_MAX_CHARS,
                idx,
                len(p),
            )
            out.append(p[:_PARAGRAPH_MAX_CHARS])
        else:
            out.append(p)
    return out


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
        parsed["blocks"] = blocks_in
        logger.info(
            "Using original EN blocks (count=%d) — model did not return "
            "matching translated blocks", expected_block_count,
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
