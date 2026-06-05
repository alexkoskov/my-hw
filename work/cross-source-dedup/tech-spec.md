---
created: 2026-06-04
status: approved
branch: dev
size: L
---

# Tech Spec: Cross-source dedup

## Solution

Add a content-based duplicate detector that complements the existing URL-only dedup. For each fetched article, extract a model fingerprint (`{strict: [brand+model tokens], brands: [brand-only tokens]}`) via a stdlib `re` extractor backed by a ~35-brand lexicon, then compare against fingerprints of all articles persisted in the last 7 days. On ≥50% Jaccard overlap (hard-block) — drop the article, write the link to `processed_news`, send a quiet `[E015]` admin ping. On 30-49% (soft-flag) — let the article through and send a full `[E014]` admin ping with model context. The new gate lives between `_is_text_only_checklist` and `pending_repo.insert_pending` in `news_bot.job()`.

To close the cold-start window (7 days where new fingerprints have no historical fingerprints to compare against), a one-shot `backfill_fingerprints.py` script re-fetches the last 14 days of `published_articles` via `fetch_full_article` and writes computed fingerprints back into a newly-added `model_fingerprint TEXT` column. The script is idempotent (skips rows where the column is already non-NULL).

The dedup gate is wrapped in a top-level `try/except Exception` (AC9 degraded mode) — any failure in extraction, similarity computation, or repo queries lets the article pass through to `insert_pending` unchanged. A single rate-limited `[E016]` admin ping notifies the operator the first time degraded mode fires (once per hour).

## Architecture

### What we're building/modifying

- **`model_extractor.py` (NEW)** — pure module mirroring `boilerplate_filter.py` structure. Exports `extract_fingerprint(article: dict) -> dict[str, list[str]]` and `similarity(fp_a: dict, fp_b: dict) -> float`. Module-level compiled regex over the brand lexicon (~35 entries). ReDoS-safe (bounded quantifiers, anchored).
- **`backfill_fingerprints.py` (NEW)** — top-level one-shot CLI script (mirrors `hw_review.py` shape). `argparse` with `--days N` (default 14), `--dry-run`, `--verbose`. Walks `published_articles` rows missing fingerprint, re-fetches via `fetch_full_article`, writes back via `update_published_fingerprint`. Logs summary to stdout.
- **`pending_articles_repo.py` (MODIFY)** — add migration ALTER block for the new column on `pending_articles` + `published_articles`; add `model_fingerprint` to `_PENDING_JSON_COLS`; extend `insert_pending` to accept the new field (backward-compatible via `entry.get('model_fingerprint')`); add three new helpers — `list_recent_pending_fingerprints(conn, days=7)`, `list_recent_published_fingerprints(conn, days=7)`, `update_published_fingerprint(conn, link, fingerprint)`; add `bot_state`-backed helpers — `is_pair_rate_limited(conn, new_link, existing_link)`, `mark_pair_pinged(conn, new_link, existing_link)`, `is_dedup_degraded_rate_limited(conn)`, `mark_dedup_degraded_pinged(conn)`.
- **`admin_alerts.py` (MODIFY)** — three new builders: `alert_cross_source_dupe(new_link, existing_link, new_source, existing_source, overlap_pct, n_matches, n_total, models)` → E014 columnar full format; `alert_cross_source_blocked(new_link, existing_link, overlap_pct)` → E015 short 2-line format; `alert_dedup_degraded(reason)` → E016 short rate-limited alert for AC9 failures.
- **`news_bot.py` (MODIFY)** — new private helper `_check_cross_source_dedup(article, fingerprint, conn) -> ('block', match) | ('flag', match) | ('pass', None)`; wire into `job()` between line 1815 (post-`_is_text_only_checklist`) and line 1817 (pre-`row` dict assembly). Wrap entire dedup logic in `try/except Exception` with `'pass'` fallback (AC9).

### How it works

```
news_bot.job() fetch loop, per entry that survived earlier filters:
  1. fetch_full_article(entry) -> article (paragraphs + title + subtitle)
  2. _is_text_only_checklist(...)  ->  if True, continue (unchanged)
  3. NEW DEDUP GATE:
       try:
         fp = model_extractor.extract_fingerprint(article)
         decision, match = _check_cross_source_dedup(article, fp, conn)
         if decision == 'block':
           mark_processed(link, title, pub_date)
           send_admin_notification(alert_cross_source_blocked(link, match.link, pct))
           continue
         if decision == 'flag':
           if not pair_rate_limited(new=link, existing=match.link):
             send_admin_notification(alert_cross_source_dupe(...))
             mark_pair_pinged(new=link, existing=match.link)
       except Exception as exc:
         logger.exception("dedup gate failed, degraded mode active")
         if not degraded_recently():
           send_admin_notification(alert_dedup_degraded(reason=type(exc).__name__))
           mark_degraded_pinged()
         # fall through with fp = None  -> article publishes unchanged

  4. row = { ..., 'model_fingerprint': fp or None, ... }
  5. pending_repo.insert_pending(row)  ->  inserted += 1
```

`_check_cross_source_dedup` reads `list_recent_pending_fingerprints(conn, 7)` + `list_recent_published_fingerprints(conn, 7)`, computes `similarity()` against each, returns the best match if it crosses either threshold:

- ≥0.50 → `('block', match)`
- 0.30-0.49 → `('flag', match)`
- <0.30 → `('pass', None)`

`backfill_fingerprints.py` operator flow (run once after first deploy):
```
$ python3 backfill_fingerprints.py
2026-06-XX 10:00:00 INFO Scanning published_articles WHERE published_at >= ('-14 days') AND model_fingerprint IS NULL
2026-06-XX 10:00:01 INFO 47 rows to backfill
...progress...
2026-06-XX 10:04:23 INFO Backfill complete:
  Window:    14 days (228 rows scanned)
  Processed: 47 (computed fingerprint)
  Skipped:   181 (already had fingerprint)
  Empty fp:  3 (industry news, no brands found)
  Errors:    0
  Duration:  4m 23s
```

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| Compiled brand regex `_BRAND_RE` | `model_extractor.py` module load | `extract_fingerprint` | 1 (singleton, module-level) |
| Brand lexicon `_LEXICON` (frozenset of canonical brand keys) | `model_extractor.py` module load | `extract_fingerprint`, `similarity` | 1 (immutable) |
| `bot_state` k/v table | `pending_articles_repo.init_schema` (existing) | rate-limit helpers, degraded-mode tracker | 1 (existing) |

## Decisions

### Decision 1: Module structure — `model_extractor.py` top-level
**Decision:** Create `model_extractor.py` at repo root, mirroring `boilerplate_filter.py` shape (module docstring → compiled regex constants → pure helper functions, no I/O).
**Rationale:** Supports user-spec AC1, AC2. Matches existing one-shot-filter convention; no new directories introduced. Top-level placement makes `from model_extractor import extract_fingerprint` work from `news_bot.py` without sys.path tricks. Deploy bundle (`deploy.sh`) already picks up top-level `*.py`.
**Alternatives considered:** Sub-package `dedup/`. Rejected — only 1 module, overkill. `lib/` folder. Rejected — no `lib/` convention in repo.

### Decision 2: Lexicon size — 35 brand entries, tiered by frequency
**Decision:** Hardcode a 35-entry brand list in `_LEXICON` at module level. Tier 1 (Japanese — Toyota, Honda, Nissan, Mazda, Subaru, Mitsubishi, Datsun, Lexus, Acura), Tier 2 (American muscle — Ford, Chevrolet, Dodge, Plymouth, Pontiac, Buick, Cadillac, Chrysler, AMC, Jeep), Tier 3 (European — BMW, Mercedes, Audi, Porsche, Volkswagen, Volvo, Land Rover, Range Rover, Mini, Aston Martin, Lotus), Tier 4 (exotics — Ferrari, Lamborghini, Bugatti, Koenigsegg, McLaren, Pagani). Lexicon stored as tuples of `(canonical_key, regex_alternation_pattern)` to handle aliases (`Chevy|Chevrolet`, `VW|Volkswagen`, `Mercedes(?:-Benz)?`).
**Rationale:** Supports user-spec AC1. Code-research §14.A.2 derived this list from sampling all source fixtures. Covers ~95% of typical HW catalog content. Tier-by-frequency ordering allows future maintainers to see where coverage is densest.
**Alternatives considered:** Externalised JSON/YAML lexicon. Rejected — adds a file I/O dependency to a module-load step; no operational benefit for ~35 lines of data. Open-ended user-extensible config. Rejected — overengineering for a stable list that changes maybe once per quarter.

### Decision 3: Regex patterns — ReDoS-safe bounded quantifiers
**Decision:** Each brand-model regex follows the shape:
```
(?:(?:19|20)\d{2}\s{1,2})?           # optional year prefix, dropped
(?P<brand>Toyota|Honda|...|Land\s{1,2}Rover|...)
\s{1,2}(?P<model>[A-Z][a-zA-Z0-9-]{0,24})
(?:\s{1,2}[A-Z][a-zA-Z0-9-]{0,24}){0,2}   # up to 2 more model words
```
All quantifiers bounded (`{1,2}`, `{0,24}`, `{0,2}`); no nested unbounded greedy quantifiers. Brand alternation uses `re.compile` once at module load. Case-sensitive for brands that have prose false-match risk (`BMW`, `AMC`, `Lotus`); case-insensitive for unambiguous brands. Compiled into a single combined regex via alternation for one-pass scanning.
**Rationale:** Supports user-spec AC1. ReDoS-safe (user-spec Risk 6 mitigation). Bounded model length avoids gluttonous matches. The case-sensitivity split (per code-research §14.A.3) prevents `Lotus` matching prose `lotus`, `BMW` matching `bmwxyz123`.
**Alternatives considered:** Single uppercase regex for all brands. Rejected — `BMW` false-matches lowercase prose. NLP NER (e.g. spaCy). Rejected — heavyweight dep, slower, overkill for ~35 named entities.

### Decision 4: Similarity formula — guarded two-level Jaccard
**Decision:** `similarity(a, b)` computes:
```python
strict_a, strict_b = a['strict'], b['strict']
brands_a, brands_b = a['brands'], b['brands']

if not strict_a or not strict_b:           # AC6 — empty fp returns 0; caller skips dedup
    return 0.0
if len(strict_a) <= 1 or len(strict_b) <= 1:  # AC8 — too small for brands fallback
    return jaccard(strict_a, strict_b)
if len(brands_a) < 2 or len(brands_b) < 2:    # AC8 — brand fallback needs ≥2 brands each
    return jaccard(strict_a, strict_b)
return max(jaccard(strict_a, strict_b), jaccard(brands_a, brands_b))
```
**Rationale:** Supports user-spec AC6 (empty fp guard), AC8 (1-token + brand-fallback guard), AC10 (two-level granularity). Code-research §14.C worked through corner cases — two Subaru-only articles (1 brand each) → 1.0 brand match would be a false block; this formula returns the strict Jaccard instead (likely 0.0 unless they share the exact model).
**Alternatives considered:** Cosine on TF-IDF vectors. Rejected — needs corpus statistics, no clear win. Token-level edit distance. Rejected — solves a problem we don't have (PT-EN model names are identical English tokens).

### Decision 5: Storage — `model_fingerprint TEXT` JSON column on both tables
**Decision:** Add a single `model_fingerprint TEXT` column to `pending_articles` and `published_articles`. Encoded as JSON via existing `_dumps()` helper (matches `paragraphs`, `images`, `blocks`, `ru_paragraphs`, `ru_blocks` convention — `ensure_ascii=False`). Schema migration via `try/except sqlite3.OperationalError` in `init_schema`, extending the existing migration block at `pending_articles_repo.py:185-194`.

Column value shape: `{"strict": ["toyota 4runner", "subaru legacy gt"], "brands": ["toyota", "subaru"]}` — JSON object with two list keys. Empty fingerprint stored as `{"strict": [], "brands": []}` (NOT NULL, distinct from "not yet processed" = NULL).
**Rationale:** Supports user-spec AC1, AC2, AC11 (storage). Single-column-per-table change is the smallest viable migration. Matches existing JSON-column convention. NULL vs `{"strict":[],"brands":[]}` distinction supports backfill idempotency.
**Alternatives considered:** Separate `article_fingerprints` table with `(link PK, models JSON)`. Rejected — extra join, +1 zombie-state risk, no operational benefit. Two columns (`strict_fp TEXT`, `brands_fp TEXT`). Rejected — one less JSON write doesn't outweigh schema doubling.

### Decision 6: Rate-limit storage — `bot_state` k/v with compound key
**Decision:** Use the existing `bot_state` k/v table for soft-flag rate-limit (user-spec AC5). Key shape: `softflag_pair:{new_link}\n{existing_link}` (newline separator — links contain `:` and `/`). Value: ISO-8601 timestamp of last ping. Helpers in `pending_articles_repo.py` — `is_pair_rate_limited(conn, new, existing, window_days=7)` reads + parses, `mark_pair_pinged(conn, new, existing)` writes. Similar pair: `dedup_degraded_last_pinged_at` for AC9 degraded-mode rate-limit (window: 1 hour).
**Rationale:** Supports user-spec AC5, AC9. Volume is trivial (~150-300 keys over 6 months at current article rates). Existing `bot_state` infrastructure is exactly designed for this (small k/v cross-tick state — see outage_state.py pattern at code-research §14.D). Avoids a third table.
**Alternatives considered:** New `softflag_pings(new_link, existing_link, last_pinged_at)` table with composite PK. Rejected — adds DDL surface, schema-pin tests, ALTER migration to track. New file `dedup_state.py` mirroring `outage_state.py`. Rejected — premature abstraction for ~30 lines of helpers; live with the helpers in the repo module for now.

### Decision 7: Three new alert codes — E014, E015, E016
**Decision:** Add three builder functions to `admin_alerts.py`, one per event class:
- **E014 — `[E014] 🤔 Похож на дубль`** (soft-flag) — full columnar format with both links, source names, percentage, model list, "what happened", "what to do". Mirrors E006 verbosity. **Per-pair rate-limited to 1× / 7 days** (user-spec AC5).
- **E015 — `[E015] 🚫 Заблокирован дубль`** (hard-block visibility) — short 2-3 line format: code + emoji + new_link + existing_link + percentage. No "what to do" — operator intervention is optional. Not rate-limited (each block is distinct news).
- **E016 — `[E016] ⚠️ Дедуп в degraded mode`** (AC9 fallback notification) — short alert with exception class name, "what happened" (silent fallthrough — article published as usual). **Globally rate-limited to 1× / 1 hour** to prevent ping flood if extractor regression hits every article.
**Rationale:** Supports user-spec AC3, AC4, AC9. Distinct event classes deserve distinct codes per the existing `admin_alerts.py` convention (E001-E033). Code numbers continue the platform sequence (E014, E015, E016 are unclaimed per code-research §3.2).
**Alternatives considered:** Single E014 with severity field. Rejected — code numbers are the existing operator-visible grep handle; splitting events into separate codes aids triage. Use `logger.info` instead of ping for E015. Rejected — operator confirmed in user-spec Решения they want visibility on hard-blocks too.

### Decision 8: Hard-block path calls `mark_processed` (divergent from checklist filter)
**Decision:** When dedup decides `'block'`, call `news_bot.mark_processed(link, title, pub_date)` immediately before `continue`. The link is inserted into `processed_news` so that next tick's `is_processed(link)` filter at `news_bot.py:584-591` skips re-fetching this URL forever.
**Rationale:** Supports user-spec AC3. Divergent from `_is_text_only_checklist` pattern, which intentionally allows re-fetch (its rejection reason — empty body — can change between ticks; ours can't). Without this, every tick re-fetches the same dupe (network cost) and re-runs extract+compare (CPU cost), producing identical drop decisions.
**Alternatives considered:** Let re-fetch happen, just keep dropping. Rejected — wasteful, no benefit. Move to a separate `dropped_articles` table. Rejected — `processed_news` semantics already cover "seen, will never publish again".

### Decision 9: Comparison NOT filtered by source_name
**Decision:** `list_recent_*_fingerprints` returns all rows regardless of source. Similarity comparison runs against the full window — t-hunted vs autoevolution AND autoevolution vs autoevolution (same source, different week's roundup).
**Rationale:** Supports user-spec AC7. Within-source republishes (autoevolution running two Boulevard Mix articles a week apart with overlapping models) are legitimate dedup targets. The existing pending-table-existence filter on the fetch loop already prevents same-link self-match (URL guard fires before the new dedup gate).
**Alternatives considered:** Filter `WHERE source_name != current_source`. Rejected — misses within-source dupes; user-spec confirmed in Решения.

### Decision 10: Backfill via URL re-fetch (Path A from clarification)
**Decision:** `backfill_fingerprints.py` calls `news_bot.fetch_full_article(entry_stub)` per published row (within the `--days N` window) to re-derive `paragraphs`, then computes fingerprint and writes back. **`published_articles` does not store `paragraphs`** — verified in code-research §14.E.3; this is the only way to back-derive without a schema change.

Failure modes:
- Fetch returns None or empty paragraphs but no exception (article deleted, source down) → store `{"strict": [], "brands": []}` (computed-empty is a valid terminal state for a permanently gone article); count toward summary as `Empty fp`.
- Fetch raises (Cloudflare 403, network timeout, ConnectionError, etc.) → **leave `model_fingerprint` NULL** (NULL = "not attempted or transient fail, retry later"); count toward summary `Errors`. Operator re-runs the script after Cloudflare cooldown to retry NULL rows.
- Rate limit defense (Cloudflare) → 1-second sleep between fetches (~50 articles × ~3 s/fetch + sleep = ~2.5 min total).

**Idempotency contract:** `WHERE model_fingerprint IS NULL` is the canonical "needs backfill" marker. Rows with `'{"strict":[],"brands":[]}'` are NOT re-processed (computed-empty is terminal). Rows with non-empty fingerprint are NOT re-processed.

`--days N` is clamped to `[1, 90]` in argparse to defend against operator typos.
**Rationale:** Supports user-spec AC11. **DEVIATION from user-spec** — user-spec AC11 assumed reading paragraphs from `published_articles`; that column doesn't exist. User approved Path A in tech-spec clarification phase (2026-06-04).
**Alternatives considered:** Add `paragraphs` column to `published_articles` going forward. Rejected — doesn't fix cold start (historical rows still empty), so we'd still need Path A; +1 schema migration to maintain; +13 MB/year DB size for no operational gain. Drop backfill entirely. Rejected — leaves the 7-day window cold-start gap that motivated the feature. Store sentinel on fetch failure to prevent retry. Rejected — transient Cloudflare 403 should not permanently poison the row; NULL preserves the retry opportunity.

### Decision 11: Auto-migration via `try/except sqlite3.OperationalError`
**Decision:** Add 2 lines to `pending_articles_repo.init_schema()` migration block (`pending_articles_repo.py:185-194`):
```python
for ddl in (
    "ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT",
    "ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT",
):
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError:
        pass
```
**Rationale:** Supports user-spec AC10. Reuses the exact proven pattern from the `telegraph_url` migration (2026-04-30). Idempotent on re-run (SQLite's `OperationalError` fires only on duplicate-column attempt, not on real errors).
**Alternatives considered:** Versioned migration framework (alembic, yoyo). Rejected — overkill for the project's pace (2 migrations in 2 years); CLAUDE.md and patterns.md explicitly favor inline `try/except` style.

### Decision 12: Fallback degraded mode wraps the entire dedup gate
**Decision:** The `try/except Exception` wraps everything from `extract_fingerprint(article)` through the decision dispatch. On exception: log `logger.exception(...)`, send rate-limited E016, set `fp = None`, fall through to `row` assembly with `model_fingerprint=None`. The article publishes as if dedup didn't exist.
**Rationale:** Supports user-spec AC9. Single broad except is intentional — we don't know upfront which sub-call can throw (regex compile error, repo SQL, exotic article shape). The contract is "dedup never blocks publishing" — a single broad handler enforces it.
**Alternatives considered:** Narrow except per call-site. Rejected — invites future bugs sneaking past per-site handlers (e.g. a new helper added later without try/except). Re-raise on `KeyboardInterrupt`/`SystemExit`. Implicit — Python's `except Exception` doesn't catch those.

### Decision 13: Calibration test runs as a regular pytest, not a manual step
**Decision:** `tests/test_model_extractor.py::test_calibration_accuracy` loads `tests/fixtures/cross_source_dedup_pairs.py` (8 labelled pairs from code-research §14.F), runs `extract_fingerprint` + `similarity` on each, asserts ≥7/8 correct classifications (≥87.5% accuracy floor; user-spec AC12 target is ≥95% but the floor is the gating threshold). Runs in CI on every PR.

**Must-pass split:** the 2026-06-03 real-pair fixture (Pair 1 — t-hunted PT ↔ autoevolution EN Car Culture Road Trip Mix) MUST classify correctly — a separate `test_calibration_real_pair_must_pass` asserts that this single pair is labelled `'block'`. The overall ≥7/8 threshold guards against drift; the must-pass guards against masking the load-bearing example with easy synthetic pairs.
**Rationale:** Supports user-spec AC12. Tying calibration to CI catches threshold regressions automatically. Removes "operator must run a manual calibration step before deploy" — operator already has enough on the QA list. Must-pass split addresses test-reviewer F-CAL-MASK finding.
**Alternatives considered:** Manual pre-deploy script. Rejected — humans skip manual steps; CI doesn't. Single accuracy test without must-pass. Rejected — 1 misclassification budget could absorb the only pair that motivated the feature.

### Decision 14: Dedup gate position in `news_bot.job()` — last filter before persist
**Decision:** The dedup gate runs as the LAST filter in the fetch loop — immediately after `_is_text_only_checklist` and immediately before the `row = {...}` assembly that feeds `pending_repo.insert_pending`. Three checks fire BEFORE the gate (earlier and cheaper): URL-equality `is_processed(link)`, sibling-brand title filter, pending-table existence check (all pre-fetch), then `_is_text_only_checklist` post-fetch. The new gate is post-fetch and the most expensive (extract + N similarity calls + 2 SQL reads).
**Rationale:** Supports user-spec AC1 (per-article fingerprint), AC3 (hard-block before insert), AC4 (soft-flag before insert). Three concrete reasons for "last filter" placement:
1. **Body required** — fingerprint extraction needs `article.paragraphs`, available only after `fetch_full_article` (lines 1801-1802).
2. **Cost ordering** — cheaper filters run first. The dedup gate is the most expensive of the post-fetch filters; placing it last avoids paying its cost on articles already rejected by the cheaper checklist filter.
3. **Symmetry with insert path** — `row` is assembled immediately after this gate, and the gate decides whether `row` is built at all (hard-block) or built with `model_fingerprint` populated (pass/flag). Pre-row-assembly is the natural seam.
**Alternatives considered:** Run before checklist filter. Rejected — wastes extract cost on checklist drops. Run inside `insert_pending` as a pre-insert callback. Rejected — couples repo to extractor, violates separation of concerns (repo doesn't know about brands). Run as a separate background job. Rejected — race conditions, doesn't prevent the duplicate publish that's the whole point.

## Data Models

### Schema changes

```sql
-- ALTER applied via try/except in pending_articles_repo.init_schema()
ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT;
ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT;
```

Column value (JSON, ensure_ascii=False):
```json
{
  "strict": ["toyota 4runner", "subaru legacy gt", "land rover s2"],
  "brands": ["toyota", "subaru", "land rover"]
}
```

NULL vs `{"strict":[],"brands":[]}`:
- **NULL** — row pre-dates feature OR backfill hasn't touched it yet. Idempotency marker for backfill script.
- **`{"strict":[],"brands":[]}`** — extractor ran, article has no recognized brands (industry news, retrospective).

### `bot_state` keys (existing table, new key prefixes)

| Key shape | Value | Purpose |
|-----------|-------|---------|
| `softflag_pair:{new_link}\n{existing_link}` | ISO-8601 timestamp | Per-pair soft-flag rate-limit, 7-day window (AC5) |
| `dedup_degraded_last_pinged_at` | ISO-8601 timestamp | Global E016 rate-limit, 1-hour window (AC9) |

### Interfaces

```python
# model_extractor.py
Fingerprint = dict[str, list[str]]  # {'strict': [...], 'brands': [...]}

def extract_fingerprint(article: dict) -> Fingerprint: ...
def similarity(a: Fingerprint, b: Fingerprint) -> float: ...

# pending_articles_repo.py (new helpers)
def list_recent_pending_fingerprints(conn, days: int = 7) -> list[dict]: ...
def list_recent_published_fingerprints(conn, days: int = 7) -> list[dict]: ...
def update_published_fingerprint(conn, link: str, fingerprint: Fingerprint) -> None: ...
def is_pair_rate_limited(conn, new_link: str, existing_link: str, window_days: int = 7) -> bool: ...
def mark_pair_pinged(conn, new_link: str, existing_link: str) -> None: ...
def is_dedup_degraded_rate_limited(conn, window_hours: int = 1) -> bool: ...
def mark_dedup_degraded_pinged(conn) -> None: ...

# admin_alerts.py
def alert_cross_source_dupe(new_link, existing_link, new_source, existing_source,
                             overlap_pct: int, n_matches: int, n_total: int,
                             models: list[str]) -> str: ...  # E014
def alert_cross_source_blocked(new_link, existing_link, overlap_pct: int) -> str: ...  # E015
def alert_dedup_degraded(reason: str) -> str: ...  # E016
```

## Dependencies

### New packages

None. Stdlib `re` + `json` + `set` + `datetime` only.

### Using existing (from project)

- `pending_articles_repo._dumps` — JSON serialization with `ensure_ascii=False`.
- `pending_articles_repo._row_to_dict` — auto-deserialization of JSON columns (extended via `_PENDING_JSON_COLS`).
- `news_bot.fetch_full_article` — used by backfill script to re-fetch HTML.
- `news_bot.mark_processed` — hard-block path writes link to `processed_news`.
- `news_bot.send_admin_notification` — Telegram delivery for E014/E015/E016.
- `news_bot._resolve_source_name` — derives `source_name` from URL in alert builders.
- `bot_state` schema and lifecycle from `outage_state.py` — pattern for state helpers.

## Testing Strategy

**Feature size:** L (new module + backfill script + schema migration + 7 test files + 3 new alert codes).

### Unit tests

- `tests/test_model_extractor.py::TestExtractFingerprint` — ~10 scenarios:
  - Single brand+model: «2018 Toyota 4Runner» → `{'strict': ['toyota 4runner'], 'brands': ['toyota']}`.
  - Multi-word brand: «Land Rover S2 (BP)» → `{'strict': ['land rover s2'], 'brands': ['land rover']}`.
  - Brand alias: «Chevy Camaro» → `{'strict': ['chevrolet camaro'], 'brands': ['chevrolet']}`.
  - Empty body: returns `{'strict': [], 'brands': []}`.
  - PT-text with EN brand+model: t-hunted body fixture → expected fingerprint.
  - Mixed brands in single body: all extracted.
  - Year prefix dropped: «2018 Toyota 4Runner» strict token does NOT contain "2018".
  - False-positive guard: «bmwxyz» does NOT extract `BMW`.
  - Long body structural bound: `extract_fingerprint` on 10KB synthetic body completes without hang (qualitative — no wall-clock assertion to avoid CI flakiness; ReDoS-safety covered by bounded-quantifier review in Decision 3).
- `tests/test_model_extractor.py::TestSimilarity` — 8 scenarios:
  - Empty fp on either side → 0.0 (AC6 path).
  - Full overlap → 1.0.
  - Partial strict, no brand fallback (≥2 brands each side) → max applied correctly.
  - 1-strict-token both sides → brands fallback NOT applied (AC8).
  - 1-strict, 1-brand both sides → returns strict jaccard only.
  - 2-strict, 1-brand both sides → brands fallback NOT applied (AC8 brand-count guard boundary).
  - Exactly at threshold 0.50 (e.g. 2/4 overlap) → classified as block.
  - Exactly at threshold 0.30 (e.g. 3/10 overlap) → classified as flag.
- `tests/test_model_extractor.py::test_calibration_accuracy` — 8-pair fixture, ≥7/8 correct (Decision 13 floor).
- `tests/test_model_extractor.py::test_calibration_real_pair_must_pass` — Pair 1 (real 2026-06-03 t-hunted↔autoevolution) MUST classify as `block` (Decision 13 must-pass split — protects load-bearing example from being absorbed by the 1-misclassification budget).
- `tests/fixtures/cross_source_dedup_pairs.py` — fixture content per Task 1 description AND Testing Strategy mirror: 4 duplicate pairs (Pair 1 = real 2026-06-03, Pairs 2-4 synthetic), 4 non-duplicate pairs (Pair 5 = same brand different models for AC8 probe, Pair 6 = 1-token both sides for guard, Pair 7 = different series same brand, Pair 8 = industry news vs car review).
- `tests/test_pending_articles_repo.py` — updated `EXPECTED_PENDING` (adds `model_fingerprint`); new tests for `list_recent_*_fingerprints` (seed N rows split inside/outside the 7-day window, assert only the inside-window subset is returned — structural replacement for system-level perf assertion), `update_published_fingerprint`, rate-limit helpers (including E016 window-expiry 3-step: mark → check rate-limited within 1h → check NOT rate-limited after fast-forward >1h); JSON-column roundtrip test (`insert_pending` writes a dict, `get_pending` returns a dict — pins `_PENDING_JSON_COLS` correctness against typos).
- `tests/test_migration.py` — updated `EXPECTED_PENDING_COLUMNS` and `EXPECTED_PUBLISHED_COLUMNS`; new test for double-init idempotency.
- `tests/test_admin_alerts.py` — three new tests: `test_e014_cross_source_dupe`, `test_e015_cross_source_blocked`, `test_e016_dedup_degraded`. Each asserts code prefix, severity emoji, key substrings.
- `tests/test_backfill_fingerprints.py` — idempotency (no double-write on second run), dry-run mode (no writes), summary log shape, error counting on fetch failures.

### Integration tests

- `tests/test_integration.py::TestCrossSourceDedup` — 7 scenarios (using `_PrepPhaseBase` mock):
  - **Hard-block path:** two mock articles with 100% model overlap → second NOT inserted into pending; second link added to `processed_news` via `mark_processed` (pins Decision 8 semantic); E015 ping fired.
  - **Pass-through with non-empty fp:** new article with non-empty fingerprint, no match found in the 7-day window → inserted into pending with the fingerprint populated, no pings fired. This is the dominant production case — protects against a future regression where "no match" accidentally pings or blocks.
  - **Soft-flag path:** two mock articles with 35% overlap → both inserted, E014 ping fired once.
  - **Soft-flag rate-limit:** same pair repeats within 7 days → second E014 NOT fired.
  - **Within-source dedup (AC7):** two articles from SAME source_name (e.g. both autoevolution) with 100% overlap → second blocked (pins Decision 9 no-source-filter against future "optimization").
  - **Empty fingerprint:** article with body containing no brands → passes through, fingerprint `{"strict":[],"brands":[]}` stored in pending, no comparison done.
  - **Degraded mode:** `extract_fingerprint` patched to raise → article publishes anyway, fingerprint stored as NULL, E016 ping fired once (rate-limited).
- `tests/test_integration.py::TestFingerprintCarryThrough` — separate test (AC2): publish a pending row with non-empty `model_fingerprint` via `move_to_published`, assert `published_articles.model_fingerprint` matches input (pins AC2 carry-through).

### E2E tests

None. Manual operator-side verification (admin pings spot-check + 2-week channel review) per user-spec covers E2E. CI pytest covers the seams between modules.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Per-task smoke checks (Verify-smoke fields) execute during implementation. They run real commands against the local repo — no deployment needed. The integration test suite (Wave 2, Task 4) is the strongest pre-deploy smoke: it exercises the actual `news_bot.job()` loop with all gates wired.

Post-deploy verification (Final Wave) is operator-driven: operator runs `backfill_fingerprints.py` on prod, watches for E015 pings the first 2 weeks, spot-checks E014 pings as they arrive.

### Tools required

- `bash` + `pytest` (full test suite, targeted runs).
- `python3` for `python -c` smoke checks (extractor on synthetic input).
- `sqlite3` CLI (operator-side, to verify ALTER applied on prod DB after first tick).
- No MCP tools needed (Telegram channel spot-check is operator-visual; bot logs are journalctl on VPS, operator-handled).

## Risks

| Risk | Mitigation |
|------|-----------|
| Backfill re-fetch hits Cloudflare 403 on multiple autoevolution rows | Backfill counts errors per row, continues. Operator can re-run after Cloudflare cooldown. Empty fp `'[]'` stored for retry on next run. |
| Lexicon misses a hot 2026-2027 brand (e.g. new EV maker) | extraction returns empty for that brand's articles → no dedup applied. Detect via monthly review of archive for missed dupes. Add brand to `_LEXICON`, redeploy. |
| Brand alias collision (e.g. `Lotus` matching prose mention of lotus flower) | Code-research §14.A.3 documented case-sensitivity split; `BMW`/`AMC`/`Lotus` use case-sensitive match. Calibration fixture (1-token guard probe) catches regression. |
| Degraded mode hides a real extractor bug | E016 ping fires (rate-limited) on first failure. Operator sees pattern in admin chat, debugs. CI test for E016 firing on exception. |
| ALTER TABLE pushes DB lock under concurrent cron tick + backfill | ALTER on SQLite is atomic-ish; backfill is operator-initiated AFTER the first cron tick completes init_schema migration. Operationally separated. |
| Soft-flag E014 noise (lamley weekly roundups) | AC5 per-pair rate-limit 7 days. Hard cap on operator visibility per pair. |
| Calibration fixture loses calibration as lexicon grows | Fixture file is git-tracked and reviewed in PRs. CI test `test_calibration_accuracy` catches accuracy drift on every commit. |

## User-Spec Deviations

- **User-spec AC11 (Backfill from `published_articles.paragraphs`):** user-spec assumed paragraphs are stored in `published_articles`. Verified absent (code-research §14.E.3). Tech-spec switches backfill to URL re-fetch via `fetch_full_article` (see Decision 10). User approved Path A in tech-spec clarification phase. → **[APPROVED 2026-06-04]**

- **Added: E015 quiet ping on hard-block** (extends user-spec AC3). User-spec AC3 says hard-block logs INFO + operator gets E015 ping. Tech-spec keeps both — INFO log AND a separate 2-3 line `[E015]` ping (no `Что сделать` block since operator action is optional). Reason: code separation (logger.info goes to stdout/journalctl; E015 ping goes to operator Telegram for live awareness). → **[APPROVED via user-spec Решения section]**

- **Added: E016 rate-limited degraded-mode ping** (extends user-spec AC9). User-spec AC9 says "log ERROR with traceback". Tech-spec adds a rate-limited Telegram ping (1× / 1 hour) so operator sees the regression without watching logs. Reason: AC9 visibility requirement (operator can't read VPS logs in real-time). → **[APPROVED via user-spec Решения — operator visibility on every dedup branch]**

- **Refined: Fingerprint shape is dict, not list** (refines user-spec AC1). User-spec AC1 says "JSON list[str]". Tech-spec uses a JSON object `{"strict": [...], "brands": [...]}` to encode the two-level granularity from AC9 (token shape) more naturally. Round-trip semantics preserved (operator can inspect either set). → **[APPROVED — operator confirmed AC8/AC9/AC10 intent during user-spec phase]**

- **Refined: Backfill test scenario "malformed paragraphs JSON" removed** (refines user-spec AC11 test list). User-spec listed `обработка malformed paragraphs JSON` as a backfill test scenario, predicated on reading `published_articles.paragraphs` from DB. Per Decision 10 (Path A) backfill re-fetches paragraphs via `fetch_full_article` and never reads `paragraphs` from `published_articles` (column doesn't exist). The malformed-JSON path is now unreachable. Replaced with: `fetch raises → leaves NULL` and `fetch returns empty → stores computed-empty` (Task 5 test list). → **[APPROVED via Decision 10 reversal — implicit consequence]**

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] Все ALTER TABLE миграции идемпотентны при повторном вызове `init_schema()` (через `try/except sqlite3.OperationalError`).
- [ ] Schema-pin тесты обновлены: `tests/test_pending_articles_repo.py::EXPECTED_PENDING` содержит `model_fingerprint`; `tests/test_migration.py::EXPECTED_PENDING_COLUMNS` и `EXPECTED_PUBLISHED_COLUMNS` обновлены.
- [ ] `pytest -q` зелёный включая все новые тесты (test_model_extractor, test_backfill_fingerprints, новые в test_admin_alerts, test_integration::TestCrossSourceDedup, обновлённые test_migration и test_pending_articles_repo).
- [ ] Нет регрессий в существующих тестах (1012+ tests at HEAD pass).
- [ ] Performance: `extract_fingerprint` на 10KB body завершается <10ms; полный dedup-этап (extract + 50 similarity calls + repo query) <100ms per article.
- [ ] Без новых внешних зависимостей в `requirements.txt`.
- [ ] Backfill script idempotent: повторный запуск на той же БД пропускает уже обработанные строки и возвращает summary с `Processed: 0`.
- [ ] E016 rate-limit работает: при последовательных вызовах в течение часа ping отправляется ровно один раз.
- [ ] Migration block переживает `init_db()` дважды без warning/error в логах.
- [ ] `deploy.sh` FILES list содержит оба новых top-level файла (`model_extractor.py`, `backfill_fingerprints.py`); без них прод упадёт с `ImportError` на первом тике после деплоя.
- [ ] `backfill_fingerprints.py` импортирует `news_bot` ПЕРЕД `logging.basicConfig()` — иначе `_TokenRedactingFilter` не прикрепится к root-логгеру (security-review caveat 5).

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: model_extractor.py + calibration fixture
- **Description:** Create new `model_extractor.py` module with brand lexicon, ReDoS-safe compiled regex, `extract_fingerprint` and `similarity` per Decisions 2-4. Add `tests/fixtures/cross_source_dedup_pairs.py` with 8 labelled pairs (real 2026-06-03 pair + AC8 probes + 1-token guard per code-research §14.F). Add `tests/test_model_extractor.py` with `TestExtractFingerprint`, `TestSimilarity`, `test_calibration_accuracy` (≥7/8 floor), and `test_calibration_real_pair_must_pass` (Decision 13 must-pass split).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "from model_extractor import extract_fingerprint; print(extract_fingerprint({'title':'2018 Toyota 4Runner gold chase', 'subtitle':'', 'paragraphs':['Subaru Legacy GT (BP).']}))"` → output contains `toyota 4runner`, `subaru legacy gt` in strict; `toyota`, `subaru` in brands.
- **Files to modify:** none (all new)
- **Files to read:** `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`, `tests/test_autoevolution_source.py`, `tests/test_t_hunted_source.py`, `work/cross-source-dedup/code-research.md` (§14.A-C, §14.F)

#### Task 2: Schema migration + repo helpers + rate-limit helpers
- **Description:** Extend `pending_articles_repo.py` `init_schema` migration block with 2 ALTER TABLEs (`model_fingerprint TEXT` on pending + published) per Decision 11. Add `model_fingerprint` to `_PENDING_JSON_COLS`. Extend `insert_pending` to accept the new field via `entry.get('model_fingerprint')` (backward-compat). Add 3 new query/write helpers (`list_recent_pending_fingerprints`, `list_recent_published_fingerprints`, `update_published_fingerprint`) and 4 bot_state-backed rate-limit helpers per Decision 6 (`is_pair_rate_limited`, `mark_pair_pinged`, `is_dedup_degraded_rate_limited`, `mark_dedup_degraded_pinged`). Update schema-pin tests (`EXPECTED_PENDING` in `tests/test_pending_articles_repo.py`; `EXPECTED_PUBLISHED` there; `EXPECTED_PENDING_COLUMNS` in `tests/test_migration.py`). Add unit tests for all 7 new helpers covering: SQL parametrization, recent-window filtering, rate-limit window expiry, independent pair tracking.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "import sqlite3, pending_articles_repo; conn=sqlite3.connect(':memory:'); pending_articles_repo.init_schema(conn); pending_articles_repo.init_schema(conn); print([r[1] for r in conn.execute('PRAGMA table_info(pending_articles)')])"` → output contains `model_fingerprint` and no exception fires on double-call.
- **Files to modify:** `pending_articles_repo.py`, `tests/test_pending_articles_repo.py`, `tests/test_migration.py`
- **Files to read:** `pending_articles_repo.py`, `outage_state.py`, `work/cross-source-dedup/code-research.md` (§2, §14.D, §14.G, §14.H, §14.I)

#### Task 3: Admin-ping builders E014, E015, E016
- **Description:** Add 3 pure builder functions to `admin_alerts.py` per Decision 7: `alert_cross_source_dupe(...)` for E014 columnar full format (mirror E006 shape), `alert_cross_source_blocked(new_link, existing_link, overlap_pct)` for E015 short 2-3 line, `alert_dedup_degraded(reason)` for E016 short alert. Add 3 unit tests in `tests/test_admin_alerts.py` mirroring E006 test pattern.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_admin_alerts.py -k "e014 or e015 or e016" -v` → 3 tests pass.
- **Files to modify:** `admin_alerts.py`, `tests/test_admin_alerts.py`
- **Files to read:** `admin_alerts.py`, `work/cross-source-dedup/code-research.md` (§3.3-3.4, §14.K)

### Wave 2 (зависит от Wave 1)

#### Task 4: Wire dedup gate in news_bot.job() + integration tests
- **Description:** Add private `_check_cross_source_dedup(article, fp, conn) -> tuple[str, dict | None]` helper to `news_bot.py`. Wire into `job()` at the position defined by Decision 14 (after `_is_text_only_checklist`, before `row` assembly). Wrap entire dedup logic in `try/except Exception` per Decision 12. Hard-block branch calls `mark_processed` + E015 ping (Decision 8). Soft-flag branch checks `is_pair_rate_limited`, sends E014 if not limited, then `mark_pair_pinged`. Degraded branch logs traceback, sends rate-limited E016, sets `fp = None`. Add `tests/test_integration.py::TestCrossSourceDedup` covering all 7 scenarios from Testing Strategy (hard-block, pass-through with non-empty fp, soft-flag, soft-flag rate-limit, within-source dedup, empty fingerprint, degraded mode) plus the separate `TestFingerprintCarryThrough` (pending→published roundtrip).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_integration.py::TestCrossSourceDedup tests/test_integration.py::TestFingerprintCarryThrough -v` → all 8 tests pass.
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`
- **Files to read:** `news_bot.py`, `model_extractor.py` (from Task 1), `pending_articles_repo.py` (from Task 2), `admin_alerts.py` (from Task 3), `tests/test_integration.py`, `work/cross-source-dedup/code-research.md` (§1, §14.K)

#### Task 5: backfill_fingerprints.py one-shot script
- **Description:** Create top-level `backfill_fingerprints.py` mirroring `hw_review.py` argparse + `main(argv=None) -> int` shape per Decision 10. CLI: `--days N` (default 14, clamped to [1, 90]), `--dry-run`, `--verbose`. Per published row with `model_fingerprint IS NULL` in window: re-fetch via `fetch_full_article`, on success run `extract_fingerprint` + `update_published_fingerprint`; on empty-but-no-exception store `{"strict":[],"brands":[]}` (computed-empty, terminal); on exception leave NULL (transient, retry on next run) and count as error. 1-second sleep between fetches. **`import news_bot` BEFORE `logging.basicConfig()`** so `_TokenRedactingFilter` attaches to root logger (security-review caveat). Log summary per code-research §14.E.5 (Window/Processed/Skipped/Empty fp/Errors/Duration). Add `tests/test_backfill_fingerprints.py` covering: idempotency on second run, `--days` window honored, `--dry-run` writes nothing, fetch-exception leaves NULL, computed-empty stores `'{}'`-shape, summary structure.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 backfill_fingerprints.py --dry-run --days 1` on test DB → exits 0, summary contains `Processed:`, `Skipped:`, `Errors:`.
- **Files to modify:** `deploy.sh` (add `model_extractor.py` and `backfill_fingerprints.py` to FILES list — per tech-spec AC).
- **Files to create (new):** `backfill_fingerprints.py`, `tests/test_backfill_fingerprints.py`.
- **Files to read:** `hw_review.py`, `news_bot.py`, `model_extractor.py` (from Task 1), `pending_articles_repo.py` (from Task 2), `deploy.sh`, `work/cross-source-dedup/code-research.md` (§14.E)

### Audit Wave

#### Task 6: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified (model_extractor.py, backfill_fingerprints.py, pending_articles_repo.py, admin_alerts.py, news_bot.py). Review holistically: cross-component consistency (regex compile location, JSON shape uniformity, error handling discipline), Shared Resources Architecture compliance (singleton compiled regex), adherence to project patterns (`boilerplate_filter.py` mirror, `outage_state.py` pattern for bot_state helpers). Write audit report to `work/cross-source-dedup/logs/audits/code-audit.md`.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit
- **Description:** Full-feature security audit. Read all source files. Focus on: backfill script SSRF surface (does `entry_stub` validation match `news_bot.fetch_full_article` expectations?), regex ReDoS verification (bounded quantifiers per Decision 3), SQL injection in new repo helpers (parametrized queries only), no secrets in admin-ping templates, link content in pings is operator-trusted (no rendering injection). Write report to `work/cross-source-dedup/logs/audits/security-audit.md`.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit
- **Description:** Full-feature test quality audit. Read all test files (test_model_extractor, test_backfill_fingerprints, new sections in test_admin_alerts, test_pending_articles_repo, test_migration, test_integration). Verify: calibration test is meaningful (not tautological), integration test scenarios cover all 4 branches (block/flag/pass/degraded), schema-pin tests catch missing column, mocks don't leak through assertions. Write report to `work/cross-source-dedup/logs/audits/test-audit.md`.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 9: Pre-deploy QA
- **Description:** Acceptance testing. Run `pytest -q` (must be all green, no regressions). Verify each user-spec AC (AC1-AC12) against tests + code paths. Verify each tech-spec AC (idempotency, schema-pin, performance budget, no new deps). Verify `python3 backfill_fingerprints.py --dry-run --days 1` runs on a test DB and produces a clean summary. Confirm fingerprint shape contract (dict-with-two-list-keys vs the user-spec's `list[str]` — deviation documented + approved). Report results.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 10: Deploy
- **Description:** Standard SCP-based deploy via `./deploy.sh` (operator runs locally). Bundle includes new top-level files (`model_extractor.py`, `backfill_fingerprints.py`) and modified files (`news_bot.py`, `pending_articles_repo.py`, `admin_alerts.py`, `tests/...`). Verify deploy bundle FILES list at top of `deploy.sh` is updated to include the new top-level files. Operator runs `./deploy.sh` from local; first cron tick on VPS auto-runs `init_db()` migration. Verify on VPS via `sqlite3 /home/hwbot/bot/news.db ".schema pending_articles"` that `model_fingerprint TEXT` column is present.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 11: Post-deploy verification
- **Description:** Live environment verification on prod VPS:
  - Operator runs `python3 backfill_fingerprints.py --days 14` on prod manually (~5 min) — tool: bash on VPS SSH session.
  - Verify summary output reports Errors: 0 (or low, with operator-documented exceptions for Cloudflare 403s) — tool: bash stdout inspection.
  - Verify `SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL` matches expected backfilled count — tool: sqlite3 CLI on VPS.
  - 2-week passive monitoring: spot-check each `[E014]` and `[E015]` admin ping in operator Telegram — tool: Telegram visual review (operator-side).
  - 1× / week: review channel @myhwchannel last 7 days for any visible duplicates (false negatives) — tool: Telegram visual review.
  Tools: bash on VPS (SSH session), sqlite3 CLI, Telegram visual (operator-side).
- **Skill:** post-deploy-qa
- **Reviewers:** none
