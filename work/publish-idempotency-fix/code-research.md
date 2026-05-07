# Code Research: publish-idempotency-fix

Bug: duplicate Telegram channel post when a stale `pending_articles` row coexists with a `published_articles` row for the same link. The slot loop publishes (Telegraph + Telegram) BEFORE `move_to_published` writes the published row → IntegrityError → strike → next slot publishes the SAME row again.

Reproduced 2026-05-07 — same Telegraph URL posted at 10:00 and 11:30 МСК.

This document covers exact wiring, repo internals, helper signatures, test infrastructure, risks, and deploy specifics.

---

## A. Exact wiring of `_fallback_publish` and slot loop

### A.1 `_fallback_publish` — file `/workspaces/debian-2/my-hw/news_bot.py`

**Signature** (line 960):
```
def _fallback_publish(row, via_review=False):
```

Returns `True` on success. Re-raises `ClaudeOutageError` after a successful degraded-mode publish (so `job()` can advance its slot loop without strike).

### A.2 Side-effect order — annotated

| Step | Line(s) | Side-effect type | Notes |
|------|---------|------------------|-------|
| 0 | 984 | `link = row['link']` | pure read |
| 1a | 995 | `outage_signal = None` | pure |
| 1b | 1045 | `if outage_state.is_fallback_active():` — short-circuit Google branch | DB read (bot_state) |
| 1c | 1056 | `transcreate_via_claude(row)` | network (Anthropic SDK), can raise `ClaudeTranscreationError` / `ClaudeOutageError` |
| 1d | 1106 | `outage_state.record_outage_event(...)` (only on `ClaudeOutageError`) | DB write (bot_state) |
| 1e | 1111 | `send_admin_notification(ping_text)` (only on outage path, possibly multiple) | **Telegram side-effect** (admin chat, not channel) |
| 1f | 1126 | `_google_translate()` | network (Google Translate) — only on outage / already-in-fallback / ClaudeTranscreationError-not-applicable |
| 1g | 1145–1152 | `_strip_plugs(...)` | pure |
| **2** | **1193** | **`telegraph_publisher.publish_article(...)`** | **Telegraph network — creates a NEW Telegra.ph page** (skipped if `row['telegraph_url']` already set, line 1182) |
| **2b** | **1212** | **`pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)`** | DB write (pending_articles) — only when Telegraph URL was just created |
| 3 | 1221 | `pending_repo.update_staged(link, ru_title, ru_subtitle, ru_paragraphs, ru_blocks)` | DB write (pending_articles) |
| **4** | **1237** | **`send_telegraph_teaser(telegraph_url, link)`** | **Telegram CHANNEL side-effect — this is the duplicate post visible to users** |
| **5** | **1244** | **`pending_repo.move_to_published(link, telegraph_url, telegraph_path, via_review=via_review)`** | DB write — INSERT to published_articles + INSERT OR IGNORE processed_news + DELETE pending. **THIS IS WHERE IntegrityError IS RAISED on the duplicate path.** |
| 6 | 1249 | `_cleanup_preview_html(row.get('preview_html_path'))` | filesystem unlink (best-effort) |
| 7 | 1263 | `if outage_signal is not None: raise outage_signal` | re-raise after successful publish |
| 8 | 1266 | `return True` | |

### A.3 Where the new guard MUST be inserted

**Earliest line where a guard catches all 4 entry conditions BEFORE any side effect:** between line 984 (`link = row['link']`) and line 985 (blank).

The 4 entry conditions in scope:
1. **Claude success path** — enters via line 1055 `try:` block. Earliest deviation from common code is line 1051 (the `else:` of the fallback shortcut). Putting the guard at line 985 covers this path.
2. **Claude per-article failure** — `ClaudeTranscreationError` raised at line 1065, which propagates out (re-raise at 1078) WITHOUT having published anything. This case never reaches Step 2/4/5, so a guard here is harmless but the behaviour is also already correct (no duplicate post). The guard would still let the loop short-circuit to skip_pending instead of striking.
3. **Claude API outage** — `ClaudeOutageError` at line 1079, then `record_outage_event` (line 1106) + admin pings (line 1111) + Google translate (line 1126) + Steps 2–5. The outage case is the path most likely to publish a duplicate (it does run Steps 2–5). The guard at line 985 prevents this.
4. **`outage_state.is_fallback_active()` shortcut** — line 1045 `if` branch — Google translate (line 1049) + Steps 2–5. Same as case 3.

**Conclusion: insert the guard at the very top of `_fallback_publish`, immediately after `link = row['link']` (line 984) and BEFORE any other code (in particular before the `outage_state.is_fallback_active()` read at 1045).** That position dominates all 4 paths.

**Bypass risk if placed wrong:**
- If placed after line 1045 → the `is_fallback_active()` branch already invoked a DB read (cheap, safe), but the guard still works.
- If placed after line 1055 (inside `else:`) → Claude success path is guarded BUT the `is_fallback_active()` shortcut path (line 1045) bypasses it entirely → still posts duplicates on outage days.
- If placed after line 1126 (after `_google_translate`) → too late, Telegraph page may already be created (if `telegraph_url` was NULL) — Telegraph-side cleanup is impossible (Telegraph create is non-idempotent and there's no delete API). Wasted work.
- If placed after line 1182 (Telegraph reuse check) → Telegraph URL is already cached from the prior publish, so reuse path is taken and no Telegraph call is made; BUT translate work still runs unnecessarily. Acceptable but wastes Claude tokens.
- If placed after line 1212 (`mark_telegraph_published`) → DB write to pending_articles already happened (no-op overwrite); still acceptable.
- If placed after line 1237 (`send_telegraph_teaser`) → **TOO LATE** — duplicate Telegram message already sent. This is the current buggy state.
- If placed after line 1244 (`move_to_published`) → IntegrityError already raised; useless.

**Operational note:** Wave-1-6 of llm-transcreation-and-distributed-publishing intentionally ran Step 2 (Telegraph) BEFORE persisting RU fields (line 1207–1212) so a Telegraph failure leaves the row retry-eligible. The guard sits outside this ordering and only short-circuits the function before any of Steps 1–5.

### A.4 Slot-loop wiring — `job()` step (e), file `news_bot.py`

```
1720  for idx, slot in enumerate(slots, start=1):
...
1733      rows = pending_repo.list_pending()
1734      if not rows: ...break
1740      row = rows[0]               # picks oldest pending
1741      link = row.get('link')
1746      try:
1747          _fallback_publish(row, via_review=False)
1748          published_count += 1
1749      except ClaudeOutageError:
1761          published_count += 1     # degraded publish counted
1762      except Exception as exc:
1763          safe = sanitize_error_message(exc)
1764          logger.error(f"[slot {idx}/{len(slots)}] publish failed for {link}: {safe}")
1768          new_count = pending_repo.increment_attempt(link, safe)
1775          if new_count >= 3:
1777              pending_repo.move_to_failed(link, safe)
```

**Key observation:** the slot loop catches `Exception` (line 1762) — which includes `sqlite3.IntegrityError` raised by `move_to_published`. The error is logged via `sanitize_error_message`, attempt_count is incremented, and at strike 3 the row is moved to `failed_articles`. **Crucially, the loop does NOT remove the row from pending after the first failed slot — it stays in pending until 3rd strike.** Because `_fallback_publish` is called ONCE per slot, and the next slot just re-reads `list_pending()[0]` (same row, same stale `telegraph_url`), the duplicate Telegram post fires on EACH slot until the row reaches strike 3.

`list_pending()` ordering (`pending_articles_repo.py:411`):
```
ORDER BY CASE WHEN date(fetched_at) = date('now') THEN 0 ELSE 1 END,
         fetched_at ASC
```
Today's batch first (oldest-first within), then carry-over oldest-first. A stale row from a previous day will be Tier 1 and drained before any fresh row.

### A.5 Exception propagation summary

| Raise site | Caught at | Effect |
|------------|-----------|--------|
| `IntegrityError` from `INSERT INTO published_articles` (`pending_articles_repo.py:582`) | `_fallback_publish` line 1244 — NOT caught locally → propagates to slot loop | Slot loop line 1762 `except Exception` → strike, eventual `move_to_failed` |
| `RuntimeError` from `send_telegraph_teaser` returning False (line 1239) | same | same |
| `ClaudeTranscreationError` (line 1078) | slot loop line 1762 | strike |
| `ClaudeOutageError` (line 1264) | slot loop line 1749 | NOT a strike, `published_count += 1` |
| `GoogleTranslationError` (line 1027) | slot loop line 1762 | strike |

---

## B. `move_to_published` transaction shape

File: `/workspaces/debian-2/my-hw/pending_articles_repo.py`

### B.1 Signature + body

```
546  def move_to_published(link, telegraph_url, telegraph_path, via_review):
562      conn = _connect()
563      try:
564          # Step 0: SELECT title, ru_title, source_name, pub_date FROM pending_articles
569          src = conn.execute(
570              "SELECT title, ru_title, source_name, pub_date "
571              "FROM pending_articles WHERE link=?", (link,)).fetchone()
574          if src is None: return    # noop on missing pending
578          title, ru_title, source_name, pub_date = src

580          # Step 1: INSERT INTO published_articles (link is PRIMARY KEY)
581          conn.execute(
582              "INSERT INTO published_articles "
583              "(link, title, ru_title, telegraph_url, telegraph_path, "
584              " source_name, via_review) "
585              "VALUES (?, ?, ?, ?, ?, ?, ?)",
586              (link, title, ru_title, telegraph_url, telegraph_path,
587               source_name, 1 if via_review else 0))

591          # Step 2: INSERT OR IGNORE INTO processed_news
592          conn.execute(
593              "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
594              "VALUES (?, ?, ?)", (link, title, pub_date))

597          # Step 3: DELETE FROM pending_articles
598          conn.execute("DELETE FROM pending_articles WHERE link=?", (link,))
602          conn.commit()
603      except Exception:
604          conn.rollback()
605          raise
```

### B.2 Where IntegrityError is raised, where caught

- **Raised at** line 581–590 — `INSERT INTO published_articles`. Schema (`_PUBLISHED_DDL`, line 70–81): `link TEXT PRIMARY KEY`. Duplicate link → `sqlite3.IntegrityError: UNIQUE constraint failed: published_articles.link`.
- **Caught at** line 603 (rollback + re-raise) → propagates to caller `_fallback_publish` (no local catch) → propagates to slot loop `except Exception` at `news_bot.py:1762`.
- The `processed_news` INSERT at line 593 already uses `INSERT OR IGNORE` — so it never raises on PK conflict. **Precedent for `INSERT OR IGNORE`-based idempotency exists in this same function.**

### B.3 Is `INSERT OR IGNORE` safe to apply to step 1?

**Yes, with caveats — see below.**

Behaviour change: `INSERT OR IGNORE INTO published_articles ...` will silently skip the INSERT when `link` is already present. **rowcount is 0**, but `move_to_published` does NOT read `cur.rowcount` after step 1 (no rowcount-dependent control flow). Steps 2 (processed_news) and 3 (DELETE pending) execute unconditionally. The function returns normally.

Net effect when row already published:
- `published_articles` — preserved untouched (the older row, with its original `published_at` and `via_review`).
- `processed_news` — gets the link (already there from the first publish, IGNORE).
- `pending_articles` — DELETEd (the stale row is cleaned up — desirable).

**Caveats:**
- The two columns the published row holds — `telegraph_url` and `telegraph_path` — keep their FIRST values, not the new ones passed in. For our bug this is correct (first publish IS the canonical one; the duplicate publish is the symptom we're suppressing).
- `via_review` similarly stays at its first value. Acceptable.
- The function loses the ability to signal "already published" to the caller (it returns None on both paths — true insert and ignored insert). For defense-in-depth this is fine; the upper-level guard in `_fallback_publish` is the one that pings admin.
- Callers who depend on the IntegrityError to surface "already published" — there are NONE. `grep -rn IntegrityError tests/ news_bot.py hw_review.py` returns only `pending_articles_repo.py:235` (`insert_pending` UNIQUE-conflict catch, unrelated).

**No tests assert that `move_to_published` raises `IntegrityError`** — confirmed by grep across `tests/`. The closest existing test is `tests/test_pending_articles_repo.py:623` (`test_move_to_published_rollback_on_error`) which injects a `sqlite3.OperationalError` on the 3rd execute — not affected by this change.

### B.4 Precedent of `INSERT OR IGNORE` pattern

- Line 593 — `INSERT OR IGNORE INTO processed_news` (inside `move_to_published` itself).
- Line 673 — `INSERT OR IGNORE INTO processed_news` inside `skip_pending`.
- Both use the pattern WITHOUT reading rowcount. This matches the proposed change exactly.

---

## C. Existing helpers we can reuse

### C.1 `pending_articles_repo.get_published(link)` — line 372

```
def get_published(link: str) -> Optional[dict]:
    """Return one published row or None. No JSON columns on this table."""
```
Opens its own connection, returns the full row dict (deserialised — but no JSON columns on this table). Returns `None` if not found. **Suitable for the upper-level guard read.**

Schema columns available: `link`, `title`, `ru_title`, `telegraph_url`, `telegraph_path`, `source_name`, `published_at`, `via_review` (per `_PUBLISHED_DDL` line 70–81).

### C.2 `pending_articles_repo.skip_pending(link)` — line 656

```
def skip_pending(link: str) -> None:
    """Write the link to processed_news (dedup) and DELETE from pending.
    NO write to published_articles — skip is not a publish."""
```

Body (line 662–686):
1. SELECT title, pub_date FROM pending_articles
2. INSERT OR IGNORE INTO processed_news (link, title, pub_date)
3. DELETE FROM pending_articles WHERE link=?
4. commit (rollback on exception)

`return` early if pending row not found (line 668–669) — safe no-op. **Exactly the cleanup we need: removes the stale pending row, ensures the link is in processed_news for future fetch-time filtering, does NOT touch published_articles.**

### C.3 `news_bot.send_admin_notification(message)` — line 357

```
def send_admin_notification(message):
    """Send a notification message to the admin.
    The ``message`` is passed through ``_redact_text`` BEFORE the Telegram
    payload is built so that any caller that accidentally embeds a secret
    (Telegram bot token, Anthropic API key) sees ``***`` in the chat
    rather than the raw value.  Per Decision 12.
    """
```

- Returns `True`/`False` (False on send failure).
- Auto-prepends `[INSTANCE_LABEL]` from env var when set (so prod vs test instance can be distinguished by the operator).
- Catches `TelegramError` internally and logs — does not raise.
- Synchronous wrapper around `asyncio.run(_send())`.
- Already used inside `_fallback_publish`'s outage path at line 1111.

**No throttling.** Each call sends one Telegram message. If the guard fires N times for N stale rows, the operator gets N pings. See risks section.

### C.4 Existing pattern of "skip article and ping admin"

**No exact precedent inside `_fallback_publish`** — the outage path pings admin but does NOT skip the article (it goes on to publish via Google).

**Closest precedent:** `hw_review.cmd_publish` at lines 581–589:
```python
row = repo.get_pending(link)
if row is None:
    if (pub := repo.get_published(link)) is not None:
        _err(f"{link} already published at {pub['telegraph_url']}")
    elif (fail := repo.get_failed(link)) is not None:
        _err(f"{link} in failed: {fail.get('last_error') or ''}")
    else:
        _err(f"{link} not found")
    return 1
```

Same defensive read of `get_published` — but in the OPPOSITE situation (pending vanished, published exists). This is the manual-review CLI's "row vanished between list and publish" guard. The inverse (pending PRESENT + published PRESENT → skip + admin ping) is the one we need to add.

There's also a test for that path at `tests/test_hw_review_publish_flow.py:259-313` (`test_publish_vanished_row_already_published`) — useful as a structural template.

---

## D. Test infrastructure for the new tests

### D.1 `tests/conftest.py` — line 1–13

Trivial — just adds repo root to `sys.path`. **No shared fixtures.** Each test file owns its own setUp/tearDown.

### D.2 `tests/test_fallback_publish_paths.py` — 469 lines

**Existing layout:**
- Base case `_FallbackPublishPathsCase(unittest.TestCase)` at line 90.
- `setUp` (line 98): tempfile DB → `patch('news_bot.DB_FILE', self.db_path)` → `news_bot.init_db()` (creates all tables) → patches Telegram tokens (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID`) to mock strings.
- `tearDown` (line 112): stop all patchers, unlink the tempfile.
- `_insert(**kw)` helper at line 120: builds a sample entry via `_sample_entry(**kw)` and calls `repo.insert_pending(entry)`.

**Mock patterns used:**
- `MagicMock` + `manager.attach_mock(...)` to track call ORDER across mocks (line 158–171, 408–420).
- `patch('news_bot.transcreate_via_claude', mock_claude)` — pins the bound name on `news_bot` module (since import-time alias).
- `patch('news_bot.outage_state.is_fallback_active', return_value=False)` — easy override.
- `patch('news_bot.telegraph_publisher.publish_article', ...)`, `patch('news_bot.send_telegraph_teaser', ...)`, `patch('news_bot.pending_repo.move_to_published', ...)` — all the side-effect points, mockable individually.
- `MagicMock(side_effect=AssertionError("..."))` to assert "must NOT be called" — typical idiom.
- For "send_message NOT called" — patch `send_telegraph_teaser` (Telegram CHANNEL post wrapper) with a MagicMock and assert `mock_teaser.assert_not_called()`.

**Pre-staging a published row** — pattern from `tests/test_hw_review_publish_flow.py:259-313`:
```python
conn = sqlite3.connect(self.db_path)
try:
    conn.execute(
        "INSERT INTO published_articles "
        "(link, title, ru_title, telegraph_url, telegraph_path, "
        " source_name, via_review) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (link, 'EN', 'РУ', 'https://telegra.ph/OLD-URL', 'OLD-URL',
         'autoevolution', 1))
    conn.commit()
finally:
    conn.close()
```
This is the canonical "raw INSERT" template. Reuse for the new test.

**Existing classes** (templates to copy from):
- `TestClaudePath` (line 131) — happy Claude success.
- `TestGoogleFallbackPath` (line 226) — per-article ClaudeTranscreationError raises straight up.
- `TestGoogleEnglishGuard` (line 281) — exercises already-in-fallback shortcut + identity Google.
- `TestAlreadyInFallback` (line 316) — `is_fallback_active()=True` shortcut.
- `TestOutageDegradedThenReraises` (line 368) — full outage path with admin notify.

The new test class fits naturally next to `TestAlreadyInFallback` — same fixture base.

### D.3 `tests/test_pending_articles_repo.py` — 892 lines

**Schema setup pattern:** `_TmpDbCase` at line 99:
```python
def setUp(self):
    fd, self.db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    self.db_patcher = patch.object(news_bot, 'DB_FILE', self.db_path)
    self.db_patcher.start()
    conn = sqlite3.connect(self.db_path)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS processed_news ...')
        conn.commit()
        repo.init_schema(conn)
    finally:
        conn.close()
```

**Transactional repo tests use `WrappingConn` pattern** (line 636–667) — wraps `sqlite3.connect` to inject failures at a specific execute-call-index. Reusable for testing rollback and exception propagation.

**Test pattern for `INSERT OR IGNORE` change** — model on:
- `test_move_to_published_atomic` (line 577)
- `test_move_to_published_via_review_false` (line 608)
- `test_move_to_published_rollback_on_error` (line 623) — fault injection pattern

For the new test (move_to_published handles existing row gracefully):
1. Insert a pending row (`repo.insert_pending`) + stage it (`self._stage(entry)`).
2. Pre-insert a published row with the same link (raw `INSERT INTO published_articles`).
3. Call `repo.move_to_published(link, ...)`.
4. Assert: NO exception raised; pending_articles row gone; published_articles row UNCHANGED (original telegraph_url preserved); processed_news has the link.

`_stage(entry)` helper exists — it populates the `ru_*` columns so the NOT NULL `ru_title` constraint on published_articles passes. See test_pending_articles_repo.py earlier in the file.

### D.4 `tests/test_distributed_schedule_integration.py` — 694 lines

End-to-end slot loop test pattern at `TestDistributedSchedule.setUp` (line 150–229).

**Always-on patches:**
- `patch('news_bot.DB_FILE', self.db_path)` + `news_bot.init_db()`
- Telegram credentials (mock strings)
- `news_bot.send_admin_notification` → MagicMock (`self.mock_notify`)
- `news_bot.time.sleep` → MagicMock (no real wall-clock waits)
- `news_bot.send_telegraph_teaser` → `return_value=True`
- `news_bot.telegraph_publisher.publish_article` → `return_value='https://telegra.ph/fake-page-04-27'`
- `news_bot.transcreate_text` → identity-ish stub
- `news_bot.load_feeds`, `news_bot.fetch_rss`, `news_bot.fetch_mattel_news`, `news_bot.SOURCES`, `news_bot.fetch_full_article` — full pipeline mocked

**Per-test patches:**
- `news_bot.transcreate_via_claude` (different `side_effect` per scenario)
- `freezegun.freeze_time('2026-04-27 09:00:00')` for deterministic slot computation (09:00 UTC == 12:00 МСК — note this comment is stale, the test description says "12:00" but `_fallback_publish` uses `WINDOW_START_TIME` = 10:00 МСК per docstring; either way the test fixes a known frozen instant).

**End-to-end driver:** `news_bot.job()` is invoked synchronously (no schedule library involvement). The crash-loop guard (line 1538–1566) sleeps via the patched `time.sleep` (no-op).

**Helpers worth knowing:**
- `_published_links()` (line 258) — direct SELECT from `published_articles`.
- `_pending_links()` (line 270) — calls `pending_articles_repo.list_pending`.
- `_admin_messages()` (line 274) — extracts ping payloads from `mock_notify.call_args_list`.

For an integration test of the new guard — pre-seed a stale pending row + a published row with the same link, then call `news_bot.job()`. Assert:
- `mock_teaser` (= `send_telegraph_teaser`) call count includes only fresh rows (NOT the duplicate one).
- Admin pings include the new "stale-row-cleanup" diagnostic.
- After the run, pending_articles is empty (stale row cleaned up).

### D.5 Existing fixtures: builders

- `tests/test_pending_articles_repo.py:_sample_entry(...)` (line 84) — pending row builder.
- `tests/test_fallback_publish_paths.py:_sample_entry(...)` (line 58) — pending row builder.
- `tests/test_fallback_publish_paths.py:_claude_dict(...)` (line 76) — Claude response shape.
- `tests/test_distributed_schedule_integration.py:_create_mock_rss_entry`, `_create_mock_full_article`, `_make_claude_result` (lines 69–120).

**No published-row builder exists** — every test using a pre-existing published row inlines a raw INSERT. That's the pattern to follow.

**Time mocks:**
- `freezegun.freeze_time(...)` everywhere in the integration test file (`from freezegun import freeze_time`, line 52).
- `patch('news_bot.time.sleep')` (line 173) for slot-by-slot waits.
- `schedule.run_pending` is NOT exercised in tests — `job()` is called directly. The schedule library is module-level only.

---

## E. Risks and gotchas

### E.1 Race conditions: can two `job()` invocations interleave?

**No.** `news_bot.main()` runs the daily loop on a single thread:
```
1879  while True:
1880      schedule.run_pending()   # synchronous job() call
1881      time.sleep(60)
```
The `schedule==1.2.1` library `run_pending()` calls jobs synchronously on the same thread. There's also a one-shot `job()` call at line 1876 on first boot. Between these, a `job()` body runs to completion before another tick is scheduled.

**However**, there IS a different concurrency vector: the operator-driven `hw_review.py publish N` CLI (currently dormant — archived 2026-04-30) opens its own connection. If revived during a running `job()` cron tick, it could publish a row that the slot loop is currently iterating over. The existing `hw_review.cmd_publish` already handles this race at line 581–589 (re-read pending, fall through to "already published" message). The new guard makes the symmetric case safe in the auto path.

**Within the slot loop itself** (line 1720–1786), each iteration is sequential. No cross-slot interleaving.

**Crash-loop on restart:** systemd may restart the service if it crashes. The crash-loop guard (line 1538–1566) sleeps until `last_published + MIN_INTERVAL_MINUTES`. This is unrelated to the duplicate-post bug but worth noting because a deploy-restart 30s after a publish would otherwise re-execute step 1876's one-shot `job()`.

### E.2 Backwards compatibility of `INSERT OR IGNORE` — tests asserting IntegrityError

**Confirmed by `grep -rn 'IntegrityError\|UNIQUE constraint' tests/`:** zero tests assert that `move_to_published` raises `IntegrityError` (or any UNIQUE-conflict error). The only `IntegrityError` reference is `pending_articles_repo.py:235` inside `insert_pending` — unrelated.

`tests/test_move_to_published_rollback_on_error` (line 623) injects a `sqlite3.OperationalError` on the 3rd execute (= `INSERT OR IGNORE INTO processed_news`). Switching step 1 to `INSERT OR IGNORE` does NOT change the call sequence — same 4 executes (SELECT, INSERT OR IGNORE published, INSERT OR IGNORE processed, DELETE pending) — so the test stays green.

### E.3 Logging volume / admin ping throttle

**`send_admin_notification` has NO throttling.** Each call dispatches one Telegram message. If the operator restores a backup containing K stale rows whose links are also in published_articles, the guard will fire K times, sending K admin pings.

Counter-argument: K stale rows is a real, actionable incident — the operator NEEDS to know all of them. A summary ping at the end of `job()` would lose information about which links are affected. Recommendation for the spec: don't pre-throttle, but keep the ping body short ("⚠️ stale pending row {link} matched published_articles — auto-cleaned"). If post-deploy reality shows >5 pings in a single tick, add a single end-of-job summary ping in a follow-up.

The same K rows scenario with the bug present would have produced 3K Telegram CHANNEL posts (3 strikes × K rows). The fix is a clear win.

### E.4 Dormant `hw_review.py` — does the guard affect it?

`hw_review.py` is **not deployed** (per `references/deployment.md:100`) but its tests (`test_hw_review_publish_flow.py`, etc.) are part of the 740-test green-bar invariant.

The guard goes into `_fallback_publish` (`news_bot.py`), which `hw_review.py` doesn't call. `hw_review.cmd_publish` has its OWN publish flow that calls `repo.move_to_published(link, ..., via_review=True)` directly (line 655). It already has the symmetric guard at line 581–589.

So: changing `_fallback_publish` does NOT touch hw_review's tests. Changing `move_to_published` to `INSERT OR IGNORE` DOES affect hw_review's publish flow — but in the same way: it makes the manual publish idempotent in case of a stale-published-row race. No hw_review test asserts that `move_to_published` raises (verified by grep).

The hw_review test at `test_hw_review_publish_flow.py:259-313` uses raw INSERT into `published_articles` to set up the "already published" scenario, then asserts that `cmd_publish` errors out with "already published" BEFORE calling `move_to_published` (it patches `publish_article` and `send_telegraph_teaser` to assert NOT called). The `INSERT OR IGNORE` change to `move_to_published` doesn't affect this test — `cmd_publish` short-circuits before reaching `move_to_published`.

**740-test green bar:** safe.

### E.5 Subtle: stale `ru_paragraphs` and re-staging

The buggy stale row may have `telegraph_url` cached AND `ru_paragraphs` cached (because the previous publish ran Step 3 `update_staged` at line 1221). On the second run, the function does Steps 1 (translate again — wasteful but harmless), 2 (skip Telegraph creation — line 1182 reuse path), 3 (overwrite RU fields with new translation — wasteful), 4 (Telegram teaser — duplicate post!), 5 (IntegrityError).

If we put the guard at the very top, Steps 1–4 are all skipped. Translation tokens saved.

### E.6 `pending_articles` `link` is also PRIMARY KEY

Schema (line 56): `link TEXT PRIMARY KEY` on pending too. So the bug specifically requires that the published row's `link` matches an EXISTING pending row's `link`. The original timeline (per user-spec) is:
1. Row gets fetched (insert_pending).
2. First slot: publishes successfully → moves to published_articles, deletes pending.
3. SOMEHOW the row reappears in pending_articles with the same link (this is the root cause investigation operator handles).
4. Slot loop re-publishes.

For step 3 to happen, either:
- `insert_pending` was called manually (not visible in the cron path — the prep phase filters out links already in `pending_articles` AND in `processed_news` via `filter_new_entries` → `is_processed`).
- A backup restore copied a stale `pending_articles` row over a current `published_articles`+`processed_news` state.
- A migration script malfunctioned.

**The fix is symptom-only and explicitly does NOT investigate root cause** (per user-spec interview). It just makes the symptom safe.

### E.7 `update_staged` and `mark_telegraph_published` — DB writes that the guard skips

If the guard fires before line 1212 / 1221, the stale pending row's `telegraph_url` and `ru_paragraphs` are NOT overwritten. Then `skip_pending` deletes the row entirely. Net: the stale Telegraph URL on the deleted row is lost — but a copy already lives in `published_articles.telegraph_url` (the canonical one from the first publish), so the operator has the URL on record.

---

## F. Deploy specifics

### F.1 Deploy FILES list

Per `.claude/skills/project-knowledge/references/deployment.md:93`:
- `news_bot.py` — **YES, in FILES** (core cron-path).
- `pending_articles_repo.py` — **YES, in FILES** (core cron-path).
- `hw_review.py` — **NOT in FILES** (line 100: "manual review path archived 2026-04-30, code preserved + tests green for ad-hoc revival, but never deployed").

The FILES list lives in two places:
- `.github/workflows/deploy.yml`
- `deploy.sh`
And is asserted byte-for-byte identical (deployment.md:98). Adding/removing files requires touching both.

The fix touches only `news_bot.py` + `pending_articles_repo.py` — both already in FILES. **No deploy file-list change needed.**

### F.2 Test → prod path

Per deployment.md:68:
1. `git push origin dev` → `ci.yml` runs pytest → on green, `deploy_test.yml` triggers via `workflow_run`.
2. `deploy_test.yml` → SCPs FILES list to `/home/hwbot/bot_test/`, `pip install`, `sudo systemctl restart news_bot_test.service`.
3. Test instance posts to `@myhwchannel123` (operator's only-subscriber test channel).
4. Operator inspects test channel → confirms.
5. `git checkout main && git merge dev && git push origin main` → `ci.yml` → `deploy.yml` → prod (`/home/hwbot/bot/`, channel `-1004027529994`).

The test channel is the right place to verify "no duplicate" empirically: deploy with the fix, manually inject a stale pending row matching a published_articles row in the test DB, watch the next cron tick clean it up without re-posting.

### F.3 Post-deploy operator action

The user-spec mentions the failed_articles cleanup needed AFTER the fix is in prod, because the buggy run already pushed the row to failed_articles after 3 strikes:

```
sqlite3 /home/hwbot/bot/news.db \
  "DELETE FROM failed_articles WHERE link='https://orangetrackdiecast.com/2026/05/02/hot-wheels-2026-car-culture-team-transport-k-case-report/';"
```

**This is a one-time manual operator step** — out of scope for the code change per user-spec interview.

Per `feedback_operator_prod_ops.md` (auto-memory): the operator handles production ops; Claude prepares the SQL and the operator runs it.

### F.4 Other deploy notes

- ANTHROPIC_API_KEY, ANTHROPIC_MODEL, TZ are written to server `.env` by `deploy.yml` step "Write runtime env vars" (deployment.md:47). Not affected.
- TELEGRAM_*, TELEGRAPH_ACCESS_TOKEN preserved verbatim. Not affected.
- Cron schedule: `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow"))` (news_bot.py:1872). Not affected.
- The fix does NOT require a DB migration (no schema change). `news.db` is unchanged.

---

## G. Summary — concrete implementation cues

> ⚠️ **STALE — superseded by tech-spec Decisions 1, 4, 8.** This section was drafted during the user-spec phase. It uses `logger.warning` (tech-spec Decision 1 finalises **INFO** with `[idempotency-guard]` tag) and wraps `send_admin_notification` in `try/except` (tech-spec Decision 4 finalises **return-value check, no try/except** — `send_admin_notification` never raises). The `try/except` around `skip_pending` IS in the final design (per tech-spec Decision 8 — handles cleanup-failure case). Implementors: read tech-spec Decisions 1/4/8 + Architecture diagram, NOT this section's snippet. Section preserved for traceability only.

**File 1: `/workspaces/debian-2/my-hw/news_bot.py`** — insert at line 985 (immediately after `link = row['link']`):
```python
# Idempotency guard (publish-idempotency-fix): if the link is already in
# published_articles, the previous publish completed but a stale pending
# row remained — clean it up and skip ALL side effects (Telegraph,
# Telegram teaser, move_to_published). One admin ping per occurrence so
# operator can investigate root cause via journalctl.
existing = pending_repo.get_published(link)
if existing is not None:
    logger.warning(
        f"[idempotency] {link} already in published_articles "
        f"(url={existing['telegraph_url']}); cleaning up stale pending row."
    )
    try:
        send_admin_notification(
            f"⚠️ Stale pending row matched published_articles for {link} "
            f"— auto-cleaned. (telegraph_url={existing['telegraph_url']})"
        )
    except Exception as notify_err:
        logger.error(
            f"[idempotency] admin ping failed: "
            f"{sanitize_error_message(notify_err)}"
        )
    pending_repo.skip_pending(link)
    return True
```

Returning `True` matches the function's success contract — slot loop counts this as a published row in `published_count`. (Alternative: return without incrementing; would require a sentinel value or a separate exception. Simpler to return True.)

**File 2: `/workspaces/debian-2/my-hw/pending_articles_repo.py`** — change line 582 from `INSERT INTO published_articles` to `INSERT OR IGNORE INTO published_articles`. No other change. Steps 2 (processed_news) and 3 (DELETE pending) execute unconditionally regardless of step 1's rowcount, which is correct behaviour for the defense-in-depth case.

**Test additions:**
1. `tests/test_fallback_publish_paths.py` — new class `TestStalePendingMatchesPublished` covering: pending row + matching published row → guard fires → `mock_teaser`, `mock_publish`, `mock_move`, `mock_mark` ALL not called → `mock_notify` called once → pending row deleted via `skip_pending` (assert via DB read).
2. `tests/test_pending_articles_repo.py` — new test `test_move_to_published_idempotent_when_published_row_exists` covering: pre-insert published row → call `move_to_published` → no exception → published row unchanged → pending row deleted → processed_news has link.
3. (Optional, integration) `tests/test_distributed_schedule_integration.py` — pre-seed stale pending + published, run `job()`, assert one publish (the legitimate one) + one admin ping (the diagnostic).

**Project doc updates** (per `feedback_always_log_and_document.md` memory): after merge, log session work in `work/SESSION-YYYY-MM-DD.md` and update `.claude/skills/project-knowledge/references/architecture.md` if the idempotency contract is documented there.

---

## Appendix: file:line index

| Reference | File | Line |
|-----------|------|------|
| `_fallback_publish` def | `news_bot.py` | 960 |
| `_fallback_publish` link read | `news_bot.py` | 984 |
| **proposed guard insertion** | `news_bot.py` | **985** |
| `is_fallback_active` shortcut | `news_bot.py` | 1045 |
| Telegraph publish (Step 2) | `news_bot.py` | 1193 |
| `mark_telegraph_published` | `news_bot.py` | 1212 |
| `update_staged` | `news_bot.py` | 1221 |
| `send_telegraph_teaser` (Telegram) | `news_bot.py` | 1237 |
| `move_to_published` call | `news_bot.py` | 1244 |
| `job()` def | `news_bot.py` | 1509 |
| Slot loop start | `news_bot.py` | 1720 |
| `_fallback_publish` call from job | `news_bot.py` | 1747 |
| `except Exception` strike branch | `news_bot.py` | 1762 |
| `send_admin_notification` def | `news_bot.py` | 357 |
| `sanitize_error_message` def | `news_bot.py` | 122 |
| `move_to_published` def | `pending_articles_repo.py` | 546 |
| **proposed `INSERT OR IGNORE`** | `pending_articles_repo.py` | **582** |
| INSERT OR IGNORE processed_news | `pending_articles_repo.py` | 593 |
| DELETE pending | `pending_articles_repo.py` | 599 |
| `get_published` | `pending_articles_repo.py` | 372 |
| `skip_pending` | `pending_articles_repo.py` | 656 |
| `_PUBLISHED_DDL` (link PK) | `pending_articles_repo.py` | 70 |
| `list_pending` ordering | `pending_articles_repo.py` | 411 |
| `insert_pending` IntegrityError catch | `pending_articles_repo.py` | 235 |
| Fallback path test base | `tests/test_fallback_publish_paths.py` | 90 |
| Pending repo test base | `tests/test_pending_articles_repo.py` | 99 |
| WrappingConn rollback pattern | `tests/test_pending_articles_repo.py` | 636 |
| Pre-stage published row (raw INSERT) | `tests/test_hw_review_publish_flow.py` | 271 |
| Integration slot-loop driver | `tests/test_distributed_schedule_integration.py` | 282 |
| Always-on integration mocks | `tests/test_distributed_schedule_integration.py` | 150 |
| Deploy FILES list reference | `.claude/skills/project-knowledge/references/deployment.md` | 93 |
| `hw_review.py` not deployed | `.claude/skills/project-knowledge/references/deployment.md` | 100 |

---

## §H — Implementation-level details (added for tech-spec phase)

## Updated: 2026-05-06

Implementer-grade context: verbatim excerpts at the two diff sites, copy-pasteable test patterns, signature confirmations, wave layout. Read this section together with §A.3 (guard placement rationale) and §G (concrete cues) — H is the surgical layer below G.

### H.1 Verbatim excerpt — `_fallback_publish` guard insertion site

File: `/workspaces/debian-2/my-hw/news_bot.py`, lines 980–1000 (boundary range — `_fallback_publish` def starts at 960; lines 980–984 are the closing tail of the docstring, line 984 is `link = row['link']`, the new guard goes between 984 and 985).

```
980          successful degraded-mode publish so ``job()`` can advance its
981          outage-aware slot loop. Other exceptions propagate to the
982          caller so ``attempt_count`` can be bumped.
983      """
984      link = row['link']
985
986      # Step 1: EN → RU. Two-tier translation engine — see comment header.
987      en_title = row.get('title') or ''
988      en_subtitle = row.get('subtitle') or ''
989      en_paragraphs = row.get('paragraphs') or []
990      en_blocks = row.get('blocks')
991
992      # ``outage_signal`` carries a ``ClaudeOutageError`` to re-raise after
993      # Steps 2–5 complete. It MUST stay None on the happy / per-article
994      # branches so the function's normal True return path is preserved.
995      outage_signal = None
```

**Diff anchor:** the guard block is inserted immediately AFTER line 984 (`link = row['link']`) and BEFORE the blank at 985. The current blank line 985 is preserved as the post-guard separator. No re-indentation of any subsequent code is needed.

### H.2 Verbatim excerpt — `move_to_published` INSERT site

File: `/workspaces/debian-2/my-hw/pending_articles_repo.py`, lines 575–610 (covers the SELECT no-op early-return through the `processed_news` INSERT OR IGNORE):

```
575            # Nothing to move; treat as no-op rather than error. Caller should
576            # have guarded against this, but a missing row is not corruption.
577            return
578        title, ru_title, source_name, pub_date = src
579
580        # Step 1
581        conn.execute(
582            "INSERT INTO published_articles "
583            "(link, title, ru_title, telegraph_url, telegraph_path, "
584            " source_name, via_review) "
585            "VALUES (?, ?, ?, ?, ?, ?, ?)",
586            (
587                link, title, ru_title, telegraph_url, telegraph_path,
588                source_name, 1 if via_review else 0,
589            ),
590        )
591        # Step 2
592        conn.execute(
593            "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
594            "VALUES (?, ?, ?)",
595            (link, title, pub_date),
596        )
597        # Step 3
598        conn.execute(
599            "DELETE FROM pending_articles WHERE link=?",
600            (link,),
601        )
602        conn.commit()
603    except Exception:
604        conn.rollback()
605        raise
606    finally:
607        conn.close()
```

**Diff anchor:** line 582 — change `"INSERT INTO published_articles "` to `"INSERT OR IGNORE INTO published_articles "`. **Single-token edit.** The trailing space is preserved (next line begins with `"(link, title, ...`).

### H.3 Outage-state shortcut — confirmation of position

The `is_fallback_active()` shortcut sits at line 1045 (verbatim, lines 1041–1051):

```
1041      used_google_fallback = False
1042
1043      # Already-in-fallback shortcut (Decision 5 / tech-spec "Publish loop"):
1044      # when the state machine says Claude is down + 2h grace elapsed, route
1045      # straight to Google without trying Claude.
1046      if outage_state.is_fallback_active():
1047          logger.info(
1048              f"[fallback] is_fallback_active=True — routing {link} via Google"
1049          )
1050          ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()
1051          used_google_fallback = True
```

(Note: the line numbers above are 1-based against the actual current file. The §A.3 cross-ref at "line 1045 `if outage_state.is_fallback_active()`" was approximate; the precise `if` is line 1046, with the comment header at 1043–1045. The fix is unaffected.)

**Verified:** the guard at line 985 dominates this block — both the `if` branch (lines 1046–1051) and the `else` Claude branch (line 1052+) execute strictly AFTER the guard's early return. There is no path where line 985 is bypassed and 1046 is reached.

### H.4 Decision 9 retry test — confirmation it stays green

The Decision 9 contract ("Telegraph success + Telegram fail → retry reuses telegraph_url") is anchored in `tests/test_hw_review_publish_flow.py` at the `TestPublishRetryIdempotency` class (lines 156–235), specifically `test_publish_retry_reuses_telegraph_url` (line 158).

**Pivotal assertions (lines 174–199):**
```
174          self.assertEqual(rc1, 1)
175          self.assertIn('telegram send failed', self.stderr.getvalue())
176          # Pending row retained, telegraph_url populated.
177          row = repo.get_pending(entry['link'])
178          self.assertIsNotNone(row)
179          self.assertEqual(row['telegraph_url'], tg_url)
180          self.assertTrue(row['telegraph_path'])  # non-empty path
...
193          # CRITICAL: publish_article was NOT called a second time.
194          self.assertEqual(mock_publish.call_count, 1)
195          self.assertEqual(mock_teaser.call_count, 2)
```

**Why this stays green with the new guard:**
- The test exercises `hw_review.cmd_publish`, NOT `_fallback_publish`. The guard goes into `_fallback_publish` only.
- Even if the test were re-routed through `_fallback_publish`, the `get_published(link)` lookup returns `None` between run 1 and run 2 (run 1 only wrote `telegraph_url` to **pending_articles** via `mark_telegraph_published`; nothing went into `published_articles`). The guard's `if existing is not None` is False → fall-through → normal Decision-9 reuse path takes effect at line 1182.
- The `INSERT OR IGNORE` change to `move_to_published` does not affect this test either: run 2 reaches `move_to_published(link, ...)` with NO pre-existing `published_articles` row, so the INSERT inserts (rowcount 1, behavior unchanged from `INSERT INTO`).

**No auto-path equivalent exists.** A grep across `tests/test_fallback_publish_paths.py` and `tests/test_job_distributed_publish.py` for "reuse"/"telegraph_url"/"call_count == 1 across two runs" returns nothing. The Decision-9 contract is exercised end-to-end only via the manual-review CLI test. The auto-path behaviour is exercised indirectly: `_fallback_publish` reads `row.get('telegraph_url')` at news_bot.py:1180 and the existing `TestClaudePath` does NOT pre-populate it → the reuse branch is dead-coded in those tests. Adding a new `tests/test_fallback_publish_paths.py::TestStalePendingMatchesPublished` does not increase that coverage gap; it tests an orthogonal contract.

### H.5 `send_admin_notification` — full signature and ping interaction

File: `/workspaces/debian-2/my-hw/news_bot.py`, lines 357–392 (full body):

```
357  def send_admin_notification(message):
358      """Send a notification message to the admin.
359
360          The ``message`` is passed through ``_redact_text`` BEFORE the Telegram
361          payload is built so that any caller that accidentally embeds a secret
362          (Telegram bot token, Anthropic API key) sees ``***`` in the chat
363          rather than the raw value.  Per Decision 12.
364      """
365      if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
366          logging.error("Telegram credentials or admin ID not set.")
367          return False
368      safe_message = _redact_text(message)
369      # Prepend [INSTANCE_LABEL] when set so operator can distinguish
370      # admin pings from prod vs test bot in the same admin chat.
371      if INSTANCE_LABEL:
372          safe_message = f"[{INSTANCE_LABEL}] {safe_message}"
...
389          except TelegramError as e:
390              logging.error(f"Failed to send admin notification: {e}")
391              return False
392      return asyncio.run(_send())
```

**Confirmed contract:**
- **Returns:** `True` on successful Telegram send, `False` when (a) credentials missing OR (b) `TelegramError` raised by `bot.send_message`. Never raises.
- **Synchronous wrapper** around `asyncio.run(_send())`. Caller must NOT be inside another event loop (none of `_fallback_publish`'s call paths are).
- **Signature:** single positional `message` arg (str). No keyword args. No throttle, no batch.
- **`_redact_text` interaction with the guard's ping:** the guard's text is `"⚠️ Stale pending row {link} matched published_articles ..."`. The link is a URL (`http://...`/`https://...`) — it does NOT match `_BOT_TOKEN_RE`, `_OPENROUTER_KEY_RE`, `_ANTHROPIC_KEY_RE`, `_OPENAI_KEY_RE`, or `_GEMINI_KEY_RE` (all of those have specific token-shape prefixes — `\d+:[A-Za-z0-9_-]+`, `sk-or-`, `sk-ant-`, `sk-`, `AIza` respectively). The link passes through `_redact_text` unchanged. **No special escaping needed.**
- **`INSTANCE_LABEL` interaction:** when env var is set (e.g. `INSTANCE_LABEL=test` on bot_test, unset on prod), the ping arrives as `[test] ⚠️ Stale pending row http://example.com/x matched published_articles ...`. On prod (label unset / empty after `.strip()`), no prefix → bare message. This matches every other admin ping in the codebase (outage path at line 1111, recovery ping at line 953). **No code change needed for INSTANCE_LABEL handling — it's automatic.**
- **`parse_mode=None`** (line 383 — passed implicitly via the awaited call). Plain text. No Markdown escaping needed for the URL.

### H.6 `skip_pending` — full body and contract confirmation

File: `/workspaces/debian-2/my-hw/pending_articles_repo.py`, lines 656–686 (full body):

```
656  def skip_pending(link: str) -> None:
657      """Write the link to ``processed_news`` (dedup) and DELETE from pending.
658
659          NO write to ``published_articles`` — skip is not a publish (AC user-spec
660          L74 / tech-spec Decision 2).
661      """
662      conn = _connect()
663      try:
664          src = conn.execute(
665              "SELECT title, pub_date FROM pending_articles WHERE link=?",
666              (link,),
667          ).fetchone()
668          if src is None:
669              return
670          title, pub_date = src
671
672          conn.execute(
673              "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
674              "VALUES (?, ?, ?)",
675              (link, title, pub_date),
676          )
677          conn.execute(
678              "DELETE FROM pending_articles WHERE link=?",
679              (link,),
680          )
681          conn.commit()
682          except Exception:
683              conn.rollback()
684              raise
685          finally:
686              conn.close()
```

**Confirmed:**
- ✅ Writes to `processed_news` via `INSERT OR IGNORE` (line 672–676) — re-fetch is suppressed because the prep phase's `is_processed(link)` filter consults `processed_news`.
- ✅ DELETEs from `pending_articles` (line 677–680).
- ✅ Both in **one transaction** — single `conn.commit()` at line 681. Single `try` / `except`-rollback / `finally`-close.
- ✅ Returns `None` (no `return` value beyond bare `return` on missing-row at line 669; no `return True/False`).
- ✅ **No-op on missing pending row** (lines 668–669) — silent early return, no exception. **This is critical for the guard:** if a concurrent slot/operator already cleaned up the row, `skip_pending` is safe to call.

### H.7 Test patterns — verbatim canonical setUp blocks

#### H.7.1 `tests/test_fallback_publish_paths.py` — `_FallbackPublishPathsCase` (lines 90–123)

Canonical setUp (lines 98–110):
```
 98      def setUp(self):
 99          self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
100          os.close(self.db_fd)
101          self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
102          self.db_patcher.start()
103          news_bot.init_db()
104
105          self.token_patcher = patch('news_bot.TELEGRAM_BOT_TOKEN', 'mock_token')
106          self.channel_patcher = patch('news_bot.TELEGRAM_CHANNEL_ID', '@ch')
107          self.admin_patcher = patch('news_bot.TELEGRAM_ADMIN_ID', '@admin')
108          self.token_patcher.start()
109          self.channel_patcher.start()
110          self.admin_patcher.start()
```

Pre-staging a published row (raw INSERT pattern from `tests/test_hw_review_publish_flow.py:271`):
```python
conn = sqlite3.connect(self.db_path)
try:
    conn.execute(
        "INSERT INTO published_articles "
        "(link, title, ru_title, telegraph_url, telegraph_path, "
        " source_name, via_review) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (link, 'EN', 'РУ', 'https://telegra.ph/OLD-URL', 'OLD-URL',
         'autoevolution', 1))
    conn.commit()
finally:
    conn.close()
```

Mock pattern for "must NOT be called" (from `TestClaudePath`, line 160):
```python
mock_google = MagicMock(side_effect=AssertionError(
    "Google transcreate_text must NOT be called on the Claude path"
))
```

Anthropic-mock pattern (from `TestClaudePath`, line 173):
```python
with patch('news_bot.transcreate_via_claude', mock_claude), \
     patch('news_bot.transcreate_text', mock_google), \
     patch('news_bot.outage_state.is_fallback_active', return_value=False), \
     patch('news_bot.telegraph_publisher.publish_article', mock_publish), \
     patch('news_bot.pending_repo.mark_telegraph_published', mock_mark), \
     patch('news_bot.send_telegraph_teaser', mock_teaser), \
     patch('news_bot.pending_repo.move_to_published', mock_move):
    ok = news_bot._fallback_publish(row, via_review=False)
```

For the new `TestStalePendingMatchesPublished`: same context-manager stack but ALL of `mock_claude`, `mock_publish`, `mock_mark`, `mock_teaser`, `mock_move` use `side_effect=AssertionError("must NOT be called")`. Patch `news_bot.send_admin_notification` with a `MagicMock(return_value=True)` to assert one call after.

#### H.7.2 `tests/test_pending_articles_repo.py` — `_TmpDbCase` (lines 99–128)

Canonical setUp (lines 102–118):
```
102      def setUp(self):
103          fd, self.db_path = tempfile.mkstemp(suffix='.db')
104          os.close(fd)
105          self.db_patcher = patch.object(news_bot, 'DB_FILE', self.db_path)
106          self.db_patcher.start()
107          # Initialize processed_news (existing table) and the three new tables.
108          conn = sqlite3.connect(self.db_path)
109          try:
110              conn.execute(
111                  'CREATE TABLE IF NOT EXISTS processed_news '
112                  '(link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, '
113                  'processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
114              )
115              conn.commit()
116              repo.init_schema(conn)
117          finally:
118              conn.close()
```

Canonical happy-path move test (lines 577–606 — model the new `test_move_to_published_idempotent_when_published_row_exists` on this):
```
577      def test_move_to_published_atomic(self):
578          entry = _sample_entry(link='http://m/1')
579          repo.insert_pending(entry)
580          self._stage(entry)
581
582          repo.move_to_published(
583              entry['link'],
584              telegraph_url='https://telegra.ph/M-04-23',
585              telegraph_path='M-04-23',
586              via_review=True,
587          )
588
589          # pending gone
590          self.assertIsNone(repo.get_pending(entry['link']))
591          # published created
592          pub = repo.get_published(entry['link'])
593          self.assertIsNotNone(pub)
594          self.assertEqual(pub['telegraph_url'], 'https://telegra.ph/M-04-23')
595          ...
600          # processed_news has link
601          with self._conn() as c:
602              row = c.execute(
603                  "SELECT 1 FROM processed_news WHERE link=?",
604                  (entry['link'],),
605              ).fetchone()
606          self.assertIsNotNone(row)
```

`self._stage(entry)` at line 568–575 populates `ru_*` columns so the NOT-NULL `ru_title` constraint on `published_articles` passes.

#### H.7.3 `tests/test_distributed_schedule_integration.py` — `TestDistributedSchedule` setUp (lines 150–229)

Canonical always-on patches (lines 150–197 — abridged to the load-bearing lines):
```
150      def setUp(self):
151          # Tempfile DB.
152          self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
153          os.close(self.db_fd)
154          self.db_patcher = patch('news_bot.DB_FILE', self.db_path)
155          self.db_patcher.start()
156          news_bot.init_db()
...
167          self.notify_patcher = patch('news_bot.send_admin_notification')
168          self.mock_notify = self.notify_patcher.start()
...
173          self.sleep_patcher = patch('news_bot.time.sleep')
174          self.mock_sleep = self.sleep_patcher.start()
...
177          self.teaser_patcher = patch('news_bot.send_telegraph_teaser',
178                                      return_value=True)
179          self.mock_teaser = self.teaser_patcher.start()
180          self.publish_article_patcher = patch(
181              'news_bot.telegraph_publisher.publish_article',
182              return_value='https://telegra.ph/fake-page-04-27',
183          )
184          self.mock_publish_article = self.publish_article_patcher.start()
```

Helpers (lines 258–276):
```
258      def _published_links(self):
259          conn = sqlite3.connect(self.db_path)
...
270      def _pending_links(self):
271          return [r['link'] for r in pending_articles_repo.list_pending()]
272
274      def _admin_messages(self):
275          return [c.args[0] for c in self.mock_notify.call_args_list if c.args]
```

Representative test driver (lines 282–311 — `test_full_happy_path_three_articles_three_slots_three_publishes`, abridged):
```
282      def test_full_happy_path_three_articles_three_slots_three_publishes(self):
...
296          entries = [
297              _create_mock_rss_entry('http://example.com/a1', title='Title 1'),
...
302          self._set_rss_entries(entries)
303
304          with freeze_time('2026-04-27 09:00:00'):  # 09:00 UTC == 12:00 МСК
305              with patch('news_bot.transcreate_via_claude') as mock_claude:
306                  mock_claude.side_effect = [
307                      _make_claude_result(['First paragraph.', 'Second paragraph.'],
308                                          title_prefix=f'Article {i}')
309                      for i in range(1, 4)
310                  ]
311                  news_bot.job()
```

For the new integration test of the guard: pre-seed pending + matching published row via raw INSERT (template at H.7.1), call `news_bot.job()` inside `freeze_time('2026-04-27 09:00:00')`, then assert: `self.mock_teaser.call_count == 1` (only the legitimate fresh row), `len(self._admin_messages())` includes the diagnostic ping, `self._pending_links() == []`, `len(self._published_links()) == 1` (the original published row preserved, NOT a new one).

### H.8 Wave / Task layout for tech-spec

**Wave 1 — implementation + tests (parallel-friendly).** All within a single feature branch. 7 tests total per §G.

| # | File | Touch | Parallel? |
|---|------|-------|-----------|
| W1.T1 | `news_bot.py:985` | Insert guard block (10 lines, see §G snippet) | depends-on: nothing |
| W1.T2 | `pending_articles_repo.py:582` | Single-token edit `INSERT` → `INSERT OR IGNORE` | depends-on: nothing — parallel with T1 |
| W1.T3 | `tests/test_fallback_publish_paths.py` | New class `TestStalePendingMatchesPublished` (~40 LoC) | depends-on: T1 (test asserts the guard's behaviour) |
| W1.T4 | `tests/test_pending_articles_repo.py` | New `test_move_to_published_idempotent_when_published_row_exists` (~25 LoC) | depends-on: T2 |
| W1.T5 | `tests/test_pending_articles_repo.py` | New `test_move_to_published_with_existing_pub_does_not_overwrite_telegraph_url` (~20 LoC) | depends-on: T2; can run in same file as T4 |
| W1.T6 | `tests/test_fallback_publish_paths.py` | New `test_stale_pending_already_in_fallback_path` — covers the `is_fallback_active=True` branch (~30 LoC) | depends-on: T1 |
| W1.T7 | `tests/test_fallback_publish_paths.py` | New `test_stale_pending_admin_notify_failure_is_swallowed` — `send_admin_notification` raises → `skip_pending` still runs, return True (~25 LoC) | depends-on: T1 |
| W1.T8 | `tests/test_distributed_schedule_integration.py` | New `test_stale_pending_in_published_does_not_re_post` integration test (~50 LoC) | depends-on: T1, T2 |
| W1.T9 | `tests/test_pending_articles_repo.py` | New `test_skip_pending_idempotent` regression (verify no behaviour change in `skip_pending` when called from the new guard path) (~20 LoC) | depends-on: nothing — parallel from start |

**Within W1, the parallelisable subgroups are:**
- Group A (source edits): T1, T2 — independent, can be done by 2 developers concurrently or one developer in either order.
- Group B (repo-level tests): T4, T5, T9 — same file, single developer, but new test methods don't conflict line-wise.
- Group C (fallback-path tests): T3, T6, T7 — same file, single developer.
- Group D (integration): T8 — separate file, separate dev.

Recommended ordering for one developer: T1 → T2 → T9 (simple regression) → T4 → T5 → T3 → T6 → T7 → T8. Each test runnable in isolation via `pytest tests/test_<file>.py::TestClass::test_method -v`.

**Wave 2 — audit (3 parallel auditors).**

| Auditor | Scope | Files |
|---------|-------|-------|
| W2.A1 | code-reviewer | `news_bot.py:985-1010` (new guard), `pending_articles_repo.py:582` (single-token diff). Check: clarity, comment quality, error-swallow on admin-ping failure, log-redaction safety, no new instance-state leak. |
| W2.A2 | test-reviewer | All 7 new tests + the existing `test_move_to_published_atomic`/`test_move_to_published_rollback_on_error` for regression-shape unchanged. Check: AAA structure, fixture reuse, `assertEqual` on call_count is brittle vs `assert_called_once_with`, etc. |
| W2.A3 | security-auditor | (a) admin ping payload pass-through (link is user-controllable RSS data — XSS irrelevant in plain-text Telegram, but redact + parse_mode=None already proven); (b) `INSERT OR IGNORE` impact on data-integrity contract (no row corruption, audit-log of via_review preserved); (c) any DB write reordering risk. |

All three run in parallel against the same SHA; outputs aggregated by feature lead before Wave 3.

**Wave 3 — QA + deploy (sequential).**

| # | Step | Owner | Notes |
|---|------|-------|-------|
| W3.S1 | pre-deploy QA | Claude (`/pre-deploy-qa`) | full pytest green-bar (740 tests + 7 new = 747); manual AC walk-through against user-spec.md L74-L92 |
| W3.S2 | dev-branch deploy | operator (push `git push origin dev`) | triggers `ci.yml` → `deploy_test.yml` → `bot_test` (test channel `@myhwchannel123`) |
| W3.S3 | live verification on test instance | operator + Claude | manually inject stale pending row matching a published_articles row via SQL, wait next 12:00 МСК cron tick (or trigger via SIGHUP), verify (a) NO duplicate teaser in test channel, (b) admin ping arrives once, (c) pending row gone afterwards |
| W3.S4 | merge to main + prod deploy | operator (`git merge dev && push origin main`) | triggers `deploy.yml` → prod (channel `-1004027529994`) |
| W3.S5 | post-deploy AVP | Claude (`/post-deploy-qa`) | live-channel checks; failed_articles cleanup SQL (one-time, already in §F.3) |
| W3.S6 | finalise | Claude (`/done`) | archive feature folder, log session work per `feedback_always_log_and_document.md`, update `references/architecture.md` if idempotency contract docs touched |

Wave 3 is strictly sequential — each gate blocks the next. S3 is the highest-signal verification of the fix.
