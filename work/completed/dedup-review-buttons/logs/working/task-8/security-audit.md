# Security Audit — dedup-review-buttons (Task 8, Audit Wave)

- **Auditor:** security-auditor (Task 8, Audit Wave)
- **Date:** 2026-07-24
- **Commit audited:** f50d974 (final state after waves 1–3 + review fixes afe4944/3feada9/c2c7e0d/1470468)
- **Scope:** `news_bot.py` (env/flag block L136-149, `_is_admin_press` L559, `resolve_dedup_callback` L582, listener block L670-993, E014 send site L2834-2876, `main()` wiring L3302), `admin_alerts.py` (`build_dedup_review_keyboard` L754), `pending_articles_repo.py` (`_connect` L182, token store L1044-1101, `skip_pending` L747), `.env.example` (L15-23). Methodology: OWASP Top 10 (2021) sweep + task-8 focus areas (end-to-end auth chain, injection, secret hygiene, DoS on the public getUpdates path, cross-component trust boundaries). Prior per-task security rounds (task-1/2/3/4/5) read first; accepted/declined findings are NOT re-litigated — they are listed under "Residual accepted risks".

## Verdict

**PASS — no Critical, no High, no Medium findings.** The bot's first inbound Telegram path is implemented fail-closed end-to-end. All state-changing operations sit behind a double admin gate (`_is_admin_press` in the handler BEFORE any DB read, and again as step 1 of `resolve_dedup_callback`); every new SQL statement is parameterized; the token is CSPRNG-minted server-side and never appears in logs or outgoing messages; the listener thread is triple-wrapped (per-update / per-poll / outer) so no failure can reach the publish process. Three Low defense-in-depth findings below; none blocks deploy.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 0 |
| Low      | 3 (new) + 4 residual accepted from per-task rounds |

## Findings

### Critical

None.

### High

None.

### Medium

None.

### Low

**SEC-A8-1 (Low) — Dead buttons + token minting when the flag is on but the admin id is non-numeric.**
- **Issue:** The E014 send site (`news_bot.py:2848`) gates keyboard rendering on `REVIEW_BUTTONS_ENABLED` alone, while the listener additionally requires a numeric `TELEGRAM_ADMIN_ID` (`_review_listener_enabled`, `news_bot.py:925`). With `REVIEW_BUTTONS_ENABLED=1` + the default non-numeric `@sunny413x`, the bot keeps minting tokens (`put_review_token`) and rendering buttons that no listener will ever serve — the operator sees an eternal spinner and `bot_state` accumulates orphan `review_token:*` rows. Mitigations already in place: startup WARNING + explanatory admin ping (`_maybe_start_review_listener`, `news_bot.py:956-974`), so the misconfiguration is loudly visible.
- **Impact:** Availability/consistency only (misconfiguration UX + slow row growth); no confidentiality or integrity impact — presses on such buttons are still rejected fail-closed at both gates.
- **Fix:** Gate the send-site block on `_review_listener_enabled()` instead of the bare flag (one-line change at `news_bot.py:2848`), so keyboard rendering and listening share the exact same effective gate.

**SEC-A8-2 (Low) — No TTL/cleanup for never-pressed review tokens.**
- **Issue:** `put_review_token` (`pending_articles_repo.py:1049`) rows are deleted only on a terminal press (`resolve_dedup_callback` step 5). An E014 alert the operator never touches leaves its `review_token:<token>` row in `bot_state` forever. Growth is NOT attacker-controllable (foreign `callback_data` never creates rows — only the E014 send site mints, rate-limited to one alert per article-pair per 7 days), so the ceiling is a few rows/week driven by feed volume.
- **Impact:** Unbounded-but-glacial DB growth; no exploit path. Pure housekeeping.
- **Fix:** Opportunistic purge, e.g. at the top of `job()` delete `review_token:*` rows whose stored link is no longer in `pending_articles` (the button is guaranteed stale at that point — resolve would answer «устарела/недоступна» anyway), or record a timestamp alongside the link and purge rows older than N days.

**SEC-A8-3 (Low) — `_review_edit_message` bypasses `_redact_text` (defense-in-depth parity).**
- **Issue:** `news_bot.py:745-757` sends `original_text + "\n\n" + status_text` to `edit_message_text` without a `_redact_text` pass. Not exploitable today: `original_text` is Telegram's copy of the alert that already went through `_redact_text` inside `send_admin_notification` at send time, and `status_text` comes from a fixed 5-string set in `resolve_dedup_callback`. But the edit path is a NEW outgoing-message sink that does not share the belt-and-suspenders redaction the send path has (Decision 12 pattern).
- **Impact:** None today; latent gap if a future caller ever feeds dynamic text into the edit path.
- **Fix:** One line: `text=_redact_text(text)` inside `_review_edit_message` (or at the composition site in `_handle_review_update`), matching `send_admin_notification` parity.

### Residual accepted risks (from per-task rounds — not re-litigated, listed for completeness)

1. **SEC-T5-2 (Low, DECLINED with documented rationale, decisions.md):** `logger.exception` tracebacks in the per-update catch-all bypass `_TokenRedactingFilter`. Pre-existing project-wide pattern; reachable exceptions verified token-free; project-wide traceback-redaction is out of feature scope. Disposition re-verified as acceptable.
2. **Task-1 Low (accepted):** no atomic "consume" primitive — `get_review_token_link` then `delete_review_token` are separate transactions. In the final state the listener is the SINGLE consumer thread, so the window is only reachable in the documented double-poller misconfiguration (409), where both downstream ops (`skip_pending`, `delete_review_token`) are idempotent — worst case is a duplicate honest status edit. Accepted.
3. **Task-2 Low (accepted):** `build_dedup_review_keyboard` does not bound/validate `token` at the builder layer. Final state confirms its only caller passes the server-minted `secrets.token_urlsafe(9)` value (12 chars → callback_data 17 bytes < 64).
4. **Task-3 SEC-3-01 (Low, accepted):** token is an opaque identifier, not a secret — the security boundary is the admin-id gate, and 72 bits of CSPRNG entropy vastly exceeds what an unguessable button id needs. Confirmed correct framing in the final state.

## Focus-area verification (end-to-end, final state)

**Auth chain (press → parse → gate → resolve → repo).** Updates arrive only via `getUpdates` over TLS authenticated by the bot token — no forgery surface. `_handle_review_update` (`news_bot.py:770`) parses the grammar FIRST (no I/O), then runs `_is_admin_press` (`news_bot.py:799`) BEFORE the link pre-fetch, so a non-admin press costs zero DB reads (SEC-T5-1 fix verified in final code, pinned by `mock_link_read.assert_not_called()` in tests). `resolve_dedup_callback` re-gates as its first statement (`news_bot.py:628`) — belt-and-braces, so no future caller of the pure function can skip auth. All DB writes (`skip_pending`, `delete_review_token`) are unreachable without passing both gates. Fail-closed verified at three levels: non-numeric `TELEGRAM_ADMIN_ID` (default `@sunny413x`) → listener refuses to start (`_review_listener_enabled`), `_is_admin_press` returns False for everyone, resolve returns `(None, "")`. No action-before-auth path exists.

**Injection (callback_data → SQL).** `_parse_review_callback_data` (`news_bot.py:701`) accepts ONLY exactly-three-field `dd:<c|k>:<token>` with non-empty token, rejects non-str, >64 bytes, wrong prefix/letter — everything else silently dropped before any I/O. The token reaches SQLite exclusively as a bound parameter under the `review_token:` prefix (`pending_articles_repo.py:1060-1062, 1077-1079, 1092-1094`); `skip_pending`/`get_pending`/`get_published` are likewise fully parameterized. No string-concatenated SQL anywhere in the new code. No parse_mode on any new outgoing/edited message (plain text — no Markdown/HTML injection), no shell/eval sinks.

**Secret hygiene (new paths).** Bot token: constructed into `Bot(...)` only (`news_bot.py:737, 751, 766`); poll-level and Conflict-level error logs route through `sanitize_error_message` (`news_bot.py:883, 895`); `_TokenRedactingFilter` + `_BOT_TOKEN_RE` scrub token shapes from all `record.msg/args`. The operator-decision INFO log (`news_bot.py:832-835`) contains action + link + status — no token value (verified). Review token never appears in any log or message text — only inside `callback_data` (invisible payload) and `bot_state`. Startup/warning pings (`news_bot.py:963-967, 984-986`) contain no secret values and go through `send_admin_notification`'s `_redact_text`. `.env.example` contains placeholders only (`your_bot_token_here`), the `REVIEW_BUTTONS_ENABLED` block documents the fail-closed and 409 constraints — no real secrets.

**DoS (public getUpdates surface).** Reachability: a `callback_query` requires pressing an inline button on a message our bot sent — E014 keyboards go only to the admin's private chat (forwards strip inline keyboards), so the hostile-presser population is effectively empty; everything else is filtered server-side by `allowed_updates=['callback_query']` (`news_bot.py:741`). Cost profile: 30 s long-poll (idle-cheap), batch size bounded by Telegram (≤100), malformed data rejected with zero I/O, non-admin press costs one `answer_callback_query`. Loop survival: offset advanced BEFORE handling (`news_bot.py:906`) so a poisoned update is consumed exactly once and can never wedge the loop; per-update try/except (`news_bot.py:903-915`), poll-level backoffs 5 s generic / 60 s on 409 Conflict with an explicit single-listener operator message (`news_bot.py:874-899`, both backoffs mutation-pinned by tests), outer belt-and-braces handler (`news_bot.py:916-922`); daemon thread — publish loop fully isolated. Memory: no unbounded in-process structures; `bot_state` growth is send-site-only and rate-limited (see SEC-A8-2). SQLite contention from the second writer absorbed by the pinned `timeout=5.0` busy handler (`pending_articles_repo.py:197`) + `BEGIN IMMEDIATE` (no lock-upgrade deadlock).

**Cross-component trust: poisoned `review_token:*` value in `bot_state`.** Threat model: a corrupted/poisoned stored link (writable only by the E014 send site with a feed-derived link, or by direct DB access — which is already full compromise). Sinks in the final state: (a) `get_pending`/`skip_pending`/`get_published` — parameterized SQL, a hostile string is just a key that matches nothing; (b) the operator-decision INFO log via `%s` — passes the token-redacting filter; a link with embedded newlines could cosmetically forge log lines, but feed links already flow into logs raw project-wide (no NEW surface); (c) the edited Telegram message — the link is NOT included (edit text = Telegram's own copy of the alert + fixed status), so a poisoned value cannot reach the chat via the new paths; (d) no code fetches, opens, or executes the link. Conclusion: no exploitable path.

## OWASP Top 10 (2021) sweep

| Category | Verdict | Notes |
|----------|---------|-------|
| A01 Broken Access Control | **Pass** | Double fail-closed admin gate before any state change; no action-before-auth; non-admin → answer-only, zero DB reads. |
| A02 Cryptographic Failures | **Pass** | Token = `secrets.token_urlsafe(9)` (CSPRNG, 72 bits, and not the security boundary); secrets stay in env; all Telegram traffic TLS via PTB. |
| A03 Injection | **Pass** | Exact-grammar parser; 100% parameterized SQL; plain-text messages (no parse_mode); no shell/eval sinks. |
| A04 Insecure Design | **Pass** | Fail-closed design, idempotent cancel/delete, race-honest post-hoc `get_published` re-read (c2c7e0d); SEC-A8-1/-2 are Low polish. |
| A05 Security Misconfiguration | **Pass w/ Low** | SEC-A8-1 (flag on + non-numeric admin → dead buttons; loudly warned). `.env.example` clean; default OFF; 409 double-poller handled + documented. |
| A06 Vulnerable Components | **N/A** | Feature adds zero packages — only stdlib `secrets`/`threading` and the existing PTB `Conflict` import; requirements untouched. |
| A07 Identification & Auth Failures | **Pass** | Identity = Telegram `from_user.id`, transitively authenticated by the token-authenticated getUpdates channel; no sessions/passwords introduced. |
| A08 Software & Data Integrity | **Pass** | No deserialization of untrusted data (callback_data is a string split, never eval/pickle); poisoned `bot_state` value analyzed above — no exploitable sink; token persisted BEFORE send (mutation-pinned) so no unwritten-token race. |
| A09 Security Logging & Monitoring | **Pass** | Every operator decision logged at INFO (action+link+status, no token); listener errors logged with `sanitize_error_message`; 409 produces an explicit operator-actionable ERROR; residual SEC-T5-2 traceback gap declined with documented rationale. |
| A10 SSRF | **N/A** | No new outbound requests derived from callback data; the stored link is never fetched — only compared as a DB key. |

## Tech-spec «Risks» table — closure status

| Tech-spec risk | Status | Evidence |
|---|---|---|
| Shared bot token: second getUpdates consumer 409s | **Closed** | Flag default OFF (`news_bot.py:147-149`); dedicated `Conflict` branch, 60 s backoff + explicit single-listener message (`news_bot.py:874-887`); test-pinned. |
| Flag enabled on test too → both poll → 409 | **Closed** | Same 409 branch names the exact remediation; «review listener active» startup log/ping makes a double-enable visible; `.env.example` documents ONE-instance rule. |
| `database is locked` from the second writer | **Closed** | `sqlite3.connect(..., timeout=5.0)` pinned by a call-spy test; `BEGIN IMMEDIATE` in token writers; two-writer concurrency test green. |
| Slot-boundary race (press mid-publish) | **Closed** | Post-hoc `get_published` re-read after `skip_pending` (`news_bot.py:649-652`, commit c2c7e0d) → honest «уже опубликовано»; `skip_pending` never touches `published_articles`; race test pins it. |
| Non-admin presses a button | **Closed** | `_is_admin_press` double gate, fail-closed on non-numeric admin id; non-admin → empty answer, zero DB reads, zero state change (test-pinned). |
| Stale token after restart / double press | **Closed** | Tokens persist in SQLite across restarts; consumed/unknown token → «⚠️ Кнопка устарела» idempotently; keyboard removed on first terminal press (`reply_markup=None`). |
| Listener thread crashes | **Closed** | Triple try/except (per-update / per-poll / outer), daemon thread, backoffs mutation-pinned; nothing escapes the thread; publish loop unaffected. |
| `callback_data` > 64 bytes | **Closed** | `dd:c:<token_urlsafe(9)>` = 17 bytes (measured); parser additionally rejects >64-byte inbound data (`news_bot.py:714`). |

## Verification of audit constraints

- Audit performed on commit f50d974; `git status` was clean when the audit began and **no source files were modified by this audit**.
- **Flag to lead:** at audit close, `git status` showed `news_bot.py` modified by a PARALLEL Audit Wave task's in-flight mutation check — `news_bot.py:3302` `_maybe_start_review_listener()` replaced with `pass  # MUTATION: listener wiring removed`. Left untouched here (owned by the parallel agent), but it MUST be restored before any commit: if committed, this mutation silently disables the entire listener (buttons render, nothing serves them). All findings in this report refer to the committed state f50d974, which was read before the mutation appeared. *(Resolved in ed10c58 — wiring restored and now mutation-pinned by a test, see M-2; verification below.)*

---

## Fix verification round — commit ed10c58 (2026-07-25)

Re-audited the `ed10c58` diff (`news_bot.py`, `pending_articles_repo.py`) and the resulting working-tree state. Full suite reported 1333 passed by the fixer.

**Per-finding verdicts:**

- **SEC-A8-1 — RESOLVED (and strengthened).** The E014 send site now gates on `_review_listener_enabled()` (`news_bot.py:2923`) instead of the bare flag. `_review_listener_enabled()` is `_review_listener_gate_reason() == 'ok'`, where `'ok'` requires flag on AND non-empty `TELEGRAM_BOT_TOKEN` (CA-3) AND numeric `TELEGRAM_ADMIN_ID` — the exact same effective gate as the listener startup, single-sourced (CA-6). Buttons/tokens are now minted only when a listener with the same config would serve them; the fix also covers the no-token case beyond the original recommendation. Fail-closed property preserved: every non-`'ok'` reason renders no buttons, mints no tokens, starts no listener, and the `no_token`/`bad_admin` startup warnings name only the env-var NAME, never a value.
- **SEC-A8-3 — RESOLVED.** `_review_edit_message` now sends `text=_redact_text(text)` (`news_bot.py:762`) — the edit path has the same belt-and-suspenders redaction as `send_admin_notification` (Decision 12 parity).
- **SEC-A8-2 — DECLINED, disposition accepted.** No TTL/janitor for never-pressed `review_token:*` rows, per the documented no-janitor trade-off. Acceptable for a Low: growth is not attacker-controllable (mint site only, 1 alert/pair/7 days) and bounded by feed volume, not input.

**New-code security pass (no new findings):**

- `_review_listener_gate_reason()` / reworked `_maybe_start_review_listener()`: fail-closed extended (CA-3 `no_token` branch prevents the `Bot(token=None)` perpetual 5s error loop — a small DoS-profile improvement); warning logs/pings contain no secret values.
- CA-5 byte-length cap: `len(data.encode('utf-8')) > 64` is the correct comparison (a ≤64-codepoint multibyte payload can exceed 64 bytes). Observation, no severity: `.encode('utf-8')` can raise `UnicodeEncodeError` on a lone-surrogate string; that exception is contained by the listener's per-update try/except (offset already advanced — logged and skipped, thread survives), and the input is not attacker-reachable in practice (Telegram validates callback_data server-side). Grammar safety contract holds at the thread level.
- CA-2 (decision log before Telegram edit/answer): same fields (action + link + status, no token) — improves A09 auditability (a transient Telegram failure no longer loses the audit line), no hygiene change.
- CA-1a (pre-teaser `get_pending` re-check in `_fallback_publish`): parameterized read, honest log, returns success-without-publish — closes the cancel-vs-in-flight-publish integrity race from the cancel side. No new surface.
- CA-1b (`move_to_published` dozapis on missing row): all SQL parameterized (`INSERT OR IGNORE`), WARNING via `%s` args, no secrets; keeps `published_articles` consistent with a real channel post. No new surface.
- M-2: `_maybe_start_review_listener()` wiring present at `news_bot.py:3377` and now mutation-pinned by a test — the parallel-mutation flag above is closed.

**Verdict: fix round PASS — 0 Critical / 0 High / 0 Medium / 0 new Low. All Task 8 findings closed or acceptably declined.**
- All feature files read in their final post-implementation state (tech-spec Files-to-modify + decisions.md cross-checked).
- Prior review rounds (task-1/2/3/4/5 security-auditor JSONs) read; accepted/declined dispositions honored, not re-litigated.
