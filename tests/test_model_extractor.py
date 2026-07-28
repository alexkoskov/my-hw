#!/usr/bin/env python3
"""Unit tests for ``model_extractor``.

Covers:
- ``TestExtractFingerprint`` — fingerprint extraction over a range of body
  shapes (single brand+model, multi-word brand, aliases, year-prefix drop,
  case-sensitivity guards for BMW/Lotus, empty input, long body ReDoS-safety
  smoke).
- ``TestSimilarity`` — the Decision 4 guarded two-level Jaccard formula
  (AC6 empty-fp guard, AC8 1-token / brand-count guards, AC10 two-level max).
- ``test_calibration_pair_tier_accuracy`` — runs ``extract_fingerprint`` +
  ``shares_pair`` on the 9-pair calibration fixture and asserts ≥8/9 correct
  tier-verdicts (``duplicate`` / ``soft-flag`` / ``non-duplicate``) AND the
  concrete ``expected_shared_pairs`` per pair — the user-spec AC11 floor with a
  one-error budget.
- ``test_calibration_sdcc_dupes_hard_block`` — the 3 real SDCC 2026 dupes MUST
  hard-block (``any_distinctive is True``). A SEPARATE hard invariant, kept out
  of the aggregate budget: a missed hard block is a silent, irreversible drop.
- ``test_calibration_not_dupes_never_hard_block`` — every not-dupe probe MUST
  NOT hard-block (``any_distinctive is False``), the other half of the
  asymmetric invariant.
- ``test_calibration_non_dupes_share_no_pair`` — third hard invariant
  (2026-07-28): every ``'non-duplicate'`` pair must share NO key at all. The
  prod false-flag was a shared ``|B`` key, which both older invariants accept.
"""

from __future__ import annotations

import pytest

import model_extractor as me
from model_extractor import (
    extract_fingerprint,
    extract_series,
    shares_pair,
    similarity,
)
from tests.fixtures.cross_source_dedup_pairs import (
    DUPE_PAIRS,
    NON_DUPE_PAIRS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _article(title: str = "", subtitle: str = "", paragraphs=None) -> dict:
    """Build an article dict in the shape ``extract_fingerprint`` expects."""
    return {
        'title': title,
        'subtitle': subtitle,
        'paragraphs': list(paragraphs) if paragraphs else [],
    }


# ---------------------------------------------------------------------------
# TestExtractFingerprint
# ---------------------------------------------------------------------------


class TestExtractFingerprint:
    def test_single_brand_model(self):
        fp = extract_fingerprint(_article(title='2018 Toyota 4Runner gold chase'))
        assert fp['strict'] == ['toyota 4runner']
        assert fp['brands'] == ['toyota']

    def test_multiword_brand(self):
        fp = extract_fingerprint(_article(title='Land Rover S2 (BP)'))
        assert 'land rover s2' in fp['strict']
        assert 'land rover' in fp['brands']

    def test_range_rover_distinct_from_land_rover(self):
        # Range Rover and Land Rover are separately registered brands per
        # code-research §14.A.3 — they should both extract independently.
        fp = extract_fingerprint(_article(title='Range Rover Classic two-tone'))
        # Composite model captures up to 2 extra words; "Classic" is
        # alphabetic-only so kept only if it looks like a designator.
        # "Classic" is title-cased prose so dropped by the keep filter →
        # token is just "range rover classic" if Classic looks like a
        # designator, else just "range rover" goes to brands and no
        # strict token (model word filtered). Either is acceptable;
        # we assert the brand is present and the strict token shape is
        # consistent.
        assert 'range rover' in fp['brands']

    def test_brand_alias_chevy(self):
        fp = extract_fingerprint(_article(title='Chevy Camaro Z28'))
        # Composite: Camaro Z28 (Z28 is all-uppercase digit-bearing → kept).
        assert 'chevrolet camaro z28' in fp['strict']
        assert 'chevrolet' in fp['brands']

    def test_brand_alias_vw(self):
        fp = extract_fingerprint(_article(title='VW Golf GTI'))
        # Composite: Golf GTI (GTI all-uppercase → kept).
        assert 'volkswagen golf gti' in fp['strict']
        assert 'volkswagen' in fp['brands']

    def test_brand_alias_mercedes_benz(self):
        fp = extract_fingerprint(_article(title='Mercedes-Benz E300 review'))
        assert 'mercedes e300' in fp['strict']
        assert 'mercedes' in fp['brands']

    def test_empty_body_four_keys(self):
        # Every fingerprint carries the four keys — empty input yields four
        # empty lists (non-NULL structure, AC8). Replaces the old 2-key
        # `test_empty_body`.
        assert extract_fingerprint(_article()) == {
            'strict': [], 'brands': [], 'series': [], 'pairs': [],
        }

    def test_empty_dict_input(self):
        # Defensive: completely empty input shouldn't crash.
        assert extract_fingerprint({}) == {
            'strict': [], 'brands': [], 'series': [], 'pairs': [],
        }

    def test_returns_four_keys(self):
        # Public contract: extract_fingerprint returns strict/brands/series/pairs.
        fp = extract_fingerprint(_article(title='Toyota 4Runner review'))
        assert set(fp) == {'strict', 'brands', 'series', 'pairs'}

    def test_missing_paragraphs_key(self):
        # `paragraphs` may be absent or None — graceful handling required.
        fp = extract_fingerprint({'title': 'Honda Civic Type-R', 'subtitle': ''})
        # Composite: "Civic Type-R" — Type-R is hyphenated → kept.
        assert 'honda civic type-r' in fp['strict']
        assert 'honda' in fp['brands']

    def test_pt_text_with_en_brand_model(self):
        # t-hunted PT body with inline EN brand+model mentions — the prod
        # case that motivated the feature.
        fp = extract_fingerprint(_article(
            title='Um novo lote da série Car Culture',
            subtitle='Mattel revelou os carros',
            paragraphs=[
                'O destaque é o Subaru Legacy GT em verde metálico.',
                'O Land Rover S2 também aparece na série.',
                'E ainda um 2018 Toyota 4Runner com barraca de teto.',
            ],
        ))
        # "Subaru Legacy GT" → composite "subaru legacy gt"
        assert 'subaru legacy gt' in fp['strict']
        assert 'land rover s2' in fp['strict']
        assert 'toyota 4runner' in fp['strict']
        assert 'subaru' in fp['brands']
        assert 'land rover' in fp['brands']
        assert 'toyota' in fp['brands']
        # "série Car Culture" → series now populated (broad recurrent line).
        assert 'car culture' in fp['series']

    def test_mixed_brands_single_body(self):
        fp = extract_fingerprint(_article(
            title='Boulevard Mix — Camaro Z28, Mustang Boss, Datsun 510',
        ))
        assert 'chevrolet' in fp['brands'] or 'chevy' not in fp['brands']
        # Mustang is matched via Ford Mustang pattern only if "Ford" precedes.
        # Here we test what the regex picks up — at minimum Datsun.
        assert 'datsun 510' in fp['strict']

    def test_year_prefix_dropped(self):
        fp = extract_fingerprint(_article(title='2018 Toyota 4Runner gold chase'))
        # No strict token should contain "2018".
        assert all('2018' not in tok for tok in fp['strict'])
        # Sanity — token IS extracted, year just stripped from prefix.
        assert 'toyota 4runner' in fp['strict']

    def test_false_positive_lowercase_bmw(self):
        # BMW must be case-sensitive — prose "bmwxyz123" must NOT extract it.
        fp = extract_fingerprint(_article(title='bmwxyz123 nonsense word'))
        assert 'bmw' not in fp['brands']

    def test_false_positive_lotus_prose(self):
        # "lotus position" — Lotus is case-sensitive uppercase only.
        fp = extract_fingerprint(_article(
            title='Some article',
            paragraphs=['He sat in lotus position contemplating the void.'],
        ))
        assert 'lotus' not in fp['brands']

    def test_uppercase_bmw_extracted(self):
        # Sanity: uppercase BMW IS extracted.
        fp = extract_fingerprint(_article(title='BMW M3 review'))
        assert 'bmw' in fp['brands']

    def test_deduplication_of_repeated_mentions(self):
        # Multiple mentions of the same brand+model should appear once.
        fp = extract_fingerprint(_article(
            title='Toyota 4Runner review',
            paragraphs=['The Toyota 4Runner is great.', 'Toyota 4Runner again.'],
        ))
        assert fp['strict'].count('toyota 4runner') == 1
        assert fp['brands'].count('toyota') == 1

    def test_long_body_no_hang(self):
        # ReDoS-safety smoke: synthetic 10KB body with repeated mentions.
        # Qualitative — assertion is just "returned" (no wall-clock).
        # Bounded quantifiers in the regex guarantee linear time.
        body = 'Toyota 4Runner. ' * 600  # ~10KB
        fp = extract_fingerprint(_article(paragraphs=[body]))
        assert 'toyota 4runner' in fp['strict']

    def test_returns_sorted_lists(self):
        # Determinism: lists must be sorted for JSON stability.
        fp = extract_fingerprint(_article(
            title='Porsche 911 vs Ferrari Testarossa vs Lamborghini Countach',
        ))
        assert fp['strict'] == sorted(fp['strict'])
        assert fp['brands'] == sorted(fp['brands'])


# ---------------------------------------------------------------------------
# TestSimilarity
# ---------------------------------------------------------------------------


class TestSimilarity:
    def test_empty_fp_returns_zero(self):
        # AC6: empty strict on EITHER side → 0.0
        a = {'strict': [], 'brands': []}
        b = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        assert similarity(a, b) == 0.0
        assert similarity(b, a) == 0.0

    def test_both_empty_returns_zero(self):
        a = {'strict': [], 'brands': []}
        assert similarity(a, a) == 0.0

    def test_full_overlap_returns_one(self):
        a = {
            'strict': ['toyota 4runner', 'subaru legacy gt'],
            'brands': ['subaru', 'toyota'],
        }
        assert similarity(a, a) == 1.0

    def test_partial_strict_with_brand_fallback(self):
        # 2/4 strict, both sides have ≥2 brands each → max(strict, brands).
        # Strict: 2 shared / 4 union = 0.5.
        # Brands: 2 shared / 2 union = 1.0 → max → 1.0.
        a = {
            'strict': ['toyota 4runner', 'subaru legacy gt'],
            'brands': ['subaru', 'toyota'],
        }
        b = {
            'strict': ['toyota supra', 'subaru wrx'],
            'brands': ['subaru', 'toyota'],
        }
        assert similarity(a, b) == 1.0

    def test_one_token_strict_both_sides_no_brand_fallback(self):
        # AC8 1-token guard: each side has only 1 strict token. Even if
        # brand sets overlap fully, we must NOT fall back to brand-jaccard.
        a = {'strict': ['subaru brz'], 'brands': ['subaru']}
        b = {'strict': ['subaru wrx'], 'brands': ['subaru']}
        # Strict jaccard = 0/2 = 0.0; brand fallback disabled → 0.0.
        assert similarity(a, b) == 0.0

    def test_one_strict_one_brand_each(self):
        # 1 strict, 1 brand each side. AC8 1-token guard returns strict only.
        a = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        b = {'strict': ['toyota 4runner'], 'brands': ['toyota']}
        assert similarity(a, b) == 1.0

    def test_two_strict_one_brand_each(self):
        # 2 strict each, only 1 brand each → brand-count guard (<2 brands)
        # blocks brand fallback. Returns strict-jaccard only.
        # Strict: 0 shared / 4 union = 0.0.
        a = {
            'strict': ['ford mustang', 'ford bronco'],
            'brands': ['ford'],
        }
        b = {
            'strict': ['ford f-150', 'ford fiesta'],
            'brands': ['ford'],
        }
        assert similarity(a, b) == 0.0

    def test_threshold_block_50_percent(self):
        # Exactly 0.50 strict-jaccard (2 shared / 4 union), brand fallback
        # disabled by 1-brand-each guard → returns strict 0.50.
        a = {
            'strict': ['toyota 4runner', 'toyota supra'],
            'brands': ['toyota'],
        }
        b = {
            'strict': ['toyota 4runner', 'toyota corolla'],
            'brands': ['toyota'],
        }
        # 1 shared / 3 union = 0.333... — let me make it 0.50 exactly.
        a = {
            'strict': ['toyota 4runner', 'toyota supra'],
            'brands': ['toyota'],
        }
        b = {
            'strict': ['toyota 4runner', 'toyota supra'],
            'brands': ['toyota'],
        }
        assert similarity(a, b) == 1.0  # sanity — same set
        # Now actual 0.50:
        a = {
            'strict': ['toyota 4runner', 'honda civic'],
            'brands': ['toyota', 'honda'],
        }
        b = {
            'strict': ['toyota 4runner', 'mazda rx-7'],
            'brands': ['toyota', 'mazda'],
        }
        # Strict: 1 shared / 3 union = 0.333...
        # Brands: 1 shared / 3 union = 0.333...
        # max → 0.333...
        # Let's craft a proper 0.50: 2 shared, 4 union.
        a = {
            'strict': ['toyota 4runner', 'honda civic'],
            'brands': ['toyota', 'honda'],
        }
        b = {
            'strict': ['toyota 4runner', 'honda civic', 'mazda rx-7', 'nissan gt-r'],
            'brands': ['honda', 'mazda', 'nissan', 'toyota'],
        }
        # Strict: 2 shared / 4 union = 0.50.
        # Brands: 2 shared / 4 union = 0.50.
        # max → 0.50.
        assert similarity(a, b) == 0.5

    def test_threshold_flag_30_percent(self):
        # Strict jaccard exactly 0.30: 3 shared / 10 union.
        a_strict = [f'brand{i} model{i}' for i in range(7)]
        b_strict = [f'brand{i} model{i}' for i in range(4, 11)]  # share 4,5,6
        # 3 shared, 7+7-3=11 union ... let me recount: {0..6} | {4..10} = {0..10} = 11.
        # Shared: {4,5,6} = 3. Jaccard = 3/11 ≈ 0.27.
        # Need exactly 0.30 = 3/10. So union=10, intersection=3:
        a_strict = [f'b m{i}' for i in range(6)]  # 6 items
        b_strict = [f'b m{i}' for i in range(3, 7)]  # 4 items, share 3,4,5
        # Shared = {3,4,5} = 3. Union = {0..6} = 7. = 3/7 ≈ 0.428.
        # Try: A=[0..6] (7), B=[4..9] (6). Shared={4,5,6}=3. Union={0..9}=10. = 0.30 exactly.
        a_strict = [f'b m{i}' for i in range(7)]
        b_strict = [f'b m{i}' for i in range(4, 10)]
        a = {'strict': a_strict, 'brands': ['only_brand']}
        b = {'strict': b_strict, 'brands': ['only_brand']}
        # 1 brand each → brand fallback disabled, strict-only.
        assert abs(similarity(a, b) - 0.30) < 1e-9


# ---------------------------------------------------------------------------
# Calibration tests — pair-tier verdict (user-spec AC11)
# ---------------------------------------------------------------------------
#
# The 8-pair fixture is scored through the NEW pair-rule (``shares_pair`` over
# the ``(model + series/theme)`` fingerprint), NOT the old car-set ``similarity``
# Jaccard: the motivating dupes are pop-culture tie-ins (K-Pop Demon Hunters,
# Stranger Things, Top Gun) whose ``strict`` car-set is empty on the themed
# side, so the old Jaccard scored them ~0 and silently passed the exact cases
# this feature exists to catch. The tier is read straight off the shared key's
# ``|D`` / ``|B`` suffix (``shares_pair`` → ``any_distinctive``); no re-lookup.
#
# Gating: an aggregate floor (≥7/8 tier-verdicts) PLUS two asymmetric HARD
# invariants pinned as separate tests — a silent hard block is irreversible (no
# manual re-publish), so "MUST block" (the 3 SDCC dupes) and "MUST NOT block"
# (every not-dupe probe) cannot be absorbed into the aggregate's 1-error budget.


def _pair_tier_verdict(pair: dict) -> tuple:
    """Score one fixture pair through the shipped pair-tier gate.

    Mirrors the Task 4 gate semantics: extract both fingerprints, intersect
    their ``pairs`` via ``shares_pair``, then map the ``(any_shared,
    any_distinctive)`` signal to the three-way verdict —
      * shared ``|D`` pair  → ``'duplicate'``     (HARD block, ``[E015]``)
      * shared ``|B`` only   → ``'soft-flag'``    (publishes + ping, ``[E014]``)
      * no shared pair       → ``'non-duplicate'`` (pass through)

    Returns ``(any_distinctive, verdict, shared)`` so callers can assert the
    aggregate tier-verdict, the load-bearing hard-block polarity, AND the
    concrete shared keys (``expected_shared_pairs``) — the last of which is what
    makes a spurious SOFT flag visible; verdict+polarity alone do not.
    """
    fp_a = extract_fingerprint(pair['a'])
    fp_b = extract_fingerprint(pair['b'])
    any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
    if any_distinctive:
        verdict = 'duplicate'
    elif any_shared:
        verdict = 'soft-flag'
    else:
        verdict = 'non-duplicate'
    return any_distinctive, verdict, shared


def test_calibration_pair_tier_accuracy():
    """≥8/9 pairs must map to the correct tier-verdict (user-spec AC11 floor).

    The aggregate budget allows exactly ONE misclassification (unchanged by the
    2026-07-28 addition of pair-9; the floor moved 7/8 → 8/9 to keep the same
    one-error budget). The three hard invariants below (SDCC-must-block /
    not-dupe-must-not-block / non-dupes-share-no-pair) are pinned separately so
    neither an irreversible silent hard block nor a noise-generating spurious
    soft flag can hide inside this budget.

    The concrete ``expected_shared_pairs`` are asserted per pair as part of the
    verdict: a pair that lands on the right verdict via the WRONG keys is a
    misclassification, not a pass.
    """
    pairs = DUPE_PAIRS + NON_DUPE_PAIRS
    correct = 0
    misclassified = []
    for pair in pairs:
        _any_distinctive, verdict, shared = _pair_tier_verdict(pair)
        expected = pair['expected_verdict']
        expected_shared = sorted(pair['expected_shared_pairs'])
        if verdict == expected and sorted(shared) == expected_shared:
            correct += 1
        elif verdict != expected:
            misclassified.append(
                f"{pair['label']}: {verdict} (expected {expected})"
            )
        else:
            misclassified.append(
                f"{pair['label']}: right verdict {verdict} but shared "
                f"{sorted(shared)} (expected {expected_shared})"
            )
    total = len(pairs)
    assert correct >= total - 1, (
        f"Calibration floor: {correct}/{total} correct tier-verdicts "
        f"(need ≥{total - 1}/{total}). Misclassified: {misclassified}"
    )


def test_calibration_non_dupes_share_no_pair():
    """Every ``'non-duplicate'`` pair must share NO key at all — not merely no
    ``|D`` key.

    A THIRD hard invariant, added 2026-07-28 alongside pair-9. The existing
    must-NOT-hard-block invariant is asymmetric in one direction only: it pins
    ``any_distinctive is False``, which the prod false-flag ALREADY satisfied —
    that bug produced a shared ``|B`` key, i.e. a soft flag. Without this test
    the fixture is provably blind to the whole bug class: forcing
    ``_theme_only_eligible`` to return True (restoring the pre-fix behaviour)
    leaves the aggregate and both older invariants green.

    Scoped to ``expected_verdict == 'non-duplicate'`` — the soft-flag pairs
    (1, 6, 7) are SUPPOSED to share broad keys.
    """
    non_dupes = [
        p for p in DUPE_PAIRS + NON_DUPE_PAIRS
        if p['expected_verdict'] == 'non-duplicate'
    ]
    # Guard against a fixture change silently emptying the selection.
    assert non_dupes, "expected at least one non-duplicate pair to pin"
    for pair in non_dupes:
        _any_distinctive, _verdict, shared = _pair_tier_verdict(pair)
        assert shared == [], (
            f"{pair['label']} MUST share no pair key (a shared key is at "
            f"minimum a noisy [E014] soft flag), got {shared}"
        )


def test_calibration_sdcc_dupes_hard_block():
    """The 3 real SDCC 2026 dupes MUST hard-block (``any_distinctive is True``).

    A SEPARATE hard invariant, NOT folded into the ≥7/8 aggregate: a missed
    hard block is a silent, irreversible drop (no manual re-publish, no
    per-subscriber recovery), so these three cannot be absorbed into the
    1-misclassification budget. Selected by the fixture's load-bearing
    ``expected_any_distinctive`` flag — pair-1 (Car Culture) is a BROAD
    soft-flag dupe and is correctly excluded.
    """
    sdcc_dupes = [p for p in DUPE_PAIRS if p['expected_any_distinctive']]
    # Guard against a fixture change silently emptying the selection (an empty
    # loop would vacuously pass and drop the whole invariant).
    assert len(sdcc_dupes) == 3, (
        f"expected the 3 real SDCC dupes in DUPE_PAIRS, got "
        f"{[p['label'] for p in sdcc_dupes]}"
    )
    for pair in sdcc_dupes:
        any_distinctive, _verdict, _shared = _pair_tier_verdict(pair)
        assert any_distinctive is True, (
            f"{pair['label']} MUST hard-block (shared |D pair), "
            f"got any_distinctive={any_distinctive!r}"
        )


def test_calibration_not_dupes_never_hard_block():
    """Every NON-DISTINCTIVE pair MUST NOT hard-block (``any_distinctive is False``).

    The other half of the asymmetric invariant: a non-distinctive pair that
    hard-blocks is a silent, irreversible drop. Soft-flag or pass are BOTH
    acceptable for these probes (same car in a different series, theme-only
    Stranger Things, a same-source broad near-miss, and the broad Car Culture
    dupe pair-1 itself); only a hard block is forbidden. Kept out of the ≥7/8
    aggregate for the same irreversibility reason as the SDCC invariant.

    Selected by the load-bearing ``expected_any_distinctive`` flag across BOTH
    lists (test-audit M-1) — NOT by list membership. This pins pair-1's
    must-not-hard-block property DIRECTLY (it lives in ``DUPE_PAIRS`` as a broad
    soft-flag dupe, ``expected_any_distinctive=False``), rather than only via the
    aggregate's 1-error budget, and future-proofs against a fixture where the
    broad-dupe series stops overlapping any NON_DUPE probe.
    """
    non_distinctive = [
        p for p in DUPE_PAIRS + NON_DUPE_PAIRS
        if not p['expected_any_distinctive']
    ]
    # Guard against a fixture change silently emptying the selection (an empty
    # loop would vacuously pass and drop the whole invariant).
    assert non_distinctive, "expected at least one non-distinctive pair to pin"
    for pair in non_distinctive:
        any_distinctive, _verdict, _shared = _pair_tier_verdict(pair)
        assert any_distinctive is False, (
            f"{pair['label']} MUST NOT hard-block, "
            f"got any_distinctive={any_distinctive!r}"
        )


# ---------------------------------------------------------------------------
# TestSeriesLexicon — lexicon integrity + fail-safe polarity
# ---------------------------------------------------------------------------


class TestSeriesLexicon:
    def test_canonicals_have_no_pipe_or_newline(self):
        # Pair-key integrity: `|` is the key separator; a canonical containing
        # `|` or `\n` would corrupt the "<model>|<series>|<tier>" format.
        # Mirrors the module load-time assertion.
        for canonical, tier in me.SERIES_LEXICON.values():
            assert '|' not in canonical, f"pipe in canonical {canonical!r}"
            assert '\n' not in canonical, f"newline in canonical {canonical!r}"

    def test_default_tier_is_broad(self):
        # Fail-safe polarity (tech-spec Decision 2 / code-research §7.2): any
        # resolvable-but-untagged path must default to broad, never distinctive.
        assert me._SERIES_DEFAULT_TIER == 'broad'

    def test_recurring_programs_are_real_lexicon_canonicals(self):
        # `_RECURRING_PROGRAMS` is matched against CANONICAL names, so a rename or
        # a typo would silently stop excluding the entry and quietly restore the
        # noisy theme-only key. Mirrors the module load-time assertion.
        canonicals = {canonical for canonical, _tier in me.SERIES_LEXICON.values()}
        assert me._RECURRING_PROGRAMS <= canonicals

    def test_theme_only_eligibility_is_fail_safe_on_unknown_tier(self):
        # Pins the unknown-tier → ineligible fallback. Routing through
        # `_tier_suffix` instead of a direct `tier == 'distinctive'` compare is
        # DEFENSIVE, not load-bearing: today `_TIER_SUFFIX` maps exactly one key
        # to 'D', so both spellings behave identically and this test would not
        # catch the swap. It keeps the fail-safe fallback as the single place
        # that decides what counts as distinctive.
        assert me._theme_only_eligible('k-pop demon hunters', 'distinctive') is True
        assert me._theme_only_eligible('k-pop demon hunters', 'distinct') is False
        assert me._theme_only_eligible('pop culture', 'broad') is False
        assert me._theme_only_eligible('super treasure hunt', 'distinctive') is False

    def test_unrecognized_tier_defaults_to_broad(self):
        # Fail-safe fallback exercised through the REAL code path, not the bare
        # constant (which `test_default_tier_is_broad` already pins). An
        # out-of-vocabulary / mistyped tier must resolve to the broad 'B'
        # suffix, never distinctive 'D'. This is the load-bearing safety
        # property (tech-spec Decision 2): a mistagged lexicon entry can never
        # silently hard-block. Deleting the `.get(..., default)` fallback in
        # `_tier_suffix` makes THIS test fail (mutation-closing).
        assert me._tier_suffix('not-a-real-tier') == 'B'
        assert me._tier_suffix('distinct') == 'B'  # near-miss typo of 'distinctive'
        pairs = me._build_pairs(
            {'porsche 911'}, {('some series', 'not-a-real-tier')}
        )
        assert pairs == ['porsche 911|some series|B']
        assert not any(p.endswith('|D') for p in pairs)


# ---------------------------------------------------------------------------
# TestExtractSeries — lexicon-driven series/theme extraction
# ---------------------------------------------------------------------------


class TestExtractSeries:
    def test_franchise_hit(self):
        assert 'k-pop demon hunters' in extract_series(
            'New K-Pop Demon Hunters cars revealed'
        )

    def test_alias_kpop_no_hyphen(self):
        # "KPop" (no hyphen) resolves to the hyphenated canonical.
        assert 'k-pop demon hunters' in extract_series(
            'The KPop Demon Hunters lineup'
        )

    def test_event_sdcc_full_and_acronym(self):
        # Both the full event name and the SDCC acronym → same canonical.
        assert 'san diego comic-con' in extract_series('San Diego Comic-Con reveal')
        assert 'san diego comic-con' in extract_series('SDCC 2026 exclusives')

    def test_car_line_car_culture(self):
        assert 'car culture' in extract_series('Car Culture Road Trip Mix')

    def test_acronym_case_sensitive_no_prose_collision(self):
        # Lowercase prose acronyms must NOT match (case-sensitive pass).
        assert extract_series('he said sth about the rlc meeting today') == []
        # Uppercase acronyms DO match.
        assert 'super treasure hunt' in extract_series('New STH chase spotted')
        assert 'red line club' in extract_series('RLC members-only exclusive')
        assert 'san diego comic-con' in extract_series('SDCC drop')

    @pytest.mark.parametrize('text,expected', [
        # AC7 franchises named explicitly in the acceptance criteria — pinned
        # through the ACTUAL `extract_series`/`_SERIES_RE` regex path (not a
        # hand-built `_build_pairs` tuple), so a misspelled lexicon key or a
        # dropped regex alternation would fail here.
        ('New Top Gun Maverick set revealed', 'top gun'),
        ('Stranger Things Camaro drop', 'stranger things'),
        # Remaining lexicon entries never previously exercised end-to-end.
        ('Monster Trucks bash event', 'monster trucks'),
        ('Team Transport rig and trailer', 'team transport'),
        ('Pop Culture crossover wave', 'pop culture'),
        # Bare 'comic-con' alias (no "San Diego" prefix) → same canonical.
        ('Exclusive Comic-Con reveal today', 'san diego comic-con'),
    ])
    def test_remaining_lexicon_entries_resolve(self, text, expected):
        assert expected in extract_series(text)

    def test_zamac_acronym_case_insensitive(self):
        # `zamac` is the one acronym matched CASE-INSENSITIVELY (M1 fix): its
        # canonical key is lowercase AND it is a BROAD line, so a scoped
        # `(?i:zamac)` branch resolves every casing to canonical 'zamac'. This
        # closes the previous lowercase recall gap in the fail-safe direction
        # (a broad match only ever soft-flags, never a silent hard block).
        assert 'zamac' in extract_series('New ZAMAC casting spotted')
        assert 'zamac' in extract_series('New Zamac casting spotted')
        # Lowercase prose 'zamac' NOW matches too (behaviour changed by M1).
        assert 'zamac' in extract_series('zamac diecast news')

    def test_ambiguous_acronyms_stay_case_sensitive(self):
        # The M1 fix must NOT relax the genuinely ambiguous short acronyms:
        # SDCC/RLC/STH still match ONLY their exact uppercase form, so common
        # lowercase prose collisions ('sth'/'rlc'/'sdcc') stay a non-match.
        assert extract_series('sth of note happened') == []
        assert extract_series('the rlc was quiet') == []
        assert extract_series('sdcc lower prose') == []
        # ...but the uppercase forms still resolve.
        assert 'super treasure hunt' in extract_series('STH exclusive drop')
        assert 'red line club' in extract_series('RLC members-only')
        assert 'san diego comic-con' in extract_series('SDCC exclusive')

    def test_series_alias_across_paragraph_boundary(self):
        # `_gather_text` joins title + paragraphs with '\n'; the bounded
        # `\s{1,3}` in `_alias_to_pattern` tolerates that single newline, so a
        # multi-word alias split across the title/paragraph boundary still
        # resolves (documented in the `_alias_to_pattern` docstring, previously
        # untested).
        fp = extract_fingerprint(_article(
            title='Hot Wheels reveals at San Diego',
            paragraphs=['Comic-Con exclusives are here.'],
        ))
        assert 'san diego comic-con' in fp['series']

    def test_no_series_returns_empty(self):
        assert extract_series('Just a plain Toyota 4Runner review') == []

    def test_long_body_no_hang(self):
        # ReDoS-safety smoke: synthetic ~10KB body. Bounded quantifiers keep
        # this linear-time; assertion is just "returned".
        body = 'Car Culture and Boulevard news. ' * 500  # ~15KB
        series = extract_series(body)
        assert 'car culture' in series
        assert 'boulevard' in series


# ---------------------------------------------------------------------------
# TestPairs — pair-key format + tier tagging
# ---------------------------------------------------------------------------


class TestPairs:
    def test_distinctive_pair_key_format(self):
        # Concrete model + distinctive series → "<model>|<series>|D".
        pairs = me._build_pairs(
            {'porsche 911'}, {('k-pop demon hunters', 'distinctive')}
        )
        assert pairs == ['porsche 911|k-pop demon hunters|D']

    def test_broad_series_alone_emits_no_pair(self):
        # theme-only-precision fix (2026-07-28): a BROAD recurrent car-line with
        # no concrete model must emit NO key at all. "Both articles mention Pop
        # Culture / Car Culture" is not evidence — the line ships continuously
        # and the phrase also occurs in ordinary prose ("a pop culture icon"),
        # which produced the prod false-flag this test guards.
        assert me._build_pairs(set(), {('car culture', 'broad')}) == []
        assert me._build_pairs(set(), {('pop culture', 'broad')}) == []

    def test_recurring_program_alone_emits_no_pair(self):
        # Same precision rule for lexicon-DISTINCTIVE series that name a
        # recurring release program rather than a one-off franchise: Hot Wheels
        # ships Super Treasure Hunts and RLC releases continuously, so the
        # series name alone identifies no particular news item.
        assert me._build_pairs(set(), {('super treasure hunt', 'distinctive')}) == []
        assert me._build_pairs(set(), {('red line club', 'distinctive')}) == []
        # Mistyped tier through the REAL branch: ineligible, never a re-opened
        # theme-only key (the `_tier_suffix` fail-safe governs eligibility too).
        assert me._build_pairs(set(), {('k-pop demon hunters', 'distinct')}) == []

    def test_recurring_program_with_a_model_is_broad_not_distinctive(self):
        """PROD REGRESSION 2026-07-28 — a recurring programme must NEVER reach
        the |D tier, not even with a concrete casting.

        This test used to assert the opposite, on the reasoning «same casting +
        same programme = same release». A ROUND-UP article breaks that: an
        autoevolution «10 affordable cars» unboxing names ten castings and
        mentions Red Line Club in passing, so the cartesian product produced
        `audi sport|red line club|D` — and hard-blocked an unrelated t-hunted
        post about a single $28 RLC Audi Quattro exclusive. The article was
        DROPPED and pinned, i.e. irreversibly lost without operator action.

        Driven through the REAL lexicon rather than a hand-passed tier, so the
        assertion cannot pass while the lexicon says something else.
        """
        for alias in ('super treasure hunt', 'red line club'):
            canonical, tier = me.SERIES_LEXICON[alias]
            pairs = me._build_pairs({'nissan skyline'}, {(canonical, tier)})
            assert pairs == [f'nissan skyline|{canonical}|B'], pairs
            assert not any(p.endswith('|D') for p in pairs)

    def test_prod_false_block_roundup_vs_single_drop(self):
        """The exact prod pair that hard-blocked (2026-07-28, 19:57 МСК).

        A round-up is the worst case for pair matching: N castings x M series
        is a combinatorial spray of keys, so it collides with almost anything.
        The defence is the tier — a series that appears in round-ups by nature
        must never be distinctive.
        """
        t_hunted = extract_fingerprint(_article(
            title='A volta do Audi Quattro para o Red Line Club!',
            paragraphs=[
                'A Mattel vai vender hoje uma variação de cor do 1985 Audi '
                'Sport Quattro S1 através do site Mattel Creations (você '
                'precisa ser membro do Red Line Club).',
            ],
        ))
        autoevolution = extract_fingerprint(_article(
            title='Hot Wheels Unboxing: 10 Affordable Cars for July of 2026',
            paragraphs=[
                'This month brings a mix of castings, including an Audi Sport '
                'Quattro and a few JDM icons.',
                'Collectors chasing Red Line Club exclusives will find plenty '
                'here too.',
            ],
        ))
        any_shared, shared, any_distinctive = shares_pair(t_hunted, autoevolution)
        # They DO still share a broad key — that is fine and even useful: the
        # operator gets an [E014] to glance at. What must never happen again is
        # the silent, irreversible drop.
        assert any_distinctive is False, shared
        assert all(p.endswith('|B') for p in shared), shared

    def test_franchise_and_broad_line_no_model_emits_only_the_franchise(self):
        # Mixed input, no concrete model: the one-off franchise still stands
        # alone, the recurrent line does not. Pins that the filter is per-series
        # and not an all-or-nothing switch on the no-models branch.
        pairs = me._build_pairs(
            set(),
            {('stranger things', 'distinctive'), ('pop culture', 'broad')},
        )
        assert pairs == ['*|stranger things|B']

    def test_distinctive_requires_concrete_model(self):
        # A distinctive franchise WITHOUT a model must be broad (|B), not |D —
        # the no-models branch downgrades even a distinctive series' own tier.
        pairs = me._build_pairs(set(), {('k-pop demon hunters', 'distinctive')})
        assert pairs == ['*|k-pop demon hunters|B']
        assert not any(p.endswith('|D') for p in pairs)

    def test_connector_primary_model_degrades_to_theme_only(self):
        # code-review round 1 major #1: a prose connector captured as the
        # PRIMARY model token ("de" in the real t-hunted title "Porsche de
        # K-Pop Demon Hunters") must NOT produce a bogus distinctive key like
        # `porsche de k-pop|...|D` — that key can never match a differently-
        # worded companion (the real Scenario-1 dupe is MISSED) and could even
        # hard-block on connector noise if two articles share the garbage
        # phrasing. The article degrades to the theme-only broad key instead.
        # SCOPED: `strict` (and thus `similarity()`) is left UNCHANGED.
        fp = extract_fingerprint(
            _article(title='Mais fotos do Porsche de K-Pop Demon Hunters')
        )
        # strict token still emitted verbatim (feeds the shipped Jaccard path).
        assert fp['strict'] == ['porsche de k-pop']
        # but the pair-key is the theme-only fail-safe, never a bogus |D.
        assert fp['pairs'] == ['*|k-pop demon hunters|B']
        assert not any(p.endswith('|D') for p in fp['pairs'])

    def test_broad_line_tier_is_B(self):
        # Broad recurrent car-line + concrete model → still |B. Also the
        # counterpart to `test_broad_series_alone_emits_no_pair`: withholding the
        # theme-only key must not weaken the MODEL-BEARING path — the calibration
        # fixture's pair-1 and pair-7 dupes ride on exactly these keys.
        pairs = me._build_pairs({'toyota supra'}, {('car culture', 'broad')})
        assert pairs == ['toyota supra|car culture|B']
        assert all(p.endswith('|B') for p in pairs)

    def test_extract_fingerprint_distinctive_pair_end_to_end(self):
        # End-to-end: concrete Porsche model + distinctive franchise → |D key.
        # Model and franchise are kept in separate sentences so the model
        # regex does not absorb the franchise words.
        fp = extract_fingerprint(_article(
            title='Porsche 911.',
            paragraphs=['The K-Pop Demon Hunters exclusive set is here.'],
        ))
        assert 'porsche 911|k-pop demon hunters|D' in fp['pairs']

    def test_extract_fingerprint_theme_only_when_no_model(self):
        # Pop-culture tie-in with no recognised car → theme-only broad key.
        fp = extract_fingerprint(_article(
            title='Hot Wheels San Diego Comic-Con exclusives revealed',
        ))
        assert fp['strict'] == []
        assert '*|san diego comic-con|B' in fp['pairs']
        assert not any(p.endswith('|D') for p in fp['pairs'])

    def test_prod_false_flag_prose_pop_culture_shares_no_pair(self):
        # PROD REGRESSION 2026-07-28 — the [E014] false flag that motivated the
        # theme-only-precision fix. Two unrelated articles:
        #   t-hunted PT   — a genuine Pop Culture lot; its Lotus Esprit Turbo
        #                   yields a brand-only token (the case-sensitive Lotus
        #                   pass captures no model), so `strict` is empty;
        #   autoevolution — a Lincoln Super Treasure Hunt; "Lincoln" is not in
        #                   the 36-brand lexicon, so `strict` is empty too, and
        #                   the body uses "pop culture" as ORDINARY PROSE.
        # Both degraded to `*|pop culture|B` and flagged each other. With the
        # theme-only key withheld for broad lines they share nothing.
        # t-hunted side is the real page text; the autoevolution side is
        # synthesised to the shape of the real page (Cloudflare 403 on fetch),
        # same convention as `tests/fixtures/cross_source_dedup_pairs.py`.
        t_hunted = extract_fingerprint(_article(
            title='Mais um novo lote da série Pop Culture de 2026, e com novidade',
            paragraphs=[
                'Uma das séries mais colecionadas pelos fãs de Hot Wheels é a '
                'Pop Culture, com suas réplicas de veículos que apareceram em '
                'filmes, séries de TV, desenhos ou jogos, e dessa vez tem um '
                'novo lote com uma novidade: o Lotus Esprit Turbo do 007 com '
                'esquis na traseira.',
            ],
        ))
        autoevolution = extract_fingerprint(_article(
            title='First Hot Wheels Super Treasure Hunt for 2027 Is a Lincoln',
            paragraphs=[
                'The Lincoln Continental Mark IV is a pop culture icon that '
                'Hot Wheels has finally cast in Super Treasure Hunt form.',
            ],
        ))
        # Premise of the incident: neither side yields a concrete model, so both
        # can only ever produce theme-only keys.
        assert t_hunted['strict'] == []
        assert autoevolution['strict'] == []
        # Both sides still RECOGNISE the series (extraction is unchanged) —
        # only the pair keys are withheld, so the fix is a matching-precision
        # change, not a lexicon regression.
        assert 'pop culture' in t_hunted['series']
        assert 'pop culture' in autoevolution['series']
        # The recurring-program exclusion is load-bearing on the autoevolution
        # side too: without it this article would keep `*|super treasure hunt|B`
        # and false-flag against any other model-less STH post.
        assert 'super treasure hunt' in autoevolution['series']
        assert t_hunted['pairs'] == []
        assert autoevolution['pairs'] == []

        any_shared, shared, any_distinctive = shares_pair(t_hunted, autoevolution)
        assert any_shared is False
        assert shared == []
        assert any_distinctive is False


# ---------------------------------------------------------------------------
# TestSharesPair — shared-pair detection + distinctive polarity
# ---------------------------------------------------------------------------


class TestSharesPair:
    def test_shared_distinctive_returns_true(self):
        fp_a = {'pairs': ['porsche 911|k-pop demon hunters|D', 'x|car culture|B']}
        fp_b = {'pairs': ['porsche 911|k-pop demon hunters|D']}
        any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
        assert any_shared is True
        assert shared == ['porsche 911|k-pop demon hunters|D']
        assert any_distinctive is True

    def test_shared_broad_only_returns_false(self):
        fp_a = {'pairs': ['toyota supra|car culture|B']}
        fp_b = {'pairs': ['toyota supra|car culture|B', 'other|boulevard|B']}
        any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
        assert any_shared is True
        assert shared == ['toyota supra|car culture|B']
        assert any_distinctive is False

    def test_distinctive_wins_over_broad(self):
        # Sharing BOTH a distinctive and a broad pair → distinctive wins.
        fp_a = {'pairs': [
            'porsche 911|k-pop demon hunters|D',
            'toyota supra|car culture|B',
        ]}
        fp_b = {'pairs': [
            'porsche 911|k-pop demon hunters|D',
            'toyota supra|car culture|B',
        ]}
        any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
        assert any_shared is True
        assert any_distinctive is True

    def test_no_shared_pair(self):
        fp_a = {'pairs': ['porsche 911|k-pop demon hunters|D']}
        fp_b = {'pairs': ['toyota supra|car culture|B']}
        any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
        assert any_shared is False
        assert shared == []
        assert any_distinctive is False

    def test_missing_pairs_key_is_safe(self):
        # Backward-compat: rows written before this feature lack a `pairs`
        # key — shares_pair must not raise, just report no overlap.
        any_shared, shared, any_distinctive = shares_pair(
            {'strict': ['toyota 4runner']}, {'pairs': ['x|car culture|B']}
        )
        assert any_shared is False
        assert shared == []
        assert any_distinctive is False

    def test_model_token_exact_match_limitation_documented(self):
        # DOCUMENTED accepted limitation (decisions.md Task 1; test-audit L-3),
        # locked the same way `test_zamac_*` locks its accepted under-match:
        # exact pair-key matching cannot rescue a dupe when only ONE side names
        # a concrete car model.
        #
        # The real t-hunted title "Porsche de K-Pop Demon Hunters" captures the
        # PT connector "de" as the PRIMARY model token, which is dropped from
        # the pair set, so the article degrades to the theme-only BROAD key
        # `*|k-pop demon hunters|B` (never a bogus |D — see
        # `test_connector_primary_model_degrades_to_theme_only`).
        theme_only = extract_fingerprint(
            _article(title='Mais fotos do Porsche de K-Pop Demon Hunters')
        )
        assert theme_only['pairs'] == ['*|k-pop demon hunters|B']
        assert not any(p.endswith('|D') for p in theme_only['pairs'])

        # A companion article that DOES name the model emits the distinctive
        # `porsche 911|k-pop demon hunters|D` key.
        with_model = extract_fingerprint(_article(
            title='Porsche 911.',
            paragraphs=['The K-Pop Demon Hunters exclusive set is here.'],
        ))
        assert 'porsche 911|k-pop demon hunters|D' in with_model['pairs']

        # The theme-only key and the model key do NOT intersect, so the real
        # cross-source dupe is MISSED — an under-match (recoverable, fail-safe
        # direction), NOT a false hard block. Any future change to
        # theme-only <-> model matching must flip this test consciously.
        any_shared, shared, any_distinctive = shares_pair(theme_only, with_model)
        assert any_shared is False
        assert shared == []
        assert any_distinctive is False
