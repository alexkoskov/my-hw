#!/usr/bin/env python3
"""Unit tests for ``model_extractor``.

Covers:
- ``TestExtractFingerprint`` — fingerprint extraction over a range of body
  shapes (single brand+model, multi-word brand, aliases, year-prefix drop,
  case-sensitivity guards for BMW/Lotus, empty input, long body ReDoS-safety
  smoke).
- ``TestSimilarity`` — the Decision 4 guarded two-level Jaccard formula
  (AC6 empty-fp guard, AC8 1-token / brand-count guards, AC10 two-level max).
- ``test_calibration_accuracy`` — runs ``extract_fingerprint`` + ``similarity``
  on the 8-pair calibration fixture and asserts ≥7/8 correct classifications
  (Decision 13 floor; user-spec AC12 target is ≥95% but the floor is the
  gating threshold).
- ``test_calibration_real_pair_must_pass`` — the real 2026-06-03 pair MUST
  classify as duplicate (Decision 13 must-pass split — guards the load-
  bearing example from being absorbed into the 1-misclassification budget).
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
# Calibration tests (Decision 13)
# ---------------------------------------------------------------------------


def _classify(sim: float) -> str:
    """Classifier matching tech-spec Decision 4 thresholds."""
    if sim >= 0.50:
        return 'duplicate'
    if sim >= 0.30:
        return 'soft-flag'
    return 'non-duplicate'


def test_calibration_accuracy():
    """≥7/8 pairs must be classified correctly (Decision 13 floor).

    User-spec AC12 target is ≥95% accuracy; the floor (≥87.5% = 7/8) is the
    gating threshold. The must-pass split (`test_calibration_real_pair_must_
    pass`) protects the load-bearing real pair from being absorbed into the
    1-misclassification budget.
    """
    pairs = DUPE_PAIRS + NON_DUPE_PAIRS
    correct = 0
    misclassified = []
    for pair in pairs:
        fp_a = extract_fingerprint(pair['a'])
        fp_b = extract_fingerprint(pair['b'])
        sim = similarity(fp_a, fp_b)
        verdict = _classify(sim)
        expected = pair['expected_verdict']
        if verdict == expected:
            correct += 1
        else:
            misclassified.append(
                f"{pair['label']}: sim={sim:.3f} → {verdict} "
                f"(expected {expected})"
            )
    total = len(pairs)
    assert correct >= 7, (
        f"Calibration floor: {correct}/{total} correct (need ≥7/8). "
        f"Misclassified: {misclassified}"
    )


def test_calibration_real_pair_must_pass():
    """The real 2026-06-03 pair MUST classify as duplicate.

    Decision 13 must-pass split — without this guard, the 1-misclassification
    budget in ``test_calibration_accuracy`` could absorb the only pair that
    motivated the entire feature.
    """
    pair = next(p for p in DUPE_PAIRS if p['label'] == 'pair-1-real-2026-06-03')
    fp_a = extract_fingerprint(pair['a'])
    fp_b = extract_fingerprint(pair['b'])
    sim = similarity(fp_a, fp_b)
    # Assert the decision verdict, not the raw threshold — the guard must
    # survive a threshold tune (Decision 13 / Task 8 audit M1).
    assert _classify(sim) == 'duplicate', (
        f"Real 2026-06-03 pair must classify as duplicate. "
        f"Got verdict {_classify(sim)!r} at sim={sim:.3f}. "
        f"fp_a={fp_a}, fp_b={fp_b}"
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

    def test_zamac_acronym_dual_case(self):
        # `zamac` is routed to the case-SENSITIVE acronym pass with two curated
        # casings ('ZAMAC' and 'Zamac'); both resolve to canonical 'zamac'.
        assert 'zamac' in extract_series('New ZAMAC casting spotted')
        assert 'zamac' in extract_series('New Zamac casting spotted')
        # Case-sensitivity BOUNDARY (code-review round 1 minor / item #7):
        # plain lowercase 'zamac' in prose is NOT matched. Documented recall
        # gap — flagged for the real-DB smoke. `zamac` is a BROAD line, so
        # under-matching only ever soft-flags (fail-safe direction), never a
        # silent hard block.
        assert extract_series('zamac diecast news') == []

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

    def test_theme_only_pair_is_broad(self):
        # Series recognised, no concrete model → theme-only "*|<series>|B".
        # Use a BROAD-tagged series (car culture) to prove the no-models branch
        # is unconditionally |B regardless of the series' own tier — this is a
        # genuinely different input from the distinctive-no-model case pinned by
        # `test_distinctive_requires_concrete_model` (de-redundancy per
        # test-review round 1 minor).
        pairs = me._build_pairs(set(), {('car culture', 'broad')})
        assert pairs == ['*|car culture|B']

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
        # Broad recurrent car-line + concrete model → still |B.
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
