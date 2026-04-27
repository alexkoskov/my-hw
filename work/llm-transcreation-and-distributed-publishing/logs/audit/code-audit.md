# Code Audit — `llm-transcreation-and-distributed-publishing`

**Auditor:** code-auditor (Task 14)
**Date:** 2026-04-27
**Branch:** `dev`
**Last commit:** `e659d05 chore: complete wave 8 — task 13 done (deploy bundle)`
**Scope:** Holistic static review of feature deliverables (Wave 1–8) before pre-deploy QA.
**Output:** read-only report; no source/test files touched.

---

## 1. Focus-area conclusions

### Focus 1 — Decision 9 idempotency in `_fallback_publish` — **PASS**

`news_bot.py:850–890` (Step 2: Telegraph) writes the URL via
`pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)` at
**line 890**. `news_bot.py:907–919` (Step 4: Telegram teaser) calls
`send_telegraph_teaser(...)` at **line 915**. Ordering invariant is preserved:
`mark` (890) < `teaser` (915). On the API-level outage degraded path the body
flow is identical: lines 798–848 only mutate Step-1 state, and steps 2–5 run
their canonical sequence regardless of which engine produced the RU body.

```python
# news_bot.py:885-890
        # Persist BEFORE Telegram so a teaser failure leaves the row
        # retry-idempotent (Decision 9). ``move_to_published`` below
        # reads ``telegraph_url`` from its own explicit argument, not
        # from the row — so the pending-row copy is the idempotency
        # anchor, not an input to the move.
        pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)
```

Cross-test: `tests/test_fallback_publish_paths.py:201–208` and `:297–302`
explicitly assert `names.index('mark') < names.index('teaser')` for both Claude
happy path and Google per-article fallback. Outage degraded path is exercised
in `TestOutageDegradedThenReraises` (line 365+).

### Focus 2 — Decision 5 SDK exception classification — **PASS**

`claude_transcreation._classify_exception` (lines 340–388) covers all 9
documented SDK classes correctly:

* API-level → `ClaudeOutageError`: `APIConnectionError`, `APITimeoutError`,
  `RateLimitError`, `InternalServerError`, `AuthenticationError`,
  `PermissionDeniedError`, `NotFoundError` (lines 355–364).
* Per-article → `ClaudeTranscreationError`: `BadRequestError`,
  `UnprocessableEntityError`, generic `APIStatusError` (other) (lines 366–375),
  plus already-typed `ClaudeTranscreationError` re-passed through (377–378).
* Catch-all `anthropic.APIError` → `ClaudeOutageError` (382–385) — operator
  sees a ping rather than silent degradation. Truly unexpected non-anthropic
  exceptions are returned unwrapped (388).

The wrap site (`transcreate_via_claude`, lines 484–496) catches
`anthropic.APIError` and routes through `_classify_exception`, then re-raises.
There is **no** broad `except Exception:` over `client.messages.create(...)`
that would mask classification — confirmed.

Cross-test: `tests/test_claude_transcreation.py:148–235` covers all 9 SDK
exception branches; lines 239+ cover malformed JSON / shape mismatch.

### Focus 3 — `outage_state` state machine — **PASS**

`outage_state._compute_next_state` (lines 267–329) implements the transition
table from code-research §14.4 verbatim:

* `started_at is None` → `ping_1_sent`, `fallback_now=False` (296–303).
* `ping_count<=1`, `elapsed >= 1h` → `ping_2_sent`, `fallback_now=True`
  (309–314); else hold at `ping_1_sent` with `fallback_now=True` (315).
* `ping_count==2`, `elapsed >= 2h` → `google_fallback_active` (319–325); else
  hold at `ping_2_sent` (326).
* `ping_count>=3` → steady-state `google_fallback_active` (329).

`record_outage_event` (336–392) and `record_recovery_event` (395–454) both
open `BEGIN IMMEDIATE` (lines 359, 429) and rollback on exception. `_connect`
applies `PRAGMA busy_timeout = 5000;` per Decision 16 (line 109).

`_get` returns `None` on missing key (line 125). `_parse_dt` (155–167) tolerates
corrupted ISO strings — logs warning, returns `None`. `get_ping_count`
(202–213) tolerates corrupted int strings the same way. Naive datetimes are
rejected at the boundary (`_serialise_dt` lines 174–177; both record_*
functions lines 352–356, 414–418).

Cross-test: `tests/test_outage_state.py` 12 tests cover all four transitions,
persistence, real concurrency under `BEGIN IMMEDIATE`, corrupted-content
tolerance, and naive-dt rejection.

### Focus 4 — `compute_publish_slots` algorithm — **PASS**

`compute_publish_slots.py:45–104`. Key invariants:

* `WINDOW_START=time(13,0)`, `WINDOW_END=time(20,0)`, `MIN_INTERVAL_MINUTES=40`
  (lines 40–42) — match user-spec AC3.
* Hard `>= window_end_dt` returns `(slots=[], carry_over=n)` (84–86) — AC4.
* `effective_start = max(window_start_dt, now)` (88) collapses cron-tick and
  restart cases into one branchless formula.
* `interval = max(remaining/N, MIN_INTERVAL_MINUTES)` (91–92) and
  `slots_count = min(N, floor(remaining/interval) + 1)` (96–97) — AC5.
* The 11/day cap emerges naturally as `floor(420/40)+1 = 11`; **no separate
  constant** is declared, which preserves customisation through the
  `min_interval_min` parameter.
* tz-aware input enforced (71–72); slots inherit `now.tzinfo` via
  `datetime.combine(..., tzinfo=tzinfo)` (80–81).
* `n <= 0` short-circuits to `([], 0)` (76–77).

Cross-test: `tests/test_compute_publish_slots.py` 15 tests cover N∈{0,1,4,7,
10,11,15,20}, restart at 16:00 / 19:50, now=20:00 boundary, now>20:00,
tz-naive raise, tzinfo preservation, module-constants export.

### Focus 5 — Leftover deleted-symbol references — **PASS (with caveat)**

Grep on the live source/test/deploy artefacts (excluding `work/`,
`.claude/skills/project-knowledge/`, `.git`):

```
$ grep -rn "_overflow_fast_track|FALLBACK_THROTTLE_SECONDS|QUEUE_CAP|
           IDLE_TIMEOUT_HOURS|GRACE_WINDOW_HOURS|_idle_fallback_publish" \
    news_bot.py hw_review.py claude_transcreation.py compute_publish_slots.py \
    outage_state.py pending_articles_repo.py .env.example deploy.sh \
    .github/workflows/deploy.yml
→ (no output — clean)
```

PK docs (`patterns.md:185`) carry a single intentional historical note (allowed
per task constraints).

**Caveat — legacy files present on disk as untracked (see Critical Findings #C1):**
the legacy test files `tests/test_overflow.py` (~786 LoC, references
`QUEUE_CAP`, `FALLBACK_THROTTLE_SECONDS`, `_overflow_fast_track`) and
`tests/test_idle_fallback.py` (~492 LoC, references `FALLBACK_THROTTLE_SECONDS`,
`GRACE_WINDOW_HOURS`) are **physically present on disk as untracked files** in
the working tree at audit time, even though commit `050b6eb` deleted them in
Task 9. `git status` shows:

```
Untracked files:
    tests/test_idle_fallback.py
    tests/test_overflow.py
```

`pytest tests/` does not honour git tracking — it picks up every `.py` under
the directory. The files reference symbols that no longer exist on `news_bot`;
running the suite as-is would fail collection (NameError on
`news_bot.QUEUE_CAP` / `news_bot._overflow_fast_track`) or hang on the new
distributed-publish loop. This is a deploy blocker until the files are
removed from disk.

### Focus 6 — Decision 8 flat-path fallback in `_load_prompt` — **PASS**

`claude_transcreation._load_prompt` (lines 135–173) implements the subdir →
flat fallback exactly as required:

```python
# claude_transcreation.py:145-153
    candidate = path
    if not os.path.isfile(candidate):
        flat_fallback = os.path.join(_MODULE_DIR, "ux-guidelines.md")
        if not os.path.isfile(flat_fallback):
            raise FileNotFoundError(...)
        candidate = flat_fallback
```

Cache key is `(path, mtime, body)` so a subdir-miss → flat-hit doesn't return
stale content from the prior load (lines 156–160, addressing the round-1 fix
flagged in Task 03's review).

Cross-test: `tests/test_claude_transcreation.py:299` (`test_load_prompt_subdir_
then_flat_fallback`).

The deploy bundle (`deploy.sh:50`, `.github/workflows/deploy.yml:114`) ships
`ux-guidelines.md` from the subdir; `scp` (without `-r`) flattens it onto the
server's `$DEPLOY_PATH`, matching what `_load_prompt`'s fallback path is built
for.

### Focus 7 — Manual-review path call sites (`hw_review.py`) — **PASS**

`hw_review.py:569–662` (`cmd_publish`) does not import or invoke
`_fallback_publish` / `_fallback_publish_google_only` — it implements its own
publish chain. Cross-checked symbols:

* Telegraph URL persisted via `repo.mark_telegraph_published(...)` at
  **line 627** — BEFORE `send_telegraph_teaser(...)` at **line 637**.
  Decision 9 ordering preserved; matches `_fallback_publish`'s layout.
* `repo.move_to_published(link, telegraph_url, telegraph_path,
  via_review=True)` at **line 655** — `via_review=True` invariant intact.
* Re-uses `from news_bot import send_telegraph_teaser` (line 75) — channel
  teaser format `#<source> #news` is byte-identical for both paths
  (`news_bot.py:611–643`).
* `repo.list_pending` / `repo.count_pending` / `news_bot.SOURCE_EMOJI`
  (`hw_review.py:150, 193, 198`) — all signatures unchanged.

`pending_articles_repo` exports `list_pending`, `count_pending`,
`mark_telegraph_published`, `move_to_published`, `get_pending` — all unchanged
in signature; `init_schema` is the only modification (added `_BOT_STATE_DDL`
on a single line, lines 108–113 + 178). No regressions for `hw_review`.

---

## 2. 11 review-dimensions summary (modules under audit)

Modules covered: `claude_transcreation.py`, `compute_publish_slots.py`,
`outage_state.py`, `news_bot.py` (new sections — `_fallback_publish`, `job`,
`main`, `_TokenRedactingFilter`, `_redact_text`, `_parse_published_at_utc`,
`_fallback_publish_google_only`), `pending_articles_repo.py:_BOT_STATE_DDL +
get_max_published_at`.

| # | Dimension | Verdict | Notes |
|---|-----------|---------|-------|
| 1 | Architectural patterns | OK | Clean separation: `compute_publish_slots` pure; `outage_state` thin SQLite KV + pure transition fn; `claude_transcreation` SDK wrapper; `_fallback_publish` orchestrator. No god objects; cycle `news_bot ↔ pending_articles_repo` resolved by lazy `news_bot.DB_FILE` access (existing pattern). |
| 2 | Separation of concerns | OK | `_compute_next_state` (pure) vs `record_outage_event` (I/O) split is exemplary. `_load_prompt` mixed in same file as SDK wrapper, but justified by mtime caching coupling. |
| 3 | Readability / maintainability | OK | Inline narrative comments throughout `news_bot.job` (steps a/b/c/d/e), `_fallback_publish` (steps 1-5), `outage_state._compute_next_state` (transition table prose). Minor: `news_bot._fallback_publish` is ~220 LoC; close to "extract method" threshold. Local `_google_translate` closure is the right factoring. |
| 4 | Error handling / logging | OK | `sanitize_error_message` shared by every error log path. `_redact_text` covers Telegram + Anthropic key shapes; filter attached at root + each handler + named noisy + anthropic loggers. Outage state failure inside `_fallback_publish` is caught and the publish proceeds in degraded mode — correct (line 839–847). No empty catch blocks. No `print` debugging in active code. |
| 5 | Type safety | N/A | Pure-Python codebase, no MyPy/strict typing in repo. `Optional[...]` hints used consistently in `outage_state` and `claude_transcreation`. |
| 6 | Testing coverage | OK | Per the Wave-by-Wave decisions log: 566 passing across the suite at end of Wave 7. Unit + integration coverage on every new module; `test_distributed_schedule_integration.py` provides 4 end-to-end scenarios. |
| 7 | Dependencies | OK | `anthropic>=0.45.0,<0.46.0`, `pytz>=2024.1` pinned. `pip install --user` on server side. Health check fail-soft on `ANTHROPIC_API_KEY` missing. |
| 8 | Security | OK | API-key shapes redacted at three layers (regex / env-name list / handler filter). `.env` write step uses heredoc-on-stdin discipline (no command-line secrets). `chmod 600 .env` after each run. No hardcoded creds. SQL all parameterized via `?`. See findings I1/I2 below for low-severity follow-ups. |
| 9 | Performance | OK | `compute_publish_slots` is O(N) over `slots_count<=11`. `outage_state.record_recovery_event` uses double-checked locking to avoid `BEGIN IMMEDIATE` contention on the steady-state healthy hot path. `_load_prompt` caches by `(path, mtime, body)` — no re-read on every `transcreate_via_claude` call. No N+1 patterns introduced. |
| 10 | Cross-file consistency | OK | All `news_bot` ↔ `claude_transcreation` ↔ `outage_state` ↔ `pending_articles_repo` ↔ `compute_publish_slots` call signatures match. `_fallback_publish` calls `pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)` — repo signature line 321 takes those three args. `move_to_published(link, telegraph_url, telegraph_path, via_review=...)` — repo signature line 512 matches. `outage_state.record_outage_event(now=datetime.now(timezone.utc))` is tz-aware — OK. |
| 11 | Resource management | OK | `outage_state._connect` short-lived per call, closed in `finally`. `claude_transcreation._DEFAULT_CLIENT` is a lazy singleton (lines 80, 408–419) — only one HTTP client per process. SQLite connections all wrapped in try/finally. No resource leaks. |

---

## 3. Findings (severity-ranked)

### Critical

**C1. Legacy test files present as untracked files in `tests/`.** Severity:
**critical**.
- Location: `tests/test_overflow.py`, `tests/test_idle_fallback.py`.
- `git status` lists both as **Untracked**. They are not in HEAD (they were
  deleted in commit `050b6eb`, Task 9, Wave 5) but they sit on disk inside
  the test-discovery directory.
- Each references symbols that **no longer exist** on `news_bot`
  (`_overflow_fast_track`, `QUEUE_CAP`, `FALLBACK_THROTTLE_SECONDS`,
  `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`).
- Impact: `pytest tests/` does not consult git — it discovers every `.py`
  under `tests/`. Running the suite as-is on the working tree will fail
  collection (`AttributeError: module 'news_bot' has no attribute
  'QUEUE_CAP'`) or hang on the new distributed-publish loop. CI green at end
  of Wave 8 was measured against a clean tree; whoever pulled in these stale
  files (rebase artefact / IDE checkout / parallel agent) re-introduced the
  problem. Pre-deploy QA pytest run will be blocked until the files are
  removed.
- Recommendation: operator must `rm tests/test_overflow.py
  tests/test_idle_fallback.py` BEFORE running the pre-deploy QA suite. After
  removal, no further code changes are needed — the underlying production
  code never referenced these tests. This is the only blocker found in the
  audit.

### High

None.

### Medium

**M1. Dead helper `_fallback_publish_google_only` is a thin pass-through.**
Severity: medium.
- Location: `news_bot.py:1081–1093`.
- Today the helper is `return _fallback_publish(row, via_review=False)` — its
  own docstring acknowledges this. The branch in `job()` that selects between
  the two helpers (lines 1322–1326) is therefore semantically a no-op: both
  paths go through the same body and the `if outage_state.is_fallback_active()`
  check also lives inside `_fallback_publish` (line 788).
- Impact: no functional bug; just dead code increasing surface area. The
  intent (per its own comment) is "Task 9's planned cleanup migrate the
  implementation off `_fallback_publish` without touching `job()`" — but Task
  9 was a pure deletion, so the migration never happened.
- Recommendation: either inline `_fallback_publish(row, via_review=False)`
  directly in `job()` and delete the helper, or implement a real
  Google-only path that skips the `is_fallback_active()` re-check. Defer to
  post-deploy follow-up; not a blocker.

**M2. Cron immediate-run on startup violates the 12:00-МСК invariant.**
Severity: medium.
- Location: `news_bot.py:1444–1448`.
- After registering the daily-12:00-МСК schedule, `main()` calls `job()`
  immediately (line 1448 — "Run immediately for first-boot population").
- This means a container restart at, say, 18:00 МСК will fire the full prep+
  publish flow at 18:00 — mixing slot scheduling with restart timing.
  `compute_publish_slots` handles it (effective_start=now), but the operator-
  visible behaviour deviates from "daily 12:00 cron tick" promised in tech
  spec §Architecture step 7.
- The crash-loop guard absorbs the burst-publish risk, so this is not a
  correctness bug — just a behavioural deviation from the named schedule.
- Recommendation: either remove the immediate `job()` call (cron will pick up
  at 12:00 МСК next day) or document this in deployment.md as "first-cycle
  warm-up". Defer to operator decision.

### Low / nit

**L1. `transcreate_text` imports `re` inside the function body.** Severity:
low.
- Location: `news_bot.py:438` — `import re` inside the function.
- The module already imports `re` at the top (line 9). This is a leftover
  from before the module's regex consolidation.
- Recommendation: drop the local import. One-line cleanup.

**L2. `_parse_published_at_utc` truncates fractional seconds with `split('.', 1)[0]`.** Severity: low.
- Location: `news_bot.py:1067–1068`.
- For the SQLite default-CURRENT_TIMESTAMP path the column is integer-second
  resolution, so the branch is defensive only. If the helper ever receives an
  ISO string with `+00:00` suffix following a `.NNNNNN` fractional second
  (`'2026-04-27 13:00:00.123456+00:00'`) the timezone token would be lost
  along with the fractional part. Correctness preserved (function then sets
  `tzinfo=timezone.utc` on the parsed naive datetime), but it's not obvious
  the code is intentionally relying on SQLite's integer-second contract.
- Recommendation: add a one-line comment "SQLite CURRENT_TIMESTAMP is
  integer-second; we discard any fractional/TZ suffix and re-stamp UTC".

**L3. `_redact_text` regex uses `[A-Za-z0-9_=.-]{16,}` greedy.** Severity:
low (informational).
- Location: `news_bot.py:221`.
- A trailing `=` or `.` immediately before whitespace will be consumed into
  the `***`. Benign — the redaction is correct; just noting that the regex
  also captures one trailing punctuation char into the masked span on certain
  log shapes.
- Recommendation: none — regex is intentionally broad per Decision 12.

### Informational

**I1. Logging filter attaches to handlers iterated at module-import time.**
Severity: info.
- Location: `news_bot.py:300–301`. After-import handlers added by tests or
  third-party code won't get the filter automatically. Mitigation: tests use
  `caplog` which goes through the propagation path covered by the named-logger
  filters. No real exposure window today; operator should be aware if a
  future addition installs new root handlers post-import.

**I2. `health_check` does a real Anthropic API call on every startup.**
Severity: info.
- Location: `claude_transcreation.py:564–571` — 10-token probe on every
  `main()` invocation.
- Cost: negligible (~10 tokens per cron-process restart). On the typical
  daily-restart cadence this is well within the documented sanity threshold
  (deployment.md Cost Monitoring section).
- No action recommended.

**I3. `_fallback_publish` doc-comment header is ~50 lines; function doc-string
duplicates content.** Severity: info.
- Location: `news_bot.py:671–725` vs `:727–749`.
- Slight redundancy, but the header is the canonical Decision 1/5/9 contract
  reference; not a finding.

---

## 4. Acceptance-criteria mapping (sanity check)

Per task §Acceptance Criteria checklist:

- [x] All listed source files read end-to-end.
- [x] tech-spec / user-spec / decisions.md reviewed (all 13 task entries
  present in `decisions.md`; no missing reports).
- [x] 7 focus-areas each have an explicit PASS/FAIL/N/A with file:line cites.
- [x] grep on the 6 deleted symbols documented; one critical caveat (C1)
  surfaced.
- [x] 11 review dimensions applied; findings grouped by severity.
- [x] `logs/audit/code-audit.md` created with structured sections + Verdict.
- [x] No source/test files modified by this audit.

---

## 5. Test-suite status (per decisions.md last verification points)

* End of Task 13 (final imp task, Wave 8): `pytest tests/ -q` → **566 passed**
  (decisions.md line 328).
* No subsequent commits modified source or tests after `74fffdf`.
* The auditor did NOT re-run the suite (per task constraints — that's QA's
  job). However, finding **C1** means re-running pytest **as-is** in the
  current working tree may produce import errors / hangs because of the
  staged-but-not-committed legacy test files.

Recommended pre-deploy QA action: verify the working tree has **no
`tests/test_overflow.py` / `tests/test_idle_fallback.py`** on disk (they
should be neither tracked nor untracked) before running `pytest tests/ -q`.

---

## 6. Verdict

**FAIL** — single critical finding (C1) blocks deploy until resolved.

* Implementation quality: high. The 7 focus areas all PASS with tight,
  invariant-preserving code. No high-severity correctness issues. The 11
  review dimensions show no architectural concerns.
* Test coverage: comprehensive, 566 green tests at the end of Wave 8.
* The single blocker is a working-tree hygiene issue, not a code-quality
  issue: two legacy test files (deleted in commit `050b6eb`) are present on
  disk and `git add`-staged. They reference symbols that no longer exist on
  `news_bot` and will break test collection if committed.

**Operator action required (before deploy):**
1. `rm tests/test_overflow.py tests/test_idle_fallback.py` (files are
   currently untracked-but-present, so a plain `rm` removes them; no `git
   rm --cached` needed because nothing is staged).
2. Re-run `pytest tests/ -q` and confirm ≥ 566 passing.

After C1 is cleared the verdict becomes **PASS-WITH-FIXES** (M1, M2 carry
over as post-deploy follow-ups; L1/L2/L3 nit-level, no action required for
this deploy).
