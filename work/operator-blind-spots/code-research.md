# Code research — operator-blind-spots

Researched 2026-08-17 by reading the code directly (no subagent — the session
forbids spawning agents unasked). Two halves, one spec:

1. **Per-source silence alert** — a source offered fresh feed entries but none
   reached the queue for 2 days.
2. **[E036] daily re-send** — one summary message a day carrying buttons for
   every held article, so a lost hold ping stops stranding its article.

---

## 1. Source attribution already exists — no source-module changes

This corrects the estimate given to the operator before the code was read.

| What | Where |
|---|---|
| `_resolve_source_name(link)` → `autoevolution` / `orangetrack` / `lamley` / `t-hunted` / `mattel` / `other` | news_bot.py:3158 |
| `NETLOC_TO_SOURCE` map | news_bot.py:2841-2906 |
| `entry['source_name']` set at RSS fetch time | news_bot.py:3805 |
| Established fallback idiom `entry.get('source_name') or _resolve_source_name(link)` | news_bot.py:4386, :4545 |

`_resolve_source_name` never raises (bare `urlparse` in a try, `'other'` on any
miss), so per-source attribution is a pure function of the link and is available
at **every** point in the intake loop. The four source modules
(`autoevolution_source.py`, `lamley_source.py`, `t_hunted_source.py`,
`orangetrack_source.py`) need **no change at all**.

Caveat to verify in the tech-spec: `source_name` is stamped inside
`_fetch_rss_entries` (feeds.json sources), while orangetrack arrives via its own
`_fetch_orangetrack_entries`. The `or _resolve_source_name(link)` fallback covers
it, so use that idiom rather than trusting `entry['source_name']` alone.

## 2. The funnel is per-tick and aggregate

`funnel` is a flat dict of 11 plain ints built at news_bot.py:4082 and
incremented through the b3 loop (`dropped_no_article` :4143/:4149,
`dropped_checklist` :4207, `dropped_promo` :4237, `dropped_genre` :4330,
`dropped_dedup_block` :4396, `dedup_degraded` :4512, `held_for_review` :4580,
`staged` :4628). It is emitted as one `[funnel]` log line (:4633) and rendered
into `[E008]`/`[E009]` by `admin_alerts._format_funnel`.

Two structural facts that shape the design:

- **It is per-tick.** The 2-day condition is inherently cross-tick, so the funnel
  alone cannot answer it. Something must persist.
- **It is aggregate.** Adding a parallel per-source breakdown is the actual work
  of sub-feature 1 — the counters are already at the right places in the loop,
  they just need a source key alongside.

## 3. Cross-tick state: `bot_state`, and the restart trap

`bot_state (key TEXT PRIMARY KEY, value TEXT)` is the established home for
cross-tick state and needs no migration (repo `init_schema`). Existing keys are
catalogued in architecture.md § Data Model.

**The single most important implementation constraint for BOTH halves:**
`main()` calls `job()` immediately on startup (news_bot.py:5113), and prod
restarts are routine — four on 2026-08-13 alone. Any "once a day" alert that
does not persist a sent-marker will fire once per restart instead.

There is a working precedent and a cautionary counterexample, and they sit in the
same codebase:

- **Precedent (do this):** `is_hold_cap_ping_rate_limited(window_hours=6)` /
  `mark_hold_cap_pinged()` (pending_articles_repo.py:686, :721) — a UTC timestamp
  in `bot_state`, read before sending and written after. Note its documented
  fail-OPEN stance: corrupt/missing bookkeeping sends the ping anyway, because
  silencing an alert because its own bookkeeping broke is the worse failure.
  Both new alerts should inherit that stance.
- **Counterexample (do not copy):** the `[E017]` dry-spell check
  (news_bot.py:4967-4983) has **no rate limit at all**. During a real dry spell
  it fires on every `job()` — i.e. once per restart. It has not bitten visibly
  because dry spells are rare, but it is exactly the shape to avoid, and the
  `[E036]` daily summary would be far more visible if it repeated per restart.
  Worth flagging in the tech-spec as an adjacent defect (out of scope to fix
  here unless the operator wants it).

The same per-calendar-day idiom now used by `record_fetch_failure`
(`date(last_failed_at) < date('now')`, repo :606, shipped 2026-08-13) is the
other proven option and reads more naturally as "once per day" than a 24 h
window.

## 4. [E036] send path, and what a summary message needs

Current send site: news_bot.py:4585-4620, inside the staging loop. Sequence is
mint token → `put_review_token(kind=REVIEW_TOKEN_KIND_HOLD)` →
`build_hold_review_keyboard(token)` → `send_admin_notification(..., reply_markup=kb)`,
all inside one try so a storage fault degrades to a failed ping rather than
breaking intake.

Pieces the daily summary reuses as-is:

| Piece | Where |
|---|---|
| `list_held()` — held rows oldest-first, exact complement of `list_pending` | pending_articles_repo.py:1000 |
| `alert_held_for_review(link, title, markers, reason=…, buttons_enabled=…)` | admin_alerts.py:1057 |
| `build_hold_review_keyboard(token)` → `hd:a:<token>` / `hd:r:<token>` | admin_alerts.py:1113 |
| token store `put_review_token` / `get_review_token` / `delete_review_token` | repo :1633, :1666, :1708 |
| fail-closed gate `_review_listener_enabled()` (audit SEC-A8-1) | news_bot.py |

What is genuinely new:

- **A summary alert builder + a multi-row keyboard.** One row per held article,
  two buttons each. `callback_data` stays well inside Telegram's 64-byte limit
  (`hd:a:` + `secrets.token_urlsafe(9)` ≈ 17 bytes), so no new encoding is
  needed. Needs a row cap for the case of many held articles — Telegram accepts
  large keyboards but an unbounded one is a latent failure.
- **Token reuse, not re-mint.** `get_review_token` is keyed by token, so finding
  "the live token for this link" needs either a reverse scan of
  `review_token:*` in `bot_state` or a new accessor. Reusing keeps the original
  message's buttons working and creates no orphan tokens; minting a fresh token
  each day would leave a trail of dead keys with no janitor (the current design
  tolerates stale tokens precisely because they are few).
- **A free alert code.** In use: E001-E028, E030-E038. **E029 is unused** (a gap
  in the sequence — confirm it is genuinely free and not merely retired), then
  E039/E040. Two new codes are needed, one per half.

## 5. Interaction with the fetch-retry cap (shipped 2026-08-13)

`FETCH_RETRY_CAP = 3` retires a persistently unfetchable link by writing it to
`processed_news` (news_bot.py:4147-4200). Once retired, the link is filtered out
by the b2 not-seen-before filter — so it stops counting as a "fresh entry".

Ordering therefore matters and is tight: the silence alarm fires on **day 2**,
the cap retires evidence on **day 3**. It works, but the margin is one day and
nothing currently pins it. This deserves an explicit regression test — it is the
kind of coupling that a later tuning change ("let's make the cap 2") would break
silently, turning the alarm permanently mute.

## 6. Tests — where this work lands

- `tests/test_integration.py` — job()-level harness with stubbed sources; the
  natural home for "reproduce the August outage and assert the alarm fires".
- `tests/test_admin_alerts.py` — message builders (pure functions).
- `tests/test_pending_articles_repo.py` — new repo accessors and their
  restart/once-per-day semantics.
- `tests/test_job_prep_phase.py`, `tests/test_job_distributed_publish.py` — the
  intake and publish halves of `job()` respectively.

Suite baseline at time of research: **1960 passed, 506 subtests** (full run
green, 2026-08-17).

Project test convention worth honouring (memory + patterns): one behaviour = one
test function, a set of inputs = `parametrize`. Do not fragment.

## 7. Risks the code surfaces

1. **Noise defeats the feature.** Both halves add admin pings; the operator's own
   earlier decision (architecture.md:420) rejected reminders for this reason.
   The chosen shapes (one summary a day; ≤4 source pings a day) bound it, but the
   restart trap in §3 is what would break the bound in practice.
2. **`architecture.md:420 must be rewritten` when this lands.** It currently
   documents "no re-mint path, reminders rejected" as settled. Leaving it would
   invite a future reader to undo the feature.
3. **Fail-open vs fail-closed pull in opposite directions here.** The alerting
   side must fail open (a broken counter must not silence the alarm); the
   button-minting side must fail closed (`_review_listener_enabled()`, audit
   SEC-A8-1 — an instance that cannot listen must not render buttons). The daily
   summary touches both, so the tech-spec needs to state which rule governs which
   branch.
4. **`_format_funnel` is operator-facing text.** Extending it per-source risks
   making the daily ping unreadable. The per-source breakdown probably belongs in
   the new alert, not in the existing `[E008]` line.
