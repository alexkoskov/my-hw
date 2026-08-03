# Task 7 — Code Audit: dedup-review-buttons (holistic, final state)

**Date:** 2026-07-24
**Auditor:** code-auditor (Audit Wave, task 7)
**Scope:** final state of `pending_articles_repo.py`, `admin_alerts.py`, `news_bot.py`
(flag, E014 send site, `resolve_dedup_callback`, `_is_admin_press`, listener helpers,
`_run_review_listener`, `main()` wiring), `.env.example`; reference module
`outage_state.py`; feature tech-spec (Architecture + Shared resources) and project
knowledge (`architecture.md`, `patterns.md`, `deployment.md`). Tests read for context
only (their quality is Task 9's scope; security depth is Task 8's).
**Method:** full-file reads (not diffs), cross-checked against tech-spec Decisions
1–10, the Shared resources table, and the `code-research.md` baseline coordinates.
**Baseline:** `python3 -m pytest -q` → **1324 passed, 0 failed** (45.65s), run during
this audit. `git status` for all feature source files → clean (no edits by this task).

## Summary

| Severity | Count |
|----------|-------|
| blocker  | 0 |
| major    | 1 |
| minor    | 2 |
| nit      | 3 |

**Verdict: issues found — no blockers.** The four focus dimensions (thread-safety,
shared-resource compliance, Bot-init consistency, listener error isolation) are
structurally sound; the one major finding is a residual cross-component race
(cancel pressed while the same article's publish is in flight) that the tech-spec
believed was covered but is not fully.

Accepted per-task findings were NOT re-litigated: SEC-T5-2 (traceback redaction,
declined with reason), the fresh-Bot-per-call deviation (approved, and verified
consistent below), and the tech-spec's explicit "no token janitor" trade-off
(Data Models: «rows are tiny; a press cleans its own token»).

---

## Focus dimension 1 — Thread-safety: listener thread vs publish loop

**Assessment: PASS (no findings).**

- **Per-thread connections.** No sqlite3 connection ever crosses a thread. Every
  repo/state helper the listener touches (`get_review_token_link`,
  `delete_review_token`, `get_pending`, `get_published`, `skip_pending`) opens its
  own short-lived `_connect()` and closes in `finally`
  (`pending_articles_repo.py:1049-1101, 747-777`). The publish loop's long-lived
  `dedup_conn` (`news_bot.py` job() dedup gate) is created and closed inside the
  main thread only. Matches the Shared resources table ("separate `_connect()` per
  thread ... 1 file, N per-thread connections").
- **Writer serialization.** `pending_articles_repo._connect()` pins
  `sqlite3.connect(..., timeout=5.0)` (`pending_articles_repo.py:182-197`) — the
  same 5 s contention absorption as `outage_state._connect()`'s
  `PRAGMA busy_timeout = 5000` (`outage_state.py:105-115`); the mechanism
  difference is deliberate and documented (protects the execute-counter in the
  fault-injection test; architecture.md was corrected in commit 1470468 to state
  exactly this). Token writes use `BEGIN IMMEDIATE`
  (`pending_articles_repo.py:1059, 1091`), mirroring `outage_state._set`
  (`outage_state.py:140`), so listener-vs-publish-loop write contention resolves
  through the busy handler, not a lock-upgrade deadlock. The two-writer test
  (`tests/test_pending_articles_repo.py::TestConcurrentWriters`) pins this.
- **No shared mutable state.** The listener's `offset` is a local variable
  (`news_bot.py:870`); each Telegram call constructs a fresh `Bot` inside its own
  `asyncio.run` event loop (`news_bot.py:725-767`), so no `Bot`/httpx client/event
  loop is shared between threads or across loops. Module globals the listener reads
  (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `REVIEW_BUTTONS_ENABLED`, backoff
  constants) are set once at import and never written at runtime — read-only
  sharing is safe under the GIL.
- **Same-thread dual-connection check at the send site:** `put_review_token`
  opens its own connection while job()'s `dedup_conn` is open
  (`news_bot.py:2847-2853`). No deadlock: `dedup_conn` is in autocommit at that
  point (only SELECTs executed; `mark_pair_pinged` + commit happen after) so it
  holds no lock while `put_review_token`'s `BEGIN IMMEDIATE` runs.

## Focus dimension 2 — Shared-resource compliance (tech-spec Architecture)

**Assessment: PASS (no findings).**

- **`news.db`** — one file, per-thread short-lived connections (above), verified
  against the Shared resources table.
- **Bot token / single poller.** Exactly one `get_updates` consumer exists in the
  codebase (`_review_get_updates`, `news_bot.py:725-742`; grep found no other), it
  runs only when `_maybe_start_review_listener()` passes the gate, and the gate is
  the single flag `REVIEW_BUTTONS_ENABLED` (default OFF — allowlist of on-words,
  `news_bot.py:147-149`) plus numeric `TELEGRAM_ADMIN_ID` (fail-closed,
  `news_bot.py:925-941`). A 409 `Conflict` is caught specifically, explains the
  single-listener rule in the log, and backs off 60 s (`news_bot.py:874-887`).
- **Double gate is real.** The SAME module attribute gates keyboard attachment at
  the E014 send site (`news_bot.py:2848`) and the listener startup
  (`news_bot.py:954`); a flag-off instance mints no token, renders no buttons, and
  never polls — exactly Decision 6. `.env.example:15-23` documents default OFF,
  prod-only, the 409 constraint, and the numeric-admin requirement; wording matches
  code behaviour.
- **E014-only buttons.** `reply_markup` is passed at exactly one call site
  (`news_bot.py:2866`); all other alerts (E006/E008/E009/E015/E016/E034) use the
  default `None`. Mint/put/build live inside the existing E014 `try` and inside
  `if alerted:` — a storage fault degrades to "Failed to send E014 notification"
  and rate-limited flags mint nothing (`news_bot.py:2834-2872`).

## Focus dimension 3 — Bot-init pattern drift

**Assessment: PASS (no drift; one adjacent minor — CA-3).**

All four Telegram call sites construct the bot identically —
`Bot(token=TELEGRAM_BOT_TOKEN)` from the same module global, inside the coroutine,
one `asyncio.run` per call: `send_admin_notification._send` (`news_bot.py:518`),
`_review_get_updates` (`:737`), `_review_edit_message` (`:751`),
`_review_answer_callback` (`:766`). The per-call (rather than loop-lifetime) Bot in
the listener is the approved Task 5 deviation; the cross-event-loop rationale
(httpx pool bound to the creating loop) is documented at `news_bot.py:726-735` and
is correct for `python-telegram-bot==21.10` (verified installed source:
`get_updates` also adds the long-poll `timeout` to the HTTP read timeout, so
`timeout=30` cannot self-time-out). No duplicated token source, no config fork.
The only asymmetry is that the send path guards missing credentials
(`news_bot.py:508`) while the listener path does not — recorded as CA-3 (minor).

## Focus dimension 4 — Listener error isolation

**Assessment: PASS (no findings on isolation itself; CA-2 is an adjacent logging
ordering issue).**

Three concentric guards in `_run_review_listener` (`news_bot.py:846-922`):
per-update try/except (offset acked BEFORE handling, so a poisoned update is
consumed exactly once and skipped, `:902-915`), poll-cycle try/except with 5 s
backoff and a dedicated 60 s `Conflict` branch (`:872-900`, exception order
correct — `Conflict` before `Exception`), and a belt-and-braces outer handler
(`:916-922`) so nothing ever escapes the thread. The thread is `daemon=True`
(`news_bot.py:976-980`); its death cannot keep the process alive or take the
publish loop down. Backoff sleeps go through the `stop_event`-aware
`_review_listener_sleep` seam and are mutation-pinned by tests (afe4944). Startup
pings in `_maybe_start_review_listener` are individually try/excepted so a
Telegram fault cannot break `main()` (`:962-992`). Error messages route through
`sanitize_error_message` (`:883, :895`). This satisfies the spirit of
patterns.md → Error Isolation (per-unit try/catch, degraded-not-dead) transposed
to a thread, and the tech-spec "Listener thread crashes" risk row.

---

## Findings

### CA-1 (major) — Cancel pressed during an in-flight publish: article publishes to the channel, operator is told «✅ Отменено оператором», `published_articles` silently misses the row

- **Where:** `news_bot.py:639-656` (`resolve_dedup_callback` cancel branch),
  `pending_articles_repo.py:642-644` (`move_to_published` silent no-op on missing
  row), `news_bot.py:2036-2067` (`_fallback_publish` idempotency guard — top of
  function only), tech-spec «How it works» ("The `_fallback_publish` idempotency
  guard (`get_published` check at top) already covers the residual boundary case"
  — this claim is what the audit contradicts).
- **What:** The Task 4 polish (c2c7e0d) closed the race where the publish
  *completes* between `get_pending` and `skip_pending` (post-skip
  `get_published` re-read → honest «уже опубликовано»). It did NOT close the wider
  window where the publish is *in flight* when cancel lands. `_fallback_publish`
  runs for tens of seconds to minutes per article (LLM transcreation → Telegraph →
  3 s cache warmup → Telegram teaser → `move_to_published`), and the pending row
  exists until the final step. Sequence:
  1. Slot loop picks the flagged row, `_fallback_publish` passes its top
     `get_published` guard (row not yet published) and starts transcreating.
  2. Operator presses «🚫 Не публиковать». `resolve_dedup_callback`:
     `get_pending(link)` → row still present → `skip_pending(link)` deletes it and
     stamps `processed_news`; post-skip `get_published(link)` → still `None`
     (move hasn't run) → answers **«✅ Отменено оператором»**, token consumed.
  3. The in-flight publish continues regardless: `update_staged` /
     `mark_telegraph_published` no-op on the deleted row (rowcount 0, return
     ignored), the **teaser still posts to the channel** (`news_bot.py:2275-2281`),
     and `move_to_published` finds `src is None` and **returns silently**
     (`pending_articles_repo.py:642-644` — "treat as no-op rather than error").
- **Evidence of impact:** channel post exists; operator was told it was cancelled
  (decision effectively lost, silently); `published_articles` has no row for a
  post that IS in the channel — which also skews every consumer of that table
  (crash-loop guard `get_max_published_at`, the 7-day published-fingerprint dedup
  window, the E017 dry-spell check, E034 recap says "published" while the button
  says "cancelled"). No crash, no redelivery loop, dedup still holds via
  `processed_news` (skip_pending wrote it).
- **Why major, not blocker:** the window exists only while THAT article's own slot
  publish is executing (minutes/day at most, and only on days with an E014 flag on
  the head-of-queue article); the channel outcome is visible to the operator; no
  state is corrupted beyond the missing audit row; the feature's stated fallback
  («уже опубликовано, отменить нельзя») shows the designers accepted near-miss
  presses — this race just answers the wrong string in the near-miss.
- **Recommendation (fix outside this task):** pick one or combine:
  (a) in `_fallback_publish`, re-check `get_pending(link)` immediately before the
  teaser send (Step 4) and abort the publish (return True, no post) if the row
  vanished — shrinks the dishonesty window from minutes to milliseconds;
  (b) make `move_to_published` log a WARNING instead of a silent no-op when the
  pending row is missing after a teaser was sent (it already has a post-commit
  defensive precedent), and dozapis the `published_articles` row from explicit
  arguments so the audit table matches the channel;
  (c) at minimum, correct the tech-spec sentence claiming the top-of-function
  guard covers this boundary case.

### CA-2 (minor) — Operator-decision INFO log is emitted only after both Telegram calls succeed

- **Where:** `news_bot.py:821-835` (`_handle_review_update`: `edit_message_text`
  → `answer_callback_query` → `logger.info("[review] operator decision: ...")`).
- **What:** By the time the edit runs, the decision is already applied
  (`skip_pending` done, token deleted). If `edit_message_text` or
  `answer_callback_query` raises (transient TelegramError, message deleted,
  >4096-char edit), the per-update guard catches it and logs a generic
  "failed to handle update N — skipped" traceback — the dedicated
  action+link+status audit line is lost even though the state change happened.
  user-spec requires every operator decision to land in the log.
- **Recommendation:** move the `logger.info` to immediately after
  `resolve_dedup_callback` returns a terminal outcome, before any Telegram I/O
  (the log needs nothing from the edit/answer calls).

### CA-3 (minor) — Listener gate does not check `TELEGRAM_BOT_TOKEN`; misconfig produces a perpetual 5 s error loop

- **Where:** `news_bot.py:925-941` (`_review_listener_enabled` checks only flag +
  numeric admin) vs `news_bot.py:508` (send path refuses to run without
  credentials).
- **What:** With `REVIEW_BUTTONS_ENABLED=1`, numeric admin id, and an unset/empty
  `TELEGRAM_BOT_TOKEN`, the listener starts; `Bot(token=None)` raises
  `InvalidToken` inside `_review_get_updates`, is caught by the generic poll-cycle
  handler, and retries every 5 s forever — an ERROR-log stream for the process
  lifetime. Isolation holds (publish loop unaffected; it is equally dead without a
  token), so this is operational noise in a config that is broken anyway.
- **Recommendation:** add a `TELEGRAM_BOT_TOKEN` presence check to
  `_review_listener_enabled` (or to `_maybe_start_review_listener`'s warning
  branch), mirroring the send path's guard.

### CA-4 (nit) — Duplicate token→link read per admin press, with a cosmetic TOCTOU in the log

- **Where:** `news_bot.py:807-813` (`_handle_review_update` pre-fetches
  `get_review_token_link(token)` for the decision log) and `news_bot.py:632`
  (`resolve_dedup_callback` reads it again as the source of truth).
- **What:** Two DB reads per admin press; deliberate and documented ("single
  source of truth — resolve deletes it"), and post-afe4944 the pre-fetch runs only
  for admin presses. Residual: the logged `link` can theoretically diverge from
  the link resolve acted on (a `put_review_token` racing between the two reads) —
  log-cosmetic only. If resolve ever returned the link alongside the status, both
  reads and the divergence would disappear.

### CA-5 (nit) — 64-byte callback_data cap enforced in characters, not bytes

- **Where:** `news_bot.py:698, 714` (`_REVIEW_CALLBACK_DATA_MAX_LEN = 64`;
  `len(data) > _REVIEW_CALLBACK_DATA_MAX_LEN` on a `str`).
- **What:** The comment says "Telegram hard-caps callback_data at 64 bytes";
  `len(str)` counts code points, so a non-ASCII payload of ≤64 chars but >64
  UTF-8 bytes passes this pre-filter. Harmless downstream: such data still has to
  match the exact `dd:<c|k>:<token>` grammar, and an unknown token resolves to the
  stale branch (admin) or an empty answer (non-admin). Own keyboard tokens are
  ASCII (`token_urlsafe`). Optional: compare `len(data.encode('utf-8'))`.

### CA-6 (nit) — Redundant flag check in `_maybe_start_review_listener`

- **Where:** `news_bot.py:954-956` (`if not REVIEW_BUTTONS_ENABLED: return None`
  immediately followed by `if not _review_listener_enabled()`, which re-checks the
  same flag at `news_bot.py:935`).
- **What:** The duplication is functionally intentional (distinguish "flag off →
  silent" from "flag on but non-numeric admin → warn + ping") but encodes the
  distinction implicitly. A gate returning a reason (`'off' | 'bad_admin' | 'ok'`)
  would make the three startup shapes explicit and single-source the flag read.
  Pure readability; behaviour is correct and test-pinned.

---

## General code quality (in-scope dimensions)

- **Readability / conventions:** consistent with the codebase — module-level
  constants with rationale comments, const↔env name contract preserved
  (`REVIEW_BUTTONS_ENABLED` mirrors `DEDUP_SERIES_ENABLED` with a documented,
  deliberate inverted default), Google-style docstrings, `[review]` /
  `[startup]` log prefixes match existing greppable-code culture, no secrets or
  token values in logs (decision log deliberately omits the token,
  `news_bot.py:830-835`).
- **Duplication / dead code:** none introduced. All new helpers have exactly one
  production caller each (plus tests); `_is_admin_press` correctly deduplicates the
  auth check between `resolve_dedup_callback` and `_handle_review_update`
  (afe4944). Token store deliberately follows the `outage_state._get/_set` contour
  rather than the `conn`-accepting dedup-helper contour, with the reason documented
  at `pending_articles_repo.py:1038-1042`.
- **Backward compatibility of the send path:** `reply_markup` is keyword-only with
  default `None` (`news_bot.py:486-488`); every pre-existing positional
  `send_admin_notification(...)` call is unaffected, and `reply_markup` correctly
  bypasses `_redact_text` (telegram object, not text).
- **SQL hygiene:** token helpers use `?` placeholders only; key prefix constant
  matches the established `bot_state` prefix pattern; DDL untouched.
- **Docs vs code:** `architecture.md` (Inbound review path, `review_token:` key,
  busy-timeout wording post-1470468), `deployment.md` runbook, and
  `.env.example` all byte-match the implemented grammar (`dd:c:/dd:k:`), gate
  semantics, backoffs (5 s / 60 s), poll parameters
  (`timeout=30, allowed_updates=['callback_query']`) and log line
  («review listener active»). One spec inaccuracy found — the tech-spec's residual
  -race coverage claim — recorded inside CA-1, not as a separate finding.
- **Edge cases from the task file:** stale token after restart / double press →
  idempotent («Кнопка устарела», delete of absent token is a no-op) — OK;
  non-numeric `TELEGRAM_ADMIN_ID` → fail-closed at both listener startup and
  per-press gate — OK; callback_data ≤64 bytes → token scheme holds
  (`dd:c:` + ~12 chars ≈ 17 bytes) — OK; in-flight publish race → NOT fully
  covered — CA-1.

## Explicit statement on critical findings

**No blocker-level findings.** One major (CA-1) — a residual race that should be
addressed in a follow-up fix task (analysis only here; no code was changed by this
audit).

---

## Fix verification round — commit `ed10c58` (2026-07-25)

Verified against the committed final state (full reads of the changed regions, not
just the diff) plus targeted test runs. Independent full-suite re-run at `ed10c58`:
**1333 passed, 0 failed** (51.81s) — matches the fixer's count (baseline 1324 + 9).

| Finding | Verdict | Evidence |
|---------|---------|----------|
| CA-1 (major) | **resolved** | Two-sided guard, exactly the recommended (a)+(b)+(c): **(a)** `_fallback_publish` re-checks `get_pending(link)` immediately before the Telegram teaser and aborts as success-without-publish if the row vanished (`news_bot.py:2340-2347` — no channel post, no strike; deliberately bypassed on teaser-already-sent retries, where completing the move is the consistent outcome). **(b)** `move_to_published`'s missing-row path is no longer a silent no-op: WARNING + defensive dozapis of the `published_articles` row from explicit args, title recovered from the `processed_news` stamp with link fallback for the NOT NULL columns, `processed_news` re-stamped (`pending_articles_repo.py:641-687`). **(c)** the inaccurate coverage claim corrected in both tech-spec «How it works» and `architecture.md` (plus `deployment.md` gate wording). Tests: `TestInFlightCancelGuard` (abort-before-teaser + control), `TestMoves::test_move_to_published_missing_row_warns_and_dozapis_published`, `..._no_processed_news_uses_link` — all pass. |
| CA-2 (minor) | **resolved** | Operator-decision INFO log moved to right after `resolve_dedup_callback` returns a terminal outcome, BEFORE `edit_message_text`/`answer_callback_query` (`news_bot.py:828-836`). Pinned by `TestReviewListener::test_decision_logged_even_when_edit_fails` — passes. |
| CA-3 (minor) | **resolved** | `_review_listener_gate_reason()` returns `'no_token'` for an empty `TELEGRAM_BOT_TOKEN`; the listener refuses to start with a WARNING + admin ping naming the broken knob (`news_bot.py:935-1020`) — no perpetual 5 s error loop. Pinned by `test_review_listener_not_started_when_bot_token_missing` — passes. Bonus beyond the finding (SEC-A8-1, Task 8's): the E014 send site now gates on `_review_listener_enabled()` instead of the bare flag, so an instance that can't listen also mints no tokens and renders no buttons. |
| CA-4 (nit) | **not addressed — accepted** | The duplicate `get_review_token_link` pre-fetch remains, and is now load-bearing for CA-2's fix (the link must be captured before resolve consumes the token to log it pre-I/O). The optional "resolve returns the link" refactor stays optional; no action required. |
| CA-5 (nit) | **resolved** | `_REVIEW_CALLBACK_DATA_MAX_BYTES` compared against `len(data.encode('utf-8'))` (`news_bot.py:695-717`); grammar rejection matrix now includes `'dd:c:' + 'ю'*30` (35 chars / 65 UTF-8 bytes). |
| CA-6 (nit) | **resolved** | Single flag read via `_review_listener_gate_reason()` (`'ok' | 'off' | 'no_token' | 'bad_admin'`); `_maybe_start_review_listener` branches on the reason, `_review_listener_enabled()` reduces to `reason == 'ok'`. |

**Residual observations (non-blocking, recorded for completeness):**
1. The CA-1a abort path returns `True` → `_publish_with_retries` maps it to
   `'published'` → the E034 recap counts a cancelled-mid-flight article as
   published although nothing posted (`news_bot.py:2657-2659, 3144-3146`). Same
   pre-existing semantics as the zombie idempotency-guard path (also returns
   `True`); the `[review-cancel]` INFO line tells the truth. Cosmetic.
2. A cancel landing in the guard→teaser window (milliseconds) still posts and
   answers «Отменено» — but the audit table is now consistent via the CA-1b
   dozapis. This is exactly the accepted residual from the recommendation.
3. Flake note for Task 9 / lead: the NEW `test_main_wires_review_listener` (M-2
   pin, `tests/test_job_distributed_publish.py:398-416`) failed ONCE
   (`assert_called_once_with`, mock.py:990) in a targeted 5-class subset run
   during this verification; it passed in isolation, in 12/12 stress re-runs of
   the same subset, and in the full suite. Not reproducible; likely a rare
   order/timing artifact. Worth watching, not a resolution blocker.

**Round verdict: all audit findings resolved** (CA-4 nit consciously left, with a
now-stronger reason to keep the pre-fetch). Docs and spec match the new code.
