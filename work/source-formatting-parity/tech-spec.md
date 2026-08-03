---
created: 2026-07-30
status: approved
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
  heuristic (`looks_like_heading`, opt-in — see Decision 2b), and a `BlockBuilder`
  that owns the block accumulator + image dedup and exposes the four `emit_*`
  operations. Per-site behaviour arrives as **four** policy inputs (chrome-class
  predicate, image-src picker, image-dedup key, video-provider DATA). Structural
  policy that already exists and is test-pinned (heading levels, `<br>`-splitting,
  bullet-less `<li>`) becomes the shared default; the heading heuristic does NOT,
  because it does not exist today in any parser.
- **`orangetrack_source.py`** — becomes the first consumer. Its published output must
  not change; its existing tests must pass **without being edited**.
- **`t_hunted_source.py`, `lamley_source.py`** — gain a block-building pass alongside
  the flat list, plus the aligned subtitle lift.
- **`autoevolution_source.py`** — its weaker `_runs_from_tag` (href-only; bold and
  italic are flattened, by its own docstring's admission) is replaced by the shared one.
- **`telegraph_publisher.py`** — image cap applied on the blocks path.
- **`news_bot.py`** — runtime `blocks`/`paragraphs` count guard; threads the
  per-source image cap into the renderer.
- **`_llm_common.py`** — bounds the marker-encoding loop on the REQUEST path.
- **`feature_flags.py` (new)** — the kill switch, importable by parsers.
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
**Decision:** New `dom_blocks.py` holds the walker body and leaf helpers. **Four**
policy inputs per site: chrome-class predicate, image-src picker, **image-dedup key**,
and — deliberately not a callable — **video-provider DATA** (host tuple + provider
name). The heading-level policy, `<br>`-splitting and bullet-less `<li>` become shared
defaults.

The video seam is data, not a callable, for a security reason. `orangetrack_source`
gates on `urlparse(...).hostname in _YOUTUBE_HOSTS` BEFORE running the ID regex, and
its own docstring names the gap it closes: «autoevolution's regex-only path has this
gap; we close it here». If the wrapper were injectable, autoevolution would inject that
regex-only path and the extraction would *sanction* the defect behind an interface —
the exact opposite of this decision's rationale. Keeping the host gate inside
`dom_blocks` and injecting only the allowed hosts fixes it once for everyone. Vimeo
hosts are added so autoevolution loses nothing.

The image-dedup key is the fourth seam because the two sites encode size differently:
orangetrack uses `src.split("?", 1)[0]`, t-hunted needs its Blogger size-suffix regex.
Sharing orangetrack's key would regress t-hunted to duplicate thumbnails on its
dominant post format.
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
**Decision:** `_build_content_from_blocks` takes the cap **as a parameter** and stops
emitting image blocks past it, keeping the hero. The value is threaded from the single
call site in `news_bot.py`, because the caps are per-source and different (t-hunted 30,
lamley 10, orangetrack 10, autoevolution 10) while the renderer is source-agnostic — a
single hard-coded default would violate AC9 for somebody.
**Rationale:** Serves user-spec AC9. Proven that non-empty `blocks` makes the
publisher ignore `images` entirely, so `IMAGE_LIMIT` becomes dead code — lamley pages
would jump from 10 to up to 50 images.
**Correction to an earlier claim:** an earlier draft said «orangetrack already caps in
its post-pass». It does **not**. `IMAGE_LIMIT` slices only the derived flat
`images_flat` list; `_emit_image` has no counter, and the publisher ignores `images`
entirely when `blocks` is non-empty. So **orangetrack and autoevolution already publish
more images than intended, today, in production.** This feature fixes that — which
means orangetrack's rendered output WILL change on gallery posts. See User-Spec
Deviations: that is an intended bug fix, not a regression, but it does collide with
AC10 as literally worded.
**Alternatives considered:** cap in each parser before returning blocks (rejected —
three places to forget); exempt orangetrack to preserve AC10 literally (rejected —
that preserves a known defect to satisfy the letter of a criterion whose intent was
«don't break orangetrack», not «keep its bugs»).

### Decision 6: Kill switch read from a parser-importable location
**Decision:** One env flag, default ON, parsed with the same truthy/falsy word set as
the existing two. It lives where the parsers can read it — **not** in `news_bot`.
**Rationale:** Serves user-spec AC11. Default **ON**, chosen by the operator
2026-07-30 against the security reviewer's recommendation of opt-in; the reviewer's
point is recorded in Risks so the trade-off is visible rather than lost.
Flag off ⇒ **the three new parsers** skip block emission. It must NOT gate emission
inside `dom_blocks`, because orangetrack is the shared module's first consumer and
flag-off would then strip its blocks too — a regression on a working source and an
AC10 violation in the flag-off state.
**Correction:** an earlier draft claimed the parsers *cannot* import `news_bot`
(circular). Verified false: `from news_bot import X` does raise, but bare
`import news_bot` with late attribute access works in both import orders and is the
documented house style at `news_bot.py:131-133`. A separate `feature_flags.py` is still
preferred — it keeps the parsers off a 4600-line module — but as a preference, not an
impossibility.
**Alternatives considered:** gate only downstream in `fetch_full_article` (rejected —
strips `blocks` but the parser has already changed the flat text, so "off" would not
mean "as before"); reuse `DEDUP_SERIES_ENABLED` (rejected — unrelated blast radius).
**Scope of the guarantee (operator-approved):** flag-off means *no formatting, flat
text*, **not** byte-identical to today — measured drift is **−0.10 %** (t-hunted) and
**−1.18 %** (lamley, and that delta is WordPress chrome removal, i.e. an improvement).
Byte-identity would require keeping two text-derivation paths alive in parallel.

### Decision 8: Resource bounds live where the work happens [TECHNICAL]
**Decision:** Bound the marker-encoding loop on the REQUEST path in `_llm_common`, and
bound runs/text at PARSE time in `dom_blocks`. Over the bound, strip runs and emit
plain text — never truncate the article. The ReDoS contract (bounded quantifiers only,
`str.endswith` rather than a regex for the heading-punctuation test) is stated here, in
the tasks and in the acceptance criteria — not only in the audit task.
**Rationale:** `[TECHNICAL]` — no user-spec requirement, but the security review found
a live availability hazard. `_encode_format_markers` does a `text.find`-per-run scan
with **no bound**, while the existing `_MAX_TEXT_FOR_RUNS` / `_MAX_RUNS_PER_BLOCK`
bound only the RENDER path. Today the loop is free for autoevolution by accident — its
href-only runs carry no `formats`, so every iteration short-circuits. Task «autoevolution
uses the shared walker» removes that accident. Within t-hunted's existing 2 MB fetch
cap, ~200k single-character bold spans give ~4·10¹⁰ character comparisons: tens of
minutes to hours on a single-process bot whose restart is barred 10:00–20:00 МСК, with
all three publish slots inside that window.
This bound is a RESOURCE guard, not the editorial threshold AC7 forbids: it fires only
on pathological input, and it deliberately does not gate the heading heuristic on
length — a block whose runs were stripped simply stops being «entirely bold».

### Decision 9: The evidence must be reproducible in the repo [TECHNICAL]
**Decision:** Commit the 14 fetched article pages as HTML fixtures, and generate a
golden file (`{title, subtitle, paragraphs, blocks, images}` + `preview_nodes(...)` for
every orangetrack fixture) on the pre-extraction commit. The golden file is compared by
string equality; editing it is a gate failure, checked the same way as editing the
tests.
**Rationale:** `[TECHNICAL]`. Two problems this closes. First, `tests/fixtures/`
contains **zero HTML** today, so every number this spec leans on — 24/286 promotions,
lamley's 14/41/48/50 images, 13-of-14 misalignment, the 73-vs-85 char headroom — is
unreproducible, and the «measured 50-image lamley fixture» a planned integration test
targets does not exist. Second, «orangetrack's existing tests pass unedited» is a
weaker gate than it sounds: `grep` for `strong|<b>|<em>` returns **0** across all three
parser test files, so they are structurally blind to this entire feature, and no
fixture is a real article. `.dockerignore` already excludes `tests/`, so the corpus
costs prod nothing.

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

**Feature size:** L. Delivered in **two phases** (operator decision 2026-07-30):
**Phase 1** ships the shared module, all safety work and t-hunted; **Phase 2** adds
lamley and autoevolution. The chain is proven on the source where the operator actually
noticed the problem before two more parsers are touched.

### Unit tests
- `dom_blocks` positive: bold inside a paragraph; nested bold-in-italic; overlapping
  runs (first-wins); empty/whitespace runs skipped; flattening yields no doubled spaces
  and every run stays locatable via `text.find`.
- Heading heuristic **negative controls** (the 2026-07-28 lesson — a one-sided
  invariant misses the opposite failure):
  - partially-bold short paragraph → NOT a heading;
  - whole-bold paragraph ending in `.` → NOT a heading;
  - whole-bold **long** paragraph → IS a heading, pinned deliberately so a future
    re-added length cap fails a test rather than passing silently;
  - `<p><strong>Part</strong> and <strong>two</strong></p>` → NOT whole-bold (the
    predicate needs one merged span, and this is the mechanically load-bearing case);
  - `<p><strong>Ford</strong> vs Ford</p>` → the span merge is built on `text.find`,
    so a repeated substring must not fake full coverage.
- Heuristic **off for orangetrack, on for the others** — asserted per source.
- Heading-level defaults: h2/h3/h4 → level 3, h5 → paragraph, h1/h6 dropped.
- `<br>`-splitting and bullet-less `<li>` preserved as shared defaults.
- Image-dedup key: t-hunted's Blogger size-suffix collapses variants; orangetrack's
  query-strip behaviour is unchanged.
- Video: a non-YouTube host containing a YouTube-shaped path is NOT wrapped; Vimeo
  hosts ARE.
- Resource bounds: over-bound input strips runs and emits plain text, article intact.
- Kill switch: flag off ⇒ the three new parsers emit no `blocks`; **orangetrack still
  does** (it must not be gated).

### Integration tests
- Formatting survives parser → `insert_pending` (JSON in SQLite) → translation →
  `telegraph_publisher` nodes. Phase 1: t-hunted. Phase 2: lamley, autoevolution.
- Alignment guard: mismatch ⇒ `blocks` dropped, article published, WARNING logged;
  plus a **false-positive control** — the guard does NOT fire on aligned articles. An
  always-firing guard would disable the feature on 100 % of publications and still pass
  every positive test. `None` vs `[]` is asserted, since that distinction is live.
  The mismatch is reached through the REAL parser via the title-dedup divergence
  (`t_hunted_source.py:198` / `lamley_source.py:390` skip a `<p>` equal to the title
  while the walker does not), not via a fabricated dict.
- Image cap: per-source value is threaded and honoured — t-hunted 30 AND lamley 10,
  with a lower control so a single hard-coded 10 fails.
- AC6 `markers_lost`: a translation with no markers publishes as plain text and
  `send_admin_notification` is not called.
- AC7 `bold_heavy`: an article ≥90 % bold keeps every span through to Telegraph nodes.
- AC3 behavioural half: a `list_item` block is patched from the MAIN response and does
  not reach the caption pass — currently untested in all four engine test files.
- An article near the `_is_text_only_checklist` 500-char floor is not newly dropped.
- orangetrack: golden-file equality (Decision 9), not merely «tests pass unedited».

**Every `-k` selector named above must match at least one collected test.** `pytest -k`
collecting zero exits 0, so an unwritten test is a silent pass — the QA task verifies
collection counts, not just exit codes.

### E2E tests
None live. autoevolution article pages return **403** to our client and lamley is
throttled 20 s + 24 h blacklist, so a live gate would be flaky by construction. The
substitute is the **committed HTML corpus** of Decision 9 — not the operator's
post-deploy read, which cannot gate anything.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach
All pre-deploy verification runs **offline against the committed corpus**. The
heading-heuristic measurement is re-run over those fixtures with fixed expected values,
so it is deterministic and CI-runnable. The preview check renders a blocks fixture
through `preview_renderer.render_html` and asserts `<h3>`, `<strong>` and a list item
are present.

A live re-fetch of t-hunted (the only source reachable without a 403) is kept as
**post-deploy drift monitoring**, where a network failure is explicitly
**inconclusive** — never a failure. Rationale: deploys are barred 10:00–20:00 МСК, so a
spurious network failure in a pre-deploy gate costs a whole day, and this project
already has a logged incident where an outage alarm turned out to be server DNS.

### Tools required
`bash`, `python3`, `pytest` pre-deploy — no network, no MCP. Post-deploy: `ssh` +
`docker logs` (operator-run), `curl_cffi` for the optional drift check, and manual
reading of the channel.

## Risks

| Risk | Mitigation |
|------|-----------|
| orangetrack output drifts during extraction | Golden-file string equality over every fixture (Decision 9). «Tests pass unedited» alone is a weak gate — `grep` for `strong\|<b>\|<em>` returns 0 across all three parser test files, so they are blind to this feature. |
| Whole-bold long paragraph becomes an oversized heading | Unobserved in 286 real paragraphs. Pinned deliberately by a test so the behaviour is a decision, not an accident. Kill switch + operator's post-deploy read. Cosmetic and reversible. |
| Heuristic changes orangetrack's live output | Decision 2b keeps it off there. Measured: 0 of 49 orangetrack paragraph blocks in its 37 fixtures would flip — but its live feed was never fetched, and its `has-*-color` spans map to bold, so the real exposure is unmeasured. Off-by-default is the answer to an unmeasured risk. |
| Unbounded encode loop stalls the bot | Decision 8 bounds the request path. Consequence if missed: hours of stall on a process that cannot be restarted 10:00–20:00 МСК, with every publish slot inside that window. |
| Injected video wrapper reintroduces the missing host allowlist | Decision 1 makes the seam DATA, not a callable; the `urlparse` host gate stays in `dom_blocks`. |
| Image src reaches the page unvalidated | autoevolution's picker uses `startswith("http")` (so `httpx://evil/x.jpg` passes) and its gallery path applies no scheme test at all. The publisher — already «the single choke point» per Decision 5 — validates the scheme itself. |
| Blocks/paragraphs desynchronise in an uncovered shape | Runtime guard (Decision 4) + false-positive control. |
| Flag default ON was chosen against the security review | Recorded here deliberately. The reviewer's argument: no pre-deploy E2E is possible, Decision 3b silences every failure, and disabling requires a restart barred during the publishing day — so the switch cannot deliver the mitigation it is credited with, in time. The operator accepted this trade knowingly on 2026-07-30. Phase-1-only scope (t-hunted) narrows the blast radius. |
| Flat text shifts which paragraphs the promo filter scans | `_is_promo_article` reads the first 8 paragraphs; a test pins a case where chrome sits at the head. |
| Deploy-files invariant test asserts across THREE manifests | `deploy.sh`, `deploy.yml`, `deploy_test.yml` — a file list naming only `deploy.sh` produces two failing tests. |
| Second LLM call cost grows | Decision 7 stops list-item re-translation. Measured on the first publications; the operator's standing rule is quality over token cost. |

## User-Spec Deviations

- **AC2 (heading heuristic):** user-spec fixes the length limit at **≤80 chars**.
  Tech-spec **removes the length condition entirely**. Reason: 286 real paragraph
  blocks contain **zero** whole-bold non-headings, so length discriminated nothing and
  only clipped genuine headings (a lamley subheading at 85 chars). Also supersedes the
  user-spec's Тестирование line and «Как проверить» step 3, which pin the 80/81
  boundary — that test is intentionally dropped and replaced by negative controls. →
  **APPROVED by operator 2026-07-30**
- **AC2 (trailing punctuation):** `…` and `….` additionally count as
  heading-compatible, to keep lamley's «Also ran….». → **APPROVED 2026-07-30**
- **AC2 (scope):** the heuristic is opt-in per source and **off for orangetrack**
  (Decision 2b). Not in the user-spec; required to keep AC10. → **implied by AC10**
- **AC9 vs AC10 — a genuine collision.** AC9 requires the image cap on the blocks
  path; AC10 requires orangetrack byte-identical. But orangetrack's cap is **not**
  applied on the blocks path today (`IMAGE_LIMIT` slices only the derived flat list),
  so it already publishes more images than intended. Honouring AC9 therefore CHANGES
  orangetrack's rendered output on gallery posts. Tech-spec honours AC9 and treats this
  as an intended bug fix, reading AC10's intent as «don't break orangetrack» rather
  than «keep its bugs». The golden file will show the diff explicitly, so the change is
  reviewed rather than discovered. → **APPROVED by operator 2026-08-03.** Fix the cap
  everywhere, orangetrack included. Two conditions the operator's approval rests on, so
  Task 6 must honour both: (1) the orangetrack image-count change is presented as a
  reviewable golden-file diff BEFORE it ships — it must not first appear in the channel;
  (2) the diff is expected to show FEWER images on gallery posts and nothing else — any
  other orangetrack difference is a regression, not part of this deviation.
- **AC11 (kill switch):** means *no formatting, flat text*, not byte-identity —
  measured drift −0.10 % / −1.18 %. Default **ON** per operator, against the security
  review's opt-in recommendation (see Risks). → **APPROVED 2026-07-30**
- **Risk 2 (flat-text shrinkage) — downgraded.** The nested-`<li><p>` premise is
  measurably false (zero occurrences in 14 articles and 15 fixtures). Kept at its true
  weight with a boundary test. → informational
- **Added: `dom_blocks.py`, `feature_flags.py`, an HTML corpus and a golden file.**
  Reason: Risk 1 forbids copy-paste; the corpus and golden file make the spec's own
  evidence reproducible and its main gate real. → **flagged for the record**
- **Added: resource bounds and the ReDoS contract** (Decision 8), `[TECHNICAL]`, from
  the security review. → **flagged for the record**

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] orangetrack golden file matches byte-for-byte, and the golden file itself is
      unedited (the diff for the image-cap change is reviewed and accepted separately)
- [ ] `grep -c "strong\|<b>\|<em>"` > 0 in the parser test files that previously
      returned 0
- [ ] Every `-k` selector in the verification plan collects ≥1 test
- [ ] All parser regex uses bounded quantifiers; the heading-punctuation test uses
      `str.endswith`, not a regex
- [ ] Request-path marker encoding is bounded; over-bound input yields plain text and
      an intact article
- [ ] Video wrapping gates on host BEFORE the ID regex, for every source
- [ ] Image `src` scheme is validated in the publisher, not only in the source picker
- [ ] Per-source image caps are threaded (t-hunted 30, lamley 10) — a single
      hard-coded value fails a test
- [ ] Heading heuristic OFF for orangetrack, ON for the sources in scope
- [ ] Flag off ⇒ the three new parsers emit no `blocks`; orangetrack still does
- [ ] New modules registered in ALL THREE deploy manifests
- [ ] No new package dependencies; no DB migration
- [ ] Full suite green, no regressions (baseline: 1628 passed, 441 subtests)

## Implementation Tasks

<!-- PHASE 1 ships. Phase 2 is listed at the end and will be decomposed separately. -->

### Wave 1 (независимые)

#### Task 1: HTML corpus + orangetrack golden gate
- **Description:** Commit the 14 fetched article pages as HTML fixtures so every measured number in this spec becomes reproducible, and generate the orangetrack golden file on the pre-extraction commit. Serves Decision 9; this must land before any extraction so the gate has a baseline.
- **Skill:** code-writing
- **Reviewers:** test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_orangetrack_golden.py -q` green on unmodified code
- **Files to modify:** `tests/fixtures/*.html` (new), `tests/fixtures/orangetrack_golden.json` (new), `tests/test_orangetrack_golden.py` (new)
- **Files to read:** `work/source-formatting-parity/code-research.md`, `orangetrack_source.py`, `telegraph_publisher.py`

#### Task 2: Feature flag module
- **Description:** Add `feature_flags.py` exposing this feature's kill switch, importable by the parsers. Serves AC11; Decision 6 fixes the default and states that the flag must not gate emission inside the shared module.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "import feature_flags; print(feature_flags.source_formatting_enabled())"` → `True` with no env set
- **Files to modify:** `feature_flags.py` (new), `.env.example`, `tests/test_feature_flags.py` (new), `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml`, `tests/test_deploy_files_invariant.py`
- **Files to read:** `news_bot.py`, `.claude/skills/project-knowledge/references/deployment.md`

#### Task 3: `list_item` as a patchable block type in all four engines
- **Description:** Align the four per-engine patchable-type tuples with the shared one so list items are patched from the main response instead of being re-translated by the caption pass. Serves AC3; see Decision 7.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_claude_transcreation.py -q -k list_item` collects ≥1 test and passes
- **Files to modify:** `claude_transcreation.py`, `gemini_transcreation.py`, `openai_transcreation.py`, `openrouter_transcreation.py`, and the four engine test files
- **Files to read:** `_llm_common.py`

#### Task 4: Bound the request-path marker encoding
- **Description:** `_encode_format_markers` scans `text.find` per run with no bound, on the path that builds the LLM request. Bound it; over the bound, strip runs and send plain text. Serves Decision 8 — the security review's availability finding.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** encode a pathological 200k-run paragraph and assert it completes under a second with runs stripped
- **Files to modify:** `_llm_common.py`, `tests/test_llm_common.py`
- **Files to read:** `telegraph_publisher.py` (the existing render-path bounds)

### Wave 2 (зависит от Wave 1)

#### Task 5: Extract `dom_blocks.py` and migrate orangetrack onto it
- **Description:** Create the shared module — inline-markup walker, run flattening, the opt-in heading heuristic, resource bounds, and a `BlockBuilder` with the four per-site policy inputs — and make `orangetrack_source` its first consumer. The golden file from Task 1 is the gate. Register the module in all three deploy manifests.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_orangetrack_source.py tests/test_orangetrack_golden.py -q` passes AND `git diff --stat tests/test_orangetrack_source.py tests/fixtures/orangetrack_golden.json` is empty
- **Files to modify:** `dom_blocks.py` (new), `orangetrack_source.py`, `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml`, `tests/test_deploy_files_invariant.py`, `tests/test_dom_blocks.py` (new)
- **Files to read:** `work/source-formatting-parity/code-research.md`, `telegraph_publisher.py`, `boilerplate_filter.py`

#### Task 6: Rendering surface — per-source image cap + src validation + preview parity
- **Description:** Thread the per-source image cap from the call site into `_build_content_from_blocks` and enforce it there, validate image `src` schemes in the publisher rather than trusting each source's picker, and own AC12 by proving the preview renders this feature's headings, bold and list items. Note this changes orangetrack's gallery output — see the AC9/AC10 deviation.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_telegraph_publisher.py tests/test_preview_renderer.py -q`
- **Files to modify:** `telegraph_publisher.py`, `news_bot.py`, `preview_renderer.py`, `tests/test_telegraph_publisher.py`, `tests/test_preview_renderer.py`
- **Files to read:** `orangetrack_source.py`, `lamley_source.py`, `t_hunted_source.py`

### Wave 3 (зависит от Wave 2)

#### Task 7: t-hunted emits blocks
- **Description:** Drive `BlockBuilder` over the Blogger body so t-hunted returns `blocks` with `runs` alongside the flat list, with the subtitle lift AND the title-dedup predicate applied consistently to both lists. The heading heuristic is enabled here and is t-hunted's only heading path; its Blogger image-dedup key is injected.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** parse every t-hunted corpus fixture and assert `len(patchable blocks) == len(paragraphs)` for each
- **Files to modify:** `t_hunted_source.py`, `tests/test_t_hunted_source.py`
- **Files to read:** `work/source-formatting-parity/code-research.md`, `dom_blocks.py`, `orangetrack_source.py`, `boilerplate_filter.py`

#### Task 8: Runtime alignment guard
- **Description:** Drop `blocks` and log a WARNING when the patchable-block count disagrees with the paragraph count, at the site where the row is assembled. Serves AC8; see Decision 4. Needs a false-positive control — an always-firing guard would silently disable the feature.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 -m pytest -q -k "mismatch or aligned"` collects ≥2 tests and passes
- **Files to modify:** `news_bot.py`, `tests/test_news_bot_alignment.py` (new)
- **Files to read:** `_llm_common.py`, `pending_articles_repo.py`, `dom_blocks.py`

#### Task 9: End-to-end formatting suite (Phase 1 scope)
- **Description:** Pin the chain for t-hunted: formatting survives parser → queue → translation → Telegraph; markers lost by the LLM degrade silently with no operator ping; a bold-heavy article keeps every span; the per-source image cap holds with a lower control; an article near the checklist floor is not newly dropped.
- **Skill:** code-writing
- **Reviewers:** test-reviewer
- **Files to modify:** `tests/test_integration.py`
- **Files to read:** `dom_blocks.py`, `telegraph_publisher.py`, `news_bot.py`

### Audit Wave

#### Task 10: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified. Review holistically for cross-component issues: whether the shared module removed duplication or relocated it, whether the four policy inputs are the right seams, `BlockBuilder` state ownership, and consistency with Decisions 1-9. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 11: Security Audit
- **Description:** Full-feature security audit. OWASP Top 10 across components, with attention to the bounded-quantifier contract, the host-before-regex video gate for every source, image `src` scheme validation in the publisher, and the request-path encoding bound. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 12: Test Audit
- **Description:** Full-feature test quality audit. Verify coverage and meaningful assertions, with attention to the 2026-07-28 lesson about one-sided invariants. Confirm the heuristic negative controls and the alignment guard's false-positive control exist, and that every `-k` selector in the verification plan collects at least one test. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 13: Pre-deploy QA
- **Description:** Acceptance testing: run all tests, verify every acceptance criterion from user-spec and this tech-spec. Confirm the orangetrack golden file and test files are unedited, that every named `-k` selector collects tests, and that the suite has no regressions against the 1628-passed baseline.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 14: Deploy
- **Description:** Promote `dev` → `main` and hand the operator the one-line deploy command. Deploy MUST run outside the 10:00–20:00 МСК publishing window: `job()` executes immediately on container start, so a restart inside the window fetches and publishes into whatever remains of it.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 15: Post-deploy verification
- **Description:** Live verification after the operator deploys:
  - first t-hunted publication — bold, subheadings and lists match the source page — tool: manual read + `curl_cffi`
  - image count on a gallery post does not exceed the cap, for t-hunted AND orangetrack (whose output intentionally changed) — tool: bash
  - no blocks/paragraphs mismatch WARNING in the logs — tool: `ssh` + `docker logs`
  - decorated lead «💬 …» present and the last paragraph is not English — tool: manual read
  A `curl_cffi` failure is INCONCLUSIVE, never a failure.
  Tools: `ssh`, `docker logs`, `bash`, `curl_cffi`, manual channel reading.
- **Skill:** post-deploy-qa
- **Reviewers:** none

### Phase 2 (отдельная поставка, декомпозируется после Phase 1)

Not decomposed here. Scope: `lamley_source` emits blocks (both heading paths, unconditional lift, WordPress chrome via the shared predicate); `autoevolution_source` swaps its href-only walker for the shared one; the end-to-end suite extends to both; then its own audit, QA and deploy waves. Phase 2 starts only after Phase 1 has been live long enough for the operator to read several t-hunted publications.
