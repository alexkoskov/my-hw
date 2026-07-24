---
created: 2026-07-22
status: approved
branch: dev
size: M
---

# Tech Spec: dedup-review-buttons

## Solution

Attach two inline buttons — «🚫 Не публиковать» / «👍 Оставить» — under the
`[E014] «Похож на дубль»` admin ping only. The bot gains its first-ever inbound
Telegram path: a **background long-poll thread** (`bot.get_updates`) running
alongside the existing blocking `schedule`/publish loop. On «Не публиковать» the
handler removes the suspected-duplicate row from `pending_articles` via the
existing `skip_pending(link)` before its publish slot; on «Оставить» it just
acknowledges. Feedback is given by editing the alert message (buttons → status
line). The listener runs **only on the prod instance** (shared bot token → a
second `get_updates` consumer would 409).

No new modules and no new DB tables: the listener + keyboard live in existing
deployed files (`news_bot.py`, `admin_alerts.py`, `pending_articles_repo.py`),
and the callback→article mapping reuses the existing `bot_state` key/value store.

## Architecture

### What we're building/modifying

- **`admin_alerts.build_dedup_review_keyboard(token)`** (new fn in existing
  `admin_alerts.py`) — returns an `InlineKeyboardMarkup` with two buttons whose
  `callback_data` is `dd:c:<token>` (cancel) and `dd:k:<token>` (keep). Pure,
  unit-testable, no I/O.
- **`send_admin_notification(..., reply_markup=None)`** (modify `news_bot.py`) —
  add a keyword-only `reply_markup` forwarded to `bot.send_message`. Keyword-only
  keeps every existing positional-args test green.
- **E014 send site** (`news_bot.py` `job()` flag branch, ~L2374-2397) — before
  sending, mint a token, persist `token→link` in `bot_state`, build the keyboard,
  pass `reply_markup`.
- **Review-token store** (new helpers in `pending_articles_repo.py`, backed by the
  existing `bot_state` table) — `put_review_token(token, link)`,
  `get_review_token_link(token)`, `delete_review_token(token)`.
- **`pending_articles_repo._connect()`** (modify) — add `PRAGMA busy_timeout=5000`
  so the new second writer (listener thread) waits instead of erroring on a lock.
- **`resolve_dedup_callback(...)`** (new fn in `news_bot.py`) — pure decision
  logic: given `(action, token, from_user_id)`, verify admin, resolve queue state,
  perform the cancel, return a `(status_text, answer_text)` result. No Telegram
  I/O — fully unit/integration testable.
- **Review listener** (new fn in `news_bot.py`, e.g. `_run_review_listener()`) —
  background thread: long-poll `bot.get_updates(offset, timeout=30)`, dispatch
  callback queries to `resolve_dedup_callback`, then `edit_message_text` +
  `answer_callback_query`. Gated by `REVIEW_BUTTONS_ENABLED` + numeric admin id.
- **`main()`** (modify) — start the listener thread (if gated on) before the
  `schedule` loop.
- **Config/docs** — new env `REVIEW_BUTTONS_ENABLED` in `.env.example`; PK docs
  update in `architecture.md` / `deployment.md`.

### How it works

**Send (prep phase, ~10:00 МСК).** `job()` flags a soft-dupe (`decision == 'flag'`,
not rate-limited) → mint `token = secrets.token_urlsafe(9)` → `put_review_token(
token, link)` → `build_dedup_review_keyboard(token)` → `send_admin_notification(
alert_cross_source_dupe(...), reply_markup=kb)`.

**Listen (whole process lifetime, prod only).** A daemon thread opens its own
`Bot` and loops `get_updates(offset, timeout=30, allowed_updates=['callback_query'])`.
For each callback query:
1. Parse `dd:<c|k>:<token>`; ignore anything else.
2. `resolve_dedup_callback(action, token, from_user_id)`:
   - `from_user_id != int(TELEGRAM_ADMIN_ID)` → return "ignored" (answer empty, no edit).
   - `get_review_token_link(token)` is None → stale → status «⚠️ Кнопка устарела».
   - action `keep` → status «👍 Оставлено» (no state change).
   - action `cancel`:
     - link still in `pending_articles` → `skip_pending(link)` → «✅ Отменено оператором».
     - else `get_published(link)` present → «⚠️ Уже опубликовано, отменить нельзя».
     - else (failed/held/gone) → «⚠️ Статья уже недоступна».
   - `delete_review_token(token)` after a terminal outcome.
3. `edit_message_text(original_text + "\n\n" + status, reply_markup=None)` +
   `answer_callback_query`.

**Publish loop is unchanged** and needs no cancel-check: it re-reads `list_pending()`
each slot and takes `rows[0]`, so a row deleted before its slot simply never
publishes (verified: `news_bot.py` slot loop). The `_fallback_publish` idempotency
guard (`get_published` check at top) already covers the residual boundary case.

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| `news.db` (SQLite file) | `news_bot.init_db` | Publish loop (main thread) + review listener (bg thread) — **separate `_connect()` per thread** (sqlite3 conns not thread-safe); `PRAGMA busy_timeout=5000` serialises writers | 1 file, N per-thread connections |
| Telegram bot account/token | env `TELEGRAM_BOT_TOKEN` | Send path (`Bot()` per call) + listener (`get_updates`) — one account shared prod+test → **only prod polls** (409 otherwise) | 1 account |

## Decisions

### Decision 1: Background `get_updates` long-poll thread (not Application, not separate process, not webhook)
**Decision:** Run inbound handling as a daemon thread doing a manual
`bot.get_updates(offset, timeout=30)` long-poll loop, dispatching to pure handler
logic. Reuse the existing `asyncio.run(coro)`-per-call style inside the thread.
**Rationale:** Matches the project's minimalist "plain script, no framework" style
(every send is already `asyncio.run(_send())`). Avoids PTB `Application.run_polling`
signal-handler-off-main-thread problems, needs no public HTTPS endpoint (bot is
behind a VPN, no web server), and adds no second deployable. Serves user-spec
"приём нажатий работает параллельно … не задерживает публикации".
**Alternatives considered:** (a) PTB `Application`/`Updater` framework — heavier,
signal-handler issues in a thread, foreign to this codebase. (b) Separate process/
container — cleaner isolation but doubles the deploy surface for a hobby single-op
bot; the singleton flock already guarantees one process/host. (c) Webhook — needs
public HTTPS; impossible behind the VPN without new infra. Overkill.

### Decision 2: Reuse `skip_pending(link)` for cancel
**Decision:** «Не публиковать» calls the existing
`pending_articles_repo.skip_pending(link)` (INSERT OR IGNORE `processed_news` +
DELETE `pending_articles`, one committed transaction, idempotent no-op if the row
is already gone).
**Rationale:** Exactly the "drop from queue, never publish, mark seen" semantics we
need; already battle-tested by the archived manual-review path. Serves user-spec
"«Не публиковать» убирает эту новую статью из очереди".
**Alternatives considered:** A new dedicated cancel fn — needless duplication.

### Decision 3: `callback_data` = short token mapped via `bot_state` [TECHNICAL]
**Decision:** `callback_data` carries `dd:<c|k>:<token>` where `token =
secrets.token_urlsafe(9)` (~12 chars). The `token→link` map is stored in the
existing `bot_state` table under key `review_token:<token>`.
**Rationale:** The `pending_articles` PK is the article URL, far over Telegram's
64-byte `callback_data` limit. A short random token fits; `bot_state` already
exists (owned by `init_schema`) so no new table/migration. Serves the buttons
requirement (US "две кнопки").
**Alternatives considered:** (a) `rowid` in callback_data — SQLite rowids can be
reused after deletes → risk of cancelling the wrong article. (b) New
`review_tokens` table — extra DDL for no benefit over `bot_state`. (c) Hash of URL
+ scan — O(n) match on every press.

### Decision 4: `PRAGMA busy_timeout=5000` in `_connect()` [TECHNICAL]
**Decision:** Add `busy_timeout=5000` to `pending_articles_repo._connect()`.
**Rationale:** The listener thread introduces a second concurrent writer to
`news.db`; the bare `sqlite3.connect` would raise `database is locked` immediately.
5000 ms matches the existing `outage_state` convention. Serves user-spec risk
"не ломает и не задерживает публикации".
**Alternatives considered:** WAL mode — larger change, unneeded at this write
volume. Per-call timeout only on the cancel path — inconsistent; `_connect()` is
the single choke point, safe to harden globally.

### Decision 5: Admin auth requires a numeric `TELEGRAM_ADMIN_ID`; fail-closed
**Decision:** The handler compares `callback_query.from_user.id` (int) to
`int(TELEGRAM_ADMIN_ID)`. If `TELEGRAM_ADMIN_ID` is non-numeric (e.g. the
`@sunny413x` default), the listener refuses to act (fail-closed) and logs/pings a
startup warning.
**Rationale:** `from_user.id` is always numeric; a `@username` can never match, so a
non-numeric admin id must disable the feature rather than silently accept or reject
ambiguously. Serves user-spec "реагирует только на администратора".
**Alternatives considered:** Resolving `@username→id` via `get_chat` — extra API
call + brittle; prod `.env` already uses a numeric id per deployment.md.

### Decision 6: Prod-only gate via new env `REVIEW_BUTTONS_ENABLED` (default off)
**Decision:** The listener thread starts only when `REVIEW_BUTTONS_ENABLED` is truthy
AND the admin id is numeric. Default off (unset/blank/`0/false/no/off`). Set `=1`
only in the hand-managed prod `.env`. The SAME flag also gates keyboard
*attachment* at the E014 send site — so a non-prod instance shows no buttons at all
(no confusing inert buttons on the test channel).
**Rationale:** One bot account serves prod+test; only one may long-poll. An explicit
default-off flag (mirroring the project's `DEDUP_SERIES_ENABLED` toggle culture)
guarantees test never accidentally polls and 409s prod, and never renders
dead buttons. Serves user-spec "слушает только прод" / "тест кнопки не обслуживает".
**Alternatives considered:** Gate on `INSTANCE_LABEL == 'prod'` — reuses an existing
var but couples listening to labelling; a dedicated flag is explicit and lets the
operator flip it without touching the label.

### Decision 7: Feedback by editing the alert message [serves US "кнопки → статус"]
**Decision:** On any terminal press, `edit_message_text` to append the status line
and drop the keyboard (`reply_markup=None`); also `answer_callback_query`.
**Rationale:** Gives clear in-place feedback and makes the buttons un-pressable again
(idempotency). Matches the chosen UX from the interview.
**Alternatives considered:** Separate reply message (clutters chat, re-pressable);
toast-only (no history trail).

### Decision 8: No new module — extend existing deployed files [TECHNICAL]
**Decision:** All code lands in `news_bot.py`, `admin_alerts.py`,
`pending_articles_repo.py` (already in the deploy FILES list).
**Rationale:** Adding a new first-party import to `news_bot.py` would require
mirroring it into both `deploy.sh` and `deploy.yml` FILES arrays (deploy INVARIANT)
or prod ImportError. Reusing deployed files sidesteps that entirely.

### Decision 9: Cancel is final; no "undo" button [serves US]
**Decision:** No mechanism to re-queue a cancelled article from the chat.
**Rationale:** It is a suspected duplicate; like the `[E015]` hard-block, undo is
unnecessary and would complicate state. Matches the approved user-spec.

### Decision 10: Missed-window detection via queue state, not a timer
**Decision:** "Already published" is decided by inspecting DB state at press time
(row absent from `pending_articles` + present in `published_articles`), not by
tracking the slot clock.
**Rationale:** Simpler and race-free — the same DB the publish loop mutates is the
source of truth. Serves user-spec "уже опубликовано, отменить нельзя".

## Data Models

No new tables. Review tokens live in the existing `bot_state(key TEXT PRIMARY KEY,
value TEXT)`:

- key: `review_token:<token>` where `<token> = secrets.token_urlsafe(9)`
- value: the article `link` (URL)
- lifecycle: written at E014 send; read on button press; deleted after a terminal
  outcome. Stale tokens (bot restarted, row already gone) are harmless — the handler
  resolves them to «устарела/недоступна». Optional janitor not required (rows are
  tiny; a press cleans its own token).

`callback_data` grammar: `dd:c:<token>` (cancel) / `dd:k:<token>` (keep) — ≤ ~16
bytes, well under the 64-byte limit.

## Dependencies

### New packages
- None. `python-telegram-bot==21.10` already provides `InlineKeyboardButton`,
  `InlineKeyboardMarkup`, `Bot.get_updates`, `Bot.edit_message_text`,
  `Bot.answer_callback_query`. `secrets` is stdlib.

### Using existing (from project)
- `pending_articles_repo.skip_pending` / `get_published` — cancel + missed-window check.
- `bot_state` table + `init_schema` — token store (new thin helpers alongside
  `outage_state`-style accessors).
- `admin_alerts.alert_cross_source_dupe` — unchanged text; keyboard attached at send.
- `news_bot.send_admin_notification` — extended with `reply_markup`.
- `INSTANCE_LABEL` / prod-guard patterns — precedent for a prod-only gate.

## Testing Strategy

**Feature size:** M

### Unit tests
- `build_dedup_review_keyboard`: two buttons, exact `callback_data` (`dd:c:` / `dd:k:`
  + token), labels with emoji.
- `send_admin_notification(reply_markup=...)`: forwards markup to `send_message`;
  omitted → unchanged call (existing positional tests stay green).
- token store: `put`/`get`/`delete` round-trip; `get` of unknown token → None.
- `resolve_dedup_callback`: non-admin → ignored (no state change); cancel on pending
  row → `skip_pending` called + «Отменено»; cancel on published → «Уже опубликовано»;
  cancel on missing → «недоступна»; keep → «Оставлено», no state change; stale token
  → «устарела».
- gate: non-numeric `TELEGRAM_ADMIN_ID` → listener refuses to start (fail-closed);
  `REVIEW_BUTTONS_ENABLED` off → no thread.

### Integration tests
- Real SQLite: stage a pending row → `resolve_dedup_callback(cancel)` → row absent
  from `pending_articles`, present in `processed_news`; then drive the slot publish
  path → the article is NOT published.
- Press cancel after the row moved to `published_articles` → status «Уже
  опубликовано», published row intact.
- Concurrency: a publish-loop writer and a cancel writer against the same DB with
  `busy_timeout` → no `database is locked`.

### E2E tests
- None automated — a real button press needs a live bot, and the shared token means
  only prod can listen. Covered by the operator's manual first-run check (user-spec
  "Пользователь проверяет").

## Agent Verification Plan

**Source:** user-spec "Как проверить".

### Verification approach
Automated unit + integration tests cover keyboard, forwarding, token store, handler
decisions, and the cancel→not-published flow on real SQLite. Per-task smoke checks
verify the keyboard/callback_data shape and the `get_updates` wiring via `python -c`
imports. The end-to-end "tap in Telegram" is operator-driven post-deploy on prod
(only prod listens): trigger/await a real `[E014]`, tap «Не публиковать» → article
does not publish + buttons become «✅ Отменено»; on another, tap «Оставить».

### Tools required
- `bash`, `python -c` (import/shape smoke checks).
- Telegram MCP (if available) for the post-deploy live check; otherwise operator
  taps manually and reports.

## Risks

| Risk | Mitigation |
|------|-----------|
| Shared bot token: a second `get_updates` consumer 409s | Prod-only gate (`REVIEW_BUTTONS_ENABLED` default off) + existing singleton flock; test keeps buttons inert |
| Operator accidentally enables the flag on test too → both poll → 409 | Doc: enable on exactly ONE instance; startup log states "review listener active" so a double-enable is visible |
| `database is locked` from the new second writer | `PRAGMA busy_timeout=5000` in `_connect()`; concurrency integration test |
| Race at the slot boundary (press mid-publish) | Cancel = delete row; loop re-reads `list_pending` each slot; in-flight publish already guarded by `_fallback_publish` idempotency → resolves to «уже опубликовано» |
| Non-admin presses a button | Fail-closed numeric-admin check; non-admin → ignored, no state change |
| Stale token after restart / double press | Idempotent handler: missing token/row → «устарела/недоступна», never crashes; keyboard removed on first terminal press |
| Listener thread crashes | Loop wrapped in try/except with backoff; a listener failure logs + pings but MUST NOT kill the publish process (daemon thread, isolated) |
| `callback_data` > 64 bytes | Short token scheme (Decision 3), asserted by unit test |

## User-Spec Deviations

- **Added: env `REVIEW_BUTTONS_ENABLED` (default off).** Not named in user-spec; it
  is the concrete mechanism implementing the approved requirement "слушает только
  прод". The same flag gates both listening AND keyboard rendering, so a non-prod
  instance shows no buttons and never polls. No behavior change beyond the approved
  requirement. → [APPROVED — operator approved the tech-spec incl. this deviation, 2026-07-22]

## Acceptance Criteria

Технические критерии (дополняют пользовательские из user-spec):

- [ ] `send_admin_notification` forwards `reply_markup`; called without it → byte-identical to today (existing tests green).
- [ ] `[E014]` alert carries exactly two buttons with `callback_data` `dd:c:<token>` / `dd:k:<token>`; no other alert (E006/E008/E009/E015/E034…) carries buttons.
- [ ] Token round-trips through `bot_state`; unknown token resolves to a safe "stale" status.
- [ ] `resolve_dedup_callback` enforces numeric-admin auth (non-admin → no state change) and covers pending/published/missing/keep/stale branches.
- [ ] Cancel before slot removes the row from `pending_articles` → not published; post-publish cancel → «уже опубликовано», published row intact.
- [ ] `_connect()` sets `busy_timeout`; concurrent writer test shows no `database is locked`.
- [ ] Listener starts only when `REVIEW_BUTTONS_ENABLED` is on AND admin id numeric; otherwise no thread (fail-closed).
- [ ] A listener error never aborts the publish loop.
- [ ] Each terminal press logs the operator's decision (cancel/keep + link + status) at INFO.
- [ ] Full existing suite green; no regressions.

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: DB concurrency guard + review-token store
- **Description:** Add `PRAGMA busy_timeout=5000` to `pending_articles_repo._connect()` and add `bot_state`-backed token helpers `put_review_token` / `get_review_token_link` / `delete_review_token`. Foundation for safe concurrent cancel and callback→article mapping.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import pending_articles_repo as r; r.put_review_token('t','http://x'); assert r.get_review_token_link('t')=='http://x'; r.delete_review_token('t'); assert r.get_review_token_link('t') is None; print('ok')"`
- **Files to modify:** `pending_articles_repo.py`, `tests/test_pending_articles_repo.py`
- **Files to read:** `outage_state.py`, `news_bot.py` (DB_FILE, init_db)

#### Task 2: Keyboard builder + `reply_markup` forwarding
- **Description:** Add `admin_alerts.build_dedup_review_keyboard(token)` returning the two-button `InlineKeyboardMarkup` (`dd:c:`/`dd:k:` callback_data), and add a keyword-only `reply_markup=None` to `send_admin_notification` forwarded to `send_message`. Keyword-only preserves existing tests.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import admin_alerts as a; kb=a.build_dedup_review_keyboard('t'); d=[b.callback_data for r in kb.inline_keyboard for b in r]; assert d==['dd:c:t','dd:k:t'], d; print('ok')"`
- **Files to modify:** `admin_alerts.py`, `news_bot.py`, `tests/test_admin_alerts.py`, `tests/test_admin_ping.py`
- **Files to read:** `news_bot.py` (send_admin_notification, imports L46)

### Wave 2 (зависит от Wave 1)

#### Task 3: Attach keyboard at the E014 send site
- **Description:** In `job()`'s soft-flag branch, and only when `REVIEW_BUTTONS_ENABLED` is on, mint a token, `put_review_token(token, link)`, build the keyboard, and pass `reply_markup` into the E014 `send_admin_notification`. Flag off → no token, no buttons (unchanged behavior). Only the E014 path; other alerts untouched.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`
- **Files to read:** `admin_alerts.py` (alert_cross_source_dupe, keyboard builder), `pending_articles_repo.py` (token helpers)

#### Task 4: Callback decision logic (`resolve_dedup_callback`)
- **Description:** Pure function taking `(action, token, from_user_id)` → performs numeric-admin auth, resolves queue state (pending → `skip_pending`; published → «уже опубликовано»; missing → «недоступна»; keep → «Оставлено»; stale token → «устарела»), deletes the token on terminal outcomes, returns `(status_text, answer_text)`. No Telegram I/O.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`
- **Files to read:** `pending_articles_repo.py` (skip_pending, get_published, token helpers)

### Wave 3 (зависит от Wave 2)

#### Task 5: Background review listener + main() wiring
- **Description:** Add `_run_review_listener()` — a daemon thread long-polling `bot.get_updates(offset, timeout=30, allowed_updates=['callback_query'])`, parsing `dd:<c|k>:<token>`, calling `resolve_dedup_callback`, then `edit_message_text` (append status, drop keyboard) + `answer_callback_query`. Wrap the loop so a failure never kills publishing. Log the operator's decision (cancel/keep + link + resolved status) at INFO, in the project's diagnostic style. Start it from `main()` only when `REVIEW_BUTTONS_ENABLED` is truthy AND admin id is numeric (fail-closed, with a startup log/ping).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import news_bot; print(hasattr(news_bot,'_run_review_listener'))"`
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`
- **Files to read:** `news_bot.py` (main, send_admin_notification, INSTANCE_LABEL, TELEGRAM_ADMIN_ID)

#### Task 6: Config + Project Knowledge docs
- **Description:** Add `REVIEW_BUTTONS_ENABLED` to `.env.example` (documented default-off + prod-only), and update `architecture.md` (inbound path + bot_state review-token key) and `deployment.md` (enable on prod `.env` only, one instance, numeric admin id) so the operator runbook is complete.
- **Skill:** documentation-writing
- **Reviewers:** code-reviewer
- **Files to modify:** `.env.example`, `.claude/skills/project-knowledge/references/architecture.md`, `.claude/skills/project-knowledge/references/deployment.md`
- **Files to read:** `work/dedup-review-buttons/tech-spec.md`

### Audit Wave

#### Task 7: Code Audit
- **Description:** Full-feature code quality audit. Read all files created/modified in this feature (from decisions.md + tech-spec "Files to modify"). Review holistically: thread-safety of the listener vs publish loop, shared-resource (news.db / bot token) compliance with Architecture, no duplicate Bot init pattern drift, error isolation of the listener. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 8: Security Audit
- **Description:** Full-feature security audit. Read all feature source. Focus: callback auth (numeric admin, fail-closed), no token/secret leak in logs or edited messages, callback_data injection/parse safety, DoS surface of the public get_updates path (only admin acted on), SQLite write safety. OWASP Top 10 across components. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 9: Test Audit
- **Description:** Full-feature test quality audit. Read all test files added/changed. Verify coverage of every `resolve_dedup_callback` branch, the cancel→not-published integration, the busy_timeout concurrency test, the gate/fail-closed cases, and that existing send tests stayed green. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 10: Pre-deploy QA
- **Description:** Acceptance testing: run the full pytest suite, verify every acceptance criterion from user-spec and tech-spec (no live environment needed). Report pass/fail per criterion.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 11: Deploy (operator-applied)
- **Description:** Prepare the deploy: confirm no new first-party import was added (FILES list unchanged); write the operator runbook — push `dev` (test auto-deploys via `deploy_test.yml`, buttons dormant, flag off), then on prod (Moscow host, OUTSIDE 10:00–20:00 МСК) add `REVIEW_BUTTONS_ENABLED=1` + numeric `TELEGRAM_ADMIN_ID` to the hand-managed prod `.env` and `git pull && docker compose up -d --build`. Claude prepares commands; operator applies.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 12: Post-deploy verification (operator-driven)
- **Description:** Live prod verification: confirm `docker logs hw-news-bot` shows "review listener active"; on a real/synthetic `[E014]` alert, tap «🚫 Не публиковать» → article does not publish in its slot + buttons become «✅ Отменено»; on another, tap «👍 Оставить» → publishes + «👍 Оставлено»; confirm test bot did NOT start a listener (no 409 in logs).
  Tools: Telegram MCP (if available) / manual operator tap + `bash` (docker logs).
- **Skill:** post-deploy-qa
- **Reviewers:** none
