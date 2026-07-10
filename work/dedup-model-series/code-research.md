# Code Research: dedup-model-series

Feature: add a NEW hard-block dedup rule keyed on `(car model + series/theme)` pairs, layered ON TOP of the shipped `cross-source-dedup` set-overlap rule. Window 30 days, ANY source, whole-article block on ≥1 shared pair, visible `[E015]`-style ping, fall back to the existing set-overlap rule when no series/theme is recognised, degraded-mode on crash, one-time ~30-day backfill.

This extends the already-shipped `work/cross-source-dedup/` feature (deployed to the TEST instance; prod promotion deferred per that feature's Task 10). All paths below are absolute-relative to the repo root `/workspaces/debian-2/my-hw/`.

Cross-referenced against:
- `news_bot.py` (~2400 LoC now; dedup gate at 2099-2216, helper at 907-999)
- `pending_articles_repo.py` (~1010 LoC; migration + 10 dedup helpers)
- `admin_alerts.py` (E014/E015/E016 builders at 405-481)
- `model_extractor.py` (326 LoC; lexicon + regex + similarity)
- `backfill_fingerprints.py` (307 LoC)
- `tests/test_model_extractor.py`, `tests/test_integration.py`, `tests/test_pending_articles_repo.py`, `tests/test_migration.py`, `tests/test_backfill_fingerprints.py`, `tests/test_admin_alerts.py`, `tests/fixtures/cross_source_dedup_pairs.py`
- `work/cross-source-dedup/{user-spec,tech-spec,decisions}.md`

---

## 0. Decided rules (from dedup-model-series interview) vs shipped state

From `work/dedup-model-series/logs/userspec/interview.yml` + the orchestrator brief:

| Rule | New (model+series) pair-rule | Existing set-overlap rule (unchanged) |
|------|------------------------------|----------------------------------------|
| Window | **30 days** | 7 days |
| Source scope | **ANY source** (incl. same-source follow-ups) | cross-source only (see §1.4 — the shipped code already reversed to cross-source-only) |
| Match test | block on **≥1 shared `(model+series)` pair** | Jaccard ≥0.50 on the strict/brand sets |
| Block scope | **whole new article** | whole article |
| Verdict tiers | hard-block only (no soft-flag tier) + visible `[E015]`-style ping | block ≥0.50 / soft-flag `[E014]` 0.30-0.49 / pass |
| Fallback | if NO series/theme recognised → fall to the existing set-overlap rule | n/a |
| Degraded mode | publish on any crash (reuse `[E016]`) | already implemented |
| Backfill | one-time ~30-day | one-time 14-day (shipped) |

The motivating real trio (must land in the calibration fixture): SDCC 2026 exclusives — Stranger Things / K-Pop Demon Hunters / Top Gun (+ a Porsche) — appearing as **t-hunted PT + autoevolution EN + a same-source "more photos" follow-up**. Two of the three miss the current dedup: (a) pop-culture tie-ins produce an empty/thin car-model fingerprint → currently skipped by the AC6 short-circuit; (b) same-source follow-ups are skipped by the cross-source-only guard.

---

## 1. The existing dedup path, end-to-end

### 1.1 `model_extractor.py` — extract_fingerprint / similarity / lexicon / regex

File: `model_extractor.py` (pure module, stdlib `re`+`set` only, mirrors `boilerplate_filter.py`).

- `Fingerprint = Dict[str, List[str]]` — shape `{'strict': ['toyota 4runner', ...], 'brands': ['toyota', ...]}`. Lists sorted for deterministic JSON.
- `_LEXICON` — `frozenset` of 36 canonical brand keys (`model_extractor.py:69-76`). Doc-comment says "35" but Mini was added (self-noted at line 77-80). This is the **brand** lexicon; there is **no series/theme lexicon anywhere in the repo** (greenfield for this feature).
- `_BRAND_ALIASES` (`:83-87`) — `chevy→chevrolet`, `vw→volkswagen`, `mercedes-benz→mercedes`.
- `_MODEL_AFTER_BRAND_RE` (`:115-132`) — single compiled, case-insensitive, ReDoS-safe (all quantifiers bounded: `{1,3}`, `{1,2}`, `{0,2}`, `{1,24}`, `{0,24}`). Optional year prefix `(?:(?:19|20)\d{2}\s{1,3})?` (dropped from output). Named groups `brand`, `model`, `model_extra` (up to 2 designator words).
- `_MODEL_EXTRA_KEEP_RE` (`:147`) — keeps only designator-looking extra tokens (all-caps / digit-bearing / hyphenated: `GT`, `Z28`, `4Runner`, `F-150`), drops prose ("gold", "review").
- `_UPPERCASE_BRANDS_RE` (`:165`) — case-sensitive `\b(?P<brand>AMC|BMW|Lotus)\b` (prose-collision defence).
- `extract_fingerprint(article) -> Fingerprint` (`:223-287`) — concatenates `title + subtitle + paragraphs` via `_gather_text` (`:198-216`, tolerant of missing keys/None), two regex passes, returns sorted lists. Empty input → `{'strict': [], 'brands': []}`.
- `similarity(a, b) -> float` (`:290-325`) — guarded two-level Jaccard: AC6 empty-guard → 0.0; AC8 1-token guard → strict-only; AC8 ≥2-brands-both-sides gate for brand fallback; else `max(strict_jaccard, brands_jaccard)`. `_jaccard` at `:172-184`.

Key extension seam: this module is where the **series/theme extractor** should live (see §4). `extract_fingerprint` already scans the concatenated body — the series extractor can run in the same pass and enrich the same returned dict.

### 1.2 `_check_cross_source_dedup` — the comparison helper

File: `news_bot.py:907-999`. Signature:
```python
def _check_cross_source_dedup(article, fingerprint, conn, new_source=None)
    -> ('block', match) | ('flag', match) | ('pass', None)
```
- Thresholds: `_DEDUP_BLOCK_THRESHOLD = 0.50` (`:899`), `_DEDUP_FLAG_THRESHOLD = 0.30` (`:904`).
- AC6 short-circuit: empty `fingerprint['strict']` → `('pass', None)` **without any SQL** (`:949-954`). **This is exactly the branch that lets pop-culture tie-ins slip through today** — the new pair-rule must fire BEFORE (or instead of) this short-circuit when a series/theme is present.
- Candidate set (`:956-959`): `list_recent_pending_fingerprints(conn, 7) + list_recent_published_fingerprints(conn, 7)` — **hardcoded `7`**.
- Same-source skip (`:964-968`): `if new_source and row.get('source_name') == new_source: continue`. This is the shipped reversal of Decision 9 (see §1.4).
- NULL/malformed candidate `model_fingerprint` skipped silently (`:969-973`).
- Picks best (highest) sim; builds `match` dict with `link, source_name, models (shared strict), overlap_pct, n_matches, n_total` (`:982-995`). Returns block/flag/pass.

### 1.3 Where it is wired into `job()`

File: `news_bot.py`, fetch loop. Order (matches shipped code-research §1):
1. `fetch_full_article(entry)` → `article` (`:2083`).
2. Guard: no paragraphs → skip (`:2084-2086`).
3. `_is_text_only_checklist(entry, article)` → `continue` (`:2092-2097`).
4. **DEDUP GATE** (`:2099-2216`) — the seam this feature modifies.
5. `row = {..., 'model_fingerprint': fp}` assembly (`:2218-2229`).
6. `pending_repo.insert_pending(row)` (`:2230-2238`).

Gate internals (all inside one broad `try/except Exception` per Decision 12 / AC9):
- `new_source = entry.get('source_name') or _resolve_source_name(link)` (`:2121`).
- Opens a dedicated short-lived conn `pending_repo._connect()` (`:2123`, closed in `finally` at `:2180`). (Code-audit flagged the private `_connect()` reach-in as a follow-up.)
- `fp = model_extractor.extract_fingerprint(article)` (`:2125`).
- `decision, match = _check_cross_source_dedup(article, fp, dedup_conn, new_source)` (`:2126`).
- **block branch** (`:2130-2151`): INFO log, `mark_processed(link, title, pub_date)` (Decision 8), `send_admin_notification(alert_cross_source_blocked(...))` (E015), `continue`.
- **flag branch** (`:2153-2178`): `is_pair_rate_limited` check → `alert_cross_source_dupe(...)` (E014) → `mark_pair_pinged` → `commit`.
- **pass**: nothing; `fp` falls into `row`.
- **except** (`:2182-2216`): `logger.exception`, second `_connect()`, `is_dedup_degraded_rate_limited` → `alert_dedup_degraded(type(exc).__name__)` (E016) → `mark_dedup_degraded_pinged`, set `fp = None`. Article still publishes.

`mark_processed(link, title, pub_date)` at `news_bot.py:684`. `_resolve_source_name` derives source from netloc.

### 1.4 Decision 9 was REVERSED in the shipped code (critical)

The tech-spec Decision 9 ("Comparison NOT filtered by source_name") was reversed on **2026-06-14** — the shipped `_check_cross_source_dedup` now **skips same-source candidates** (`news_bot.py:964-968`), and `tests/test_integration.py::test_within_source_not_deduped` (`:1171-1232`) pins that behaviour. The orchestrator brief describes the new feature as "REVERSES the existing Decision 9 'cross-source only'" — i.e. the NEW pair-rule must compare against ANY source, re-restoring the original any-source intent, but **only for the pair-rule**. The set-overlap fallback keeps whatever source scope is decided (see §5.3 for the composition ambiguity this creates and the test that must change).

---

## 2. Storage layer

### 2.1 Current schema + JSON-column plumbing

File: `pending_articles_repo.py`.
- `_PENDING_DDL` (`:60-83`) — `pending_articles`, PK `link`, timestamp `fetched_at` (`:77`). No paragraphs-less problem here (pending DOES store `paragraphs`).
- `_PUBLISHED_DDL` (`:85-96`) — `published_articles`, PK `link`, timestamp `published_at` (`:93`). **No `paragraphs` column** — this is why backfill must re-fetch (§3).
- `_BOT_STATE_DDL` (`:123-128`) — k/v store backing rate-limits.
- `model_fingerprint TEXT` already added to BOTH tables via the idempotent migration loop in `init_schema` (`:208-219`): a `for ddl in (...): try: conn.execute(ddl) except sqlite3.OperationalError: pass` block. Adding more `ALTER`s here is the proven pattern.
- `_PENDING_JSON_COLS` (`:133-134`) includes `'model_fingerprint'`; `_PUBLISHED_JSON_COLS = ('model_fingerprint',)` (`:138`). `_row_to_dict` (`:160`) uses these to auto-deserialise the JSON blob back into a Python dict.
- `_dumps` (`:145`, `ensure_ascii=False`, NULL-preserving), `_connect` (`:174`).

### 2.2 The dedup helpers (all already present)

- `insert_pending(entry)` (`:227-266`) — column list includes `model_fingerprint`; value `_dumps(entry.get('model_fingerprint'))` (`:262`, NULL-preserving).
- `move_to_published(...)` (`:578-...`) — carries `model_fingerprint` pending→published via SELECT-by-name (`:617`) + INSERT (`:631-635`). AC2 carry-through. **Anything added inside the `model_fingerprint` JSON blob is carried for free** (it is one opaque TEXT column).
- `list_recent_pending_fingerprints(conn, days=7)` (`:835-858`) — projects `link, source_name, title, model_fingerprint, fetched_at`; window `WHERE fetched_at >= datetime('now', ? || ' days')`.
- `list_recent_published_fingerprints(conn, days=7)` (`:861-877`) — same shape on `published_at`.
- `update_published_fingerprint(conn, link, fingerprint)` (`:880-894`) — backfill writer.
- Rate-limit helpers (`:905-1007`): `_parse_dt_tolerant`, `_pair_key` (`softflag_pair:` prefix + `\n` separator), `is_pair_rate_limited(window_days=7)`, `mark_pair_pinged`, `is_dedup_degraded_rate_limited(window_hours=1)`, `mark_dedup_degraded_pinged`. Key constants `_KEY_SOFTFLAG_PAIR_PREFIX` (`:50`), `_KEY_DEDUP_DEGRADED` (`:53`).

### 2.3 RECOMMENDATION: extend the JSON blob, do NOT add a new column

Add a `"pairs"` key (and optionally a `"series"` key) INSIDE the existing `model_fingerprint` JSON object rather than a new SQL column:

```json
{
  "strict": ["subaru legacy gt", "toyota 4runner"],
  "brands": ["subaru", "toyota"],
  "series": ["car culture", "stranger things"],
  "pairs":  ["subaru legacy gt|car culture", "porsche 911|san diego comic-con"]
}
```

Why extend the blob (strongly recommended):
1. **Zero migration, zero schema-pin churn.** `model_fingerprint TEXT` already exists; `_PENDING_JSON_COLS` / `_PUBLISHED_JSON_COLS` already deserialise it; `move_to_published` already carries it; `insert_pending` already writes it. New keys ride along automatically. A new column would touch `_PENDING_DDL`, `_PUBLISHED_DDL`, the migration loop, `insert_pending`, `move_to_published`, `_row_to_dict`, AND four schema-pin dicts.
2. **`list_recent_*_fingerprints` already return the full dict** — the gate reads `row['model_fingerprint']['pairs']` with no query change (only a `days` argument change, §5).
3. Matches the shipped Decision 5 rationale (single JSON column over multi-column / side table).

Backward-compat contract (must-handle): rows written before this feature (or pending rows computed in the 7-day window just before deploy) have a `model_fingerprint` dict WITHOUT `"pairs"`/`"series"`. The pair-rule must treat a missing `"pairs"` key as "no series recognised on that candidate" (`row_fp.get('pairs') or []`), never `KeyError`. The 30-day backfill (§3) repopulates published rows with the new keys.

If a new column were ever wanted anyway, the idempotent pattern is fixed: append two lines to the `init_schema` migration tuple (`pending_articles_repo.py:208-213`):
```python
"ALTER TABLE pending_articles ADD COLUMN model_series TEXT",
"ALTER TABLE published_articles ADD COLUMN model_series TEXT",
```
and add `'model_series'` to both `_*_JSON_COLS` + all four schema-pin dicts. Recommendation stands: don't — extend the blob.

---

## 3. `backfill_fingerprints.py` — re-extraction and the 30-day extension

File: `backfill_fingerprints.py` (307 LoC, mirrors `hw_review.py` shape).
- CLI: `--days N` default **14**, clamped `[1, 90]` via `_days_in_range` (`:100-112`); `--dry-run`; `--verbose`.
- `import news_bot` BEFORE `logging.basicConfig()` (`:72`, security caveat — `_TokenRedactingFilter`).
- Scans `published_articles WHERE published_at >= datetime('now', ? || ' days') AND model_fingerprint IS NULL` (`:243-249`). NULL is the canonical "needs backfill" marker.
- `backfill_one(conn, row, dry_run)` (`:144-201`): builds an `entry_stub` (`link, source_name, title, published=''`) from the projected row (`:168-176`) → `news_bot.fetch_full_article(entry_stub)` (narrow try/except, `:182-186`) → on empty body store terminal `{'strict':[],'brands':[]}` (`:190-195`) → else `extract_fingerprint` + `update_published_fingerprint` (`:197-201`). 1-second inter-fetch sleep (`:270-271`).
- Summary printed to stdout (`:294-300`): Window / Processed / Skipped / Empty fp / Errors / Duration.

**How to extend for series/theme (30-day window):**
1. Change the operational default (or the operator invocation) to `--days 30`. The clamp already permits 30 (`[1,90]`). Simplest: operator runs `python3 backfill_fingerprints.py --days 30` once after deploy; no code change needed to the window.
2. `backfill_one` already calls `extract_fingerprint(article)`. If the series/theme extraction is folded INTO `extract_fingerprint` (recommended, §4), the backfill populates `series`/`pairs` for free — **no backfill code change at all** beyond the window arg.
3. **Re-backfill idempotency caveat:** the SELECT filter is `model_fingerprint IS NULL`. Rows already backfilled by the shipped 14-day run have a NON-NULL blob WITHOUT the new `pairs` key → they will be SKIPPED, so they never gain series/pairs. Two options:
   - (a) Run backfill on a prod DB where the shipped 14-day backfill has NOT yet run (prod promotion was deferred — this is the likely real state). Then the first backfill is the 30-day one and populates everything. **Confirm prod backfill state before writing the tech-spec.**
   - (b) If some rows already carry the old-shape blob, add a "needs series upgrade" predicate — e.g. re-select rows where `model_fingerprint IS NULL OR json_extract(model_fingerprint,'$.pairs') IS NULL` (SQLite `json_extract` is available), or widen the marker to also re-process rows lacking the `pairs` key. This is the one real code change the 30-day series backfill may need.
4. Note the re-fetch cost: ~30 days of published rows × ~7/day ≈ ~200 fetches × (fetch + 1 s sleep) ≈ a few minutes, Cloudflare-gated (operator-run, off the publish window).

---

## 4. Where series/theme extraction should live + sourcing the lexicon

### 4.1 Location — extend `model_extractor.py` (recommended)

Add a `SERIES_LEXICON` + `extract_series(text)` inside `model_extractor.py` and enrich the dict returned by `extract_fingerprint`, rather than a new `series_extractor.py`. Reasons:
- `extract_fingerprint` already builds the concatenated `title+subtitle+paragraphs` scan string (`_gather_text`) and already runs one regex pass — series extraction is a second `finditer` over the same text, cheaply co-located.
- Callers (`news_bot._check_cross_source_dedup`, `backfill_fingerprints.backfill_one`, all tests) already import `model_extractor` and consume its dict. A single enriched dict avoids a second module import and a second body scan.
- Mirrors the shipped design (one pure extractor, one blob).

New public surface to add (keep pure, no I/O):
```python
def extract_series(text_or_article) -> list[str]      # ['car culture', 'stranger things']
def extract_pairs(fp: Fingerprint) -> list[str]       # ['subaru legacy gt|car culture', ...]
def shares_pair(fp_a, fp_b) -> tuple[bool, list[str]] # any shared (model+series) pair + the shared list
```
`extract_fingerprint` returns the enriched dict (`strict`, `brands`, `series`, `pairs`). `pairs` should be the cartesian `{strict tokens} × {series tokens}` joined with a separator that cannot appear in either token (`|` is safe; brand/model tokens are `[a-z0-9 -]`, series tokens are lexicon-controlled). Pop-culture tie-ins with NO car model but a recognised franchise need a design choice: either allow series-only pairs like `"*|stranger things"` (so the SDCC same-source follow-up with no distinct casting is still caught) or key pairs on `(brand, series)` when no full model exists. This is the single most important extraction-design decision for catching the real SDCC trio — flag it for the tech-spec.

### 4.2 The series lexicon must be built from real feed data + validated

There is **no existing series lexicon** in the repo (confirmed: `_LEXICON` is brands only; the LLM-prompt glossaries and `ux-guidelines.md` are translation aids, not series lists). HW series/theme naming DOES appear verbatim in the feeds (the shipped feature's real pair is literally "Car Culture Road Trip Mix"). Recommended sourcing:
1. **Sample `published_articles` titles + `pending_articles.paragraphs`** on the (test/prod) DB and grep for the known series families to build the lexicon empirically (same method §14.A used to derive the brand lexicon from fixtures). `pending_articles` stores `paragraphs` (JSON); `published_articles` does NOT (titles only) — for body text use pending rows or re-fetched bodies.
2. Seed families from the brief: HW car-lines (`Car Culture`, `Boulevard`, `Team Transport`, `Super Treasure Hunt`/`STH`, `RLC`/`Red Line Club`, `Zamac`, `Monster Trucks`, `Character Cars`, `Fast & Furious`, `Pop Culture`, `Premium`), events (`San Diego Comic-Con`/`SDCC`), licensed franchises (`Stranger Things`, `Top Gun`, `K-Pop Demon Hunters`, plus the usual Mattel-licensed set — Marvel, Star Wars, Mario, etc.).
3. Build as an alias→canonical map (like `_BRAND_ALIASES`): `sth→super treasure hunt`, `rlc→red line club`, `sdcc→san diego comic-con`. Multi-word, ReDoS-safe bounded regex (mirror `_MULTIWORD_BRANDS_RE`). Watch case-collision risk on short/ambiguous names (`RLC`, `STH`, `Zamac`) — apply the same case-sensitivity discipline the brand extractor uses for `AMC`/`BMW`.
4. **Validate** the lexicon against a labelled fixture (the calibration set, §6) and by sampling recent real titles — series names drift each mainline year, so the fixture + a monthly review are the guard (same risk posture as the brand lexicon).

---

## 5. The gate change: composition of the two rules

### 5.1 30-day window + any-source scan

- Bump the candidate window for the pair-rule from the hardcoded `7` (`news_bot.py:957-958`) to 30. Since 30 ⊇ 7, fetch ONCE at 30 days and derive the 7-day subset in Python (compare `fetched_at`/`published_at` to `now-7d`), or issue the 30-day query for pairs and keep the existing 7-day query for the set-overlap fallback. One 30-day scan is ~200 rows — full-scan fine, no index needed (shipped §13.5 established this).
- Drop the same-source skip (`news_bot.py:964-968`) **for the pair-rule** so any-source (incl. same-source follow-ups) is compared. Keep it for the set-overlap fallback if that rule is to remain cross-source-only (decision needed, §5.3).

### 5.2 Two-rule dispatch (pair-rule FIRST, set-overlap fallback)

Recommended shape inside `_check_cross_source_dedup` (or a new sibling helper that the gate calls first):
```
new_pairs = fp.get('pairs') or []
if new_pairs:                                  # series/theme recognised
    for cand in candidates_30d(any_source):
        shared = set(new_pairs) & set(cand_fp.get('pairs') or [])
        if shared:                             # ≥1 shared (model+series) pair
            return ('block', match_with(shared, cand))   # whole-article hard block
    return ('pass', None)                      # series present but no pair match → done, do NOT also run set-overlap
else:                                          # NO series/theme → existing behaviour
    return existing_set_overlap(fp, candidates_7d(cross_source))   # ≥0.50 block / 0.30-0.49 flag / pass
```
Open question for the tech-spec: when a series IS recognised but no pair matches, should it STILL fall through to set-overlap? The brief says "If NO series/theme recognised → fall back" — implying the fallback runs ONLY when no series was found. The snippet above honours that literally. Confirm.

The AC6 short-circuit (`news_bot.py:949-954`) must move: today an empty `strict` returns `('pass', None)` before any SQL. With the pair-rule, an article with a recognised SERIES but empty `strict` (pop-culture tie-in) must NOT short-circuit — it must run the pair scan. Re-gate the short-circuit on "empty strict AND empty series".

### 5.3 The same-source test must flip

`tests/test_integration.py::test_within_source_not_deduped` (`:1171-1232`) currently asserts a same-source 100%-overlap article PUBLISHES (pins the cross-source-only reversal). Under the new rule, if those same-source articles share a `(model+series)` pair they must be BLOCKED. This test must be rewritten (or split): keep a "same-source, no series recognised → still publishes (set-overlap is cross-source)" case AND add a "same-source, shared pair → blocked" case. This is the single behaviour reversal most likely to be missed.

### 5.4 E015 ping detail change

`alert_cross_source_blocked(new_link, existing_link, overlap_pct)` (`admin_alerts.py:448-459`) currently shows only links + `overlap_pct`. The brief wants the ping to list "what was blocked + which pair matched + which earlier article". For the pair-rule, `overlap_pct` is not the right signal (block is on ≥1 shared pair, not a percentage). Recommendation: add a new builder (e.g. `alert_series_blocked(new_link, existing_link, matched_pairs: list[str])`) OR extend E015 to accept an optional `matched_pairs` argument and render the shared pair(s) + the earlier article link. Keep the `Заблокирован дубль` substring anchor (integration tests depend on it — `admin_alerts.py:451`, comment "Не менять"). The block branch in `job()` (`news_bot.py:2141-2151`) then passes `match['models']`/matched pairs into the builder. No soft-flag (`E014`) tier for the new rule — the flag branch stays only for the set-overlap fallback.

---

## 6. Existing tests to update

| Test file | What changes |
|-----------|--------------|
| `tests/test_model_extractor.py` | Add `TestExtractSeries` (lexicon hits, aliases SDCC/STH/RLC, prose false-positives) + `TestPairs`/`shares_pair`. Extend `test_calibration_accuracy` (`:332`) and add series pairs to the fixture. The real-pair must-pass (`:363`) still holds. |
| `tests/test_integration.py::TestCrossSourceDedup` (`:860`) | Add pair-rule block scenarios (cross-source AND same-source follow-up). **Rewrite `test_within_source_not_deduped` (`:1171`)** per §5.3. Add "series recognised, no pair match → pass" and "no series → set-overlap fallback still works". `test_empty_fingerprint` (`:1240`) must change if a series-only (no car model) article now blocks. `TestFingerprintCarryThrough` (`:1339`) — verify `pairs`/`series` carry through `move_to_published`. |
| `tests/test_pending_articles_repo.py` | If the blob is extended (no new column) → `EXPECTED_PENDING`/`EXPECTED_PUBLISHED` (`:30`, `:55`) UNCHANGED (still one `model_fingerprint TEXT`). Add roundtrip test that `pairs`/`series` survive insert+get and `move_to_published`. `_PENDING_JSON_COLS` pin (`:974`) stays green. If a NEW column is chosen → update both dicts + `TestCrossSourceDedupSchemaPin` (`:934`). |
| `tests/test_migration.py` | Same conditional: blob-extension → `EXPECTED_PENDING_COLUMNS` (`:38`) / `EXPECTED_PUBLISHED_COLUMNS` (`:69`) UNCHANGED. New-column path → add the pins at `:62`/`:79` style + the idempotency assertions (`:204-205`). |
| `tests/test_backfill_fingerprints.py` | Add: 30-day window honoured; series/pairs populated on backfilled published rows; if re-processing old-shape blobs is added (§3.4), test that rows with `pairs` missing are re-upgraded and rows with `pairs` present are skipped. |
| `tests/test_admin_alerts.py` | Add a test for the new/extended E015 series-block builder (asserts the matched pair + earlier link render; keeps `Заблокирован дубль` anchor). |
| `tests/fixtures/cross_source_dedup_pairs.py` | Add the **3 real SDCC dupes** as labelled duplicate pairs: t-hunted PT ↔ autoevolution EN (Stranger Things / K-Pop Demon Hunters / Top Gun + Porsche), AND a same-source "more photos" follow-up. Existing `DUPE_PAIRS`/`NON_DUPE_PAIRS` shape at `:37`. |

---

## 7. Risks & feasibility

1. **Over-blocking (the dominant risk).** "block on ANY single shared pair" + BROAD themes (car-lines + events + franchises) is aggressive. A legit new Car Culture drop that happens to re-use one casting from a Car Culture article 29 days ago would be hard-blocked. The interview itself flags this (interview.yml note: "any-overlap + broad themes risks over-blocking legit new drops that share one casting"). Mitigations to specify: precise pair keys (full `model+series`, not `brand+series`), a tight/curated series lexicon, the visible E015 ping as the early-warning tripwire, and a calibration fixture that includes near-miss non-dupes (two different Car Culture mixes sharing one car → must NOT block). There is NO manual re-publish path and NO soft-flag tier, so a false block is silent-to-subscribers and only visible via the ping — calibration is load-bearing.
2. **Same-source follow-up semantics.** Enabling any-source re-introduces the exact false-positive class the 2026-06-14 reversal removed (autoevolution Ford F-100 vs Porsche Team Transport sharing a brand). The pair-key (model+series, not brand-only) is what makes any-source safe now; verify the pair granularity prevents brand-only collisions.
3. **Pop-culture tie-in with no car model.** The whole point of the feature (SDCC franchises) is articles where `strict` is empty. The pair extraction MUST produce series-only or brand+series pairs for these, else the feature misses its headline case. This interacts with the AC6 short-circuit removal (§5.2).
4. **Performance.** 30-day scan is ~4× the current 7-day window (~200 rows vs ~50). Each candidate: one `similarity` call OR one set-intersection on `pairs`. Set-intersection is cheaper than Jaccard; still O(candidates). Well within the shipped <100 ms/article budget. Fetch ONCE at 30 days, don't issue two overlapping queries per article.
5. **Backfill re-upgrade of old-shape blobs (§3.4).** If the shipped 14-day backfill already ran on the target DB, the `IS NULL` marker will skip those rows and they never gain `pairs`. Confirm prod backfill state; may need the widened re-select predicate.
6. **Degraded mode reuse.** The new pair-rule runs inside the same broad `try/except` (`news_bot.py:2182`); a series-extractor crash reuses E016 and publishes. No new failure surface if series extraction stays inside `extract_fingerprint`.

### Calibration fixture — mandatory contents
Per the brief, the calibration set MUST include the 3 real SDCC dupes: (a) t-hunted PT + (b) autoevolution EN + (c) same-source "more photos" follow-up, all classified `duplicate` via a shared `(model+series)` pair. Add at least one near-miss non-dupe (two distinct Car Culture mixes sharing exactly one casting) to guard the over-blocking risk. Reuse the shipped `DUPE_PAIRS`/`NON_DUPE_PAIRS` structure and the `test_calibration_accuracy` / `test_calibration_real_pair_must_pass` harness (`tests/test_model_extractor.py:332,363`).

---

## 8. Integration points summary (files to touch)

| File | Change |
|------|--------|
| `model_extractor.py` | Add `SERIES_LEXICON` + `extract_series` + `extract_pairs`/`shares_pair`; enrich `extract_fingerprint` return with `series`+`pairs`. |
| `news_bot.py` | `_check_cross_source_dedup` (`:907-999`): pair-rule first (30-day, any-source, ≥1 shared pair → block), set-overlap fallback (7-day) only when no series; move AC6 short-circuit; block branch passes matched pairs to E015 (`:2141-2151`). |
| `pending_articles_repo.py` | Recommended: NO change (blob extension rides existing `model_fingerprint`). Only if the 30-day scan needs a distinct window arg → callers pass `days=30`; helpers already take `days`. Optional §3.4 backfill re-select predicate lives in `backfill_fingerprints.py`, not here. |
| `admin_alerts.py` | New/extended E015 series-block builder (matched pair + earlier link); keep `Заблокирован дубль` anchor. |
| `backfill_fingerprints.py` | Operator runs `--days 30`; optional re-select predicate for old-shape blobs (§3.4). Series/pairs populate automatically if extraction is folded into `extract_fingerprint`. |
| `deploy.sh` + `.github/workflows/deploy*.yml` | No new top-level files if extraction stays in `model_extractor.py` (already in the FILES list). If a `series_extractor.py` is created instead, it MUST be added to all three FILES arrays (the cross-source-dedup Task 10 incident: a missing FILES entry crashloops prod on ImportError). |

Recommendation: keep all new logic inside the three existing modules (`model_extractor.py`, `news_bot.py`, `admin_alerts.py`) + the calibration fixture — no new top-level file, no new SQL column, no schema-pin churn. That keeps this an S/M feature and avoids the deploy-FILES-drift class of bug.
