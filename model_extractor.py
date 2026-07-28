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

- ``extract_fingerprint(article)`` →
  ``{'strict': [...], 'brands': [...], 'series': [...], 'pairs': [...]}``
  with sorted lists for JSON-stable / test-deterministic output. ``series``
  holds canonical series/theme names (Hot Wheels car-lines, events, pop-culture
  franchises); ``pairs`` holds ``"<model>|<series>|<tier>"`` keys (theme-only
  variant ``"*|<series>|B"`` when no concrete model was extracted AND the series
  is a one-off franchise/event — see ``_theme_only_eligible``; a broad car-line
  or a recurring program yields NO key without a model). ``tier`` is ``D``
  (distinctive) iff the series is lexicon-tagged distinctive AND a concrete model
  exists, else ``B`` (broad) — the fail-safe polarity (dedup-model-series
  tech-spec Decision 2, amended 2026-07-28).
- ``extract_series(article_or_text)`` → sorted list of canonical series names.
- ``shares_pair(fp_a, fp_b)`` → ``(any_shared, sorted_shared_pairs,
  any_distinctive)`` — pure set-intersection of the two ``pairs`` lists;
  ``any_distinctive`` is True iff any shared key ends ``|D``.
- ``similarity(fp_a, fp_b)`` → float in [0.0, 1.0] computed via the guarded
  two-level Jaccard per tech-spec Decision 4 (AC6 empty-fp guard,
  AC8 1-token / brand-count guards, AC10 two-level max).

See ``work/cross-source-dedup/tech-spec.md`` Decisions 2-4 for the
authoritative specification of the brand lexicon (35 brands tiered by
frequency), regex shape, and similarity formula; and
``work/dedup-model-series/tech-spec.md`` Decisions 1-2 for the series/theme
lexicon, pair-key format, and tier polarity.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Fingerprint shape: dict with four list-valued keys.
#   'strict' — list of "brand model" tokens (e.g. "toyota 4runner")
#   'brands' — list of brand-only tokens (e.g. "toyota")
#   'series' — list of canonical series/theme names (e.g. "car culture",
#              "san diego comic-con")
#   'pairs'  — list of "<model>|<series>|<tier>" keys (theme-only variant
#              "*|<series>|B", franchises/events only — see
#              `_theme_only_eligible`); tier ∈ {D, B}
# All values are List[str]; lists are sorted for deterministic JSON
# serialisation and test equality.
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
# Series/theme lexicon — tier-tagged (dedup-model-series tech-spec Decision 2)
# ---------------------------------------------------------------------------
#
# alias -> (canonical, tier), tier ∈ {'distinctive', 'broad'}.
#
# - 'distinctive' = concrete franchises / events / limited series. These are
#   the ONLY entries that can drive a hard block (and only when paired with a
#   concrete car model — see `_build_pairs`).
# - 'broad' = frequent recurrent Hot Wheels car-lines / themes. These only
#   ever soft-flag.
#
# FAIL-SAFE POLARITY (the load-bearing safety property, user-spec AC7 /
# tech-spec Decision 2): a series must be EXPLICITLY tagged 'distinctive' to be
# distinctive. Anything else defaults to broad via `_SERIES_DEFAULT_TIER`, so a
# new / unknown / mis-tagged franchise can never silently hard-block. The
# extractor also only ever emits series that ARE in this lexicon — a franchise
# not yet catalogued produces no series token at all (double protection).
#
# The lexicon is seeded from the user-spec families (car-lines, SDCC event,
# pop-culture franchises) and is meant to be topped up via PR as new franchises
# appear (user-spec Ограничения — franchises are an open, fast-growing set).
# Aliases resolve case-insensitively EXCEPT the short/ambiguous acronyms in
# `_ACRONYM_ALIASES` (SDCC/RLC/STH), matched case-sensitively to avoid prose
# collisions (e.g. prose "sth"/"rlc"). The one exception is `zamac`
# (in `_CASE_INSENSITIVE_ACRONYMS`): a broad line with negligible prose-collision
# risk, so it matches any casing.
SERIES_LEXICON: Dict[str, Tuple[str, str]] = {
    # ---- distinctive: concrete franchises / events / limited series ----
    'k-pop demon hunters':  ('k-pop demon hunters', 'distinctive'),
    'kpop demon hunters':   ('k-pop demon hunters', 'distinctive'),
    'stranger things':      ('stranger things',     'distinctive'),
    'top gun':              ('top gun',             'distinctive'),
    'san diego comic-con':  ('san diego comic-con', 'distinctive'),
    'comic-con':            ('san diego comic-con', 'distinctive'),
    'sdcc':                 ('san diego comic-con', 'distinctive'),
    'red line club':        ('red line club',       'distinctive'),
    'rlc':                  ('red line club',       'distinctive'),
    'super treasure hunt':  ('super treasure hunt', 'distinctive'),
    'sth':                  ('super treasure hunt', 'distinctive'),
    # ---- broad: frequent recurrent car-lines / themes ----
    'car culture':          ('car culture',         'broad'),
    'boulevard':            ('boulevard',           'broad'),
    'team transport':       ('team transport',      'broad'),
    'zamac':                ('zamac',               'broad'),
    'pop culture':          ('pop culture',         'broad'),
    'monster trucks':       ('monster trucks',      'broad'),
}

# Fail-safe default: any resolvable-but-untagged tier path → broad. Referenced
# by `_tier_suffix` so an unexpected tier string can never become distinctive.
_SERIES_DEFAULT_TIER = 'broad'

# Load-time integrity assertion (fires at import, like the compiled regex
# constants below): the pair-key format is "<model>|<series>|<tier>", so no
# canonical series name may contain the `|` separator or a newline.
assert all(
    '|' not in canonical and '\n' not in canonical
    for canonical, _tier in SERIES_LEXICON.values()
), "SERIES_LEXICON canonicals must not contain '|' or newline (pair-key integrity)"

# Load-time tier-consistency assertion: every alias that resolves to the same
# canonical must carry the same tier. A mistyped alias must never silently split
# one canonical across two tiers — with fail-safe polarity a split would at worst
# downgrade to broad, but the authoring mistake should surface at import, not in
# production. Extends the pipe/newline integrity guarantee to tier consistency.
_seen_canonical_tier: Dict[str, str] = {}
for _canonical, _tier in SERIES_LEXICON.values():
    assert _seen_canonical_tier.setdefault(_canonical, _tier) == _tier, (
        f"SERIES_LEXICON: canonical {_canonical!r} mapped to conflicting tiers "
        f"({_seen_canonical_tier[_canonical]!r} vs {_tier!r}) — all aliases of one "
        f"canonical must share the same tier"
    )
del _seen_canonical_tier, _canonical, _tier

# Tier → key suffix. `_SERIES_DEFAULT_TIER` is the fail-safe fallback: any tier
# other than the curated 'distinctive' resolves to the broad 'B' suffix.
_TIER_SUFFIX = {'distinctive': 'D', 'broad': 'B'}


def _tier_suffix(tier: str) -> str:
    """Map a lexicon tier to its pair-key suffix, defaulting to broad ('B')."""
    return _TIER_SUFFIX.get(tier, _TIER_SUFFIX[_SERIES_DEFAULT_TIER])


# Every canonical in the lexicon, derived once (used by the integrity assertion
# below; the lexicon is a module-level literal, so this can never drift).
_SERIES_CANONICALS = frozenset(
    canonical for canonical, _tier in SERIES_LEXICON.values()
)

# Canonical series that are lexicon-DISTINCTIVE but name a recurring release
# PROGRAM rather than a one-off franchise/event. A shared `model + program` pair
# is still the strongest signal we have (same casting in the same program = the
# same release), but the program name ALONE identifies no particular news item:
# Hot Wheels ships Super Treasure Hunts and Red Line Club releases continuously,
# so two unrelated articles both naming one is the norm, not a coincidence.
# Excluded from theme-only key emission by `_theme_only_eligible`.
#
# Named *_PROGRAMS, not *_SERIES, on purpose: every BROAD line is recurring too,
# yet none of them belong here — they are already excluded by the tier test. This
# set means specifically "distinctive BUT a recurring program", so adding e.g.
# `pop culture` here would be redundant, not helpful.
_RECURRING_PROGRAMS = frozenset({'super treasure hunt', 'red line club'})

# Load-time integrity assertion (same family as the pipe/newline and
# tier-consistency asserts above): every entry must be a real lexicon CANONICAL.
# A rename or typo would otherwise silently stop excluding the entry and quietly
# restore the noisy theme-only key.
assert _RECURRING_PROGRAMS <= _SERIES_CANONICALS, (
    "_RECURRING_PROGRAMS entries must be canonical SERIES_LEXICON names: "
    f"{sorted(_RECURRING_PROGRAMS - _SERIES_CANONICALS)}"
)


def _theme_only_eligible(canonical: str, tier: str) -> bool:
    """May *canonical* stand alone as a theme-only ``"*|<series>|B"`` key?

    Only when the series name BY ITSELF identifies a specific news item — i.e.
    a one-off franchise or event (K-Pop Demon Hunters, Stranger Things, Top Gun,
    San Diego Comic-Con). Two classes are excluded:

    - **Broad recurrent car-lines** — anything whose tier does not resolve to the
      distinctive suffix (Pop Culture, Car Culture, Boulevard, Zamac, Monster
      Trucks, Team Transport). They ship continuously, and several read as
      ordinary prose — "a pop culture icon" in an autoevolution body is not a Hot
      Wheels line mention at all.
    - **Recurring release programs** (`_RECURRING_PROGRAMS`) —
      lexicon-distinctive but just as continuous.

    The tier test goes through ``_tier_suffix`` rather than comparing the tier
    string directly. Today the two are equivalent (`_TIER_SUFFIX` maps exactly
    one key to ``'D'``), so this is defensive rather than load-bearing: it keeps
    the unknown-tier→broad fail-safe as the single place that decides what counts
    as distinctive, and would keep this predicate correct if a second D-tier were
    ever added.

    Prod incident 2026-07-28: a t-hunted Pop Culture lot (Lotus Esprit Turbo →
    brand-only, empty ``strict``) and an autoevolution Lincoln Super Treasure
    Hunt (Lincoln is outside the 36-brand lexicon → empty ``strict``) both
    degraded to ``*|pop culture|B`` and soft-flagged each other. Model-bearing
    keys are untouched — this narrows matching, never the lexicon.
    """
    return (
        _tier_suffix(tier) == _TIER_SUFFIX['distinctive']
        and canonical not in _RECURRING_PROGRAMS
    )


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

# Prose connector words that can be spuriously captured as the PRIMARY model
# token right after a brand (e.g. PT "Porsche de K-Pop Demon Hunters" →
# model="de"). Unlike `model_extra` tokens (filtered by `_MODEL_EXTRA_KEEP_RE`),
# the primary model token must accept lowercase-alpha words because real model
# names are lowercase ("legacy", "camaro", "supra") — so a blanket allowlist
# can't be used here. This SMALL denylist rejects only pure function words.
#
# SCOPED to the pair-building path only (dedup-model-series code-review round 1,
# major #1): a connector-phrase primary token is dropped from the pair-key
# `<model>` set, so the article degrades to the theme-only `*|<series>|B`
# fail-safe key — or, since 2026-07-28, to NO key at all when the series is a
# broad line / recurring program (`_theme_only_eligible`) — instead of emitting a
# bogus distinctive key like `porsche de k-pop|...|D` that can never match a
# differently-worded companion (and, worse, could hard-block on connector noise
# if two articles share the same garbage phrasing). Both outcomes are the same
# fail-safe direction: at most a lost soft flag, never a false hard block. The
# `strict`/`brands` outputs — and therefore the shipped `similarity()` Jaccard
# path — are left UNCHANGED; only `pairs` differs.
_MODEL_CONNECTOR_STOPWORDS = frozenset({
    # Portuguese (t-hunted PT feed): "Porsche de K-Pop", "carro do evento".
    'de', 'do', 'da', 'dos', 'das', 'e', 'em', 'no', 'na', 'nos', 'nas',
    'com', 'para', 'por', 'ao', 'aos',
    # English connectors.
    'of', 'the', 'a', 'an', 'and', 'for', 'with', 'in', 'on', 'at', 'to',
    'from', 'by', 'or', 'vs',
})

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
# Series/theme scan regexes (module-level singletons, bounded quantifiers)
# ---------------------------------------------------------------------------
#
# Two compiled passes mirror the brand extractor's split:
#   1. `_SERIES_RE` — case-insensitive alternation over the multi-word / longer
#      aliases, built programmatically from the lexicon keys so the pattern can
#      never drift from `SERIES_LEXICON`.
#   2. `_SERIES_ACRONYM_RE` — DERIVED from `_ACRONYM_ALIASES` (never hand-typed,
#      so it can never drift from the lexicon). Genuinely ambiguous short
#      acronyms are case-SENSITIVE, mirroring `_UPPERCASE_BRANDS_RE`: prose
#      "sth"/"rlc"/"sdcc" lowercase must NOT match; only the exact-cased forms
#      "SDCC"/"RLC"/"STH" do. `zamac` is the deliberate exception — its
#      canonical key is lowercase and it is a BROAD line (fail-safe), so it is
#      matched CASE-INSENSITIVELY via a scoped `(?i:...)` group (lowercase
#      "zamac" in prose resolves too).
#
# ReDoS-safety: `_alias_to_pattern` relaxes literal spaces to the bounded
# `\s{1,3}` (never `\s+`/`\s*`), mirroring `_MODEL_AFTER_BRAND_RE`; the rest of
# each alias is `re.escape`-d literal text. Multi-word aliases are sorted
# longest-first so a longer alias always wins over a shorter one that could be
# its prefix ("san diego comic-con" before "comic-con").

# Short/ambiguous acronyms — routed to the acronym pass (lowercase keys in the
# lexicon). The ambiguous ones match their exact-cased (uppercase) form only;
# `zamac` matches case-insensitively (see `_acronym_to_pattern`).
_ACRONYM_ALIASES = frozenset({'sdcc', 'rlc', 'sth', 'zamac'})

# Acronyms matched case-INSENSITIVELY (via a scoped `(?i:...)` group) rather
# than exact-cased. Kept as a set so the deliberate casing decision is a single
# reviewable place, not buried in `_acronym_to_pattern`. `zamac` qualifies
# because its canonical key is lowercase AND it is a BROAD line (an under- or
# over-match only ever soft-flags — the fail-safe direction). The genuinely
# ambiguous short acronyms (sdcc/rlc/sth) are deliberately NOT here: prose
# "sth"/"rlc"/"sdcc" must stay a non-match.
_CASE_INSENSITIVE_ACRONYMS = frozenset({'zamac'})


def _alias_to_pattern(alias: str) -> str:
    """Compile one lexicon alias to a bounded, ReDoS-safe regex fragment.

    Literal spaces become bounded ``\\s{1,3}`` (tolerates 1-3 whitespace,
    including a single newline from ``_gather_text``); every other character is
    ``re.escape``-d literal text. No unbounded quantifiers.
    """
    return r'\s{1,3}'.join(re.escape(word) for word in alias.split(' '))


# Case-insensitive multi-word/longer aliases, longest-first (prefix-safe).
_CI_SERIES_ALIASES = sorted(
    (alias for alias in SERIES_LEXICON if alias not in _ACRONYM_ALIASES),
    key=len,
    reverse=True,
)
_SERIES_RE = re.compile(
    r'\b(?P<series>' + '|'.join(_alias_to_pattern(a) for a in _CI_SERIES_ALIASES) + r')\b',
    re.IGNORECASE,
)

def _acronym_to_pattern(alias: str) -> str:
    """Compile one acronym alias to a case-appropriate, ReDoS-safe regex branch.

    Derived from the alias itself (never hand-typed), so the acronym pattern
    can never drift from `_ACRONYM_ALIASES` / `SERIES_LEXICON`:

    - ``zamac`` (in `_CASE_INSENSITIVE_ACRONYMS`) → a scoped ``(?i:...)`` group
      so lowercase "zamac" in prose matches too. Safe because it is a BROAD
      line (over-/under-match only soft-flags — the fail-safe direction).
    - every other acronym → its exact UPPERCASE form only, mirroring
      `_UPPERCASE_BRANDS_RE`, so prose "sth"/"rlc"/"sdcc" stays a non-match.

    Pure ``re.escape``-d literal text with no quantifiers → ReDoS-safe.
    """
    if alias in _CASE_INSENSITIVE_ACRONYMS:
        return f'(?i:{re.escape(alias)})'
    return re.escape(alias.upper())


# Acronym alternation built from `_ACRONYM_ALIASES`, longest-first (prefix-safe,
# same convention as `_CI_SERIES_ALIASES`). Compiled WITHOUT a global re.I — the
# per-branch casing is decided in `_acronym_to_pattern`.
_ACRONYM_BRANCHES = [
    _acronym_to_pattern(a)
    for a in sorted(_ACRONYM_ALIASES, key=len, reverse=True)
]
_SERIES_ACRONYM_RE = re.compile(
    r'\b(?P<series>' + '|'.join(_ACRONYM_BRANCHES) + r')\b',
)


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


def _canonical_series(raw: str) -> Optional[Tuple[str, str]]:
    """Resolve a matched series string to ``(canonical, tier)`` or ``None``.

    Mirrors ``_canonical_brand``: lowercase + collapse internal whitespace,
    then look the key up in ``SERIES_LEXICON``.

        "San Diego  Comic-Con" → ("san diego comic-con", "distinctive")
        "Car Culture"          → ("car culture", "broad")
        "not a series"         → None
    """
    key = re.sub(r'\s{1,3}', ' ', raw.strip().lower())
    return SERIES_LEXICON.get(key)


def _scan_series(text: str) -> set:
    """Return the set of ``(canonical, tier)`` series found in *text*.

    Runs both the case-insensitive alias pass and the case-sensitive acronym
    pass. Only lexicon-resolvable matches are emitted (fail-safe: an untagged
    franchise yields nothing here → no pair → cannot hard-block).
    """
    found: set = set()
    for pattern in (_SERIES_RE, _SERIES_ACRONYM_RE):
        for match in pattern.finditer(text):
            resolved = _canonical_series(match.group('series'))
            if resolved:
                found.add(resolved)
    return found


def _build_pairs(strict: set, series_with_tier: Iterable[Tuple[str, str]]) -> List[str]:
    """Cartesian ``model × series`` → sorted ``"<model>|<series>|<tier>"`` keys.

    - With concrete models: one key per (model, series); tier suffix is ``D``
      iff the series is tagged distinctive, else ``B``.
    - With NO concrete model: a single theme-only key ``"*|<series>|B"`` —
      always broad, and only for series that pass ``_theme_only_eligible``
      (one-off franchises/events). A recurrent car-line or a recurring release
      program contributes NO key at all without a concrete model: on its own it
      is not evidence, and it produced prod false-flags (see
      ``_theme_only_eligible``). Theme-only keys stay ALWAYS broad, so a
      franchise with no casting can still only ever soft-flag.

    ``|`` is a safe separator: model tokens are ``[a-z0-9 -]`` and series
    tokens are lexicon canonicals (guaranteed pipe-free by the load-time
    assertion), so neither side can contain it.
    """
    models = sorted(strict)
    pairs: set = set()
    for canonical, tier in series_with_tier:
        if models:
            suffix = _tier_suffix(tier)
            for model in models:
                pairs.add(f"{model}|{canonical}|{suffix}")
        elif _theme_only_eligible(canonical, tier):
            # Theme-only — always broad, regardless of the series' own tier.
            pairs.add(f"*|{canonical}|B")
        # Ineligible series + no model → no key. Deliberate under-match in the
        # fail-safe direction: it can only ever cost a soft flag, never cause
        # a silent hard block.
    return sorted(pairs)


def _pass3_series(text: str, pair_models: Set[str]) -> Tuple[List[str], List[str]]:
    """Pass 3 — lexicon-driven series/theme extraction + pair-key build.

    Factored out of ``extract_fingerprint`` (keeps that function near the
    ~50-line guideline; behaviour is identical). Returns ``(series, pairs)``:

    - ``series`` — sorted canonical series/theme names found in *text*.
    - ``pairs``  — sorted ``"<model>|<series>|<tier>"`` keys from the cartesian
      of *pair_models* × recognised series (when *pair_models* is empty:
      theme-only ``"*|<series>|B"`` for franchises/events, nothing for broad
      lines and recurring programs — see ``_theme_only_eligible``).

    *pair_models* is the pair-eligible subset of ``strict`` tokens (primary
    model word passed the connector-stopword filter) — NOT the full ``strict``
    set, so a garbage connector-phrase token degrades to the theme-only key
    (or, for an ineligible series, to no key).

    Note that ``series`` is built from ALL recognised series regardless of
    pair-key eligibility — extraction is unchanged, only MATCHING narrows.
    """
    series_with_tier = _scan_series(text)
    series = sorted({canonical for canonical, _tier in series_with_tier})
    pairs = _build_pairs(pair_models, series_with_tier)
    return series, pairs


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
         'brands': sorted list of brand-only tokens,
         'series': sorted list of canonical series/theme names,
         'pairs':  sorted list of "<model>|<series>|<tier>" keys}

    Empty input yields the four-key all-empty structure (NOT NULL — that
    distinction is the backfill idempotency marker per tech-spec Decision
    10; carrying all four keys is the non-NULL structure of AC8). Year prefix
    `(?:19|20)\\d{2}` is dropped from output tokens (PT bodies often omit
    year; we want EN/PT fingerprints to match).

    Pure function — no I/O, no logging. A crash here is caught by the dedup
    gate's broad ``try/except`` (degraded mode → publish).
    """
    text = _gather_text(article)
    if not text:
        return {'strict': [], 'brands': [], 'series': [], 'pairs': []}

    strict_tokens: set = set()
    brand_tokens: set = set()
    # Pair-eligible subset of `strict_tokens`: excludes tokens whose PRIMARY
    # model word is a prose connector ("de"/"do"/"of"/…). `strict_tokens` (and
    # thus `similarity()`) is unaffected; only pair-key building uses this set,
    # so a connector-phrase token degrades to the theme-only `*|<series>|B` key
    # (or, for a broad line / recurring program, to no key at all)
    # rather than a bogus distinctive key (code-review round 1, major #1).
    pair_strict_tokens: set = set()

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
        token = f"{brand} {' '.join(model_parts)}"
        strict_tokens.add(token)
        # Pair-key eligibility: the primary model word must not be a prose
        # connector. A garbage primary ("de" in "porsche de k-pop") is kept in
        # `strict_tokens` (so `similarity()` is unchanged) but excluded from the
        # pair-key set, degrading the article to the theme-only fail-safe key —
        # or to no key at all when the series is not theme-only-eligible.
        if model_lower not in _MODEL_CONNECTOR_STOPWORDS:
            pair_strict_tokens.add(token)

    # Pass 2: case-sensitive uppercase brands (BMW / AMC / Lotus).
    for match in _UPPERCASE_BRANDS_RE.finditer(text):
        brand_raw = match.group('brand')
        if brand_raw:
            brand_tokens.add(brand_raw.lower())

    # Pass 3: series/theme extraction + pair-key build (factored into
    # `_pass3_series`). Pairs are keyed on the pair-eligible model subset so a
    # connector-phrase token degrades to the theme-only "*|<series>|B" key (or,
    # for an ineligible series, to no key — `_theme_only_eligible`).
    series, pairs = _pass3_series(text, pair_strict_tokens)

    return {
        'strict': sorted(strict_tokens),
        'brands': sorted(brand_tokens),
        'series': series,
        'pairs': pairs,
    }


def extract_series(article_or_text) -> List[str]:
    """Sorted list of canonical series/theme names found in the input.

    Accepts either an article dict (same shape as ``extract_fingerprint``) or a
    raw text string. Pure function — no I/O, no logging.
    """
    text = article_or_text if isinstance(article_or_text, str) else _gather_text(article_or_text)
    if not text:
        return []
    return sorted({canonical for canonical, _tier in _scan_series(text)})


def shares_pair(fp_a: dict, fp_b: dict) -> Tuple[bool, List[str], bool]:
    """Compare two fingerprints' ``pairs`` lists.

    Returns ``(any_shared, sorted_shared_pairs, any_distinctive)``:
      - ``any_shared``      — the two fingerprints share ≥1 pair key;
      - ``sorted_shared_pairs`` — the shared keys, sorted (stable for pings);
      - ``any_distinctive`` — any shared key ends ``|D`` (drives hard block vs
        soft flag; a shared ``|D`` wins over a shared ``|B``).

    Backward-compatible: rows written before this feature lack a ``pairs`` key —
    ``fp.get('pairs') or []`` never raises ``KeyError``. Pure function.
    """
    pairs_a = set(fp_a.get('pairs') or []) if isinstance(fp_a, dict) else set()
    pairs_b = set(fp_b.get('pairs') or []) if isinstance(fp_b, dict) else set()
    shared = pairs_a & pairs_b
    any_distinctive = any(p.endswith('|D') for p in shared)
    return bool(shared), sorted(shared), any_distinctive


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
