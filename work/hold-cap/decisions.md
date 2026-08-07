# Decisions Log: hold-cap

Standalone change (no user-spec / tech-spec) — plan item **1.2** of
`work/PLAN-2026-08-03.md`, delivered via `/write-code`. Operator chose the
"потолок придержаний" option over "периодический пинг" on 2026-08-04.

---

## Task standalone: bound hold-and-wait so one article cannot block the channel

**Status:** Done
**Commit:** _(pending — not yet committed)_
**Agent:** main agent

**Summary:** `job()` picks the next article with `list_pending()[0]` — always the
head — and an LLM outage HOLDS the row without striking it. A row that always
fails was therefore retried at every slot forever, blocking every article behind
it, and doing so **silently** because `outage_state` stops pinging at
`ping_count >= 3`. New `pending_articles.hold_count` counts consecutive holds;
past `HOLD_CAP` (6) the row is parked for `HOLD_DEFER_HOURS` (24) through the
existing `publish_after` column and the queue moves on. Nothing is struck —
the article returns to the head when the window elapses.

**Deviations:** Three additions came out of review, none in the original scope,
all closing holes this change would otherwise have opened. They are D3–D5 below.

---

### Decisions

**D1. Cap = 6 holds, defer = 24 h.**

6 is two full days of the three fixed slots. Chosen so a *genuine* LLM outage
never trips it: the 2-ping protocol declares a sustained outage after 2 h, well
inside one day, so anything still holding after six slots across two days is
article-specific rather than global. A lower cap (3 = one day) starts deferring
healthy articles during ordinary outages and spreads the queue across days for
no reason. Both values are single constants in `news_bot.py`.

**D2. The parking mechanism is the existing `publish_after`, not a new one.**

Added 2026-07-28 for the dedup soft-flag: a future UTC stamp that
`list_pending`/`count_pending` already filter on and that expires by itself. The
only new thing is `defer_publish`, the first writer to set it on an
**already-staged** row. No new filtering logic, no second concept for "withheld
but not held".

**D3. The counter is CONSECUTIVE, and only the recovery hook makes it so.**

Found by review — the first implementation had a counter that never reset, while
its own docstring promised "in a row". Lifetime-cumulative means an innocent
article banks three holds during a real outage in June, waits in carry-over for
weeks, and crosses the cap on the first bad day in August: deferred, plus an
[E038] blaming it for someone else's failure. That is the same wrong-attribution
class the 2026-06-10 E011 incident cost a day to.

`_maybe_record_recovery` — already the canonical "the LLM answered" hook — now
calls `reset_hold_counts_below(HOLD_CAP)`. Rows **at or above** the cap keep
their count deliberately: that marker is what makes a proven-stuck row yield on
its FIRST hold in the next window instead of blocking for another six slots.

**D4. Deferred rows still buy the day its slots.**

Found by review, and the sharpest failure mode: `job()` computes the slot list
**once per tick** from `count_pending()`, which excludes deferred rows. A tick
that started fully deferred would compute zero slots and skip the whole day —
including the hours after a 24 h window elapsed. So `count_deferred()` is added
to the slot input. Over-allocating is safe: the loop breaks as soon as
`list_pending()` returns empty.

Deliberately NOT folded into `count_pending()`: every other reader of that number
(the `> 50` backlog alarm, «Всего в очереди» in the plan ping) means
"publishable right now", and a deferred row is not.

**D5. A fully-deferred day must not report «новых статей нет».**

`queue_size == 0 and inserted == 0` routes to `[E009]`, which with everything
parked would be a false all-clear. `_deferred_backlog_line` adds «Отложено
(уступили очередь): N» to both `[E008]` and `[E009]` — informational, not a todo,
since deferred rows need no operator decision.

**D6. `[E038]` fires only on a real defer, and lives outside the defer's `try`.**

`defer_publish` returns whether a row was actually updated. A miss means the
review listener deleted the row first, and telling the operator their cancelled
article «вернётся сама» would be a lie. The ping sits in its own `try` for the
mirror reason: nested inside the defer's, a repo failure produced neither the
defer nor the ping — precisely the silent-blocked-queue state the cap exists to
end.

---

**Reviews:**

*Round 1:*
- code-reviewer: 3 major, 7 minor → [code-reviewer-1.json](logs/working/task-standalone/code-reviewer-1.json)
- test-reviewer: 4 major, 4 minor → [test-reviewer-1.json](logs/working/task-standalone/test-reviewer-1.json)

Every major was applied (D3, D4, D5 and the test hardening below). The code
reviewer verified the migration empirically against a simulated live prod DB
(rows present, column dropped, `init_schema` re-run): column added, pre-existing
rows read NULL, `count_pending`/`list_pending` unchanged, re-run idempotent.

**Verification:**

- `python3 -m pytest -q` → **1671 passed, 462 subtests** (1646 before this change)
- `pre-commit` on all changed files → all hooks pass
- Mutation control:

  | Mutation | Result |
  |---|---|
  | cap never fires / cap lowered to 2 | 2 fail each |
  | `COALESCE` dropped from `increment_hold` (legacy NULL rows) | 4 fail |
  | `increment_hold` bumps every row (`OR 1=1`) | 3 fail |
  | counter reset on defer | 3 fail |
  | no reset on recovery | 1 fail |
  | reset also clears proven-stuck rows | 2 fail |
  | slots ignore the deferred backlog | 1 fail |
  | defer window = 0 / defer into the past | 1 fail each |
  | ping sent when nothing was deferred | 2 fail |
  | alert swaps hold count and defer hours | 2 fail |
  | cause dropped from the `'held'` return | 1 fail |

- Two assertions were found worthless by mutation and rewritten, both the same
  defect: **asserting text that also appears in a static string.** The [E038]
  test matched `'402'`, which the builder's own advice line contains, so it
  passed against an implementation that never received the cause. The
  defer-timestamp test derived its expected value from `HOLD_DEFER_HOURS`, so
  setting that constant to 0 — which makes the whole feature a no-op in
  production — changed both sides and still passed. Now: a sentinel string that
  cannot occur in static text, and a comparison against the frozen clock rather
  than against the constant.

- Not verified on production. Reaching the cap requires six consecutive holds of
  the same row; there is no safe way to induce that on the live queue.

---

### Left open

1. **Steady state still pings up to 3× a day.** A permanently stuck row yields on
   its first hold each window, so with three slots it can emit three [E038]s in a
   day. Tolerable now (the situation is meant to be rare and the operator wants
   to know), but if it becomes noise the fix is the same shape as
   `outage_state`'s ping cadence — rate-limit per link.
2. **`_DistribLoopBase` mocks the whole `datetime` module**, so any test in that
   file that reaches `job()`'s tail hits `strptime` returning a MagicMock in the
   dry-spell check (caught and logged). Pre-existing debt; it means these classes
   cannot grow end-of-tick assertions without fixing the fixture first.
