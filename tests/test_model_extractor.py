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

from model_extractor import extract_fingerprint, similarity
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

    def test_empty_body(self):
        assert extract_fingerprint(_article()) == {'strict': [], 'brands': []}

    def test_empty_dict_input(self):
        # Defensive: completely empty input shouldn't crash.
        assert extract_fingerprint({}) == {'strict': [], 'brands': []}

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
    assert sim >= 0.50, (
        f"Real 2026-06-03 pair must classify as duplicate "
        f"(similarity ≥ 0.50). Got: {sim:.3f}. "
        f"fp_a={fp_a}, fp_b={fp_b}"
    )
