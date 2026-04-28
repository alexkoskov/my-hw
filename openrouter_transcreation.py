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
_DEFAULT_MAX_TOKENS = 8000

#: Default model spec; "provider/model" form. Override via OPENROUTER_MODEL.
#: GPT-5.5 is the latest non-pro OpenAI model on OpenRouter.
_DEFAULT_MODEL = "openai/gpt-5.5"

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
  "paragraphs": ["<RU paragraph 1>", "<RU paragraph 2>", ...],
  "blocks": [{"type": "...", "text": "<RU>", "caption": "<RU>"}, ...] | null
}

The output JSON MUST contain `paragraphs` of EXACTLY the same length as
the input EN paragraphs, in the same order. Do not merge or split. If a
block was provided, the `blocks` array must mirror its length and types.
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
        raise ClaudeTranscreationError(
            f"OpenRouter response paragraph count mismatch: "
            f"expected {expected_paragraph_count}, got {len(paragraphs)}"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            raise ClaudeTranscreationError(
                f"OpenRouter response 'blocks' length mismatch: "
                f"expected {expected_block_count}, got "
                f"{len(blocks) if isinstance(blocks, list) else 'non-list'}"
            )

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
        client.chat.completions.create(
            model=os.getenv("OPENROUTER_MODEL", _DEFAULT_MODEL),
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
