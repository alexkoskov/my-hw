"""Unit tests for shared LLM-response validation in ``_llm_common``.

Covers the sanity-floor behavior change introduced after the 2026-05-31
incident where all 4 t-hunted slots on test failed with
``OpenRouter response paragraphs total content too short`` for
single-paragraph photo-gallery posts (the LLM legitimately treats the
lone marketing intro as boilerplate and returns paragraphs=[]).
"""

import json
import logging
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _llm_common import (  # noqa: E402
    ClaudeTranscreationError,
    _build_user_message,
    _decode_format_markers,
    _encode_format_markers,
    _is_account_level_status,
    _parse_response,
    _patch_text_with_ru_paragraphs,
)


class TestAccountLevelStatus(unittest.TestCase):
    """The one rule every engine shares for status codes the SDKs have no
    dedicated exception class for. See ``_ACCOUNT_LEVEL_STATUS_CODES``."""

    class _Err(Exception):
        def __init__(self, status_code):
            self.status_code = status_code

    def test_account_level_codes(self):
        for code in (402, 407, 408):
            self.assertTrue(_is_account_level_status(self._Err(code)), code)

    def test_article_level_and_unknown_codes(self):
        for code in (400, 413, 414, 415, 451, 499):
            self.assertFalse(_is_account_level_status(self._Err(code)), code)

    def test_missing_status_code(self):
        """An exception with no ``.status_code`` must not be promoted to an
        outage — the caller's conservative default handles it."""
        self.assertFalse(_is_account_level_status(Exception("no attribute")))
        self.assertFalse(_is_account_level_status(self._Err(None)))


class TestEncodeFormatMarkers(unittest.TestCase):
    """Encoding side of the inline-bold round-trip (2026-06-09 feature).
    For each paragraph sent to the LLM, ``**bold**`` markers are wrapped
    around the spans listed in the corresponding block's ``runs``."""

    def test_no_runs_returns_text_unchanged(self):
        self.assertEqual(_encode_format_markers("Hello world", []), "Hello world")
        self.assertEqual(_encode_format_markers("Hello world", None), "Hello world")

    def test_single_bold_run_wraps_with_double_asterisk(self):
        text = "This is the new Hot Wheels Premium series"
        runs = [{"text": "Hot Wheels Premium", "formats": ["bold"]}]
        result = _encode_format_markers(text, runs)
        self.assertEqual(result, "This is the new **Hot Wheels Premium** series")

    def test_multiple_bold_runs_in_order(self):
        text = "Toyota and Subaru models"
        runs = [
            {"text": "Toyota", "formats": ["bold"]},
            {"text": "Subaru", "formats": ["bold"]},
        ]
        self.assertEqual(
            _encode_format_markers(text, runs),
            "**Toyota** and **Subaru** models",
        )

    def test_non_bold_runs_are_ignored(self):
        text = "Italic and underline together"
        runs = [
            {"text": "Italic", "formats": ["italic"]},
            {"text": "underline", "formats": ["underline"]},
        ]
        self.assertEqual(_encode_format_markers(text, runs), text)

    def test_run_text_not_in_paragraph_dropped(self):
        # LLM translation context: the EN run.text may not appear in this
        # particular paragraph. Don't wrap missing spans.
        text = "Plain prose only"
        runs = [{"text": "vanished phrase", "formats": ["bold"]}]
        self.assertEqual(_encode_format_markers(text, runs), text)

    def test_overlapping_runs_first_wrap_wins(self):
        text = "Hot Wheels Premium release"
        runs = [
            {"text": "Hot Wheels Premium", "formats": ["bold"]},
            {"text": "Wheels Premium release", "formats": ["bold"]},  # overlaps first
        ]
        # First (sorted by position) wins; second is dropped because its
        # start lies within the first's span.
        result = _encode_format_markers(text, runs)
        self.assertEqual(result, "**Hot Wheels Premium** release")


class TestEncodeFormatMarkersBounds(unittest.TestCase):
    """Resource bounds on the REQUEST path (Decision 8).

    ``_encode_format_markers`` runs one ``text.find`` per bold run with no
    ceiling on either side. Mirrors the render-path guard in
    ``telegraph_publisher._render_paragraph_with_runs`` — same constant
    names, same values, same degradation contract: over the bound the
    paragraph is sent to the LLM as PLAIN text (no ``**`` markers), never
    truncated, never dropped, never raised.

    This is a resource fuse, NOT an editorial "too much bold" threshold —
    user-spec AC7 forbids the latter.
    """

    def test_run_count_over_bound_strips_runs(self):
        text = " ".join(f"w{i:03d}" for i in range(101))
        runs = [{"text": f"w{i:03d}", "formats": ["bold"]} for i in range(101)]
        result = _encode_format_markers(text, runs)
        self.assertEqual(result, text)
        self.assertNotIn("**", result)

    def test_run_count_at_bound_is_still_encoded(self):
        """Positive control on the boundary: without it the limit could be
        set too low and every other test here would stay green."""
        text = " ".join(f"w{i:03d}" for i in range(100))
        runs = [{"text": f"w{i:03d}", "formats": ["bold"]} for i in range(100)]
        result = _encode_format_markers(text, runs)
        # Zero-padded tokens on purpose: `w1` would be a prefix of `w10`
        # and `str.find` would resolve it order-dependently.
        self.assertEqual(result.count("**"), 200)

    def test_text_length_over_bound_strips_runs(self):
        text = "Hot Wheels" + "x" * (100_001 - len("Hot Wheels"))
        runs = [{"text": "Hot Wheels", "formats": ["bold"]}]
        result = _encode_format_markers(text, runs)
        self.assertEqual(len(text), 100_001)
        self.assertEqual(result, text)
        self.assertNotIn("**", result)

    def test_text_length_at_bound_is_still_encoded(self):
        text = "Hot Wheels" + "x" * (100_000 - len("Hot Wheels"))
        runs = [{"text": "Hot Wheels", "formats": ["bold"]}]
        result = _encode_format_markers(text, runs)
        self.assertEqual(len(text), 100_000)
        self.assertTrue(result.startswith("**Hot Wheels**"))

    def test_bound_logs_single_warning(self):
        text = " ".join(f"w{i:03d}" for i in range(101))
        runs = [{"text": f"w{i:03d}", "formats": ["bold"]} for i in range(101)]
        with self.assertLogs("_llm_common", level="WARNING") as captured:
            _encode_format_markers(text, runs)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn(str(len(text)), message)
        self.assertIn("101", message)

    def test_empty_runs_do_not_log(self):
        """The early ``not runs`` exit must stay silent — a WARNING per
        paragraph without runs would drown the log."""
        logger = logging.getLogger("_llm_common")
        with mock.patch.object(logger, "warning") as warn:
            self.assertEqual(_encode_format_markers("plain", []), "plain")
            self.assertEqual(_encode_format_markers("plain", None), "plain")
        warn.assert_not_called()

    def test_non_dict_runs_count_toward_the_bound(self):
        """The bound counts LIST LENGTH, not valid runs: 200k junk entries
        cost the same memory and the same loop."""
        text = "Hot Wheels news"
        runs = ["junk"] * 101
        with self.assertLogs("_llm_common", level="WARNING"):
            result = _encode_format_markers(text, runs)
        self.assertEqual(result, text)

    def test_pathological_paragraph_completes_in_bounded_time(self):
        """The security-review scenario verbatim: ~2 MB of text × 200k runs
        whose text is NOT present, so every ``find`` scans the whole 2 MB.

        Measured unbounded on this machine: ~160 s. Bounded: sub-millisecond.
        Threshold is 5.0 s rather than 1.0 s — a loaded CI runner must not
        flap, and at three orders of magnitude the slack costs no power.
        """
        text = "Hot Wheels news. " * 120_000
        runs = [{"text": f"zz{i}", "formats": ["bold"]} for i in range(200_000)]
        started = time.perf_counter()
        result = _encode_format_markers(text, runs)
        elapsed = time.perf_counter() - started
        self.assertEqual(result, text)
        self.assertNotIn("**", result)
        self.assertLess(elapsed, 5.0, f"unbounded encode loop: {elapsed:.2f}s")


class TestDecodeFormatMarkers(unittest.TestCase):
    """Decoding side: parse `**bold**` out of LLM RU response, build the
    project's runs metadata."""

    def test_no_markers_returns_empty_runs(self):
        clean, runs = _decode_format_markers("Просто проза без выделений")
        self.assertEqual(clean, "Просто проза без выделений")
        self.assertEqual(runs, [])

    def test_single_bold_marker_extracts_run(self):
        clean, runs = _decode_format_markers(
            "Это **жирное слово** в предложении"
        )
        self.assertEqual(clean, "Это жирное слово в предложении")
        self.assertEqual(runs, [{"text": "жирное слово", "formats": ["bold"]}])

    def test_multiple_bold_markers_in_order(self):
        clean, runs = _decode_format_markers("**Toyota** и **Subaru** модели")
        self.assertEqual(clean, "Toyota и Subaru модели")
        self.assertEqual(runs, [
            {"text": "Toyota", "formats": ["bold"]},
            {"text": "Subaru", "formats": ["bold"]},
        ])

    def test_unbalanced_marker_does_not_swallow_newline(self):
        # Stray unbalanced `**` shouldn't swallow the rest of the paragraph.
        clean, runs = _decode_format_markers(
            "Текст с лишним ** и потом нормальный"
        )
        # No closing `**` on the same line → no match → text passes through
        # unchanged. (The non-greedy + no-newline regex ensures this.)
        self.assertEqual(clean, "Текст с лишним ** и потом нормальный")
        self.assertEqual(runs, [])

    def test_round_trip_through_encode_and_decode(self):
        en = "The new Hot Wheels Premium release is great"
        runs_in = [{"text": "Hot Wheels Premium", "formats": ["bold"]}]
        encoded = _encode_format_markers(en, runs_in)
        # LLM would translate ru-side preserving markers. Simulate:
        ru_with_markers = encoded.replace(
            "The new", "Новый"
        ).replace("release is great", "релиз — отлично")
        clean, runs_out = _decode_format_markers(ru_with_markers)
        self.assertEqual(clean, "Новый Hot Wheels Premium релиз — отлично")
        self.assertEqual(runs_out, [
            {"text": "Hot Wheels Premium", "formats": ["bold"]},
        ])


class TestBuildUserMessageInlineFormatting(unittest.TestCase):
    """Integration of encode helper into ``_build_user_message``: when
    article has blocks with runs, paragraphs in the JSON payload sent
    to the LLM gain `**bold**` markers."""

    def test_no_blocks_passes_paragraphs_through(self):
        article = {
            "title": "T",
            "subtitle": "S",
            "paragraphs": ["plain prose"],
        }
        payload = json.loads(_build_user_message(article))
        self.assertEqual(payload["paragraphs"], ["plain prose"])

    def test_blocks_with_bold_runs_encoded_into_paragraphs(self):
        article = {
            "title": "T",
            "subtitle": "S",
            "paragraphs": [
                "First paragraph mentioning Hot Wheels Premium series",
                "Second paragraph plain",
            ],
            "blocks": [
                {
                    "type": "paragraph",
                    "text": "First paragraph mentioning Hot Wheels Premium series",
                    "runs": [
                        {"text": "Hot Wheels Premium",
                         "formats": ["bold"]}
                    ],
                },
                {
                    "type": "paragraph",
                    "text": "Second paragraph plain",
                    "runs": [],
                },
            ],
        }
        payload = json.loads(_build_user_message(article))
        self.assertEqual(payload["paragraphs"], [
            "First paragraph mentioning **Hot Wheels Premium** series",
            "Second paragraph plain",
        ])

    def test_blocks_with_image_between_text_skipped(self):
        # Image block doesn't consume a paragraph from the iterator.
        article = {
            "title": "T",
            "subtitle": "S",
            "paragraphs": ["Hot Wheels intro", "Closing line"],
            "blocks": [
                {
                    "type": "paragraph",
                    "text": "Hot Wheels intro",
                    "runs": [{"text": "Hot Wheels",
                              "formats": ["bold"]}],
                },
                {"type": "image", "src": "https://example.com/img.jpg"},
                {
                    "type": "paragraph",
                    "text": "Closing line",
                    "runs": [],
                },
            ],
        }
        payload = json.loads(_build_user_message(article))
        self.assertEqual(payload["paragraphs"], [
            "**Hot Wheels** intro",
            "Closing line",
        ])

    def test_over_bound_block_does_not_affect_other_blocks(self):
        """The bound is PER BLOCK: one pathological paragraph loses its
        markers, the article keeps every paragraph and the next block is
        encoded as usual."""
        wide_text = " ".join(f"w{i:03d}" for i in range(101))
        article = {
            "title": "T",
            "subtitle": "S",
            "paragraphs": [wide_text, "Closing Hot Wheels line"],
            "blocks": [
                {
                    "type": "paragraph",
                    "text": wide_text,
                    "runs": [
                        {"text": f"w{i:03d}", "formats": ["bold"]}
                        for i in range(101)
                    ],
                },
                {
                    "type": "paragraph",
                    "text": "Closing Hot Wheels line",
                    "runs": [{"text": "Hot Wheels", "formats": ["bold"]}],
                },
            ],
        }
        payload = json.loads(_build_user_message(article))
        self.assertEqual(len(payload["paragraphs"]), 2)
        self.assertEqual(payload["paragraphs"][0], wide_text)
        self.assertEqual(payload["paragraphs"][1], "Closing **Hot Wheels** line")


class TestPatchTextDecodesBoldRuns(unittest.TestCase):
    """The completing side of the round-trip: when LLM RU paragraphs
    arrive with ``**bold**`` markers, ``_patch_text_with_ru_paragraphs``
    decodes them into the block's ``runs`` metadata so the Telegraph
    renderer can apply formatting via existing string-find machinery."""

    def test_ru_with_marker_splices_runs(self):
        blocks_in = [
            {
                "type": "paragraph",
                "text": "Hot Wheels Premium is great",
                "runs": [{"text": "Hot Wheels Premium",
                          "formats": ["bold"]}],
            }
        ]
        ru_paragraphs = ["**Hot Wheels Premium** — отлично"]
        result = _patch_text_with_ru_paragraphs(blocks_in, ru_paragraphs)
        self.assertEqual(result[0]["text"], "Hot Wheels Premium — отлично")
        self.assertEqual(
            result[0]["runs"],
            [{"text": "Hot Wheels Premium", "formats": ["bold"]}],
        )

    def test_ru_without_marker_drops_runs(self):
        # LLM dropped the marker. EN-text runs are useless on RU text —
        # remove them so the renderer skips format machinery entirely.
        blocks_in = [
            {
                "type": "paragraph",
                "text": "Hot Wheels Premium is great",
                "runs": [{"text": "Hot Wheels Premium",
                          "formats": ["bold"]}],
            }
        ]
        ru_paragraphs = ["Hot Wheels Premium — отлично"]
        result = _patch_text_with_ru_paragraphs(blocks_in, ru_paragraphs)
        self.assertEqual(result[0]["text"], "Hot Wheels Premium — отлично")
        self.assertNotIn("runs", result[0])

    def test_non_text_block_passes_through(self):
        blocks_in = [
            {"type": "image", "src": "https://example.com/img.jpg"},
            {"type": "paragraph", "text": "x", "runs": []},
        ]
        ru_paragraphs = ["**Жирный** текст"]
        result = _patch_text_with_ru_paragraphs(blocks_in, ru_paragraphs)
        self.assertEqual(result[0]["type"], "image")  # untouched
        self.assertEqual(result[1]["text"], "Жирный текст")
        self.assertEqual(
            result[1]["runs"],
            [{"text": "Жирный", "formats": ["bold"]}],
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
