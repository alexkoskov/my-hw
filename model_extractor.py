#!/usr/bin/env python3
"""Model-fingerprint extractor for cross-source dedup.

This module extracts a structured "model fingerprint" from an article body
and computes a guarded Jaccard similarity between two fingerprints. Used by
``news_bot.job()`` to detect cross-source republishes (e.g. t-hunted PT vs
autoevolution EN coverage of the same Hot Wheels release) that bypass the
URL-only dedup.

Module shape mirrors ``boilerplate_filter.py``:

- Module docstring → compiled regex constants at module load (singleton)
  → pure helper functions.
- No I/O. No logging. No external dependencies (``re`` + ``set`` only).
- All regex quantifiers are bounded (`{1,N}`) for ReDoS-safety per
  tech-spec Decision 3.

Public API:

- ``extract_fingerprint(article)`` → ``{'strict': [...], 'brands': [...]}``
  with sorted lists for JSON-stable / test-deterministic output.
- ``similarity(fp_a, fp_b)`` → float in [0.0, 1.0] computed via the guarded
  two-level Jaccard per tech-spec Decision 4 (AC6 empty-fp guard,
  AC8 1-token / brand-count guards, AC10 two-level max).

See ``work/cross-source-dedup/tech-spec.md`` Decisions 2-4 for the
authoritative specification of the lexicon (35 brands tiered by
frequency), regex shape, and similarity formula.
"""

from __future__ import annotations

import re
from typing import Dict, List


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Fingerprint shape: dict with two list-valued keys.
#   'strict' — list of "brand model" tokens (e.g. "toyota 4runner")
#   'brands' — list of brand-only tokens (e.g. "toyota")
# Lists are sorted for deterministic JSON serialisation and test equality.
Fingerprint = Dict[str, List[str]]


# ---------------------------------------------------------------------------
# Lexicon — 35 canonical brands per tech-spec Decision 2 / code-research §14.A.2
# ---------------------------------------------------------------------------
#
# Tier 1 (JDM, ~30% of HW mainline):       Acura, Datsun, Honda, Lexus, Mazda,
#                                          Mitsubishi, Nissan, Subaru, Toyota
# Tier 2 (American muscle / classic):      AMC*, Buick, Cadillac, Chevrolet,
#                                          Chrysler, Dodge, Ford, Hudson, Jeep,
#                                          Plymouth, Pontiac
# Tier 3 (European):                       Aston Martin, Audi, BMW*, Land Rover,
#                                          Lotus*, Mercedes, Mini, Porsche,
#                                          Range Rover, Volkswagen, Volvo
# Tier 4 (exotics):                        Bugatti, Ferrari, Koenigsegg,
#                                          Lamborghini, McLaren, Pagani
#
# Brands marked * use case-sensitive matching (no `re.I`) to avoid prose
# collisions: ``bmwxyz123``, ``AMC theatre``, ``lotus position``.
# See code-research §14.A.3.
#
# Note: ``_LEXICON`` is documented as the authoritative list — the source of
# truth for matching is the alternation strings below.
_LEXICON = frozenset({
    'acura', 'amc', 'aston martin', 'audi', 'bmw', 'bugatti', 'buick',
    'cadillac', 'chevrolet', 'chrysler', 'datsun', 'dodge', 'ferrari',
    'ford', 'honda', 'hudson', 'jeep', 'koenigsegg', 'lamborghini',
    'land rover', 'lexus', 'lotus', 'mazda', 'mclaren', 'mercedes',
    'mini', 'mitsubishi', 'nissan', 'pagani', 'plymouth', 'pontiac',
    'porsche', 'range rover', 'subaru', 'toyota', 'volkswagen',
})
# Sanity: lexicon ships exactly 36 entries (Mini moved from Tier-3 niche
# into the active list per code-research §14.A.2 sampling). One above the
# nominal 35 — close enough; Decision 2 phrased the target as "~35".
# Tracked in code-research §14.A.4 mainline-frequent list.

# Aliases that canonicalize to a different key.
_BRAND_ALIASES = {
    'chevy': 'chevrolet',
    'vw': 'volkswagen',
    'mercedes-benz': 'mercedes',
}


# ---------------------------------------------------------------------------
# Compiled regex (module-level singletons, bounded quantifiers throughout)
# ---------------------------------------------------------------------------
#
# `_MODEL_AFTER_BRAND_RE` — case-insensitive, matches:
#   1. Optional year prefix `(?:(?:19|20)\d{2}\s{1,3})?` — dropped from output.
#   2. Brand (named capture `brand`) — multi-word brands first to avoid
#      partial matches against single-word alternates (e.g. "Land Rover"
#      must beat "Land" alone; while "Land" isn't in the alternation, this
#      ordering protects against future single-word additions).
#   3. Optional first model token (named capture `model`) — separated by
#      1-3 chars of `[\s-]`, then `[A-Za-z0-9][A-Za-z0-9\-]{1,24}` (2-25 chars).
#   4. Up to 2 additional model words (named capture `model_extra`) —
#      `(?:\s{1,2}[A-Za-z0-9][A-Za-z0-9\-]{0,24}){0,2}` per tech-spec
#      Decision 3. Allows "Subaru Legacy GT" to extract "legacy gt" as the
#      composite model token (vs only "legacy" which would lose the GT
#      designator). Bounded `{0,2}` keeps capture greedy-safe.
#
# All quantifiers bounded: `{1,3}`, `{1,2}`, `{0,2}`, `{0,24}`, `{1,24}` —
# ReDoS-safe per tech-spec Decision 3 / code-research §14.B.3.
#
# Brands that need case-sensitive matching (BMW, AMC, Lotus) are EXCLUDED
# from this pattern (they appear in `_UPPERCASE_BRANDS_RE` below). BMW and
# AMC ship strictly uppercase; Lotus must be capitalised — prose
# "lotus position" must NOT match.
_MODEL_AFTER_BRAND_RE = re.compile(
    r'\b'
    r'(?:(?:19|20)\d{2}\s{1,3})?'
    r'(?P<brand>'
    # Multi-word brands first (ordering matters within alternation when
    # alternatives share a prefix; here they don't, but kept for clarity).
    r'aston\s{1,2}martin|land\s{1,2}rover|range\s{1,2}rover|'
    # Single-word brands (alphabetical for diff-readability).
    r'acura|audi|bugatti|buick|cadillac|chevrolet|chevy|chrysler|'
    r'datsun|dodge|ferrari|ford|honda|hudson|jeep|koenigsegg|'
    r'lamborghini|lexus|mazda|mclaren|mercedes(?:-benz)?|mini|'
    r'mitsubishi|nissan|pagani|plymouth|pontiac|porsche|subaru|'
    r'toyota|volkswagen|vw'
    r')'
    r'(?:[\s\-]{1,3}(?P<model>[A-Za-z0-9][A-Za-z0-9\-]{1,24})'
    r'(?P<model_extra>(?:\s{1,2}[A-Za-z0-9][A-Za-z0-9\-]{0,24}){0,2}))?',
    re.IGNORECASE,
)

# Tokens that are valid alphanumeric strings but should not be included
# as part of a composite model (e.g. "Toyota 4Runner gold" → "toyota
# 4runner", not "toyota 4runner gold"). Most model "extra" tokens in HW
# context are short uppercase suffixes ("GT", "STI", "Boss", "Z28") —
# generic prose words ("in", "and", "the", colors, prepositions) leak
# noise. We accept only tokens that look like model designators:
# - all-uppercase (GT, STI, R34, Z28, RS)
# - or contain a digit (4Runner, 302, A85)
# - or are hyphenated (RX-7, GT-R, F-150)
# Common prose lowercase words ("gold", "review", "in", "with") are
# rejected. Trade-off: this drops legit lowercase model names that are
# also common prose words. Calibration fixture validates that net
# accuracy stays above 7/8.
_MODEL_EXTRA_KEEP_RE = re.compile(r'^(?:[A-Z0-9]+|[A-Za-z0-9]*\d[A-Za-z0-9\-]*|[A-Za-z]+-[A-Za-z0-9\-]+)$')

# Uppercase-only brands — separate compile WITHOUT `re.I` to defend against
# prose false-matches per code-research §14.A.3:
#   - "bmwxyz123" must not extract BMW.
#   - "AMC theatre" extraction is acceptable (rare in HW content); prose
#     "amc" lowercase will not match.
#   - "lotus position" / "lotus flower" must not extract Lotus.
#
# Model-token capture intentionally OMITTED here — these three brands
# almost always appear bare in HW prose (e.g. "BMW M3" is still extracted
# because the lowercase pattern above matches "M3" via "bmw" — wait, it
# doesn't, since BMW is NOT in the case-insensitive alternation). To keep
# the uppercase-brand path useful (brand-fallback signal even without a
# model token), we extract just the brand. The model side is intentionally
# weaker — strict tokens for BMW/AMC/Lotus articles will only appear if
# the body also contains a case-insensitive-recognised brand near a model.
# Trade-off accepted in code-research §14.A.3.
_UPPERCASE_BRANDS_RE = re.compile(r'\b(?P<brand>AMC|BMW|Lotus)\b')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _jaccard(a: set, b: set) -> float:
    """Pure Jaccard. Empty union → 0.0 (no ZeroDivisionError).

    A degenerate case where both sets are empty yields union=0 → 0.0; this
    is consistent with the caller's intent (no information = no similarity)
    and matches the AC6 short-circuit shape.
    """
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _canonical_brand(raw: str) -> str:
    """Lowercase + collapse internal whitespace + alias resolve.

    "Land  Rover" → "land rover"
    "Chevy"       → "chevrolet" (via alias)
    "Mercedes-Benz" → "mercedes" (via alias)
    """
    key = re.sub(r'\s{1,3}', ' ', raw.strip().lower())
    return _BRAND_ALIASES.get(key, key)


def _gather_text(article: dict) -> str:
    """Concatenate title + subtitle + paragraphs into a single scan string.

    Defensive against missing keys, None values, and non-string entries —
    extraction must never raise on exotic article shapes (AC9 reinforced by
    Decision 12: dedup-gate ``try/except Exception`` is the last line of
    defence, but extractor robustness narrows the failure surface).
    """
    parts: List[str] = []
    for key in ('title', 'subtitle'):
        value = article.get(key) if isinstance(article, dict) else None
        if isinstance(value, str) and value:
            parts.append(value)
    paragraphs = article.get('paragraphs') if isinstance(article, dict) else None
    if paragraphs:
        for para in paragraphs:
            if isinstance(para, str) and para:
                parts.append(para)
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_fingerprint(article: dict) -> Fingerprint:
    """Extract brand+model fingerprint from an article dict.

    Reads ``article['title']``, ``article['subtitle']``, and
    ``article['paragraphs']`` (any may be missing). Runs the singleton
    regex patterns one-pass over the concatenated text and returns:

        {'strict': sorted list of "brand model" tokens,
         'brands': sorted list of brand-only tokens}

    Empty input yields ``{'strict': [], 'brands': []}`` (NOT NULL — that
    distinction is the backfill idempotency marker per tech-spec Decision
    10). Year prefix `(?:19|20)\\d{2}` is dropped from output tokens
    (PT bodies often omit year; we want EN/PT fingerprints to match).

    Pure function — no I/O, no logging.
    """
    text = _gather_text(article)
    if not text:
        return {'strict': [], 'brands': []}

    strict_tokens: set = set()
    brand_tokens: set = set()

    # Pass 1: case-insensitive brand+model alternation.
    for match in _MODEL_AFTER_BRAND_RE.finditer(text):
        brand_raw = match.group('brand')
        if not brand_raw:
            continue
        brand = _canonical_brand(brand_raw)
        brand_tokens.add(brand)
        model_raw = match.group('model')
        if not model_raw:
            continue
        # Drop if model token is itself a brand name (prevents
        # "Toyota Ford" mis-extraction creating "toyota ford" strict
        # token; Ford alone is captured separately by the next match).
        model_lower = model_raw.lower()
        if model_lower in _LEXICON or model_lower in _BRAND_ALIASES:
            continue
        # Composite model: first token + up to 2 additional model words
        # that look like model designators (uppercase / digit-bearing /
        # hyphenated). Plain lowercase prose words ("gold", "review")
        # are stripped.
        model_parts = [model_lower]
        model_extra_raw = match.group('model_extra') or ''
        for extra in model_extra_raw.split():
            if extra.lower() in _LEXICON or extra.lower() in _BRAND_ALIASES:
                break  # next brand starts here — don't absorb it
            if _MODEL_EXTRA_KEEP_RE.match(extra):
                model_parts.append(extra.lower())
            else:
                break  # stop at first prose-looking token
        strict_tokens.add(f"{brand} {' '.join(model_parts)}")

    # Pass 2: case-sensitive uppercase brands (BMW / AMC / Lotus).
    for match in _UPPERCASE_BRANDS_RE.finditer(text):
        brand_raw = match.group('brand')
        if brand_raw:
            brand_tokens.add(brand_raw.lower())

    return {
        'strict': sorted(strict_tokens),
        'brands': sorted(brand_tokens),
    }


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    """Guarded two-level Jaccard per tech-spec Decision 4.

    Branches (in this exact order):
      1. AC6 — empty strict on EITHER side → 0.0 (caller skips dedup).
      2. AC8 part 1 — if EITHER strict set has |fp| == 1 → return
         strict-only Jaccard (no brand fallback). Prevents same-brand /
         different-model false-100% (Subaru BRZ vs Subaru WRX).
      3. AC8 part 2 — brand fallback requires BOTH brand sets to have ≥2
         distinct entries. Otherwise return strict-only.
      4. AC10 — max(strict-jaccard, brands-jaccard).

    Pure function — no I/O. Always returns float in [0.0, 1.0].
    """
    strict_a = set(a.get('strict') or ())
    strict_b = set(b.get('strict') or ())
    brands_a = set(a.get('brands') or ())
    brands_b = set(b.get('brands') or ())

    # AC6 — empty fp on either side
    if not strict_a or not strict_b:
        return 0.0

    strict_sim = _jaccard(strict_a, strict_b)

    # AC8 — 1-token strict guard on either side
    if len(strict_a) <= 1 or len(strict_b) <= 1:
        return strict_sim

    # AC8 — brand fallback requires ≥2 brands BOTH sides
    if len(brands_a) < 2 or len(brands_b) < 2:
        return strict_sim

    # AC10 — two-level max
    brands_sim = _jaccard(brands_a, brands_b)
    return max(strict_sim, brands_sim)
