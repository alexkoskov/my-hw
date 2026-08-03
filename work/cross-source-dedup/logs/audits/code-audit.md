# Code Audit — cross-source-dedup

**Auditor:** code-auditor (skill: code-reviewing)
**Date:** 2026-06-05
**Scope:** 5 files — `model_extractor.py` (new), `backfill_fingerprints.py` (new), `pending_articles_repo.py` (modified), `admin_alerts.py` (modified, E014/E015/E016 builders only), `news_bot.py` (modified, `_check_cross_source_dedup` helper + dedup gate in `job()`).
**Reference patterns read:** `boilerplate_filter.py`, `outage_state.py`, `hw_review.py`, `deploy.sh`.

## Verdict

**PASS-WITH-NOTES**

Implementation is solid, internally consistent, and aligned with tech-spec Decisions 1–14. JSON shape `{"strict": [...], "brands": [...]}` is uniform across producer / storage / consumer. Compiled regex and lexicon are true module-level singletons. The dedup gate honours Decision 14 placement and Decision 12 broad-except contract. No Critical findings. A small number of Major / Minor items are listed below — none block deploy, but two (private-API coupling, double-connection in degraded path) are worth a follow-up issue.

## Summary

Holistic quality is high: the new `model_extractor.py` is a faithful mirror of `boilerplate_filter.py`; the four new `bot_state`-backed helpers follow the `outage_state.py` tolerance pattern (corrupt timestamp → warning + False, never raises); `backfill_fingerprints.py` matches the `hw_review.py` argparse + `main(argv=None) -> int` + `sys.exit(main())` shape; `deploy.sh` ships both new top-level files. The cross-component JSON-shape contract is consistent: `extract_fingerprint` produces a dict with two sorted lists; `pending_articles_repo._PENDING_JSON_COLS` + `_PUBLISHED_JSON_COLS` round-trip it; `_check_cross_source_dedup` consumes `cand_fp.get('strict')` defensively; `alert_cross_source_dupe` consumes the `models` list as the intersection of strict sets. Error-handling discipline matches the spec — narrow `except sqlite3.OperationalError` only in `init_schema` migration; broad `except Exception` only in `news_bot.job()` dedup gate (Decision 12) and around network fetch in `backfill_one` (narrow scope, explicit `noqa: BLE001` comment). All recommendations below are tracked-and-improve rather than fix-before-deploy.

## Findings

### Critical

No critical findings.

### Major

- **[F-M-1] `news_bot.py:1945, 2016` — Cross-module access to a private symbol `pending_repo._connect()`.**
  The dedup gate calls `pending_repo._connect()` (leading-underscore = module-private by convention) to open its own short-lived connection. This is a real coupling — if `pending_articles_repo` ever renames `_connect` or changes its return contract, the dedup gate breaks silently (no tests cover the symbol name itself).
  *Pattern reference:* `outage_state.py:105` keeps its `_connect` private and does NOT expose it to other modules; `pending_articles_repo.py` itself keeps it private (`def _connect` line 174).
  *Recommendation:* either promote a small public `connect()` helper on `pending_articles_repo` (1-line wrapper) and switch both call sites, OR add a public `open_dedup_connection()` helper on the repo that returns a connection with the PRAGMA / busy_timeout config the dedup path actually needs. The latter would also pave the way for adding `PRAGMA busy_timeout = 5000` (matching `outage_state._connect`) without leaking the choice into `news_bot.py`.

- **[F-M-2] `news_bot.py:1944-2042` — Double connection in degraded-mode path.**
  The happy path opens `dedup_conn` inside the outer `try`, uses it for reads and the optional `mark_pair_pinged`, then closes it in `finally`. When the outer `try` raises, control enters the `except Exception` branch — at which point a brand-new `rl_conn = pending_repo._connect()` is opened solely for the E016 rate-limit bookkeeping. This works (the first conn is already closed by the `finally`), but it does mean the degraded path holds TWO short-lived connections sequentially, and the rate-limit bookkeeping is itself wrapped in its own `try/except/finally`. Slight smell of nested error handling that could be simplified.
  *Pattern reference:* `outage_state.record_outage_event` opens exactly one connection inside one `try/commit/rollback/finally` for the entire state transition.
  *Recommendation:* refactor the degraded branch to open the rate-limit conn unconditionally at the top of the `except`, do the bookkeeping, then close in a single `finally`. Two-level try is intentional (rate-limit bookkeeping must not raise out of the broad handler), so the nesting can stay — but consolidate the connection lifecycle.

### Minor

- **[F-Mi-1] `model_extractor.py:69-80` — Lexicon ships 36 entries, not 35.**
  `_LEXICON` documents 35 tiered brands but actually contains 36 (`mini` added per code-research §14.A.2). Author noted this in the trailing comment ("close enough; Decision 2 phrased the target as '~35'"). Minor inconsistency between header tier comment and actual frozenset content.
  *Recommendation:* update the tier-3 header comment line (`# Tier 3 (European): ... Mini, ...`) to make `Mini` explicit, OR drop `mini` if it's brittle.

- **[F-Mi-2] `model_extractor.py:165` — Uppercase-brand pattern lacks `re.UNICODE` flag and trailing whitespace assertion.**
  `_UPPERCASE_BRANDS_RE` uses `\b(?P<brand>AMC|BMW|Lotus)\b`. Edge case: `BMWi` (no separator) won't match because `\b` after `BMW` requires a non-word char, but `BMW3` won't match either (also good — `bmwxyz` would fail, and `BMW3` ambiguously could be a model). Fine in practice; just worth a one-line docstring note that `\b` rejects `BMWi`, `BMW3` etc. Not a bug.

- **[F-Mi-3] `pending_articles_repo.py:138` — `_PUBLISHED_JSON_COLS` lacks the `published_at` audit-friendly comment that `_PENDING_JSON_COLS` has.**
  Trivial. Comment line above `_PUBLISHED_JSON_COLS` says "Currently only the dedup fingerprint added in the 2026-06-XX migration (Decision 11)" — good. Not an issue, listed for completeness.

- **[F-Mi-4] `backfill_fingerprints.py:243-249` — SELECT projection duplicates `list_recent_published_fingerprints` schema knowledge.**
  The backfill SELECT inlines `link, source_name, title, model_fingerprint` instead of reusing the list helper. The author justified this in the comment: backfill needs the `IS NULL` filter that the helper doesn't expose, so the helper is genuinely not usable as-is. Acceptable trade-off; the comment makes the divergence explicit. Listed here only so a future refactor knows there's a precedent for adding a `model_fingerprint IS NULL`-filtered variant if backfill grows.

- **[F-Mi-5] `news_bot.py:2014` — `logger.exception(...)` with no link context.**
  `logger.exception("dedup gate failed, degraded mode active")` includes the traceback but not the article link. Operator reading journalctl after an E016 ping has to grep nearby lines to find which article hit the bug.
  *Recommendation:* append `link` to the log line: `logger.exception("dedup gate failed for %s, degraded mode active", link)`.

### Nit

- **[F-N-1] `model_extractor.py:78-80` — Sanity comment ends without final punctuation in one line ("Tracked in code-research §14.A.4 mainline-frequent list."). Cosmetic.
- **[F-N-2] `backfill_fingerprints.py:184` — `# noqa: BLE001 — broad catch is the contract` is the right thing to do, but the comment could spell out which contract (Decision 10 / backfill spec — transient fetch errors leave NULL).
- **[F-N-3] `admin_alerts.py:430-446` — E016 builder format is intentionally short; no findings, just confirming the substring anchor "Дедуп в degraded mode" matches what the tech-spec specifies.

## Cross-component consistency

### JSON shape `model_fingerprint`

Verified uniform across the four touchpoints:

| Layer | File:line | Shape consumed/produced |
|-------|-----------|-------------------------|
| Producer | `model_extractor.py:284-287` | `{'strict': sorted(list), 'brands': sorted(list)}` |
| Storage write | `pending_articles_repo.py:262, 880-894` | `_dumps({...})` — `ensure_ascii=False`, NULL-preserving |
| Storage read | `pending_articles_repo.py:133-138, 160-171` | `_PENDING_JSON_COLS` / `_PUBLISHED_JSON_COLS` include `model_fingerprint` → `_loads_or_none` → dict |
| Carry-through | `pending_articles_repo.py:616-636` (`move_to_published`) | SELECT + INSERT keep the column verbatim — Task 4 scope-creep delta is correct |
| Consumer (gate) | `news_bot.py:769-786` | `isinstance(cand_fp, dict)` guard; `cand_fp.get('strict') or []` defensive |
| Consumer (alert) | `admin_alerts.py:391` (E014) | `models: list[str]` — passed from `_check_cross_source_dedup` `shared` (sorted intersection of strict sets) |

NULL vs `{"strict":[],"brands":[]}` distinction (Decision 5) is implemented correctly:
- `extract_fingerprint` on empty article returns `{'strict': [], 'brands': []}` (extractor ran, no brands → terminal empty).
- `insert_pending` with absent `entry.get('model_fingerprint')` → `_dumps(None)` → NULL (not yet processed).
- Backfill terminal computed-empty path (`backfill_fingerprints.py:188-195`) stores the empty-dict shape, NOT NULL. This matches Decision 10 idempotency contract.

**No findings.**

### Compiled regex / lexicon singleton compliance

Verified via `grep -rn "_BRAND_RE\|_MODEL_AFTER_BRAND_RE\|_UPPERCASE_BRANDS_RE\|_LEXICON\|_BRAND_ALIASES" --include="*.py"`:

| Resource | Owner | Count | Notes |
|----------|-------|-------|-------|
| `_LEXICON` (frozenset) | `model_extractor.py:69` | 1 | Module-level singleton, immutable. |
| `_BRAND_ALIASES` (dict) | `model_extractor.py:83` | 1 | Module-level singleton. |
| `_MODEL_AFTER_BRAND_RE` (compiled) | `model_extractor.py:115` | 1 | `re.compile(...)` at module load. |
| `_UPPERCASE_BRANDS_RE` (compiled) | `model_extractor.py:165` | 1 | Module-level. Case-sensitive (no `re.I`) per Decision 3. |
| `_MODEL_EXTRA_KEEP_RE` (compiled) | `model_extractor.py:147` | 1 | Bonus singleton for the extra-token filter. |

No duplicate compilations inside `extract_fingerprint` body. Perf budget (<10 ms on 10 KB body per AC) is preserved.

**No findings.**

### Error-handling discipline

Verified across all 5 files:

| Site | Handler | Spec basis |
|------|---------|-----------|
| `pending_articles_repo.init_schema` migration ALTERs | `except sqlite3.OperationalError: pass` | Decision 11 (idempotent migration). |
| `pending_articles_repo._parse_dt_tolerant` | `except ValueError: log warning, return None` | Mirrors `outage_state._parse_dt` (Decision 6). |
| `pending_articles_repo.insert_pending` PK conflict | `except sqlite3.IntegrityError: rollback, return False` | Existing pattern (concurrent prep race). |
| Transactional moves (`move_to_published`, `move_to_failed`, `skip_pending`, `retry_from_failed`) | `except Exception: rollback, raise` | Existing transactional pattern — broad on purpose. |
| `news_bot.job()` dedup gate outer | `except Exception:` + `logger.exception(...)` + E016 ping | Decision 12 / AC9 — broad handler is the contract. |
| `news_bot.job()` E014/E015/E016 send | `except Exception as notify_err: logger.error(...)` (nested) | Defensive — notification failure must not propagate. |
| `news_bot.job()` rate-limit bookkeeping in degraded path | `except Exception: logger.exception(...)` | Defensive — bookkeeping fault must not leak past the broad handler. |
| `backfill_fingerprints.backfill_one` fetch | `except Exception as exc: log error, return 'error'` with `noqa: BLE001` | Decision 10 — fetch is the only network call, others should bubble up. |

The narrow vs broad distinction is followed deliberately: migrations are narrow (`OperationalError` only), the dedup gate and notification sends are broad on purpose (contract: "dedup never blocks publishing"). No file confuses one discipline for the other.

**No findings.**

## Shared Resources Architecture compliance

Re-walked the tech-spec "Shared resources" table:

| Resource | Owner (creates) | Consumers verified | Compliance |
|----------|----------------|---------------------|------------|
| `_BRAND_RE` family (`_MODEL_AFTER_BRAND_RE`, `_UPPERCASE_BRANDS_RE`, `_MODEL_EXTRA_KEEP_RE`) | `model_extractor.py` module load | `extract_fingerprint` only (within `model_extractor`) | OK — single instance, module-level. |
| `_LEXICON` (frozenset) | `model_extractor.py` module load | `extract_fingerprint`, the internal `if model_lower in _LEXICON` guards in the same module | OK — immutable, single instance. |
| `bot_state` k/v table | `pending_articles_repo.init_schema` | New consumers: `is_pair_rate_limited`, `mark_pair_pinged`, `is_dedup_degraded_rate_limited`, `mark_dedup_degraded_pinged` (this feature); existing: outage state machine (`outage_state.*`). Key namespaces are disjoint (`softflag_pair:*` and `dedup_degraded_last_pinged_at` vs `outage_*` / `last_ping_sent_at` / `ping_count` etc.). | OK — no key collisions; both readers/writers tolerate corrupt values. |

The 4 new bot_state-backed helpers (`pending_articles_repo.py:931-1007`) are correctly added as a new consumer of the shared k/v table. No second instance of the table or competing schema.

**No findings.**

## Pattern adherence

### `model_extractor.py` ↔ `boilerplate_filter.py` (Decision 1)

Compared section ordering:

| Section | `boilerplate_filter.py` | `model_extractor.py` | Match |
|---------|-------------------------|----------------------|-------|
| Module docstring | Lines 1-24 | Lines 1-29 | OK |
| `from __future__ import annotations` | Line 26 | Line 31 | OK |
| `import re` + typing | Line 28-29 | Lines 33-34 | OK |
| Bounded length constants | `_MAX_BOILERPLATE_LEN` (line 37) | `_DAYS_MIN`/`_DAYS_MAX` are in backfill, not here — N/A in extractor | N/A — extractor doesn't need length cap |
| Compiled regex constants | `_LONG_BOILERPLATE_PATTERNS` (54), `_PLATFORMS_RE` (77) | `_MODEL_AFTER_BRAND_RE` (115), `_MODEL_EXTRA_KEEP_RE` (147), `_UPPERCASE_BRANDS_RE` (165) | OK — same shape |
| Pure helper functions, no I/O | `is_boilerplate`, `filter_boilerplate` | `_jaccard`, `_canonical_brand`, `_gather_text`, `extract_fingerprint`, `similarity` | OK — pure, no I/O, no logging |

**No findings.** Pattern adherence is high; the extractor reads like a natural twin to the boilerplate filter.

### `bot_state` helpers ↔ `outage_state.py` (Decision 6)

| Aspect | `outage_state.py` | `pending_articles_repo.py` new helpers | Match |
|--------|-------------------|-----------------------------------------|-------|
| Connection ownership | Short-lived `_connect()` per call, `finally close()` | Caller passes `conn` (long-lived in backfill, short-lived from `news_bot.py`) | DIVERGENT — by design (`pending_articles_repo.py:829-833` comment explains: backfill needs long transaction). |
| Timestamp parsing | `_parse_dt` — `ValueError` → warning + None | `_parse_dt_tolerant` — same shape | OK |
| Storage | `INSERT OR REPLACE INTO bot_state (key, value)` | Same | OK |
| Key naming | `outage_started_at`, `ping_count`, etc. | `softflag_pair:{...}\n{...}`, `dedup_degraded_last_pinged_at` | Distinct namespaces — OK |
| Timezone | tz-aware ISO-8601 | tz-aware ISO-8601 | OK |
| Corrupt-tolerance | Returns False / 0 / None, never raises | Same | OK |

Divergence on connection ownership is justified and documented (`pending_articles_repo.py:828-834` block comment). The rest is a faithful mirror.

**No findings.**

### `backfill_fingerprints.py` ↔ `hw_review.py` (Decision 10)

| Aspect | `hw_review.py` | `backfill_fingerprints.py` | Match |
|--------|----------------|------------------------------|-------|
| Shebang | `#!/usr/bin/env python3` | Same | OK |
| `from __future__ import annotations` | Line 56 | Line 59 | OK |
| `import news_bot` BEFORE `logging.basicConfig` | Line 74 (before any `basicConfig`) | Line 72 (before `main()` calls `basicConfig`) | OK — convention honored |
| `_build_parser() -> argparse.ArgumentParser` | Line 762 | Line 115 | OK |
| `main(argv=None) -> int` | Line 821 | Line 216 | OK — exact signature |
| `if __name__ == '__main__': sys.exit(main())` (pragma no cover) | Line 828 | Line 305 | OK |
| Top-level exception policy | Bubbles up with traceback | Same; narrow `try/except` only over `fetch_full_article` | OK |

Verified `news_bot._TokenRedactingFilter` (`news_bot.py:347-352`) is attached unconditionally at module load — so the import-order convention is symbolic in current code, but the comment correctly documents it as a future-proofing measure. Matches the tech-spec AC and Decision 10.

**No findings.**

## Coverage by review dimension

1. **Correctness** — OK. JSON shapes round-trip; dedup decision tree matches Decision 7 thresholds (≥0.50 block, [0.30, 0.50) flag, else pass); `mark_processed(link, title, pub_date)` on hard-block is present at `news_bot.py:1958-1962` with all three args; `move_to_published` carries `model_fingerprint` through (AC2). See [F-M-1], [F-M-2] for non-blocking nits.
2. **Complexity / over-engineering** — OK. `extract_fingerprint` is 60 lines, two `finditer` passes, no nested loops. `_check_cross_source_dedup` is 80 lines with clear early-returns. Backfill `main()` is 90 lines including the print summary. None exceed the 100-line threshold.
3. **Consistency** — OK. JSON shape uniform; key naming consistent; tolerant timestamp parsing mirrors `outage_state.py`.
4. **Error handling** — OK. Discipline split (`except OperationalError` narrow in migration, `except Exception` broad in dedup gate per Decision 12) is honored — see "Cross-component" section table for evidence. [F-Mi-5] is the one minor.
5. **Observability (logging)** — OK with minor: [F-Mi-5] — `logger.exception` in degraded path lacks the article link context.
6. **Performance** — OK. Compiled regex singletons (no per-call recompile); two SQL reads per article (7-day window); `_check_cross_source_dedup` short-circuits on empty fingerprint. Backfill 1s sleep between fetches is intentional rate-limit defense.
7. **Maintainability** — OK. Each helper has a docstring linking to Decision N / AC. The lexicon's tier comments make future additions discoverable. [F-Mi-1] is a 1-line tier-comment fix.
8. **Naming** — OK. E014/E015/E016 builder names match Task 4 callers (`alert_cross_source_dupe`, `alert_cross_source_blocked`, `alert_dedup_degraded`); private helpers prefixed `_`; threshold constants `_DEDUP_BLOCK_THRESHOLD` / `_DEDUP_FLAG_THRESHOLD` are descriptive.
9. **Security (cross-check)** — Not the focus of this audit; brief notes: SQL is parametrized (`?` placeholders, `f"-{int(days)}"` pattern with int-coercion — same as `list_pending_stale`); admin pings include links but no token/PII content; redactor filter is module-load attached. Security-auditor (Task 7) owns the full pass.
10. **Test cross-check** — Not the focus; brief notes: schema-pin tests updated (`EXPECTED_PENDING` and `EXPECTED_PUBLISHED` both contain `model_fingerprint`, `EXPECTED_PENDING_COLUMNS` and `EXPECTED_PUBLISHED_COLUMNS` updated); calibration must-pass test exists per Decision 13; integration tests cover all 4 dedup branches per Task 4 contract. test-master (Task 8) owns the full pass.
11. **Deviations vs tech-spec / user-spec** — OK. Lexicon ships 36 not 35 (already documented as in-scope per Decision 2 "~35" wording). `move_to_published` two-line extension to carry `model_fingerprint` is documented in `decisions.md` Task 4 as a known scope delta. No undocumented deviations spotted.

## Recommendations

**Before deploy (none blocking, optional):**
- [F-Mi-5] Add `link` context to the `logger.exception` line in the dedup gate degraded path. One-line fix.

**Track as follow-up issue (post-deploy):**
- [F-M-1] Promote a public `pending_articles_repo.connect()` helper so `news_bot.py` stops reaching into `_connect()` (private). Pairs naturally with [F-M-2].
- [F-M-2] Consolidate the degraded-mode rate-limit bookkeeping into a single connection-managed block. Reduces nested try-depth in `news_bot.py:job()` and makes the connection lifecycle explicit.

**Defer / nit / no action:**
- [F-Mi-1], [F-Mi-2], [F-Mi-3], [F-Mi-4], [F-N-1], [F-N-2], [F-N-3] — cosmetic / documentation polish. Can be folded into a future maintenance pass or left alone.

**No spec changes recommended.** Decisions 1–14 are honored; the 36-vs-35 lexicon detail is within the Decision 2 "~35" wording; the `move_to_published` carry-through is a necessary mechanical consequence of AC2 that Task 2 missed and Task 4 correctly absorbed.

---

**Files reviewed (5):**
- `model_extractor.py` — No findings beyond [F-Mi-1], [F-Mi-2], [F-N-1].
- `backfill_fingerprints.py` — No findings beyond [F-Mi-4], [F-N-2].
- `pending_articles_repo.py` — No findings beyond [F-Mi-3].
- `admin_alerts.py` (E014/E015/E016) — No findings beyond [F-N-3]. Builders are pure, substring anchors are present, format matches Decision 7.
- `news_bot.py` (dedup gate + helper) — [F-M-1], [F-M-2], [F-Mi-5].

**Reference files compared:** `boilerplate_filter.py`, `outage_state.py`, `hw_review.py`, `deploy.sh`. All pattern mirrors verified.
