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
_DEFAULT_MODEL = "gemini-2.5-flash-lite"

#: Lazily-instantiated singleton client.
_DEFAULT_CLIENT: Optional["genai.Client"] = None

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
# Response parsing (identical to claude_transcreation)                        #
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
            f"Gemini response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ClaudeTranscreationError(
            f"Gemini response is not a JSON object (got {type(parsed).__name__})"
        )

    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ClaudeTranscreationError("Gemini response missing/empty 'title'")

    alts = parsed.get("alts")
    if not isinstance(alts, list) or not (2 <= len(alts) <= 3) \
            or not all(isinstance(a, str) and a.strip() for a in alts):
        raise ClaudeTranscreationError(
            "Gemini response 'alts' must be a list of 2-3 non-empty strings"
        )

    subtitle = parsed.get("subtitle")
    if not isinstance(subtitle, str):
        raise ClaudeTranscreationError("Gemini response missing/invalid 'subtitle'")

    paragraphs = parsed.get("paragraphs")
    if not isinstance(paragraphs, list) \
            or not all(isinstance(p, str) for p in paragraphs):
        raise ClaudeTranscreationError(
            "Gemini response 'paragraphs' must be a list of strings"
        )
    if len(paragraphs) != expected_paragraph_count:
        raise ClaudeTranscreationError(
            f"Gemini response paragraph count mismatch: "
            f"expected {expected_paragraph_count}, got {len(paragraphs)}"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            raise ClaudeTranscreationError(
                f"Gemini response 'blocks' length mismatch: "
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
# Post-processing (identical to claude_transcreation)                         #
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
# Exception classification (Gemini-specific mapping)                          #
# --------------------------------------------------------------------------- #


def _classify_exception(exc: BaseException) -> Exception:
    """Map a Gemini SDK exception to ``ClaudeOutageError`` / ``ClaudeTranscreationError``.

    google-genai exception hierarchy:
        APIError (base)
          ├── ClientError  — 4xx responses
          └── ServerError  — 5xx responses

    Mapping:
        ClientError (429 quota / rate limit, 401/403 auth) → outage
        ClientError (400 invalid input, 422 unprocessable)  → per-article
        ServerError (any 5xx)                               → outage
        UnknownApiResponseError                             → outage
        Generic APIError                                    → outage (conservative)
    """
    if isinstance(exc, (ClaudeTranscreationError, ClaudeOutageError)):
        return exc

    if isinstance(exc, genai_errors.ClientError):
        # ClientError exposes the status code as ``.code`` attribute.
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (400, 422):
            return ClaudeTranscreationError(f"{type(exc).__name__}: {exc}")
        # 401/403 auth, 404 model-not-found, 429 quota — all advance the state machine
        return ClaudeOutageError(f"{type(exc).__name__}({code}): {exc}")

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
