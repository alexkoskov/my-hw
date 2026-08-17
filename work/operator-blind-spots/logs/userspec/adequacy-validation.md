# Adequacy validation — operator-blind-spots

Scope: is the proposed solution reasonable, buildable on this stack, right-sized,
neither over- nor under-engineered. Document quality is out of scope.

Verdict: **changes_required** — 3 critical, 5 major, 3 minor.
Worst category: **underengineering**.

Code read directly: `news_bot.py`, `admin_alerts.py`, `pending_articles_repo.py`,
`architecture.md`, `SESSION-2026-08-13.md`. Every claim below carries the line it
was verified against.

---

## The five challenges, answered

### 1. The reversal of architecture.md:420 — sound

The reversal holds up. Three reasons:

- The rejected thing had two halves — *reminders* and *timeouts*. The dangerous
  half is the timeout (auto-publish after N days), and the spec explicitly keeps
  it rejected: «нет ответа — не публикуем» is preserved and the summary publishes
  nothing by itself.
- The operator re-decided with the prior rejection restated verbatim in the
  question (interview Q1b). That is a decision, not an oversight.
- The existing mitigation is real but insufficient: «На утверждении: N»
  (`admin_alerts.py:312-327`) reports a *count*. It cannot name the article or
  restore its button. The incremental value — identity + a working button — is
  genuinely new.

One correction: the spec's stated *reason* for the reversal («тогда напоминания
понимались как пинг на каждую статью») is a reconstruction. `architecture.md:420`
says only that reminders/timeouts were rejected; it does not record the shape.
Lead with the verifiable 11 Aug incident instead. (Finding 11, minor.)

We are **not** rebuilding something removed for good reason — but see challenge
below on what the daily nag does to the meaning of silence (Finding 6).

### 2. Would an aggregate alarm have caught August? — mostly yes, and that matters

`SESSION-2026-08-13.md` §2 already considered and rejected an aggregate counter
(«consecutive days that published nothing») on the grounds that it is the same
number as the dry-spell gap. Fair. But the session missed what its own fix did:

`DRY_SPELL_ALERT_DAYS` was lowered 3 → 2 (`news_bot.py:258`) *specifically* so
the 11 Aug tick would fire. Add item 3 of this spec ([E017] restart marker) and
an August-shaped blackout now pings on **day 2** — the same day the new
per-source alarm would.

So the per-source machinery is **not** justified by the August incident. Its real
justification is the *partial* outage: one source dies while the others keep the
channel publishing, so the dry-spell gap keeps resetting and [E017] never fires.
That is visible in the session's own table — the 12 Aug deferred article reset the
gap to 0 while autoevolution was still blocked. Real gap, never measured, never
stated in the spec. (Finding 4, major.)

### 3. Day-2 alarm vs day-3 fetch cap — safe, and safer than the spec says

Verified:

- `record_fetch_failure` increments **at most once per calendar day**
  (`pending_articles_repo.py:609-643`), so retirement lands on the third distinct
  failing day.
- Retirement happens inside the b3 loop **after** `new_entries` was computed
  (`news_bot.py:4121-4132` then `:4167-4184`), so a link still counts as fresh on
  the very tick that retires it.

Consequence: `FETCH_RETRY_CAP = 2` would leave the day-2 alarm intact, and even
`= 1` would not mute it while the source keeps publishing new links (autoevolution
posts ~15-20/day — the monitored case). The alarm goes mute only if the source
stops publishing entirely *and* all its links are retired — already classified as
not-an-alarm.

Risk 3's «запас всего сутки» overstates the coupling and points the regression
test at the wrong invariant. Pin «an entry retired on this tick still counts
toward this tick's freshness», not the threshold value. (Finding 9, minor.)

### 4. Over-engineering — per-source state is justified, but not as four counters

The 2-day condition is inherently cross-tick, so something must persist, and
`bot_state` needs no DDL. Justified.

What is not examined: «how long since source X delivered» is derivable from data
that already exists — `pending_articles.source_name` + `fetched_at`
(`architecture.md:392`) and `published_articles.published_at` resolved through
`_resolve_source_name(link)` (`news_bot.py:3158`). Under that framing the only
thing that *must* be stored is the once-per-day send marker, which items 2 and 3
need regardless. Without a line in the spec, the tech-spec will default to four
per-source counters plus four per-source markers — eight keys, each with its own
restart-safety and per-calendar-day semantics. (Finding 10, minor.)

The per-source funnel *breakdown* itself is cheap (eight one-line additions
alongside existing `funnel[...] += 1` sites) and earns its place: naming the
collapse step is what distinguishes «источник заблокирован» from «сломался
фильтр», and those need different responses.

### 5. Token reuse — the accessor does not exist, and the fallback is undefined

Confirmed by reading `pending_articles_repo.py:1633-1723`: **every** accessor is
keyed by token. `put_review_token` / `get_review_token` / `get_review_token_link`
/ `delete_review_token`. The link lives in the *value* as `<kind>|<link>`. There
is no link → token lookup.

Reuse is still the right call, and the scan is cheap (`bot_state` is small; exact
match on `'hold|' + link`). But it is unbuilt work the spec presents as free, and
it must filter on kind — a `dedup` token for the same link can legitimately
coexist, and cross-kind redemption is the exact SEC-CG-2 bug the kind field
exists to prevent (`architecture.md:418-419`). (Finding 5, major.)

Worse: the spec defines no behaviour when a held row has **no** token. Reachable
two ways — held while `_review_listener_enabled()` was False, or
`put_review_token` raised inside the best-effort try at `news_bot.py:4585-4620`.
Those are the stranded articles the feature exists to rescue, and reuse-only
leaves them button-less forever. (Finding 2, **critical**.)

---

## The finding the challenges did not ask for, and the one that blocks

### The listener strips the whole keyboard on any press

`_review_edit_message` (`news_bot.py:1007-1024`):

```
await bot.edit_message_text(chat_id=…, message_id=…, text=…, reply_markup=None)
```

and `_handle_review_update` (`news_bot.py:1105-1112`) appends a single status line
to the whole message: `original_text + "\n\n" + status_text`.

For [E036] — one article, one keyboard — that is correct. For a summary of five
held articles: the first press deletes the other four articles' buttons, and the
appended «✅ Одобрено — выйдет в ближайший слот» names no article.

The operator can therefore resolve **one article per day**. A feature whose whole
purpose is to unstick stranded articles unsticks them at one per day, and two of
the spec's acceptance criteria cannot both hold as written. (Finding 1,
**critical**.) This needs a user-spec-level decision, because it changes what the
operator sees.

### The summary is unbounded against a hard limit

- `list_held()` has no LIMIT (`pending_articles_repo.py:1000-1020`).
- Held rows are never evicted — `list_pending_for_eviction` carries
  `WHERE hold_reason IS NULL` (`repo:976-990`). Only an operator press removes one.
- `send_admin_notification` has no length guard (`news_bot.py:601-665`). Past
  Telegram's 4096 chars: `TelegramError`, 3 retries, `return False`, one log line.

A terse row (title + link) is ~140-150 chars → ~25-28 articles kills the message.
AC «ровно одно сводное сообщение — сколько бы статей ни висело» actively forbids
the fix. Failure is total and silent, at the moment the feature matters most.
(Finding 3, **critical**.)

### Silence stops being a free answer

`resolve_hold_callback`'s contract says it plainly (`news_bot.py:849-852`):
«Doing NOTHING is a supported outcome». Held rows are never evicted, so after this
feature an article declined by silence — the documented way to decline — is listed
back at the operator every day for the life of the database. «Ограничения» claims
the rule is unchanged; behaviourally its cost changed. (Finding 6, major.)

### «Дошла до очереди публикации» is ambiguous

A held article is inserted and counted as staged (`news_bot.py:4570-4580`) but
never becomes publishable. If held counts as «дошла», a content-gate regression
that holds every article from a source reads as perfectly healthy — a silent
source with a healthy counter, precisely this feature's target failure class.
Unstated, the tech-spec will reach for `staged`, which is the unsafe reading.
(Finding 7, major.)

---

## Sizing

Declared M covers three items. Items 2 and 3 reuse existing pieces; item 1 is the
bulk (per-source breakdown across eight increment sites, cross-tick state, a new
alert, integration tests reproducing a two-day outage) and ships independently.

Shipping item 3 **first** is what makes challenge 2 answerable: it closes the
total-blackout case, so whatever remains uncovered afterwards is the honest
justification for item 1. Nothing in the code forces the bundle — the only shared
piece is the once-per-day send-marker idiom, which increment 1 establishes and
increment 2 reuses. (Finding 8, major.)

---

## Full findings

Machine-readable: `logs/userspec/adequacy-review.json`.

| # | Severity | Category | Issue |
|---|----------|----------|-------|
| 1 | critical | feasibility | Listener's `reply_markup=None` edit kills all other buttons in a multi-row summary |
| 2 | critical | underengineering | No defined behaviour for a held row with no live token — the population being rescued |
| 3 | critical | underengineering | Summary unbounded vs Telegram's 4096-char cap; AC forbids a cap; failure is silent |
| 4 | major | better_alternative | August incident already covered by [E017]@2 + item 3; real gap (partial outage) unstated |
| 5 | major | feasibility | No link → token accessor exists; reuse needs a new kind-filtered lookup |
| 6 | major | underengineering | «Нет ответа = не публикуем» becomes a daily nag; end state undefined |
| 7 | major | underengineering | «Дошла до очереди» ambiguous for held articles; unsafe default likely |
| 8 | major | sizing | Three independent items in one M spec; item 3 should ship first |
| 9 | minor | feasibility | Risk 3 misdiagnoses the cap coupling; test would pin the wrong invariant |
| 10 | minor | better_alternative | Per-source counters may be derivable from existing tables; only the marker must persist |
| 11 | minor | better_alternative | Reversal rationale asserts what architecture.md:420 meant; record doesn't support it |
