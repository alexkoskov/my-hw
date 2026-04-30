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

    ``blocks`` is intentionally OMITTED — block image URLs and caption
    strings are handled by ``_translate_block_strings`` (variant B+
    second-pass) instead. Keeping them out of the main prompt cuts
    input tokens roughly in half on long autoevolution unboxing posts
    (which were hitting the 8K output cap and producing truncated JSON).
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


def _is_mostly_russian(paragraphs: list, threshold: float = 0.30) -> bool:
    """Cheap heuristic: do at least ``threshold`` of all letter chars
    fall in the Cyrillic block (U+0400–U+04FF)?

    Hot Wheels articles always come in English, so a translated response
    must contain a meaningful share of Cyrillic. Brand / model names stay
    in Latin (Hot Wheels, Nissan GT-R, Bugatti, …) — 30% leaves headroom
    for heavy brand-name density while still flagging a response that
    silently returned the source verbatim. A 0-letter response (rare —
    digits / symbols only) returns True so downstream length validators
    catch it instead of this one.
    """
    total = 0
    cyr = 0
    for p in paragraphs:
        if not isinstance(p, str):
            continue
        for ch in p:
            if ch.isalpha():
                total += 1
                if 0x0400 <= ord(ch) <= 0x04FF:
                    cyr += 1
    if total == 0:
        return True
    return (cyr / total) >= threshold


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
            f"OpenRouter response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ClaudeTranscreationError(
            f"OpenRouter response is not a JSON object (got {type(parsed).__name__})"
        )

    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ClaudeTranscreationError("OpenRouter response missing/empty 'title'")

    alts = parsed.get("alts")
    if not isinstance(alts, list) or not (2 <= len(alts) <= 3) \
            or not all(isinstance(a, str) and a.strip() for a in alts):
        raise ClaudeTranscreationError(
            "OpenRouter response 'alts' must be a list of 2-3 non-empty strings"
        )

    subtitle = parsed.get("subtitle")
    if not isinstance(subtitle, str):
        raise ClaudeTranscreationError("OpenRouter response missing/invalid 'subtitle'")

    paragraphs = parsed.get("paragraphs")
    if not isinstance(paragraphs, list) \
            or not all(isinstance(p, str) for p in paragraphs):
        raise ClaudeTranscreationError(
            "OpenRouter response 'paragraphs' must be a list of strings"
        )
    if len(paragraphs) != expected_paragraph_count:
        # Soften: long autoevolution / lamley articles often see the model
        # merge two short adjacent paragraphs into one (or split a long
        # one) for editorial flow. We accept the LLM-chosen segmentation
        # — Telegraph just renders <p> nodes, exact 1:1 mapping is not a
        # structural requirement. Log for observability so operators
        # notice if a model starts dropping content wholesale.
        logger.warning(
            "OpenRouter response paragraph count diverges: "
            "expected %d, got %d; accepting LLM-chosen segmentation",
            expected_paragraph_count, len(paragraphs),
        )

    # Sanity floor: total translated content must be at least 30 chars.
    # Anything shorter is almost certainly a stub or empty response from
    # the model and would land in the channel as a near-blank Telegraph
    # page — operator would rather skip the article (and let the 3-strike
    # flow surface it for review) than publish garbage.
    total_chars = sum(len(p) for p in paragraphs)
    if total_chars < 30:
        raise ClaudeTranscreationError(
            f"OpenRouter response paragraphs total content too short "
            f"({total_chars} chars < 30 minimum) — likely empty / stub translation"
        )

    # Reject EN-leaking responses: if the model silently returned the
    # source paragraphs without translating, refuse to publish so the
    # article enters the 3-strike → failed_articles flow rather than
    # surfacing in the channel as English.
    if not _is_mostly_russian(paragraphs):
        raise ClaudeTranscreationError(
            "OpenRouter response paragraphs appear to be English — "
            "translation likely skipped (Cyrillic letter share below 30%)"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        # Soften: long autoevolution articles (10+ inline images / videos)
        # see the model return ``blocks: null`` rather than a 1:1 array.
        # We accept that and let the caller substitute the original
        # (filtered) EN blocks for structural completeness — captions
        # stay English, but galleries / videos / image structure
        # survive on the Telegraph page (variant B).
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            logger.warning(
                "OpenRouter response 'blocks' length diverges: "
                "expected %d, got %s; caller will fall back to original "
                "EN blocks for structure",
                expected_block_count,
                len(blocks) if isinstance(blocks, list) else type(blocks).__name__,
            )
            parsed["blocks"] = None  # signal to caller: no usable blocks

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


def _patch_text_with_ru_paragraphs(blocks_in: list, ru_paragraphs: list) -> list:
    """Splice RU paragraphs from the main response into the EN-fallback
    blocks at every ``lead`` / ``paragraph`` / ``heading`` position
    (1:1 in occurrence order — see ``autoevolution_source`` ``paragraphs``
    extraction). Image / video blocks are passed through untouched.

    This guarantees the article body publishes in Russian even if the
    variant B+ second-pass call fails or comes back partial; captions
    are still up to that second-pass call to translate (or stay EN as
    a controlled degradation).
    """
    if not isinstance(blocks_in, list) or not blocks_in:
        return blocks_in
    paragraphs_iter = iter(ru_paragraphs or [])
    result = []
    for block in blocks_in:
        if not isinstance(block, dict):
            result.append(block)
            continue
        new_block = dict(block)
        if block.get("type") in ("lead", "paragraph", "heading"):
            try:
                ru = next(paragraphs_iter)
                if isinstance(ru, str) and ru.strip():
                    new_block["text"] = ru
            except StopIteration:
                # More text-blocks than RU paragraphs (rare) — keep EN
                pass
        result.append(new_block)
    return result


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
