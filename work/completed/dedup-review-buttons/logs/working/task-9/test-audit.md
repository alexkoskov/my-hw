# Test Audit — dedup-review-buttons (Task 9)

**Auditor:** test-auditor · **Date:** 2026-07-24 · **Methodology:** test-master / test-quality-review.md
**Scope:** final test state after waves 1–3 (Tasks 1–5) + ad-hoc flaky fix c8519da. Analysis only; no code changed (one temporary mutation spot-check on `news_bot.py`, restored via `git checkout`, working tree verified clean).

## Verdict

**GO — тесты фичи готовы к Pre-deploy QA.**

Per the decision matrix (test-quality-review.md): **0 critical, 0 high, 2 medium, 4 low** → `passed`.
Both medium findings are cross-component *seam* gaps (each ≈5 LoC to close), not weaknesses in any existing test; every existing test audited has meaningful assertions, and the per-task review rounds already mutation-hardened the weak spots. Recommend folding M-1/M-2 into the Task 10 pre-deploy round as optional cheap adds — neither blocks QA.

## Suite results (recorded, not assumed)

| Run | Result |
|---|---|
| `python3 -m pytest tests/ -q` (full) | **1324 passed, 0 failed, 0 skipped** in 53.3 s |
| Targeted: `test_pending_articles_repo.py test_admin_alerts.py test_admin_ping.py test_integration.py -q` | 220 passed in 15.6 s |
| Mutation spot-check (M-2, see below), full suite | 1324 passed — gap confirmed |

**Count trajectory:** 1284 (pre-feature) → 1324 = **+40**, matching decisions.md exactly: Task 1 +10 (`TestReviewTokenStore` 6, `TestConnectBusyTimeout` 2, `TestConcurrentWriters` 2), Task 2 +7 (`TestDedupReviewKeyboard` 4, `TestReplyMarkupForwarding` 3), Task 3 +3 (`TestDedupReviewButtons`), Task 4 +10 (`TestResolveDedupCallback`, incl. 2 polish), Task 5 +10 (`TestReviewListener`). Proportionate for an M-size feature introducing a 6-branch decision core, the bot's first inbound Telegram path, and a second DB writer — no bloat, no padding tests found.

**File list vs spec:** matches tech-spec «Files to modify» for Tasks 1–5 exactly; no unexpected new test files. One deviation, already recorded in decisions.md: the flaky-fix landed in `tests/test_job_distributed_publish.py` (actual location of `TestSlotLoopTransientRetry`), not `tests/test_integration.py` as originally assigned.

---

## Findings

### MEDIUM

**M-1 — no builder→parser round-trip test for the `dd:<c|k>:<token>` grammar (cross-task seam Task 2 ↔ Task 5)**
- **Evidence:** `grep -rn _parse_review_callback_data tests/` → zero hits (the parser is exercised only indirectly through `_handle_review_update`); `build_dedup_review_keyboard` appears in tests only in `tests/test_admin_alerts.py`. The grammar lives as literals in two production modules (`admin_alerts.py` builder; `news_bot.py:701` parser) pinned by literals in two separate test files (`test_admin_alerts.py:517`, `test_integration.py:3436`).
- **Issue:** no single test proves the builder's actual output is accepted by the parser. A tandem edit of the builder + its own unit test (e.g. grammar rename) would ship a mismatch whose failure mode is **silent by design** — `_parse_review_callback_data` rejects unknown grammar and the listener just advances the offset: buttons render, presses do nothing, no error anywhere.
- **Tech-spec anchor:** AC-2 («exactly two buttons with `callback_data dd:c:<token>` / `dd:k:<token>`») — each end is tested, the *compatibility* of the ends is not.
- **Fix:** one ~4-line test (in `TestReviewListener` or `TestDedupReviewKeyboard`): build a real keyboard, feed each button's `callback_data` to `news_bot._parse_review_callback_data`, assert `('cancel', token)` / `('keep', token)`.

**M-2 — `main()` activation wiring unpinned: deleting the `_maybe_start_review_listener()` call leaves the whole suite green (mutation-verified)**
- **Evidence:** replaced `news_bot.py:3302` `_maybe_start_review_listener()` with `pass` → full suite **1324/1324 passed**; restored (`git checkout -- news_bot.py`, line verified back). All three gate tests (`test_integration.py:3255/3275/3288`) call `_maybe_start_review_listener()` directly; `TestMainHealthChecks` runs `main()` but asserts nothing about the listener.
- **Issue:** the feature's *only* production activation path is uncovered — a refactor dropping the call would ship a fully dead feature (buttons render flag-on, nothing ever resolves), silently.
- **Tech-spec anchor:** AC-7 («Listener starts only when flag on AND admin numeric…») — the fail-closed halves and the positive start are covered at the function seam, not from `main()`.
- **Mitigation (why medium, not high):** the deployment.md rollout runbook (Task 6) makes the operator verify «review listener active» in `docker logs` on first enable (Task 12), so the silent-death mode is caught at rollout, and the gate function itself is thoroughly tested.
- **Fix:** file precedent already exists in `tests/test_job_distributed_publish.py` — either patch `news_bot._maybe_start_review_listener` inside `TestMainHealthChecks._run_main_once` and `assert_called_once`, or an `inspect.getsource(news_bot.main)` assertion à la `test_main_registers_tz_aware_daily_cron`.

### LOW

**L-1 — cancel→slot test re-authors the slot loop inline** (`test_integration.py:2824-2828`, `test_cancel_then_slot_publish_does_not_publish`). The test replays `rows = list_pending(); rows[0] → _fallback_publish` itself rather than driving the production loop. Already adjudicated *informational* by the task-4 test-reviewer; compositional coverage closes it indirectly (`skip_pending` removes the row from `list_pending` — repo tests; `job()` publishes only the head of `list_pending` — `TestDistributedPublishLoop` drives the real loop). No action required; noting so Pre-deploy QA doesn't re-litigate it.

**L-2 — default-OFF contract pinned via ambient environment** (`test_integration.py:3137`, `assertNotIn('REVIEW_BUTTONS_ENABLED', os.environ)`). Couples the test to the developer's shell (an exported var → spurious failure in an otherwise-correct build) and is a weak proxy: it checks the env at *test* time while the constant is computed at *import* time. Related, previously recorded as informational (task-3 review): all flag tests monkeypatch the constant, so the `os.getenv` on-word parsing expression itself (`news_bot.py:147`) is never exercised against a set env value — accepted, matches the `DEDUP_SERIES_ENABLED` convention. Optional fix: drop the env assertion or move the parsing expression behind a testable helper.

**L-3 — residual GIL handoff window in the two-writer rendezvous** (`test_pending_articles_repo.py:1416-1419`, `SignallingConn`): `write_reached.set()` and the blocking `execute()` are two statements, so a freak schedule could commit before the writer blocks — silently skipping real contention on that run (never a false failure). Already recorded informational in task-1 round-2 review; 15x stability confirmed there. No action.

**L-4 — mid-handler partial-failure state untested**: if `_review_edit_message` raises *after* `resolve_dedup_callback` consumed the token and skipped the row (deleted alert message, network blip), the per-update guard logs+skips — cancel took effect but no answer and no operator-decision INFO log is emitted. Loop survival for a raising handler IS covered (`test_review_listener_handler_error_advances_offset_and_survives`); the state contract of the partial application isn't. Accepted resilience-by-log design (tech-spec Risks row); cheap to pin if ever needed. Ties to AC-9 (decision log) only in this degraded corner.

---

## Coverage matrix (verified against code, not test names)

**`resolve_dedup_callback` — every branch mapped** (all in `tests/test_integration.py::TestResolveDedupCallback`, real tempfile SQLite):

| Branch (news_bot.py:582-664) | Test | State assertions beyond text? |
|---|---|---|
| non-admin → `(None, "")` | `test_non_admin_press_ignored_no_state_change` | yes: `skip_pending` not called, row pending, token alive |
| non-numeric admin fail-closed | `test_non_numeric_admin_id_fail_closed` | yes: row pending, not in processed_news, token alive |
| stale token → «⚠️ Кнопка устарела» | `test_stale_token_returns_expired` | yes: row untouched |
| keep → «👍 Оставлено» | `test_keep_returns_kept_no_state_change` | yes: row pending, token consumed |
| cancel-pending → skip + «✅ Отменено оператором» | `test_cancel_pending_row_skips_and_returns_cancelled` | yes: gone from pending, in processed_news, NOT in published; token consumed; second press → stale (idempotence) |
| cancel-published → «⚠️ Уже опубликовано…» | `test_cancel_published_returns_already_published` | yes: `skip_pending` not called, published row intact, token consumed |
| cancel-race (published between check and skip) | `test_cancel_race_published_between_check_and_skip` | yes: honest status, published row intact |
| cancel-missing → «⚠️ Статья уже недоступна» | `test_cancel_missing_returns_unavailable` | token consumed |
| unknown action (defensive) | `test_unknown_action_safe_fallback_token_not_consumed` | yes: 5 shapes, token NOT consumed, zero state change |
| token deleted only on terminal outcomes | asserted inside each branch test above | — |

**Integration scenarios (tech-spec Testing Strategy):** cancel→not-published-in-slot ✔ (`test_cancel_then_slot_publish_does_not_publish`, survivor-link positive control — see L-1 caveat); post-publish cancel with intact published row ✔; both directions of the slot-boundary race ✔.

**Concurrency (`TestConcurrentWriters`):** genuine contention, not sequential writes — `BEGIN IMMEDIATE` holds RESERVED on the main thread; a `SignallingConn` wrapper fires `write_reached` immediately before the cancel-writer's blocking `INSERT INTO processed_news`; the lock is released only after the event fires, so the writer provably waits out a held lock (no sleeps; Event with 10 s failure-guard timeouts only). Negative control `test_zero_timeout_control_raises_database_locked` demonstrates the exact failure mode being prevented. Busy-timeout contract double-pinned: behavior (`PRAGMA busy_timeout`=5000) + code line (connect call-spy `timeout=5.0` — necessary because the stdlib default masks a revert).

**Gate / fail-closed:** flag off → no thread, silent ✔; flag on + non-numeric admin → no thread, WARNING + explanatory ping ✔; gate open → daemon thread, «review listener active» log + ping ✔ (thread kwargs incl. `daemon=True` asserted). Listener-side: non-admin press → empty answer, **zero DB reads** (SEC-T5-1 spy), no edit ✔. See M-2 for the one unpinned wiring line.

**Alert-path regression:** `send_admin_notification` without `reply_markup` → text unchanged, `reply_markup=None` reaches `send_message` ✔; keyword-only contract via `pytest.raises(TypeError)` protects all existing positional callers ✔; forwarding preserves object identity (`is` sentinel) ✔. «No other alert carries buttons» — real negative test `test_only_e014_carries_keyboard` with an E015 actually firing in the same run (vacuity-guarded) ✔. Flag-off run asserts zero `review_token:*` rows in `bot_state` (byte-identical pre-feature behavior) ✔. Full pre-existing alert suite green in the 1324.

**Listener (Task 5):** grammar rejection matrix (10 malformed shapes + no-callback update, zero `Bot` construction) ✔; letter→word mapping ✔; poisoned update acked + loop survives ✔; generic error and 409 Conflict both log AND back off with the *correct constant* (spy `assert_called_once_with`, constant-specific — mutation-hardened in review round 1) ✔; terminal dispatch: real DB skip + exact `edit_message_text` kwargs (status appended, keyboard removed) + `answer_callback_query` + operator-decision INFO log (action+link+status, no token) ✔.

**Ordering invariant (Task 3):** token persisted BEFORE send — enforced by an at-call-time probe inside the send mock with a `probe_hits` vacuity guard (mutation-hardened in review round 1) ✔.

## Acceptance Criteria mapping (tech-spec)

| AC | Status |
|---|---|
| 1. `reply_markup` forwarding / omitted byte-identical | ✔ `TestReplyMarkupForwarding` (3) |
| 2. E014 two buttons exact `callback_data`; no other alert | ✔ send-site + only-E014 tests — *compatibility seam gap M-1* |
| 3. Token round-trip; unknown → stale | ✔ `TestReviewTokenStore` + stale branch |
| 4. Numeric-admin auth + all branches | ✔ full matrix above |
| 5. Cancel-before-slot / post-publish cancel | ✔ (L-1 caveat, adjudicated) |
| 6. `_connect()` busy_timeout; no `database is locked` | ✔ 4 tests incl. negative control |
| 7. Listener starts iff flag+numeric admin; else no thread | ✔ at function seam — *main() wiring gap M-2* |
| 8. Listener error never aborts publish loop | ✔ error/409/poisoned-update trio + daemon flag |
| 9. Terminal press → INFO decision log | ✔ dispatch test (L-4 degraded corner) |
| 10. Full suite green, no regressions | ✔ 1324/1324 recorded |

## Pyramid, duplication, determinism

**Pyramid:** ~15 unit (keyboard 4, forwarding 3, token store 6, connect contract 2) / ~25 integration-flavored on real SQLite (concurrency 2, resolve 10, send-site 3, listener 10) / 0 automated E2E. The zero-E2E is per tech-spec and NOT a gap: the shared bot token means only prod can poll — covered by the operator's manual post-deploy check (Task 12). Distribution fits the test-master profile for an API/backend bot; the integration-heavy top half is justified because the feature's risk lives in DB state transitions and the send↔listen seams, not in pure computation.

**Duplication:** no redundant tests found. Apparent overlaps each earn their place: resolve-level vs listener-level non-admin tests pin different seams (pure gate contract vs SEC-T5-1 zero-DB-read + answer-only I/O behavior); keyboard shape re-asserted at the send site proves the site uses the builder with the *minted* token, not just that the builder works; the PRAGMA behavior test alongside the connect call-spy is a deliberate behavior-plus-code-line pair (its docstring explains why both exist). Terminal-outcome application in the listener is exercised once (cancel) with keep covered at the mapping seam — correct economy, the application code path is shared.

**Determinism:** zero `time.sleep`/wall-clock/network in all audited feature tests (grep-verified); synchronization is Event-based (timeouts are 10 s failure guards, not schedule dependencies); listener loop driven in-thread via `stop_event` with backoff constants patched to 0; all Telegram I/O mocked. The c8519da fix is verified correct: `TestSlotLoopTransientRetry` now inherits `_DistribLoopBase` (frozen `datetime.now` = 10:00 МСК, the file's own precedent), retry semantics unchanged, with an explanatory docstring — suite green regardless of run time (confirmed at 22:51 МСК in decisions.md and at audit time).

**Review-round hygiene:** all prior test-reviewer findings (task 1 HIGH+MEDIUM+LOW, task 3 MEDIUM, task 5 MAJOR) verified as actually fixed in the final files, each with independent mutation confirmation on record. This audit did not repeat those mutations; its one new spot-check (M-2) targeted a seam no prior round examined.

## Recommendations (priority order)

1. **M-1:** add the ~4-line builder→parser round-trip test (Task 10 window or 5-min follow-up).
2. **M-2:** pin the `main()` wiring (patch-assert in `_run_main_once` or `inspect.getsource`); until then, Task 12's «review listener active» log check is the compensating control — keep it mandatory in the rollout runbook.
3. L-2: consider dropping the `os.environ` assertion to de-flake developer environments.
4. L-1/L-3/L-4: no action; recorded so they aren't re-discovered later.

---

## Fix verification round — commit `ed10c58` (2026-07-25, test-auditor)

Both audit findings verified **RESOLVED**. Suite at `ed10c58`: **1333 passed, 0 failed** (48 s) = 1324 baseline + 9 new tests (the +9 also covers other audit-wave findings: CA-1/CA-2/CA-3/CA-5/SEC-A8-1 — outside this report's two findings, not re-audited here beyond noting their tests pass).

**M-2 — RESOLVED (mutation re-verified).** New test `tests/test_job_distributed_publish.py::TestMainHealthChecks::test_main_wires_review_listener` patches `news_bot._maybe_start_review_listener` inside the existing `_run_main_once` harness and does `assert_called_once_with()`. Re-ran the exact audit mutation: replaced `news_bot.py` line `_maybe_start_review_listener()` in `main()` with `pass` → the new test **FAILS** (AssertionError from `assert_called_once_with`; pre-fix the identical mutation left all 1324 tests green). Restored via `git checkout -- news_bot.py`; `git diff` afterward shows only the uncommitted `work/.../decisions.md` log entries — code tree clean.

**M-1 — RESOLVED (genuine seam test, not a tautology).** New test `tests/test_integration.py::TestReviewListener::test_keyboard_callback_data_round_trips_through_parser` calls the REAL `admin_alerts.build_dedup_review_keyboard` with an independently minted `secrets.token_urlsafe(9)` token and feeds each button's actual `callback_data` string into the REAL `news_bot._parse_review_callback_data`, asserting `[('cancel', token), ('keep', token)]`. No mocks on either side; the expected value derives from the independent token, not from either implementation, so neither end can satisfy the assertion by construction — it spans the `admin_alerts`→`news_bot` seam. Litmus mutation confirmed: builder prefix `dd:` → `dx:` on the cancel button → the round-trip test **FAILS**; restored (`git checkout -- admin_alerts.py`), tree clean, full suite back to 1333 green. The silent-drift class described in M-1 (tandem edit of builder + its unit test) is now caught.

Audit verdict unchanged: **GO** — now with 0 open medium findings (2 medium → resolved; 4 low remain informational/no-action, of which L-4 was incidentally also closed by `ed10c58`'s CA-2 test `test_decision_logged_even_when_edit_fails`).
