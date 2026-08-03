# Wave 3 — Test Audit (Task 8)

**Verdict: PASS**

## Summary

Tests post-Wave 2 (commits `828e503` parser tests and `78338d7` publisher tests) are litmus-grade. The integration gate `test_orangetrack_rendering_end_to_end` uses EXACT dict equality on the four target nodes (heading, paragraph-with-anchor, two bulleted list items) — this is the single most important regression lock of the feature, and it is correctly implemented. Every required edge-case litmus is physically present in the codebase: AC2 negative-bullet assertion, overlapping-runs first-wins-with-second-as-plain-text, lookalike `wwwfake-` rejection, case-sensitive negative, mailto/javascript scheme drop, regex meta-chars via `str.find`, `babc67c` h5-paragraph regression, bullet-doubling guard (Decision 10), and DoS bounds with proper `caplog`-asserted WARNING. Pyramid balance is healthy for an M-size feature: 13 new parser tests + 25 new publisher tests + 1 integration end-to-end (collected as 38 cases incl. parametrized expansion).

## Litmus checklist

| Litmus | Test (file:class::name) | Status |
|---|---|---|
| AC2 negative — parser does NOT prepend bullet | `tests/test_orangetrack_source.py::TestListItemParsing::test_li_block_text_does_not_contain_bullet` (line 1015) — asserts `block["text"] == "Ferrari"`, `"•" not in block["text"]`, `not block["text"].startswith("• ")` | PASS |
| Integration end-to-end uses EXACT dict comparison (not substring) | `tests/test_orangetrack_source.py::TestOrangetrackRenderingEndToEnd::test_orangetrack_rendering_end_to_end` (line 1103) — asserts `h3_nodes == [{"tag": "h3", "children": ["Раздел"]}]`, `expected_para_with_anchor in p_nodes` with full nested dict literal incl. `attrs` and `children`, plus order assertion `idx_h3 < idx_para < idx_li1 < idx_li2` | PASS |
| Overlapping runs — first wins, second appears as plain text in children | `tests/test_telegraph_publisher.py::TestRenderParagraphWithRuns::test_overlapping_runs_first_wins_second_appears_plain` (line 677) — input `text="ABCDE"` with overlapping `BCD` and `CDE` spans, asserts EXACT `["A", {<a href="…/a">BCD</a>}, "E"]` (note: second run's text "CDE" partially survives via "E" suffix; the wrapping logic correctly leaves the un-wrapped tail as plain text) | PASS |
| Bullet-doubling guard (Decision 10) | `tests/test_telegraph_publisher.py::TestListItemRendering::test_list_item_strips_leading_bullet_in_text` (line 853) — asserts `nodes == [{"tag": "p", "children": ["• ", "Ferrari"]}]` for input `text="• Ferrari"` | PASS |
| Lookalike-domain rejection (`removeprefix` vs `lstrip`) | `tests/test_telegraph_publisher.py::TestSchemesAndDomainEdges::test_lookalike_domain_not_matched_negative` (line 809) — input href `https://wwwfake-orangetrackdiecast.com/f`, asserts `children == [text]` (plain text, no wrap) | PASS |
| Case-sensitive match negative | `tests/test_telegraph_publisher.py::TestSchemesAndDomainEdges::test_case_sensitive_match_negative` (line 816) — `run.text="Mercedes"` vs `text="I love mercedes cars"`, asserts plain text only | PASS |
| `mailto:` scheme dropped | `tests/test_telegraph_publisher.py::TestSchemesAndDomainEdges::test_mailto_scheme_dropped` (line 759) — `mailto:admin@orangetrackdiecast.com`, asserts plain text | PASS |
| `javascript:` scheme dropped | `tests/test_telegraph_publisher.py::TestSchemesAndDomainEdges::test_javascript_scheme_dropped` (line 765) — asserts plain text | PASS |
| DoS bound — huge text fall-through + WARNING | `tests/test_telegraph_publisher.py::TestDoSBounds::test_dos_bound_skips_helper_on_huge_text` (line 894) — `text` length `_MAX_TEXT_FOR_RUNS+1`, asserts `children == [text]` AND `caplog.records` contains `"DoS bound" in r.message and r.levelno == logging.WARNING` | PASS |
| DoS bound — too many runs fall-through + WARNING | `tests/test_telegraph_publisher.py::TestDoSBounds::test_dos_bound_skips_helper_on_too_many_runs` (line 905) — same assertion shape | PASS |
| H5 regression (`babc67c`) | `tests/test_orangetrack_source.py::TestListItemParsing::test_h5_remains_paragraph` (line 1041) — asserts `"heading" not in types` and `"Section"` text appears in a paragraph block | PASS |
| Regex meta-chars literal `str.find` | `tests/test_telegraph_publisher.py::TestRenderParagraphWithRuns::test_run_text_with_regex_meta_chars` (line 715) — `run.text=".?*()"`, EXACT children list with link-wrapped `.?*()` | PASS |

## AC coverage matrix

| AC | Description | Test |
|---|---|---|
| AC1 | `<li>` from `<ul>`/`<ol>` → bulleted paragraph; nested formatting preserved | `test_li_parsed_as_list_item_block`, `test_ol_parsed_as_list_item_block`, `test_li_with_inline_anchor`, `test_li_with_strong`, `test_empty_li_dropped`, `test_list_item_renders_with_bullet_prefix`, `test_list_item_with_same_site_link_combines` |
| AC2 | Bullet added at publisher (after LLM), not at parser | `test_li_block_text_does_not_contain_bullet` (parser-side negative), `test_list_item_renders_with_bullet_prefix` (publisher-side positive), `test_list_item_strips_leading_bullet_in_text` (Decision 10 guard) |
| AC3 | h2/h3/h4 → uniform visual heading | `test_h2_h3_h4_parsed_as_heading_level_3` (parametrized), `test_heading_block_renders_h3` |
| AC4 | h5 stays paragraph (`babc67c`) | `test_h5_remains_paragraph` |
| AC5 | h1/h6 ignored | `test_h1_h6_ignored` |
| AC6 | Same-site links clickable, exact case-sensitive substring, first-occurrence wrap | `test_render_paragraph_with_same_site_link`, `test_run_text_repeated_first_only_wrapped`, `test_case_sensitive_match_negative`, `test_www_prefix_normalized_for_same_site` |
| AC7 | External-domain links → plain text | `test_render_paragraph_with_external_link_dropped` |
| AC8 | run.text not in block.text → silent drop | `test_run_text_not_in_paragraph_text_dropped` |
| AC9 | Edge cases never crash: empty/None/malformed href, regex chars, repeated href, empty run.text, overlapping runs | `test_run_with_empty_text_skipped`, `test_run_with_none_href_skipped`, `test_run_with_malformed_href_skipped`, `test_run_text_with_regex_meta_chars`, `test_two_runs_same_href_independent_wrapping`, `test_overlapping_runs_first_wins_second_appears_plain`, `test_no_runs_renders_plain_text` (parametrized over `[]`/`None`), `test_empty_netloc_dropped_when_source_also_empty` |
| AC10 | All existing tests stay green | Verified by Task 4 / Task 5 reports (`pytest tests/test_orangetrack_source.py -q` → 84 passed; `pytest tests/test_telegraph_publisher.py -q` → 64 passed). Final regression confirmation = Task 9 (Wave 4 pre-deploy QA). |
| AC11 | End-to-end synthetic-HTML → exact node-tree | `test_orangetrack_rendering_end_to_end` (the keystone integration test) |

All ACs have at least one explicit test. No coverage gap.

## Pyramid balance

- New parser tests in `tests/test_orangetrack_source.py`: 10 unit tests in `TestListItemParsing` (collected as 12 cases — `test_h2_h3_h4_parsed_as_heading_level_3` is parametrized over h2/h3/h4) + 1 integration in `TestOrangetrackRenderingEndToEnd` = **13 collected cases**.
- New publisher tests in `tests/test_telegraph_publisher.py`: across `TestRenderParagraphWithRuns` (10 cases), module-level parametrized `test_no_runs_renders_plain_text` (2 cases), `TestSchemesAndDomainEdges` (6 cases), `TestListItemRendering` (3 cases), `TestHeadingRendering` (2 cases), `TestDoSBounds` (2 cases) = **25 collected cases**.
- **Total: 38 new test cases (37 unit + 1 integration)**. Tech-spec target was "~30 unit + 1 integration"; actual is slightly above target due to deliberate parametrization (h2/h3/h4 split, `runs=[]`/`runs=None` split, www-prefix normalization in both directions in one body). No bloat.
- Distribution is healthy for an M-size feature touching three source files: many fast unit tests, one integration test that exercises the full pipeline, no E2E (correctly — pytest-only project).

## Findings

No critical, no major, no minor findings.

Notes (informational only, not findings):

- The `test_overlapping_runs_first_wins_second_appears_plain` test uses inputs `BCD` and `CDE` which only partially overlap (positions 1-3 and 2-4 in `ABCDE`); the surviving `E` segment validates the "second appears as plain text" property exactly as Decision 5 prescribes — the assertion that the un-wrapped tail of the second span survives in `children` is the litmus signal. The choice of partially-overlapping fixtures (rather than fully-overlapping) is deliberate and arguably stronger because it pins both the exclusion of the second `<a>` AND the survival of its non-overlapping suffix as plain text.
- The DoS-bound tests correctly use `caplog.at_level(logging.WARNING, logger="telegraph_publisher")` and assert both the fall-through behaviour (`children == [text]`) AND the WARNING log message containing the literal `"DoS bound"` substring (which matches the source-side log message at `telegraph_publisher.py:182-183`). No vacuous `caplog`-only or behaviour-only assertion.
- Every assertion in the audited test set is exact-equality on dict structures or full children lists. No `mock.called` count-only assertions, no `assert in str(...)` substring sniffing on the structural integration test.
- Litmus test (delete-the-line-and-see): if the bullet-prepend in `_build_content_from_blocks` list_item branch were removed, `test_list_item_renders_with_bullet_prefix`, `test_list_item_with_same_site_link_combines`, `test_list_item_strips_leading_bullet_in_text`, and the integration test would all fail. If the `removeprefix("www.")` were swapped to `lstrip("www.")`, `test_lookalike_domain_not_matched_negative` would fail. If `_render_paragraph_with_runs` skipped its scheme check, `test_mailto_scheme_dropped` and `test_javascript_scheme_dropped` would fail. Each litmus protects a real failure path.

## Recommendations

- Proceed to Wave 4 (Task 9 — Pre-deploy QA). No remedial iteration of Tasks 4 or 5 needed.
- No tech-spec § Testing Strategy edit recommended — actual test set matches the spec near-1:1, and the deviations (parametrization expansions) are improvements, not gaps.
- Suggest the operator note in the deploy log that this audit confirmed the integration-gate exact-dict assertion — that node-tree assertion is what catches a regression of the form "node is present but children order is broken", which substring assertions would silently miss.

---

**Audit complete.** Verdict at top: `PASS`.
