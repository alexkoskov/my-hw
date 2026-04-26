---
created: 2026-04-26
status: draft
branch: dev
size: L
---

# Tech Spec: llm-transcreation-and-distributed-publishing

## Solution

Replace the auto-fallback path's two collapsed concerns — translation engine and publishing trigger — with a clean two-layer design that mirrors the manual-review path quality:

- **Translation layer:** new `claude_transcreation` module wraps the Anthropic SDK, loads `ux-guidelines.md` as system prompt (the same prompt operator's local Claude uses), produces a JSON response with title (with emoji), 2-3 alts, subtitle, paragraphs/blocks. Per-article failure (refusal, malformed output, 4xx) → fallback to existing `transcreate_text` (Google Translate) for THAT article only. API-level outage (network, 429, 5xx, auth) → state-machine outage protocol with 2 admin pings and 2-hour grace before global Google fallback.
- **Scheduling layer:** cron fires once daily at 12:00 МСК (replacing interval-based `every(12).hours`). After fetch+stage, `compute_publish_slots(N, now, window=13:00–20:00, min_interval=40)` produces N evenly-spaced datetime slots; `posts_today = min(N, 11)` with carry-over to next day. The `job()` function then sleeps between slots and publishes one article per slot via the new translation layer. Container restart mid-window simply re-enters `compute_publish_slots(remaining_pending, now, ...)`. Crash-loop guard reads `MAX(published_at)` on startup and waits for `last_published_at + 40min` before resuming, preventing burst posting under repeated restarts.

Outage state persists in a new tiny SQLite table (`bot_state`, key/value) so admin pings survive container restarts. Schedule itself is in-memory — recomputed on every cron tick + on startup. Legacy code (`_overflow_fast_track`, inline idle-fallback in `job()`, `FALLBACK_THROTTLE_SECONDS` throttle, 4000-char Telegram-era body truncation, bureaucratic regex post-processor, deprecated env vars `QUEUE_CAP`/`IDLE_TIMEOUT_HOURS`/`GRACE_WINDOW_HOURS`) is fully removed — the new model makes them redundant. Manual-review (`hw_review`) path is untouched; operator can preempt any article locally and bot will skip on next tick.

## Architecture

### What we're building/modifying

**New modules:**

- **`claude_transcreation.py`** — Anthropic SDK wrapper. Loads `ux-guidelines.md` once (cached by mtime), composes system prompt + technical envelope (JSON output schema), formats EN article as user message JSON, calls Claude API, parses response, applies HW-glossary post-pass and emoji-prefix safety net. Classifies SDK exceptions into outage vs per-article. ~150 LoC.
- **`compute_publish_slots.py`** — Pure scheduling algorithm. Function `compute_publish_slots(n, now, window_start, window_end, min_interval_min=40) -> (slots, carry_over)`. Edge cases: N=0 (empty schedule), N=1 (single slot at window_start), N exceeds capacity (cap by floor=40, return carry_over). ~50 LoC.
- **`outage_state.py`** — SQLite-backed key/value access for `bot_state` table. State machine helpers `record_outage_event(now)` and `record_recovery_event(now)` are atomic via `BEGIN IMMEDIATE`. ~120 LoC.

**Modified existing:**

- **`news_bot.py`** — `job()` refactor: removes inline idle-fallback and overflow logic; adds crash-loop guard + distributed-publish loop. `_fallback_publish` refactor: tries Claude first (via new module), falls back to Google for per-article problems, raises `OutageError` for API-level. `main()`: cron switches to `schedule.every().day.at("12:00", tz="Europe/Moscow")`. `_TokenRedactingFilter`: extends with `sk-ant-...` pattern for `ANTHROPIC_API_KEY` redaction. `transcreate_text`: bureaucratic regex (19 patterns) and 4000-char truncation removed; HW-glossary (14 patterns) preserved as safety net.
- **`pending_articles_repo.py`** — `init_schema()` adds idempotent `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)`.
- **`requirements.txt`** — adds `anthropic>=0.45.0,<1.0` and `pytz>=2024.1`.
- **`.env.example`** — removes `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS`. Adds `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (optional, default `claude-haiku-4-5`), `TZ=Europe/Moscow`.
- **`deploy.sh`** — adds `ux-guidelines.md` to FILES list (flat path on server).
- **`.github/workflows/deploy.yml`** — same FILES addition + `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `TZ` secrets pass-through.

**Deleted:**

- **`_overflow_fast_track`** (~210 LoC in `news_bot.py`).
- Inline idle-fallback steps 1a/1b in `job()` (~80 LoC).
- `tests/test_overflow.py`, `tests/test_idle_fallback.py`, most of `tests/test_fallback_throttle.py` (~28 tests total).
- `FALLBACK_THROTTLE_SECONDS` use sites in news_bot.py (~3 use sites).
- Bureaucratic regex (19 patterns) in `transcreate_text`.

**New tests:**

- `tests/test_compute_publish_slots.py` — ~12 tests for scheduling algorithm edge cases.
- `tests/test_claude_transcreation.py` — ~8 tests for SDK wrapper, mocked anthropic.
- `tests/test_outage_state.py` — ~6 tests for state machine + persistence + concurrency.
- `tests/test_distributed_schedule_integration.py` — ~4 tests for full flow.
- Updates to `tests/test_no_token_leak_in_logs.py`, `tests/test_migration.py`, `tests/test_integration.py`.

### How it works

**Daily flow on the production VPS:**

1. **12:00 МСК** — `schedule.every().day.at("12:00", tz="Europe/Moscow")` triggers `job()`. The `tz` argument requires `pytz` (verified via `inspect.getsource(schedule.Job.at)` — stdlib `zoneinfo` is rejected with TypeError).
2. **Crash-loop guard** — `job()` reads `MAX(published_at)` from `published_articles`. If `now - last_published < 40min`, sleeps until that gap elapses before continuing. This protects the channel against burst posting under systemd/cron rapid-restart loops.
3. **Fetch sources** — autoevolution + lamley + mattel via the existing `SOURCES` registry. Boilerplate filter, image policy, dedup unchanged.
4. **Insert pending** — same path as today.
5. **Compute schedule** — `compute_publish_slots(count_pending(), now_msk)` returns `(slots, carry_over)`. With floor=40, `posts_today = min(N, 11)`.
6. **Admin ping** — Telegram message to operator with the day's plan: "Fetched N, scheduled M today at 13:00, 13:40, ..., 19:40. Carry-over: K." Suppress on N=0.
7. **Publish loop** — for each slot in `slots`:
   - `time.sleep((slot - now).total_seconds())` until slot arrives.
   - Pop oldest pending row (`list_pending()[0]`).
   - Check outage state: if `is_fallback_active()`, route directly to Google Translate path; otherwise call `_fallback_publish` (Claude primary, Google per-article fallback).
   - On `OutageError` (state-machine signal): the state machine already recorded the event and sent admin ping; this article was already routed through Google as a side effect. Continue to next slot.
   - On unexpected exception: standard 3-strikes attempt counter → `move_to_failed`.

**Outage state machine:**

States: `no_outage`, `ping_1_sent`, `ping_2_sent`, `google_fallback_active`, `recovery_pending` (transient).

API-level errors (network, timeout, 429, 5xx, auth, model-not-found) advance the state machine. Per-article errors (400, 422, malformed JSON, refusal) do NOT — they just route THIS article through Google and leave the state untouched. The state machine sends admin pings #1 (immediately on first outage), #2 (after 1h), #3 (switching to Google fallback after 2h), and recovery (when Claude succeeds again). Storage in `bot_state` table — survives container restart.

**Container restart mid-window:**

On startup, `news_bot.main()` triggers `job()` immediately (existing pattern). Crash-loop guard kicks in if needed. `compute_publish_slots(remaining_pending, now)` recomputes the schedule for the rest of the window — no migration of old slots. `published_articles` already-published rows are skipped via Decision 9 idempotency from manual-review-workflow (telegraph_url presence).

**Manual-review path (unchanged):**

Operator's local `hw_review` continues to work as before. If operator publishes article N locally between cron and the auto slot, the bot's `list_pending()` filter (where `ru_paragraphs IS NULL`) will skip it. Channel teaser format `#<source> #news` is byte-identical between paths (Decision 14 from manual-review-workflow).

### Shared resources

| Resource | Owner | Consumers | Instance count |
|----------|-------|-----------|----------------|
| `anthropic.Anthropic` client | `claude_transcreation` (module init) | `transcreate_via_claude` | 1 (singleton) |
| `pytz.timezone('Europe/Moscow')` | `news_bot.main` + `claude_transcreation` | scheduling, logging | 1 (constant) |
| Compiled regex (HW glossary, `_FLIGHT_PUSH_RE`, etc) | various modules | their owners | 1 each (module-level) |
| SQLite `news.db` | `pending_articles_repo`, `outage_state` | cron + hw_review CLI | 1 file, default journal mode, `BEGIN IMMEDIATE` for outage state machine |

No new heavy resources introduced. The Anthropic client is lightweight (single HTTP session); `ux-guidelines.md` is loaded once into a module-level string variable.

## Decisions

### Decision 1: Claude API as primary, Google Translate as per-article fallback only

**Decision:** Replace `transcreate_text` (Google Translate) with `claude_transcreation.transcreate_via_claude` in the auto-fallback path. Keep `transcreate_text` for per-article fallback when Claude refuses/malforms a single article, and for global fallback during API-level outages.

**Rationale:** Supports user-spec AC9 (Claude API as primary translator with ux-guidelines prompt) and user-spec story 1 (subscriber-quality parity between manual and auto paths). Google Translate quality is observably inferior — operator confirmed this from comparison demos earlier. Cost (~$3/мес at Haiku 4.5 + 10 articles/day) is negligible.

**Alternatives considered:**
- Replace Google Translate entirely with no fallback — rejected (single point of failure, user-spec explicitly preserves Google as fallback path).
- Use Claude Sonnet by default — rejected (5x cost, Haiku quality verified sufficient via manual-review demos).

### Decision 2: Distributed publishing schedule with fixed 12:00 МСК cron + 13:00–20:00 МСК window + max(420/N, 40) interval

**Decision:** Cron switches to `schedule.every().day.at("12:00", tz="Europe/Moscow").do(job)`. After fetch, compute `slots = compute_publish_slots(N, now, 13:00, 20:00, min_interval_min=40)`. Publish one article per slot via `time.sleep` between iterations. `posts_today = min(N, 11)` with carry-over.

**Rationale:** Supports user-spec AC1–AC6. The 13:00–20:00 МСК prime-audience window matches operator's stated subscriber demographic; floor=40 prevents spammy bursts; cap of 11/day is the natural consequence of `(20:00 - 13:00) / 40min + 1`.

**Alternatives considered:**
- Hourly cron with single-article drain — rejected by operator earlier in dialogue (preferred batched fetch + distributed publish).
- Active scheduler (apscheduler with persistent jobstore) — rejected (overkill; in-memory recompute on restart is bounded delay).

### Decision 3: In-memory schedule, persistent outage state in new `bot_state` SQLite table

**Decision:** Schedule is recomputed on every cron tick and on container startup — never persisted to disk. Outage state (timestamps + counters) lives in a new `bot_state(key, value)` SQLite table. Idempotent `CREATE TABLE IF NOT EXISTS` migration in `pending_articles_repo.init_schema()`.

**Rationale:** Supports user-spec AC7 (container restart recompute), AC11–AC13 (outage protocol survives restart), AC24 (idempotent SQLite migration). Schedule needs no persistence because the inputs (`pending_articles` rows + current time) fully determine it. Outage state DOES need persistence because the 2-hour grace window can span container restarts.

**Alternatives considered:**
- Persist schedule to DB — rejected (schema migration overhead for trivial gain; recompute is sub-millisecond).
- File-based outage state (e.g. JSON in `/tmp`) — rejected (not atomic, lost on `/tmp` cleanup).

### Decision 4: pytz for TZ handling, not stdlib zoneinfo

**Decision:** [TECHNICAL] Add `pytz>=2024.1` to `requirements.txt`. Use `pytz.timezone('Europe/Moscow')` everywhere. Set `TZ=Europe/Moscow` in `.env` for log readability + naive `datetime.now()` consistency.

**Rationale:** `schedule==1.2.1` `Job.at(time_str, tz=...)` accepts only `pytz.BaseTzInfo` or string IANA name — stdlib `zoneinfo.ZoneInfo` raises TypeError (verified by reading `inspect.getsource(schedule.Job.at)`). Without this dep, the cron trigger required by user-spec AC1 wouldn't work as specified. User-spec describes TZ handling at constraint level only; pytz is the implementation forced by the existing scheduler dep.

**Alternatives considered:**
- Schedule in UTC equivalent (`at("09:00")` since MSK = UTC+3 always) — rejected (brittle if Russia ever reintroduces DST).
- Switch to apscheduler with native zoneinfo — rejected (heavier dep, not worth it for a single cron trigger).

### Decision 5: Per-article vs API-level error classification (mapped from anthropic SDK exceptions)

**Decision:** API-level (advances outage state machine): `APIConnectionError`, `APITimeoutError`, `RateLimitError` (429), `InternalServerError` (5xx), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (model 404). Per-article (single-article Google fallback, no state change): `BadRequestError` (400), `UnprocessableEntityError` (422), `APIStatusError` for unrecognized codes, `ClaudeTranscreationError` (parse failure of model response).

**Rationale:** Supports user-spec AC14 (API-level → 2-ping protocol) and AC15 (per-article → fallback for that article only). Misclassification would either cause false outage state (one weird article triggers global Google fallback for hours) or false article skip (auth failure quietly translates one article via Google but never alerts admin). The boundary maps cleanly to anthropic SDK's exception hierarchy.

**Alternatives considered:**
- Treat all 4xx as per-article — rejected (auth/permission failures need admin alerting; quietly serving Google translation hides config bugs).
- Treat all anthropic errors as outage — rejected (a single refusal/malformed-JSON shouldn't take the channel offline).

### Decision 6: System prompt = ux-guidelines.md verbatim + JSON envelope, model = Haiku 4.5

**Decision:** System prompt body = full `ux-guidelines.md` content (~3200 tokens) + a small "Output format" envelope appended after a horizontal rule, requiring JSON output with strict schema (title, alts[2-3], subtitle, paragraphs[N=input length], blocks?). Default `ANTHROPIC_MODEL=claude-haiku-4-5`. User message = JSON-shaped article (source_name, title, subtitle, paragraphs, blocks). Block API call (no streaming). SDK default retries (`max_retries=2`). No prompt caching (40+ min between slots > 5-min cache TTL).

**Rationale:** Supports user-spec AC9 (same prompt as manual-review path). Operator confirmed Haiku quality is sufficient earlier in dialogue. JSON envelope is mandatory because ux-guidelines targets human-readable Telegram-paste format; without an explicit envelope, parsing is fragile. Block call simplifies parsing vs streaming. Skipping prompt caching saves complexity for zero practical gain at our slot frequency.

**Alternatives considered:**
- Sonnet 4.6 default — rejected (5x cost, quality margin not necessary at this scale).
- Markdown response parsing (no JSON envelope) — rejected (brittle title/subtitle/paragraph extraction from prose).
- Prompt caching — rejected (slots > TTL; would require batched translation at fetch time, conflicting with manual-review preemption).

### Decision 7: ux-guidelines.md becomes runtime cron-side dependency

**Decision:** Ship `.claude/skills/project-knowledge/references/ux-guidelines.md` to the production server as part of the deploy bundle. Add to `deploy.sh` FILES list and `.github/workflows/deploy.yml` files block. The `claude_transcreation._load_prompt` reads the file at module import (cached by mtime) and uses its content as the Claude system prompt.

**Rationale:** Supports user-spec AC27 + AC28 (ux-guidelines.md added to deploy bundle; architectural shift documented). Inverts the prior architecture.md statement that "hw_review.py + preview_renderer.py + ux-guidelines.md are operator-side only." Operator confirmed this shift in interview question 9.

**Alternatives considered:**
- Hardcode the prompt body in `claude_transcreation.py` — rejected (duplicate source of truth; updates to prompt would require code change + redeploy instead of file edit).
- Fetch from a hosted URL at runtime — rejected (added network dependency, latency on cold start, risk of 4xx during outage).

### Decision 8: Flat-path fallback for ux-guidelines.md on server (deploy quirk)

**Decision:** [TECHNICAL] `claude_transcreation._load_prompt` tries the original subdir path (`.claude/skills/project-knowledge/references/ux-guidelines.md`) first, then falls back to the flat filename in `DEPLOY_PATH/ux-guidelines.md`. The deploy script uses `scp` which flattens subdirs by default — the file ends up at `DEPLOY_PATH/ux-guidelines.md` on the server, not at the subdir path.

**Rationale:** Code-research §14.10 risk #12. The alternative (tarball with subdirs preserved) adds operator complexity for marginal benefit; the flat-path fallback is a 5-line change in the loader.

**Alternatives considered:**
- Tarball + untar on server side preserving subdirs — rejected (operator-side complexity, fragile shell logic).
- Hardcode flat-only path — rejected (breaks local development where the subdir is the natural location).

### Decision 9: Crash-loop guard via `MAX(published_at)` check on startup

**Decision:** At the start of `job()`, before fetch+publish, read `MAX(published_at)` from `published_articles`. If `now - last_published < MIN_INTERVAL_MINUTES (40)`, sleep until `last_published + 40min` before proceeding. This protects the channel from burst posting under systemd/Docker restart loops.

**Rationale:** Supports user-spec AC8 (crash-loop guard). Without it, a bot crashing every 30 seconds and being restarted by systemd would publish a new article on each restart — a pathological burst pattern. The guard is cheap (one SELECT) and bounded (sleep ≤ 40min once).

**Alternatives considered:**
- Use the existing throttle mechanism (`FALLBACK_THROTTLE_SECONDS`) — rejected (we're deleting throttle as legacy in this feature).
- Track restart count separately — rejected (overengineered; the `published_at` timestamp is the canonical signal we care about).

### Decision 10: 13 atomic tasks across 6 waves

**Decision:** [TECHNICAL] Decompose implementation into 13 tasks organized in 6 waves to maximize parallelism while honoring dependencies. Wave 1 (parallel foundations): tasks 1–4. Wave 2 (parallel, depend on Wave 1): tasks 5, 6. Wave 3: task 7 (the `_fallback_publish` refactor). Wave 4 (sequential — same touch point in news_bot.py): tasks 8, 9, 10. Wave 5 (parallel): tasks 11, 12. Wave 6: task 13 (final deploy bundle update). Plus Audit Wave (3 parallel auditors) and Final Wave (QA, deploy, post-deploy verification).

**Rationale:** Code-research §14.9. The most dependency-heavy task is task 8 (job() refactor) — depends on tasks 5 (outage_state), 2 (compute_publish_slots), 7 (_fallback_publish refactor) being complete. Tasks 8→9→10 are sequenced sequentially despite touching the same file because legacy deletes (9) and bureaucratic-regex strip (10) need a clean refactor (8) underneath.

## Data Models

**No new external data models.** Article/entry/page shapes (5-key entry dict, 4-key article dict from parsers, Telegraph node tree) all unchanged.

**One new internal SQLite table:**

```sql
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Active keys (all values stored as ISO-8601 strings or `'0'`/`'1'`/`'2'` for counters/flags):

- `outage_started_at` — ISO timestamp when first Claude outage error fired. NULL = no active outage.
- `last_ping_sent_at` — ISO timestamp when the last admin ping (#1, #2, or recovery) was sent.
- `ping_count` — `'1'` after ping #1, `'2'` after ping #2, `'3'` after fallback-switch ping.
- `fallback_active` — `'1'` if Google Translate is the active engine, else NULL.
- `last_health_check_at` — ISO timestamp of the most recent Claude probe attempt during recovery_pending state. Rate-limits probes.

Reading a missing key returns Python `None`. Setters use `INSERT OR REPLACE`. State-machine helpers (`record_outage_event`, `record_recovery_event`) wrap their reads+writes in `BEGIN IMMEDIATE` transactions to prevent two concurrent fallback-publish callers from double-incrementing `ping_count`.

## Dependencies

### New packages

- `anthropic>=0.45.0,<1.0` — Anthropic Python SDK. Used by `claude_transcreation`. Version pinned to lock the exception class hierarchy referenced in Decision 5.
- `pytz>=2024.1` — IANA timezone library. Required by `schedule==1.2.1` for `Job.at(tz=...)`. Cannot use stdlib `zoneinfo` (verified — TypeError).

### Using existing (from project)

- `requests==2.32.3` — Telegraph API + Telegram Bot API + source HTTP fetches.
- `beautifulsoup4==4.12.3` — body HTML parse for autoevolution / lamley / mattel.
- `feedparser` — RSS parsing.
- `deep-translator` — Google Translate fallback (kept for per-article and global Google fallback paths).
- `python-telegram-bot` — channel teaser + admin pings.
- `schedule==1.2.1` — daily cron trigger at fixed time.
- `python-dotenv` — `.env` loading on import (already added in feature `mattel-parser-rewrite`).
- stdlib `re`, `json`, `time`, `logging`, `typing`, `datetime`, `os`, `sqlite3`, `pathlib`.

## Testing Strategy

**Feature size:** L (13 tasks, ~290 LoC removal, ~58 test churn, 4 new test files, 3 new modules).

### Unit tests

**`tests/test_compute_publish_slots.py`** (~12 tests):
- N=0 → empty slots, carry_over=0.
- N=1 → single slot at 13:00, carry_over=0.
- N=4 → 4 slots evenly spaced (interval = 105 min), carry_over=0.
- N=7 → 7 slots, hourly (interval = 60 min), carry_over=0.
- N=10 → 10 slots, interval = 42 min, carry_over=0.
- N=11 → 11 slots, capped at floor=40 min, carry_over=0.
- N=15 → 11 slots (capped), carry_over=4.
- N=20 → 11 slots, carry_over=9.
- Container restart mid-window (now=16:00, N=5) → slots from 16:00 with interval 48 min.
- Container restart at 19:50 (only 10 min left) → 1 slot at 19:50, rest carry_over.
- TZ-naive datetime input → ValueError (assertion).
- Window edges: now < window_start → first slot at window_start; now > window_end → empty slots, all carry_over.

**`tests/test_claude_transcreation.py`** (~8 tests, mocked anthropic SDK):
- Success path: returns dict with title (with emoji), 2-3 alts, subtitle, paragraphs (count matches input).
- `RateLimitError` → raises `OutageError`.
- `AuthenticationError` → raises `OutageError`.
- `APIConnectionError` → raises `OutageError`.
- `APITimeoutError` → raises `OutageError`.
- `BadRequestError` → raises `ClaudeTranscreationError` (per-article fallback signal).
- Malformed JSON output → `ClaudeTranscreationError`.
- Title without emoji prefix → regex wrapper adds emoji as safety net.

**`tests/test_outage_state.py`** (~6 tests):
- `record_outage_event` from `no_outage` → `ping_1_sent`, returns ping list.
- After 1h elapsed in `ping_1_sent` + new outage → `ping_2_sent`, second ping.
- After 2h elapsed in `ping_2_sent` + new outage → `google_fallback_active`, third ping.
- `record_recovery_event` from any active state → `no_outage`, recovery ping.
- Persistence: write state, simulate container restart (new connection), read state — values match.
- Concurrent writers (two threads each call `record_outage_event`) → `BEGIN IMMEDIATE` serializes; ping_count incremented exactly once.

**`tests/test_distributed_schedule_integration.py`** (~4 tests, end-to-end with mocks):
- Full happy path: cron tick at 12:00 with mocked sources fetching N=3 → 3 slots scheduled at 13:00/15:20/17:40 → 3 publishes via mocked Claude → DB delta correct.
- Outage during slot 2: mocked `RateLimitError` → state advances to `ping_1_sent`, slot 2 article goes via Google, slot 3 retries Claude → recovers → recovery ping.
- Container restart mid-window: simulate startup at 16:00 with 2 unpublished pending → slots recomputed (16:00, 18:00) → publishes proceed.
- Manual-review preemption: operator publishes pending row 2 between fetch and slot 2 → bot's `list_pending` filter skips it, slot 2 takes row 3 instead.

### Integration tests

`tests/test_integration.py` and `tests/test_job_prep_phase.py` get updated patches for the new auto-publish path:
- Patch targets shift from `news_bot.transcreate_text` to the new claude path.
- New assertions on `bot_state` table contents after outage scenarios.
- Removed assertions about `_overflow_fast_track` / `_idle_fallback_publish` (deleted).

### E2E tests

None — would require live Anthropic API calls (cost) and live Telegraph/Telegram (rate limits + channel pollution). Manual smoke is the substitute (see Agent Verification Plan).

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Per-task `Verify-smoke` runs concrete commands during implementation; Final-Wave QA replays the full suite + runs end-to-end smoke against staging or live test channel after deploy. The auditors in the Audit Wave write reports only — they don't fix.

Core verification commands (from user-spec AC30, AC31):

1. `pytest tests/ -q` — full suite green.
2. `pytest tests/test_compute_publish_slots.py tests/test_claude_transcreation.py tests/test_outage_state.py tests/test_distributed_schedule_integration.py -v` — targeted new tests.
3. Pre-deploy manual smoke: invoke the new claude-transcreation module with a sample article and verify it returns a valid RU dict in <30 seconds.
4. Post-deploy manual smoke: at 12:00 МСК first cron tick after deploy, admin ping arrives; at 13:00 МСК first publication appears in test channel with emoji-title, no boilerplate, and `↳ автоперевод` marker before the source footer in the Telegraph page.
5. Outage drill (optional, operator-supervised): temporarily remove `ANTHROPIC_API_KEY` from `.env`, observe ping #1, ping #2 after 1h, switch to Google after 2h. Restore key, observe recovery ping on next slot.

Operator-side post-deploy verification window:
- 12+ hours after deploy: no parsing-error or OutageError messages in admin chat during normal operation.
- 7-day window: monitor channel quality, confirm no Gemini-style fallback posts during normal operation, confirm pending queue doesn't grow unbounded.

### Tools required

`bash`, `pytest`, `python3`. No MCP tools required (backend service, no UI).

## Risks

| Risk | Mitigation |
|------|-----------|
| `_fallback_publish` refactor breaks Decision 9 idempotency from manual-review-workflow (Telegraph URL persisted before Telegram send) — high-touch function called from multiple sites | Keep steps 2–5 (Telegraph + persist + Telegram + move) untouched; only step 1 (transcreate) changes. Add dedicated `test_fallback_publish_claude_path` + `test_fallback_publish_google_fallback_path` to lock both branches before refactor merges. |
| `news_bot.py` refactor (task 8) breaks ~470 existing tests via removal of `_overflow_fast_track`, throttle, and old env vars | Sequence task 8 strictly after tasks 5, 2, 7 complete. Run full test suite incrementally — fix red, commit, advance. Don't combine with task 9 (legacy deletes) in same PR. |
| `scp file1 file2 .claude/skills/.../ux-guidelines.md host:dest/` flattens subdirs — file ends up at `dest/ux-guidelines.md`, not at expected path | Decision 8: `_load_prompt` tries subdir path first, falls back to flat path. Document in `deployment.md`. |
| Operator inattention to outage pings → bot silently runs Google Translate for hours/days | Switch-back ping when Claude API recovers gives operator visible signal. 24-hour cron cycle guarantees Claude is probed at least once per day. Acceptable per user-spec story 3. |
| Cost spike if a source loops and produces 100+ articles/day | Algorithm caps at 11 publishes/day; pending grows. AC20 admin warning at `len(pending) > 50` flags the problem. Operator can manually purge or fix the source. |
| Anthropic SDK exception class hierarchy changes between minor versions | Pin `anthropic>=0.45.0,<1.0`. Tests in `test_claude_transcreation.py` cover each exception branch — version bump that breaks classification will be caught by CI. |
| `pytz` deprecation (long-term) — Python community moving to stdlib `zoneinfo` | Acceptable now (`schedule==1.2.1` requires it). When `schedule` adds zoneinfo support, swap. Track in roadmap. |
| `BEGIN IMMEDIATE` deadlock between `news_bot` cron and `hw_review` CLI under heavy concurrent load | Both writers are short-lived (sub-50ms typical). 5-second `busy_timeout` absorbs contention. `outage_state` writes touch a different table from `pending_articles` writes — null logical conflict. |

## User-Spec Deviations

None of the technical decisions contradict user-spec requirements. All Decisions either implement a specific user-spec AC or are marked `[TECHNICAL]` for implementation-level choices the user-spec doesn't constrain:

- **Decision 4 (pytz):** [TECHNICAL] — user-spec only mandates "TZ-aware schedule library OR UTC-equivalent" (Constraints); pytz is the implementation choice forced by the existing `schedule==1.2.1` dependency.
- **Decision 8 (flat-path fallback):** [TECHNICAL] — addresses a deploy-mechanism quirk; user-spec doesn't constrain how `ux-guidelines.md` is shipped, only that it must be present at runtime.
- **Decision 10 (13 tasks across 6 waves):** [TECHNICAL] — user-spec doesn't specify decomposition granularity; tech-spec optimizes for parallelism + clean PR boundaries.

All other decisions trace to specific user-spec ACs.

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] `requirements.txt` includes `anthropic>=0.45.0,<1.0` and `pytz>=2024.1`.
- [ ] `.env.example` lists `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (optional), `TZ=Europe/Moscow`, and removes the four legacy env vars.
- [ ] `pending_articles_repo.init_schema()` creates `bot_state` table idempotently. Migration test asserts the table+columns+PK shape.
- [ ] `news_bot._TokenRedactingFilter` redacts `sk-ant-[A-Za-z0-9_-]{20,}` patterns. Regression test covers a real-shaped key.
- [ ] `_overflow_fast_track`, inline idle-fallback in `job()`, `FALLBACK_THROTTLE_SECONDS`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS` are deleted from `news_bot.py`. No remaining references via grep.
- [ ] `transcreate_text` no longer truncates at 4000 chars; bureaucratic regex (19 patterns) deleted; HW glossary (14 patterns) preserved.
- [ ] Cron uses `schedule.every().day.at("12:00", tz="Europe/Moscow")`. Startup logs show MSK time.
- [ ] `claude_transcreation.transcreate_via_claude` produces valid RU dict for sample article in <30s.
- [ ] `pytest tests/ -q` zelёный after refactor (existing ~470 + 4 new test files - 28 deleted = ~480-490 tests).
- [ ] Manual smoke: outage drill (remove API key) reproduces 2-ping protocol + Google fallback after 2h, with state persisting across container restart.
- [ ] All AC1–AC31 from user-spec verifiable via the verification plan commands above.

## Implementation Tasks

### Wave 1 (parallel — independent foundations)

#### Task 1: Add `bot_state` migration
- **Description:** Add `bot_state` DDL constant + extend `init_schema()` in `pending_articles_repo.py` to include the new key/value table. Update `tests/test_migration.py` to assert table+columns+PK shape. Idempotent CREATE TABLE IF NOT EXISTS.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `pending_articles_repo.py`, `tests/test_migration.py`
- **Files to read:** `tests/test_migration.py`

#### Task 2: Create `compute_publish_slots.py` + tests
- **Description:** Pure scheduling algorithm module. Function `compute_publish_slots(n, now, window_start, window_end, min_interval_min=40) -> (slots, carry_over)`. ~12 unit tests covering N=0..30, container restart mid-window, TZ-naive ValueError. No external dependencies — pure stdlib.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `compute_publish_slots.py` (new), `tests/test_compute_publish_slots.py` (new)
- **Files to read:** user-spec.md (AC1–AC8 for edge case examples), code-research.md §14.2

#### Task 3: Create `claude_transcreation.py` + tests
- **Description:** Anthropic SDK wrapper module. Loads `ux-guidelines.md` (mtime-cached), composes system prompt + JSON envelope, calls Claude API (block, no streaming), parses response, applies HW glossary post-pass and emoji-prefix safety. Classifies SDK exceptions per Decision 5. ~8 mocked anthropic tests. Includes flat-path fallback (Decision 8) for ux-guidelines.md location.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** invoke transcreate_via_claude with sample article in dev container — valid dict response under 30s
- **Files to modify:** `claude_transcreation.py` (new), `tests/test_claude_transcreation.py` (new)
- **Files to read:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `news_bot.py` `transcreate_text` (legacy reference), code-research.md §14.3

#### Task 4: Extend `_TokenRedactingFilter` for ANTHROPIC_API_KEY
- **Description:** Add `'ANTHROPIC_API_KEY'` to `_SECRET_ENV_NAMES`; add anthropic-key regex pattern. Extend `tests/test_no_token_leak_in_logs.py` with anthropic-key fixture. Verify both env-var name and inline-key patterns get redacted.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor
- **Files to modify:** `news_bot.py` (`_TokenRedactingFilter` and `_SECRET_ENV_NAMES`), `tests/test_no_token_leak_in_logs.py`
- **Files to read:** `news_bot.py` (token redaction section)

### Wave 2 (parallel — depend on Wave 1)

#### Task 5: Create `outage_state.py` + tests
- **Description:** SQLite-backed key/value access for `bot_state` table. Public API: simple getters/setters for each key + state-machine helpers `record_outage_event(now)` and `record_recovery_event(now)`. Atomic via `BEGIN IMMEDIATE`. ~6 tests covering each state transition + persistence + concurrency.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `outage_state.py` (new), `tests/test_outage_state.py` (new)
- **Files to read:** `pending_articles_repo.py` (connection pattern), code-research.md §14.4 (state machine)

#### Task 6: Update `requirements.txt` + `.env.example`
- **Description:** Pin new dependencies: `anthropic>=0.45.0,<1.0` and `pytz>=2024.1`. Update `.env.example`: remove `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS`. Add `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (optional, commented as default `claude-haiku-4-5`), `TZ=Europe/Moscow`. Document each new var inline.
- **Skill:** code-writing
- **Reviewers:** code-reviewer
- **Files to modify:** `requirements.txt`, `.env.example`
- **Files to read:** `.env` (current shape, for reference)

### Wave 3 (depends on Wave 2)

#### Task 7: Refactor `_fallback_publish` for Claude primary + Google per-article fallback
- **Description:** Replace `transcreate_text` calls in `_fallback_publish` with `claude_transcreation.transcreate_via_claude`. On per-article exception (`ClaudeTranscreationError`), fall back to `transcreate_text` for THAT article only. On API-level outage exception (`OutageError`), call `outage_state.record_outage_event(now)` for state transition + admin ping; bubble up to caller. Preserve Decision 9 idempotency from manual-review-workflow (Telegraph URL persisted before Telegram send). Add 2 dedicated tests — one for each branch.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py` (`_fallback_publish`), `tests/test_fallback_publish_paths.py` (new)
- **Files to read:** `news_bot.py` (current `_fallback_publish`), `claude_transcreation.py`, `outage_state.py`

### Wave 4 (sequential — all touch news_bot.py)

#### Task 8: Refactor `job()` for distributed-publish loop + cron change
- **Description:** Replace `schedule.every(12).hours.do(job)` with `schedule.every().day.at("12:00", tz="Europe/Moscow").do(job)`. Refactor `job()`: add crash-loop guard (read `MAX(published_at)`, sleep if needed), call `compute_publish_slots(count_pending(), now_msk)`, send admin ping with the day's plan, run distributed-publish loop with `time.sleep` between slots. Each slot: try `_fallback_publish`; on `OutageError` continue (state machine handled it); on unexpected exception 3-strikes the row.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `news_bot.py` (`job()`, `main()`)
- **Files to read:** `news_bot.py` (current `job()`, `main()`), `compute_publish_slots.py`, `outage_state.py`

#### Task 9: Delete legacy code (`_overflow_fast_track`, idle-fallback, throttle, env vars)
- **Description:** Remove `_overflow_fast_track` function from `news_bot.py`. Remove inline idle-fallback step 1a/1b from `job()` (now redundant with distributed schedule). Remove `FALLBACK_THROTTLE_SECONDS` env var read + use sites. Remove `QUEUE_CAP` env var + use sites. Remove `IDLE_TIMEOUT_HOURS` and `GRACE_WINDOW_HOURS`. Delete `tests/test_overflow.py`, `tests/test_idle_fallback.py`, throttle-specific tests in `tests/test_fallback_throttle.py`. Verify `pytest tests/ -q` green after deletes.
- **Skill:** code-writing
- **Reviewers:** code-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_overflow.py` (delete), `tests/test_idle_fallback.py` (delete), `tests/test_fallback_throttle.py` (trim)
- **Files to read:** `news_bot.py` (current state)

#### Task 10: Strip bureaucratic regex + 4000-char truncation from `transcreate_text`
- **Description:** Remove the 19-pattern bureaucratic post-process regex from `transcreate_text` (Claude doesn't write canceleristic Russian, so this is dead weight in the Google fallback path). Remove the 4000-char body truncation (Telegraph has no length cap; the truncation was a Telegram-era vestige). Preserve the 14-pattern HW glossary (brand-specific phrasing safety net). Update or trim relevant tests in `tests/test_translation.py`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `news_bot.py` (`transcreate_text`), `tests/test_translation.py`
- **Files to read:** `news_bot.py` (current `transcreate_text`)

### Wave 5 (parallel — depend on Wave 4)

#### Task 11: Update integration tests for new auto-publish path
- **Description:** Update `tests/test_integration.py` and `tests/test_job_prep_phase.py`: shift patch targets from `news_bot.transcreate_text` to the new claude path; add assertions on `bot_state` table contents in outage scenarios; remove assertions about deleted symbols (`_overflow_fast_track`, throttle). Verify integration coverage of: outage, recovery, container restart, manual-review preemption.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `tests/test_integration.py`, `tests/test_job_prep_phase.py`
- **Files to read:** these files (current state), `news_bot.py` (post-refactor)

#### Task 12: Create `tests/test_distributed_schedule_integration.py`
- **Description:** End-to-end integration tests for the new distributed schedule. ~4 tests covering: full happy path (3 articles → 3 slots → 3 publishes), outage during slot mid-day (state advances + Google fallback + recovery on next slot), container restart mid-window (recompute slots + continue), manual-review preemption (operator publishes pending row, bot skips it on next slot). All external dependencies mocked: anthropic SDK, telegraph, telegram, time.sleep, schedule.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `tests/test_distributed_schedule_integration.py` (new)
- **Files to read:** code-research.md §14.5 (publish loop pseudocode), other integration tests for fixture patterns

### Wave 6 (final — deploy bundle update)

#### Task 13: Update deploy bundle (deploy.sh + GitHub Actions + PK docs)
- **Description:** Add `ux-guidelines.md` to `deploy.sh` FILES list (lands flat on server — Decision 8 fallback handles this). Add same file to `.github/workflows/deploy.yml` files block. Add `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TZ` secrets to deploy workflow (write to .env on server). Update PK files: `architecture.md` (note ux-guidelines.md is now cron-side runtime dep + new modules), `patterns.md` (new auto-publish pattern, removed throttle/overflow patterns), `deployment.md` (new env vars + setup notes for Anthropic console + cost monitoring).
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** dry-run deploy to staging path: copy FILES list manually with `scp`, verify ux-guidelines.md lands at expected location; verify env file has new vars
- **Files to modify:** `deploy.sh`, `.github/workflows/deploy.yml`, `.claude/skills/project-knowledge/references/architecture.md`, `.claude/skills/project-knowledge/references/patterns.md`, `.claude/skills/project-knowledge/references/deployment.md`
- **Files to read:** current versions of all the above; user-spec.md (Constraints section for env var list); code-research.md §14.10 (deploy quirk)

### Audit Wave

#### Task 14: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified across tasks 1–13. Focus areas: (1) `_fallback_publish` refactor preserves Decision 9 idempotency from manual-review-workflow, (2) `claude_transcreation` properly classifies SDK exceptions per Decision 5, (3) `outage_state` state machine logic matches the transition table, (4) `compute_publish_slots` algorithm matches user-spec AC3+AC4+AC5, (5) no leftover references to deleted symbols, (6) Decision 8 flat-path fallback present in `_load_prompt`, (7) `news_bot.py` refactor doesn't accidentally break the manual-review path's call sites. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 15: Security Audit
- **Description:** Full-feature security audit. Read all source files created/modified. Cover OWASP-relevant concerns: (1) `ANTHROPIC_API_KEY` redaction in logs (no leak via Anthropic SDK error messages, exception text, retry warnings), (2) `claude_transcreation` system prompt construction — no prompt injection attack surface from the article body content, (3) SQLite `BEGIN IMMEDIATE` deadlock potential between cron + hw_review writers, (4) `bot_state` schema doesn't introduce SQL injection vector (parameterized queries), (5) deploy bundle changes don't expose secrets in workflow YAML, (6) Anthropic API call doesn't leak environment data in error responses. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 16: Test Audit
- **Description:** Full-feature test quality audit. Read all test files created/modified across tasks 1–13. Verify: (1) test count delta matches plan (~28 deleted, ~30 added), (2) all user-spec ACs (AC1–AC31) traceable to at least one test, (3) outage state machine tests cover each transition + edge case, (4) crash-loop guard tested with `MAX(published_at) < 40min` ago, (5) `compute_publish_slots` tests cover N=0, 1, 11, 12, 30 boundary conditions, (6) `claude_transcreation` tests cover each anthropic SDK exception class branch, (7) integration tests don't rely on real network/API calls, (8) no test references deleted symbols. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 17: Pre-deploy QA
- **Description:** Acceptance testing: run all tests, verify acceptance criteria from user-spec (AC1–AC31) and tech-spec (10 technical ACs). Run `pytest tests/ -q` and the targeted new test files. Run pre-deploy smoke: invoke `claude_transcreation.transcreate_via_claude` against a sample article (cost: ~$0.01 in tokens, acceptable for QA). Verify result is valid RU dict in <30s. Verify `_TokenRedactingFilter` redacts a sample anthropic key in synthetic log line. Write QA report; flag any deferred AC for post-deploy verification.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 18: Deploy
- **Description:** Run the GitHub Actions deploy workflow (or manual `bash deploy.sh` per operator's `project_post_mrw_pending` decision). Verify deploy logs show successful copy of all FILES including `ux-guidelines.md`, successful `pip install -r requirements.txt` on server (anthropic + pytz pulled), `.env` on server has all required new vars (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TZ`).
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 19: Post-deploy verification
- **Description:** Live environment verification on production VPS:
  - Wait until next 12:00 МСК cron tick fires.
  - Verify admin ping arrives in operator's chat with the day's schedule.
  - Wait until 13:00 МСК first publication.
  - Open the Telegraph URL of the first auto-published article. Verify: title with emoji prefix, body without boilerplate (no "Share on Facebook" etc), `↳ автоперевод` marker before the source footer.
  - Verify the channel post is single-line `#<source> #news`.
  - Check Telegram admin chat for absence of unexpected error messages.
  - Verify `pending_articles` queue drains as expected (next slot publishes another article ~40 min later).
  - 24-hour observation: confirm no unexpected pings, queue size remains bounded.
  - Tools: bash, ssh to VPS, sqlite3 (or Python sqlite3 module), Telegram client.
- **Skill:** post-deploy-qa
- **Reviewers:** none
