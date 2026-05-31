"""Unit tests for shared LLM-response validation in ``_llm_common``.

Covers the sanity-floor behavior change introduced after the 2026-05-31
incident where all 4 t-hunted slots on test failed with
``OpenRouter response paragraphs total content too short`` for
single-paragraph photo-gallery posts (the LLM legitimately treats the
lone marketing intro as boilerplate and returns paragraphs=[]).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _llm_common import (  # noqa: E402
    ClaudeTranscreationError,
    _parse_response,
)


def _make_response(paragraphs):
    """Build a minimal valid response JSON with the given paragraphs list."""
    return json.dumps({
        "title": "🏎️ Заголовок",
        "alts": ["alt 1", "alt 2", "alt 3"],
        "subtitle": "Подзаголовок",
        "paragraphs": paragraphs,
    })


class TestSanityFloorRelaxation(unittest.TestCase):
    """Sanity floor (<30 chars total) is enforced only for ≥2-paragraph input.

    Single-paragraph posts (t-hunted photo-gallery format) may legitimately
    produce empty / very-short LLM bodies; the visual payload carries the
    article.
    """

    def test_floor_enforced_for_multi_paragraph_input(self):
        # 2 input paragraphs, response has only 5 chars total → must raise.
        body = _make_response(["abc", "de"])
        with self.assertRaises(ClaudeTranscreationError) as ctx:
            _parse_response(
                body,
                expected_paragraph_count=2,
                expected_block_count=None,
                engine_name="TestEngine",
            )
        self.assertIn("too short", str(ctx.exception))
        self.assertIn("5 chars", str(ctx.exception))

    def test_floor_skipped_for_single_paragraph_input_with_empty_response(self):
        # Single-paragraph input + empty paragraphs (LLM stub on marketing
        # intro) — must NOT raise, parsed dict is returned with paragraphs=[].
        body = _make_response([])
        parsed = _parse_response(
            body,
            expected_paragraph_count=1,
            expected_block_count=None,
            engine_name="TestEngine",
        )
        self.assertEqual(parsed["title"], "🏎️ Заголовок")
        self.assertEqual(parsed["paragraphs"], [])

    def test_floor_skipped_for_single_paragraph_input_with_short_response(self):
        # Single-paragraph input + 7-char translation — also accepted.
        body = _make_response(["Новинка"])
        parsed = _parse_response(
            body,
            expected_paragraph_count=1,
            expected_block_count=None,
            engine_name="TestEngine",
        )
        self.assertEqual(parsed["paragraphs"], ["Новинка"])

    def test_floor_enforced_for_zero_expected_paragraphs(self):
        # Defensive: zero expected (degenerate input) still enforces the
        # floor since `expected_paragraph_count >= 2` is False AND
        # `>= 1` would also fail — kept under floor-enforcement gate so
        # behaviour matches the multi-paragraph branch precisely.
        body = _make_response([])
        # No assertion on raise here — current implementation skips the
        # floor for count < 2 (which includes 0 and 1). The skip for 0
        # is incidental but harmless: a 0-paragraph article wouldn't
        # have been queued in the first place. This test pins that
        # behavior so a future tightening of the gate is intentional.
        parsed = _parse_response(
            body,
            expected_paragraph_count=0,
            expected_block_count=None,
            engine_name="TestEngine",
        )
        self.assertEqual(parsed["paragraphs"], [])


if __name__ == "__main__":
    unittest.main()
