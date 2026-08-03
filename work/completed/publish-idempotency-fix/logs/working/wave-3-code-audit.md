# Wave 3 — Code Audit (Task 6)

**Verdict: PASS — ready for Pre-deploy QA (Task 9).**

Feature: `publish-idempotency-fix`
Auditor: code-reviewer (Task 6)
Date: 2026-05-06
Scope: holistic post-Wave-2 audit of the two surgical edits (idempotency
guard in `news_bot._fallback_publish` + `INSERT OR IGNORE` in
`pending_articles_repo.move_to_published`) plus the 7 new tests across
3 test files.

## Summary

Two source edits and seven tests land cleanly against the tech-spec.
Guard is positioned at the dominator point in `_fallback_publish`
(immediately after `link = row['link']`, before all 4 branching
side-effects), uses the correct logger level + marker tag, satisfies
Decisions 1, 2, 3, 4, 8 verbatim. The `INSERT OR IGNORE` change is a
single-keyword edit that preserves the bind tuple, parameterization,
and transaction shape. No findings — no fix-cycle requested.

## Decision-by-decision check

### Decision 1 — Guard at `news_bot.py:985`, INFO log + `[idempotency-guard]` tag

**Tech-spec quote:** «Insert the idempotency guard at the very top of
`_fallback_publish`, immediately after `link = row['link']` on line 984.
Use `logger.info` (not WARNING) with `[idempotency-guard]` marker tag».

**Code (`news_bot.py:984–996`):**
```
984:    link = row['link']
985:
986:    # Idempotency guard (publish-idempotency-fix, Decisions 1, 2, 3, 4, 8).
...
991:    existing = pending_repo.get_published(link)
992:    if existing is not None:
993:        logger.info(
994:            f"[idempotency-guard] {link} already in published_articles — "
995:            f"skipping re-publish of stale pending row"
996:        )
```

**Dominator-position check** — branching points (post-Task-1, +36 line
shift from the tech-spec quoted positions due to the guard block itself):

| Branch | Tech-spec line | Post-Task-1 line | Position vs guard |
|---|---|---|---|
| `is_fallback_active()` shortcut | 1045 | 1081 | AFTER guard (✓) |
| `transcreate_via_claude` (Claude success) | 1052 | 1092 | AFTER guard (✓) |
| `except ClaudeTranscreationError` (per-article) | 1065 | 1101 | AFTER guard (✓) |
| `except ClaudeOutageError` (degraded mode) | 1079 | 1115 | AFTER guard (✓) |

Verified via `grep -n` on `is_fallback_active`, `transcreate_via_claude`,
`ClaudeTranscreationError`, `ClaudeOutageError`. All four branches sit
strictly below the guard's `return True` at line 1020 — the guard is the
single architectural dominator described in the tech-spec.

**Verdict:** PASS.

### Decision 2 — Reuse `skip_pending(link)` for cleanup

**Tech-spec quote:** «Cleanup path of the guard calls
`pending_articles_repo.skip_pending(link)` rather than direct `DELETE
FROM pending_articles`.»

**Code (`news_bot.py:1007–1008`):**
```
1007:        try:
1008:            pending_repo.skip_pending(link)
```

The guard calls `pending_repo.skip_pending(link)` — alias resolved at
`news_bot.py:48` (`import pending_articles_repo as pending_repo`). The
underlying `skip_pending` (`pending_articles_repo.py:656–686`) does
exactly the documented two-step transaction: `INSERT OR IGNORE INTO
processed_news` + `DELETE FROM pending_articles` + `commit()`. No
direct `DELETE` introduced; semantics align with the manual-operator
skip path.

**Verdict:** PASS.

### Decision 3 — Admin ping mandatory, prefix `⚠️ Skipped re-publish of `

**Tech-spec quote:** «Every guard activation sends one admin ping via
`send_admin_notification`. Not toggleable.»

Architecture diagram: `ping_ok = send_admin_notification("⚠️ Skipped
re-publish of {link} — already in published_articles. Investigate stale
pending row.")`.

**Code (`news_bot.py:997–1001`):**
```
997:        ping_text = (
998:            f"⚠️ Skipped re-publish of {link} — already in "
999:            f"published_articles. Investigate stale pending row."
1000:        )
1001:        ping_ok = send_admin_notification(ping_text)
```

Prefix matches verbatim. `link` interpolated. Ping is mandatory (no
config gate, no toggle). `send_admin_notification` already runs
payload through `_redact_text` internally
(`news_bot.py:368`) — verified per patterns.md (line 156: «`_redact_text`
is reused by `send_admin_notification` so admin Telegram pings (non-
logging path) cannot leak the API key»). The ping text contains only
the (publicly-visible) article URL, so redaction is moot here, but the
defense-in-depth is preserved.

**Verdict:** PASS.

### Decision 4 — No try/except around `send_admin_notification`; check return value

**Tech-spec quote:** «Capture the return value of
`send_admin_notification`. If `False`, log WARNING and continue with
cleanup. Do NOT wrap the call in try/except: function never raises».

**Code (`news_bot.py:1001–1006`):**
```
1001:        ping_ok = send_admin_notification(ping_text)
1002:        if not ping_ok:
1003:            logger.warning(
1004:                f"admin ping for [idempotency-guard] skip of {link} failed "
1005:                f"(Telegram down or credentials missing) — continuing cleanup"
1006:            )
```

No try/except wraps the call. Return value is captured and inspected;
WARNING (not ERROR) is logged on `False` per the tech-spec rationale
(degraded-mode alert, not a publish error).

**Cross-verified contract** — `send_admin_notification`
(`news_bot.py:357–392`) catches `TelegramError` internally (line 389),
returns `False` on missing credentials (line 367), `True` / `False`
from the inner `_send` coroutine. No `raise` reachable from any branch.
The "function never raises" invariant is accurate for the inputs the
guard provides (a plain string).

**Cleanup-failure path uses bare `send_admin_notification` too** —
`news_bot.py:1015` calls the function without try/except either. The
return value of the cleanup-failure ping is intentionally NOT checked
(symmetric with the pattern: subscriber safety > operator alert
delivery; `return True` runs unconditionally on the next line). This is
not a finding — Decision 4 only mandates "no try/except" and "check
return value if you care about it"; in the cleanup-failure branch the
guard has nothing to do with the result (it's already returning True).

**Verdict:** PASS.

### Decision 8 — `skip_pending` failure: log ERROR + 2nd admin ping + return True

**Tech-spec quote:** «If `skip_pending` raises during guard cleanup,
the guard logs ERROR + sends a second admin ping about the cleanup
failure + STILL returns `True`».

**Code (`news_bot.py:1007–1020`):**
```
1007:        try:
1008:            pending_repo.skip_pending(link)
1009:        except Exception as cleanup_err:
1010:            logger.error(
1011:                f"[idempotency-guard] skip_pending failed for {link}: "
1012:                f"{cleanup_err!r} — leaving row in pending; next slot's guard "
1013:                f"will retry cleanup"
1014:            )
1015:            send_admin_notification(
1016:                f"⚠️ Idempotency-guard cleanup failed for {link}: "
1017:                f"{type(cleanup_err).__name__}. Pending row will retry on "
1018:                f"next slot."
1019:            )
1020:        return True
```

Exception handler catches broad `Exception` (correct — covers
`sqlite3.OperationalError`, `IntegrityError`, etc.), logs `logger.error`
(level matches Decision 8), sends a second admin ping with cleanup-
failure context (uses `type(cleanup_err).__name__` rather than full
`str(exc)` — matches the patterns.md security rule on line 85: «admin-
ping template uses `type(exc).__name__` not `str(exc)` for user-visible
messages»), and `return True` is OUTSIDE the try/except (line 1020 at
indent level of the `if existing is not None:` block, not the `except`),
so it runs whether cleanup succeeded or raised. AC6 is preserved: slot
loop never sees an exception, never increments `attempt_count`.

**Verdict:** PASS.

### Decision 5 — `INSERT OR IGNORE` on `move_to_published`, line 582

**Tech-spec quote:** «Change `INSERT INTO published_articles` (line 582)
to `INSERT OR IGNORE INTO published_articles`. Steps 2 (`processed_news`)
and 3 (`DELETE pending`) execute unconditionally on the same
transaction.»

**Code (`pending_articles_repo.py:580–602`):**
```
580:        # Step 1
581:        conn.execute(
582:            "INSERT OR IGNORE INTO published_articles "
583:            "(link, title, ru_title, telegraph_url, telegraph_path, "
584:            " source_name, via_review) "
585:            "VALUES (?, ?, ?, ?, ?, ?, ?)",
586:            (
587:                link, title, ru_title, telegraph_url, telegraph_path,
588:                source_name, 1 if via_review else 0,
589:            ),
590:        )
591:        # Step 2
592:        conn.execute(
593:            "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
594:            "VALUES (?, ?, ?)",
595:            (link, title, pub_date),
596:        )
597:        # Step 3
598:        conn.execute(
599:            "DELETE FROM pending_articles WHERE link=?",
600:            (link,),
601:        )
602:        conn.commit()
```

Single-keyword change. Bind tuple (line 586–589) unchanged — same 7
positional placeholders. Parameterization preserved (no string
interpolation; SQLite native `?` binds). Steps 2 and 3 untouched.
Transaction is still a single `conn.execute × 3` + `conn.commit()` in
one `try/except: rollback; raise / finally: close()` shape (lines
562–607). Operator-driven retry (e.g. `hw_review.cmd_publish` → second
call to `move_to_published`) becomes a silent no-op on the published
row, but still cleans `processed_news` and `pending_articles`. First-
publish values are preserved (`INSERT OR IGNORE`, NOT `INSERT OR
REPLACE`), satisfying AC7.

**Verdict:** PASS.

## Project-patterns check

### Logging level + marker

Existing `_fallback_publish` flow uses `logger.info` for routine flow
decisions (`news_bot.py:1083` for the outage shortcut: `[fallback]
is_fallback_active=True — routing {link} via Google`). Guard hit log
matches the same pattern: `logger.info(f"[idempotency-guard] {link}
already in published_articles — skipping re-publish of stale pending
row")` at line 993. WARNING is reserved for the admin-ping-failed sub-
event (line 1003) and the per-article LLM failure case (line 1109).
ERROR is used for cleanup failure (line 1010). Levels match the
project pattern: INFO for expected routing, WARNING for degraded mode
(ping failed, channel still safe), ERROR for an actual code-path that
violates an invariant (skip_pending failed mid-transaction).

**Verdict:** PASS.

### Admin-ping format

Both pings (lines 997–1000 and 1015–1019) start with `⚠️ ` (matches
existing channel for warning-style alerts; cf. Decision 12 redaction
pattern in patterns.md line 85). No secrets in payload (publicly
visible URL only; cleanup ping uses `type(exc).__name__` per the
patterns.md guidance). `_redact_text` runs inside
`send_admin_notification` so even if a future caller embedded a token
the regex would catch it.

**Verdict:** PASS.

### Use of `pending_repo` alias (NOT `pending_articles_repo`)

`news_bot.py:48`: `import pending_articles_repo as pending_repo`. The
guard uses `pending_repo.get_published(link)` (line 991) and
`pending_repo.skip_pending(link)` (line 1008) — consistent with the
existing call sites in `news_bot.py` (e.g. line 1166 area uses the
same alias). No mixed usage of the long form `pending_articles_repo.X`
inside `news_bot.py`. PASS.

### Duplicate-logic check

The guard at `news_bot.py:991` (`get_published(link)`) and the
`INSERT OR IGNORE` at `pending_articles_repo.py:582` are two layers,
not duplicates:

- **Guard** = pre-check before any side-effect for the cron path
  (`_fallback_publish`); avoids the orphan-Telegraph-page risk that an
  `IntegrityError` from `move_to_published` would have caused.
- **`INSERT OR IGNORE`** = defense-in-depth for the second caller
  (`hw_review.cmd_publish`, which doesn't go through `_fallback_publish`'s
  guard; preserved per Decision 9 of the manual-review-workflow tech-spec).

These two layers cover disjoint callers; no logic duplication. PASS.

### Shared resources

Tech-spec section "Shared resources: None" (line 67). Verified:

- Guard uses no module-level mutable state.
- Guard introduces no new singletons.
- `pending_repo.get_published` opens its own short-lived sqlite
  connection (`pending_articles_repo.py:_connect()`); same for
  `skip_pending`. No connection sharing, no pool to manage.
- `send_admin_notification` constructs a fresh `Bot(token=...)` per
  call (`news_bot.py:374`); already this way pre-fix.
- No new globals, no new locks, no new files-on-disk, no concurrent
  writers (production has single-writer cron `news_bot.service` per
  code-research §E).

PASS.

### `INSERT OR IGNORE` parameterization (security pre-check)

Bind tuple is positional `(?, ?, ?, ?, ?, ?, ?)` with 7 placeholders;
parameter values from the function arguments + the `SELECT` of the
existing pending row (`title`, `ru_title`, `source_name`, `pub_date`).
No string interpolation, no f-strings inside the SQL literal. SQL-
injection surface unchanged from pre-fix. Detailed security analysis
is the responsibility of Task 7 (security-auditor); this audit only
confirms the parameterization shape is preserved.

PASS.

### Test wiring sanity (cross-check, NOT test-quality audit)

| File | Class / Test | Lines | Status |
|---|---|---|---|
| `tests/test_fallback_publish_paths.py` | `TestIdempotencyGuard` | 472–818 | exists |
| `tests/test_fallback_publish_paths.py` | `test_skip_if_link_already_published_claude_path` | 515 | exists |
| `tests/test_fallback_publish_paths.py` | `test_skip_if_link_already_published_outage_shortcut_path` | 598 | exists |
| `tests/test_fallback_publish_paths.py` | `test_skip_if_link_already_published_no_telegraph_url` | 667 | exists |
| `tests/test_fallback_publish_paths.py` | `test_admin_ping_fires_when_guard_skips` | 734 | exists |
| `tests/test_fallback_publish_paths.py` | `test_guard_continues_when_admin_ping_returns_false` | 776 | exists |
| `tests/test_pending_articles_repo.py` | `test_move_to_published_idempotent_on_duplicate_link` | 623 | exists |
| `tests/test_distributed_schedule_integration.py` | `test_slot_loop_does_not_repost_already_published` | 701 | exists |

All seven tests are present; per-task verification (decisions.md
entries Tasks 3, 4, 5) reports each test passing locally with
pre-commit clean. Test-quality assessment (litmus assertions, mock
correctness, log-marker capture) belongs to Task 8 — not duplicated
here.

PASS (existence + wiring only).

## Findings

No findings.

## Verdict

**PASS — ready for Pre-deploy QA (Task 9).**

No fix-cycle requested for any Wave-1 / Wave-2 task. All Decisions
1, 2, 3, 4, 8 verified line-for-line against the post-Wave-2 file
state; the `INSERT OR IGNORE` keyword change in Decision 5 is the
single-line surgical edit promised in the tech-spec; no shared-
resource concerns; no duplicate logic; logging level + marker tag +
admin-ping prefix all consistent with project patterns
(`patterns.md`).

Hand-off: Task 7 (security-auditor) and Task 8 (test-master) consume
this report as context for parallel audits in the same Wave 3.
