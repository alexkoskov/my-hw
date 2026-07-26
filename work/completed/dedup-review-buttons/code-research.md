# Code Research: dedup-review-buttons

> Feature goal: attach Telegram inline buttons UNDER the `[E014] «Похож на дубль»` admin
> alert. «Не публиковать» drops the suspected-duplicate article from the pending queue
> before its publish slot; «Оставить» dismisses. This is the **first** time the bot will
> RECEIVE anything from Telegram — today it only SENDS.
>
> Research date: 2026-07-22. All paths under `/workspaces/debian-2/my-hw/`.

**Confirmed baseline — the bot never receives today.** A tree-wide grep for
`getUpdates`, `CallbackQueryHandler`, `CommandHandler`, `MessageHandler`, `add_handler`,
`Application`, `Updater`, `run_polling`, `callback_query`, `answer_callback`, `from_user`
returns **zero** production hits (the only "Dispatcher" matches are the LLM-transcreation
dispatcher, unrelated to Telegram). Every Telegram interaction is an outbound
`Bot(...).send_message(...)` wrapped in `asyncio.run(...)`.

---

## 1. Where E014 is built and sent

### Builder — `admin_alerts.alert_cross_source_dupe`
`admin_alerts.py:702-749`. Pure string formatter (no I/O, no rate-limit).

```python
def alert_cross_source_dupe(
    new_link: str,
    existing_link: str,
    new_source: str,
    existing_source: str,
    overlap_pct: Optional[int] = None,
    n_matches: Optional[int] = None,
    n_total: Optional[int] = None,
    models: Optional[List[str]] = None,
    *,
    pairs: Optional[List[str]] = None,
) -> str:
```

- Returns the `[E014] 🤔 Похож на дубль` text block (template at `admin_alerts.py:735-749`).
- **The new article link is embedded in the body** as `Новая статья:\n{new_link}` (`:737`).
  This is the identity we must carry into the callback.
- Anchor comment `admin_alerts.py:714-715`: *"Подстрока 'Похож на дубль' — substring-якорь
  … Не менять."* — do not alter the header/anchor text.

### Call site — `news_bot.job()` dedup gate (`flag` branch)
`news_bot.py:2359-2397`. Only **one** call site builds E014. Inside `job()`'s per-entry loop,
after fetch + checklist, before row assembly (Decision 14):

```python
if decision == 'flag':
    alerted = not pending_repo.is_pair_rate_limited(dedup_conn, link, match['link'])
    logger.info("[E014] Cross-source soft-flag %s; matched %s ...", ...)   # :2367
    if alerted:
        try:
            send_admin_notification(
                admin_alerts.alert_cross_source_dupe(
                    new_link=link,
                    existing_link=match['link'],
                    new_source=new_source,
                    existing_source=match['source_name'],
                    overlap_pct=match['overlap_pct'],
                    n_matches=match['n_matches'],
                    n_total=match['n_total'],
                    models=match['models'],
                    pairs=match.get('pairs'),
                )
            )                                                              # :2376-2388
        except Exception as notify_err:
            logger.error("Failed to send E014 notification: %s", notify_err)
        pending_repo.mark_pair_pinged(dedup_conn, link, match['link'])     # :2394
        dedup_conn.commit()
```

- `link` here is the new article's URL — the same value that becomes the `pending_articles`
  PK a few lines later when the row is inserted. **This is the article identity for the
  «Не публиковать» action.**
- E014 fires **per-pair rate-limited** (7-day window, AC5): `is_pair_rate_limited` /
  `mark_pair_pinged` (`pending_articles_repo.py:931`, `:959`). The ping is NOT sent every
  tick — relevant if we ever want a button re-shown.
- Sibling branches: `block` → E015 `alert_cross_source_blocked` (`news_bot.py:2347-2352`),
  degraded → E016 `alert_dedup_degraded` (`news_bot.py:2416-2420`). **Only E014 gets buttons.**

### The send path — `news_bot.send_admin_notification`
`news_bot.py:469-530`. Signature:

```python
def send_admin_notification(message, *, max_attempts=ADMIN_NOTIFICATION_MAX_ATTEMPTS):
```

Body highlights:
- Guard: `if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID: return False` (`:483`).
- `safe_message = _redact_text(message)` (`:486`) then optional `[INSTANCE_LABEL]` prefix (`:489-490`).
- Inner coroutine (`:492-504`):
  ```python
  async def _send():
      bot = Bot(token=TELEGRAM_BOT_TOKEN)              # :493 — new Bot per call
      await bot.send_message(
          chat_id=TELEGRAM_ADMIN_ID,
          text=safe_message,
      )                                                # :501-504 — parse_mode=None (plain text)
  ```
- Driven by `asyncio.run(_send())` inside a bounded retry loop (`:507-525`), `TelegramError`
  only, backoff 1s/2s, `ADMIN_NOTIFICATION_MAX_ATTEMPTS = 3` (`:466`).
- **`send_message` currently passes NO `reply_markup`.** The InlineKeyboardMarkup must be
  threaded through here. Cleanest shape: add a keyword param
  `def send_admin_notification(message, *, reply_markup=None, max_attempts=...)` and forward
  it into `bot.send_message(..., reply_markup=reply_markup)`. See §8 for why this keeps the
  existing test convention intact (tests read `call.args[0]` = the text).
- Note `parse_mode=None` is deliberate (`:494-500`, anti-spoofing / entity-parse failures).
  Inline buttons do not need parse_mode, so no conflict.

The imports already present: `from telegram import Bot, LinkPreviewOptions` (`news_bot.py:46`),
`from telegram.error import TelegramError` (`:47`). **`InlineKeyboardMarkup` /
`InlineKeyboardButton` are NOT yet imported** — add to that line.

---

## 2. How the article is identified in the queue

### PK is the URL — confirmed
`pending_articles_repo.py:60-83`, table DDL:
```sql
CREATE TABLE IF NOT EXISTS pending_articles (
    link              TEXT PRIMARY KEY,    -- :62
    source_name       TEXT NOT NULL,
    ...
    pub_date          TEXT
)
```
`link` (the full article URL) is the PK. `published_articles` (`:86`) and `failed_articles`
(`:100`) are keyed the same way.

### 64-byte callback_data problem
Telegram limits `callback_data` to 64 bytes. Article URLs (e.g.
`https://www.autoevolution.com/news/...long-slug...`) routinely exceed this, so **the raw
`link` cannot be the callback payload.**

- **No integer surrogate key exists.** The table has no explicit `id`/`rowid` column; SQLite
  auto-`rowid` exists but is unstable (VACUUM/reinsert can renumber) and is never surfaced by
  the repo — `list_pending` / `get_pending` select `*` and never read `rowid`. A tree grep for
  `rowid` returns zero hits in production code.
- **No existing short-id / token-map pattern.** There is a `bot_state` key/value table
  (`pending_articles_repo.py:123-128`, `key TEXT PRIMARY KEY, value TEXT`) currently used only
  by the outage state machine — a candidate home for a token→link map if we go that route.
- **Recommended for tech-spec:** generate a short token (e.g. first 16 hex of
  `sha256(link)`) at E014-send time, and resolve token→link in the callback. Two options to
  persist the mapping: (a) a small `dedup_review_tokens(token, link, created_at)` table, or
  (b) reuse `bot_state` with a `dedupbtn:<token>` key. A pure `sha256(link)[:16]` with a
  reverse lookup over `list_pending()` links at callback time (hash each pending link, match)
  avoids new storage entirely but is O(queue size) — queue is small (single-digit rows
  typically), so this is viable and stateless. Decision deferred to tech-spec.

---

## 3. How to remove/cancel a pending row without publishing

### `pending_articles_repo.skip_pending`
`pending_articles_repo.py:727-757`. **This is exactly what «Не публиковать» calls.**

```python
def skip_pending(link: str) -> None:
    """Write the link to processed_news (dedup) and DELETE from pending.
    NO write to published_articles — skip is not a publish."""
```

Transaction body (`:733-757`):
1. `SELECT title, pub_date FROM pending_articles WHERE link=?` — if `None`, **early-return**
   (idempotent: already gone / already published). `:735-740`
2. `INSERT OR IGNORE INTO processed_news (link, title, pub_date) VALUES (?, ?, ?)` — records
   the link in the dedup ledger so it can never be re-fetched/re-queued. `:743-747`
3. `DELETE FROM pending_articles WHERE link=?` — drops it from the queue. `:748-751`
4. `conn.commit()`; `except: conn.rollback(); raise`; `finally: conn.close()`.

Already used by the idempotency guard (`news_bot.py:1595`) to clean zombie rows, so it is a
proven, self-contained call. The `INSERT OR IGNORE INTO processed_news` step is a bonus
belt-and-suspenders: even if a re-fetch raced, dedup would drop the article.

**Connection caveat:** `skip_pending` opens via the module `_connect()` (bare connect, see §5)
— **no `busy_timeout`.** If the callback runs on a second thread/loop while `job()`'s slot
loop holds a write lock, the `DELETE` can raise `sqlite3.OperationalError: database is locked`
immediately. See §5 Risks.

«Оставить» performs **no DB write** — it only edits/answers the callback (remove the buttons,
`answerCallbackQuery`).

---

## 4. The publish loop & idempotency guard

### Idempotency guard — `_fallback_publish`
`news_bot.py:1548-1601`. Top-of-function guard checks the **published** table, not pending:
```python
link = row['link']                                   # :1574
existing = pending_repo.get_published(link)          # :1581
if existing is not None:
    logger.info("[idempotency-guard] ... already in published_articles ...")
    send_admin_notification(alert_duplicate_publish_skipped(link))  # :1588
    pending_repo.skip_pending(link)                  # :1595 — cleans the zombie
    ... return True
```
This guards **double-publish**, not cancellation. It does **not** re-check whether the row is
still in `pending_articles`.

### Slot publish loop — inside `job()`
`news_bot.py:2564-2590`. Per slot:
```python
for idx, slot in enumerate(slots, start=1):
    if slot > window_end_dt: break                   # :2566 window-end insurance
    wait_seconds = max(0.0, (slot - datetime.now(MSK_TZ)).total_seconds())
    if wait_seconds > 0:
        time.sleep(wait_seconds)                     # :2573-2575 — MAIN THREAD BLOCKS between slots
    rows = pending_repo.list_pending()               # :2577 — RE-READ every slot
    if not rows: break                               # :2578 — empty queue → stop
    row = rows[0]                                     # :2584 — oldest pending row
    link = row.get('link')
    outcome, err = _publish_with_retries(row, idx, len(slots))  # :2590
```

**Where a "was this row cancelled?" check goes — and whether it's even needed:**
- The loop calls `pending_repo.list_pending()` **fresh at the top of every slot** (`:2577`)
  and always publishes `rows[0]`. `list_pending` (`pending_articles_repo.py:432-462`) is a
  `SELECT * FROM pending_articles ...` — a deleted row simply isn't returned.
- **Therefore removing the row via `skip_pending` before its slot fires is sufficient** to
  prevent publication — no extra guard is required in the loop. A row cancelled between slot N
  and slot N+1 is gone from the next `list_pending()`.
- The one residual gap is the in-flight race (operator taps «Не публиковать» **during** the
  ~seconds `_publish_with_retries` is already running for that exact `rows[0]`). The in-memory
  `row` dict was captured at `:2584` and won't see the delete. See §Risks. The idempotency
  guard (`get_published`) would NOT catch this (it checks published, not a cancel flag).

**`time.sleep` confirmed:** slot waits use `time.sleep(wait_seconds)` (`:2575`) and retry
waits use `time.sleep(PUBLISH_RETRY_DELAY_SECONDS)` (`_publish_with_retries`, `:2148`). The
main thread is fully blocked while sleeping — a getUpdates poller cannot share it (§5).

---

## 5. Process / event-loop model

### How the bot runs
`news_bot.main()` (`news_bot.py:2741-2829`):
1. Startup health checks (Claude probe, TZ, prod-DB guard).
2. `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` (`:2820`).
3. `job()` runs **immediately once** for first-boot population (`:2824`).
4. Keep-alive loop (`:2827-2829`):
   ```python
   while True:
       schedule.run_pending()
       time.sleep(60)
   ```
- `import schedule` at `news_bot.py:45`; `schedule==1.2.1` in `requirements.txt` (needs pytz,
  not zoneinfo — `main()` docstring `:2756-2758`).
- Entry: `if __name__ == "__main__": _acquire_singleton_lock(); main()` (`news_bot.py:2878-2880`).
  Kernel `flock` on `.news_bot.lock` refuses a second concurrent start **per deploy dir**
  (`news_bot.py:2841-2875`).
- `job()` itself blocks for the whole publish window (10:00 → last slot, up to ~19:30 МСК)
  because it `time.sleep`s between slots (§4). So the single main thread is unavailable for
  polling for long stretches.

### python-telegram-bot version
`requirements.txt`: **`python-telegram-bot==21.10`** — a v20+ **async** release. Every send
today is `asyncio.run(coro)` (`news_bot.py:509`, `:1442`) creating a fresh event loop per
call. There is **no long-lived `Application`, no `Updater`, no dispatcher** in the codebase.

### What it would take to run getUpdates concurrently with the blocking loop
- **A background thread with its own asyncio event loop** is the natural fit. Options:
  - `telegram.ext.Application` with `run_polling()` — but `run_polling` wants to own the main
    thread / signal handlers; running it in a worker thread needs `Application.initialize()` /
    `updater.start_polling()` with manual loop management, or
  - a hand-rolled loop calling `bot.get_updates(offset=...)` in a thread that runs its own
    `asyncio` loop (matches the existing "fresh loop per call" style but long-lived).
- The poller thread would call `pending_repo.skip_pending(link)` — a **second SQLite writer**
  against the same `DB_FILE` while the main thread's `job()` may be writing. This is the
  concurrency concern.
- **Alternative: a separate process** (its own `.news_bot.lock` name so the singleton lock
  doesn't fire). Cleaner isolation but a second deploy unit / container command.

### SQLite concurrency helpers — IMPORTANT NUANCE
The task brief said "busy_timeout / BEGIN IMMEDIATE already present." **They exist, but NOT
on the pending-articles path:**
- `pending_articles_repo._connect()` (`pending_articles_repo.py:174-177`) is a **bare**
  `sqlite3.connect(news_bot.DB_FILE)` — **no `PRAGMA busy_timeout`, no WAL, default
  `isolation_level`.** `skip_pending` and `list_pending` use this connection. So a second
  concurrent writer can hit `database is locked` **immediately** (0ms wait).
- The busy_timeout / BEGIN IMMEDIATE pattern lives in **`outage_state.py`**, a different
  module: `outage_state._connect()` sets `PRAGMA busy_timeout = 5000` (`outage_state.py:114`)
  and its writers wrap read-then-write in `BEGIN IMMEDIATE` (`outage_state.py:140`, `:266`,
  `:372`, `:442`; Decision 16). Tree grep confirms these tokens appear **only** in
  `outage_state.py` in production code.
- **Implication for tech-spec:** to make the callback's `skip_pending` safe against the
  concurrent `job()` writer, either (a) add `PRAGMA busy_timeout` to
  `pending_articles_repo._connect()` (affects all repo callers — small blast radius, matches
  the outage_state precedent), or (b) give `skip_pending` its own busy_timeout connection.
  The `outage_state` module is the copy-paste reference. This is a genuine gap the brief's
  premise glossed over.

---

## 6. Shared bot token constraint

`deployment.md:105`: *"The bot Telegram TOKEN is shared (one bot account posts to both
channels)."* Prod = Docker on Moscow `45.90.216.165` (branch `main`); test = systemd on NL
`148.135.207.54` (branch `dev`) — `deployment.md:96-103`.

**getUpdates long-polling is exclusive per token.** If both prod and test poll the same token,
Telegram returns `409 Conflict` to one of them. Only ONE instance may poll.

### Existing gate flag: `INSTANCE_LABEL`
- `INSTANCE_LABEL = os.getenv('INSTANCE_LABEL', '').strip()` (`news_bot.py:105`). Values
  `prod` / `test`, set ONCE by hand on each server, NOT rewritten by the deploy workflow
  (`deployment.md:108`, `:111`).
- Already used to gate behaviour: prod-DB guard runs only when `INSTANCE_LABEL != 'prod'`
  returns early (`news_bot.py:745`), and it prefixes every admin ping (`:489-490`).
- **This is the natural gate for "which instance polls":** e.g. only start the getUpdates
  poller when `INSTANCE_LABEL == 'prod'` (or a new dedicated flag like
  `DEDUP_REVIEW_POLL_ENABLED`). Test instance would then still SEND E014 with buttons but not
  poll — meaning buttons under the **test** channel's E014 would be inert unless we also point
  the poller there. Decision for tech-spec: buttons are operator-facing on the **admin chat**
  (shared `TELEGRAM_ADMIN_ID`), not the public channel — so the single poller must be the
  instance whose `skip_pending` targets the right `news.db`. Since prod and test have
  **independent `news.db` files** (`deployment.md:109`), a callback can only cancel a row in
  the DB of the instance that polls. This must be reconciled in the tech-spec (likely:
  prod-only polling; test verifies build/format without live buttons).

---

## 7. Admin authorization

`TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '@sunny413x')` — `news_bot.py:102`.

- Read once at import as a **string**. Default is the literal `@sunny413x` (a **username**,
  not a numeric id). The dev `.env` here sets it to a **numeric** value; prod/test `.env` are
  hand-managed (`deployment.md:111`), so the actual type is per-deployment.
- Used today only as `chat_id` in `bot.send_message(chat_id=TELEGRAM_ADMIN_ID, ...)`
  (`news_bot.py:502`, `:1588` idempotency ping). Telegram's `chat_id` accepts **either** a
  numeric id **or** a `@username` string, which is why the ambiguous type has never mattered.
- It also appears in the log-redaction env list (`news_bot.py:213`) so its value is scrubbed
  from logs.

**Authorization for callbacks:** `update.callback_query.from_user.id` is always a **numeric
`int`**. To verify `from_user.id == TELEGRAM_ADMIN_ID` we need a numeric admin id.
- If `TELEGRAM_ADMIN_ID` is the `@username` default (or any non-numeric value), the equality
  check **can never match** and every callback would be rejected. **Gotcha to document.**
- Tech-spec must either (a) require `TELEGRAM_ADMIN_ID` to be numeric in the polling
  instance's `.env` and compare `str(from_user.id) == str(TELEGRAM_ADMIN_ID)` after an
  `int()`-parse guard, or (b) introduce a dedicated `TELEGRAM_ADMIN_USER_ID` numeric env var
  for the auth check while keeping `TELEGRAM_ADMIN_ID` for the send `chat_id`. Any callback
  whose `from_user.id` fails the check must be `answerCallbackQuery`'d with a rejection and
  otherwise ignored (no `skip_pending`).

---

## 8. Tests

### Alert-builder tests — `tests/test_admin_alerts.py`
- `test_e014_cross_source_dupe` (`:272-296`): model-overlap variant. Substring anchors:
  `assertIn("[E014]", msg)`, `assertIn("🤔", msg)`, `assertIn("Похож на дубль", msg)`
  (comment `:285` "Integration tests pin this exact substring"), plus links, sources, `35%`,
  `2/6`, model names, `Что произошло`, `Что сделать`.
- `test_e014_broad_series_flag` (`:298-324`): `pairs=` variant; asserts no raw-key artifacts
  (`*`, `|B`, `|`) leak.
- Convention: **substring anchoring, never full-string equality** — the builder text can grow
  (e.g. we can append nothing to the text for buttons since buttons are `reply_markup`, not
  body). The `Похож на дубль` / `Заблокирован дубль` / `Дедуп в degraded mode` anchors carry
  explicit *"Не менять"* / *"substring-якорь"* comments at `admin_alerts.py:714-715`, `:763`,
  `:793`. **Do not touch the E014 body text.** Buttons live in `reply_markup`, which these
  builder tests don't see — the builder stays a pure `str` function; button construction
  belongs in `news_bot` (or a new helper), not in `alert_cross_source_dupe`.

### news_bot integration tests — `tests/test_integration.py`
- E014-asserting tests: `test_broad_pair_soft_flag_is_terminal` (`:1186-1240`),
  `test_soft_flag_logged_even_when_alert_rate_limited` (`:1259+`),
  `test_quiet_day_ping_shows_dedup_collapse` (`:1131+`), and a no-E014 case (`:1112`).
- **Key convention for our change:** they patch `@patch('news_bot.send_admin_notification')`
  and inspect the **first positional arg**:
  ```python
  e014_calls = [c for c in mock_admin.call_args_list
                if '[E014]' in (c.args[0] if c.args else '')]   # :1234-1236
  self.assertEqual(len(e014_calls), 1, ...)                     # :1238
  ```
  Because they read `c.args[0]` (the message text), **adding `reply_markup=` as a
  keyword-only arg to `send_admin_notification` keeps every one of these tests green** — the
  text stays positional arg 0. New tests would assert on the `reply_markup=` kwarg / button
  callback_data separately.
- Base fixture silences notifications: `self.notify_patcher = patch(
  'news_bot.send_admin_notification')` (`:88`); per-test overrides re-enable inspection
  (`:429-450`).

### Other relevant test files
- `tests/test_admin_ping.py` — exercises `send_admin_notification` send/retry directly (the
  function we'll extend). Good home for a "forwards reply_markup" unit test.
- `tests/test_pending_articles_repo.py` — repo tests incl. `PRAGMA table_info` schema
  invariants (`:196`); home for `skip_pending` behaviour + any busy_timeout addition.
- `tests/test_no_token_leak_in_logs.py` — references E014; ensures redaction. A new
  callback-handler path handling `from_user` / links must not leak the token.
- No existing test framework for **inbound** updates — all-new territory. Framework is
  `unittest` + `pytest` runner (`requirements-dev.txt`: `pytest>=8,<10`, `freezegun`).

---

## Risks / unknowns

1. **`pending_articles_repo._connect()` has no `busy_timeout`** (`:174-177`). A callback-thread
   `skip_pending` racing the main-thread `job()` writer can raise `database is locked`
   instantly. The `busy_timeout`/`BEGIN IMMEDIATE` precedent exists only in `outage_state.py`
   — must be ported to the pending path (or `skip_pending` given its own timed connection).
   The task premise ("helpers already present [in the repo]") was inaccurate; they're in a
   sibling module.

2. **In-flight publish race (small window).** If the operator taps «Не публиковать» during the
   few seconds `_publish_with_retries(rows[0])` is already executing for that exact row, the
   in-memory `row` dict (captured at `news_bot.py:2584`) won't see the delete and the article
   may still publish. The idempotency guard checks `get_published`, not a cancel flag, so it
   won't catch this. Removing the row before the slot is otherwise fully sufficient (loop
   re-reads `list_pending` each slot). Tech-spec should decide whether to add a
   pre-publish "still pending?" re-check inside `_publish_with_retries` / `_fallback_publish`.

3. **callback_data 64-byte limit vs URL PK.** No integer surrogate key and no token-map exist
   today (§2). A hashing/token scheme is net-new; decide storage (new table vs `bot_state`
   vs stateless hash-scan of `list_pending`).

4. **Admin id type ambiguity** (§7). Code default `@sunny413x` is a username; callback auth
   needs a numeric id. Equality against a non-numeric value silently rejects all callbacks.
   Needs an explicit numeric-id contract or a new env var.

5. **Shared token → exactly one poller** (§6). getUpdates 409-conflicts if both prod and test
   poll. Prod and test have independent `news.db`, so a callback only cancels rows in the
   polling instance's DB. Must pin polling to one instance (likely prod via `INSTANCE_LABEL`)
   and define what buttons do on the non-polling (test) instance.

6. **Concurrency model is net-new.** The bot has never run a long-lived event loop; `main()`'s
   thread blocks in `time.sleep` for most of the publish window. Running getUpdates needs a
   background thread with its own asyncio loop (or a separate process) — no existing pattern to
   copy; PTB 21.10 `Application.run_polling()` assumes it owns the main thread/signals.

7. **Button lifecycle / idempotency of the tap.** Not yet specified: should the buttons be
   removed after a tap (edit `reply_markup` to empty)? What if the row was already published
   or already skipped when «Не публиковать» arrives (`skip_pending` early-returns on missing
   row — safe, but the operator needs feedback via `answerCallbackQuery`)? What if E014 was
   rate-limited so no ping/buttons were sent (`is_pair_rate_limited`, §1)?

## Key file:line index

| Concern | Location |
|---|---|
| E014 builder | `admin_alerts.py:702-749` (anchor `:714-715`) |
| E014 call site (flag branch) | `news_bot.py:2359-2397` |
| `send_admin_notification` (no reply_markup) | `news_bot.py:469-530` (send `:501-504`) |
| Telegram imports | `news_bot.py:46-47` |
| `pending_articles` DDL, PK=link | `pending_articles_repo.py:60-83` (`:62`) |
| `skip_pending` | `pending_articles_repo.py:727-757` |
| `list_pending` | `pending_articles_repo.py:432-462` |
| `get_published` / `get_pending` | `pending_articles_repo.py:404-415` / `389-401` |
| repo `_connect` (bare, no busy_timeout) | `pending_articles_repo.py:174-177` |
| busy_timeout / BEGIN IMMEDIATE precedent | `outage_state.py:105-115`, `:135-148` |
| idempotency guard | `news_bot.py:1548-1601` (`:1581`) |
| slot publish loop | `news_bot.py:2564-2590` (`list_pending` `:2577`, `sleep` `:2575`) |
| `main()` / schedule / keep-alive | `news_bot.py:2741-2829` (`:2820`, `:2827`) |
| singleton flock | `news_bot.py:2831-2880` |
| config: token/channel/admin/label | `news_bot.py:100-105` |
| PTB version | `requirements.txt` → `python-telegram-bot==21.10` |
| shared-token / INSTANCE_LABEL | `deployment.md:105`, `:108`, `:111` |
| E014 builder tests | `tests/test_admin_alerts.py:272-324` |
| E014 integration tests (`args[0]` convention) | `tests/test_integration.py:1186-1240` |
| send/retry unit tests | `tests/test_admin_ping.py` |
