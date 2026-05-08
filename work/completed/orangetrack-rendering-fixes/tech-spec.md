---
created: 2026-05-07
status: approved
branch: dev
size: M
---

# Tech Spec: orangetrack-rendering-fixes

## Solution

Three localised changes to fix orangetrack visual rendering bugs:

1. **Bug 1 — `<li>` parsing.** In `orangetrack_source._walk`, when encountering `<li>` (children of `<ul>` or `<ol>`), emit content via existing `_runs_from_tag` extractor as a new `{type: "list_item", text: …, runs: [...]}` block (inline in `_walk`, no separate helper function). Bullet "• " is NOT inserted at this stage. Add `"list_item"` to `_PATCHED_TEXT_BLOCK_TYPES` in `_llm_common.py` so the LLM-patch fallback updates text. Add `"list_item"` to the `paragraphs_flat` extraction in `orangetrack_source.py` so the LLM translates the text. In `telegraph_publisher._build_content_from_blocks` (the actual block-rendering loop, NOT `preview_nodes` which is a dispatcher), render `list_item` blocks as `<p>• {text}</p>` — bullet prepended **after** translation, immune to LLM stripping.

2. **Bug 2 — heading levels.** Modify `orangetrack_source._emit_heading` to accept and emit a `level` argument. For h2/h3/h4 → emit `type: "heading", level: 3`. For h5 → keep current behavior (emit as `type: "paragraph"`, preserves `babc67c` fix from SESSION-2026-05-06.md break 3). Modify the call sites in `_walk` to dispatch based on tag name: 2/3/4 → heading, 5 → paragraph.

3. **Bug 3 — same-site link rendering.** Add a `_render_paragraph_with_runs(text, runs, source_url)` helper and `_is_same_site(href, source_netloc)` predicate in `telegraph_publisher.py`. The helper iterates runs in their original order, filters to `_is_same_site` runs only, and for each run with non-empty `run.text` finds the first occurrence of `run.text` in `text` (case-sensitive `str.find`), recording `(start, end, href)`. Spans are sorted by start position; **overlapping spans are resolved by first-wrap-wins** (later overlapping run rendered as plain text — Telegraph forbids nested `<a>`). The text is split into segments around surviving spans; each surviving span becomes an `<a>`-node, surrounding segments are plain string children. `_build_content_from_blocks` paragraph/heading/list_item rendering calls this helper when block has non-empty `runs` and a same-site `source_url`; otherwise falls through to the current `nodes.append(p(block["text"]))` path.

The integration test in `tests/test_orangetrack_source.py` verifies all three bugs together: synthetic orangetrack HTML with `<ul><li>`, `<h3>`, `<a href>` to orangetrackdiecast.com → parser → simulated LLM-patch → `preview_nodes` → exact assertions on resulting Telegraph node-tree dict structure (not substring match).

## Architecture

### What we're building/modifying

- **`orangetrack_source.py`** — modify `_walk` to handle `<li>` (and `<ol>` — both treated as wrappers whose `<li>` children become `list_item` blocks); modify `_emit_heading` and its dispatch in `_walk` (h2/h3/h4 → heading level=3, h5 → paragraph); add `"list_item"` to `paragraphs_flat` extraction.
- **`_llm_common.py`** — add `"list_item"` to `_PATCHED_TEXT_BLOCK_TYPES`.
- **`telegraph_publisher.py`** — add `_render_paragraph_with_runs` and `_is_same_site` helpers; modify the block-rendering loop inside `_build_content_from_blocks` (the actual rendering function — `preview_nodes` is just its dispatcher) to use the helper for paragraph/heading/list_item blocks; render `list_item` as `<p>• {helper_output}</p>`.
- **Tests:** new tests in `tests/test_orangetrack_source.py` (unit + integration) and `tests/test_telegraph_publisher.py` (unit). Existing `_PATCHED_TEXT_BLOCK_TYPES` is exercised through `tests/test_openrouter_transcreation.py` — no separate test_llm_common.py file is needed.

### How it works

```
orangetrack HTML
  └── orangetrack_source.fetch_orangetrack_article
        ├── _walk(soup)                                      # parse DOM
        │     ├── <ul>/<ol> → recurse into children          # existing wrapper-recursion
        │     ├── <li> → emit type='list_item' block          # NEW: inline _runs_from_tag extraction
        │     ├── <h2>/<h3>/<h4> → _emit_heading(level=3)     # CHANGED: type='heading' level=3
        │     └── <h5> → _emit_heading(emit_as_paragraph=True) # UNCHANGED: type='paragraph' (preserves babc67c)
        ├── filter_blocks(blocks)                             # existing
        └── paragraphs_flat = [b.text for b in blocks if type in (paragraph, heading, list_item)]   # CHANGED: add 'list_item'

LLM transcreation (claude/openrouter/etc)
  └── transcreate_via_<engine>
        ├── LLM call            # returns ru_paragraphs
        └── _patch_text_with_ru_paragraphs(blocks_in, ru_paragraphs)   # consumes ru for type ∈ ('lead', 'paragraph', 'heading', 'list_item')   # CHANGED via _PATCHED_TEXT_BLOCK_TYPES
              # block.text replaced by ru, block.type and block.runs preserved

Telegraph render
  └── telegraph_publisher.publish_article
        └── preview_nodes(...)   # dispatcher
              └── _build_content_from_blocks(...)   # actual rendering loop
                    for each block:
                      if type == 'paragraph':
                          children = _render_paragraph_with_runs(block.text, block.runs, source_url)   # CHANGED: helper invocation
                          nodes.append(p(*children))
                      elif type == 'heading':
                          children = _render_paragraph_with_runs(block.text, block.runs, source_url)   # CHANGED
                          nodes.append(heading(level, *children))
                      elif type == 'list_item':                                                          # NEW
                          children = _render_paragraph_with_runs(block.text, block.runs, source_url)
                          nodes.append(p('• ', *children))
                      # image/video unchanged
```

### Shared resources

None. All edits are local function changes and helpers; no globals, no singletons, no shared state.

## Decisions

### Decision 1: New block type `list_item` for `<li>` content; bullet prepended in publisher
**Decision:** Parser emits `<li>` content as `{type: "list_item", text: …, runs: [...]}`. Bullet "• " is prepended in `telegraph_publisher._build_content_from_blocks` ONLY when rendering — never in the parser, never in the text sent to the LLM.
**Rationale (US AC1, AC2):** AC2 locks bullet insertion to publisher (after LLM) — guarantees the bullet always appears in published Telegraph article and is immune to LLM stripping or translation. Using a dedicated `list_item` type (rather than a flag on `paragraph`) keeps the block-shape model compositional and leaves a clean publisher-side branch.
**Alternatives considered:**
- *Parser inserts "• " into block.text before LLM:* rejected — LLM might translate or remove the bullet (Risk 4 in user-spec).
- *Flag `is_list_item: True` on paragraph blocks:* rejected — couples list-item semantics to paragraph; harder to audit downstream code.
- *No new type, render `<ul>/<li>` directly:* rejected — Telegraph API does not support `<ul>/<li>` (only `<p>/<h3>/<h4>/<figure>/<a>`).

### Decision 2: H5 stays paragraph, h2/h3/h4 become heading level=3
**Decision:** `_emit_heading` for h2/h3/h4 emits `type: "heading", level: 3`. For h5 — preserve current behavior (emit as `type: "paragraph"`).
**Rationale (US AC3, AC4):** SESSION-2026-05-06.md break 3 documents that h5 on orangetrack is used for in-paragraph section markers; rendering h5 as bold/larger Telegraph headings looked visually uneven. h2/h3/h4 on orangetrack are full section headers (model name = section); they SHOULD be visually distinguished. This carve-out preserves `babc67c` while restoring visual hierarchy where it belongs.
**Alternatives considered:**
- *All h-tags → heading:* rejected — reverses `babc67c` decision; breaks h5-heavy articles visually.
- *All h-tags → paragraph (current):* rejected — user complaint; sections lose distinction.
- *Two-level hierarchy (h2 → level 3, h3/h4 → level 4):* rejected — Telegraph supports only h3/h4; orangetrack typically has one section level.

### Decision 3: All headings get `level: 3`
**Decision:** All h2/h3/h4 → `level: 3` (single visual treatment). H1/h6 → ignored (current behavior).
**Rationale (US AC3, AC5):** Single-level hierarchy is simpler and matches orangetrack's typical structure (one section level).
**Alternatives considered:**
- *level=4:* rejected — Telegraph renders h4 smaller/lighter than h3; user wants prominent section separation.
- *Map h2→3, h3→3, h4→4:* rejected — adds complexity for no clear benefit.

### Decision 4: Same-site detection — strict scheme + non-empty netloc + safe www-prefix handling
**Decision:** A run's href is "same-site" iff ALL of the following hold:
- `urlparse(href)` succeeds without raising;
- `urlparse(href).scheme.lower()` is in `("http", "https")` — rejects `mailto:`, `javascript:`, `data:`, etc.;
- `urlparse(href).netloc` is non-empty;
- After lower-casing and stripping `www.` prefix from BOTH sides via `removeprefix("www.")` (NOT `lstrip("www.")` — character-set strip is wrong and can match `wwwfake-site.com`), `urlparse(href).netloc` equals `urlparse(source_url).netloc`.

Empty/malformed hrefs and non-http(s) schemes return False. On `urlparse` raising an exception, the helper logs a single WARNING with truncated href + exception class name (per security audit minor finding 6) and returns False.
**Rationale (US AC6, AC7, AC9; security audit critical + major):** Strict scheme check prevents `mailto:`-leak (empty-netloc on both sides would otherwise match if `source_url` is also empty/malformed). Non-empty netloc prevents `<a href="javascript:alert(1)">` and similar from passing. `removeprefix` is exact-prefix-stripping (Python 3.9+); `lstrip("www.")` is a character-set strip vulnerable to `wwwfake-site.com` and `worangetrackdiecast.com` lookalike-domain bypass (security audit critical finding).
**Alternatives considered:**
- *Hardcode "orangetrackdiecast.com" string:* rejected — `urlparse` is more robust against subdomain edge cases.
- *`lstrip("www.")` shortcut:* rejected — character-class strip; phishing vector.
- *Allow non-http schemes for `<a>` rendering:* rejected — out of scope for this fix; orangetrack runs metadata is HTTP-only in practice.

### Decision 5: First-wrap-wins for overlapping link spans; later overlapping run renders as plain text
**Decision:** When the same paragraph has multiple same-site runs whose `run.text` substrings overlap in `block.text`, the run with the earliest start position is rendered as `<a>`. Later overlapping runs are rendered as plain text within their found-position substring (i.e., the text appears unwrapped — NOT removed; the substring still appears in the paragraph).
**Rationale (US AC9 + adequacy validator round-1 minor):** Telegraph forbids nested `<a>`. Earliest-start is deterministic and matches reading order. Dropped run's text already exists in `block.text` — it remains plain text exactly as it would have if no link was matched.
**Alternatives considered:**
- *Wrap longer span:* rejected — adds length-comparison complexity for marginal benefit.
- *Skip both overlapping runs (none rendered):* rejected — first-wrap-wins is more user-favorable.

### Decision 6: Substring approach (not sentinel-token) for link preservation
**Decision:** Same-site link preservation uses post-translation substring search (`run.text` in `block.text`). Sentinel-token alternative (replace run.text with `__LINK_N__` before LLM, restore after) is documented but deferred to a future Phase-2 feature.
**Rationale (US Risk 4):** Substring approach changes only 2 source files. Sentinel-token would touch `_llm_common._build_user_message`, `_parse_response`, and require careful prompt engineering. MVP wins on velocity; Phase-2 if substring proves too brittle.

### Decision 7: Helper `_render_paragraph_with_runs` invoked for paragraph, heading, AND list_item
**Decision:** All three text-bearing block types call `_render_paragraph_with_runs` in `_build_content_from_blocks`. Empty/None `runs` → helper returns `[block.text]` directly (single-element list, no link processing); caller still uses helper for symmetry.
**Rationale [TECHNICAL]:** Single render path = single regression surface. Cost is one helper call; benefit is consistent link rendering and uniform code path.

### Decision 8: List items don't carry explicit ordering between `<ol>` and `<ul>`
**Decision:** Both `<ul>` and `<ol>` children get the same "• " bullet prefix.
**Rationale (US AC1 — "из исходной `<ul>` или `<ol>`"):** AC1 explicitly treats both list types identically. Telegraph has no `<ol>`/`<ul>` distinction in our render path. Numbering `<ol>` items would require state in the loop and complicate the helper.
**Alternatives considered:**
- *Number `<ol>` items as "1. ", "2. " etc.:* rejected per AC1's uniform treatment.

### Decision 9: Empty `run.text` early return in helper
**Decision:** `_render_paragraph_with_runs` skips runs where `run.text` is empty/whitespace-only BEFORE calling `str.find` — because `"".find("")` returns 0 and would wrap a zero-width span at position 0.
**Rationale (US AC9 + completeness validator finding 1):** Defensive guard for malformed runs metadata. Without this guard, an empty-text run would silently insert an empty `<a>` at the start of the paragraph.
**Alternatives considered:**
- *Allow empty-text runs to wrap position 0:* rejected — produces invalid Telegraph nodes.

### Decision 10: Bullet-doubling guard + DoS bound in `_build_content_from_blocks`
**Decision:** When rendering `list_item` blocks, BEFORE prepending `"• "`, strip any leading bullet/whitespace combinations from `block.text` (case: `block.text.lstrip(" ••\t")`). This guards against LLM emitting a bullet in translated output. Additionally: skip `_render_paragraph_with_runs` (fall through to plain text) when `len(block.text) > 100000` or `len(block.runs) > 100`; log WARNING.
**Rationale (security audit minor findings 5 + 3):** Defensive bounds for adversarial input. Real orangetrack content is far below these caps; thresholds are insurance only.
**Alternatives considered:**
- *No DoS bound:* rejected — security audit recommends bound for arbitrary RSS content.
- *Hard limits on individual run.text length:* rejected — orangetrack proper-noun runs can be long ("Mercedes-Benz 190E 2.5-16 EVO II"), constraining run.text would over-strip.

## Data Models

No schema changes. Block-shape extension (in-memory only):
- New value `"list_item"` for `block["type"]`. Same shape as paragraph: `{"type": "list_item", "text": str, "runs": list}`.
- `_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading", "list_item")` — extended.
- `paragraphs_flat` extraction: `[b["text"] for b in blocks if b["type"] in ("paragraph", "heading", "list_item")]`.

`level` field already exists on heading blocks — reused unchanged.

## Dependencies

### New packages
None.

### Using existing (from project)
- `urllib.parse.urlparse` — stdlib.
- `_runs_from_tag(tag) -> List[Dict]` (orangetrack_source.py) — extracts text + href runs; reused for `<li>` parsing.
- `_PATCHED_TEXT_BLOCK_TYPES` (`_llm_common.py`) — extended to include "list_item".
- `paragraphs_flat` extraction in `orangetrack_source.py` — extended to include "list_item".
- `_build_content_from_blocks` in `telegraph_publisher.py` — extended with helper invocation.

## Testing Strategy

**Feature size:** M

### Unit tests

**In `tests/test_orangetrack_source.py`** (new tests):

- `test_li_parsed_as_list_item_block` — `<ul><li>Ferrari Testarossa</li></ul>` → block list contains `{type: "list_item", text: "Ferrari Testarossa", runs: [...]}`.
- `test_ol_parsed_as_list_item_block` — same with `<ol>` parent.
- `test_li_with_inline_anchor` — `<li><a href="...">Mercedes</a></li>` → block.text contains "Mercedes", block.runs has the href.
- `test_li_with_strong` — `<li><strong>X</strong></li>` → block.text == "X".
- `test_empty_li_dropped` — `<li></li>` → no block emitted.
- `test_li_block_text_does_not_contain_bullet` — **negative assertion (AC2 litmus):** `<li>Ferrari</li>` → `block.text == "Ferrari"`, NOT "• Ferrari". The bullet must be added by publisher, not parser.
- `test_h2_h3_h4_parsed_as_heading_level_3` — for each of h2/h3/h4 → block has `type: "heading"`, `level: 3`.
- `test_h5_remains_paragraph` — `<h5>Section</h5>` → `type: "paragraph"` (regression for `babc67c`). NOTE: existing `test_h5_emitted_as_paragraph_block` (line ~144 of file) covers basic case; this new test pins behavior post-changes.
- `test_h1_h6_ignored` — `<h1>` and `<h6>` produce no blocks.
- `test_paragraphs_flat_includes_list_item` — `paragraphs_flat` returned includes list_item text in fetch order.

**In `tests/test_telegraph_publisher.py`** (new tests):

- `test_render_paragraph_with_same_site_link` — block with `text="A B C"`, runs `[{"text": "B", "href": "https://orangetrackdiecast.com/x"}]` → paragraph node has 3 children: "A ", `<a>` for "B", " C".
- `test_render_paragraph_with_external_link_dropped` — same setup but href to different domain → plain text only.
- `test_run_text_not_in_paragraph_text_dropped` — run.text doesn't match block.text → plain text only.
- `test_run_text_repeated_first_only_wrapped` — `block.text` contains run.text twice → only first occurrence wrapped.
- `test_overlapping_runs_first_wins_second_appears_plain` — two runs with overlapping spans → first wrapped as `<a>`; **second's text still appears in paragraph children as plain text** (not removed). Litmus assertion on exact children list.
- `test_two_runs_same_href_independent_wrapping` — two runs with identical href but different `run.text` → each wrapped at first occurrence of own text.
- `test_run_text_with_regex_meta_chars` — run.text contains `.` `?` `*` `(` `)` characters — `str.find` (not regex) used → exact match works.
- `test_run_with_empty_text_skipped` — `run.text == ""` → skipped (no wrapping at position 0). Decision 9 litmus.
- `test_run_with_none_href_skipped` — `run.href is None` → skipped, no crash.
- `test_run_with_malformed_href_skipped` — href `"not a url://!!"` → urlparse exception caught, run skipped, no crash.
- `test_no_runs_renders_plain_text` — `block.runs` is `[]` AND `None` (parameterized) → plain `<p>{text}</p>`, no helper-side processing.
- `test_list_item_renders_with_bullet_prefix` — block of type `list_item` → paragraph node first child is "• " literal.
- `test_list_item_with_same_site_link_combines` — list_item with runs → "• " prefix + link rendering composed correctly.
- `test_heading_block_renders_h3` — block of type `heading` level=3 → `<h3>` node.
- `test_heading_block_with_link_combines` — heading with runs → `<h3>` containing inline `<a>`.
- `test_www_prefix_normalized_for_same_site` — run.href `https://www.orangetrackdiecast.com/x`, source_url `https://orangetrackdiecast.com/y` → matched (also reverse direction).
- `test_lookalike_domain_not_matched_negative` — run.href to `https://wwwfake-orangetrackdiecast.com/x` → NOT matched (Decision 4 `removeprefix` precision).
- `test_case_sensitive_match_negative` — run.text `"Mercedes"`, block.text contains `"mercedes"` (lowercase) → NOT matched (AC6 case-sensitive).
- `test_mailto_scheme_dropped` — `run.href = "mailto:admin@orangetrackdiecast.com"` with `source_url = "https://orangetrackdiecast.com/article"` → dropped (Decision 4 scheme=http(s) requirement).
- `test_javascript_scheme_dropped` — `run.href = "javascript:alert(1)"` → dropped.
- `test_empty_netloc_dropped_when_source_also_empty` — degenerate case with `source_url=""` and `run.href="mailto:x"` → not matched (Decision 4 non-empty netloc requirement).
- `test_list_item_strips_leading_bullet_in_text` — block of type list_item with `text="• Ferrari"` (LLM emitted bullet) → final node has children `["• ", "Ferrari"]`, NOT `["• ", "• Ferrari"]` (Decision 10 bullet-doubling guard).
- `test_dos_bound_skips_helper_on_huge_text` — block.text length > 100000 chars → helper falls through to plain text, WARNING logged (Decision 10).
- `test_dos_bound_skips_helper_on_too_many_runs` — `len(runs) > 100` → helper falls through, WARNING logged.

### Integration tests

One new test `test_orangetrack_rendering_end_to_end` in `tests/test_orangetrack_source.py`:

- Build synthetic HTML: `<article><h3>Section</h3><p>The <a href="https://orangetrackdiecast.com/x">Mercedes</a> is fast.</p><ul><li>Ferrari</li><li>Porsche</li></ul></article>`.
- Parse via `_parse_content_encoded`.
- Manually patch RU translations into `block.text` (simulate LLM): "Раздел", "Mercedes — быстрая машина.", "Ferrari", "Porsche".
- Call `telegraph_publisher.preview_nodes(title, paragraphs, images, source_url='https://orangetrackdiecast.com/article', subtitle, blocks)`.
- **Assert exact node-tree dict structure** (NOT substring match):
  - One node `{"tag": "h3", "children": ["Раздел"]}` (heading).
  - One node `{"tag": "p", "children": ["Mercedes", " — быстрая машина."]}` where `"Mercedes"` is wrapped: actually `{"tag": "a", "attrs": {"href": "https://orangetrackdiecast.com/x"}, "children": ["Mercedes"]}` followed by string `" — быстрая машина."`.
  - Two nodes for list items: `{"tag": "p", "children": ["• ", "Ferrari"]}` and `{"tag": "p", "children": ["• ", "Porsche"]}`.
- Verify additional ordering: heading appears before paragraph appears before list items, matching parse order.

### E2E tests

None — pytest-only project. Integration test above with synthetic HTML covers the deepest layer feasible.

### Regression coverage (existing tests, MUST stay green)

- All existing tests in `tests/test_orangetrack_source.py` (h5 paragraph behavior, no `<li>` regressions, `_runs_from_tag` semantics).
- All existing tests in `tests/test_telegraph_publisher.py` (preview_nodes / `_build_content_from_blocks` for blocks without runs/list_item, heading rendering, video/image blocks).
- All existing tests in `tests/test_openrouter_transcreation.py` (which directly exercises `_patch_text_with_ru_paragraphs` — list_item flows through naturally because we extend the `_PATCHED_TEXT_BLOCK_TYPES` tuple, no new test file is needed) and `tests/test_claude_transcreation.py` (which exercises a different layer of the Claude flow but still hits `_PATCHED_TEXT_BLOCK_TYPES` via the same shared module).
- All 829+ tests on dev branch (after publish-idempotency-fix) must remain passing.

## Agent Verification Plan

### Verification approach

Per-task smoke checks: each implementation task has a Verify-smoke field below.

End-to-end pre-deploy check: `pytest tests/ -q` must pass with all 829 existing tests + new tests.

Post-deploy verification: visual scan of `@myhwchannel123` for 1-2 days after prod deploy. Operator looks for: bulleted lists in case-report articles, distinct heading typography for section headers (h2/h3/h4), clickable orangetrack links inside paragraphs.

### Tools required

`pytest` (local + CI), `bash`/`ssh` (operator-side post-deploy), `journalctl` on the VPS (post-deploy log inspection). No MCP tools — visual user check is the final gate.

## Risks

| Risk | Mitigation |
|------|-----------|
| LLM might wrap RU model name in quotes («Mercedes-Benz»), breaking exact substring match | Substring search uses `str.find` on raw `block.text`; if RU has quotes, EN run.text without quotes will not match → plain text. AC8 says silently drop. Future: sentinel-token (Phase-2). |
| h2/h3/h4 → heading type changes visual weight; certain h2-heavy articles may look top-heavy | Decision 2 carve-out for h5 prevents the worst case (in-paragraph markers). For h2-heavy articles, AC3 normalizes to level=3 (single weight, not multi-level). |
| Existing `test_h5_emitted_as_paragraph_block` (line ~144) breaks if h5 carve-out has a typo | Targeted regression test `test_h5_remains_paragraph` covers the carve-out explicitly. |
| `_patch_text_with_ru_paragraphs` count mismatch if `paragraphs_flat` extension is forgotten | Test `test_paragraphs_flat_includes_list_item` catches the mismatch. |
| Two runs in same paragraph with overlapping run.text spans → Telegraph rejects nested `<a>` | Decision 5 — first-wrap-wins; later overlapping runs render as plain text within their position. |
| `<li>` with images/iframes inside (uncommon) | `_runs_from_tag` extracts text only; nested media inside `<li>` would not be emitted by the new `<li>` branch. Acceptable for MVP — orangetrack lists are text. |
| `<ol>` numbering expected by some readers but not provided | Decision 8 — uniform bullet treatment; AC1 explicitly says "ul или ol". |
| **Display-text vs href divergence** — substring approach wraps arbitrary RU text in `<a>` whose href can be any orangetrack URL. If Brad's article body itself contains a malicious orangetrack-self-link (e.g. `<a href="orangetrack/phish">Mercedes</a>`), bot republishes it as a "trusted" same-site link. | Trust boundary: orangetrack source content is treated as trusted (same as the article body itself). The link's href is ALREADY in `<a href>` of the original HTML — bot only preserves it. Mitigation against compromise of orangetrack: monitored externally (admin notices anomalous links in channel during visual scan); Decision 4's strict scheme + same-site check ensures no off-domain redirect introduced by our code. |
| **Bullet-doubling** if LLM emits leading "• " or "•" in translated list_item text. | Defensive: in `_build_content_from_blocks` list_item branch, strip leading bullet/whitespace from translated text before prepending `"• "`. Documented as Decision-10 implementation detail. |
| **Quadratic-substring DoS** on adversarial input (e.g., 1000 same-site runs × 1MB block.text). | Bounded by content shape: orangetrack RSS articles cap at ~50KB, runs at ~30 per block in practice. Defensive cap: skip helper if `len(block.text) > 100000` or `len(runs) > 100`; log WARNING. |

## User-Spec Deviations

None.

All tech-spec decisions trace to user-spec ACs:
- Decision 1 → AC1, AC2 (list_item type + bullet in publisher after LLM)
- Decision 2 → AC3, AC4 (h2/h3/h4 → heading, h5 → paragraph)
- Decision 3 → AC3, AC5 (single visual treatment; h1/h6 ignored)
- Decision 4 → AC6, AC7, AC9 (same-site netloc check with safe www-prefix; malformed URL safety)
- Decision 5 → AC9 (overlapping runs handling — adequacy round-1 minor)
- Decision 6 → User-spec Risk 4 (substring vs sentinel)
- Decision 7 → [TECHNICAL] (consistent helper application)
- Decision 8 → AC1 (ul/ol uniform)
- Decision 9 → AC9 (empty run.text guard — completeness validator finding)
- Decision 10 → AC9 (bullet-doubling guard + DoS bound — security audit findings)

## Acceptance Criteria

Technical criteria complementing user-spec AC1–AC11:

- [ ] `pytest tests/ -q` passes with 0 failures and 0 errors.
- [ ] `git diff` shows changes ONLY in: `orangetrack_source.py`, `_llm_common.py`, `telegraph_publisher.py`, `tests/test_orangetrack_source.py`, `tests/test_telegraph_publisher.py`.
- [ ] `python3 -m py_compile orangetrack_source.py _llm_common.py telegraph_publisher.py` succeeds.
- [ ] Manual review: `_render_paragraph_with_runs` correctness — segments don't overlap, plain-text segments preserve original characters, `<a>` nodes contain expected href.
- [ ] Manual review: `_emit_heading` dispatch — h2/h3/h4 → heading level=3, h5 → paragraph.
- [ ] Manual review: list_item bullet prefix added at publisher level only — NEGATIVE check that parser does NOT prepend bullet (test `test_li_block_text_does_not_contain_bullet`).
- [ ] No regression in any existing test in the modified test files.
- [ ] CI on `dev` branch: green pytest → `deploy_test.yml` triggers → SCP to test → news_bot_test.service restarts cleanly.

## Implementation Tasks

### Wave 1 (parallel — 3 source files, no conflicts)

#### Task 1: Parser changes — `<li>`/`<ol>` parsing + heading dispatch + `paragraphs_flat` extension
- **Description:** Modify `orangetrack_source.py` for both Bug 1 and Bug 2: in `_walk`, handle `<li>` (children of `<ul>`/`<ol>`) by emitting `{type: "list_item", text, runs}` blocks via existing `_runs_from_tag`; modify `_emit_heading` and its dispatch in `_walk` so h2/h3/h4 → `type: "heading", level: 3` and h5 → `type: "paragraph"` (preserves `babc67c`); extend `paragraphs_flat` extraction to include `"list_item"`. Implements user-spec AC1, AC3, AC4, AC5; Decisions 1, 2, 3, 8.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m py_compile orangetrack_source.py` succeeds.
- **Files to modify:** `orangetrack_source.py`
- **Files to read:** `work/orangetrack-rendering-fixes/tech-spec.md` (Decisions 1, 2, 3, 8); `work/SESSION-2026-05-06.md` (Break 3 — h5 typography rationale)

#### Task 2: LLM-patch — extend `_PATCHED_TEXT_BLOCK_TYPES`
- **Description:** Add `"list_item"` to `_llm_common._PATCHED_TEXT_BLOCK_TYPES` tuple. Single-line change ensures `_patch_text_with_ru_paragraphs` updates list_item text when LLM returns `blocks=null`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "from _llm_common import _PATCHED_TEXT_BLOCK_TYPES; assert 'list_item' in _PATCHED_TEXT_BLOCK_TYPES"` exits 0.
- **Files to modify:** `_llm_common.py`
- **Files to read:** `work/orangetrack-rendering-fixes/tech-spec.md` (Decision 1)

#### Task 3: Publisher — link helper + bullet rendering + heading helper invocation
- **Description:** Add `_render_paragraph_with_runs(text, runs, source_url)` and `_is_same_site(href, source_netloc)` helpers in `telegraph_publisher.py`. Modify the block-rendering loop inside `_build_content_from_blocks` (NOT `preview_nodes` — it's a dispatcher): paragraph blocks call helper; heading blocks call helper; list_item blocks strip leading bullet/whitespace from text then render as `<p>` with first child `"• "` followed by helper output. Apply DoS bounds (skip helper on text > 100KB or runs > 100, log WARNING). Implements user-spec AC2, AC6, AC7, AC8, AC9; Decisions 1, 4, 5, 7, 9, 10.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m py_compile telegraph_publisher.py` succeeds.
- **Files to modify:** `telegraph_publisher.py`
- **Files to read:** `work/orangetrack-rendering-fixes/tech-spec.md` (Decisions 4, 5, 7, 9, 10)

### Wave 2 (parallel — 2 test files, no conflicts; depends on Wave 1)

#### Task 4: Unit + integration tests for parser + integration end-to-end
- **Description:** Add unit tests in `tests/test_orangetrack_source.py` covering `<li>` parsing in `<ul>` and `<ol>`, empty `<li>`, `<li>` with inline `<a>`/`<strong>`, h2/h3/h4 heading-type emission with level=3, h5 paragraph-type regression, h1/h6 ignored, `paragraphs_flat` includes list_item, **negative AC2 check that parser does not prepend bullet**, and the integration test `test_orangetrack_rendering_end_to_end` with exact node-tree dict assertions.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_orangetrack_source.py -q` passes (existing + new).
- **Files to modify:** `tests/test_orangetrack_source.py`
- **Files to read:** `orangetrack_source.py` (post-Tasks 1, 2); `telegraph_publisher.py` (post-Task 3); `work/orangetrack-rendering-fixes/tech-spec.md` (Testing Strategy)

#### Task 5: Unit tests for telegraph_publisher helper
- **Description:** Add unit tests in `tests/test_telegraph_publisher.py` covering `_render_paragraph_with_runs` and `_build_content_from_blocks` rendering: same-site link rendered as `<a>`, external link dropped, run.text not in block.text dropped, repeated run.text first-only wrap, overlapping runs first-wins (with second appearing as plain text), two runs same href, regex meta-chars in run.text, empty/none run.text/run.href, malformed href, no-runs / `runs=[]` / `runs=None`, list_item bullet prefix, list_item with same-site link combined, heading-type renders `<h3>`, heading with link combined, www-prefix normalized (both directions), lookalike domain (`wwwfake-`) NOT matched, case-sensitive match negative, mailto/javascript scheme dropped, empty-netloc not matched, bullet-doubling guard, DoS bounds (huge text + many runs).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_telegraph_publisher.py -q` passes (existing + new).
- **Files to modify:** `tests/test_telegraph_publisher.py`
- **Files to read:** `telegraph_publisher.py` (post-Task 3); `work/orangetrack-rendering-fixes/tech-spec.md` (Testing Strategy)

### Wave 3 — Audit (parallel)

#### Task 6: Code Audit
- **Description:** Full-feature code quality audit. Read `orangetrack_source.py`, `_llm_common.py`, `telegraph_publisher.py`. Verify holistic quality: helper boundaries clean, no duplicated logic, consistency with existing code style, Decision 1-9 traceability, Decision 7 (helper applied uniformly to paragraph/heading/list_item), comment quality.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit
- **Description:** Full-feature security audit (OWASP Top 10) on the new helpers and dispatch logic. Specifically: `_render_paragraph_with_runs` builds Telegraph node-tree from RSS-fetched text — confirm no XSS/HTML-injection vector; `urlparse` exception handling correct; `removeprefix("www.")` (NOT `lstrip`) prevents lookalike-domain bypass; href normalization doesn't enable open redirect.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit
- **Description:** Full-feature test quality audit. Read all new tests across 2 test files. Verify: assertions are litmus-grade (exact dict comparisons in integration test, NOT substring), edge cases comprehensively covered (h5 regression, empty/malformed runs, overlapping runs with second as plain text, regex meta-chars, lookalike domains, case sensitivity), AC2 negative assertion present.
- **Skill:** test-master
- **Reviewers:** none

### Wave 4 — Pre-deploy QA

#### Task 9: Pre-deploy QA
- **Description:** Run full test suite locally with `pytest tests/ -q`; confirm 0 failures, 0 errors. Verify each user-spec AC1–AC11 has a corresponding passing test or executable check. Run `python3 -m py_compile` on all modified source files. Combined regression check with publish-idempotency-fix already on dev (829 baseline + new tests).
- **Skill:** pre-deploy-qa
- **Reviewers:** none

### Wave 5 — Deploy

#### Task 10: Deploy
- **Description:** Operator pushes orangetrack-rendering-fixes commits (stacked on top of publish-idempotency-fix already on dev) → CI ci.yml → deploy_test.yml SCPs to `/home/hwbot/bot_test/` + restart `news_bot_test.service`. Operator observes test channel ~30 min on next 10:00 МСК cron tick (or restart service for immediate `job()`). Then `git checkout main && git merge dev && git push origin main` → deploy.yml → prod. Operator's one-time SQL `DELETE FROM failed_articles` (for publish-idempotency-fix's team-transport-k row) is part of that other feature's deploy task — separate concern.
- **Skill:** deploy-pipeline
- **Reviewers:** none
- **Verify-smoke:** GitHub Actions ci.yml + deploy_test.yml + (after merge) deploy.yml all green; `ssh hwbot@... "systemctl status news_bot_test.service"` shows active running cleanly post-restart.
- **Verify-user:** operator visually confirms test channel `@myhwchannel123` shows new orangetrack post with bullets, h3 headings, clickable same-site links before merging dev→main.

### Wave 6 — Post-deploy verification

#### Task 11: Post-deploy verification
- **Description:** Live environment verification on prod after deploy:
  - **journalctl check:** `ssh hwbot@148.135.207.54 "journalctl -u news_bot.service --since '<deploy timestamp>' --no-pager | grep -E 'UNIQUE|IntegrityError|idempotency-guard|TypeError|AttributeError'"` → no anomalies.
  - **Visual `@myhwchannel123` scan over next 1-2 days** for fresh orangetrack articles — confirm bulleted lists in case-report posts, distinct h3 typography for section headers, clickable orangetrack links inside paragraphs.
  - **Anomaly handling:** if anything renders unexpectedly — operator opens diagnostic task with concrete URL.

  Tools: `bash`, `ssh`, `journalctl`. Visual user inspection.
- **Skill:** post-deploy-qa
- **Reviewers:** none
