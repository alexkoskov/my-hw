# Code Audit — dedup-model-series (Task 7)

- **Auditor:** Task 7 Code Audit agent (code-reviewing methodology, holistic whole-feature)
- **Date:** 2026-07-14
- **Branch:** dev (all feature code committed, Tasks 1–6)
- **Scope:** Cross-component consistency + system invariants of the whole feature perimeter, read as one system: `model_extractor.py`, `news_bot.py` (gate + toggle), `admin_alerts.py`, `backfill_fingerprints.py`, `pending_articles_repo.py` (read-only carry-through), calibration fixture, 3 deploy FILES-arrays, runbook. OWASP depth = Task 8; test-quality depth = Task 9 (touched only where it affects a seam).

---

## Verdict: **READY**

Zero blockers, zero majors. The load-bearing invariants all hold on the real committed code (not just per the spec). Findings are 2 minors + 2 nits, none of which affect publish-safety, terminality, or the fail-safe polarity. Full suite green for reference (`pytest -q` → **1241 passed**, no exclusions).

---

## Summary

The five modules compose cleanly. The pair-key format is byte-consistent from the single producer (`model_extractor.extract_fingerprint` / `_build_pairs`) through every consumer (gate `_pair_rule_verdict`, `shares_pair`, both alert builders, the fixture) — verified end-to-end by running the real extractor over all 8 calibration pairs: **8/8** shared-pairs and `any_distinctive` flags match the fixture's `expected_*`. The gate refactor is correctly terminal (block/flag return immediately, backstop only on pass), the fail-safe polarity is correct and order-independent (`|D` wins `|B`), there is no duplicate toggle/tier initialisation, back-compat JSON reads are `.get(...) or []` throughout, and the FILES-array invariant holds (no new first-party import; all three arrays byte-identical). Backfill idempotency and the widened re-select were confirmed against SQLite directly. The deferred items from decisions.md are all acceptable (fail-safe under-match direction preserved).

---

## Confirmed invariants (clean checks — stated explicitly)

1. **Gate branch order + terminality (Decision 3).** `_check_cross_source_dedup` (news_bot.py:1098–1166): pair rule runs FIRST (`if DEDUP_SERIES_ENABLED and pairs`), and on `block`/`flag` returns immediately (news_bot.py:1147–1150) — the backstop is unreachable after a pair-rule fire. The empty short-circuit is correctly re-gated to `not strict and not series` (news_bot.py:1158). The backstop runs only on pair-rule pass (news_bot.py:1163–1166). In `job()` (news_bot.py:2302–2353) exactly one of block/flag/pass is acted on per tick → one verdict, one ping. **No double verdict / double ping / fall-through.**
2. **Fail-safe polarity (Decision 2).** `_SERIES_DEFAULT_TIER='broad'` (model_extractor.py:157); `_tier_suffix` defaults any non-`distinctive` tier to `'B'` (model_extractor.py:186–188); theme-only keys are hard-coded `'B'` (model_extractor.py:425); a `|D` key is emitted only when the series is lexicon-tagged `distinctive` AND a concrete model is present (`_build_pairs`, model_extractor.py:418–425). `|D` wins `|B` order-independently via scan-and-remember (`_pair_rule_verdict`, news_bot.py:1025–1034) — a first-seen broad match is remembered but the scan continues, and a `|D` in a candidate's shared set short-circuits to block. Verified both orderings and the same-candidate-both-tiers case.
3. **No duplicate initialisation.** `DEDUP_SERIES_ENABLED` is read once at import (news_bot.py:130–132), same pattern as `DB_FILE`; the gate reads the bare module global (news_bot.py:1144), which resolves to `news_bot.DEDUP_SERIES_ENABLED` at call time (monkeypatchable in tests, env+restart in prod). No second `os.getenv` in the gate body; `_SERIES_DEFAULT_TIER` and `_TIER_SUFFIX` are single sources of truth.
4. **Pair-key format consistency.** `"<model>|<series>|<tier>"` / `"*|<series>|B"` is produced only in `_build_pairs` and consumed identically by: gate set-intersection + `endswith('|D')` (news_bot.py:1019–1025); `shares_pair` (model_extractor.py:590–594); `_render_pair` split/strip-suffix (admin_alerts.py:436–442); backfill (presence-of-key only, never parses the key); fixture `expected_shared_pairs`. Confirmed live: fixture 8/8 exact match against the real extractor.
5. **Back-compat `model_fingerprint` JSON (no migration).** `{strict,brands}` → `{strict,brands,series,pairs}` read defensively as `.get('pairs') or []` in every consumer (model_extractor.py:590–591; news_bot.py:1136–1138, 1019, 1078–1079). Pre-feature rows with a `None`/non-dict/2-key fingerprint are skipped silently in both rules. The producer's empty return (model_extractor.py:498) is the 4-key empty form and matches backfill's empty marker byte-for-byte.
6. **FILES-array invariant (Decision 6).** No new first-party import was added to `news_bot.py` — the feature lives entirely in already-listed modules (`model_extractor`, `admin_alerts`, `pending_articles_repo`, `backfill_fingerprints`). All three arrays (deploy.sh:37–63, deploy.yml:123–149, deploy_test.yml:94–120) are byte-identical and contain every feature module. **No ImportError-crashloop risk.**
7. **Degraded + toggle-off paths.** The whole gate call in `job()` is wrapped in `try/except Exception` → `[E016]` (rate-limited) + `fp=None` + publish (news_bot.py:2294–2390). Toggle off → the pair rule is skipped entirely (news_bot.py:1144) and only the legacy backstop runs, unchanged.
8. **Backfill widened re-select + idempotency (Decision 7).** Re-select catches NULL / corrupt / old-2-key via the `CASE WHEN json_valid(...) THEN json_extract('$.pairs') ELSE NULL END IS NULL` guard; `_already_backfilled` is a corrupt-safe parse-then-probe (backfill_fingerprints.py:157–177). Verified against SQLite directly: 4-key empty & filled → terminal; old-2-key, corrupt, NULL → re-selected. `json_extract('$.pairs')` is a static literal; `--days` is the only bound param.
9. **Carry-through (`pending_articles_repo`, unmodified).** `insert_pending` JSON-dumps the full fingerprint dict; `move_to_published` copies the raw `model_fingerprint` blob verbatim (pending_articles_repo.py:616–636) so `series`/`pairs` ride along with no per-key handling; `list_recent_*_fingerprints` deserialise the full dict. No key loss.
10. **Load-time integrity assertions.** Pipe/newline integrity + tier-consistency assertions (model_extractor.py:162–178) are present and pass at import.
11. **ReDoS bounds (brief — depth is Task 8).** All new regexes use bounded quantifiers only: `_alias_to_pattern` relaxes spaces to `\s{1,3}` over `re.escape`d literals (model_extractor.py:324); `_SERIES_RE` is built from those bounded fragments; `_SERIES_ACRONYM_RE` is a literal alternation; `_canonical_series` uses `\s{1,3}`. No unbounded `\s+`/`\s*`.

---

## Findings

### Minor

**M1 — Acronym series regex is hand-maintained and decoupled from the lexicon (drift risk + `zamac` lowercase under-match).**
`model_extractor.py:314` (`_ACRONYM_ALIASES`) and `model_extractor.py:340` (`_SERIES_ACRONYM_RE = re.compile(r'\b(?P<series>SDCC|RLC|STH|ZAMAC|Zamac)\b')`).
Unlike `_SERIES_RE`, which is built programmatically from `SERIES_LEXICON` and therefore can never drift, the acronym pattern is a hand-typed alternation. Two consequences: (a) adding a future acronym alias to `_ACRONYM_ALIASES`/`SERIES_LEXICON` while forgetting the regex branch produces a silent under-match with no test/import signal; (b) the lexicon key `'zamac'` is lowercase, but the case-sensitive regex only matches `ZAMAC`/`Zamac` — a body writing lowercase "zamac" yields no series token (confirmed live: `extract_series("… zamac …")` → `[]`). Both are **under-matches** (miss a series → at worst miss a broad soft-flag), so they are in the fail-safe direction and cannot cause a false hard-block. Severity is minor only because of that.
*Suggested fix:* derive the acronym pattern from `_ACRONYM_ALIASES` (emit the intended case variants per alias) the way `_SERIES_RE` is derived, or add a load-time assertion that every `_ACRONYM_ALIASES` entry has a corresponding regex branch. Since `zamac` is broad-tier, extending its casing is optional.

**M2 — Broad-flag terminality preempts the legacy ≥50% set-overlap hard-block (intended, but must be an explicit operator expectation).**
`news_bot.py:1147–1150` (pair-rule `flag` is terminal).
When two articles share a broad `|B` pair (same model + same recurring/broad series, e.g. a Car Culture casting), the gate soft-flags and **publishes** — even if their car-set `similarity` would have been ≥0.50 and the legacy backstop would have hard-blocked them. This is a deliberate, spec-sanctioned behaviour change: Decision 3 makes flag terminal to avoid the contradictory `[E014]`+`[E015]` double-ping, and calibration `pair-1-real-2026-06-03` (the real cross-source Car Culture dupe) is pinned `expected_verdict='soft-flag'` on purpose. The dupe is still recoverable (the operator gets the `[E014]` ping and can remove it via `hw_review.py`). No code change recommended; flagged so the report is honest that a broad-line republish that used to auto-block now publishes-and-notifies. *Suggested action:* confirm this expectation is stated in the `deployment.md` runbook / operator note (it is the visible behaviour change post-toggle-on).

### Nit

**N3 — Pair-verdict `overlap_pct` is a pair-Jaccard %, surfaced only in the block INFO log as "overlap %d%%".**
`_pair_match` (news_bot.py:988) computes `overlap_pct` from shared-pairs/union-of-pairs; for a pair verdict this is a different metric than the set-overlap similarity %. It is **not** shown in the operator ping (the `[E015]`/`[E014]` builders render the matched-pair block whenever `pairs` is truthy and ignore the percent), so this is purely cosmetic in the log line at news_bot.py:2304–2307. Consider omitting the `%` for pair verdicts or labelling it distinctly, to avoid a confusing "overlap 100%" in logs for a single shared distinctive pair.

**N4 — Toggle-off + pop-culture item runs a no-op backstop instead of short-circuiting.**
`news_bot.py:1158`. With the toggle off, a pop-culture article (empty `strict`, non-empty `series`) fails the `not strict and not series` short-circuit and falls into the backstop, which fetches 30-day candidates and computes `similarity` that always returns 0.0 (empty strict → AC6 guard) → pass. The verdict is correct; only a wasted fetch + N similarity calls in the temporary kill-switch state. Negligible; not worth a code change unless the toggle-off path is ever hot.

---

## Deferred-item judgments (from decisions.md, explicitly weighed)

| Item | Verdict | One-line rationale |
|------|---------|--------------------|
| `extract_fingerprint` length (~89 physical / ~40 executable) | **accept** | Under the 50-line executable guideline; already factored into `_gather_text`/`_pass3_series`/`_build_pairs`. |
| `backfill_one` length (63 physical / ~30 executable) | **accept** | Under the guideline once the docstring is excluded; single clear responsibility. |
| Model-token exact-match limitation (only one side names a model) | **accept** | Produces an under-match (missed dupe, recoverable) not a false block — the fail-safe direction is preserved. |
| `zamac` lowercase not matched | **minor (M1)** | Under-match, broad-tier only; folded into the acronym-regex drift finding. |
| Toggle blank→on deviation vs the task's impl-hint tuple | **accept** | Deliberate and correct — matches user-spec AC6 / Decision 4 "default on"; reverting would invert the intended default. |

---

## Notes for the lead

No fixer is required for READY. If an optional cleanup pass is desired, M1 (derive/assert the acronym regex from the lexicon) is the only finding with a durable maintainability payoff; M2 is a documentation confirmation, not a code change; N3/N4 are cosmetic. Security depth (ReDoS/SQL/untrusted tokens in pings) is deferred to Task 8; test-quality depth to Task 9.
