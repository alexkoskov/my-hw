# Decisions Log: orangetrack-rendering-fixes

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->

## Task 2: LLM-patch — extend `_PATCHED_TEXT_BLOCK_TYPES` with `list_item`

**Status:** Done
**Commit:** 18ea5a6
**Agent:** main agent
**Summary:** Added `"list_item"` as the fourth element of `_PATCHED_TEXT_BLOCK_TYPES` tuple in `_llm_common.py:106`. Single-line change; the existing `_patch_text_with_ru_paragraphs` filter (`block.get("type") in _PATCHED_TEXT_BLOCK_TYPES`) automatically picks up the new type, closing the variant-B fallback gap where `list_item` block text was left untranslated when LLM returns `blocks=null`. No other edits, including docstring, were made.
**Deviations:** None.

**Reviews:** Skipped (single-line change, smoke + regression tests cover correctness per task spec).

**Verification:**
- `python3 -c "from _llm_common import _PATCHED_TEXT_BLOCK_TYPES; assert 'list_item' in _PATCHED_TEXT_BLOCK_TYPES"` → exit 0 (printed `OK`)
- `python3 -m py_compile _llm_common.py` → exit 0
- `pytest tests/test_openrouter_transcreation.py tests/test_claude_transcreation.py -q` → 43 passed

## Task 1: Parser changes — `<li>`/`<ol>` parsing + heading dispatch + `paragraphs_flat` extension

**Status:** Done
**Commit:** e4ed9fb
**Agent:** main agent
**Summary:** Added explicit `<li>` branch in `_walk` that emits `{type: "list_item", text, runs}` via `_runs_from_tag` (bullet "• " is NOT inserted at parser level — Decision 1 / AC2; empty `<li>` dropped); split `_emit_heading` so h2/h3/h4 emit `{type: "heading", level: 3}` and h5 keeps `type: "paragraph"` (Decisions 2, 3 — preserves `babc67c` carve-out from SESSION-2026-05-06.md break 3); extended `paragraphs_flat` filter to include `"list_item"` so LLM-patch alignment stays correct after Task 2.
**Deviations:** None.

**Reviews:** Wave-1 implementation only — task-level reviewer agents (code-reviewer / security-auditor / test-reviewer) run in Wave 3 (Tasks 6–8) per tech-spec.

**Verification:**
- `python3 -m py_compile orangetrack_source.py` → exit 0.
- `pytest tests/test_orangetrack_source.py -q` → 71 passed (h5-as-paragraph regression `test_h5_emitted_as_paragraph_block` stays green).

## Task 3: Publisher — link helper + bullet rendering + heading

**Status:** Done
**Commit:** 43d078f
**Agent:** main agent
**Summary:** Added module-level `_is_same_site` predicate and `_render_paragraph_with_runs` helper to `telegraph_publisher.py`; rewired the block-rendering loop in `_build_content_from_blocks` so paragraph, heading, and the new `list_item` branch flow through the helper to render same-site `<a>` runs inline. `heading()` closure extended from a single `text` arg to `*children`. Defensive guards: `removeprefix("www.")` (not `lstrip`), strict `http`/`https` scheme, empty/whitespace run.text skip before `str.find`, `urlparse` exception handling, DoS bounds (100 KB text / 100 runs), and list_item leading-bullet stripping before prepending `"• "` (Decisions 4, 5, 7, 9, 10).
**Deviations:** None.

**Reviews:** Wave-1 implementation only — task-level reviewer agents (code-reviewer / security-auditor / test-reviewer) run in Wave 3 (Tasks 6–8) per tech-spec; helper unit tests are written by Task 5 in Wave 2.

**Verification:**
- `python3 -m py_compile telegraph_publisher.py` → exit 0.
- `python3 -c "from telegraph_publisher import _is_same_site, _render_paragraph_with_runs; ..."` litmus → prints `OK` (mailto dropped, lookalike `wwwfake-…` dropped, `www.` normalisation matched).
- `pytest tests/test_telegraph_publisher.py -q` → 39 passed (no regressions).

## Task 4: Unit + integration tests for parser changes

**Status:** Done
**Commit:** 828e503
**Agent:** main agent
**Summary:** Added a `TestListItemParsing` class to `tests/test_orangetrack_source.py` with 10 unit tests (12 collected — `test_h2_h3_h4_parsed_as_heading_level_3` is parametrized over h2/h3/h4) covering `<li>` emission in `<ul>`/`<ol>`, inline-anchor + `<strong>` flattening, empty-`<li>` drop, the AC2 bullet litmus (parser MUST NOT prefix "• "), heading dispatch (h2/h3/h4 → `level=3`, h5 → paragraph regression for `babc67c`, h1/h6 ignored), and `paragraphs_flat` inclusion + DOM ordering. Added `TestOrangetrackRenderingEndToEnd::test_orangetrack_rendering_end_to_end` integration that wires synthetic HTML through `_parse_content_encoded`, manually patches RU strings into `block.text` (isolated from Task 2's `_patch_text_with_ru_paragraphs` per task hint), calls `telegraph_publisher.preview_nodes` keyword-style, and asserts EXACT dict-equality on the four target nodes (`{"tag": "h3", ...}`, paragraph-with-`<a>`, two bulleted list_item paragraphs) plus their relative DOM order. Imported `_parse_content_encoded` into the test module (was previously not exposed in test imports).
**Deviations:** None.

**Reviews:** Wave-2 implementation only — task-level reviewer agents (code-reviewer / security-auditor / test-reviewer) run in Wave 3 (Tasks 6–8) per tech-spec.

**Verification:**
- `pytest tests/test_orangetrack_source.py -q` → 84 passed (71 existing + 13 new collected).
- `pytest tests/test_orangetrack_source.py::TestListItemParsing tests/test_orangetrack_source.py::TestOrangetrackRenderingEndToEnd -v` → 13 passed.

## Task 5: Unit tests for telegraph_publisher helper

**Status:** Done
**Commit:** 78338d7
**Agent:** main agent
**Summary:** Added 25 unit tests to `tests/test_telegraph_publisher.py` covering `_render_paragraph_with_runs` direct (same-site wrap, external/missing/empty/None/malformed href, repeated text first-only, overlapping spans first-wrap-wins, two-runs-same-href independent wrapping, regex-meta-chars literal match via `str.find`, plain-text fallback parametrized over `runs=[]`/`runs=None`), scheme/domain edges (mailto, javascript, empty-netloc, `www.` normalisation both directions, lookalike `wwwfake-…` negative, case-sensitive negative), `list_item` block rendering (bullet prefix, same-site link combine, leading-bullet strip Decision 10), `heading` block rendering (h3, inline link), and DoS bounds (huge text and too-many-runs both with `caplog.at_level(WARNING, logger="telegraph_publisher")` assertion). All assertions are EXACT children-list dict comparisons; for blocks-path tests with `source_url` set, `nodes[0]` is checked to ignore the unrelated `Источник:` footer.
**Deviations:** Delivered 25 tests vs spec "~23" — `test_no_runs_renders_plain_text` is parametrized so counts as 2 collected cases; remaining 23 are 1-to-1 with the TDD anchor.

**Reviews:** Wave-2 implementation only — task-level reviewer agents (code-reviewer / security-auditor / test-reviewer) run in Wave 3 (Tasks 6–8) per tech-spec.

**Verification:**
- `pytest tests/test_telegraph_publisher.py -q` → 64 passed (39 existing + 25 new).
