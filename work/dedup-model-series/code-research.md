# Code Research: dedup-model-series

Feature: add a NEW **opt-in, tiered** dedup rule keyed on `(car model + series/theme)` pairs, layered ON TOP of the shipped `cross-source-dedup` set-overlap rule. Window 30 days, ANY source. Verdict is **two-tier**: a **distinctive** pair (concrete model + concrete franchise/event/limited-series) → **hard block** + `[E015]`; a **broad** match (theme-only, or a frequent recurrent car-line) → **soft flag** + `[E014]`, article still publishes. **Fail-safe polarity:** only series explicitly tagged `distinctive` in the lexicon can hard-block; anything unknown / new / ambiguous defaults to **broad** (never a silent hard block). One **env toggle** (default ON) short-circuits the whole pair-rule. When the pair-rule does not hard-block (no match / no series / toggle off / crash), the existing set-overlap rule **always** runs as the backstop. Degraded-mode publishes on any crash. One-time ~30-day backfill.

> **This revision (2026-07-10) is the implementation-level refresh** against the approved `work/dedup-model-series/user-spec.md` (status: approved). Sections §0, §5, §7 were rewritten to match the approved **tiering / toggle / composition-backstop / fail-safe** design (the prior draft described a superseded pre-tiering, no-soft-flag, any-overlap-blocks design). All `file:line` anchors below were re-verified against the current working tree (line numbers shifted since the last draft — e.g. the gate helper is now at `news_bot.py:933`, the gate wiring at `news_bot.py:2130-2247`).

Cross-referenced against:
- `news_bot.py` (~2540 LoC now; dedup gate wiring at **2130-2247**, helper `_check_cross_source_dedup` at **933-1025**)
- `pending_articles_repo.py` (JSON-column plumbing + migration loop + the fingerprint helpers)
- `admin_alerts.py` (E014/E015/E016 builders at **427-492**)
- `model_extractor.py` (326 LoC; lexicon + regex + `extract_fingerprint` at **223-287** + `similarity`)
- `backfill_fingerprints.py` (307 LoC; SELECT filter at **243-249**)
- `tests/test_model_extractor.py`, `tests/test_integration.py`, `tests/test_pending_articles_repo.py`, `tests/test_migration.py`, `tests/test_backfill_fingerprints.py`, `tests/test_admin_alerts.py`, `tests/fixtures/cross_source_dedup_pairs.py`
- `work/cross-source-dedup/{user-spec,tech-spec,decisions}.md` (the shipped base feature — deployed to TEST only, **prod promotion deferred**)
- `work/MIGRATION-{new-server-2026-06-30,docker-vpn-2026-07-03}.md`, `work/SESSION-2026-07-06.md`, `work/CUTOVER-CHECKLIST-2026-07-06.md` (Moscow cutover; cold-DB evidence, §7.5)

All paths are relative to repo root `/workspaces/debian-2/my-hw/`.

---

## 0. Decided rules (approved user-spec) vs shipped state

| Rule | NEW (model+series) pair-rule | Existing set-overlap rule (unchanged) |
|------|------------------------------|----------------------------------------|
| Enabled | **opt-in, env toggle, default ON** (AC6) | always on |
| Window | **30 days** | 7 days |
| Source scope | **ANY source** (incl. same-source follow-ups) — but only when a series/theme is recognised | cross-source only (shipped code skips same-source, `news_bot.py:990-994`) |
| Match test | share ≥1 `(model+series)` pair | Jaccard ≥0.50 on strict/brand sets |
| Verdict tiers | **distinctive pair → hard block + `[E015]`**; **broad match → soft flag + `[E014]`** (publishes) | block ≥0.50 / soft-flag `[E014]` 0.30-0.49 / pass |
| Fail-safe | **untagged/unknown series → broad tier** (never hard block) | n/a |
| Composition | if NO hard block (no match / no series / toggle off / crash) → **always** run set-overlap backstop (AC5) | n/a |
| Degraded mode | publish on any crash, rate-limited `[E016]` (AC9) | already implemented |
| Backfill | one-time ~30-day, re-select by **missing `pairs` key** (not only `IS NULL`) | one-time 14-day (shipped) |

**The motivating real trio** (must land in the calibration fixture as hard blocks): SDCC 2026 exclusives — Stranger Things / K-Pop Demon Hunters / Top Gun (+ a Porsche) — as **t-hunted PT + autoevolution EN + a same-source "more photos" follow-up**. Two of three miss the current dedup: (a) pop-culture tie-ins produce empty/thin car `strict` → skipped by the AC6 short-circuit (`news_bot.py:975-980`); (b) same-source follow-ups are skipped by the cross-source-only guard (`:990-994`).

**Tiering polarity (the load-bearing safety property, AC7/AC11):** hard block requires a series tagged `distinctive` in the lexicon AND a concrete `strict` model on both sides. Everything else (theme-only pairs, series tagged `broad`, and any series NOT in the lexicon) is broad → soft flag. A brand-new franchise the operator has not yet catalogued can therefore only ever soft-flag, never silently drop a real article.

---

## 1. The existing dedup path, end-to-end (VALID — anchors refreshed)

### 1.1 `model_extractor.py` — extract_fingerprint / similarity / lexicon / regex

Pure module, stdlib `re`+`set` only.

- `Fingerprint = Dict[str, List[str]]` (`:45`) — shape `{'strict': ['toyota 4runner', …], 'brands': ['toyota', …]}`, lists sorted for stable JSON.
- `_LEXICON` (`:69-76`) — `frozenset` of 36 canonical brand keys (**brands only; there is NO series/theme lexicon anywhere in the repo** — greenfield).
- `_BRAND_ALIASES` (`:83-87`) — `chevy→chevrolet`, `vw→volkswagen`, `mercedes-benz→mercedes`.
- `_MODEL_AFTER_BRAND_RE` (`:115-132`) — one compiled case-insensitive pattern, all quantifiers bounded (ReDoS-safe). Optional year prefix dropped. Named groups `brand`, `model`, `model_extra`.
- `_MODEL_EXTRA_KEEP_RE` (`:147`) — keeps only designator-looking extra tokens (all-caps / digit-bearing / hyphenated).
- `_UPPERCASE_BRANDS_RE` (`:165`) — case-sensitive `\b(?P<brand>AMC|BMW|Lotus)\b`.
- `_gather_text(article)` (`:198-216`) — concatenates `title + subtitle + paragraphs`, tolerant of missing keys/None.
- `extract_fingerprint(article) -> Fingerprint` (`:223-287`) — `_gather_text`, empty → `{'strict':[], 'brands':[]}` (`:242`); **Pass 1** = `_MODEL_AFTER_BRAND_RE.finditer(text)` (`:248-276`); **Pass 2** = `_UPPERCASE_BRANDS_RE.finditer(text)` (`:279-282`); returns sorted dict (`:284-287`).
- `similarity(a, b) -> float` (`:290-325`) — guarded two-level Jaccard: AC6 empty guard `:310-311`; AC8 1-token guard `:316-317`; AC8 ≥2-brand gate `:320-321`; AC10 max `:324-325`. `_jaccard` at `:172-184`.

**Extension seam:** `extract_fingerprint` already builds the concatenated body once and runs two `finditer` passes. A **third** `finditer` over the series lexicon (§2.1) is co-located here and enriches the same returned dict.

### 1.2 `_check_cross_source_dedup` — the comparison helper

`news_bot.py:933-1025`. Signature (`:933-934`):
```python
def _check_cross_source_dedup(article, fingerprint, conn, new_source=None)
    -> ('block', match) | ('flag', match) | ('pass', None)
```
- Thresholds `_DEDUP_BLOCK_THRESHOLD = 0.50` (`:925`), `_DEDUP_FLAG_THRESHOLD = 0.30` (`:930`).
- **AC6 short-circuit** (`:975-980`): empty `fingerprint['strict']` → `('pass', None)` with no SQL. **This is exactly the branch that lets pop-culture tie-ins slip through** — must be re-gated on "empty strict AND empty series" (§3.4).
- Candidate set (`:982-985`): `list_recent_pending_fingerprints(conn, 7) + list_recent_published_fingerprints(conn, 7)` — **hardcoded `7`**.
- Same-source skip (`:990-994`): `if new_source and row.get('source_name') == new_source: continue`.
- NULL/malformed candidate skipped (`:995-999`).
- Best-sim loop `:989-1003`; match dict `:1014-1021` (`link, source_name, models, overlap_pct, n_matches, n_total`); block/flag return `:1023-1025`.

### 1.3 Where it is wired into `job()`

`news_bot.py`, fetch loop:
1. `_is_text_only_checklist(entry, article)` → `continue` (`:2123-2128`).
2. **DEDUP GATE** `:2130-2247` — the seam this feature modifies. All inside one broad `try/except Exception` (Decision 12 / AC9).
   - `fp = None` (`:2151`); `new_source = entry.get('source_name') or _resolve_source_name(link)` (`:2152`).
   - `dedup_conn = pending_repo._connect()` (`:2154`, closed in `finally` `:2211-2212`).
   - `fp = model_extractor.extract_fingerprint(article)` (`:2156`).
   - `decision, match = _check_cross_source_dedup(article, fp, dedup_conn, new_source)` (`:2157-2159`).
   - **block** (`:2161-2182`): INFO log, `mark_processed(link, title, pub_date)` (`:2167-2171`), `send_admin_notification(alert_cross_source_blocked(link, match['link'], match['overlap_pct']))` (`:2174-2176`), `continue` (`:2182`).
   - **flag** (`:2184-2209`): `is_pair_rate_limited` (`:2185`) → `alert_cross_source_dupe(...)` (`:2190-2199`) → `mark_pair_pinged` (`:2206`) → `commit` (`:2209`).
   - **pass**: `fp` falls into `row`.
   - **except** (`:2213-2247`): `logger.exception` (`:2219`), 2nd `_connect()`, `is_dedup_degraded_rate_limited` → `alert_dedup_degraded(type(exc).__name__)` (E016) → `mark_dedup_degraded_pinged`, `fp = None` (`:2247`).
3. `row = {…, 'model_fingerprint': fp}` (`:2249-2260`); `insert_pending(row)` (`:2262`).

`mark_processed(link, title, pub_date)` at `news_bot.py:684`. `_resolve_source_name` at `:1203`.

### 1.4 Decision 9 was REVERSED in the shipped code (still true)

The shipped `_check_cross_source_dedup` skips same-source candidates (`:990-994`); `tests/test_integration.py::test_within_source_not_deduped` (`:1171-1232`) pins it. The new pair-rule must compare against **ANY** source (re-restoring any-source, but **only for the pair-rule**). The set-overlap backstop keeps its cross-source-only behaviour. This is what forces the test flip in §6.

---

## 2. Series/theme extraction in `model_extractor.py` (IMPLEMENTATION)

### 2.1 The second `finditer` inside `extract_fingerprint`

`extract_fingerprint` (`:223-287`) already does two passes over `text = _gather_text(article)`. Add a **third pass** and enrich the return dict. Concrete shape:

```python
# after Pass 2 (:282), before the return (:284):
series_tokens: set = set()
for m in _SERIES_RE.finditer(text):          # NEW compiled alternation, §2.3
    alias = _canonical_series(m.group('series'))   # lower + collapse ws + alias-resolve
    if alias:
        series_tokens.add(alias)             # canonical, e.g. 'san diego comic-con'

series = sorted(series_tokens)
pairs  = _build_pairs(strict_tokens, series_tokens)   # §2.2

return {
    'strict': sorted(strict_tokens),
    'brands': sorted(brand_tokens),
    'series': series,        # NEW
    'pairs':  pairs,         # NEW
}
```

Keep the empty-input early return (`:242`) as `{'strict': [], 'brands': [], 'series': [], 'pairs': []}` so every fingerprint carries the four keys (AC8: non-NULL structure). Extraction stays pure — no I/O, no logging; a crash here is caught by the gate's broad `try/except` (§1.3 except) → degraded/publish.

New public helpers (co-located, pure):
```python
def extract_series(article_or_text) -> list[str]        # ['car culture', 'stranger things']
def _build_pairs(strict: set, series: set) -> list[str] # ['porsche 911|san diego comic-con', …]
def shares_pair(fp_a, fp_b) -> tuple[bool, list[str], bool]
    # (any_shared, sorted_shared_pairs, any_distinctive) — distinctive drives block vs flag
```

### 2.2 Fingerprint JSON shape + pair key format + tier tagging

**New shape (extend the existing `model_fingerprint` blob, no new column):**
```json
{
  "strict": ["porsche 911", "subaru legacy gt"],
  "brands": ["porsche", "subaru"],
  "series": ["san diego comic-con", "k-pop demon hunters"],
  "pairs":  ["porsche 911|k-pop demon hunters|D", "*|san diego comic-con|B"]
}
```

**Pair key format** — `"<model>|<series>|<tier>"`:
- Separator `|` is safe: model tokens are `[a-z0-9 -]` (lexicon-controlled brand + `_MODEL_EXTRA_KEEP_RE` designators), series tokens are lexicon-canonical strings, neither can contain `|`.
- `<model>` is a full `strict` token (`"porsche 911"`), NOT brand-only — brand-only keys re-introduce the exact false-positive class the 2026-06-14 same-source reversal removed. When a distinctive series is present but no `strict` model was extracted (pop-culture tie-in with no recognised casting — the SDCC same-source follow-up), emit a **theme-only** key `"*|<series>"`. Theme-only keys are **always broad** (see tier rule below), which is what keeps the fail-safe honest.
- `<tier>` tag (`D` = distinctive, `B` = broad) is derived at extraction time so the gate does a pure set-intersection and reads the tier off the key — no re-lookup in the gate. Alternative (equivalent): omit the tier suffix from the key and have `shares_pair` re-classify each shared pair via the lexicon; carrying the tag in the key is simpler and makes the stored blob self-describing. **Recommend carrying the tag in the key.**

**Tier rule (distinctive vs broad):**
- `distinctive` **iff** the series alias resolves to a lexicon entry tagged `distinctive` **AND** the pair has a concrete `<model>` (not `*`). → key ends `|D`.
- Everything else → `broad`, key ends `|B`: theme-only (`*|…`), a series tagged `broad` in the lexicon, or a series NOT in the lexicon at all (fail-safe — see §2.3, extraction only emits series that ARE in the lexicon, so "not in lexicon" means no series token at all; a mis-tagged/ambiguous entry defaults to `broad` via the lexicon default).

Matching: two fingerprints **share a pair** iff `set(a['pairs']) & set(b['pairs'])` is non-empty. **Hard block** iff any shared pair ends `|D`; else if any shared pair → **soft flag**. (A shared `|D` and a shared `|B` in the same comparison → block wins.)

### 2.3 The tier-tagged lexicon data structure

Model `SERIES_LEXICON` on `_BRAND_ALIASES` (alias→canonical map) but carry the tier:

```python
# model_extractor.py — new module constant
# alias  ->  (canonical, tier)   tier ∈ {'distinctive', 'broad'}
SERIES_LEXICON: dict[str, tuple[str, str]] = {
    # ---- distinctive: concrete franchises / events / limited series ----
    'k-pop demon hunters':  ('k-pop demon hunters', 'distinctive'),
    'kpop demon hunters':   ('k-pop demon hunters', 'distinctive'),
    'stranger things':      ('stranger things',     'distinctive'),
    'top gun':              ('top gun',             'distinctive'),
    'san diego comic-con':  ('san diego comic-con', 'distinctive'),
    'sdcc':                 ('san diego comic-con', 'distinctive'),
    'comic-con':            ('san diego comic-con', 'distinctive'),
    'rlc':                  ('red line club',       'distinctive'),
    'red line club':        ('red line club',       'distinctive'),
    'super treasure hunt':  ('super treasure hunt', 'distinctive'),
    'sth':                  ('super treasure hunt', 'distinctive'),
    # ---- broad: frequent recurrent car-lines / themes ----
    'car culture':          ('car culture',         'broad'),
    'boulevard':            ('boulevard',           'broad'),
    'team transport':       ('team transport',      'broad'),
    'zamac':                ('zamac',               'broad'),
    'pop culture':          ('pop culture',         'broad'),
    'monster trucks':       ('monster trucks',      'broad'),
    'fast & furious':       ('fast and furious',    'broad'),
    # …seeded from the user-spec families; TIER PER ENTRY, default 'broad'
}
_SERIES_DEFAULT_TIER = 'broad'   # fail-safe: any resolvable-but-untagged path → broad
```

- Build the scan regex `_SERIES_RE` from the alias keys as a bounded, ReDoS-safe multiword alternation (mirror `_MODEL_AFTER_BRAND_RE`: sort multi-word aliases first, bound every quantifier). Short/ambiguous aliases (`RLC`, `STH`, `SDCC`, `Zamac`) risk prose collisions — apply the **case-sensitive** discipline the brand extractor uses for `AMC`/`BMW`/`Lotus` (a separate case-sensitive `finditer` pass, `:279`) for the acronym subset.
- `_canonical_series(raw)` mirrors `_canonical_brand` (`:187-195`): lower, collapse whitespace, then `SERIES_LEXICON.get(key)` → returns canonical or `None`. Tier looked up from the same tuple.
- **Fail-safe polarity is enforced two ways:** (1) extraction only emits series that ARE in the lexicon; (2) the lexicon's per-entry default is `broad`, and only the hand-curated distinctive set is `distinctive`. A brand-new franchise not yet in the lexicon → no series token → no pair → can only be caught (or not) by the set-overlap backstop; it can never hard-block.

**Lexicon must be validated by sampling real data (AC7/AC11):** grep `pending_articles.paragraphs` (JSON, has bodies) and `published_articles.title` (titles only — no body column) on the test/prod DB for the seed families to confirm the verbatim strings and casing (the shipped brand lexicon was derived this way — the real Car Culture pair in the fixture is literally "Car Culture Road Trip Mix"). Series names drift each mainline year → the calibration fixture + a monthly review is the guard.

---

## 3. The gate refactor (IMPLEMENTATION)

### 3.1 Recommended dispatch inside `_check_cross_source_dedup`

Rewrite the head of the helper (`news_bot.py:975-1006`) to run the pair-rule **first**, then fall through to the existing set-overlap body **always** when there is no hard block:

```python
def _check_cross_source_dedup(article, fingerprint, conn, new_source=None):
    strict = fingerprint.get('strict') or [] if isinstance(fingerprint, dict) else []
    pairs  = fingerprint.get('pairs')  or [] if isinstance(fingerprint, dict) else []

    # ---- NEW pair-rule (opt-in, tiered, 30-day, ANY source) ----
    if news_bot.DEDUP_SERIES_ENABLED and pairs:          # toggle short-circuit (§5)
        cand30 = (pending_repo.list_recent_pending_fingerprints(conn, 30)
                  + pending_repo.list_recent_published_fingerprints(conn, 30))
        block_match = None
        flag_match  = None
        for row in cand30:                                # NO same-source skip here (any source)
            cfp = row.get('model_fingerprint')
            if not isinstance(cfp, dict):
                continue
            shared = set(pairs) & set(cfp.get('pairs') or [])
            if not shared:
                continue
            if any(p.endswith('|D') for p in shared):
                block_match = _pair_match(row, shared); break     # distinctive → hard block
            flag_match = flag_match or _pair_match(row, shared)   # broad → remember, keep scanning for a D
        if block_match:
            return ('block', block_match)                 # AC3 → E015
        if flag_match:
            return ('flag', flag_match)                   # AC4 → E014 (publishes)
        # pair present but no shared pair → FALL THROUGH to set-overlap backstop

    # ---- AC8 short-circuit: skip SQL only when BOTH are empty ----
    if not strict and not (fingerprint.get('series') or []):
        return ('pass', None)

    # ---- EXISTING set-overlap backstop (7-day, cross-source-only) — unchanged ----
    candidates = (pending_repo.list_recent_pending_fingerprints(conn, 7)
                  + pending_repo.list_recent_published_fingerprints(conn, 7))
    # …best-sim loop with the same-source skip (:990-994) kept as-is…
```

Key points:
1. **Pair-rule FIRST**, 30-day window, **ANY source** (the same-source skip at `:990-994` is NOT applied inside the pair loop — it stays only in the set-overlap loop). AC3 requires same-source follow-ups to be caught.
2. **Tiering verdict:** first distinctive shared pair → `block`; else first broad shared pair → `flag`. Scan-and-remember so a broad match never suppresses a later distinctive one.
3. **Composition backstop (AC5):** when the pair-rule does not hard-block — no shared pair, no series, toggle off — control **always** reaches the existing set-overlap body. The only early `return ('pass',…)` before the backstop is the AC8 both-empty short-circuit. (When the pair-rule returns a soft `flag`, that is a terminal verdict for this article — it publishes with a ping; do not also run set-overlap. AC5 says the backstop runs when there was **no hard block AND no soft flag decision** — practically: pair-rule `pass`-through only.)
4. **Fail-safe:** untagged series never produce a `|D` key (§2.2/§2.3), so the block branch is unreachable for them.
5. **AC6/AC8 re-gate:** the current empty-`strict` short-circuit at `:975-980` must become "empty `strict` AND empty `series`". A pop-culture tie-in has empty `strict` but non-empty `series`/`pairs` and MUST run the pair scan.

### 3.2 One 30-day fetch feeds both rules

30 ⊇ 7. Two clean options; recommend the second:
- (a) issue the 30-day query for the pair loop and the existing 7-day query for the backstop (2 queries, simplest diff), or
- (b) fetch ONCE at 30 days and derive the 7-day subset in Python by comparing `fetched_at`/`published_at` to `now-7d`. Saves one round-trip; ~200 rows at 30 days is a full-scan-fine set (shipped §13.5 established no index needed; each candidate is a cheap set-intersection or one `similarity` call, well under the <100 ms/article budget).

`list_recent_pending_fingerprints(conn, days=7)` (`:835-858`) and `list_recent_published_fingerprints(conn, days=7)` (`:861-877`) already take `days` — passing `30` needs no repo change. Their projection already returns the full deserialised `model_fingerprint` dict, so `row['model_fingerprint']['pairs']` reads for free.

### 3.3 The gate wiring in `job()` and the toggle

- The toggle is read inside `_check_cross_source_dedup` (§3.1) via `news_bot.DEDUP_SERIES_ENABLED` (module attribute → test-monkeypatchable, §5). No change to the `job()` block structure (`:2130-2247`) is needed for the toggle itself.
- Keep the degraded `try/except` + `[E016]` exactly as is (`:2213-2247`). Series extraction lives inside `extract_fingerprint` (`:2156`), already inside the guarded block — a series-extractor crash reuses E016 and publishes (AC9). No new failure surface.
- The `block` branch (`:2161-2182`) must pass the matched **pairs** into the E015 builder (§4). The `flag` branch (`:2184-2209`) is reused verbatim for the broad-tier soft flag (E014) — the pair-rule's `('flag', match)` return feeds the same rate-limited path (`is_pair_rate_limited` → `alert_cross_source_dupe` → `mark_pair_pinged`).

### 3.4 Match-dict shape for the pair-rule

`_pair_match(row, shared_pairs)` builds a dict compatible with both the E015 and E014 branches. For E014 reuse it must carry `link`, `source_name`, and the fields `alert_cross_source_dupe` reads (`overlap_pct`, `n_matches`, `n_total`, `models`). For a pair match, set `models = sorted(shared_pairs)` (the shared pair strings) and either compute a nominal `overlap_pct` or extend the flag builder. Simplest: `n_matches = len(shared)`, `n_total = len(set(a_pairs) | set(b_pairs))`, `overlap_pct = int(round(100*n_matches/n_total))`. E015 uses only `link` + the matched pairs (§4).

---

## 4. Storage / repo (VALID — confirmed, no migration)

### 4.1 Adding a JSON key needs NO migration

`model_fingerprint TEXT` already exists on both tables via the idempotent migration loop `init_schema` (`pending_articles_repo.py:208-219`). Because `pairs`/`series` ride **inside** that one opaque TEXT blob:
- `_PENDING_JSON_COLS` (`:133-134`) and `_PUBLISHED_JSON_COLS` (`:138`) already list `'model_fingerprint'`; `_row_to_dict` (`:160`) auto-deserialises the blob → new keys appear automatically.
- `insert_pending` (`:227-266`) writes `_dumps(entry.get('model_fingerprint'))` (`:262`) — NULL-preserving; new keys serialise for free.
- `move_to_published` (`:578-635`) carries `model_fingerprint` pending→published via SELECT-by-name (`:617`) + INSERT (`:631-635`) — **any key added to the blob is carried for free** (AC2).
- `list_recent_*_fingerprints` (`:835-877`) return the full dict — the gate reads `pairs` with only a `days` arg change.

→ **No `_PENDING_DDL`/`_PUBLISHED_DDL` change, no new ALTER, no schema-pin churn.** A new column would touch DDL + migration loop + `insert_pending` + `move_to_published` + `_row_to_dict` + four schema-pin dicts. Do not.

**Backward-compat contract:** rows written before this feature (and pending rows computed in the 7-day pre-deploy window) have a `model_fingerprint` dict WITHOUT `pairs`/`series`. Both the gate and the extractor must read `row_fp.get('pairs') or []` — never `KeyError`. The 30-day backfill (§5) repopulates published rows.

### 4.2 The 30-day window query

Fetch once at 30 days (`list_recent_*_fingerprints(conn, 30)`) — 30 ⊇ 7, derive the 7-day subset in Python for the backstop (§3.2). No new repo helper required; the `days` param already exists.

### 4.3 `backfill_fingerprints.py` re-select must widen from `IS NULL`

Current SELECT (`backfill_fingerprints.py:243-249`):
```sql
SELECT link, source_name, title, model_fingerprint
FROM published_articles
WHERE published_at >= datetime('now', ? || ' days')
  AND model_fingerprint IS NULL
```
This SKIPS rows that already have an old-shape blob (from any prior 14-day run) → they never gain `pairs`. Widen to also catch rows missing the `pairs` key:
```sql
SELECT link, source_name, title, model_fingerprint
FROM published_articles
WHERE published_at >= datetime('now', ? || ' days')
  AND (model_fingerprint IS NULL
       OR json_extract(model_fingerprint, '$.pairs') IS NULL)
```
- `json_extract` is a static SQL fragment (no user input) → `TestSqlAudit` `?`-placeholder invariant stays green (the only bound param is still `days`). SQLite JSON1 is compiled-in by default on the CPython builds this bot runs (≥3.9); confirm on the prod image once.
- **Also widen the defensive skip guard** in `backfill_one` (`:164`): today it is `if row.get('model_fingerprint') is not None: return 'skipped'`. That guard would re-skip exactly the old-shape rows the widened SELECT is meant to reprocess. Change it to skip only when the blob is present AND already has a `pairs` key, e.g. `fp = row.get('model_fingerprint'); if isinstance(fp, dict) and 'pairs' in fp: return 'skipped'`. (Note the SELECT returns the raw JSON string here, not a deserialised dict — parse or `json_extract`-check consistently.)
- **Idempotency preserved:** a re-run finds all rows already carrying `pairs` → `json_extract(...) IS NOT NULL` → not selected → no-op. `extract_fingerprint` (`:2156`/backfill `:197`) already produces `pairs`/`series` once §2 lands → **no other backfill code change** beyond the SELECT + guard + the operator's `--days 30`. The `--days` clamp `[1,90]` (`_days_in_range`, `:100-112`) already permits 30; default is 14 (`:125`) — operator passes `--days 30`.

---

## 5. Toggle (env var) — IMPLEMENTATION

**No boolean-env precedent exists in the codebase** (all envs are string: `DB_FILE:116`, `HEARTBEAT_FILE:2443-2446`, `TZ:2522`, `INSTANCE_LABEL:105`). Follow the `DB_FILE` pattern: **read once at import as a module constant** (satisfies AC6 "read at start, like `DB_FILE`/`TZ`"), and have the gate reference the **module attribute** so tests can monkeypatch it.

Recommended (place near `DB_FILE`, `news_bot.py:116`):
```python
# Env toggle for the (model+series) pair-rule. Default ON. Any of
# 0/false/no/off (case-insensitive) disables it → only the legacy
# set-overlap dedup runs. Read once at import (AC6, like DB_FILE/TZ);
# the gate reads news_bot.DEDUP_SERIES_ENABLED at call-time so tests
# can monkeypatch without re-import.
DEDUP_SERIES_ENABLED = os.getenv("DEDUP_SERIES_PAIRS", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")
```
- Env var name: **`DEDUP_SERIES_PAIRS`** (or `DEDUP_MODEL_SERIES`; pick one and put it in the deploy runbook + `.env`). Default-ON: unset/blank → enabled.
- Read pattern: **import-time constant** (AC6), but the gate uses `news_bot.DEDUP_SERIES_ENABLED` (attribute access, §3.1) so `monkeypatch.setattr(news_bot, "DEDUP_SERIES_ENABLED", False)` works in tests and the operator's "off + restart" flips it without code change (Scenario 5).
- Safe-rollout (Scenario 8): deploy with toggle **off**, run backfill, watch `[E014]`/logs a couple of days on the freshly-warmed DB, then set the env to on + restart.

---

## 6. Tests to update / add (IMPLEMENTATION)

| Test file | Change |
|-----------|--------|
| `tests/test_model_extractor.py` | Add `TestExtractSeries` (lexicon hits; aliases SDCC/STH/RLC/KPop; case-sensitive acronym guard; prose false-positives e.g. "comic con" lowercase). Add `TestPairs` (`_build_pairs` cartesian, theme-only `*|series`, tier suffix `|D`/`|B`) + `shares_pair` (distinctive→block, broad→flag). Extend calibration harness (`_classify` `:323`, `test_calibration_accuracy` `:332`) to also assert pair-tier verdicts; `test_calibration_real_pair_must_pass` (`:363`) still holds. |
| `tests/test_integration.py::TestCrossSourceDedup` (`:860`) | Add: distinctive-pair hard block **cross-source** AND **same-source follow-up** (both → E015, `mark_processed`, no pending row); broad-pair **soft flag** (publishes + E014, rate-limited); "series recognised, no shared pair → falls through to set-overlap backstop"; "no series → set-overlap unchanged" (AC5); toggle-off → pair-rule skipped, only set-overlap; degraded (extractor raises → E016 + publish). **Flip `test_within_source_not_deduped` (`:1171-1232`)**: split into (a) same-source, NO series → still publishes (backstop is cross-source-only) and (b) same-source, shared distinctive pair → **blocked**. `test_empty_fingerprint` (`:1240`) must change: a series-only (empty `strict`) article now runs the pair scan — keep an both-empty (no strict, no series) case that still passes. `TestFingerprintCarryThrough` (`:1339`) — assert `pairs`/`series` survive `move_to_published`. |
| `tests/test_pending_articles_repo.py` | Blob-extension path → `EXPECTED_PENDING`/`EXPECTED_PUBLISHED` UNCHANGED (still one `model_fingerprint TEXT`); schema-pin (`_PENDING_JSON_COLS` test) stays green. Add roundtrip: `pairs`/`series` survive `insert_pending`→`get_pending` and `move_to_published`. |
| `tests/test_migration.py` | Blob-extension → `EXPECTED_PENDING_COLUMNS`/`EXPECTED_PUBLISHED_COLUMNS` UNCHANGED (no new column). No migration test change unless a column is added (it should not be). |
| `tests/test_backfill_fingerprints.py` | Add: `--days 30` window honoured; `pairs`/`series` populated on backfilled rows; **re-upgrade** — a row with an old-shape blob (no `pairs`) is re-selected and upgraded, a row already carrying `pairs` is skipped (idempotency of the widened SELECT + guard, §4.3). |
| `tests/test_admin_alerts.py` | Add a test for the extended E015 series-block builder: renders the matched pair(s) + earlier link; keeps the `Заблокирован дубль` substring anchor. |
| `tests/fixtures/cross_source_dedup_pairs.py` | Add the **3 real SDCC dupes** as hard-block pairs (shared distinctive pair): t-hunted PT ↔ autoevolution EN (Stranger Things / K-Pop Demon Hunters / Top Gun + Porsche) AND a same-source "more photos" t-hunted follow-up. Add **not-dupes**: (a) same car in a DIFFERENT series → not a block; (b) same-source near-miss (different cars, same broad car-line e.g. two Car Culture mixes sharing one casting) → soft flag, NOT hard block; (c) theme-only Stranger Things "mainline vs SDCC" (no shared concrete model) → soft flag, not silent block. Reuse the `DUPE_PAIRS`/`NON_DUPE_PAIRS` structure (`:37`, `:221`) + the `expected_verdict` field (extend it to `'soft-flag'`, already anticipated in the fixture docstring `:16`). Classifier floor ≥7/8 (AC11). |

---

## 7. Risks & feasibility (REFRESHED to the tiered design)

### 7.1 Over-blocking is bounded by tiering + toggle (was the dominant risk; now mitigated by design)
The prior draft treated "block on ANY shared pair" as the dominant risk. The **approved design removes that**: only a **distinctive** pair (curated franchise/event/limited-series + a concrete `strict` model on both sides) can hard-block; broad car-lines (Car Culture, Boulevard…) and theme-only matches soft-flag and **publish**. So a legit new Car Culture drop that re-uses one casting from a 29-day-old Car Culture article → **soft flag, not block** (broad tier). Residual over-block risk narrows to: a distinctive-tagged franchise genuinely getting a legitimate second product with the *same concrete model* inside 30 days — visible via `[E015]`, reversible via the toggle. Mitigations: precise `model|series` keys (never brand-only), curated distinctive set, `[E015]` tripwire, calibration not-dupes (§6), and the env toggle (instant kill-switch). There is no manual re-publish path and no per-subscriber recovery, so a false **hard** block is only visible via the ping — calibration of the *distinctive* set is load-bearing.

### 7.2 Fail-safe polarity (must not be inverted)
The single most important correctness property: **unknown/untagged series must default to broad, never distinctive.** Enforced by (a) extraction emitting only lexicon series and (b) `_SERIES_DEFAULT_TIER = 'broad'`. A test must assert that a series present in text but tagged `broad` (or resolvable via an ambiguous path) produces a `|B` pair and can only soft-flag.

### 7.3 Pop-culture tie-in with no car model
The headline case (SDCC franchises) has empty `strict`. The pair extractor MUST emit theme-only `*|<series>` keys and the gate MUST NOT short-circuit on empty `strict` alone (§3.1 re-gates AC6 on empty strict AND empty series). Theme-only keys are broad → the SDCC same-source follow-up soft-flags unless it *also* shares a concrete `model|series` distinctive pair (which the Porsche K-Pop Demon Hunters case does → hard block, Scenario 1).

### 7.4 Same-source semantics + performance
Any-source is safe now because the pair key is `model|series` (not brand-only) — the F-100/Porsche brand collision that motivated the 2026-06-14 reversal cannot form a shared pair. Performance: 30-day scan ≈ ~200 rows, one set-intersection each; within the shipped <100 ms/article budget. Fetch once at 30 days (§3.2).

### 7.5 Cold prod DB — CONFIRMED likely; backfill almost certainly never ran on Moscow prod
Evidence gathered:
- `work/cross-source-dedup/decisions.md:38,41,42` — cross-source-dedup was deployed to the **TEST instance only**; **prod promotion DEFERRED** (operator gate). Prod stayed at `a306e14` (Mattel-disable only), i.e. **pre-fingerprint**.
- `work/cross-source-dedup/decisions.md:61,63` — the prod backfill run + the `SELECT COUNT(*) … WHERE model_fingerprint IS NOT NULL` check were explicitly listed as **deferred-to-post-deploy / not verifiable** (never executed).
- `work/CUTOVER-CHECKLIST-2026-07-06.md:88-89` + `work/SESSION-2026-07-06.md:122-127` — the Moscow prod DB was `scp`'d from the **old NL prod** `news.db` (1.19 MB, clean single-file snapshot), i.e. the pre-feature snapshot.
- `work/MIGRATION-*.md` — **no mention of backfill or fingerprint** anywhere.
→ Conclusion: the current Moscow prod `news.db` is **cold** — either the `model_fingerprint` column is all-NULL (if the deployed code's `init_schema` has since added it) or the column does not exist yet (if the running branch predates the ALTER). Both are handled by the mandatory **pre-deploy check** `SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL` (expect **~0**; a "no such column" error is itself the confirming signal that the ALTER has not run). The `backfill_fingerprints.py --days 30` run after deploy warms both the legacy car-fingerprint AND the new pairs.

### 7.6 Degraded-mode reuse
Series extraction inside `extract_fingerprint` runs inside the gate's broad `try/except` (`:2213`); any crash reuses `[E016]` and publishes. No new failure surface.

---

## 8. Deploy / files (VALID — confirmed)

- `model_extractor.py`, `admin_alerts.py`, `backfill_fingerprints.py` are ALL already in the three FILES arrays: `deploy.sh:54,56,57` + `.github/workflows/deploy.yml:140,142,143` + `.github/workflows/deploy_test.yml:111,113,114`. Keeping all new logic inside these three modules (+ the fixture) → **no FILES-drift, no ImportError crashloop** (the cross-source-dedup Task 10 incident). Do **not** create a `series_extractor.py`.
- **Pre-deploy** (§7.5): `SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL` on Moscow prod (expect ~0 / "no such column").
- **Deploy**: `git pull && docker compose up -d --build` on the Moscow host, **outside 10:00–20:00 МСК** (restart resets the in-process daily schedule). Schema unchanged (blob extension only).
- **Post-deploy**: operator runs `python3 backfill_fingerprints.py --days 30` once (warms legacy fingerprint + new pairs). Recommended safe rollout: deploy toggle **off**, backfill, observe, then toggle **on** + restart.

### Files to touch (summary)
| File | Change |
|------|--------|
| `model_extractor.py` | `SERIES_LEXICON` (alias→(canonical,tier)) + `_SERIES_RE` + case-sensitive acronym pass + `_canonical_series` + `extract_series` + `_build_pairs` + `shares_pair`; enrich `extract_fingerprint` return with `series`+`pairs` (+ empty-return shape). |
| `news_bot.py` | `DEDUP_SERIES_ENABLED` toggle constant (near `:116`); refactor `_check_cross_source_dedup` (`:933-1025`) — pair-rule first (30-day, any-source, tiered block/flag), re-gate AC6 short-circuit on empty strict AND series, always fall through to set-overlap backstop; block branch (`:2174-2176`) passes matched pairs to E015. |
| `admin_alerts.py` | Extend `alert_cross_source_blocked` (E015, `:461-472`) to render matched pair(s) + earlier link (keep `Заблокирован дубль` anchor `:464/468`); E014 (`:427-455`) reused as-is; E016 (`:478-492`) unchanged. |
| `backfill_fingerprints.py` | Widen SELECT (`:243-249`) to `… OR json_extract(model_fingerprint,'$.pairs') IS NULL`; widen `backfill_one` skip guard (`:164`) to also require the `pairs` key; operator runs `--days 30`. |
| `tests/*` + `tests/fixtures/cross_source_dedup_pairs.py` | Per §6. |

---

## 9. Suggested wave / task breakdown

- **Wave 1 (pure, parallel):**
  - T1 `model_extractor.py` — `SERIES_LEXICON` + regex + `extract_series`/`_build_pairs`/`shares_pair` + enrich `extract_fingerprint`; `TestExtractSeries`/`TestPairs`. Validate lexicon by sampling real DB (§2.3).
  - T2 `admin_alerts.py` — extend E015 with matched pairs + earlier link; `test_admin_alerts` case.
  - T3 `tests/fixtures/cross_source_dedup_pairs.py` — add 3 SDCC dupes + 3 not-dupes; extend calibration harness.
- **Wave 2 (depends on T1):**
  - T4 `news_bot.py` — toggle constant + `_check_cross_source_dedup` tiered refactor + gate block-branch wiring; `TestCrossSourceDedup` scenarios + flip `test_within_source_not_deduped` + re-gate `test_empty_fingerprint`.
  - T5 `backfill_fingerprints.py` — widened SELECT + guard; `test_backfill_fingerprints` re-upgrade cases.
- **Wave 3:** calibration/QA (≥7/8), pre-deploy `SELECT COUNT` runbook line, deploy doc (toggle-off → backfill → toggle-on).

**Feasibility:** S/M feature — no new dependency, no LLM, no schema migration, no new top-level file. The two behaviour-reversing edits (any-source pair loop + `test_within_source_not_deduped` flip) and the fail-safe polarity are the highest-risk spots; all are test-pinned above.
