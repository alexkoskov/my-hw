#!/usr/bin/env python3
"""UI-boilerplate filter for source-parser paragraph lists.

Source pages from autoevolution / lamley / Mattel embed social-share
widgets ("Share on Facebook", "Tweet", "Subscribe", ...) that bleed
through to the article body when we walk ``<p>`` / ``<li>`` tags. These
short labels:

- waste Google Translate calls in the auto-fallback path,
- pollute the manual-review screen so Claude/operator must mentally skip
  them, and
- end up on the published Telegraph page as broken UI leftovers.

This module exposes two helpers each parser calls just before returning:

- ``is_boilerplate(text)`` — single-paragraph predicate.
- ``filter_boilerplate(paragraphs)`` — drop boilerplate, preserve order.

Patterns are length-bounded (<= ``_MAX_BOILERPLATE_LEN`` chars) so a long
sentence that happens to mention "Share on Facebook" inline is kept as
real content. English patterns are primary (sources are English);
Russian patterns are a belt-and-suspenders safety net in case translated
text ever flows through this filter.
"""

from __future__ import annotations

import re
from typing import Iterable, List

# Length threshold: only filter "short" paragraphs (boilerplate is usually a
# label / button text). A long paragraph that happens to mention "Share on
# Facebook" inline is real content — keep it.
# Bumped 80 → 120 in the author-plug-filter feature so longer parenthesised
# plugs like "(follow me on Instagram for the latest reveals @diecast215)"
# (~80–110 chars) fit under the threshold.
_MAX_BOILERPLATE_LEN = 120

# Platforms covered by author-plug patterns (variant A and B of the
# author-plug-filter feature). Single tuple keeps the alternation in sync
# across patterns. Threads / OnlyFans intentionally out of scope (rare in HW
# articles).
_PLUG_PLATFORMS = (
    'instagram', 'twitter', 'x', 'tiktok', 'youtube',
    'facebook', 'reddit', 'patreon', 'discord', 'linktree',
)
_PLATFORMS_RE = '|'.join(_PLUG_PLATFORMS)

# Each pattern is matched against the WHOLE stripped paragraph,
# case-insensitive. Match → drop the paragraph.
_BOILERPLATE_PATTERNS = [
    # English — primary, since all three source sites are English-language.
    re.compile(
        r'^share\s+(on|via|to)\s+'
        r'(facebook|twitter|x|linkedin|pinterest|whatsapp|telegram|email|reddit)\b',
        re.I,
    ),
    re.compile(r'^(tweet|pin it|pin on pinterest)$', re.I),
    re.compile(r'^email this( article)?$', re.I),
    re.compile(r'^copy (link|url|article url)$', re.I),
    re.compile(r'^subscribe( to (our )?newsletter)?$', re.I),
    re.compile(r'^(related (articles?|posts?)|see also|you may also like)$', re.I),
    re.compile(r'^read more[:\s]*$', re.I),
    re.compile(r'^(tags?|filed under|categories?):', re.I),
    re.compile(r'^comments?$', re.I),
    # ------------------------------------------------------------------
    # Author social-media plugs (author-plug-filter feature, variant A).
    # Drops standalone-paragraph plugs at every source parser. Inline
    # plugs embedded inside larger paragraphs are handled by variant B
    # (post-LLM) in author_plug_filter.py.
    # ------------------------------------------------------------------
    # A1 — umbrella: "follow|check|subscribe to me/us on <platform>".
    # Replaces and supersedes the legacy "^follow us on \w+" pattern.
    re.compile(
        r'^(follow|check|subscribe\s+to)\s+(me|us)\s+on\s+(' + _PLATFORMS_RE + r')\b',
        re.I,
    ),
    # A2 — parenthesised umbrella with mandatory @handle.
    # Catches the canonical leak shape "(follow me on Instagram @diecast215)".
    re.compile(
        r'^\(\s*(follow|check|subscribe\s+to|join)\s+(me|us)\s+on\s+'
        r'(' + _PLATFORMS_RE + r')\s+@\w{2,30}\s*\)$',
        re.I,
    ),
    # A3 — "Platform: handle" shape, with or without @.
    re.compile(
        r'^(' + _PLATFORMS_RE + r')\s*:\s*@?[\w./_-]+\s*$',
        re.I,
    ),
    # A4 — orphan handle on its own line (multi-line plug fallback).
    re.compile(r'^@\w{2,30}$', re.I),
    # A5 — "subscribe to my <feed>" (author form, distinct from the "our"
    # newsletter pattern above).
    re.compile(
        r'^subscribe\s+to\s+my\s+'
        r'(channel|newsletter|patreon|youtube|page|feed)\b',
        re.I,
    ),
    # ------------------------------------------------------------------
    # Russian — defence in depth in case translated text ever reaches us.
    re.compile(
        r'^поделит(ь(ся)?|есь)\s+(на|в|через)\s+'
        r'(facebook|twitter|x|вконтакте|whatsapp|telegram|email)\b',
        re.I,
    ),
    re.compile(r'^твитнуть$', re.I),
    re.compile(r'^подписать(ся|есь)( на (нашу )?рассылку)?$', re.I),
    re.compile(r'^(читайте|смотрите) (также|далее)$', re.I),
    re.compile(r'^(тэги|теги|категории|метки):', re.I),
    re.compile(r'^комментари(и|й)$', re.I),
]


def is_boilerplate(text: str) -> bool:
    """Return True if *text* is a short standalone UI label that should be stripped.

    Empty / whitespace-only strings return False — they are not boilerplate
    in the UI-leftover sense; callers usually drop or keep them by their
    own logic. Anything longer than ``_MAX_BOILERPLATE_LEN`` is treated as
    real prose even if a trigger phrase appears at the start.
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or len(s) > _MAX_BOILERPLATE_LEN:
        return False
    for pat in _BOILERPLATE_PATTERNS:
        if pat.search(s):
            return True
    return False


def filter_boilerplate(paragraphs: Iterable[str]) -> List[str]:
    """Drop paragraphs identified as boilerplate. Preserve order of the rest."""
    return [p for p in paragraphs if not is_boilerplate(p)]


def filter_blocks(blocks: Iterable[dict]) -> List[dict]:
    """Drop content blocks whose ``text`` / ``caption`` are pure UI
    boilerplate (ads, social-share, "Subscribe" labels). Mirror of
    ``filter_boilerplate`` but for the structured-block representation
    used by autoevolution articles (lead / paragraph / image / video).

    Decision rules per block:

    * Non-dict / empty entries → drop.
    * Block with media (``src`` or ``image_url`` set) → KEEP regardless
      of caption text. We never want to lose visual content; a short
      caption like "1995 Honda NSX" must not look like boilerplate.
      Only drop a media block if its caption matches a boilerplate
      pattern AND ``text`` is empty (rare — represents an ad slot
      that happened to ship with a placeholder image).
    * Pure-text block (no media, just ``text``) → drop if
      ``is_boilerplate(text)`` matches. Same rule as
      ``filter_boilerplate`` for paragraph strings.
    * Anything else → keep.

    Order preserved.
    """
    out: List[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = (block.get("text") or "").strip()
        caption = (block.get("caption") or "").strip()
        has_media = bool(block.get("src") or block.get("image_url"))

        if has_media:
            # Drop only if caption is junk AND there's no other text.
            if caption and is_boilerplate(caption) and not text:
                continue
            out.append(block)
            continue

        # Pure-text block — drop if text is boilerplate (or empty AND
        # caption is boilerplate, though that combination is rare).
        if text and is_boilerplate(text):
            continue
        if not text and caption and is_boilerplate(caption):
            continue
        out.append(block)
    return out
