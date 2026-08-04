# Security Audit — llm-transcreation-and-distributed-publishing

**Auditor role:** security-auditor (Task 15, Wave 9)
**Date:** 2026-04-27
**Feature branch state:** post-Wave-8 (Tasks 1–13 complete)
**Verdict:** **PASS** — zero blocking findings.

---

## Summary

A holistic OWASP-relevant audit of the post-Wave-8 source surface (`news_bot.py`,
`claude_transcreation.py`, `outage_state.py`, `pending_articles_repo.py`,
`compute_publish_slots.py`, `.github/workflows/deploy.yml`, `deploy.sh`,
`requirements.txt`, `.env.example`, plus security-relevant tests).

Six OWASP-relevant probes (S1–S6) were executed against the final state. All
six closed PASS. Decisions 12 (3-layer `ANTHROPIC_API_KEY` redaction), 13
(`max_tokens=8000` cap + paragraph-count + 4000-char paragraph cap), and 16
(`PRAGMA busy_timeout=5000` on every connection in `outage_state.py`) are
faithfully realised in the code, not just declared in the spec.

Two **low-severity** observations and three **info-level** notes are recorded
below; none of them block deploy. The two low-severity items go to
**residual security risks** in `decisions.md`.

| Probe | Area | Outcome |
|---|---|---|
| S1 | `ANTHROPIC_API_KEY` leak via SDK errors / admin-ping / propagated logs | PASS |
| S2 | Prompt injection from article body content | PASS |
| S3 | SQLite deadlock between cron writer and `hw_review` CLI writer | PASS |
| S4 | SQL injection in `bot_state` migration / outage_state writes | PASS |
| S5 | Secret exposure in deploy bundle (workflow YAML + `deploy.sh`) | PASS |
| S6 | Environment data leak via Anthropic API call | PASS |

---

## Findings

| ID | Severity | Area | Description | Fix-direction | Location |
|---|---|---|---|---|---|
| L1 | low | env-redact | `_SECRET_ENV_NAMES` redaction in `sanitize_error_message` is bypassed when `ANTHROPIC_API_KEY` is the env var name itself but the key value is not yet imported into env at the time of the failure (e.g. import-time `anthropic.Anthropic()` failure in `_get_default_client` if env later changes). The regex layer (`_ANTHROPIC_KEY_RE`) catches the value-shape regardless. Defense-in-depth holds. No code change recommended. | Document as "regex layer is the load-bearing redaction; env-name layer is best-effort" in `decisions.md` residual risks. | news_bot.py:96-135 |
| L2 | low | path-traversal | `_load_prompt(path)` accepts an arbitrary `path` parameter from any caller. Currently only called with module-level `_PROMPT_PATH` constant or tests' explicit path. If a future feature plumbs a user-controlled value (e.g. operator CLI flag), it could read any file on disk. Not exploitable today — no caller passes attacker-controlled paths. | Tighten by validating `path` is under `_MODULE_DIR` before `os.path.isfile`, or annotate `prompt_path` as internal-only with a comment. | claude_transcreation.py:135-173 |
| I1 | info | dynamic-SQL | `outage_state.clear_outage_state` and the recovery path build SQL via f-string `placeholders = ','.join('?' for _ in _OUTAGE_KEYS)`. The interpolated value is a fixed-arity placeholder string derived from a module-private constant tuple, never from user input. Safe. | None — paramaterised pattern is the idiomatic way to handle variable-arity `IN(...)` in sqlite3. | outage_state.py:250-254, 442-446 |
| I2 | info | admin-ping-text | `send_admin_notification` correctly funnels every message through `_redact_text`. Several call-sites still pass `f"... {exc}"`-style strings (e.g. `_fallback_publish` per-article failure log). Even if the regex layer catches the value-shape, inline `type(exc).__name__` would be more conservative. | Spec invariant already documented at news_bot.py:312-318. Code-reviewer (task 14) may consolidate. | news_bot.py:806-822 |
| I3 | info | health-check-cost | `health_check()` makes a real `messages.create` call with `max_tokens=10`. This costs a few tokens per `news_bot.main()` startup and per recovery probe. Operator-monitored cost, no security impact. Token observability log line includes neither prompt nor response content. | None — explicit design choice (Decision 14). | claude_transcreation.py:562-577 |

---

## Probe results

### S1 — `ANTHROPIC_API_KEY` leak via Anthropic SDK errors → PASS

**Threat:** SDK retry-warning / error-string logging may embed the API key
in URLs / headers / request dumps. The token can also leak via
`send_admin_notification(f"Outage: {exc}")`-style strings (admin-ping path
lives outside the logging pipeline).

**Findings:**
- `_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_=.-]{16,}")` (news_bot.py:221) covers prod-shape `sk-ant-api03-...`, sandbox-shape with `=` and `.`, and admin-shape; verified by direct regex test (4 fixture strings in this audit).
- `_redact_text` (news_bot.py:224-254) is the single source of truth, calls both `_BOT_TOKEN_RE.sub` and `_ANTHROPIC_KEY_RE.sub`, never raises (silent-fallback invariant).
- `_TokenRedactingFilter` is attached to root logger AND every root handler (lines 291, 300-301) — closes the propagated-record gap that is the standard Python `Logger.filter()` blind spot.
- The filter is also attached to `httpx`, `httpcore`, `urllib3`, `requests`, `anthropic`, `anthropic._client`, `anthropic._base_client` (lines 302-309).
- `send_admin_notification` runs the message through `_redact_text` BEFORE the Telegram payload is built (news_bot.py:330) — belt-and-suspenders against admin-side leak.
- Admin-ping invariant at news_bot.py:312-318 documents the rule: callers should use `type(exc).__name__`, not `str(exc)`. Audited callers: `_fallback_publish` (lines 808, 820) — uses `type(exc).__name__`. PASS.
- `tests/test_no_token_leak_in_logs.py` (30 tests) covers regex shapes (prod, sandbox, edge), filter installation on each anthropic-family logger, propagated-record path via root StreamHandler, admin-notify redaction, env-name list extension. Comprehensive coverage.

**Outcome:** PASS. Decision 12 fully realised in code. No critical/high findings.

### S2 — Prompt injection from article body content → PASS

**Threat:** Adversarial `title`/`subtitle`/`paragraphs` content might
override the system prompt or amplify cost via output growth.

**Findings:**
- `_build_user_message(article)` (claude_transcreation.py:181-190) serialises the article as a JSON object passed to the user-message slot ONLY. The system prompt (`_build_system_prompt`) is composed exclusively from `ux-guidelines.md` body + a static JSON envelope (claude_transcreation.py:176-178). Article content is data, not prompt.
- `max_tokens=8000` cap (claude_transcreation.py:74, 432, 487) bounds amplification: even if a model is coerced into emitting garbage, it stops at 8000 tokens (~$0.04 at Haiku 4.5 input rates per malicious article).
- `_parse_response` (claude_transcreation.py:213-275) enforces:
  - Output is a JSON object (rejects array / scalar);
  - `title` is non-empty string;
  - `alts` is a list of 2-3 non-empty strings;
  - `paragraphs` length matches `expected_paragraph_count` (input length); attempted attack via paragraph injection / merging / splitting fails.
  - `blocks` length matches input block count when blocks are present.
- `_truncate_paragraphs` (claude_transcreation.py:317-332) caps each paragraph at 4000 chars with a warning log — defense against per-paragraph length amplification.

**Residual risk:** `title` and `subtitle` lengths are NOT explicitly capped on the Russian side. tech-spec Risks §last-row already documents this: an adversarial source could push a multi-thousand-char title into the channel. Telegraph itself imposes practical limits, but the pending row's `ru_title` column has no length cap. **Documented residual risk; acceptable.**

**Outcome:** PASS. Decision 13 fully realised in code.

### S3 — SQLite deadlock & lock contention → PASS

**Threat:** Concurrent writers (cron `news_bot.job()` distributed-publish
loop + `hw_review` CLI operator publishes) might deadlock on `bot_state` /
`pending_articles` writes.

**Findings:**
- `outage_state._connect()` (lines 100-110) executes `PRAGMA busy_timeout = 5000;` on every connection. Verified.
- `pending_articles_repo._connect()` (lines 155-158) does NOT set `busy_timeout`. This is acceptable: the repo writes use plain `conn.execute(...) → conn.commit()` (auto-commit / implicit BEGIN), not `BEGIN IMMEDIATE`. SQLite serialises writers via the journal; the second writer raises `OperationalError: database is locked` only if the first holds a write lock past the (default 0ms) busy timeout. In practice on this codebase, write transactions are sub-50ms; conflicts produce a transient retry-able error but not a deadlock.
- `BEGIN IMMEDIATE` is used ONLY inside `outage_state.py` (4 sites: `_set`, `clear_outage_state`, `record_outage_event`, `record_recovery_event`). `pending_articles_repo` does NOT use `BEGIN IMMEDIATE` anywhere. This is the linchpin per task spec hint S3: a true deadlock requires two `BEGIN IMMEDIATE` from different connections waiting on each other; since only one side uses it, the other side gets `SQLITE_BUSY` and (if retried) eventually proceeds.
- `record_recovery_event` uses double-checked locking (lines 422-447) to avoid taking the write lock on the steady-state healthy path — minimises contention on the hot publish loop.
- Tests `tests/test_outage_state.py::test_concurrency_real_threads` (per decisions Task 5 round-1 fix TR-3) explicitly race past the 1h boundary to verify `BEGIN IMMEDIATE` prevents double-increment. Confirmed in commit 6f1009e.

**Residual risk minor:** if `hw_review` CLI grows a `BEGIN IMMEDIATE` writer in the future without busy_timeout, it could deadlock with `outage_state`. **Mitigation:** add a code-review checklist note that any new `BEGIN IMMEDIATE` writer must also set `PRAGMA busy_timeout` ≥ 5000.

**Outcome:** PASS. Decision 16 realised; deadlock structurally impossible today.

### S4 — SQL injection in `bot_state` and outage_state writes → PASS

**Threat:** Raw SQL string interpolation could open injection vectors,
especially as `bot_state.value` accepts arbitrary text.

**Findings:**
- All SQL writes/reads in `outage_state.py` use `?` placeholders for the value. Audited every site:
  - `_get` line 121-124: `"SELECT value FROM bot_state WHERE key=?"`, `(key,)` ✓
  - `_set` lines 137, 139-142: parameterised DELETE / INSERT OR REPLACE ✓
  - `clear_outage_state` lines 250-254: SQL body f-string interpolates only the **placeholder count** (`?,?,?,?,?`), not values. Values bound via `_OUTAGE_KEYS` tuple ✓
  - `record_outage_event` lines 360-380: parameterised SELECT, parameterised INSERT OR REPLACE ✓
  - `record_recovery_event` lines 430-446: parameterised SELECT, parameterised DELETE ✓
- `pending_articles_repo._BOT_STATE_DDL` (lines 108-113) is a static string constant — no f-string, no interpolation. Idempotent `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)`.
- `pending_articles_repo` SQL: 100% parameterised (verified by grep — no f-string SELECT/INSERT/UPDATE/DELETE matches in the repo).
- `bot_state.value` content tolerance: `_parse_dt` (lines 155-167), `get_ping_count` (lines 202-213), `is_fallback_active` (line 222) all swallow corrupt content with a warning log + safe default. NULL / control-char / SQL-quote-bearing values cannot crash the bot at startup. AC419 covered.

**Outcome:** PASS. No f-string SQL with user-controlled content; DDL is static; readers tolerate corruption.

### S5 — Secret exposure in deploy bundle → PASS

**Threat:** Workflow YAML or `deploy.sh` could echo the API key into
GitHub Actions logs / `ps -ef` on the runner.

**Findings:**
- `.github/workflows/deploy.yml` step `Verify required secrets` (lines 38-61) iterates `[SSH_HOST, SSH_USER, DEPLOY_PATH, SSH_PRIVATE_KEY, ANTHROPIC_API_KEY]` via `[[ -z "${!var}" ]]` indirect-name-expansion check; never echoes the value. ✓
- Step `Write runtime env vars to server .env` (lines 125-182) routes secrets exclusively through `env:` mappings (`ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}`), which GitHub Actions auto-redacts in workflow logs.
- The ssh command line itself is `ssh -o StrictHostKeyChecking=yes "$SSH_USER@$SSH_HOST" 'bash -s' <<EOF` — no secret on the command line; `ps -ef` on the runner only sees `bash -s` and the user@host. ✓
- The heredoc body (lines 158-182) does inline `ANTHROPIC_API_KEY='$ANTHROPIC_API_KEY'`, which expands on the runner-side. The expanded literal is streamed over ssh stdin and never lands on a remote `ps` line. Single-quoted target syntax means the runner shell substitutes the env-mapped value before transmission. Auto-redact kicks in for any echo/error that reflects this back. ✓
- Server-side `.env` is written via `printf ... >> .env.tmp; mv .env.tmp .env` then `chmod 600 .env`. Permissions tightened at line 170 and 181. Idempotent — `grep -v -E '^(ANTHROPIC_API_KEY|ANTHROPIC_MODEL|TZ)='` strips stale entries before re-appending. ✓
- No `set -x` / `bash -x` / `echo $ANTHROPIC_*` / `echo ANTHROPIC_API_KEY=...` in either deploy file (verified by grep). ✓
- `.env.example` contains `ANTHROPIC_API_KEY=your_anthropic_api_key_here` (line 20) — explicit placeholder, not a real key.
- `requirements.txt` contains no secret material; pin block (`anthropic>=0.45.0,<0.46.0`, `pytz>=2024.1`) is normal package metadata.

**Outcome:** PASS. No critical/high findings; secret-handling discipline matches industry best practice.

### S6 — Environment data leak via Anthropic API → PASS

**Threat:** Anthropic API payloads might inadvertently include host
environment data — `os.environ` dump, `socket.gethostname()`, file paths
to `news.db`, etc.

**Findings:**
- `claude_transcreation.py` reads `os.getenv` for ONLY two values: `ANTHROPIC_MODEL` (line 472, 565) — the model identifier — and implicitly `ANTHROPIC_API_KEY` via the SDK's own `Anthropic()` constructor. Neither is ever placed in user/system message content.
- `_build_user_message(article)` (claude_transcreation.py:181-190) serialises ONLY: `source_name`, `title`, `subtitle`, `paragraphs`, `blocks`. No env, no hostname, no file path, no DB content.
- `_build_system_prompt` (line 176-178) is `prompt_body + _JSON_ENVELOPE` — both are static repository content (`ux-guidelines.md` + literal envelope template). No env interpolation.
- `_load_prompt` (lines 135-173) returns the file body verbatim; `os.path.getmtime` is used only as a cache key, not transmitted.
- No `socket.`, no `platform.`, no `pathlib.Path(__file__).resolve()`, no `os.environ` keys in any payload-building code (verified by grep).
- SDK retry traceback content (which COULD include the request URL and `Authorization: Bearer sk-ant-...` headers) goes through the `_TokenRedactingFilter` attached to the `anthropic._base_client` logger — covered by S1.

**Outcome:** PASS. Payload construction is mechanically clean.

---

## Decisions 12 / 13 / 16 — fact-check

| Decision | Spec demand | Code reality | Verdict |
|---|---|---|---|
| **12** (3-layer redaction) | (1) Telegram-token regex; (2) Anthropic-key regex broadened to include sandbox shapes; (3) `_TokenRedactingFilter` on root + every named noisy/anthropic logger + root handler; `_redact_text` shared by filter and `send_admin_notification`; admin-ping invariant `type(exc).__name__` in user-visible payload. | (1) `_BOT_TOKEN_RE` line 214 ✓ (2) `_ANTHROPIC_KEY_RE` line 221 with `[A-Za-z0-9_=.-]{16,}` covers sandbox shape ✓ (3) attached at lines 291, 300-301 (root + handlers), 302-309 (named loggers) ✓; `_redact_text` shared between `_TokenRedactingFilter.filter` and `send_admin_notification` ✓; admin-ping callers in `_fallback_publish` use `type(exc).__name__` ✓ | **MATCH** |
| **13** (`max_tokens` cap + output validation) | `max_tokens=8000`; per-paragraph 4000-char defensive cap; paragraph-count match against input length. | `_DEFAULT_MAX_TOKENS = 8000` line 74; `_PARAGRAPH_MAX_CHARS = 4000` line 71; passed via `transcreate_via_claude(...)` line 487; `_truncate_paragraphs` lines 317-332 with warning log; `_parse_response` paragraph-count match line 254-258. | **MATCH** |
| **16** (`PRAGMA busy_timeout = 5000`) | Set on every SQLite connection in `outage_state` (the BEGIN IMMEDIATE writer side). | `_connect()` line 109: `conn.execute("PRAGMA busy_timeout = 5000;")` — single connection helper used by every public function in the module. | **MATCH** |

---

## Verification grep results

- `grep -rnE 'f"SELECT|f"INSERT|f"UPDATE|f"DELETE|f\\'SELECT|f\\'INSERT|f\\'UPDATE|f\\'DELETE' outage_state.py pending_articles_repo.py news_bot.py`
  → 2 matches in `outage_state.py` (lines 251, 444). Both interpolate **placeholder count** from a fixed-arity tuple, not user values. **Safe.**
- `grep -nE 'echo.*ANTHROPIC|echo.*TOKEN|echo.*SECRET|set -x|bash -x' .github/workflows/deploy.yml deploy.sh` → empty. **PASS.**
- `grep -n 'busy_timeout' outage_state.py` → present at lines 7, 101, 107, 109; value `5000`. **PASS.**
- `grep -n 'max_tokens' claude_transcreation.py` → present at lines 30, 74, 432, 448, 487, 566; default `_DEFAULT_MAX_TOKENS = 8000`. **PASS.**
- Anthropic regex coverage: prod `sk-ant-api03-...`, sandbox `sk-ant-FAKE=SANDBOX.value...`, admin `sk-ant-admin01-XY=Z....` all matched; benign `sk-ant-shortish` correctly rejected.

---

## Residual security risks (for `decisions.md`)

1. **Title/subtitle output length is uncapped** (S2 residual) — adversarial source could deliver a multi-thousand-char title. Mitigated by Telegraph practical limits and the input being filtered by source-specific fetchers (`autoevolution_source.py` / `lamley_source.py` / `mattel_news_source.py`). **Acceptable; documented in tech-spec Risks.**

2. **`pending_articles_repo` connections lack `busy_timeout`** (S3 residual) — if a new code path adds `BEGIN IMMEDIATE` to the repo, lock-acquisition could fail under contention. Today the repo uses default auto-commit + small-window writes, so SQLITE_BUSY is unlikely. Add a code-review checklist item on future writers. **Acceptable; mitigation is a process control.**

3. **`_load_prompt(path)` accepts arbitrary path argument** (L2) — currently no caller passes attacker-controlled value. **Acceptable today; flag if a future feature plumbs CLI/env input through this argument.**

4. **`sanitize_error_message` env-layer is best-effort only** (L1) — if `ANTHROPIC_API_KEY` env var is unset at runtime when an exception fires, this layer is a no-op. The regex layer (`_ANTHROPIC_KEY_RE`) is the load-bearing redaction. **Acceptable; documented in `news_bot.py:96-135` docstring.**

---

## Final verdict

**PASS.** No critical, high, or medium findings. Two low-severity observations
recorded as residual risks (acceptable in current state). Three info-level
notes for code-reviewer / future-feature awareness.

Decisions 12 (3-layer Anthropic-key redaction), 13 (`max_tokens=8000` +
paragraph validation + 4000-char cap), and 16 (`PRAGMA busy_timeout=5000`
on outage_state connections) are faithfully implemented. The deploy bundle
handles secrets correctly via GitHub Actions `env:` mappings + ssh stdin
heredoc; no secret echo path exists.

Feature is cleared for Pre-deploy QA (Task 17).
