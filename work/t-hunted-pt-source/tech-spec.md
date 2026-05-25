---
created: 2026-05-26
status: draft
branch: dev
size: M
---

# Tech Spec: t-hunted-pt-source

## Solution

Add `t-hunted.blogspot.com` as the 4th news source by extending the existing pipeline at four narrow seams — no new architecture, no schema changes, no new dependencies. The implementation is structurally a copy of `lamley_source.py` minus its Cloudflare/WAF/throttle apparatus (~150 LOC), wired into the existing article-dispatcher, source-name registry, and Telegram hashtag emission. Portuguese-language input is handled by widening the LLM system-prompt assertion and adding a per-source style block + bilingual PT/EN/RU glossary in `ux-guidelines.md` — `source_name='t-hunted'` already flows to the LLM through `_build_user_message`, so no transcreation-engine code changes. Boilerplate filter gains a length-bounded `^`-anchored PT pattern block paralleling the existing RU defence-in-depth precedent.

## Architecture

### What we're building/modifying

- **New: `t_hunted_source.py`** — Blogger HTML scraper (~150 LOC). Exposes `fetch_t_hunted_article(link, session=None, notifier=None) -> dict | None` matching the existing lamley signature. Strict SSRF allowlist (`t-hunted.blogspot.com` only). Returns `{title, subtitle, paragraphs, images}` or `None` on failure.
- **New: `tests/test_t_hunted_source.py`** — unit tests mirroring `test_lamley_source.py` (TestFetch + TestHostAllowlist).
- **Modified: `news_bot.py`** — 4 edits: import; `NETLOC_TO_SOURCE` map entry; `_source_hashtag` patched via new `SOURCE_HASHTAG_OVERRIDE` dict; `fetch_full_article` dispatcher branch.
- **Modified: `feeds.json`** — append t-hunted RSS URL (4/5 entries, under cap).
- **Modified: `boilerplate_filter.py`** — append PT pattern block after RU defence-in-depth.
- **Modified: `admin_alerts.py`** — 3 new builders (E031 host-rejected, E032 fetch-error, E033 no-body).
- **Modified: `.claude/skills/project-knowledge/references/ux-guidelines.md`** — 3 edits: widen input-language assertion (line 22), add `### 🟤 t-hunted` per-source style block, add `## Glossary — PT/EN/RU` section with 14 baseline entries.
- **Modified: deploy plumbing** — `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` — add `t_hunted_source.py` to each FILES array.
- **Modified: test extensions** — `tests/test_sources_registry.py` (NETLOC_TO_SOURCE + `_resolve_source_name`), `tests/test_telegram.py` (locks `#thunted #news` byte format), `tests/test_boilerplate_filter.py` (PT patterns positive class), `tests/test_admin_alerts.py` (E031-E033 builders), `tests/test_distributed_schedule_integration.py` (EN+PT smoke).

### How it works

Cron tick at 10:00 МСК → `_fetch_rss_entries` reads t-hunted RSS via existing `feedparser` path → entries get `source_name='t-hunted'` via `_resolve_source_name` → `fetch_full_article` dispatcher routes blogspot URLs to `t_hunted_source.fetch_t_hunted_article` → BeautifulSoup scrape of `.post-body`/`.entry-content` → `filter_boilerplate` strips PT footer noise → entry staged into `pending_articles` with `source_name='t-hunted'` → publish loop calls `_fallback_publish` → LLM transcreation via existing dispatcher (unchanged code), `source_name` reaches `_build_user_message` payload → LLM applies widened system prompt with t-hunted per-source style + PT glossary → Telegra.ph publishes RU page → `send_telegraph_teaser` emits `#thunted #news` via `SOURCE_HASHTAG_OVERRIDE['t-hunted']='thunted'` → Telegram subscriber sees identical-shape preview card.

### Shared resources

None. No singleton resources are introduced. Existing shared resources (HTTP session pool inside `requests`, SQLite connection per-call pattern in `pending_articles_repo`, LLM client instances in `*_transcreation.py`) are unchanged.

## Decisions

### Decision 1: Use `lamley_source.py` as parser blueprint, drop WAF apparatus
**Decision:** New `t_hunted_source.py` mirrors lamley's structure 1:1 for the contract (`fetch_t_hunted_article(link, session, notifier) -> dict|None`), HTML walk (`<h1 class="post-title">` → `<div class="post-body entry-content">` → `<p>/<li>/<h2-4>/<blockquote>` → images), and SSRF allowlist pattern. Skip the WAF cooldown / 429 retry / per-URL blacklist / curl_cffi impersonation blocks — Blogger has no Cloudflare and serves plain `requests` fine.
**Rationale:** Blogger HTML structure is closest to lamley's `.entry-content` flat model. Skipping the WAF apparatus drops ~230 LOC. Net new module is ~150 LOC vs lamley's 380. Supports user-spec D2 (lamley blueprint, minus WAF).
**Alternatives considered:** Autoevolution (Cloudflare bypass + block ordering — overkill); orangetrack (~700 LOC, aggregator alerts — too heavy, alert aggregation not needed for low-volume source); build-from-scratch (rejected — wastes lamley's tested patterns).

### Decision 2: Hashtag emission via new `SOURCE_HASHTAG_OVERRIDE` map
**Decision:** Add module-level constant `SOURCE_HASHTAG_OVERRIDE = {'t-hunted': 'thunted'}` in `news_bot.py`. Patch `_source_hashtag` to first resolve source_name via `_resolve_source_name(source_url)`, look up the override map, fall back to the existing netloc-second-level logic if absent. Channel teaser emits `#thunted #news` byte-for-byte.
**Rationale:** Supports user-spec AC6. Default `parts[-2]` netloc logic returns `#blogspot` for `t-hunted.blogspot.com` — wrong attribution. Telegram hashtag spec rejects hyphens (`#t-hunted` renders as `#t` + plain text). The override map is open for extension (future blogspot/subdomain sources can register their own labels without touching the helper logic).
**Alternatives considered:** Special-case branch inside `_source_hashtag` checking `netloc.endswith('.blogspot.com')` → return `parts[-3]` — works but spreads source-specific logic into the helper; harder to extend if a future blogspot source needs a non-subdomain label.

### Decision 3: Dedicated admin alerts E031-E033 (no aggregator pattern)
**Decision:** Three new `admin_alerts` builders — `alert_t_hunted_host_rejected(link)`, `alert_t_hunted_fetch_error(link, error)`, `alert_t_hunted_no_body(link)` — next codes after E030. Lamley-style direct alerts, not an orangetrack-style per-tick aggregator.
**Rationale:** Supports user-spec AC9. Low-volume source (~2 posts/day, occasional fetch error) — direct alerts give operator immediate triage context without aggregation overhead. Three alerts cover the parser's three distinct failure modes from user-spec error scenarios table; one-each is the minimal viable signal.
**Alternatives considered:** Generic `[E002] alert_source_fetch_failed` (rejected — loses parser-specific context for triage); 4 alerts mirroring lamley (host/size/fetch/no-body — rejected, no size guard needed since Blogger doesn't serve oversized payloads).

### Decision 4: PT boilerplate patterns globally applied (no language tags)
**Decision:** Append a `# Portuguese — defence in depth for t-hunted.blogspot.com` block to `_BOILERPLATE_PATTERNS` in `boilerplate_filter.py` (after the existing RU defence-in-depth block). 10 patterns: `^compartilhar`, `^marcadores\s*:`, `^postado\s+por`, `^postagem`, `^enviar\s+por\s+email`, `^postagens\s+mais\s+(antigas|recentes)`, `^assinar\s*:`, `^leia\s+mais$`, `^postar\s+(um\s+)?coment[áa]rio`, `^p[aá]gina\s+(inicial|principal)`. Length-bounded by existing `_MAX_BOILERPLATE_LEN = 120` and `^`-anchored.
**Rationale:** Supports user-spec AC8. Mirrors the 2026-05-08 RU defence-in-depth precedent — single flat pattern list, no per-language tagging. PT vocabulary (compartilhar, marcadores, postado) has effectively zero false-positive risk on EN/RU under the 120-char length bound.
**Alternatives considered:** Per-language tagging (rejected — adds infrastructure for one new language; precedent rejected this in 2026-05-08); per-source filter call (rejected — would couple `boilerplate_filter` to source identity).

### Decision 5: `ux-guidelines.md` minimum edits — prompt widening + style skeleton + 14-entry glossary baseline
**Decision:** Three discrete edits to the canonical LLM system prompt file: (a) line 22 wording change from "входящий **английский** текст" to "входящий текст (английский или португальский)"; (b) insert `### 🟤 t-hunted` per-source style block after the Mattel block — Voice / Tone dial / Length / Structure quirks / title-examples fields, all marked `[TBD operator]` to be filled iteratively after first 5-10 publishes (per user-spec deferral); (c) insert new `## Glossary — PT/EN/RU` section between Per-source notes and Red flags, with 14 baseline entries from code-research §E (Caça → Hunt, Super-Caça/Super-T → Super-T, Mainline, Premium, Treasure Hunt, Casting, Pintura, Decalque [VERIFY], Carrinho [VERIFY], etc.). Two `[VERIFY operator]` markers flag entries needing post-deploy refinement.
**Rationale:** Supports user-spec AC7. `source_name` already plumbed into `_build_user_message` (no transcreation-engine code change). Style-block content is intentionally minimal — operator owns prose tuning. Glossary at 14 entries exceeds AC7's ≥10 floor without bloating the prompt.
**Alternatives considered:** Adding a separate PT system prompt (rejected — duplicates 90% of the canonical prompt, drift risk); deferring all PT-style hints to post-deploy (rejected — first publishes need at least a skeleton).

### Decision 6: Single atomic PR, no MVP/Extension split
**Decision:** All ~12-15 file edits land in one PR `feature/t-hunted-pt-source` → `dev`. No staged rollout; either the feature ships in full or `news_bot.service` would `ImportError` on the next cron tick (partial deploys violate the deploy invariant).
**Rationale:** Supports user-spec deploy_approach. Wave-based execution (Waves 1-3) is a parallelisation construct for agent-team, not a delivery split — all waves land in the same PR.
**Alternatives considered:** MVP (parser only, no prompt update) → Extension (prompt + glossary): rejected because the LLM would translate PT input with the EN-locked prompt — output quality would be poor without the prompt widening, defeating the value of releasing the parser.

### Decision 7: [TECHNICAL] Brown circle 🟤 emoji for `SOURCE_EMOJI['t-hunted']`
**Decision:** Use Unicode `U+1F7E4` (🟤 brown circle) as the source emoji for archived `hw_review.py` consumer.
**Rationale:** Only "warm" circle not already used by other sources (🟠 autoevolution / 🔵 lamley / 🟡 mattel / 🟣 orangetrack). Cosmetic — consumed only by the archived `hw_review` CLI, not by admin alerts or channel posts. `[TECHNICAL]` because user-spec doesn't mention SOURCE_EMOJI; this is implementation hygiene to keep the dict consistent.
**Alternatives considered:** Leave SOURCE_EMOJI entry absent (rejected — dict has all other sources, asymmetry would surprise future maintainers); pick a different emoji (no other warm circles available).

## Data Models

None. No SQLite schema changes — `source_name='t-hunted'` is a free-text value in the existing `pending_articles.source_name TEXT` column. No new tables, columns, or indexes. Existing `news.db` files on prod and test require no migration.

## Dependencies

### New packages

None. All required dependencies already in `requirements.txt`:
- `requests` — HTTP fetch (no Cloudflare bypass needed for Blogger)
- `beautifulsoup4` — HTML parsing
- `feedparser` — RSS/Atom (handles Blogger's `?alt=rss` URL natively)

### Using existing (from project)

- `admin_alerts` — extend with E031-E033 builders; existing `send_admin_notification` and `_redact_text` paths unchanged
- `boilerplate_filter` — extend `_BOILERPLATE_PATTERNS` list; existing `filter_boilerplate(paragraphs)` API unchanged
- `news_bot._fetch_rss_entries` — already universal; no per-source feed code added
- `news_bot.fetch_full_article` — new dispatcher branch
- `_llm_common._build_user_message` — passes `source_name` to LLM payload unchanged
- `pending_articles_repo` — schema-agnostic, accepts `source_name='t-hunted'` as free-text
- `telegraph_publisher` — source-agnostic; renders the standard hero+subtitle+body+footer page

## Testing Strategy

**Feature size:** M

### Unit tests

- **`tests/test_t_hunted_source.py` (new file, ~12 methods):**
  - `TestFetchTHuntedArticle::test_parses_title_subtitle_paragraphs_images`
  - `TestFetchTHuntedArticle::test_returns_none_on_http_error`
  - `TestFetchTHuntedArticle::test_returns_none_on_missing_body`
  - `TestFetchTHuntedArticle::test_image_limit_enforced`
  - `TestFetchTHuntedArticle::test_image_dedup_strips_blogger_size_suffix`
  - `TestFetchTHuntedArticle::test_boilerplate_filter_applied_before_subtitle_lift`
  - `TestHostAllowlist::test_allows_t_hunted_blogspot_com`
  - `TestHostAllowlist::test_rejects_other_blogspot_subdomain`
  - `TestHostAllowlist::test_rejects_arbitrary_external_host`
  - `TestHostAllowlist::test_rejects_non_http_scheme`
  - `TestHostAllowlist::test_fetch_returns_none_and_pings_when_host_rejected`
- **`tests/test_admin_alerts.py` (extend):**
  - `test_alert_t_hunted_host_rejected_e031`
  - `test_alert_t_hunted_fetch_error_e032`
  - `test_alert_t_hunted_no_body_e033`
- **`tests/test_sources_registry.py` (extend):**
  - extend `test_netloc_to_source_map_keys` — assertion includes `t-hunted.blogspot.com`
  - extend `test_values_are_canonical_source_names` — assertion includes `t-hunted`
  - extend `test_resolve_source_name_*` — new case for t-hunted netloc
- **`tests/test_telegram.py` (extend):**
  - `test_t_hunted_teaser_uses_thunted_tag` — locks `#thunted #news` byte format
- **`tests/test_boilerplate_filter.py` (extend):**
  - `TestIsBoilerplatePositive::test_portuguese_patterns_filtered` (parametrised over the 10 PT patterns)
  - `TestIsBoilerplateNegative::test_pt_lookalike_prose_kept` — guards against PT pattern matching real prose

### Integration tests

- **`tests/test_distributed_schedule_integration.py` (extend):**
  - `test_integration_t_hunted_smoke_en_pt_mixed_tick` — single `job()` tick with one mocked EN entry (autoevolution-style) + one mocked PT entry (t-hunted-style); asserts both publish without cross-language interference, dedup unchanged, slot ordering preserved, both rows land in `published_articles`.

### E2E tests

None. Operator visually verifies first 5-10 t-hunted publishes on test channel `@myhwchannel123` per user-spec verification_strategy. No automated browser-driven flows needed for an M-size feature.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Pre-deploy: pytest gate (all unit + integration tests green, full suite no regressions, deploy plumbing grep). Per-task `Verify-smoke` fields below cover RSS connectivity, hashtag emission lock, and SSRF allowlist enforcement.

Post-deploy: operator visual checklist on test channel via Telegram client — described in the Post-deploy verification task. Bot doesn't expose a programmatic post-fetching API on our side; the operator-facing checks are the gate.

### Tools required

- `pytest` — full test suite + targeted -k filters
- `bash` + `grep` — deploy FILES list invariant verification
- `python3` + `feedparser` — RSS smoke connectivity check
- `ssh hwbot` — server-side journalctl tail for [E031-E033] alert absence (post-deploy)

Not required: Playwright MCP, Telegram MCP (operator does visual checks directly in Telegram client).

## Risks

| Risk | Mitigation |
|------|-----------|
| **R1 (HIGH): Telegram hashtag rejects hyphens** — `#t-hunted` renders as `#t` + plain text | Hashtag fixed as `#thunted` via SOURCE_HASHTAG_OVERRIDE (Decision 2). Regression test `test_t_hunted_teaser_uses_thunted_tag` locks the byte format. |
| **R2 (HIGH): default `_source_hashtag` returns `#blogspot`** — netloc parts[-2] logic | SOURCE_HASHTAG_OVERRIDE patch resolves source_name first, falls back to legacy logic only when no override (Decision 2). |
| **R3 (MED): Blogger image URL dedup** — `=s1600` vs `=s640` in path, not query | Strip `=s\d+(-c)?` size suffix before dedup key comparison (per code-research §B). Lamley's `?`-split logic is insufficient for Blogger; we replace it for this parser. |
| **R7 (MED): one of three deploy FILES lists forgotten** — `ImportError` on next cron tick, no CI signal | Deploy-plumbing task `Verify-smoke` runs `grep -l "t_hunted_source.py" deploy.sh .github/workflows/*.yml` — expects all three files in output. Pre-deploy QA double-checks. |
| **R8 (MED): PT-EN-RU glossary baseline guesswork** — `decalque` / `carrinho` flagged `[VERIFY operator]` | Operator refines post-deploy during 7-day quality watch; refinement ships as small follow-up PR. AC7 measures structural presence (≥10 entries), not specific terminology — non-blocking for delivery. |
| **R4 (LOW): long PT essay paragraphs >4000 chars** | Existing `_truncate_paragraphs` writes WARN and continues. Monitor first 10 publishes for `[truncated]` markers; raise cap if frequent. No code change for delivery. |
| **R5 (LOW): PT patterns cross-applied to EN/RU** | Length-bound 120 chars + `^`-anchored regex give the same false-positive protection as the 2026-05-08 RU defence-in-depth precedent (Decision 4). |
| **R6 (LOW): Blogger XML feedparser bozo flag** | Existing WARN-and-continue behaviour in `fetch_rss` — no change needed. |
| **Quality regression risk (MED): RU output worse than autoevolution baseline after 7-day watch** | Explicit abort path in user-spec: do not promote dev→main; iterate prompt/glossary as follow-up PR; feature stays on test instance until quality confirmed. Full disable option (drop from `feeds.json`) remains available throughout. |

## User-Spec Deviations

None.

All user-spec ACs are implemented as-is. Tech-spec decisions that go beyond user-spec wording are explicit choices that user-spec deferred to this phase:
- **Hashtag override mechanism (Decision 2)** — user-spec AC6 explicitly deferred the technique to tech-spec; we chose Option (b) SOURCE_HASHTAG_OVERRIDE map over Option (a) special-case-in-helper.
- **Glossary size 14 entries (Decision 5)** — exceeds AC7's ≥10 floor; not a deviation, just stronger satisfaction.
- **Brown circle 🟤 emoji (Decision 7)** — user-spec doesn't mention SOURCE_EMOJI; marked `[TECHNICAL]` decision (implementation hygiene, not derived from any user-spec requirement).

## Acceptance Criteria

Technical criteria supplementing user-spec ACs:

- [ ] All new + extended unit tests green (~16 new test methods across 5 test files)
- [ ] New integration smoke test `test_integration_t_hunted_smoke_en_pt_mixed_tick` green
- [ ] Full pytest suite passes (no regressions in 933+ existing tests)
- [ ] `grep -l "t_hunted_source.py" deploy.sh .github/workflows/deploy.yml .github/workflows/deploy_test.yml` returns all three files
- [ ] `python -c "import feedparser; print(len(feedparser.parse('https://t-hunted.blogspot.com/feeds/posts/default?alt=rss').entries))"` returns ≥1 entry, no `bozo` critical
- [ ] `python -c "from news_bot import _source_hashtag; print(_source_hashtag('https://t-hunted.blogspot.com/2026/05/post.html'))"` outputs `#thunted`
- [ ] `t_hunted_source.py` ≤170 LOC (parser blueprint estimate; soft check)
- [ ] No new packages in `requirements.txt`
- [ ] No new env vars in `.env.example`
- [ ] No SQLite schema migration in `pending_articles_repo.init_schema`
- [ ] CI green on PR
- [ ] After dev merge: `news_bot_test.service` restarts cleanly on the new code (no ImportError, no `[E031-E033]` alerts traceback to t-hunted parser in the first 24h)

## Implementation Tasks

### Wave 1 (foundation — parallel)

#### Task 1: New parser module + unit tests
- **Description:** Create `t_hunted_source.py` implementing `fetch_t_hunted_article(link, session, notifier) -> dict|None` mirroring lamley's contract, plus `_is_allowed_t_hunted_url` SSRF allowlist. HTML scrape of `<h1>`/`<div class="post-body entry-content">` flat-content with Blogger `=s\d+(-c)?` image dedup. Companion test file `tests/test_t_hunted_source.py` with TestFetch + TestHostAllowlist classes (~12 methods).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_t_hunted_source.py -v` → all green
- **Files to modify:** `t_hunted_source.py` (new), `tests/test_t_hunted_source.py` (new)
- **Files to read:** `lamley_source.py`, `tests/test_lamley_source.py`, `boilerplate_filter.py`, `work/t-hunted-pt-source/code-research.md` (§3, §B)

#### Task 2: Admin alerts E031-E033
- **Description:** Add three new builders to `admin_alerts.py` — `alert_t_hunted_host_rejected(link)`, `alert_t_hunted_fetch_error(link, error)`, `alert_t_hunted_no_body(link)` — following the existing E001-E030 pattern (Russian text, code prefix, link formatting). Extend `tests/test_admin_alerts.py` with positive cases for each builder.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_admin_alerts.py -k t_hunted -v` → 3 new tests green
- **Files to modify:** `admin_alerts.py`, `tests/test_admin_alerts.py`
- **Files to read:** `admin_alerts.py` (existing E001-E030 patterns), `work/t-hunted-pt-source/code-research.md` (§A.5)

### Wave 2 (wiring — parallel, depends on Wave 1)

#### Task 3: `news_bot.py` wiring (import + dispatcher + NETLOC_TO_SOURCE + SOURCE_HASHTAG_OVERRIDE)
- **Description:** Four atomic edits in `news_bot.py`: add `import t_hunted_source`; add `'t-hunted.blogspot.com': 't-hunted'` entry to `NETLOC_TO_SOURCE`; add new module-level `SOURCE_HASHTAG_OVERRIDE = {'t-hunted': 'thunted'}` constant and patch `_source_hashtag` to consult it; add new `elif 'blogspot.com' in domain:` branch in `fetch_full_article` calling `t_hunted_source.fetch_t_hunted_article`. Also add `SOURCE_EMOJI['t-hunted'] = '🟤'` and `SOURCE_LABEL['t-hunted'] = 'T-Hunted'` entries for archived hw_review consumer (cosmetic).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python -c "from news_bot import _source_hashtag, _resolve_source_name; print(_source_hashtag('https://t-hunted.blogspot.com/x'), _resolve_source_name('https://t-hunted.blogspot.com/x'))"` → `#thunted t-hunted`
- **Files to modify:** `news_bot.py`
- **Files to read:** `t_hunted_source.py` (from Task 1), `lamley_source.py` (signature reference), `work/t-hunted-pt-source/code-research.md` (§A.1, §F)

#### Task 4: Config + boilerplate (`feeds.json` + `boilerplate_filter.py`)
- **Description:** Append `"https://t-hunted.blogspot.com/feeds/posts/default?alt=rss"` to `feeds.json` (4/5 entries, under cap). Append PT defence-in-depth block (10 `^`-anchored regex patterns) to `_BOILERPLATE_PATTERNS` in `boilerplate_filter.py` after the existing RU block. Extend `tests/test_boilerplate_filter.py` with `TestIsBoilerplatePositive::test_portuguese_patterns_filtered` (parametrised over all 10 PT patterns) and `TestIsBoilerplateNegative::test_pt_lookalike_prose_kept`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_boilerplate_filter.py -k portuguese -v` → green; `python -c "import feedparser; print(len(feedparser.parse('https://t-hunted.blogspot.com/feeds/posts/default?alt=rss').entries))"` → ≥1
- **Files to modify:** `feeds.json`, `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`
- **Files to read:** `boilerplate_filter.py` (existing patterns), `tests/test_boilerplate_filter.py`, `work/t-hunted-pt-source/code-research.md` (§4.B)

#### Task 5: Wiring tests (`test_sources_registry.py` + `test_telegram.py`)
- **Description:** Extend `tests/test_sources_registry.py` — `NETLOC_TO_SOURCE` map test asserts new entry, `_resolve_source_name` test covers t-hunted netloc. Add `tests/test_telegram.py::test_t_hunted_teaser_uses_thunted_tag` that locks `#thunted #news` byte format on `send_telegraph_teaser` output.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_sources_registry.py tests/test_telegram.py -v` → all green
- **Files to modify:** `tests/test_sources_registry.py`, `tests/test_telegram.py`
- **Files to read:** existing test files, `news_bot.py` (post-Task-3), `work/t-hunted-pt-source/code-research.md` (§C)

#### Task 6: Deploy plumbing (3 FILES lists)
- **Description:** Add `"t_hunted_source.py"` to FILES arrays in `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` — same alphabetical position in each (after `orangetrack_source.py`, before `outage_state.py` per existing convention). Forgetting any one of the three triggers `ImportError` on next cron tick.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, deploy-reviewer
- **Verify-smoke:** `grep -l "t_hunted_source.py" deploy.sh .github/workflows/deploy.yml .github/workflows/deploy_test.yml` → all three files in output
- **Files to modify:** `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml`
- **Files to read:** all three files (existing FILES arrays), `work/t-hunted-pt-source/code-research.md` (§6)

### Wave 3 (prompt + integration — parallel)

#### Task 7: `ux-guidelines.md` prompt update (widen + t-hunted block + glossary)
- **Description:** Three discrete edits to `.claude/skills/project-knowledge/references/ux-guidelines.md`: (a) line 22 wording widen to accept PT input alongside EN; (b) insert `### 🟤 t-hunted` per-source style block after Mattel block — fields Voice / Tone dial / Length / Structure quirks / title-examples, all marked `[TBD operator]` for post-deploy refinement; (c) insert new `## Glossary — PT/EN/RU` section between Per-source notes and Red flags with 14 baseline entries from code-research §E. Preserve verbatim system-prompt blockquote at line 18-42 boundary per the file's contract (LLM reads it as role).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, prompt-reviewer
- **Files to modify:** `.claude/skills/project-knowledge/references/ux-guidelines.md`
- **Files to read:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `work/t-hunted-pt-source/code-research.md` (§D, §E, §5)

#### Task 8: Integration smoke test EN+PT mixed tick
- **Description:** Extend `tests/test_distributed_schedule_integration.py` with `test_integration_t_hunted_smoke_en_pt_mixed_tick` — mocks RSS feeds returning one autoevolution-style EN entry + one t-hunted-style PT entry, runs `news_bot.job()` once, asserts both rows enter `pending_articles` with correct `source_name`, both publish via `_fallback_publish` mock without cross-language interference, dedup is not over-eager, slot ordering preserved.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_distributed_schedule_integration.py -k integration_t_hunted -v` → green
- **Files to modify:** `tests/test_distributed_schedule_integration.py`
- **Files to read:** existing integration tests, `news_bot.py` (post-Task-3), `t_hunted_source.py` (from Task 1)

### Audit Wave

#### Task 9: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified in this feature (per Files-to-modify aggregation from Wave 1-3 tasks). Review holistically for cross-component issues, duplicate logic with lamley/orangetrack, architectural consistency, no shared-resource regressions. Write audit report. Issues found → lead spawns fixer agent.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 10: Security Audit
- **Description:** Full-feature security audit. Read all source files in this feature. Analyze SSRF allowlist hardness (no glob, exact match), no introduction of credentials/tokens, no unredacted log output, request timeout & size guards, no eval/exec, regex ReDoS safety in boilerplate PT patterns. OWASP Top 10 across all components. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 11: Test Audit
- **Description:** Full-feature test quality audit. Read all test files created/extended in this feature. Verify: meaningful assertions (not just `not None`), coverage of error paths (host rejected, fetch error, no body), no test interdependence, integration test asserts cross-language non-interference. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 12: Pre-deploy QA
- **Description:** Acceptance testing. Run full pytest suite (unit + integration), verify all user-spec ACs (1-10) + tech-spec Acceptance Criteria. Verify deploy FILES grep invariant. Verify RSS endpoint accessibility. Verify `news_bot._source_hashtag` emits `#thunted` on smoke check.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 13: Deploy
- **Description:** Standard project deploy via PR merge sequence. Squash-merge `feature/t-hunted-pt-source` → `dev` → `deploy_test.yml` SCPs files to `/home/hwbot/bot_test/` and restarts `news_bot_test.service`. Operator observes 5-10 first t-hunted publishes on `@myhwchannel123` over 2-7 days. On positive verification: merge `dev` → `main` → `deploy.yml` SCPs to `/home/hwbot/bot/` and restarts `news_bot.service`.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 14: Post-deploy verification
- **Description:** Live-environment verification on test instance (and later prod after dev→main promotion):
  - **First t-hunted publish lands on test channel** within 24-48h of merge — tool: Telegram client (operator visual)
  - **Preview card shows hero image** + `#thunted` hashtag rendered without truncation — tool: Telegram client (operator visual)
  - **Telegra.ph page renders RU translation** with no PT phrases leaked — tool: Telegram client + browser (operator visual)
  - **No `[E031-E033]` admin alerts** in first 24h, then 7-day watch — tool: `ssh hwbot 'journalctl -u news_bot_test.service --since "24 hours ago" | grep -E "\[E03[1-3]\]"'` returns empty
  - **RU quality ≥ autoevolution baseline** on 5-10 t-hunted samples (subjective) — tool: operator review of channel posts vs source articles
  - **No regression in existing sources** — tool: operator scan of channel for autoevolution/lamley/orangetrack posts in normal cadence
  Tools: bash + ssh (admin alerts grep), Telegram client (visual), browser (Telegra.ph page rendering).
- **Skill:** post-deploy-qa
- **Reviewers:** none
