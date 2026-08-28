# Code Research: dedup-broad-precision

**Date:** 2026-08-19
**Scope:** the broad-pair (`|B`) branch of the cross-source dedup gate — its precision problem, and what would have to change to require the shared series to be *declared* in both articles' subject area.
**Method:** static read of `dev` + live execution of the extractor/alert builders to verify behaviour claims.

> This document reports **what exists**. No implementation plan, no tech-spec.

---

## 0. Executive findings (read this first)

| # | Finding | Impact |
|---|---------|--------|
| F1 | `_check_cross_source_dedup` **already receives the `article` dict** and never uses it. The new-article side has `title` + `paragraphs` available at verdict time with **zero plumbing**. | New side: solved. |
| F2 | The candidate fetch **already SELECTs `title`** from both `pending_articles` and `published_articles`. The candidate title is available today, **no schema and no query change**. | Candidate title: solved. |
| F3 | `published_articles` **has no `paragraphs` column at all** (`_PUBLISHED_DDL`, pending_articles_repo.py:95-107). The first-paragraph *fallback* is therefore impossible for published candidates without either a migration or a fingerprint-format change. | This is the ONE real design fork. |
| F4 | `extract_series(text)` (model_extractor.py:711) already accepts a **raw string** and returns canonical series. It is the ready-made "is series X named in this text" primitive — and today it has **no production caller** (tests only). | Matching primitive: exists. |
| F5 | Portuguese titles resolve correctly **right now** — verified live: `'Novo lote da série Car Culture'` → `['car culture']`. HW line names are not translated in PT, and `_SERIES_RE` is case-insensitive. **No test pins this.** | No new matching machinery needed. |
| F6 | **Contradiction with the brief:** the `[E014]` builder **already** renders «Совпавшая серия/тема» (not «Совпадение моделей: N%») whenever `pairs` is non-empty — and broad-pair flags always carry `pairs`. See §6.1 for what the operator is actually seeing. | The requested text change is partly already shipped. |
| F7 | The 0–7% overlap signature is **arithmetically reproducible** from `_pair_match` and confirms the diagnosis independently. See §1.3. | Root cause corroborated. |
| F8 | `backfill_fingerprints._already_backfilled` keys idempotency on the presence of `$.pairs`. A new fingerprint key would be **silently skipped** on every existing row. | Blocking risk for the fingerprint-format option. |

---

## 1. Entry points — the code that must change

### 1.1 `news_bot.py` — the gate

| Symbol | Line | Role |
|--------|------|------|
| `DEDUP_SERIES_ENABLED` | 135 | env toggle, default ON. Gates Rule 1 only. |
| `_DEDUP_BLOCK_THRESHOLD = 0.50` | 2629 | backstop hard block |
| `_DEDUP_DEFER_HOURS = 24` | 2639 | soft-flag publish defer |
| `_DEDUP_FLAG_THRESHOLD = 0.30` | 2644 | backstop soft flag |
| `_DEDUP_PAIR_WINDOW_DAYS = 30` | 2650 | pair-rule candidate window |
| `_DEDUP_BACKSTOP_WINDOW_DAYS = 7` | 2655 | backstop subset window |
| `_fetch_dedup_candidates(conn)` | 2658 | single 30-day fetch, pending + published |
| `_pair_match(row, shared_pairs, n_total)` | 2674 | builds the `match` dict |
| `_pair_rule_verdict(pairs, candidates)` | 2698 | **the broad-pair branch — the bug** |
| `_set_overlap_backstop_verdict(...)` | 2736 | legacy Jaccard backstop — **stays untouched** |
| `_check_cross_source_dedup(article, fingerprint, conn, new_source)` | 2796 | orchestrator |
| gate call site in `job()` | 4391-4393 | `decision, match = _check_cross_source_dedup(article, fp, dedup_conn, new_source)` |
| `[E014]` flag branch | 4424-4501 | defer stamp, rate-limit, token mint, send |
| `[E016]` fail-open handler | 4505+ | `except Exception` → degraded, publish anyway |

**Signature note (F1).** `_check_cross_source_dedup` takes `article: dict` as its **first parameter and never reads it** — verified by scanning the function body (news_bot.py:2796-2895): every `article` occurrence inside is in the docstring. The parameter is dead weight today and is exactly the hook the new rule needs.

### 1.2 The broad-pair branch, verbatim

`_pair_rule_verdict` (news_bot.py:2698-2734). The offending lines:

```python
        if any(p.endswith('|D') for p in shared):
            return ('block', _pair_match(row, shared_sorted, n_total))
        if flag_match is None:
            # First broad match — remember but KEEP scanning ...
            flag_match = _pair_match(row, shared_sorted, n_total)
```

There is **no threshold, no count test, no context test** on the broad branch. `shared` non-empty + no `|D` ⇒ soft flag. One shared `"<model>|<series>|B"` key is sufficient. This matches the brief exactly.

Note the scan is over **all** 30-day candidates with **no same-source skip** (deliberate, for the `|D` "more photos" case) — so the broad branch inherits a wider net than the legacy backstop, which *is* cross-source-only (news_bot.py:2765-2769).

### 1.3 Why round-ups poison it — and why the flags show 0–7%

`_build_pairs` (model_extractor.py:533-563) is a **cartesian product**: `len(strict) × len(series)` keys.

Verified live on a synthetic round-up (10 castings + 4 broad lines):

```
strict: 9  series: ['boulevard', 'car culture', 'red line club', 'team transport']
pairs: 36
```

A real «Unboxing: 10 Affordable Cars» reaching 296 pairs is consistent with this (more castings × more lines).

`_pair_match` (news_bot.py:2685-2686) computes:

```python
    n_matches = len(shared_pairs)
    overlap_pct = int(round(100 * n_matches / n_total)) if n_total else 0
```

where `n_total = len(new_pairs | cand_pairs)`. For **one** shared pair against a round-up, reproduced live:

| round-up pairs | other article pairs | reported `overlap_pct` |
|---|---|---|
| 296 | 20 | **0 %** |
| 296 | 5 | **0 %** |
| 56 | 12 | **1 %** |
| 30 | 10 | **3 %** |
| 15 | 12 | **4 %** |

This independently confirms the brief's forensic claim: a value in 0–7% **can only** come from the broad-pair branch, because the backstop cannot emit below 30% by construction (`_DEDUP_FLAG_THRESHOLD`, news_bot.py:2644, and `overlap_pct = int(round(best_sim * 100))` at news_bot.py:2786). The percentage is not merely "meaningless" — it is a **pair-set overlap**, a different quantity from the model overlap the same field carries on the backstop path. One field, two incompatible units.

### 1.4 `model_extractor.py` — fingerprint & tier construction

| Symbol | Line | Role |
|--------|------|------|
| `SERIES_LEXICON` | 138-172 | alias → `(canonical, tier)`. 10 canonicals; 4 distinctive, 6 broad. |
| `_SERIES_DEFAULT_TIER = 'broad'` | 175 | fail-safe polarity |
| load-time asserts (pipe/newline, tier consistency) | 179-197 | pair-key integrity |
| `_TIER_SUFFIX` / `_tier_suffix()` | 201-206 | tier → `D`/`B` |
| `_RECURRING_PROGRAMS` | 224 | `{super treasure hunt, red line club}`, asserted broad |
| `_theme_only_eligible(canonical, tier)` | 244 | gates the `*|<series>|B` theme-only key |
| `_ACRONYM_ALIASES` | 409 | `{sdcc, rlc, sth, zamac}` |
| `_CASE_INSENSITIVE_ACRONYMS` | 418 | `{zamac}` only |
| `_SERIES_RE` | 437 | CI alternation, longest-first, built from lexicon |
| `_SERIES_ACRONYM_RE` | 468 | case-sensitive acronyms (+ scoped `(?i:zamac)`) |
| `_canonical_series(raw)` | 503 | matched string → `(canonical, tier)` |
| `_scan_series(text)` | 517 | **text → set of `(canonical, tier)`** |
| `_build_pairs(strict, series_with_tier)` | 533 | the cartesian product |
| `_pass3_series(text, pair_models)` | 566 | series + pairs from one text |
| `_gather_text(article)` | 592 | **title + subtitle + all paragraphs**, `'\n'.join` |
| `extract_fingerprint(article)` | 617 | → `{strict, brands, series, pairs}` |
| `extract_series(article_or_text)` | 711 | **accepts a raw string** |
| `shares_pair(a, b)` | 723 | pure set intersection + `|D` probe |
| `similarity(a, b)` | 742 | backstop Jaccard — untouched |

**Pair key format:** `"<model>|<series>|<tier>"`, tier ∈ `{D, B}`; theme-only variant `"*|<series>|B"`. `|` is guaranteed safe by the load-time assert at model_extractor.py:179-183.

**Crucially, `_gather_text` flattens title and body into one string** (model_extractor.py:592-615). The fingerprint therefore **cannot distinguish** a series named in the headline from one mentioned in paragraph 14. That erasure is the mechanical root of the false-positive class — the information the new rule needs is destroyed at extraction time.

### 1.5 `admin_alerts.py` — the E014 builder

| Symbol | Line | Role |
|--------|------|------|
| `_render_pair(raw)` | 772 | `'audi sport|car culture|B'` → `'audi sport + car culture'`; strips tier; `*` → series alone |
| `_render_pairs_block(pairs)` | 790 | sorted, one per line |
| `alert_cross_source_dupe(...)` | 799 | **the E014 builder** |
| `build_dedup_review_keyboard(token)` | 878 | the two-button keyboard |
| `alert_cross_source_blocked(...)` | 901 | E015, same `pairs`/legacy fork |
| `alert_dedup_degraded(reason)` | 937 | E016 |

The branch at admin_alerts.py:823-834:

```python
    if pairs:
        match_block = "Совпавшая серия/тема:\n" + _render_pairs_block(pairs)
    elif overlap_pct is not None:
        model_list = "\n".join(models or [])
        match_block = (
            f"Совпадение моделей: {overlap_pct}% ({n_matches}/{n_total})\n"
            f"Общие модели:\n{model_list}"
        )
```

Send site: news_bot.py:4473-4491 passes `pairs=match.get('pairs')`, and `_pair_match` **always** sets `pairs`. See §6.1 for the consequence.

---

## 2. CRITICAL DATA QUESTION — title & first paragraph at verdict time

### 2.1 New-article side: fully available, already in scope

`article = fetch_full_article(entry)` at news_bot.py:4147. Every source fetcher returns `{'title', 'subtitle', 'paragraphs', 'images', 'blocks'}` (news_bot.py:3697-3745). That dict is passed straight into the gate at news_bot.py:4391-4393 and, as established in F1, is **ignored**.

**Verdict: new side needs no data work at all.** `article['title']` and `article['paragraphs'][0]` are in hand.

### 2.2 Candidate side: title YES, first paragraph NO (for published)

`_fetch_dedup_candidates` (news_bot.py:2658-2672) concatenates two repo calls:

**`list_recent_pending_fingerprints`** — pending_articles_repo.py:1518-1541:
```sql
SELECT link, source_name, title, model_fingerprint, fetched_at
FROM pending_articles
WHERE fetched_at >= datetime('now', ? || ' days')
```

**`list_recent_published_fingerprints`** — pending_articles_repo.py:1544-1560:
```sql
SELECT link, source_name, title, model_fingerprint, published_at
FROM published_articles
WHERE published_at >= datetime('now', ? || ' days')
```

Both **already project `title`**. The candidate `row` dicts reaching `_pair_rule_verdict` carry `link`, `source_name`, `title`, `model_fingerprint`, and a timestamp.

Column availability in the underlying tables:

| Column | `pending_articles` | `published_articles` |
|--------|--------------------|----------------------|
| `title` (original EN/PT) | ✅ `TEXT NOT NULL` (repo:75) | ✅ `TEXT NOT NULL` (repo:98) |
| `paragraphs` (JSON list) | ✅ `TEXT NOT NULL` (repo:77) — **but NOT selected** | ❌ **column does not exist** |
| `model_fingerprint` | ✅ (migration, repo:317-318) | ✅ (migration, repo:319-320) |

`move_to_published` (pending_articles_repo.py:1185; INSERT at 1277-1287) carries only `link, title, ru_title, telegraph_url, telegraph_path, source_name, via_review, model_fingerprint` across. **Paragraphs are dropped when an article is published** — by design, the body lives on Telegra.ph afterwards.

Note `title` is the **original-language** title (PT for t-hunted, EN for autoevolution), not `ru_title`. That is the right field: HW line names survive untranslated in PT (§3), whereas the RU transcreation may alter them.

### 2.3 The two options, with evidence

#### Option A — read the subject area from the DB at verdict time

*New side:* from `article` (free).
*Candidate side:* `title` from the existing projection (free).
*Candidate first-paragraph fallback:*

- `pending_articles`: add `paragraphs` to the SELECT at pending_articles_repo.py:1534. `paragraphs` is already in `_PENDING_JSON_COLS` (repo:162-163), so `_row_to_dict` would deserialise it automatically. **Cheap — one column in one query.**
- `published_articles`: **impossible without a migration.** No column exists, and the data was discarded at `move_to_published`. A migration would add an empty column — historical rows could never be populated (bodies are gone; only a re-fetch via `fetch_full_article` could recover them, which is what `backfill_fingerprints.py` does and which autoevolution answers with Cloudflare 403s — see backfill_fingerprints.py:237-247).

*Consequence:* the first-paragraph fallback would be **asymmetric** — available for pending candidates, absent for published ones. Since a published candidate is the more common match target over a 30-day window, this weakens the rule exactly where it is used most.

*Cost:* +1 column in one query; row memory grows by the full body text of every pending row in the 30-day window (~200 rows per the docstring at news_bot.py:2658-2666).

*Test coupling:* a `published_articles` migration would have to update the schema pin `test_migration.py:175 test_published_articles_has_expected_columns` (and :159 for pending). `TestListRecentFingerprints` (test_pending_articles_repo.py:1078) pins the projection shape of both fetch helpers.

#### Option B — store the declared series inside `model_fingerprint` at extraction time

Add a key (e.g. `declared`) to the fingerprint alongside `strict/brands/series/pairs`, computed at `extract_fingerprint` time by running `_scan_series` over title (and first paragraph as fallback) **separately** from the full-body scan.

*Evidence it fits:*
- Symmetric: works identically for pending and published candidates.
- Survives `move_to_published` — the fingerprint blob is carried across verbatim (repo:1277-1287).
- No schema change: `model_fingerprint` is a JSON TEXT blob on both tables.
- Back-compat reads are already the house pattern: every consumer uses `fp.get('pairs') or []` (news_bot.py:2837-2839, model_extractor.py:736-737), so an absent `declared` key cannot `KeyError`.
- `_gather_text` would stay as-is for `strict`/`series`/`pairs`; only a second, narrower scan is added.

*Evidence against / cost:*
- **Backfill blocker (F8).** `_already_backfilled` (backfill_fingerprints.py:164-185) returns True when the blob is a dict containing `pairs`, and the SELECT at backfill_fingerprints.py:313-320 filters on `json_extract(model_fingerprint, '$.pairs') IS NULL`. **Both** the SQL predicate and the Python guard would skip every already-populated row, so no existing row would ever gain the new key without editing both.
- Every historical row is a "no declared data" row until re-fetched, so the gate needs an explicit policy for the absent key (see §6.3).
- Re-fetching to backfill is unreliable: autoevolution returns 403 on bulk re-fetch, which the script deliberately treats as `unreachable` and does not persist (backfill_fingerprints.py:237-253).

#### Option C — hybrid (worth naming)

Title-only from the DB for the **candidate** side (free, symmetric, works today), first-paragraph fallback only for the **new** article. This needs no migration, no fingerprint change, and no backfill — at the cost of a stricter rule on the candidate side than on the new side. Whether that still clears the corpus (3 real dupes kept / 20 of 21 false flags removed) is **not determinable from the code** and would need re-running the operator's corpus analysis with title-only on the candidate side.

---

## 3. Series-name matching in text (incl. Portuguese)

### 3.1 What exists

`_scan_series(text)` (model_extractor.py:517-531) runs two compiled passes and returns `{(canonical, tier)}`:

1. `_SERIES_RE` (model_extractor.py:437) — `re.IGNORECASE` alternation over all non-acronym aliases, sorted **longest-first** so `san diego comic-con` wins over `comic-con`. Built programmatically from `SERIES_LEXICON` keys, so it cannot drift.
2. `_SERIES_ACRONYM_RE` (model_extractor.py:468) — compiled **without** global `re.I`. Per-branch casing from `_acronym_to_pattern` (model_extractor.py:442-461): `zamac` gets a scoped `(?i:...)`; `sdcc`/`rlc`/`sth` match **uppercase only**, so prose "sth"/"rlc" is not a match.

`_alias_to_pattern` (model_extractor.py:421-428) relaxes literal spaces to bounded `\s{1,3}` — which tolerates a single newline, deliberately, because `_gather_text` joins with `\n`.

Public wrapper: **`extract_series(article_or_text)`** at model_extractor.py:711-721 — takes *either* an article dict *or* a raw string, returns sorted canonical names. This is precisely the "is series X named in this text" primitive the new rule needs.

### 3.2 Portuguese — verified live, works today

Hot Wheels line names are **brand nouns and are not translated** in the t-hunted PT source; only the surrounding prose is Portuguese. Since `_SERIES_RE` is case-insensitive and anchored on `\b`, PT titles resolve. Executed against the real module on `dev`:

| PT input | `extract_series()` output |
|---|---|
| `Novo lote da série Car Culture` | `['car culture']` |
| `Mais fotos do lote da série Boulevard` | `['boulevard']` |
| `Novo lote da série Team Transport chega` | `['team transport']` |
| `Red Line Club: novo Audi` | `['red line club']` |
| `Hot Wheels Super Treasure Hunt` | `['super treasure hunt']` |

**No new matching machinery, no PT aliases, no translation step is required.**

### 3.3 Gaps to note

- **No test pins PT behaviour.** The parametrised lexicon test (tests/test_model_extractor.py:602-618) is English-only (`'New Top Gun Maverick set revealed'`, `'Monster Trucks bash event'`, …). PT resolution is currently an *emergent* property with no regression guard — the exact property the new rule would depend on.
- **`extract_series` has no production caller.** Confirmed by grep across `*.py` (excluding venv): every call site is in `tests/test_model_extractor.py`. Adopting it in the gate makes a test-only API load-bearing in prod.
- **Uppercase-only acronyms cut both ways.** A PT title «Novo lote RLC» matches; «novo lote rlc» does not. For a *precision* rule this under-match is the safe direction (fewer flags), but it is a behavioural asymmetry worth knowing.
- Real PT fixtures already exist on disk and exercise these exact phrasings: `tests/fixtures/articles/t_hunted/mais-um-lote-da-serie-boulevard-de-2026.html`, `mais-um-novo-lote-da-serie-pop-culture.html`, `a-volta-do-audi-quattro-para-o-red-line.html`, `o-penultimo-lote-de-2026-veja-as.html`.

---

## 4. Existing tests that pin current behaviour

**Baseline: full suite is green — `1970 passed, 2 skipped, 517 subtests` in 235 s** (run on `dev`, 2026-08-19).

**Framework:** pytest (`requirements-dev.txt`: `pytest>=8.0.0,<10.0.0`, `freezegun>=1.5.0`). **No `pytest.ini` / `setup.cfg` / `pyproject.toml` / `tox.ini` anywhere** — default discovery, no registered markers, no `addopts`. `tests/conftest.py` is 13 lines of `sys.path` bootstrap and defines **zero fixtures**; all DB setup is per-file. Style is mixed: `unittest.TestCase` classes run under pytest alongside bare pytest functions.
CI: `.github/workflows/ci.yml` → `python -m pytest tests/ -v` on Python 3.13, push/PR to `main`/`dev`, skipped when only docs changed. Local: `pytest` (README.md:76).

### 4.1 Files touching dedup

| File | Role | Key classes |
|---|---|---|
| `tests/test_integration.py` | **The gate end-to-end through `job()`** — the primary pin (93 dedup mentions) | `TestCrossSourceDedup` :1059, `TestFingerprintCarryThrough` :2743, `TestResolveDedupCallback` :3099, `TestInFlightCancelGuard` :3450, `TestDedupReviewButtons` :3523, `TestReviewListener` :3847 |
| `tests/test_model_extractor.py` | Unit + the 9-pair calibration harness | `TestExtractFingerprint` :63, `TestSimilarity` :218, `test_calibration_*` :390/:429/:458/:483, `TestSeriesLexicon` :520, `TestExtractSeries` :575, `TestPairs` :672, `TestSharesPair` :874 |
| `tests/test_admin_alerts.py` | E014/E015/E016 **text builders** | `TestAdminAlerts` :59 (dedup block :310-602), `TestDedupReviewKeyboard` :671 |
| `tests/test_pending_articles_repo.py` | storage / rate-limit / defer primitives | `TestListRecentFingerprints` :1078, `TestPairRateLimit` :1238, `TestDedupDegradedRateLimit` :1312, `TestPublishAfterDefer` :2082 |
| `tests/test_backfill_fingerprints.py` | backfill, 4-key shape incl. `pairs` | 15 module-level tests :202-601 |
| `tests/test_migration.py` | **schema pins** | `test_pending_articles_has_expected_columns` :159, `test_published_articles_has_expected_columns` :175 |
| `tests/test_no_token_leak_in_logs.py` | full E014 body survives logging | `test_send_admin_notification_logs_full_message_not_truncated` (literal at :666) |

### 4.2 THE breakage mechanism — `_seed_published`

The integration helper `_seed_published` (test_integration.py:1100) inserts a candidate through the real prod path (`insert_pending` → `update_staged` → `move_to_published`) but **hardcodes `title='Existing Article'`, `paragraphs=['Body.']`** while injecting a hand-written `pairs` list.

So every integration soft-flag test seeds a candidate whose **fingerprint claims a series that its title and body never mention.** Under a "series must be declared in the subject area of BOTH articles" rule, all of these stop flagging. This is the single highest-volume test change in the feature.

Compounding it: the candidate row projection carries no paragraphs at all (§2.2), so **a both-sides title/first-paragraph rule is not even expressible against these seeded rows** without the schema/query decision in §2.3.

### 4.3 Tier A — tests that WILL BREAK (integration)

| Test | Line | Seeded pair | Load-bearing assertion |
|---|---|---|---|
| `test_broad_pair_soft_flag_is_terminal` | :1313 | `['toyota supra\|car culture\|B']` (:1327) | `self.assertEqual(len(e014_calls), 1, ...)` :1371 — **the canonical pin**; plus `assertNotIn('[E015]', m)` :1375-1377 (the seed has 100 % strict overlap, so the backstop WOULD block if the flag stopped firing — see §6.7) |
| `test_soft_flag_logged_even_when_alert_rate_limited` | :1393 | same (:1410) | `rl_lines = [l for l in cm.output if '[E014]' in l and new_link in l and 'rate-limited' in l]` :1444-1447 |
| `test_soft_flag_defers_publication_by_a_day` | :2440 | `['*\|k-pop demon hunters\|B']` (:2452) | `assertGreater(delta, timedelta(hours=23))` :2478 |
| `test_theme_only_pop_culture_flags_no_model` | :2517 | `['*\|k-pop demon hunters\|B']` (:2536) | `assertEqual(len(e014_calls), 1)` :2576; `assertIn('k-pop demon hunters', e014_calls[0].args[0])` :2580 |
| `TestDedupReviewButtons` (whole class, 4 tests) | :3523 | `SOFT_FP` = `['toyota supra\|car culture\|B']` (:3548) | all ride on an E014 firing: :3676, :3731, :3779, :3830 |

Also in the blast radius: `test_soft_flag_path` :1891, `test_soft_flag_rate_limited` :1977, `test_keep_lifts_the_soft_flag_deferral` :3350 (and :3375/:3390/:3418), `test_unflagged_article_is_not_deferred` :2486.

Every soft-flag test asserts the staged-but-hidden triple (`count_pending()==0` / `list_pending()==[]` / `get_pending(link) is not None`) at :1345-1347, :1948-1953, :2027-2028, :2565-2567.

### 4.4 Tier B — rule stated at the `shares_pair` / `_build_pairs` layer

`test_model_extractor.py:883 TestSharesPair.test_shared_broad_only_returns_false` — the purest statement that one shared `|B` is a match:
```python
fp_a = {'pairs': ['toyota supra|car culture|B']}
fp_b = {'pairs': ['toyota supra|car culture|B', 'other|boulevard|B']}
any_shared, shared, any_distinctive = shares_pair(fp_a, fp_b)
assert any_shared is True                        # :887
assert any_distinctive is False                  # :889
```

The calibration harness `_pair_tier_verdict` (test_model_extractor.py:363) writes the threshold-free mapping down in code:
```python
if any_distinctive:      verdict = 'duplicate'      # :384-385
elif any_shared:         verdict = 'soft-flag'      # :386-387
else:                    verdict = 'non-duplicate'  # :388-389
```
and `test_calibration_non_dupes_share_no_pair` :429 states the consequence outright:
```python
assert shared == [], (
    f"{pair['label']} MUST share no pair key (a shared key is at "
    f"minimum a noisy [E014] soft flag), got {shared}")            # :452-455
```

Also: `test_prod_false_block_roundup_vs_single_drop` :721 (its `all(p.endswith('|B') for p in shared)` at :752 is *vacuously* true on an empty list, so it survives a narrowing — but its docstring :747-750 documents the now-obsolete intent "they DO still share a broad key … the operator gets an [E014]"), and `test_broad_line_tier_is_B` :788.

**Scope note:** if the new rule lives in `_pair_rule_verdict` (gate layer) rather than in `shares_pair`/`_build_pairs` (extractor layer), Tier B survives untouched. That placement choice determines whether the extractor's unit suite needs rewriting at all.

### 4.5 Tier C — the labelled corpus (good news)

In `tests/fixtures/cross_source_dedup_pairs.py`, three pairs carry `expected_verdict: 'soft-flag'`: `pair-1-real-2026-06-03` (:154, verdict :192, 3 shared `|B` keys :194-198), `pair-6-theme-only-stranger-things` (:342, :371, :373), `pair-7-same-source-broad-near-miss` (:390, :419, :421).

**All three name the series in BOTH titles, so all three survive a title-declaration rule unchanged.** The existing calibration corpus is therefore *not* an obstacle — it is evidence the rule is compatible with the known-good cases.

### 4.6 Match-dict key assertions

All direct assertions are builder-kwarg level in `tests/test_admin_alerts.py`: `test_e014_cross_source_dupe` :313 (`overlap_pct=35, n_matches=2, n_total=6` :319-322 → asserts `"35%"` :329, `"2/6"` :330), `test_e014_broad_series_flag` :339 (`pairs=[...]` :346), `test_e014_buttons_enabled_advises_pressing_the_buttons` :367 (both shapes side by side :374-377), `test_e015_cross_source_blocked` :485 (`overlap_pct=72` → `"72%"` :496), `test_e015_blocked_renders_matched_pairs` :501.

**Positional-contract pin — `test_e014_buttons_enabled_is_keyword_only` :471** freezes the argument order `(new_link, existing_link, new_source, existing_source, overlap_pct, n_matches, n_total, models)` at :475-481. Any signature change to `alert_cross_source_dupe` must keep these eight positional, or update this test.

**Never-render-`None` guards** (already shipped, directly relevant to dropping `overlap_pct`): `test_e015_blocked_no_pairs_no_overlap_never_renders_none_pct` :540 loops `for kwargs in ({}, {"pairs": []})` and asserts `assertNotIn("None%", msg)` :553; `test_e014_broad_no_pairs_no_overlap_never_renders_none_pct` :557 asserts `assertNotIn("None/None", msg)` :571.

**No test asserts the literals `"Совпадение моделей"` or `"Совпавшая серия/тема"`** — they appear only in `admin_alerts.py:824`/`:828` and in a *comment* at test_admin_alerts.py:559. The E014 body is pinned by components instead: `"[E014]"`, `"🤔"`, `"Похож на дубль"`, `"Что произошло"`, `"Что сделать"`, the button labels, and `assertNotIn("|D"/"|B"/"|"/"*")` no-raw-key-leak guards (:354-357, :533-535). **Rewording the E014 match block is therefore cheap** — but `"Что произошло"` / `"Что сделать"` are asserted as section headers (:334-335, :362-363, :388, :436, :601-602) and must survive.

Indirect (rendered output ⇒ which branch ran), `tests/test_integration.py`: `'Совпавшие пары'` asserted present at :1234, :2162 and **absent** at :1601, :1739; `'Совпадение:'` (legacy backstop) asserted present at :1600, :1738. These four are the tests that distinguish the two rules by their ping text.

### 4.7 Defer / fail-open / rate-limit

- **24 h defer** — no test names `_DEDUP_DEFER_HOURS`; it is pinned by duration: `assertGreater(delta, timedelta(hours=23))` / `assertLess(delta, timedelta(hours=25))` (test_integration.py:2478-2479). Repo primitives in `TestPublishAfterDefer` :2082 (:2116-2162), `defer_publish` :2227-2255, `clear_deferral` :2334-2383, `count_deferred` :2432. Schema pin: `'publish_after'` in test_migration.py:68.
- **Fail-open [E016]** — `test_integration.py:2280 test_degraded_mode` patches `extract_fingerprint` with `side_effect=RuntimeError("boom")` (:2277-2278) and asserts the row stores `model_fingerprint is None` (:2306), exactly one E016 naming `'RuntimeError'` (:2313-2315), the 1-hour rate-limit on a second `job()` (:2320-2330), and that the article still publishes (:2332). Every non-degraded dedup test also asserts `assertNotIn('[E016]', m)` (:1239, :1377, :1523, :1605, :1686, :1743, :2167, :2271, :2661) — **so any new exception path in the gate fails loudly across ~9 tests rather than silently.** That is a useful safety net for this change.
- **`softflag_pair:` keys** — the raw format is asserted verbatim in `TestPairRateLimit` (test_pending_articles_repo.py:1238): `'softflag_pair:http://c/new\nhttp://c/old'` :1274, `'softflag_pair:http://d/new\nhttp://d/old'` :1298. Five tests :1243-:1294. Gate level: :1977, :1393. `TestCrossSourceDedup._reset_tables` :1163 explicitly deletes `bot_state`.

### 4.8 How tests construct fingerprints — three idioms

**(a) Literal dict → `_seed_published` (dominant in integration):**
```python
# tests/test_integration.py:1322-1332
self._seed_published(
    'http://t-hunted.example/existing',
    {'strict': ['toyota supra'], 'brands': ['toyota'],
     'series': ['car culture'], 'pairs': ['toyota supra|car culture|B']},
    source='t-hunted',
)
```

**(b) Literal `pairs` dict → `shares_pair` (unit)** — see §4.4.

**(c) REAL `extract_fingerprint` over a fake article, then seeded** — the "probe the premise is reachable, then assert" idiom:
```python
# tests/test_integration.py:2621-2630
autoevo_fp = news_bot.model_extractor.extract_fingerprint(autoevo_article)
self.assertEqual(autoevo_fp.get('strict'), [])
self.assertIn('pop culture', autoevo_fp.get('series') or [])
self.assertEqual(autoevo_fp.get('pairs'), [])
self._seed_published('http://autoevolution.example/lincoln-sth', autoevo_fp, source='autoevolution')
```
Same pattern at :2551-2553, :2645-2649, :2701-2703, :2242-2244. **Idiom (c) is the one that would keep working under a title-declaration rule** — it derives the fingerprint from real article text. Idioms (a) and (b) hand-write fingerprints divorced from any text.

**Article factories:** `test_model_extractor.py:51 _article(title, subtitle, paragraphs)`; `test_integration.py:1155 _make_entry(link, title, published)` (RSS entry — bodies come from `mock_fetch_article.return_value`); `test_backfill_fingerprints.py:113 _seed_published`, `:166 _fake_article_with_brand`, `:183 _fake_reachable_body`.

**DB setup:** `test_integration.py:75 _IntegrationBase.setUp` — `tempfile.mkstemp('.db')` + `patch('news_bot.DB_FILE', ...)` (reaches `outage_state` + `pending_repo` too) + `init_db()`. `TestCrossSourceDedup` and `TestDedupReviewButtons` stop `notify_patcher` in `setUp` and restart it in `tearDown` (:1089-1097, :3577-3595) so per-test `@patch('news_bot.send_admin_notification')` owns the name. Dedup tests backdate rows with direct SQL (`_set_published_at` :1141) rather than `freezegun`.

---

## 5. Fixtures & corpus infrastructure

### 5.1 Layout

```
tests/fixtures/
├── cross_source_dedup_pairs.py     ← 28 KB labelled pair corpus (THE precedent)
├── mattel_flight_builder.py
├── orangetrack_golden.json
└── articles/
    ├── t_hunted/            ← 10 real PT HTML pages
    ├── autoevolution-blocked/
    ├── orangetrack/
    └── lamley/
```

### 5.2 `cross_source_dedup_pairs.py` — the existing labelled corpus

This is the direct precedent for a regression corpus and its docstring (tests/fixtures/cross_source_dedup_pairs.py:1-56) is worth reading in full before designing a new one. Structure:

- `DUPE_PAIRS` — 4 real cross/same-source dupes (3 SDCC distinctive-pair, 1 broad-only Car Culture).
- `NON_DUPE_PAIRS` — 5 probes that must not hard-block, **including the real 2026-07-28 prod false-flag**.
- Shared article constants (`_SDCC_THUNTED_ROUNDUP` at :76, `_SDCC_AUTOEVO_ROUNDUP` at :96, `_SDCC_MAIS_FOTOS` at :117) so reused articles stay byte-identical.

Per-pair dict shape (verbatim from the docstring):

```python
{
    'label': 'pair-1-real-2026-06-03',
    'a': {'title': ..., 'subtitle': ..., 'paragraphs': [...], 'source_name': 'autoevolution'},
    'b': {...},
    'expected_verdict': 'duplicate' | 'soft-flag' | 'non-duplicate',
    'expected_any_distinctive': bool,   # LOAD-BEARING invariant
    'expected_shared_pairs': ['porsche 911|k-pop demon hunters|D', ...],
    'note': 'rationale + real/synthesised marker + real source URL',
}
```

**Format note:** the dedup corpus is a **Python module, not JSON**. `tests/fixtures/orangetrack_golden.json` is the repo's only JSON corpus and is unrelated to dedup (its "dedup" hit is `gallery-dedup.html`, image dedup). The `articles/` HTML trees serve the **source parsers**, not the gate; `.pre-commit-config.yaml` excludes `^tests/fixtures/.*\.html$` from whitespace hooks to keep them byte-exact.

**Two properties directly relevant to this feature:**

1. **Fixtures are full article dicts (`title` / `subtitle` / `paragraphs`), not pre-baked fingerprints.** Fingerprints are derived by running the real `extract_fingerprint` over them. A corpus built this way automatically exercises any new title-vs-body distinction — a corpus of raw fingerprint blobs would not.
2. The docstring already records the discipline for synthesised bodies: *"Every `Porsche` in a body is written as `Porsche 911` + a lowercase word so the model extractor emits the exact strict token"*. A new corpus built from **real prod fingerprints** inverts this — real blobs cannot be re-derived, and if the feature takes Option B (a new fingerprint key) prod blobs will lack that key by definition. **A prod-fingerprint corpus can validate the pair sets but cannot by itself validate a title-declaration rule** unless the corpus also captures the titles/first paragraphs.

---

## 6. Risks & contradictions

### 6.1 CONTRADICTION with the brief — the E014 percentage is already gone

The brief states the E014 ping "must stop showing «Совпадение моделей: N%»". **It already does, for the broad-pair path.** Verified by executing the real builder:

```
[E014] 🤔 Похож на дубль
...
Источник новой: t-hunted
Источник существующей: autoevolution
Совпавшая серия/тема:
audi sport + car culture

Что произошло:
статья прошла в очередь, потому что
порог автоблокировки (50%) не достигнут.
...
```

Because `_pair_match` (news_bot.py:2688-2695) **always** populates `pairs`, and the send site (news_bot.py:4482) always forwards it, the `if pairs:` branch always wins for a broad-pair flag. «Совпадение моделей: N%» is reachable **only** via the legacy set-overlap backstop — which the brief says stays untouched.

So what the operator is actually seeing that is meaningless:

1. **`Что произошло: … порог автоблокировки (50%) не достигнут`** (admin_alerts.py:869-871) — a hardcoded, unconditional sentence. It describes the backstop's 50% threshold, which has **nothing to do with** the broad-pair verdict. This is the genuinely misleading percentage in the ping.
2. **The log lines** at news_bot.py:4398-4401, 4408-4411 and 4438-4444 all print `match['overlap_pct']` as `"overlap %d%%"` — these are where a 0–7% figure is visible, and they are unit-ambiguous between the two rules (§1.3).
3. The ping states the shared line but **never says it is declared in both articles** — that part of the brief is genuinely new text.

**Recommendation for the spec author:** re-scope this requirement to (1) the `Что произошло` sentence, (2) the log lines, (3) the new "declared in both" wording — not to a `Совпадение моделей` block that the broad path never renders.

### 6.2 Docs vs code

`architecture.md:327-336` is **accurate** — it correctly documents "A shared `|B` (broad) key **soft-flags**" as a distinct path from the "(2) Set-overlap backstop … `[0.30,0.50)` flag". The brief's phrasing ("the documented 30–49% set-overlap path") understates the docs: both paths are documented.

What the docs **do not** say, and what this feature would change:

- That the broad path has **no threshold whatsoever** — a reader of architecture.md:328 could reasonably assume some magnitude test exists.
- The docs record two hard-won precision rules (round-up rule at architecture.md:334, theme-only rule at :336) but **not** the residual round-up failure mode that survives both — a round-up still emits a full cartesian pair set and still soft-flags on one incidental key.
- `deployment.md:365-375` tells the operator to "watch the first days of `[E014]`/`[E015]` pings for false positives" — the measured 95% FP rate is the answer to that instruction and should land back in the docs.
- `project.md:41` summarises the same rules and would need the same amendment.

### 6.3 Fail-open contract ([E016])

`job()` wraps the entire gate in `try/except Exception` (news_bot.py:4505-4531): any crash increments `funnel['dedup_degraded']`, logs, sends a **1-hour rate-limited** `[E016]` (`is_dedup_degraded_rate_limited`, pending_articles_repo.py:1663), and the article **publishes with `model_fingerprint=NULL`**.

Risks for the new rule:

- New per-candidate text work (title scan, JSON parse of a `paragraphs` blob) runs **inside** this handler. A regression there degrades the gate silently-ish rather than failing loudly — [E016] is capped at one ping/hour, so a systematic fault reads as a trickle.
- The house style is defensive-read (`.get(...) or []`), used consistently at news_bot.py:2837-2839 and model_extractor.py:736-737. A `None` title or a non-list `paragraphs` on a malformed historical row must not raise.
- **Absent-data policy is a decision, not a default.** For rows with no declared-series data (pre-change fingerprints under Option B, or published rows under Option A), the gate must choose: no data ⇒ no flag (precision-preserving, but broad-pair flagging is effectively OFF for a 30-day warm-up) versus no data ⇒ flag as today (keeps the false positives during warm-up). This is invisible in the code and must be decided explicitly.

### 6.4 The 24h defer path

`_DEDUP_DEFER_HOURS = 24` (news_bot.py:2639). The defer stamp is set in the `flag` branch at news_bot.py:4424-4429, **before and independently of** the rate-limit check at :4430 — the inline comment (news_bot.py:4424-4427) is explicit that the delay protects the article while the rate-limit only protects the operator's notifications.

Consequence of the fix: **every eliminated false flag also eliminates a 24-hour publication delay.** 20 fewer flags over 1.5 months means ~20 articles that would now publish same-day. This changes queue timing and slot pressure (`count_pending()` excludes deferred rows — architecture.md:338, :397). Not a bug, but it is a real behavioural side effect that belongs in the spec's expected-changes section, and it interacts with the fixed 3/day slot ceiling.

### 6.5 Rate-limit keys `softflag_pair:`

`_KEY_SOFTFLAG_PAIR_PREFIX = 'softflag_pair:'` (pending_articles_repo.py:50); key shape `softflag_pair:{new_link}\n{existing_link}` (`_pair_key`, repo:1609-1616); 7-day window (`is_pair_rate_limited`, repo:1619-1644); written by `mark_pair_pinged` (repo:1647-1661) at news_bot.py:4499-4501.

Risks:
- Keys are **per link-pair, never garbage-collected** — `bot_state` accumulates one row per flagged pair forever. Fewer flags means slower growth; no cleanup exists either way.
- The rate-limit is checked **only when a flag fires**. Suppressing flags does not orphan anything, but historical `softflag_pair:` rows for pairs that would no longer flag become dead weight.
- No test-visible coupling to `overlap_pct` — the key is links-only, so a match-dict change cannot break it.

### 6.6 Consumers that assume the match-dict keys

Full grep across `*.py` (excluding venv/tests) for `overlap_pct` / `n_matches` / `n_total`:

| Site | Line | Note |
|---|---|---|
| `_pair_match` producer | news_bot.py:2685-2695 | sets all three + `pairs` + `models` |
| `_set_overlap_backstop_verdict` producer | news_bot.py:2782-2789 | sets all three, **no** `pairs` |
| E015 log | news_bot.py:4398-4401 | `match['overlap_pct']` — **direct index, would KeyError if dropped** |
| E015 send | news_bot.py:4408-4411 | `overlap_pct` positional + `pairs=` |
| E014 log | news_bot.py:4438-4444 | `match['overlap_pct']` — direct index |
| E014 send | news_bot.py:4477-4482 | all four passed explicitly |
| `alert_cross_source_dupe` | admin_alerts.py:804-806 | all Optional, default None |
| `alert_cross_source_blocked` | admin_alerts.py:904 | `overlap_pct` Optional |

**The builders tolerate `None`** (both have an `elif overlap_pct is not None:` guard added specifically so the operator never sees `None%` — admin_alerts.py:821-822, :918-920). **The `job()` call sites do not** — they use `match['overlap_pct']` bracket-indexing in three log statements. Removing the key from `_pair_match` without touching those three lines raises `KeyError` inside the fail-open handler → silent degraded mode + [E016]. This is the single sharpest breakage edge in the change.

`models` mirrors `pairs` in `_pair_match` (news_bot.py:2690-2691) purely for legacy-caller compatibility.

### 6.7 Other

- **`DEDUP_SERIES_ENABLED`** (news_bot.py:135) gates Rule 1 only. Turning it off does **not** disable the backstop, and the module-attribute read at news_bot.py:2842 is a bare name (documented at news_bot.py:127-136 as deliberate, so tests can monkeypatch `news_bot.DEDUP_SERIES_ENABLED`).
- **Load-time asserts** in `model_extractor` (lines 179-197, 227-242) fire at **import**. Any change to the fingerprint dict or lexicon must keep them satisfied or the whole bot fails to start.
- **`similarity()` and the backstop stay untouched** per the agreed direction — but note the backstop is reached only on a pair-rule `pass` (news_bot.py:2845-2849). **Making the broad branch stricter routes more articles into the backstop**, which was previously shadowed by the broad flag. Articles that used to get a broad flag may now get a *backstop* flag instead (if their `strict` overlap lands in `[0.30, 0.50)`) — or even a **backstop hard block** at ≥0.50. This is a non-obvious second-order effect: the fix could surface latent backstop verdicts that the broad branch has been masking for months.
- **No local DB** in the repo (`find . -name '*.db'` → empty); `news_bot.DB_FILE` is read at call time by `pending_articles_repo._connect()` (repo:204-218). Corpus work needs prod data pulled over SSH, or fixture-built DBs.

---

# Updated: 2026-08-20

**Scope of this pass:** implementation-level. The rule was frozen since §1–§6 above:
**flag a broad pair only if the pair's series is named in the TITLE of BOTH articles;
symmetric; body never read; the first-paragraph fallback is CUT.** This section reports
exact edit points, the three design forks the tech-spec must settle, and a **measured**
(not estimated) test-surgery cost.

**Method for this pass:** static read **plus a working prototype** of the whole feature
built in a throwaway `git worktree` off `6a7228c` and run against the full suite. The
prototype was deleted; its diff is at
`/private/tmp/claude-501/-Users-alex-MyFiles-ai-projects-my-hw-my-hw-bot/6deaa3b0-1223-4ac9-b32c-93316bdc8c24/scratchpad/prototype.patch`.
It is **evidence, not a proposal** — the tech-spec owns naming, comments and text.

## U0. What the frozen rule retires from the first pass

| First-pass item | Status now |
|---|---|
| **F3** — `published_articles` has no `paragraphs` column | **MOOT.** Only mattered for the first-paragraph fallback, which is cut. |
| **§2.3 Options A / B / C** (DB column vs fingerprint key vs hybrid) | **All MOOT.** The spec picked "title from the existing projection, both sides". No migration, no fingerprint key, no backfill. |
| **F8** — `_already_backfilled` keys on `$.pairs` | **MOOT** as a blocker (it only threatened Option B). Still true, still worth not disturbing. |
| **§5.2 property 1** — "fixtures are article dicts, fingerprints derived" | **INVERTED for the new corpus** — see §U6. |
| **F6 / §6.1** — «Совпадение моделей: N%» is already unreachable on the broad path | **Still true and now load-bearing**: the AC's three-way text branch is about the «Что произошло» sentence (admin_alerts.py:869-873), not the match block. |

## U1. Implementation surface — exact anchors

### U1.1 The new article's title (spec: "no plumbing needed" — confirmed, with one hazard)

`_check_cross_source_dedup(article, fingerprint, conn, new_source)` (news_bot.py:2796)
still never reads `article`. The field is **`article['title']`** — every source fetcher
sets it (`autoevolution_source.py:495,566`, `t_hunted_source.py:336`,
`lamley_source.py:429`, `orangetrack_source.py:396`).

**HAZARD — an order-dependence trap that defeats the spec's symmetry constraint.**
The candidate row's title is not `article['title']`; it is what `job()` wrote into
`pending_articles.title` at **news_bot.py:4547**:

```python
'title': article.get('title') or entry.get('title') or '',
```

That idiom (5 occurrences: 4244, 4251, 4342, 4404, 4547) falls back to the RSS entry
title when the fetcher returned an empty one. If the gate reads only `article['title']`,
then for such an article the **new** side sees `''` while the **candidate** side (once
the same article is in the DB) sees the entry title — an asymmetric verdict that depends
on which article arrived second. That is precisely what "Правило симметрично"
(user-spec Ограничения) forbids.

**Cheap fix:** hoist the expression to a local before the gate call at news_bot.py:4388
and reuse it both at the gate and at 4547. `entry` is in scope there.

### U1.2 The candidate's title — dict key is `'title'`

Both fetch helpers already project it:
`list_recent_pending_fingerprints` — `pending_articles_repo.py:1534`;
`list_recent_published_fingerprints` — `pending_articles_repo.py:1553`.
`_row_to_dict` (repo:189-201) keys the dict off `cursor.description`, so the key is
**literally `'title'`** — no aliasing, no rename. `move_to_published` (repo:1277-1287)
carries `title` verbatim, so a published candidate keeps its original-language title.
**No schema change, no query change.** (Confirms F2 at implementation level.)

### U1.3 The check itself — inside `_pair_rule_verdict` (news_bot.py:2698-2733)

Loop body anchors: 2712 `for row in candidates:` · 2718 `shared = new_pairs & cand_pairs`
· 2721 `shared_sorted` · 2723-2726 the `|D` early return · **2727-2730 the broad branch —
the edit point** · 2731-2733 the tail.

Three primitives, all already present:

1. **Series out of a pair key** — the key is `"<model>|<series>|<tier>"`, theme-only
   `"*|<series>|B"`. `_build_pairs` (model_extractor.py:533-563) guarantees exactly three
   fields: model tokens are `[a-z0-9 -]` and series canonicals are pipe-free by the
   load-time assert at model_extractor.py:179-183. So `key.split('|')[-2]` is total.
2. **Series out of a title** — `model_extractor.extract_series(text)` (model_extractor.py:711).
   It returns **canonical** names and pair keys embed **canonical** names, so a plain set
   intersection is correct with no normalisation step.
3. **Robustness** — verified live, `extract_series` never raises and returns `[]` for
   `None`, `''`, `123`, `[]`, `{}`, `{'title': None}`. This satisfies the edge case
   «Пустой или отсутствующий заголовок кандидата — проверка предмета не проходит,
   исключения нет» **with no guard code at all**.

**Cost:** hoist `extract_series(new_title)` once outside the loop; call
`extract_series(row.get('title') or '')` per candidate that shares a pair. Measured:
**4 µs per title scan** → 0.8 ms for a full 200-row window even if evaluated eagerly.
Perf is a non-issue.

### U1.4 Live scoring of the frozen rule against measurements.md

Run against the real module on `dev` (`series ∈ extract_series(title_A)` and
`∈ extract_series(title_B)`):

| measurement-1 case | expected | rule says |
|---|---|---|
| Mercedes-Benz 6x6 × 5 Unsung Heroes (`red line club`) | false | **no flag** ✅ |
| Case Q Unboxing STH × Mooneyes Diorama (`boulevard`) | false | **no flag** ✅ |
| Convention Malaysia × Last Car Culture Set (`team transport`) | false | **no flag** ✅ |
| RLC Exclusive '21 Bronco × 11 New Collectibles (`red line club`) | false | **no flag** ✅ |
| Novo lote **Car Culture** × Last **Car Culture** Set | TRUE dupe | **flag** ✅ |
| Mais fotos **Boulevard** × 2026 **Boulevard** Mix 4 | TRUE dupe | **flag** ✅ |
| Mais fotos Boulevard × Unboxing: 10 Affordable Cars | false | **no flag** ✅ |
| **Car Culture 2-Pack Mix 4 × Last Car Culture Set** | false | **FLAGS** ⚠️ |

The last row **is** the "≤1 ложный" the AC budgets for. Both titles genuinely name
`car culture`; no title-scope rule can separate it. Worth naming explicitly in the
tech-spec so the corpus AC is not read as "0 false positives".

Also confirmed: `extract_series("New Hot Wheels RLC Exclusive '21 Ford Bronco")` →
`['red line club']` (uppercase acronym matches) while `"novo lote rlc"` → `[]` — the
edge case about acronym casing behaves exactly as the spec describes.

## U2. THE THIRD SIGNAL — options and the recommendation

**Precondition finding that reshapes this whole question:** grep across all `*.py`
(venv excluded) shows **no test and no non-`news_bot` module calls
`_check_cross_source_dedup`, `_pair_rule_verdict`, `_set_overlap_backstop_verdict`,
`_pair_match` or `_fetch_dedup_candidates`.** Each has exactly one caller, all inside
`news_bot.py`. **Changing their arity or return shape breaks zero tests.** 100 % of gate
coverage is end-to-end through `job()` in `tests/test_integration.py`.

The three states the `[E014]` builder must tell apart:
(a) broad-pair flag · (b) ordinary backstop flag (sim 0.30–0.50) · (c) capped backstop
flag (sim ≥ 0.50, block withheld). Today `alert_cross_source_dupe` (admin_alerts.py:799)
forks only on `if pairs:` (admin_alerts.py:823), so (b) and (c) are indistinguishable.

| Option | Call sites that change | Verdict |
|---|---|---|
| **1. Extra key in the match dict** — gate sets `match['block_withheld']=True` on the downgrade; `job()` forwards it as a new keyword-only kwarg | gate (news_bot.py:2871 region) · E014 send (news_bot.py:4477-4491) · builder signature (admin_alerts.py:799-811) · «Что произошло» (admin_alerts.py:869-873) | **RECOMMENDED — least invasive.** The builder already carries `buttons_enabled` as a keyword-only bool after `*`; a second one is the same shape. The positional-contract pin `test_e014_buttons_enabled_is_keyword_only` (test_admin_alerts.py:471) asserts a 9th **positional** arg raises `TypeError` — a keyword-only param keeps that green. |
| **2. Derive it in `job()` from `match['overlap_pct'] >= 50`** | E014 send only | **WRONG — it lies.** `overlap_pct = int(round(best_sim*100))` (news_bot.py:2786) but the block branch tests `best_sim >= 0.50` (news_bot.py:2792). Enumerated: **350 Jaccard fractions below 0.50 round to ≥50 %**, the smallest being `50/101 = 0.4950`. Round-up fingerprints reach ~74 strict models (296 pairs ÷ 4 series), so `\|strict\|` in the 50-100 range is real prod data. In those cases `job()` would print "the threshold WAS reached but the block was withheld" for an article the backstop would only ever have flagged — a direct violation of «ни в одном не врёт». |
| **3. Verdict enum** — new decision string e.g. `'flag_capped'` | `_check_cross_source_dedup` returns · `job()` dispatch at news_bot.py:4395 and 4420 · both gate docstrings | Works, but strictly more invasive than 1 and **still needs the builder kwarg anyway** — the builder is pure and cannot see `decision`. No gain. |

**Note:** option 1 does not need a `None`-safe read anywhere the backstop path is absent
— `match.get('block_withheld')` on a `_pair_match` dict simply returns `None`.

## U3. THE CAP — where the downgrade belongs

Control flow today, `_check_cross_source_dedup` (news_bot.py:2836-2872):

```
2842  if DEDUP_SERIES_ENABLED and pairs:
2843      candidates = _fetch_dedup_candidates(conn)
2844      decision, match = _pair_rule_verdict(pairs, candidates)
2845-2848 if decision != 'pass': return (decision, match)      # TERMINAL
2864-2865 if not strict and not series: return ('pass', None)  # empty short-circuit
2869-2872 return _set_overlap_backstop_verdict(fingerprint, strict, candidates, new_source)
```

**The subject-rejected path always reaches the backstop.** A non-empty `pairs` implies a
non-empty `series` (`_pass3_series`, model_extractor.py:566-590, derives both from the
same `_scan_series` result), so the empty short-circuit at 2864 can never fire on this
path. No new branch is needed to get there.

**Where the cap belongs: in the gate, at the `return` on news_bot.py:2871-2872** — i.e.
bind the backstop result, downgrade, then return.

- **Not in `_set_overlap_backstop_verdict`** (news_bot.py:2736-2794). The user-spec is
  explicit that the backstop's thresholds are out of scope («Пороги правила set-overlap
  не трогаем»); adding a `cap` parameter there puts the change inside the function the
  spec fences off, for zero benefit.
- **Not in `job()`.** `job()` cannot know a pair was subject-rejected unless the gate
  tells it, and the `decision == 'block'` branch (news_bot.py:4395-4418) has irreversible
  side effects — `mark_processed` at 4402-4406 and the `[E015]` send at 4407-4417. A
  downgrade there means re-ordering a branch that drops articles. Highest risk, no gain.

**State that must be threaded:** `_pair_rule_verdict` must report that at least one
broad pair failed the subject check. Because the funnel counter (§U4) needs the same
fact, make it an **`int` count**, not a bool — one value serves the cap (`> 0`) and the
ping. Return shape `(decision, match, suppressed)`; §U2's precondition finding makes the
arity change free. `_check_cross_source_dedup` then also returns 3-tuple so `job()` can
count — its single call site is **news_bot.py:4391-4393**.

**Two consequences worth pinning in the tech-spec:**

1. Case (c) is only reachable when the new article has non-empty `strict` and a
   cross-source 7-day candidate at ≥0.50. A **theme-only** rejected key (`*|<series>|B`)
   has empty `strict`, so `similarity()` returns 0.0 at its AC6 guard and the backstop is
   a guaranteed no-op — the cap never fires there.
2. The cap applies to the **backstop's** block only. A later `|D` pair still returns
   `('block', …)` from `_pair_rule_verdict` and still fires `[E015]` — required by
   «Поведение распознающей (`|D`) пары не изменилось» and unaffected by the cap.

## U4. THE COUNTER — funnel plumbing

**architecture.md:341's "counts only" claim is accurate**, verified against the code:
`funnel` is declared as a flat dict of plain ints at **news_bot.py:4082-4094**, every
consumer goes through `_funnel_int` (admin_alerts.py:172-182) which clamps to a
non-negative int and never raises, and no funnel value is ever a string. A
`dedup_suppressed: 0` int fits the contract exactly. The **diagnostic detail** the
risk-1 mitigation asks for (both links, the shared line, both titles) belongs in the log
line, not the funnel — the same split architecture.md already documents.

Exact edit points:

| What | Anchor |
|---|---|
| Declare `'dedup_suppressed': 0` | news_bot.py:4082-4094 (next to `'dedup_degraded'` at :4091) |
| Increment | news_bot.py:4391-4393, right after the gate call — **once per article**, not per rejected candidate (see the ambiguity below) |
| `[funnel]` log line | news_bot.py:4632-4642 (format string + arg list) |
| `[E009]` breakdown block | `_format_funnel`, admin_alerts.py:221-271 — read at :246-258, new bullet next to the `if held:` conditional at :259-263 |
| `[E008]` compact line | `_format_funnel_line`, admin_alerts.py:275-305 — suffix built like `failed_part` at :296, appended in the return at :300-303 |

**`job()` runs once a day** — a single fixed cron tick at 10:00 МСК (news_bot.py:4011,
:5090-5091, `schedule.every().day`), so the per-tick funnel *is* the daily figure and
`[E008]`/`[E009]` *is* the daily ping. No aggregation layer needed.

**Do NOT fold it into the «отсеяно» sum** (`_format_funnel_line`, admin_alerts.py:288-294).
A suppressed flag is not a drop — the article is staged. This is the same reasoning
`held_for_review` already carries, with a ready-made precedent test to copy
(`test_held_rows_are_not_counted_as_dropped`, test_admin_alerts.py:871-877).

**Zero existing funnel tests break.** `TestIntakeFunnel` (test_admin_alerts.py:713) uses
*partial* dicts — `_funnel_int` reads a missing key as 0 — and every assertion is
`assertIn`/`assertNotIn`, never an exact-block equality. Verified by running the full
prototype: all funnel tests stayed green.

**AMBIGUITY the tech-spec must close.** «Число подавленных срабатываний» has two
readings: (i) count of rejected *candidate pairs* — a round-up article can contribute 20
in one tick; (ii) count of *articles* whose broad flag was withheld — which is what a
«срабатывание» meant in measurement 1 (31 `softflag_pair:` keys = 31 pings, since the old
rule remembered only the first broad match and emitted one `[E014]` per article).
**(ii) is the one that is comparable to the 31/22 baseline.** A second sub-question: if
the pair is rejected but the backstop then flags anyway, was anything "suppressed"? The
operator did see a ping. The spec's step 7 assumes the common case («статья идёт в
очередь молча») and does not say.

## U5. TEST SURGERY — MEASURED, NOT ESTIMATED

**This is the headline correction of this pass.** measurements.md замер 3 calls the test
edit «самая объёмная часть работ» on the basis of 25 `_seed_published` call sites. That
estimate is **wrong by an order of magnitude**, and it is now measured rather than argued.

### U5.1 Method

A full prototype of the frozen feature — subject check, scan continuation, the cap, the
third signal, the three-way `[E014]` text, the funnel counter, and the percent-free broad
log branch — was built in a detached worktree off `6a7228c` and run against the whole
suite. Diff size: **`news_bot.py` +51/−20, `admin_alerts.py` +25/−2, tests +4/−1.**

### U5.2 The measured numbers

| Run | Result |
|---|---|
| Baseline, unmodified `6a7228c` | `1970 passed, 2 skipped, 517 subtests` |
| Prototype **with** the cap, tests untouched | **`2 failed`**, 1968 passed |
| Prototype **without** the cap, tests untouched | **`8 failed`**, 1962 passed |
| Prototype with the cap **+ a 3-line change to `_seed_published`'s default** | **`1970 passed, 2 skipped`** — fully green |

### U5.3 The 2 tests that actually go red

Both are in `tests/test_integration.py::TestCrossSourceDedup`, both seed a **theme-only**
`*|k-pop demon hunters|B` pair against the default candidate title `'Existing Article'`
(which names no series). Their `strict` is empty on both sides, so the backstop cannot
substitute a flag — `similarity()` returns 0.0 at its AC6 guard:

| Test | def | seed | dies on |
|---|---|---|---|
| `test_soft_flag_defers_publication_by_a_day` | :2440 | :2450 | `publish_after` stays NULL → the 23 h/25 h delta asserts at :2470-2473 |
| `test_theme_only_pop_culture_flags_no_model` | :2517 | :2532 | `assertEqual(len(e014_calls), 1)` :2570-2573 and `assertIn('k-pop demon hunters', …)` :2574 |

### U5.4 The 6 tests that ride entirely on the cap

These seed `toyota supra|car culture|B` with **100 % strict overlap**, so once the broad
flag is suppressed the backstop *would* hard-block them. With the cap they stay green
(the `[E014]` is substituted by a capped backstop flag); **without** the cap all six flip
to `[E015]` and go red:

`test_broad_pair_soft_flag_is_terminal` :1313 · `test_soft_flag_logged_even_when_alert_rate_limited` :1393 ·
`test_flag_on_e014_send_includes_review_keyboard` :3625 · `test_flag_off_e014_send_has_no_keyboard` :3707 ·
`test_flag_on_non_numeric_admin_no_keyboard_no_token` :3748 · `test_only_e014_carries_keyboard` :3796.

**They are the de-facto regression test for the cap** — and the fact that they pass for
the *wrong reason* (a backstop flag standing in for a pair flag) is exactly why the
`_seed_published` fix below matters: it restores their original intent.

### U5.5 The fix — 3 lines, one place

`_seed_published` (test_integration.py:1100-1101) hardcodes `title='Existing Article'`,
`paragraphs=['Body.']` while injecting a hand-written `pairs` list. Making the default
title derive from the seeded fingerprint's `series` fixes **all 8** at once and
future-proofs new seeds:

```python
def _seed_published(self, link, fingerprint, source='autoevolution', title=None):
    if title is None:
        _series = ' '.join(fingerprint.get('series') or [])
        title = f'Existing {_series} Article' if _series else 'Existing Article'
```

Verified: `extract_series('Existing Car Culture Article')` → `['car culture']`,
`extract_series('Existing K-Pop Demon Hunters Article')` → `['k-pop demon hunters']`.
Full suite green with this change alone. (Per-test explicit `title=` kwargs at the 8
call sites are the alternative; 8 lines instead of 3, no other difference.)

### U5.6 Cause buckets — what is NOT broken

- **Old-`[E014]`-text assertions: ZERO.** Repo-wide grep confirms **no test asserts
  «порог автоблокировки», «Совпавшая серия/тема» or «Совпадение моделей»** — both live
  only in `admin_alerts.py:824`/`:871`. The body is pinned by components instead
  (`"[E014]"`, `"Похож на дубль"`, `"Что произошло"` at test_admin_alerts.py:336/364/601,
  `"Что сделать"`, and the no-raw-key-leak `assertNotIn` guards). Rewriting the sentence
  three ways is free — only the `"Что произошло"` header must survive.
  `'Совпадение:'` / `'Совпавшие пары'` at test_integration.py:1234, 1600-1601, 1738-1739,
  2162 are all **`[E015]`** assertions, untouched.
- **"Broad verdict is terminal": ZERO red.** `test_broad_pair_soft_flag_is_terminal`
  (:1313) survives once its candidate title declares the series — a *subject-passing*
  broad flag is still terminal, so the test keeps validating real behaviour. Its
  docstring (:1319-1323) and the gate docstrings (news_bot.py:2809-2818, :2846-2848)
  claim **both** verdicts are terminal, which stops being true. **Prose amendment, not a
  test rewrite.**
- **Extractor layer: ZERO.** Putting the rule in `news_bot._pair_rule_verdict` leaves
  `TestSharesPair` (6 tests, test_model_extractor.py:874-931), `TestPairs` (12 tests,
  :672-872) and the `_pair_tier_verdict` calibration harness (:363-387) untouched — they
  call `model_extractor` directly. Pushing the rule down into `shares_pair`/`_build_pairs`
  instead would break all 18 on the signature change alone, since neither function takes
  a title. **Strong argument for the gate-layer placement.**
  Also verified: all three `expected_verdict: 'soft-flag'` corpus pairs name their series
  in **both** titles, so the existing calibration corpus survives either way.
- **Whole files with zero breakage:** `test_model_extractor.py`, `test_admin_alerts.py`,
  `test_pending_articles_repo.py` (41 dedup-adjacent tests), `test_backfill_fingerprints.py`
  (15 — its `_seed_published` at :113 is a *different*, module-level helper that never
  runs `job()`), `test_no_token_leak_in_logs.py` (its `[E014]` string at :666 is a
  hand-written literal, not the builder). **All breakage is confined to
  `tests/test_integration.py`.**

### U5.7 What is NOT covered today and must be written

Nothing in the suite pins the new behaviour. Minimum new tests:

1. Broad pair, series in **both** titles → `[E014]` with the pairs block and no
   «порог» sentence.
2. Broad pair, series in only **one** title → no `[E014]`, no 24 h defer, and the
   suppressed counter increments.
3. Subject-rejected pair **+ 100 % strict overlap** → `[E014]`, and **`[E015]` is
   impossible** (the cap; this is the AVP step-4 test).
4. Subject-rejected pair on candidate *N*, then a `|D` pair on candidate *N+1* →
   `[E015]` still fires (scan continuation).
5. Subject-rejected pair on candidate *N*, then a subject-**passing** broad pair on
   candidate *N+1* → `[E014]` names candidate *N+1*.
6. Empty / `None` candidate title → no flag, no exception.
7. Portuguese title resolves (`Novo lote da série Car Culture`) — **currently an
   emergent property with no regression guard anywhere** (the parametrised lexicon test
   at test_model_extractor.py:602-618 is English-only).
8. The three `[E014]` text variants, rendered by the real builder.

## U6. CORPUS FIXTURE

### U6.1 Conventions on disk

`tests/fixtures/` holds `cross_source_dedup_pairs.py` (28 KB labelled corpus, a **Python
module**), `orangetrack_golden.json` (the only JSON corpus), `mattel_flight_builder.py`,
and `articles/{t_hunted,autoevolution-blocked,orangetrack,lamley}/` HTML trees.

JSON convention, from `tests/test_orangetrack_golden.py:44-55`:

```python
_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "orangetrack_golden.json")
json.load(fh)["fixtures"]
```

The file is `{"__README__": "<why this exists / editing it IS the gate failure>",
"fixtures": {…}}`, and the loader resolves the path from `__file__` (pytest is run both
from repo root and from `tests/`). That header-plus-payload shape is the house style for
a data corpus that must not be casually regenerated — worth copying verbatim here.

### U6.2 The natural shape for this corpus

**Pre-baked fingerprints + titles, not article dicts.** This inverts the first pass's
§5.2 property 1, and the frozen rule is what makes it sound: the rule reads **only**
titles and **only** the `pairs` sets, both of which survive in the prod DB. Bodies are
unrecoverable (autoevolution answers 403 in bulk; `published_articles` never stored
paragraphs) — and with the fallback cut, **they are not needed.**

```json
{
  "label": "2026-07-28-mercedes-6x6-vs-unsung-heroes",
  "a": {"link": "...", "title": "New Hot Wheels Mercedes-Benz 6x6 ...",
        "source_name": "autoevolution", "fingerprint": {"strict": [...], "brands": [...],
        "series": [...], "pairs": ["porsche 911 gt2 evo|red line club|B", ...]}},
  "b": {"...": "same shape"},
  "operator_verdict": "not-a-dupe" | "dupe",
  "expected_flag": false,
  "shared_pairs_today": ["porsche 911 gt2 evo|red line club|B"],
  "note": "prod softflag_pair key, flagged YYYY-MM-DD; the shared line is a passing mention"
}
```

**The harness can call the real production function directly.** `_pair_rule_verdict` is
pure — no DB, no I/O — so the test builds a one-element candidate list
`[{'link':…, 'source_name':…, 'title':…, 'model_fingerprint': <fp dict>}]` and asserts the
verdict. **No sqlite fixture, no `job()`, no mocking.** This is a materially cheaper
harness than `TestCrossSourceDedup`'s.

**Sizing / hooks:** the largest fingerprint has 296 pair keys (~12 KB); ~48 article
records → roughly 100–200 KB. Well under `check-added-large-files --maxkb=1000`
(`.pre-commit-config.yaml`). JSON is safe under `trailing-whitespace` /
`end-of-file-fixer` (unlike the HTML evidence trees, which are explicitly excluded).

### U6.3 The prod pull — exactly what must be extracted

Repeating the first pass: **no local DB exists** (`find . -name '*.db'` → empty). The
data is only on the prod host (`45.90.216.165`, `/data/news.db`, via
`docker exec -i hw-news-bot`).

1. **The flag ledger** — `SELECT key FROM bot_state WHERE key LIKE 'softflag_pair:%'`.
   Key shape is `softflag_pair:{new_link}\n{existing_link}` (`_pair_key`,
   pending_articles_repo.py:1609-1616). 31 rows expected. **Never garbage-collected**, so
   this side is stable.
2. **Fingerprints + titles for both sides** —
   `SELECT link, title, model_fingerprint FROM published_articles` and the same from
   `pending_articles`. 22 of the 31 pairs have both sides surviving; the other 9 were
   cleaned away with their rows.
3. **Titles for the two extra dupes** (14.08 `team transport`, 15.08 `boulevard`) —
   `SELECT link, title FROM processed_news WHERE link IN (…)`. That table is
   `(link, title, pub_date)` (news_bot.py:1548).
4. **Rebuild the two missing t-hunted fingerprints** — `news_bot.fetch_full_article({'link': …})`
   then `model_extractor.extract_fingerprint(article)`. The t-hunted parser works live
   (measurement 2 used it on six posts). Their autoevolution partners' fingerprints
   survived in the DB (4 and 90 pairs), so **no autoevolution re-fetch is needed** —
   which is fortunate, because bulk re-fetch there is 403 (backfill_fingerprints.py:237-253).

**Corpus size is 24 pairs, not 31**: 22 surviving + 2 rebuilt = 3 true dupes + 21 false —
which is exactly the arithmetic in the acceptance criterion. The remaining 9 are
unrecoverable and should be documented as such in the `__README__`.

**Land the raw dump first, transform second.** The repo has a precedent and a hook rule
for exactly this: `work/source-formatting-parity/corpus-raw/` (see
`work/source-formatting-parity/tasks/1.md:224-243`), excluded from the whitespace hooks
in `.pre-commit-config.yaml`. The rationale there applies verbatim — the extract is
unrepeatable (9 of 31 fingerprints are *already* gone), so it must be committed as-is
before anyone starts reshaping it.

## U7. Spec items the code cannot support as written

Four things, in descending severity. None is a blocker; all need a sentence in the
tech-spec.

**U7.1 — The 24 h defer AC contradicts itself in the capped branch.** AC:
«Суточная отсрочка публикации ставится ровно тогда, когда выдан `[E014]`, и не ставится
для отклонённых по предмету пар.» In the capped case **both clauses apply at once**: the
pair *was* subject-rejected *and* an `[E014]` *is* issued. The code cannot honour both —
the defer stamp is set in the `decision == 'flag'` branch (news_bot.py:4426-4432), before
the rate-limit check, and a capped verdict is a `'flag'`. **The first clause must win:
the defer follows the `[E014]`, not the rejection.** Otherwise a flagged article gets a
cancel button with no window to press it — the exact 2026-07-28 incident
`_DEDUP_DEFER_HOURS` exists to prevent (news_bot.py:2632-2638).

**U7.2 — Which pairs the `[E014]` names when only some pass.** A single candidate can
share several broad pairs with only some passing the title check. `_pair_match(row,
shared_sorted, n_total)` (news_bot.py:2726, :2730) currently passes **all** shared keys,
and `alert_cross_source_dupe` renders them verbatim through `_render_pairs_block`
(admin_alerts.py:790-793). Rendering the full set would name lines that are *not*
declared in both titles — «ни в одном не врёт» forbids it. The builder cannot filter
(it never sees the titles), so **the gate must pass only the passing subset**. Note this
also changes `n_matches`, which is moot on the broad path (no percent sentence) but is
still forwarded at news_bot.py:4479-4480.

**U7.3 — `match['overlap_pct']` is bracket-indexed in three log statements.** news_bot.py:4400,
:4410, :4440 (and :4478 on the send). The AC «Лог-строки дедупа не печатают процент
пересечения для broad-ветки» is satisfied by **branching the log on `match.get('pairs')`**,
not by dropping the key from `_pair_match`: a `KeyError` there lands inside the fail-open
handler (news_bot.py:4505-4531) and degrades the gate silently at one `[E016]` per hour.
Keep `overlap_pct` in the dict; just stop printing it. (Sharpest breakage edge, carried
forward from §6.6.)

**U7.4 — «Правило симметрично» is not free.** See §U1.1: it holds only if the gate reads
the same `article.get('title') or entry.get('title') or ''` expression that
news_bot.py:4547 writes to the row. Reading a bare `article['title']` silently
reintroduces order-dependence for any article whose fetcher returned an empty title.

**Minor, no action needed:** the `[E015]` log at news_bot.py:4398-4400 prints the same
unit-ambiguous `overlap_pct` on the `|D` pair-rule path. Out of scope per «Поведение
распознающей (`|D`) пары не изменилось», but it is the same defect and a one-line fix if
the tech-spec wants consistency.

## U8. Docs that become false

- `architecture.md:328` — «Both verdicts are terminal.» False for a subject-rejected `|B`.
- `architecture.md:328` — the `|B` line needs the title-declaration condition and the
  measured 95 % FP rate that motivated it (`deployment.md:365-375` currently instructs
  the operator to watch for exactly this).
- `project.md:41` — «a shared *broad* pair soft-flags it» needs the same qualifier.
- `architecture.md:341` — the funnel counter list gains `dedup_suppressed`.
- **In-code prose:** `_check_cross_source_dedup`'s docstring (news_bot.py:2809-2818) and
  the inline comment at news_bot.py:2846-2848 both assert terminality of both verdicts.
