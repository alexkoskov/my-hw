"""OpenAI wrapper — alternate translation engine (ChatGPT family).

Mirror of ``claude_transcreation.py`` and ``gemini_transcreation.py`` with
the same public API: ``transcreate_via_claude``, ``health_check``,
``ClaudeOutageError``, ``ClaudeTranscreationError``, ``is_outage_error``,
``is_per_article_error``. The "Claude" prefix on exceptions is preserved
for backward compatibility with news_bot.py and existing tests; semantics
are provider-agnostic.

Selected via ``LLM_PROVIDER=openai`` in the environment OR auto-selected
by the dispatcher when ``OPENAI_API_KEY`` is the only LLM key present.
Dispatcher lives in ``llm_transcreation.py``.

Default model is ``gpt-4o-mini`` (fast + cheapest in OpenAI's chat lineup).
Override via ``OPENAI_MODEL`` env var.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import openai

from _llm_common import ClaudeTranscreationError, ClaudeOutageError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Module constants                                                            #
# --------------------------------------------------------------------------- #

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

_PROMPT_PATH = os.path.join(
    _MODULE_DIR,
    ".claude",
    "skills",
    "project-knowledge",
    "references",
    "ux-guidelines.md",
)

_PROMPT_CACHE: dict = {"mtime": None, "body": None, "path": None}

_TITLE_EMOJIS = ("🏆", "🏎️", "🚀", "💎", "🤝", "📢", "🚗", "🔥")

_PARAGRAPH_MAX_CHARS = 4000
#: Output token cap; 30K leaves headroom for long unboxing-style posts
#: that were truncating at 8K. See openrouter_transcreation for rationale.
_DEFAULT_MAX_TOKENS = 30000
_DEFAULT_MODEL = "gpt-4o-mini"

#: Lazily-instantiated singleton client.
_DEFAULT_CLIENT: Optional["openai.OpenAI"] = None

_JSON_ENVELOPE = """\

---

## Output format (technical envelope)

Output a single JSON object — no markdown fence, no commentary. Schema:

{
  "title": "<RU title with emoji prefix from {🏆,🏎️,🚀,💎,🤝,📢,🚗,🔥}>",
  "alts": ["<alt RU title 1>", "<alt RU title 2>", "<alt RU title 3>"],
  "subtitle": "<RU subtitle>",
  "paragraphs": ["<RU paragraph 1>", "<RU paragraph 2>", ...]
}

The output JSON MUST contain `paragraphs` of EXACTLY the same length as
the input EN paragraphs, in the same order. Do not merge or split.

Image URLs and caption strings are NOT part of this request — they are
handled by a separate downstream call. Do not output a `blocks` field.
"""


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


def _build_system_prompt(prompt_body: str) -> str:
    return prompt_body.rstrip() + "\n" + _JSON_ENVELOPE


def _build_user_message(article: dict) -> str:
    """Serialise the article for the main translation call.

    ``blocks`` is intentionally OMITTED — captions/text inside blocks
    are translated by the variant B+ second-pass in a smaller focused
    call. Removing them halves prompt tokens on long unboxing posts.
    """
    payload = {
        "source_name": article.get("source_name"),
        "title": article.get("title"),
        "subtitle": article.get("subtitle"),
        "paragraphs": article.get("paragraphs") or [],
    }
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Response parsing                                                            #
# --------------------------------------------------------------------------- #


_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL
)


def _strip_json_fence(text: str) -> str:
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
    raw = _strip_json_fence(text)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ClaudeTranscreationError(
            f"OpenAI response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ClaudeTranscreationError(
            f"OpenAI response is not a JSON object (got {type(parsed).__name__})"
        )

    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ClaudeTranscreationError("OpenAI response missing/empty 'title'")

    alts = parsed.get("alts")
    if not isinstance(alts, list) or not (2 <= len(alts) <= 3) \
            or not all(isinstance(a, str) and a.strip() for a in alts):
        raise ClaudeTranscreationError(
            "OpenAI response 'alts' must be a list of 2-3 non-empty strings"
        )

    subtitle = parsed.get("subtitle")
    if not isinstance(subtitle, str):
        raise ClaudeTranscreationError("OpenAI response missing/invalid 'subtitle'")

    paragraphs = parsed.get("paragraphs")
    if not isinstance(paragraphs, list) \
            or not all(isinstance(p, str) for p in paragraphs):
        raise ClaudeTranscreationError(
            "OpenAI response 'paragraphs' must be a list of strings"
        )
    if len(paragraphs) != expected_paragraph_count:
        # Soften: see openrouter_transcreation for rationale. Accept the
        # LLM's paragraph segmentation — Telegraph renders <p> nodes
        # without a structural 1:1 mapping requirement.
        logger.warning(
            "OpenAI response paragraph count diverges: "
            "expected %d, got %d; accepting LLM-chosen segmentation",
            expected_paragraph_count, len(paragraphs),
        )

    total_chars = sum(len(p) for p in paragraphs)
    if total_chars < 30:
        raise ClaudeTranscreationError(
            f"OpenAI response paragraphs total content too short "
            f"({total_chars} chars < 30 minimum) — likely empty / stub translation"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            logger.warning(
                "OpenAI response 'blocks' length diverges: expected %d, got %s; "
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
# Post-processing                                                             #
# --------------------------------------------------------------------------- #


def _apply_emoji_safety_net(title: str) -> str:
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


def _translate_block_strings(
    blocks: list,
    client: "openai.OpenAI",
    model: str,
    *,
    timeout_s: int = 60,
    max_tokens: int = 4000,
) -> list:
    """Second-pass translation of block ``text`` / ``caption`` fields.

    See ``openrouter_transcreation._translate_block_strings`` for full doc.
    Always returns a list — keeps EN blocks on any failure.
    """
    if not isinstance(blocks, list) or not blocks:
        return blocks

    items: list[tuple[int, str, str]] = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        for field in ("text", "caption"):
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
    """Map an OpenAI SDK exception to our outage/per-article axis.

    API-level (advance outage state machine):
        APIConnectionError, APITimeoutError, RateLimitError,
        AuthenticationError, PermissionDeniedError, NotFoundError,
        InternalServerError, ConflictError.

    Per-article (single-article Google fallback, no state change):
        BadRequestError (400), UnprocessableEntityError (422),
        APIStatusError (other 4xx — conservative).
    """
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
        # Conservative default for unrecognized status codes.
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
    """Lazily build the singleton ``openai.OpenAI`` client.

    Module import must succeed without ``OPENAI_API_KEY`` set (so test
    collection and ``news_bot`` startup health checks don't fail).
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY env var not set")
        _DEFAULT_CLIENT = openai.OpenAI(api_key=api_key)
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
    """Transcreate one article via OpenAI.

    Public API matches Claude/Gemini engines for drop-in dispatcher
    compatibility. ``client`` parameter accepts an injected
    ``openai.OpenAI`` instance for tests.
    """
    paragraphs_in = article.get("paragraphs") or []
    blocks_in = article.get("blocks")
    expected_paragraph_count = len(paragraphs_in)
    expected_block_count = len(blocks_in) if isinstance(blocks_in, list) else None

    if model is None:
        model = os.getenv("OPENAI_MODEL", _DEFAULT_MODEL)

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
            f"OpenAI response shape unexpected: {exc}"
        ) from exc

    if not text:
        raise ClaudeTranscreationError("OpenAI response.content is empty")

    # Token + latency observability (AC19).
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    response_model = getattr(response, "model", model)
    logger.info(
        "openai_transcreation: model=%s input_tokens=%s output_tokens=%s "
        "latency_ms=%s",
        response_model,
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
        parsed["blocks"] = blocks_in
        logger.info(
            "Using original EN blocks (count=%d) — model did not return "
            "matching translated blocks", expected_block_count,
        )
        # Variant B+: focused second pass to translate the EN
        # captions/text we just fell back to.
        parsed["blocks"] = _translate_block_strings(
            parsed["blocks"], client, model, timeout_s=timeout_s,
        )

    return parsed


# --------------------------------------------------------------------------- #
# Health check                                                                #
# --------------------------------------------------------------------------- #


def health_check(client: Optional["openai.OpenAI"] = None) -> bool:
    """Cheap probe: True iff OpenAI reachable AND ux-guidelines.md loadable.
    Never raises."""
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
        client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", _DEFAULT_MODEL),
            messages=[
                {"role": "system", "content": "You are a health probe."},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=10,
            timeout=15,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("health_check: probe failed: %s", type(exc).__name__)
        return False
