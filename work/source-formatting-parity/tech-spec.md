---
created: 2026-07-30
status: draft
branch: dev
size: L
---

# Tech Spec: source-formatting-parity

## Solution

Extract the inline-markup machinery that today lives inside `orangetrack_source.py`
into a new shared module, migrate orangetrack onto it with **byte-identical output**,
then drive the same module from `t_hunted_source`, `lamley_source` and
`autoevolution_source` so all four emit `blocks` with `runs`.

Research (`code-research.md` Part II, measured on **14 real articles**) establishes the
seam precisely: `_runs_from_tag` needs only 5 module symbols and exactly **one** real
policy hook (`_has_color_class`); `_walk`'s body contains **zero site-specific
strings** — all site knowledge arrives through 6 free names, of which only three are
genuinely per-site (chrome-class predicate, image-src picker, video-embed wrapper).
Everything else (h2/h3/h4 → level 3, h5 → paragraph, h1/h6 dropped, `<br>`-splitting,
bullet-less `<li>`) is *good* policy already pinned by orangetrack's tests and becomes
the shared default.

Three cross-cutting defects must land in the same release, because each one only
becomes reachable once a parser starts emitting blocks:

1. `_build_content_from_blocks` has **no `images` parameter and no cap** — proven at
   `telegraph_publisher.py:358-363`. All four sampled lamley articles exceed their
   10-image cap (14, 41, 48, 50 images).
2. The subtitle lift desynchronises `paragraphs` from `blocks` by exactly 1 — **13 of
   14 real articles** would hit this, publishing the last paragraph in English.
3. No kill switch exists for this feature, and deploys are barred 10:00–20:00 МСК.

## Architecture

### What we're building/modifying

- **`dom_blocks.py` (new)** — the shared mechanism: inline-markup walker
  (`runs_from_tag`), run flattening (`text_from_runs`), the bold-paragraph heading
  heuristic (`looks_like_heading`), and a `BlockBuilder` that owns the block
  accumulator + image dedup and exposes the four `emit_*` operations. Per-site
  behaviour arrives as three injected callables; everything else defaults to
  orangetrack's current, test-pinned policy.
- **`orangetrack_source.py`** — becomes the first consumer. Its published output must
  not change; its existing tests must pass **without being edited**.
- **`t_hunted_source.py`, `lamley_source.py`** — gain a block-building pass alongside
  the flat list, plus the aligned subtitle lift.
- **`autoevolution_source.py`** — its weaker `_runs_from_tag` (href-only; bold and
  italic are flattened, by its own docstring's admission) is replaced by the shared one.
- **`telegraph_publisher.py`** — image cap applied on the blocks path.
- **`news_bot.py`** — runtime `blocks`/`paragraphs` count guard.
- **The four engine modules** — add `list_item` to the patchable-block-type tuples.

### How it works

```
parser  ──►  BlockBuilder (shared)  ──►  blocks[] + runs[]
   │                                          │
   │  flat paragraphs[] derived from          │
   │  paragraph|heading|list_item text        │
   ▼                                          ▼
filter_blocks / filter_boilerplate      insert_pending (blocks column, exists)
   │                                          │
   ▼                                          ▼
aligned subtitle lift (both lists)      LLM: **bold** markers ──► runs
   │                                          │
   ▼                                          ▼
count guard: len mismatch ⇒ drop blocks, WARNING
   │
   ▼
telegraph_publisher._build_content_from_blocks (image cap applied)
```

Flag off ⇒ parsers emit no `blocks`; publication falls back to `_build_content` and
reads as flat text, as today.

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| `dom_blocks` module constants (compiled regex, tag maps) | `dom_blocks.py` at import | all four parsers | 1 (module singleton) |
| `BlockBuilder` instance | each parser, per article | that parser's walk only | 1 per article (NOT shared — it owns mutable accumulator state) |

`BlockBuilder` holding mutable state is exactly why it is per-article and never a
module singleton: `blocks` and `seen_image_bases` are captured by all four emitters
(`orangetrack_source.py:553-554`), so a shared instance would leak blocks between
articles.

## Decisions

### Decision 1: Share the walker via injected policy, not by copy
**Decision:** New `dom_blocks.py` holds the walker body and leaf helpers. Three
callables are injected per site: chrome-class predicate, image-src picker,
video-embed wrapper. The heading-level policy, `<br>`-splitting and bullet-less `<li>`
become shared defaults.
**Rationale:** Serves user-spec Risk 1 («перенос кода вместе с его багами») — the two
functions that carried all three 2026-07-28 defects end up in ONE place, so the next
such bug is fixed once, not four times. Research proves the walker body is
site-agnostic; only those three names carry site knowledge.
**Alternatives considered:** copy-paste into three parsers (rejected — exactly what
Risk 1 forbids); share only the leaf helpers and let each parser keep its own walker
(rejected — triplicates the dispatch table and the `<br>`/`<li>` policy, which is
where the subtle behaviour lives); import from `orangetrack_source` directly
(rejected — `_walk` is a closure over mutable state, not importable, and it would make
one source a library for the others).

### Decision 2: Heading heuristic = whole-bold AND not sentence-terminated
**Decision:** A paragraph becomes a heading when its text is **entirely** covered by
bold runs AND it does not end in `.` (`…` and `….` are heading-compatible; `?` and `!`
already were). **No length limit.**
**Rationale:** Serves user-spec AC2, with an operator-approved change. Measured on 286
paragraph blocks: 24 promotions, all genuine; **zero whole-bold non-headings exist in
the data**, so length was doing no discriminating work — it only clipped genuine
headings (one lamley subheading at 85 chars). Real `<h2>/<h3>/<h4>` tags still produce
heading blocks directly; the heuristic is the fallback, and for t-hunted it is the
ONLY path (0 real heading tags across 10 articles).
**Alternatives considered:** keep ≤80 (rejected by operator — clips real headings for
no measured benefit); ≤100 (rejected — same arbitrary cliff, one article further out);
treat any trailing punctuation as disqualifying (rejected — the motivating t-hunted
subheading is a question).
**Residual risk:** a blog that bolds an entire lead paragraph gets an oversized
heading. Unobserved in 14 articles; covered by the kill switch and the operator's
post-deploy check. See Risks.

### Decision 2b: The heading heuristic is OPT-IN per source, default OFF
**Decision:** `looks_like_heading` is applied only when the calling parser asks for
it. Enabled for t-hunted, lamley and autoevolution; **disabled for orangetrack.**
**Rationale:** Serves user-spec AC10 (byte-identity) and closes the gap the
completeness validator flagged as critical. orangetrack has **no** bold→heading
behaviour today, and it was never part of the 286-paragraph measurement — so applying
the heuristic to it by default would change its published output and break the one
hard gate this feature has. Default-OFF also matches the project's fail-safe habit: a
new source added later gets the conservative behaviour until someone measures it.
**Alternatives considered:** apply everywhere (rejected — silently violates AC10, and
the measurement does not cover orangetrack); measure orangetrack and enable it there
too (rejected for this feature — it is out of the user-spec's scope, which names three
sources; can be a follow-up once someone samples orangetrack's whole-bold paragraphs).

### Decision 3: Lift the subtitle from both lists in one operation
**Decision:** t-hunted (`:216-220`) and lamley (`:400-401`) lift `paragraphs[0]` **and**
the first patchable block together, inside the same guard.
**Rationale:** Serves user-spec AC8 and Ограничения «Подъём лида» (AC10 is Decision
1's — orangetrack hardcodes `subtitle = ""`, so the lift does not touch it).
`_llm_common` pairs the two lists
**positionally** on encode and consumes them sequentially on decode; a 1-off
desynchronisation publishes the tail paragraph in English (incident 2026-05-06).
Research: **13 of 14 real articles** break the invariant without this.
**Alternatives considered:** hardcode `subtitle = ""` as orangetrack does (`:839`) —
rejected by operator, the decorated lead is a deliberate channel element; patch the
pairing inside `_build_user_message:223` (rejected — research proves the engines read
`blocks_in` independently, so the fix cannot live there).

### Decision 3b: Degradation is silent and unconditional
**Decision:** Every failure mode in this feature degrades to flat text **without**
pinging the operator: unparsable markup (AC5), markers the LLM dropped on one
paragraph (AC6), and a count mismatch (AC8 — logged WARNING, still no ping). No
threshold on how much of an article is bold (AC7). Bold the LLM added where the source
had none is honoured, not stripped (AC4) — already true in production since the
2026-07-28 `_decode_bold_markers` fix, and this feature must not regress it.
**Rationale:** Serves user-spec AC4, AC5, AC6, AC7. Formatting is cosmetic: an
operator ping for a lost bold span would be noise, and the operator said so
explicitly. The asymmetry against AC8 is deliberate — a count mismatch is logged
because it indicates a code defect, not a source quirk.
**Alternatives considered:** ping on markup failure (rejected — noise); cap
bold-heavy articles (rejected by operator, AC7); re-strip LLM-added bold (rejected by
operator).

### Decision 4: Runtime count guard drops `blocks`, never the article
**Decision:** Before the row is staged, compare patchable-block count with paragraph
count; on mismatch drop `blocks`, log WARNING, publish as flat text.
**Rationale:** Serves user-spec AC8. Both sides currently swallow the mismatch
silently (`except StopIteration` with no log), so a dev-time test cannot cover an
article nobody wrote a fixture for. Fail-open matches the promo filter, content gate
and dedup gate.
**Alternatives considered:** rely on the parity test alone (rejected — see above);
raise and skip the article (rejected — violates fail-open, loses content).

### Decision 5: Image cap enforced inside the blocks renderer
**Decision:** `_build_content_from_blocks` takes a cap and stops emitting image blocks
past it, keeping the hero.
**Rationale:** Serves user-spec AC9. Proven that non-empty `blocks` makes the
publisher ignore `images` entirely, so `IMAGE_LIMIT` becomes dead code — lamley pages
would jump from 10 to up to 50 images.
**Alternatives considered:** cap in each parser before returning blocks (rejected —
three places to forget, and orangetrack already caps in its post-pass, so the renderer
is the single choke point every source passes through).

### Decision 6: Kill switch read from a parser-importable location
**Decision:** One env flag, default ON, parsed with the same truthy/falsy word set as
the existing two. It lives where the parsers can read it — **not** in `news_bot`.
**Rationale:** Serves user-spec AC11. Research found the structural blocker: the three
parsers cannot import `news_bot` (circular). Flag off ⇒ parsers skip block emission
entirely.
**Alternatives considered:** gate only downstream in `fetch_full_article` (rejected —
strips `blocks` but the parser has already changed the flat text, so "off" would not
mean "as before"); reuse `DEDUP_SERIES_ENABLED` (rejected — unrelated blast radius).
**Scope of the guarantee (operator-approved):** flag-off means *no formatting, flat
text*, **not** byte-identical to today — measured drift is **−0.10 %** (t-hunted) and
**−1.18 %** (lamley, and that delta is WordPress chrome removal, i.e. an improvement).
Byte-identity would require keeping two text-derivation paths alive in parallel.

### Decision 7: `list_item` added to the engines' patchable types
**Decision:** Add `list_item` to the per-engine tuples (claude `:159`, gemini `:132`,
openai `:129`, openrouter `:217`) so list items are patched from the main response.
**Rationale:** Serves user-spec AC3. Confirmed omitted in all four while the shared
tuple at `_llm_common.py:115` includes it; the omission sends already-translated
Russian list items to the caption pass for re-translation.
**Alternatives considered:** leave as-is (rejected — AC3 explicitly forbids
re-translation).

## Data Models

No schema change. `blocks` already exists on `pending_articles`, is in
`_PENDING_JSON_COLS`, is NULL-preserving in `insert_pending`, and `runs` round-trips
inside that JSON untouched.

Block shapes are unchanged from the contract
`telegraph_publisher._build_content_from_blocks` already documents:

```
{'type': 'paragraph',  'text': str, 'runs': [ {...} ]}
{'type': 'heading',    'text': str, 'level': 3|4, 'runs': [...]}
{'type': 'list_item',  'text': str, 'runs': [...]}
{'type': 'lead',       'text': str}
{'type': 'image',      'src': str, 'caption': str}
{'type': 'video',      'src': str}
```

Run shape: `{'text': str, 'formats': ['bold'|'italic'|'underline'|'strikethrough'], 'href': str?}`.

## Dependencies

### New packages
None.

### Using existing (from project)
- `telegraph_publisher._render_paragraph_with_runs` — already renders all four inline
  formats; no change needed beyond the image cap.
- `boilerplate_filter.filter_blocks` / `filter_boilerplate` — the two-list filtering
  the parsers already call. Note: an earlier reading of the research called these two
  passes a second desynchronisation source; Part II §3.2 **refutes** that and names the
  real second source — the **parser-local title-dedup filter**. Task 5/6 must reconcile
  that filter across both lists, not just the boilerplate passes.
- `_llm_common._encode_format_markers` / `_decode_format_markers` — the `**bold**`
  round-trip across translation.
- `pending_articles_repo.insert_pending` — persists `blocks` unchanged.

## Testing Strategy

**Feature size:** L

### Unit tests
- `dom_blocks`: bold inside a paragraph; whole-bold paragraph → heading; whole-bold
  ending in `.` → NOT heading; ending in `…` / `….` / `?` / `!` → heading; nested
  bold-in-italic; overlapping runs (first-wins); empty/whitespace runs skipped;
  flattening produces no doubled spaces and every run stays locatable via `text.find`.
- Heading-level policy defaults: h2/h3/h4 → level 3, h5 → paragraph, h1/h6 dropped.
- `<br>`-splitting and bullet-less `<li>` preserved as shared defaults.
- Per parser (t-hunted, lamley, autoevolution): bold survives into blocks; malformed
  markup degrades to flat text; aligned lift keeps the two lists equal in length,
  including t-hunted's single-paragraph gallery case where the guard does not fire.
- Image cap: blocks path emits no more than the cap, hero preserved.
- Kill switch: flag off ⇒ no `blocks` key emitted by any of the three parsers.

### Integration tests
- Bold survives parser → `insert_pending` (JSON in SQLite) → translation →
  `telegraph_publisher` nodes, for each of the three sources.
- Count mismatch ⇒ `blocks` dropped, article published, WARNING logged.
- Image cap holds end-to-end on a gallery-heavy lamley fixture (the measured 50-image
  case).
- An article near the `_is_text_only_checklist` 500-char floor is not newly dropped
  (measured delta is 0.1–1.2 %, but the boundary must be pinned).
- orangetrack regression: its existing tests pass **unedited**.

### E2E tests
None. E2E would require live fetches; autoevolution article pages return **403** to our
client (its RSS returns 200) and lamley is throttled 20 s + 24 h blacklist. The
operator's side-by-side comparison after deploy is the E2E substitute.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach
Automated tests cover mechanism. Beyond them the agent renders a real fixture through
`preview_renderer.render_html` and asserts the presence of `<h3>`, `<strong>` and list
items — the preview allowlist was fixed on 2026-07-28 specifically so this check is
honest. The agent also re-runs the measurement script from `code-research.md` Part II
against live t-hunted articles (the only source reachable without a 403) to confirm the
heading heuristic still scores as measured.

### Tools required
`bash`, `python3`, `pytest`, `curl_cffi` (already a project dependency, used by the
research script). No MCP tools required pre-deploy. Post-deploy uses `ssh` +
`docker logs` (operator-run) and manual reading of the channel.

## Risks

| Risk | Mitigation |
|------|-----------|
| orangetrack output drifts during extraction | Its existing tests must pass **unedited** — that is the acceptance gate for Wave 1, not a nice-to-have. The exact-`runs` equality assertion in `tests/test_autoevolution_source.py:180-184` is the analogous tripwire for Task 7. |
| Whole-bold long paragraph becomes an oversized heading (Decision 2 residual) | Unobserved in 286 real paragraphs. Kill switch + operator's post-deploy read. Reversible: cosmetic, no content lost. |
| Blocks/paragraphs desynchronise in a shape no fixture covers | Runtime guard (Decision 4) drops `blocks` and logs; the article still publishes. |
| Image count explodes on gallery posts | Decision 5 caps inside the renderer, the single choke point. Pinned by an integration test on the measured 50-image lamley article. |
| Flat text shifts which paragraphs the promo filter scans | `_is_promo_article` reads the first 8 paragraphs; lamley loses 4–5 chrome entries, which on the four sampled articles were trailing (so the first 8 are unchanged) — **not guaranteed in general**. Test pins a case where chrome sits at the head. |
| `test_deploy_files_invariant.py` is blind to new modules | It greps only for literal existing filenames. `Dockerfile` does `COPY . .` so Docker prod ships `dom_blocks.py` automatically, but `deploy.sh`'s explicit FILES array does not — Task 1 adds it to both. |
| Second LLM call cost grows on three more sources | Decision 7 stops list-item re-translation. Cost measured on the first publications after deploy; the operator's standing rule is quality over token cost, so this is monitored, not pre-emptively optimised. |

## User-Spec Deviations

- **AC2 (heading heuristic):** user-spec fixes the length limit at **≤80 chars** with
  measured justification. Tech-spec **removes the length condition entirely** —
  heading = whole-bold AND not ending in `.`. Reason: measurement across 286 real
  paragraph blocks found **zero** whole-bold non-headings, so length discriminated
  nothing and only clipped genuine headings (a lamley subheading at 85 chars). The
  user-spec's own t-hunted evidence reproduced exactly; it simply does not generalise
  to lamley. It also supersedes the user-spec's Тестирование line and «Как проверить»
  step 3, which both pin the 80/81 boundary — that test is intentionally dropped, not
  forgotten, and is replaced by negative controls (whole-bold LONG paragraph → still a
  heading; whole-bold ending in `.` → not a heading). → **APPROVED by operator
  2026-07-30**
- **AC2 (trailing punctuation):** user-spec says «не заканчивается точкой». Tech-spec
  additionally treats `…` and `….` as heading-compatible, to keep lamley's «Also ran….»
  subheading. → **APPROVED by operator 2026-07-30**
- **AC11 (kill switch):** user-spec says the flag returns «старое поведение (плоский
  текст)». Tech-spec states explicitly that this means *no formatting, flat text* and
  **not** byte-identical output — measured drift −0.10 % (t-hunted) / −1.18 % (lamley,
  chrome removal). Byte-identity would require two parallel text-derivation paths. →
  **APPROVED by operator 2026-07-30**
- **Risk 2 (flat-text shrinkage) — downgraded.** User-spec treats this as a significant
  risk on the premise that recursive `find_all` double-counts nested
  `<li><p>` / `<blockquote><p>`. Measurement found **zero** such nesting in 14 real
  articles and 15 fixtures; real delta is −0.10 % / −1.18 %, nowhere near the 500-char
  checklist floor. The risk stays in the table at its true (low) weight with a boundary
  test, rather than being deleted. → informational, no scope change
- **Added: `dom_blocks.py` as a new module** (user-spec says «переиспользовать
  обходчик», which reads as reuse-in-place). Reason: `_walk` is a closure over mutable
  state and cannot be imported; the alternative is copy-paste, which user-spec Risk 1
  forbids. → **implied by user-spec Risk 1; flagged for the record**

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] `orangetrack_source` tests pass **without any test file being edited**
- [ ] No new package dependencies
- [ ] No DB migration (the `blocks` column already exists)
- [ ] `dom_blocks.py` present in `deploy.sh` FILES array AND in
      `tests/test_deploy_files_invariant.py`
- [ ] For every source emitting blocks: patchable-block count == paragraph count, or
      `blocks` is dropped with a WARNING
- [ ] Image blocks per page ≤ the source's existing cap
- [ ] Flag off ⇒ no `blocks` emitted by any of the three parsers
- [ ] All four engines list `list_item` as a patchable block type
- [ ] Preview renders `<h3>` / `<strong>` / list items produced by this feature
- [ ] The heading heuristic is OFF for orangetrack and ON for the other three
- [ ] Full suite green with no regressions (baseline: 1628 passed, 441 subtests)

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: Extract `dom_blocks.py` and migrate orangetrack onto it
- **Description:** Create the shared module — inline-markup walker, run flattening, the opt-in heading heuristic, and a `BlockBuilder` with per-site policy hooks — and make `orangetrack_source` its first consumer. Its published output must not change. Register the new module in `deploy.sh` FILES and in the deploy-files invariant test.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_orangetrack_source.py -q` passes AND `git diff --stat tests/test_orangetrack_source.py` is empty
- **Files to modify:** `dom_blocks.py` (new), `orangetrack_source.py`, `deploy.sh`, `tests/test_deploy_files_invariant.py`, `tests/test_dom_blocks.py` (new)
- **Files to read:** `work/source-formatting-parity/code-research.md`, `telegraph_publisher.py`, `boilerplate_filter.py`

#### Task 2: Rendering surface — image cap + preview parity
- **Description:** Apply the source's image cap inside `_build_content_from_blocks`, keeping the hero figure (AC9; Decision 5 explains why the renderer is the chosen place). In the same task, own AC12: prove the preview renders the headings, bold and list items this feature produces, and add whatever is still missing. Both halves are "what the reader and the operator actually see", and they touch different files.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_telegraph_publisher.py tests/test_preview_renderer.py -q`, then render a blocks fixture through `preview_renderer.render_html` and grep for `<h3>`, `<strong>` and a list item
- **Files to modify:** `telegraph_publisher.py`, `preview_renderer.py`, `tests/test_telegraph_publisher.py`, `tests/test_preview_renderer.py`
- **Files to read:** `orangetrack_source.py`, `lamley_source.py`, `t_hunted_source.py`

#### Task 3: Feature flag module
- **Description:** Add `feature_flags.py` exposing this feature's kill switch, readable by the parsers. Serves AC11; see Decision 6 for placement and default.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "import feature_flags; print(feature_flags.source_formatting_enabled())"` → `True` with no env set
- **Files to modify:** `feature_flags.py` (new), `.env.example`, `tests/test_feature_flags.py` (new), `deploy.sh`, `tests/test_deploy_files_invariant.py`
- **Files to read:** `news_bot.py`, `.claude/skills/project-knowledge/references/deployment.md`

#### Task 4: `list_item` as a patchable block type in all four engines
- **Description:** Align the four per-engine patchable-type tuples with the shared one so list items are patched from the main response. Serves AC3; see Decision 7.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_claude_transcreation.py -q`
- **Files to modify:** `claude_transcreation.py`, `gemini_transcreation.py`, `openai_transcreation.py`, `openrouter_transcreation.py`, `tests/test_claude_transcreation.py`
- **Files to read:** `_llm_common.py`

### Wave 2 (зависит от Wave 1)

#### Task 5: t-hunted emits blocks
- **Description:** Drive `BlockBuilder` over the Blogger body so t-hunted returns `blocks` with `runs` alongside the flat list, with the subtitle lift and the title-dedup filter applied to both lists consistently. The bold-paragraph heuristic is enabled here and is t-hunted's only heading path.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** parse a saved t-hunted fixture and assert `len(patchable blocks) == len(paragraphs)`
- **Files to modify:** `t_hunted_source.py`, `tests/test_t_hunted_source.py`
- **Files to read:** `work/source-formatting-parity/code-research.md`, `dom_blocks.py`, `orangetrack_source.py`, `boilerplate_filter.py`

#### Task 6: lamley emits blocks
- **Description:** Same for lamley, which needs both heading paths (real tags and the heuristic) and whose lift is unconditional. WordPress chrome is removed via the shared chrome predicate.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** parse a saved lamley fixture and assert `len(patchable blocks) == len(paragraphs)`
- **Files to modify:** `lamley_source.py`, `tests/test_lamley_source.py`
- **Files to read:** `work/source-formatting-parity/code-research.md`, `dom_blocks.py`, `orangetrack_source.py`

#### Task 7: autoevolution uses the shared runs walker
- **Description:** Replace autoevolution's href-only inline walker with the shared one so its bold and italic survive. Its existing block/lead/heading structure must keep working.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_autoevolution_source.py -q` — the exact-`runs` equality assertion is the tripwire
- **Files to modify:** `autoevolution_source.py`, `tests/test_autoevolution_source.py`
- **Files to read:** `work/source-formatting-parity/code-research.md`, `dom_blocks.py`

### Wave 3 (зависит от Wave 2)

#### Task 8: Runtime alignment guard
- **Description:** Drop `blocks` and log a WARNING when the patchable-block count disagrees with the paragraph count, at the site where the row is assembled. Serves AC8; see Decision 4.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_integration.py -q -k mismatch`
- **Files to modify:** `news_bot.py`, `tests/test_news_bot_alignment.py` (new)
- **Files to read:** `_llm_common.py`, `pending_articles_repo.py`, `dom_blocks.py`

#### Task 9: End-to-end formatting suite
- **Description:** Pin the whole chain for all three sources: formatting survives parser → queue → translation → Telegraph; the image cap holds on the measured 50-image lamley article; markers lost by the LLM degrade silently; a bold-heavy article keeps every span; an article near the checklist floor is not newly dropped.
- **Skill:** code-writing
- **Reviewers:** test-reviewer
- **Files to modify:** `tests/test_integration.py`
- **Files to read:** `dom_blocks.py`, `telegraph_publisher.py`, `news_bot.py`

### Audit Wave

#### Task 10: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified in this feature. Review holistically for cross-component issues: whether the shared module removed duplication or merely relocated it, whether the per-site policy hooks are the right seam, `BlockBuilder` state ownership, and consistency with Decisions 1-7. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 11: Security Audit
- **Description:** Full-feature security audit. Read all source files created/modified. OWASP Top 10 across components, with attention to the ReDoS contract the project requires of parser regex (bounded quantifiers only), href/img scheme allowlists surviving the extraction, the YouTube host allowlist that must run before the video-ID regex, and the DoS bounds in the runs renderer now that three more sources feed it. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 12: Test Audit
- **Description:** Full-feature test quality audit. Read all test files created. Verify coverage and meaningful assertions, with attention to the 2026-07-28 lesson: one-sided invariants («bold appears») that miss the opposite failure («bold appears where it should not»). Confirm negative controls exist for the heading heuristic and for the alignment guard. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 13: Pre-deploy QA
- **Description:** Acceptance testing: run all tests, verify every acceptance criterion from user-spec and this tech-spec. Explicitly confirm the orangetrack test files are unedited and the suite has no regressions against the 1628-passed baseline.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 14: Deploy
- **Description:** Promote `dev` → `main` and hand the operator the one-line deploy command. Deploy MUST run outside the 10:00–20:00 МСК publishing window: `job()` executes immediately on container start, so a restart inside the window fetches and publishes into whatever remains of it.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 15: Post-deploy verification
- **Description:** Live verification after the operator deploys:
  - first t-hunted publication — bold, subheadings and lists match the source page — tool: manual read + `curl_cffi` fetch of the original
  - image count on a gallery post does not exceed the cap — tool: bash
  - no WARNING about a blocks/paragraphs mismatch in the logs — tool: `ssh` + `docker logs`
  - decorated lead «💬 …» present and the last paragraph is not English — tool: manual read
  Tools: `ssh`, `docker logs`, `bash`, `curl_cffi`, manual channel reading.
- **Skill:** post-deploy-qa
- **Reviewers:** none
