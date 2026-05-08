---
created: 2026-05-07
status: approved
branch: dev
size: S
---

# Tech Spec: publish-idempotency-fix

## Solution

Two surgical edits to make the publish pipeline idempotent on `link`:

1. **Primary guard in `news_bot._fallback_publish` (line 985, immediately after `link = row['link']` at line 984).** Before any side effect (Telegraph create, Telegram send, repository writes), call `pending_articles_repo.get_published(link)`. If non-`None` → log INFO with `[idempotency-guard]` tag, send admin ping (best-effort, return-value checked), call `pending_articles_repo.skip_pending(link)` to clean the zombie row, return `True`. If `skip_pending` itself raises (DB-level error), log ERROR + send a second admin ping about cleanup failure + STILL return `True` (subscribers must not see a duplicate post; cleanup retry is left to the next slot's guard activation). The slot loop in `news_bot.job()` (line 1747) treats `True` as success — no `attempt_count` increment, no eventual `move_to_failed`.

2. **Defense-in-depth at `pending_articles_repo.move_to_published` (line 582).** Replace `INSERT INTO published_articles ...` with `INSERT OR IGNORE INTO published_articles ...`. The remaining steps (`INSERT OR IGNORE` into `processed_news`, `DELETE` from pending) execute unconditionally on the same transaction. Covers both the deployed cron-side `_fallback_publish` caller AND the live-in-code `hw_review.cmd_publish` caller (preserved per Decision 9 of the manual-review-workflow tech-spec; not in deploy FILES list, but tests stay green and operator may revive it ad-hoc).

Two tests verify the primary guard fires on the dominator path BEFORE all 4 branching points (Claude success at line 1052+, per-article fallback at 1065, ClaudeOutageError at 1079, `is_fallback_active()` shortcut at 1045). One repository test verifies `move_to_published` idempotency without value overwrite. One integration test runs `job()` end-to-end with a pre-staged zombie row + a fresh row, asserts that Telegram is called exactly once with the fresh link.

## Architecture

### What we're building/modifying

- **`news_bot._fallback_publish`** — add idempotency guard at function entry; preserves all existing side-effect order (Telegraph → Telegram → move) for the non-zombie path.
- **`pending_articles_repo.move_to_published`** — change `INSERT` to `INSERT OR IGNORE` on the `published_articles` step; behavior unchanged on first call.
- **`tests/test_fallback_publish_paths.py`** — 5 new tests covering guard activation across the dominator path + admin-ping failure handling + log-marker assertion.
- **`tests/test_pending_articles_repo.py`** — 1 new test verifying `move_to_published` idempotency.
- **`tests/test_distributed_schedule_integration.py`** — 1 new test exercising slot-loop with mixed zombie + fresh rows.

### How it works

```
news_bot.job()                           # daily 10:00 МСК cron tick
  └── slot loop (line 1733-1747)
       └── _fallback_publish(row)        # one slot, one row
             ├── link = row['link']      # line 984 (existing)
             ├── ★ GUARD (NEW, line 985–~1003)
             │     ├── if get_published(link) is not None:
             │     │     ├── logger.info("[idempotency-guard] {link} already published — skipping re-publish")
             │     │     ├── ping_ok = send_admin_notification("⚠️ Skipped re-publish of {link} — already in published_articles. Investigate stale pending row.")
             │     │     ├── if not ping_ok: logger.warning("admin ping for guard skip failed (Telegram down?) — continuing cleanup")
             │     │     ├── try:
             │     │     │     skip_pending(link)
             │     │     │  except Exception as cleanup_err:
             │     │     │     logger.error("[idempotency-guard] skip_pending failed for {link}: {cleanup_err} — leaving row in pending; next slot's guard will retry cleanup")
             │     │     │     send_admin_notification("⚠️ Idempotency-guard cleanup failed for {link}: {type}. Pending row will retry on next slot.")
             │     │     └── return True             # slot loop sees success → no attempt_count++, no move_to_failed
             │     └── (else: continue normal flow)
             ├── translation (Claude / per-article fallback / Google outage)
             │   ├── line 1045: if outage_state.is_fallback_active(): … (Google shortcut)
             │   ├── line 1052: claude_result = transcreate_via_claude(row)
             │   ├── line 1065: except ClaudeTranscreationError (per-article fallback)
             │   └── line 1079: except ClaudeOutageError (degraded mode)
             ├── Telegraph create / reuse (line 1180+)
             ├── Telegram teaser send (line 1237)
             └── move_to_published(link, ...) (line 1244)
                   ├── INSERT OR IGNORE INTO published_articles  # ★ CHANGED (line 582 of pending_articles_repo.py)
                   ├── INSERT OR IGNORE INTO processed_news      # already this pattern (line 593)
                   └── DELETE FROM pending_articles
                   (single transaction, commit at the end)
```

The guard sits at line 985 — BEFORE the `is_fallback_active()` shortcut at line 1045, BEFORE the Claude-vs-Google branching at 1052/1065/1079, BEFORE any DB write or network call. Single dominator position covers all 4 entry conditions described in user-spec AC2.

### Shared resources

None. Both edits are local function changes. No new global state, no new singletons.

## Decisions

### Decision 1: Guard location at line 985 of `news_bot.py`
**Decision:** Insert the idempotency guard at the very top of `_fallback_publish`, immediately after `link = row['link']` on line 984. Use `logger.info` (not WARNING) with `[idempotency-guard]` marker tag so AC10 is satisfied without elevating routine cleanup events to warnings.
**Rationale (AC1, AC2, AC10):** This position dominates all 4 entry conditions of `_fallback_publish` (Claude-OK at line 1052, per-article fallback at 1065, ClaudeOutageError at 1079, `is_fallback_active()` shortcut at 1045). Placing the guard later (e.g. inside the `else:` of `is_fallback_active`) would skip the outage path → still post duplicates during outage days. INFO level matches the existing pattern for routine flow-control logs in `_fallback_publish` (see `logger.info` at line 1046 for the outage shortcut and 1183 for Telegraph reuse) — guard activation is expected operational signal, not an error.
**Alternatives considered:**
- *Inside slot loop in `job()` (line 1733)*: rejected — `_fallback_publish` currently has one caller (the slot loop at `news_bot.py:1747`); guard at function entry is the architectural dominator that gets idempotency for free for any future caller without requiring updates to the call site. Slot-loop placement also pollutes `job()` with publish-flow concerns that belong inside `_fallback_publish`.
- *Between Telegraph and Telegram steps (post-line 1212)*: rejected — Telegraph would already have created a page (orphan if guard later fires). Function entry is the only "no-side-effect-yet" point.
- *WARNING log level*: rejected — existing pattern uses INFO for expected routing decisions; WARNING is reserved for genuine anomalies (e.g. crash-loop guard at line 1561). Operator's `journalctl | grep idempotency-guard` works at any level.

### Decision 2: Reuse `skip_pending(link)` for cleanup
**Decision:** Cleanup path of the guard calls `pending_articles_repo.skip_pending(link)` rather than direct `DELETE FROM pending_articles`.
**Rationale (AC3):** `skip_pending` already does exactly what we need in one transaction — `INSERT OR IGNORE INTO processed_news` + `DELETE FROM pending_articles` (verified at `pending_articles_repo.py:656-686`). Writing to `processed_news` is essential: without it, the next cron tick's fetch may re-stage the link (depends on whether RSS still serves it), creating a loop of guard activations + admin pings. Reusing `skip_pending` keeps the semantics aligned with the manual operator skip path.
**Alternatives considered:**
- *Direct `DELETE`*: rejected — would not write to `processed_news`; risk of re-stage loop.
- *New `clean_zombie(link)` helper*: rejected — `skip_pending` already encodes the right contract; semantic match is exact ("forget about this link").

### Decision 3: Admin ping mandatory, not optional
**Decision:** Every guard activation sends one admin ping via `send_admin_notification`. Not toggleable.
**Rationale (AC4 + Risk 2):** Root cause (how zombie rows appear) is OUT OF SCOPE for this fix. Without the ping, recurring zombies become invisible. The ping is the ONLY mechanism the operator has to notice a recurring root-cause issue. Pinging cost is one Telegram API call per K stale rows per tick; K is bounded in practice.
**Alternatives considered:**
- *Log-only*: rejected — operator does not grep journalctl daily; Telegram ping is the proven channel for actionable alerts (matches outage-state-machine pattern).
- *Throttled/batched ping*: rejected for this iteration — added complexity not justified at expected K=1 frequency. Re-evaluate if K>5/tick observed.

### Decision 4: Admin ping failure does not block cleanup; check return value, no try/except needed
**Decision:** Capture the return value of `send_admin_notification`. If `False`, log WARNING and continue with cleanup. Do NOT wrap the call in try/except: function never raises (verified at `news_bot.py:357-392` — `TelegramError` caught internally, returns `True/False`).
**Rationale (AC5):** Subscribers (no duplicate post) take priority over operator (delayed alert). Cleanup MUST run so the same zombie row doesn't re-fire on the next slot. Try/except would be dead defensive code.
**Alternatives considered:**
- *try/except around `send_admin_notification`*: rejected — function never raises; defensive try/except is dead code that misleads future readers.
- *Block cleanup until ping succeeds (retry loop)*: rejected — could starve the slot loop on a Telegram outage.
- *Use `logger.error` instead of WARNING*: rejected — admin-ping failure is degraded-mode alert (we kept the channel safe), not a publish error.

### Decision 5: `INSERT OR IGNORE` on `move_to_published` — defense-in-depth across all callers
**Decision:** Change `INSERT INTO published_articles` (line 582) to `INSERT OR IGNORE INTO published_articles`. Steps 2 (`processed_news`) and 3 (`DELETE pending`) execute unconditionally on the same transaction.
**Rationale (AC6 + AC7):** Even with the primary guard at line 985, defense-in-depth covers TWO real callers: (a) `_fallback_publish` (the cron path, where the guard sits); (b) `hw_review.cmd_publish` (live in code at `hw_review.py:569`, dispatched at `hw_review.py:815`; not in deploy FILES list, but tests stay green and operator may revive ad-hoc per `architecture.md`). The change preserves the original published-row values (`INSERT OR IGNORE`, NOT `INSERT OR REPLACE`) — first publish wins, retry is a silent no-op on the published row, but still cleans pending.
**Alternatives considered:**
- *Catch `IntegrityError` in Python*: rejected — `INSERT OR IGNORE` is the SQLite-native idempotency pattern, already used at line 593 for `processed_news`. Consistency with existing pattern.
- *Skip Decision 5 entirely (rely on guard alone)*: rejected — `hw_review.cmd_publish` does NOT pass through `_fallback_publish`'s guard (it has its own publish path); without `INSERT OR IGNORE` here, an operator-driven retry could still hit `IntegrityError`.

### Decision 6: No migration code for one-time `failed_articles` cleanup
**Decision:** Operator runs a one-time SQL `DELETE FROM failed_articles WHERE link='https://orangetrackdiecast.com/2026/05/02/...team-transport-k...'` after prod deploy. No code-level migration in `init_schema`.
**Rationale (user-spec Ограничения "Без миграции в коде"):** Migration code lives forever in `init_schema` and runs on every cron tick. The actual cleanup is one row, one time. SQL on the server is cheaper. Operator action is documented in user-spec "Как проверить" → "Агент проверяет" step 6.
**Alternatives considered:**
- *Idempotent migration in `init_schema`*: rejected — overengineering for one row.
- *Leave the row*: rejected — operator memory is finite; ghost row in `failed_articles` is misleading for future debugging.

### Decision 7: [TECHNICAL] Skip `pre-publish` check at slot-loop level
**Decision:** Do NOT add a `get_published()` check inside `news_bot.job()` slot loop before calling `_fallback_publish`. The guard inside `_fallback_publish` is the single point of truth.
**Rationale [TECHNICAL]:** The slot loop calls `_fallback_publish(row)` once per slot; pulling `get_published` up to the slot level adds a redundant DB round-trip per slot (7/day) without semantic benefit. The function-level guard is the architectural dominator — any caller of `_fallback_publish` (slot loop today, operator-CLI tomorrow) gets idempotency for free.
**Alternatives considered:**
- *Slot-loop-level guard*: rejected — duplicates the check at two layers, requires updates in two places when callers grow, and pays the DB round-trip on every slot regardless of whether the row is fresh or zombie.
- *Repository-level guard inside `pending_articles_repo.list_pending()`*: rejected — repository should not encode publication idempotency; that belongs to publish-flow logic. Mixing layers reduces testability.

### Decision 8: skip_pending failure does not strike the slot
**Decision:** If `skip_pending` raises during guard cleanup, the guard logs ERROR + sends a second admin ping about the cleanup failure + STILL returns `True`. AC6 (guard slot is not a publish failure) takes priority over the strike-counting machinery. The pending row remains in place; next slot's guard activation will re-attempt cleanup.
**Rationale (resolves AC6 vs. AC8 tension):** AC6 says guard activation must NOT increment `attempt_count`. AC8 says other DB errors propagate as publish failures. These collide on `skip_pending` failure unless the guard explicitly handles it. The guiding principle: subscriber-visible duplicate prevention is the primary contract; cleanup failure is degraded mode the operator must investigate (admin ping fires regardless), not a publish failure (no Telegram side-effect happened in this slot at all).
**Alternatives considered:**
- *Let `skip_pending` raise propagate to slot loop*: rejected — slot loop catches, increments `attempt_count`. After 3 slots the row goes to `failed_articles`. Violates AC6.
- *Bypass `skip_pending` and DELETE pending directly*: rejected — would skip processed_news write, risk re-stage loop on next cron tick fetch (Decision 2 reasoning).

## Data Models

No schema changes. Existing tables used:
- `pending_articles` — read indirectly via `skip_pending`'s internal DELETE.
- `published_articles` — read by `get_published(link)`.
- `processed_news` — written by `skip_pending` (`INSERT OR IGNORE`).

No new types, no new interfaces. Tests use existing repo helpers + raw SQL `INSERT INTO published_articles (...)` to pre-stage published rows — pattern verified at `tests/test_hw_review_publish_flow.py:271`.

## Dependencies

### New packages
None.

### Using existing (from project)
- `pending_articles_repo.get_published(link)` — read of `published_articles` row by link, returns dict or None (`pending_articles_repo.py:372`).
- `pending_articles_repo.skip_pending(link)` — atomic write to `processed_news` + `DELETE FROM pending_articles` (`pending_articles_repo.py:656`).
- `news_bot.send_admin_notification(message)` — Telegram admin ping with secret redaction + `INSTANCE_LABEL` prefix; returns `True`/`False`, never raises (`news_bot.py:357`).

## Testing Strategy

**Feature size:** S

### Unit tests

5 new tests in `tests/test_fallback_publish_paths.py` (extends existing `_FallbackPublishPathsCase` base class at line 90):

- **T1 `test_skip_if_link_already_published_claude_path`** — pre-stage `published_articles` row + zombie row in `pending_articles`; default Claude path mock. Call `_fallback_publish(row)`. Assert (negative side-effects): `send_telegraph_teaser` not called, `publish_article` (Telegraph create) not called, `mark_telegraph_published` not called, `update_staged` not called, `move_to_published` not called, `transcreate_via_claude` not called (positive proof guard fires BEFORE LLM, covering all 4 line-1052/1065/1079 paths via dominator-position semantics). Assert (cleanup): pending row deleted; `link` present in `processed_news`. Assert (return): returns `True`. Assert (log marker AC10): `assertLogs` captures one INFO-level entry containing `[idempotency-guard]` and the `link`.
- **T2 `test_skip_if_link_already_published_outage_shortcut_path`** — same setup, but mock `outage_state.is_fallback_active()` to return `True` (covers the line-1045 shortcut path that bypasses the Claude-Try-Block). Same assertions. Critical: catches regression where guard is placed AFTER the `is_fallback_active` shortcut at line 1045.
- **T3 `test_skip_if_link_already_published_no_telegraph_url`** — same setup, but zombie pending row's `telegraph_url=NULL`. Assert (specifically): `publish_article` (Telegraph create) NOT called — proves guard fires BEFORE the Telegraph-create branch, not just BEFORE Telegraph-reuse. Other assertions same as T1.
- **T4 `test_admin_ping_fires_when_guard_skips`** — Claude-path setup. Assert: `send_admin_notification` called exactly once with text containing `"⚠️ Skipped re-publish of "` and the literal `link`.
- **T5 `test_guard_continues_when_admin_ping_returns_false`** — Claude-path setup; mock `send_admin_notification` to return `False` (matches actual function semantics — never raises). Assert: `skip_pending` still called, returns `True`, WARNING log emitted ("admin ping for guard skip failed").

1 new test in `tests/test_pending_articles_repo.py` (extends existing `_TmpDbCase` pattern at line 99):

- **T6 `test_move_to_published_idempotent_on_duplicate_link`** — insert pending row, call `move_to_published(link, url1, path1, via_review=False)`. Insert another pending row with same link (raw SQL — Pending PK was deleted on first move). Call `move_to_published(link, url2, path2, via_review=True)` again. Assert: NO `IntegrityError` raised; `published_articles` has 1 row with ORIGINAL `url1`/`path1`/`via_review=0` (`INSERT OR IGNORE` preserves first write); pending_articles empty.

### Integration tests

1 new test in `tests/test_distributed_schedule_integration.py` (extends existing `TestDistributedSchedule` class at line 128):

- **T7 `test_slot_loop_does_not_repost_already_published`** — pre-stage:
  - 1 row in `published_articles` for `link_zombie`.
  - 1 zombie row in `pending_articles` for `link_zombie` (with cached `telegraph_url`, `fetched_at` 2 days ago — carry-over tier, **`attempt_count=2`** so the AC6 check below is litmus-grade: without the guard, the next slot's UNIQUE-failure would be strike 3 and push the row to `failed_articles`).
  - 1 fresh row in `pending_articles` for `link_fresh` (`fetched_at` today — fresh tier).

  Run `news_bot.job()` end-to-end with mocked Telegram bot, mocked Anthropic SDK, mocked Telegraph. Assert:
  - `send_telegraph_teaser` called EXACTLY ONCE with `link=link_fresh` (litmus check — wrong link would silently pass a count-only assertion).
  - `published_articles` final state: 2 rows total (1 pre-existing for `link_zombie` + 1 fresh for `link_fresh`).
  - `failed_articles`: 0 rows. `link_zombie` did NOT get marched into failed (covers AC6 — slot was not a publish failure).
  - `send_admin_notification` received guard-ping for `link_zombie`.
  - `processed_news`: contains both `link_zombie` (via guard's `skip_pending`) and `link_fresh` (via `move_to_published`).
  - **AC6 explicit check (litmus):** zombie pending row had `attempt_count=2` before job(). With the guard active, the row is gone (skipped via `skip_pending`), `failed_articles=0`. WITHOUT the guard, the slot would have raised UNIQUE → strike 3 → `move_to_failed` → `failed_articles=1`. The empty `failed_articles` is therefore proof that the guard intercepted before the strike machinery ran.

  **Out-of-scope acknowledgement (Сценарий D — recurring stale row):** T7 is single-tick. The recurring case (zombie reappears next tick) is not a separate test — it would be the same code path activated K times, no new logic surface. Documented here so reviewers don't expect a multi-tick test.

### E2E tests

None — pytest-only project; no Selenium/Playwright/etc. The integration test above with mocked external services is the deepest layer feasible.

### Regression coverage (existing tests, MUST stay green)

- `tests/test_hw_review_publish_flow.py::TestPublishRetryIdempotency::test_publish_retry_reuses_telegraph_url` (line 158) — Decision 9 retry path through `hw_review`; not via `_fallback_publish`, but exercises `move_to_published` with cached telegraph_url and verifies the URL is reused. With `INSERT OR IGNORE` change in Decision 5, must still pass.
- `tests/test_hw_review_publish_flow.py::test_publish_retry_after_teaser_exception` (line 205) — additional retry idempotency path.
- `tests/test_pending_articles_repo.py::test_move_to_published_rollback_on_error` (line 623) — sqlite OperationalError still rolls back. Confirms AC8: other DB errors propagate.
- All 822 tests run in CI; pytest must report 0 failures.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Per-task smoke checks: each implementation task has a Verify-smoke field below.

End-to-end pre-deploy check: `pytest tests/ -q` must pass with all 822 existing tests + 7 new ones (829+ total) reported.

Post-deploy verification (live prod): operator runs three diagnostic commands documented in the Post-deploy task description below — checks for `UNIQUE constraint failed` absence in journalctl, confirms `failed_articles` cleanup succeeded, verifies the next 10:00 МСК cron tick produced no `[idempotency-guard]` log lines (= DB consistent post-fix).

### Tools required

`pytest` (local + CI), `bash`/`ssh` (operator-side post-deploy), `sqlite3` (operator-side cleanup + post-deploy spot checks), `journalctl` on the VPS (post-deploy log inspection). No MCP tools — channel verification is visual user check.

## Risks

| Risk | Mitigation |
|------|-----------|
| Guard fires for legitimate operator-driven re-publish (e.g. code rollback, manual re-stage) | Admin ping notifies operator; rare case; documented in user-spec Risk 1 |
| `_fallback_publish` is called by `hw_review.cmd_publish` (live in code, not deployed) | hw_review tests exercise their own publish path (via `cmd_publish` direct call) without going through `_fallback_publish`; the guard adds no behavior change for those tests because they don't pre-stage the SAME link in `published_articles`. Also: `hw_review.cmd_publish` calls `move_to_published` directly — Decision 5's `INSERT OR IGNORE` covers any future operator retry. |
| `INSERT OR IGNORE` swallows a real bug (concurrent writers writing same link with different metadata) | Production has single writer (cron `news_bot.service`); race architecturally impossible per code-research §E. Defense-in-depth only. |
| Recurring zombie rows = recurring admin pings → operator desensitizes | Documented in user-spec Risk 5; operator opens diagnostic task after first pings; rate is bounded by cron-tick frequency (7 slots/day max). |
| Test T7 passes vacuously (Telegram count check matches even on wrong link) | Litmus assertion in T7: `mock_teaser.call_args` must match `link_fresh`, not just count. Documented in T7 description. |
| `skip_pending` itself raises during guard cleanup | Decision 8: log ERROR + admin ping + return True. Subscribers safe; next slot's guard activation retries cleanup. |

## User-Spec Deviations

- **AC6 vs AC8 collision on `skip_pending` failure** — user-spec has an internal conflict the tech-spec must resolve:
  - **AC6 says:** «Слот, в котором сработал guard, не считается ошибкой публикации: счётчик попыток (`attempt_count`) у строки не увеличивается».
  - **AC8 says:** «Прочие ошибки БД при любых записях по ходу публикации... — будь то фиксация в опубликованных, **cleanup зомби-строки после admin-ping'а** или иные репозиторные операции — пропагируются и обрабатываются slot loop'ом как обычная publish failure (с инкрементом `attempt_count` и возможным переездом в `failed_articles` после трёх таких)».
  - **Tech-spec choice (Decision 8):** AC6 wins for the guard-cleanup path. If `skip_pending` raises during guard activation, the guard logs ERROR + sends a second admin ping + returns `True` (no strike, no `move_to_failed`).
  - **Why:** Subscriber-visible duplicate prevention is the primary contract. The slot did NOT publish anything (no Telegram side-effect), so calling it a "publish failure" semantically misrepresents what happened. Operator gets the cleanup-failure ping → can investigate. Next slot's guard activation will retry cleanup. Letting `attempt_count` run up to 3 on cleanup-only errors would eventually push the row to `failed_articles` (semantically wrong: the article IS published) AND potentially eat 3 admin pings of guard-activation noise before `move_to_failed` finally happens.
  - **Operator approval:** Approved 2026-05-07 (operator chose AC6-wins semantics over literal AC8 interpretation). → **[APPROVED]**

All other tech-spec decisions trace to user-spec ACs:
- Decision 1 → AC1, AC2, AC10 (guard location ensures all 4 paths covered + log marker)
- Decision 2 → AC3 (skip_pending semantics)
- Decision 3 → AC4 (admin-ping mandatory)
- Decision 4 → AC5 (ping failure does not block; check return value, no try/except)
- Decision 5 → AC6, AC7 (idempotent move + value preservation + other errors propagate)
- Decision 6 → user-spec Ограничения ("Без миграции в коде")
- Decision 7 → [TECHNICAL] (architectural choice, no user-spec requirement; preserves AC2 contract from a single chokepoint)
- Decision 8 → resolves AC6 vs. AC8 tension on `skip_pending` cleanup failure (degraded-mode handling, no strike)

## Acceptance Criteria

Technical criteria complementing user-spec AC1–AC10:

- [ ] `pytest tests/ -q` passes with 0 failures and 0 errors. Existing test count + 7 new = 829+ tests reported.
- [ ] `git diff` of source files shows changes only in `news_bot.py` (one block ~20 lines added at line 985) and `pending_articles_repo.py` (one keyword change on line 582).
- [ ] Static check: `python -m py_compile news_bot.py pending_articles_repo.py` succeeds (no syntax errors).
- [ ] Manual review of guard block: matches Decision 1 + Decision 8 contracts (call `get_published`, branch on non-None, log INFO with `[idempotency-guard]` tag, send admin ping checking return value, log WARNING on ping False, try/except around `skip_pending` with ERROR log + admin ping on cleanup failure, always return True).
- [ ] Manual review of `move_to_published` change: only `INSERT INTO` → `INSERT OR IGNORE INTO` on line 582; `processed_news` and `DELETE` lines unchanged.
- [ ] No regression in `test_publish_retry_reuses_telegraph_url`, `test_publish_retry_after_teaser_exception`, `test_move_to_published_rollback_on_error` (these assert pre-existing contracts).
- [ ] `git diff` shows new tests in 3 test files: 5 in test_fallback_publish_paths.py, 1 in test_pending_articles_repo.py, 1 in test_distributed_schedule_integration.py.
- [ ] CI on `dev` branch: green pytest → `deploy_test.yml` triggers automatically → SCP to `/home/hwbot/bot_test/` succeeds → `news_bot_test.service` restarts cleanly.
- [ ] Post-deploy on prod: `journalctl -u news_bot.service --since '<deploy time>'` shows no `UNIQUE constraint failed` errors. Operator-run `DELETE FROM failed_articles WHERE link='...'` succeeds.

## Implementation Tasks

### Wave 1 (parallel — source edits)

#### Task 1: Add idempotency guard to `_fallback_publish`
- **Description:** Insert idempotency guard at `_fallback_publish` function entry implementing Decisions 1, 2, 3, 4, 8. On hit (link already in published), log INFO with `[idempotency-guard]` tag, send admin ping (best-effort, return-value checked, WARNING on False), call `skip_pending` wrapped in try/except (on cleanup failure: ERROR log + second admin ping + still return True), return True. On miss, fall through to existing flow unchanged.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -m py_compile news_bot.py` succeeds; `python -c "from news_bot import _fallback_publish; print(_fallback_publish.__doc__[:100])"` runs without ImportError.
- **Files to modify:** `news_bot.py`
- **Files to read:** `pending_articles_repo.py` (for `get_published` and `skip_pending` signatures), `work/publish-idempotency-fix/code-research.md` (§A, §H.1, §H.5, §H.6)

#### Task 2: Make `move_to_published` idempotent on duplicate link
- **Description:** Single keyword change on the published-articles INSERT inside `move_to_published`, implementing Decision 5. Steps 2–3 of the transaction (`INSERT OR IGNORE` into `processed_news`, `DELETE FROM pending_articles`) unchanged.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -m py_compile pending_articles_repo.py` succeeds; `grep -n "INSERT OR IGNORE INTO published_articles" pending_articles_repo.py` returns one match.
- **Files to modify:** `pending_articles_repo.py`
- **Files to read:** `work/publish-idempotency-fix/code-research.md` (§B, §H.2)

### Wave 2 (parallel — tests; depends on Wave 1)

#### Task 3: Add 5 unit tests for the `_fallback_publish` guard
- **Description:** Add T1–T5 to `tests/test_fallback_publish_paths.py` covering Claude-path + outage-shortcut-path + no-telegraph_url variant + admin-ping-fires + admin-ping-returns-False. Each test pre-stages `published_articles` via raw SQL, calls `_fallback_publish` once, asserts no Telegraph/Telegram side effects + correct cleanup. T1 includes `assertLogs` for the `[idempotency-guard]` marker (covers AC10).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_fallback_publish_paths.py -q -k "skip_if_link_already_published or admin_ping"` reports 5 new tests passed.
- **Files to modify:** `tests/test_fallback_publish_paths.py`
- **Files to read:** `news_bot.py` (post-Task-1), `tests/test_hw_review_publish_flow.py` (line 271 pre-stage pattern), `work/publish-idempotency-fix/code-research.md` (§D, §H.7)

#### Task 4: Add 1 repository test for `move_to_published` idempotency
- **Description:** Add T6 to `tests/test_pending_articles_repo.py`. Two consecutive `move_to_published` calls with same `link` and different `(url, path, via_review)`; second call must not raise; published row must hold values from FIRST call (`INSERT OR IGNORE` preserves original); pending_articles empty.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_pending_articles_repo.py -q -k "test_move_to_published_idempotent_on_duplicate_link"` reports 1 new test passed.
- **Files to modify:** `tests/test_pending_articles_repo.py`
- **Files to read:** `pending_articles_repo.py` (post-Task-2), `work/publish-idempotency-fix/code-research.md` (§D, §H.7)

#### Task 5: Add 1 integration test for slot-loop with mixed zombie + fresh rows
- **Description:** Add T7 to `tests/test_distributed_schedule_integration.py` (extends `TestDistributedSchedule` class). Pre-stage 1 published row + 1 zombie pending (carry-over tier) + 1 fresh pending (fresh tier). Run `job()`. Assert: Telegram called exactly once with the FRESH link (litmus check); failed_articles empty; admin ping for zombie link; processed_news contains both links; AC6 explicit (zombie row never reached failed_articles).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_distributed_schedule_integration.py -q -k "test_slot_loop_does_not_repost_already_published"` reports 1 new test passed.
- **Files to modify:** `tests/test_distributed_schedule_integration.py`
- **Files to read:** `news_bot.py` (post-Task-1), `pending_articles_repo.py` (post-Task-2), `work/publish-idempotency-fix/code-research.md` (§D, §H.7)

### Audit Wave

#### Task 6: Code Audit
- **Description:** Full-feature code quality audit. Read `news_bot.py` (guard block) and `pending_articles_repo.py` (INSERT OR IGNORE change). Verify holistic quality: guard placement matches Decision 1, log/ping format consistent with project patterns, no duplicate logic, no shared-resource issues. Cross-check guard against Decisions 1–4, 8. Write audit report to `logs/working/wave-3-code-audit.md`.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit
- **Description:** Full-feature security audit (OWASP Top 10) on the guard block and INSERT OR IGNORE change. Specifically: the admin-ping payload contains a URL — confirm `_redact_text` handles it cleanly (no leak of unrelated secrets), the guard does NOT take untrusted input from outside the row dict, the `INSERT OR IGNORE` preserves parameterization (no SQL injection introduced). Write audit report to `logs/working/wave-3-security-audit.md`.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit
- **Description:** Full-feature test quality audit. Read all 7 new tests across 3 test files. Verify: T7's litmus assertion (call_args matches FRESH link, not just count); T2 actually mocks the outage path correctly (regression-catch surface); T6 asserts ORIGINAL values preserved (catches accidental INSERT OR REPLACE); T5 mocks return-False, not raise (matches actual `send_admin_notification` behavior); T1 includes log-marker assertion (AC10). Write audit report to `logs/working/wave-3-test-audit.md`.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 9: Pre-deploy QA
- **Description:** Run full test suite locally with `pytest tests/ -q`; confirm 0 failures, 0 errors, and 7 new tests reported as passed (829+ total). Verify each user-spec AC1–AC10 has a corresponding passing test or executable check. Run `python -m py_compile news_bot.py pending_articles_repo.py`. Report findings; on green, hand off to Deploy.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 10: Deploy
- **Description:** Standard two-stage deploy. (1) Operator: `git push origin dev` → CI ci.yml pytest → deploy_test.yml SCPs to `/home/hwbot/bot_test/` and restarts `news_bot_test.service`. Operator observes `@myhwchannel123` test channel for ~30 min for regressions and optionally pre-stages a synthetic zombie row to verify guard activation in test instance. (2) `git checkout main && git merge dev && git push origin main` → deploy.yml SCPs to `/home/hwbot/bot/` and restarts `news_bot.service`. (3) Operator runs one-time SQL: `ssh hwbot@148.135.207.54 "sqlite3 /home/hwbot/bot/news.db \"DELETE FROM failed_articles WHERE link='https://orangetrackdiecast.com/2026/05/02/hot-wheels-2026-car-culture-team-transport-k-case-report/';\""` (per Decision 6).
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 11: Post-deploy verification
- **Description:** Live environment verification on prod after deploy:
  - **Step 1: confirm no UNIQUE crash** — `ssh hwbot@148.135.207.54 "journalctl -u news_bot.service --since '<deploy timestamp>' --no-pager | grep -E 'UNIQUE constraint failed|IntegrityError'"` → expected: no output.
  - **Step 2: confirm cleanup applied** — `ssh hwbot@148.135.207.54 "sqlite3 /home/hwbot/bot/news.db \"SELECT COUNT(*) FROM failed_articles WHERE link LIKE '%team-transport-k%';\""` → expected: `0`.
  - **Step 3: observe next 10:00 МСК cron tick** — wait until next morning, then `ssh hwbot@148.135.207.54 "journalctl -u news_bot.service --since today --no-pager | grep -E 'idempotency-guard|UNIQUE'"` → expected: no `UNIQUE`, possibly some `idempotency-guard` lines if zombie rows exist (in which case guard handled them correctly without duplicate post — verify by Step 4).
  - **Step 4: visual channel check** — operator opens `@myhwchannel123` and scans the next 1–2 days of posts for duplicate Telegraph URLs. Visual confirmation that subscribers see no duplicates.

  Tools: `bash`, `ssh`, `sqlite3`, `journalctl`. No MCP tools needed.
- **Skill:** post-deploy-qa
- **Reviewers:** none
