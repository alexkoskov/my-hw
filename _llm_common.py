"""Shared LLM transcreation helpers + exception types.

Provider-agnostic building blocks used by every per-engine module
(``claude_transcreation``, ``gemini_transcreation``, ``openai_transcreation``,
``openrouter_transcreation``). The engine modules contain only SDK-specific
glue: client lifecycle, exception classification, and the actual API call.

Exception class identity is shared so ``isinstance(exc, ClaudeOutageError)``
works regardless of which engine raised it. The "Claude" prefix is preserved
for backward compatibility with ``news_bot.py`` imports and existing tests;
semantics are provider-agnostic.

Adding a new engine
-------------------
1. Create ``<name>_transcreation.py`` mirroring an existing engine.
2. ``from _llm_common import (ClaudeOutageError, ClaudeTranscreationError,
   _PROMPT_PATH_DEFAULT, _TITLE_EMOJIS, _PARAGRAPH_MAX_CHARS, _JSON_ENVELOPE,
   _is_mostly_russian, _strip_json_fence, _apply_emoji_safety_net,
   _truncate_paragraphs, _patch_text_with_ru_paragraphs, _build_user_message,
   _build_system_prompt, _parse_response)``.
3. Implement engine-specific bits: ``_load_prompt`` (state cache),
   ``_classify_exception``, ``_get_default_client``, ``transcreate_via_claude``,
   ``health_check``.
4. Add a branch in ``llm_transcreation._select_engine``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #


class ClaudeTranscreationError(Exception):
    """Per-article LLM failure (refusal, malformed JSON, schema mismatch).

    Caller falls back to Google Translate for THIS article only;
    outage state machine NOT advanced.
    """


class ClaudeOutageError(Exception):
    """API-level LLM failure (network, 429, 5xx, auth, quota).

    Caller advances outage state machine (admin pings, 2h grace,
    then global Google fallback).
    """


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #


#: Default location of ``ux-guidelines.md`` inside the repo. Engines pass this
#: as the default to ``_load_prompt``; see Decision 8 (flat-path fallback) in
#: each engine's own ``_load_prompt`` for the deploy-time layout.
_PROMPT_PATH_DEFAULT_PARTS = (
    ".claude", "skills", "project-knowledge", "references", "ux-guidelines.md",
)

#: Allowed title emoji prefixes (per ux-guidelines.md + patterns.md).
_TITLE_EMOJIS = ("🏆", "🏎️", "🚀", "💎", "🤝", "📢", "🚗", "🔥")

#: Per-paragraph defensive truncation cap (Decision 13).
_PARAGRAPH_MAX_CHARS = 4000

#: Static JSON envelope appended to ``ux-guidelines.md`` body (Decision 6).
#: Operator's prompt edits land in the file; this envelope is the technical
#: contract that turns prose-style guidance into a parseable response.
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

## Inline formatting markers

If an input paragraph contains `**word or phrase**` markers, those spans
were **bold** in the original article. Preserve the markers around the
corresponding translated words in your output paragraphs — wrap the
Russian translation in `**...**`. Do not add new `**...**` markers to
spans that were not marked in the input. Plain prose is rendered
without markers.
"""

_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL
)

_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading", "list_item")


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def _build_system_prompt(prompt_body: str) -> str:
    """Append the JSON envelope to the ``ux-guidelines.md`` body."""
    return prompt_body.rstrip() + "\n" + _JSON_ENVELOPE


def _encode_format_markers(text: str, runs: list) -> str:
    """Wrap bold spans in ``text`` with ``**...**`` markers per the
    ``runs`` metadata. Returns text unchanged if no bold runs present.

    Spec contract: only ``formats == ['bold']`` style spans are
    encoded — italic/underline/strikethrough are currently dropped
    across translation. Bold covers the vast majority of editorial
    inline emphasis we see in the wild (autoevolution / orangetrack),
    and the markdown-round-trip approach degrades gracefully when an
    LLM drops a marker (the word just appears unbolded — same as
    today's behaviour for ALL formatting).

    First-wrap-wins on overlapping runs; same convention as
    telegraph_publisher._render_paragraph_with_runs at the rendering
    side, so the in-text positions agree with the eventual Telegraph
    render path.
    """
    if not runs:
        return text
    spans = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_text = run.get("text") or ""
        if not run_text.strip():
            continue
        formats = run.get("formats") or []
        if "bold" not in formats:
            continue
        pos = text.find(run_text)
        if pos < 0:
            continue
        spans.append((pos, pos + len(run_text)))
    if not spans:
        return text
    spans.sort(key=lambda s: s[0])
    accepted = []
    last_end = -1
    for start, end in spans:
        if start >= last_end:
            accepted.append((start, end))
            last_end = end
    out = []
    cursor = 0
    for start, end in accepted:
        out.append(text[cursor:start])
        out.append(f"**{text[start:end]}**")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


#: Match `**...**` bold markers in LLM output. Non-greedy body so two
#: adjacent bold spans on the same line don't merge into one capture.
#: Disallow newlines in the body — a stray unbalanced `**` shouldn't
#: swallow the rest of the paragraph.
_BOLD_MARKER_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _decode_format_markers(text: str):
    """Parse ``**bold**`` markers out of ``text``. Returns a tuple of
    ``(clean_text, runs)`` — ``clean_text`` has the markers stripped,
    ``runs`` is a list of dicts in the project's runs format
    (``{'text': '<span>', 'formats': ['bold']}``). Empty ``runs`` if
    the input has no markers.
    """
    matches = list(_BOLD_MARKER_RE.finditer(text))
    if not matches:
        return text, []
    runs = []
    parts = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor:m.start()])
        bold_text = m.group(1)
        parts.append(bold_text)
        runs.append({"text": bold_text, "formats": ["bold"]})
        cursor = m.end()
    parts.append(text[cursor:])
    return "".join(parts), runs


def _build_user_message(article: dict) -> str:
    """Serialise the article for the main translation call.

    ``blocks`` is intentionally OMITTED from the payload; captions/text
    inside blocks are translated by ``_translate_block_strings``
    (variant B+) instead. When ``blocks`` IS present in the article and
    carries inline ``runs`` metadata, the corresponding paragraphs are
    rebuilt with ``**bold**`` markdown markers so the LLM can preserve
    them in the translated output (decoded back to runs by
    ``_patch_text_with_ru_paragraphs``).
    """
    paragraphs = list(article.get("paragraphs") or [])
    blocks = article.get("blocks")
    if isinstance(blocks, list) and blocks and paragraphs:
        # Walk blocks in order; for each patchable block, take the next
        # paragraph and encode bold runs into it. Mismatch in length is
        # tolerated — leftover paragraphs at the tail are sent as-is.
        marked = []
        para_iter = iter(paragraphs)
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in _PATCHED_TEXT_BLOCK_TYPES:
                continue
            try:
                p = next(para_iter)
            except StopIteration:
                break
            runs = block.get("runs") or []
            marked.append(_encode_format_markers(p, runs))
        marked.extend(para_iter)
        paragraphs = marked

    payload = {
        "source_name": article.get("source_name"),
        "title": article.get("title"),
        "subtitle": article.get("subtitle"),
        "paragraphs": paragraphs,
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_mostly_russian(paragraphs: list, threshold: float = 0.30) -> bool:
    """Heuristic: total Cyrillic letter share across all paragraphs.

    30% accommodates brand-name density (Hot Wheels, Nissan GT-R) while still
    flagging an entirely-English response. Empty / no-letter paragraphs
    return True (other validators handle empty content).
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
    *,
    engine_name: str = "LLM",
    log: Optional[logging.Logger] = None,
) -> dict:
    """Parse the LLM's JSON response. Raise ``ClaudeTranscreationError`` on
    malformed JSON, missing keys, or schema-shape mismatch.

    ``engine_name`` flavours the error message text only — useful when
    triaging ``last_error`` strings in SQLite. ``log`` controls which logger
    receives soft-divergence WARNINGs; defaults to the ``_llm_common``
    module logger.
    """
    if log is None:
        log = logger
    raw = _strip_json_fence(text)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ClaudeTranscreationError(
            f"{engine_name} response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ClaudeTranscreationError(
            f"{engine_name} response is not a JSON object "
            f"(got {type(parsed).__name__})"
        )

    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ClaudeTranscreationError(
            f"{engine_name} response missing/empty 'title'"
        )

    alts = parsed.get("alts")
    if not isinstance(alts, list) or not (2 <= len(alts) <= 3) \
            or not all(isinstance(a, str) and a.strip() for a in alts):
        raise ClaudeTranscreationError(
            f"{engine_name} response 'alts' must be a list of 2-3 "
            f"non-empty strings"
        )

    subtitle = parsed.get("subtitle")
    if not isinstance(subtitle, str):
        raise ClaudeTranscreationError(
            f"{engine_name} response missing/invalid 'subtitle'"
        )

    paragraphs = parsed.get("paragraphs")
    if not isinstance(paragraphs, list) \
            or not all(isinstance(p, str) for p in paragraphs):
        raise ClaudeTranscreationError(
            f"{engine_name} response 'paragraphs' must be a list of strings"
        )
    if len(paragraphs) != expected_paragraph_count:
        # Soften: accept the LLM's paragraph segmentation — Telegraph renders
        # <p> nodes without a structural 1:1 mapping requirement.
        log.warning(
            "%s response paragraph count diverges: "
            "expected %d, got %d; accepting LLM-chosen segmentation",
            engine_name, expected_paragraph_count, len(paragraphs),
        )

    # Sanity floor: total translated content must be at least 30 chars —
    # but only when the input had ≥2 paragraphs. Single-paragraph posts
    # (t-hunted photo-gallery format: 1 short intro + N product photos)
    # legitimately produce a thin LLM body — sometimes the model treats
    # the lone marketing intro as boilerplate and returns paragraphs=[]
    # while filling title/alts/subtitle. For those posts the visual
    # payload (title + hero + gallery) carries the article, so we accept
    # the thin output instead of striking the slot 3 times (incident
    # 2026-05-31 with all 4 t-hunted slots failing this check).
    if expected_paragraph_count >= 2:
        total_chars = sum(len(p) for p in paragraphs)
        if total_chars < 30:
            raise ClaudeTranscreationError(
                f"{engine_name} response paragraphs total content too short "
                f"({total_chars} chars < 30 minimum) — likely empty / stub translation"
            )

    # Reject EN-leaking responses (translation silently skipped).
    if not _is_mostly_russian(paragraphs):
        raise ClaudeTranscreationError(
            f"{engine_name} response paragraphs appear to be English — "
            f"translation likely skipped (Cyrillic letter share below 30%)"
        )

    blocks = parsed.get("blocks")
    if expected_block_count is not None:
        if not isinstance(blocks, list) or len(blocks) != expected_block_count:
            log.warning(
                "%s response 'blocks' length diverges: expected %d, got %s; "
                "caller will fall back to original EN blocks for structure",
                engine_name,
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


def _apply_emoji_safety_net(title: str) -> str:
    """If ``title`` doesn't already start with one of the 8 known emojis,
    prepend a content-aware emoji (same regex cascade as legacy
    ``transcreate_text(is_title=True)``).

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


def _patch_text_with_ru_paragraphs(blocks_in: list, ru_paragraphs: list) -> list:
    """Splice RU paragraph text into matching EN structural blocks
    (lead / paragraph / heading) by sequential consumption of
    ``ru_paragraphs``. Non-matching blocks pass through unchanged.

    Used by variant B fallback when the model returns ``blocks: null``
    on long block-shaped articles — preserves structural completeness
    while letting the main-call paragraph translations land in the
    Telegraph render.

    ``**bold**`` markers in each RU paragraph are decoded into the
    block's ``runs`` metadata so the Telegraph renderer can wrap the
    translated bold spans in ``<strong>``. When the LLM drops markers
    (or none were present on input) the block's old EN-text runs are
    cleared — they reference English substrings that don't appear in
    the Russian translation anyway, so the renderer would have dropped
    them silently in any case.
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
        if block.get("type") in _PATCHED_TEXT_BLOCK_TYPES:
            try:
                ru = next(paragraphs_iter)
                if isinstance(ru, str) and ru.strip():
                    clean_text, new_runs = _decode_format_markers(ru)
                    new_block["text"] = clean_text
                    # Replace EN-text runs with RU-text runs (or drop the
                    # field entirely when no markers came back, so the
                    # renderer skips the whole format machinery).
                    if new_runs:
                        new_block["runs"] = new_runs
                    elif "runs" in new_block:
                        del new_block["runs"]
            except StopIteration:
                pass
        result.append(new_block)
    return result


def _resolve_prompt_path(module_dir: str) -> str:
    """Build the default ux-guidelines.md path for an engine module.

    Engines pass ``os.path.dirname(os.path.abspath(__file__))`` and get back
    the subdir-style path (the ``_load_prompt`` body in each engine handles
    the flat-fallback for production deploys).
    """
    return os.path.join(module_dir, *_PROMPT_PATH_DEFAULT_PARTS)
