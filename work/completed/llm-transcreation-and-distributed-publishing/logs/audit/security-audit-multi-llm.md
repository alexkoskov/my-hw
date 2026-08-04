# Security Audit — Multi-LLM Engines (Post-Wave 9 Incremental)

**Project:** Hot Wheels News Bot (`/workspaces/debian-2/my-hw/`)
**Audit date:** 2026-04-27
**Scope:** Post-Wave 9 changes to add Gemini / OpenAI / OpenRouter LLM engines
plus the dispatcher, redaction extensions, pre-commit gitleaks hook, and
deploy.yml flexible-LLM-key logic. Read-only, no network calls.
**Standard:** OWASP Top 10 (2021) + project-specific secret-management invariants.

---

## Verdict: **PASS_WITH_NOTES**

- Critical: 0
- High: 0
- Medium: 1
- Low: 5
- Info: 4

No blocking issues. Secret-management hygiene is solid: `.env` is `chmod 600`,
never tracked, never in history; redaction covers all four LLM key shapes plus
the Telegram bot token; the deploy pipeline keeps secrets off command lines via
SSH-stdin heredoc pattern. The findings below are defense-in-depth gaps and
hardening opportunities, none of which constitute exploitable weaknesses.

---

## Section 1 — Secret-management hygiene

### Status: PASS

Verified:

| Check | Result |
|---|---|
| `.env` file mode | `600` (`-rw-------`) ✓ |
| `.env` tracked by git | NO ✓ (only `.env.example` is tracked) |
| `.env` ever in history | NO ✓ (`git log --all --diff-filter=A -- .env` is empty) |
| `.gitignore` covers `.env`, `.env.*`, `*.key`, `*.pem`, `secrets/`, `credentials.json` | YES ✓ (lines 1–13 of `.gitignore`) |
| Hardcoded secrets in source | NONE found ✓ |
| Real keys in `git log --all -p` | NONE — every match is a `FAKE_*` test fixture or doc comment ✓ |
| `.env.example` placeholder values only | YES ✓ — only literal `your_..._here` strings (lines 2–66) |

Test-fixture key strings caught by the grep (all clearly synthetic):

- `FAKE_ANTHROPIC_KEY_PROD = "sk-ant-api03-FAKE_KEY_FOR_TESTS_..."`
- `FAKE_GEMINI_KEY = "AIzaSyFAKE_KEY_FOR_TESTS_..."`
- `FAKE_OPENROUTER_KEY = "sk-or-v1-FAKE_OPENROUTER_KEY_..."`
- `FAKE_OPENAI_KEY_PROJ = "sk-proj-FAKE_PROJECT_KEY_..."`

These are intentional fixtures used by `tests/test_no_token_leak_in_logs.py`
to verify the redaction filter; they are not real keys.

---

## Section 2 — Redaction completeness (`news_bot.py`)

### Status: PASS

Verified by reading the regex definitions (lines 218–248) and running each
key shape against the live `_redact_text` pipeline (in-process):

| Key shape | Sample | Redaction result |
|---|---|---|
| Telegram bot token | `1234567890:ABCDEFaaa...` | `***` ✓ |
| OpenRouter `sk-or-v1-...` | 64-char body | `***` ✓ |
| Anthropic `sk-ant-api03-...` | 60-char body | `***` ✓ |
| Anthropic sandbox `sk-ant-admin01-...==` | with `=`/`.` | `***` ✓ |
| OpenAI legacy `sk-<48-base62>` | classic shape | `***` ✓ |
| OpenAI project `sk-proj-...` | hyphenated body | `***` ✓ |
| OpenAI service `sk-svcacct-...` | hyphenated body | `***` ✓ |
| Gemini `AIza<35>` | Google API-key shape | `***` ✓ |

**Pattern-order correctness (the load-bearing question):** The legacy OpenAI
pattern `sk-[A-Za-z0-9]{32,}` uses a bare `[A-Za-z0-9]` character class — no
hyphens, no equals, no dots. The hyphen in `sk-or-v1-`, `sk-ant-`, and `sk-proj-`
breaks the contiguous run, so the legacy pattern cannot eat those keys even if
it ran first. Pattern order in `_redact_text` (OpenRouter → Anthropic → OpenAI →
Gemini) is correct as documented and as a belt-and-suspenders measure. No regex
overmatch found.

**`_TokenRedactingFilter` attachment** (lines 326–349):

| Logger | Filter attached | Notes |
|---|---|---|
| Root logger | YES (line 326) | Logger-level filter; covers records originating on root |
| All current root handlers | YES (lines 335–336) | Closes the gap that logger-level filters don't run on records propagated up from child loggers |
| `httpx`, `httpcore`, `urllib3`, `requests` | YES (line 337–338) | |
| `anthropic`, `anthropic._client`, `anthropic._base_client` | YES (line 344) | |
| `openai`, `openai._base_client` | YES (line 345) | Used by both `openai_transcreation` and `openrouter_transcreation` |
| `google_genai`, `google_genai.models` | YES (line 346) | |

**`_SECRET_ENV_NAMES` coverage** (lines 96–106): 9 names — all four LLM keys,
both OpenRouter aliases (`OPENROUTER_API_KEY` and `OPEN_ROUTER_API_KEY`), the
Telegram triple, and `TELEGRAPH_ACCESS_TOKEN`. Complete.

**`send_admin_notification` redaction order** (line 370): `_redact_text(message)`
runs BEFORE the Telegram payload is built. ✓

---

## Section 3 — Multi-LLM engines (3 new files)

### Status: PASS

Reviewed:

- `gemini_transcreation.py` (15.7K)
- `openai_transcreation.py` (15.5K)
- `openrouter_transcreation.py` (16.3K)

#### 3.1 — System-prompt construction does not embed untrusted article content

`_build_system_prompt(prompt_body)` returns `prompt_body.rstrip() + _JSON_ENVELOPE`
where `prompt_body` is loaded from `ux-guidelines.md` (project file) and
`_JSON_ENVELOPE` is a hardcoded constant. Article content is built separately
by `_build_user_message(article)` which JSON-encodes the article and is passed
as the `user`-role message (Gemini: `contents=user_msg`; OpenAI/OpenRouter:
`messages=[{"role":"system",...}, {"role":"user",...}]`). Clean separation —
**no prompt-injection surface in the system slot**.

#### 3.2 — SDK exception classification doesn't leak environment data

In all three engines, `_classify_exception(exc)` produces the redacted message
via `f"{type(exc).__name__}: {exc}"`. SDK exceptions can include URL fragments
that embed an API key (e.g. some OpenAI errors print the request URL). However:

- The downstream `news_bot.py` callers stringify these via `sanitize_error_message`
  AND the global `_TokenRedactingFilter` runs on the SDK loggers. Two-layer
  defense.
- No `_classify_exception` reads `os.environ` directly. Verified by grep: the
  only `os.environ` references in the codebase are in `send_post.py`,
  `post_latest_news.py`, `telegraph_publisher.py` — all are `.env` parsing or
  Telegraph token reads. None go into exception text.

#### 3.3 — Client lifecycle: API key read lazily, never logged, never on cmdline

| Engine | Key read | When | Passed via |
|---|---|---|---|
| Gemini | `os.getenv("GEMINI_API_KEY")` | First `transcreate_via_claude()` call | `genai.Client(api_key=...)` constructor only |
| OpenAI | `os.getenv("OPENAI_API_KEY")` | First call | `openai.OpenAI(api_key=...)` constructor only |
| OpenRouter | `_resolve_api_key()` (tries `OPENROUTER_API_KEY` then `OPEN_ROUTER_API_KEY`) | First call | `openai.OpenAI(api_key=..., base_url=...)` constructor only |

No `logger.info("api_key=%s", key)` patterns anywhere. Singleton clients are
cached at module level (`_DEFAULT_CLIENT`) — no repeated reads.

#### 3.4 — `_load_prompt(path)` accepts an arg with safe default

Each engine's `_load_prompt(path: str = _PROMPT_PATH)` accepts a path argument.
The argument is forwarded from `transcreate_via_claude(article, *, prompt_path
= _PROMPT_PATH, ...)`. Verified the only call site in production code is
`news_bot.py:838 — claude_result = transcreate_via_claude(row)` which passes
NO `prompt_path` kwarg. The default is the in-repo path. **Not a present-day
attack surface** because there's no public/HTTP boundary that lets an attacker
control `prompt_path`. Flagged in §Findings as informational hardening.

#### 3.5 — Token + latency observability log lines do not embed API key

All three engines log:

```python
logger.info(
    "<engine>_transcreation: model=%s input_tokens=%s output_tokens=%s latency_ms=%s",
    model, input_tokens, output_tokens, latency_ms,
)
```

The format string contains only model name, integer counters, and latency.
No request URL, no header, no client config. ✓

---

## Section 4 — CI/CD security (`.github/workflows/deploy.yml`)

### Status: PASS_WITH_NOTES

#### 4.1 — Secrets via `env:` mapping (auto-redacted)

All steps that touch secrets pass them through `env:` blocks (lines 39–47,
80–81, 94–97, 142–159, 222–225). GitHub Actions automatically redacts any
secret-mapped value that appears in workflow logs. ✓

#### 4.2 — SSH heredoc keeps secrets off command lines

The "Write runtime env vars to server .env" step (lines 141–219) pipes a
heredoc into `ssh ... 'bash -s'`. The SSH command line carries only `bash -s`
and the user@host target. Secret values are inlined via runner-shell variable
expansion into the heredoc body, which travels over SSH stdin — never visible
in `ps -ef` on either runner or remote. ✓

#### 4.3 — `.env` chmod 600 immediately

Line 200: `chmod 600 .env` after `touch .env` (line 199). Then `.env.tmp` is
written, and a final `chmod 600 .env` runs on line 218 after `mv`. The
permissions are set correctly at rest. See **Finding L-2** for the brief
TOCTOU window between `mv` and the second `chmod`.

#### 4.4 — Secrets never echoed

Verified by full read of deploy.yml. No `echo "$ANTHROPIC_API_KEY"` or
equivalent. The "Verify required secrets" step echoes only `::error::` strings
and a final `"All required secrets present."` (line 72). ✓

#### 4.5 — "Verify required secrets" step uses ANY-of-LLM-keys logic

Lines 62–67:

```bash
if [[ -z "$ANTHROPIC_API_KEY" && -z "$OPENAI_API_KEY" && -z "$GEMINI_API_KEY" && -z "$OPENROUTER_API_KEY" ]]; then
  echo "::error::No LLM API key set..."
  exit 1
fi
```

Correct — fails ONLY if all four keys are unset. Single-key deploy works.
Note that `OPEN_ROUTER_API_KEY` (the alias) is NOT checked here — see
**Finding L-3**.

#### 4.6 — Idempotent .env update

Line 204: `grep -v -E '^(ANTHROPIC_API_KEY|...|TZ)=' .env > .env.tmp || true`
strips stale entries before re-appending (line 216). Repeated deploys do not
duplicate lines. ✓

---

## Section 5 — Pre-commit hooks (`.pre-commit-config.yaml`)

### Status: PASS

| Check | Result |
|---|---|
| gitleaks pinned to a tag | YES — `rev: v8.21.4` (line 17). Not floating `main`. ✓ |
| `detect-private-key` hook | YES — line 31 ✓ |
| `check-added-large-files` | YES — line 29, `--maxkb=1000` ✓ |
| Bypass mechanism documented | YES — comment block lines 7–8 explains `--no-verify` is intentional and explain-in-message ✓ |
| Other hygiene hooks | `trailing-whitespace`, `end-of-file-fixer`, `check-merge-conflict`, `check-yaml` ✓ |

`pre-commit-hooks` repo also pinned (`rev: v5.0.0`).

---

## Section 6 — SQLite

### Status: PASS

#### 6.1 — Parameterization

All DB operations across `news_bot.py`, `pending_articles_repo.py`,
`outage_state.py`, and `hw_review.py` use `?` placeholders. Greps for
`execute(f"`, `execute(.*\.format`, `execute(.*%[sd]`, and `execute("…{`
return zero hits. The only string-built SQL is in `outage_state.clear_outage_state`:

```python
placeholders = ','.join('?' for _ in _OUTAGE_KEYS)
sql = f"DELETE FROM bot_state WHERE key IN ({placeholders})"
conn.execute(sql, _OUTAGE_KEYS)
```

`_OUTAGE_KEYS` is a module-level tuple of constants, never user-controlled.
Placeholders are derived from its length only. Safe.

#### 6.2 — `BEGIN IMMEDIATE` deadlock potential

`outage_state.py` uses `BEGIN IMMEDIATE` + `PRAGMA busy_timeout = 5000` at
lines 109, 135, 253, 359, 429. The cron writer (`news_bot.job()`) and the
`hw_review` operator-side writer can both attempt to acquire the same
write-lock. The 5-second `busy_timeout` absorbs typical sub-50ms write
windows. Worst case, one of them gets `OperationalError: database is locked`
and the bot logs an error or `hw_review` fails its operation. Availability
concern, not a security one. Documented in tech-spec Decision 16. PASS.

#### 6.3 — `bot_state` schema injection vector

DDL: `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)`.
All keys are module-level constants (`_KEY_OUTAGE_STARTED_AT` etc.); values
are ISO-8601 strings or short flags. Both pass through `?` placeholders. **No
injection vector.** ✓

---

## Section 7 — Network surface

### Status: PASS

**Outbound only**, no inbound listeners:

- `Flask | FastAPI | aiohttp.web | http.server | HTTPServer | socket.bind |
  bind\(\( | listen\(\) | run_polling | run_webhook | set_webhook | webhook |
  TCPServer | create_server` — **zero matches** in the codebase.

Outbound destinations:

| Destination | Module | Purpose |
|---|---|---|
| `api.telegram.org` | `news_bot.py`, `send_post.py` | Channel + admin pings |
| `api.telegra.ph` | `telegraph_publisher.py` | Long-read host |
| `www.autoevolution.com` | `autoevolution_source.py` | RSS scraping |
| `www.lamleygroup.com` | `lamley_source.py` | News scraping |
| `creations.mattel.com` | `mattel_news_source.py` | News scraping |
| `api.anthropic.com` | `claude_transcreation.py` | LLM (one of 4) |
| `generativelanguage.googleapis.com` | `gemini_transcreation.py` | LLM (one of 4) |
| `api.openai.com` | `openai_transcreation.py` | LLM (one of 4) |
| `openrouter.ai/api/v1` | `openrouter_transcreation.py` | LLM gateway (one of 4) |

`OPENROUTER_BASE_URL` is operator-configurable (`os.getenv` with default
`https://openrouter.ai/api/v1`). On a hostile-operator threat model this
would be SSRF; on the actual threat model (operator owns both server and
secrets), it's a configuration knob, not a vulnerability. See **Finding
INFO-1**.

---

## Section 8 — Dependency hygiene

### Status: PASS_WITH_NOTES

`requirements.txt` (production):

```
feedparser==6.0.10
requests==2.32.3
beautifulsoup4==4.12.3
deep-translator==1.11.4
anthropic>=0.45.0,<0.46.0
google-genai>=1.70.0,<2.0.0
openai>=2.0.0,<3.0.0
schedule==1.2.1
pytz>=2024.1
python-telegram-bot==21.10
curl_cffi==0.15.0
python-dotenv             ← UNPINNED
```

| Package | Pin | Note |
|---|---|---|
| `feedparser==6.0.10` | exact | latest stable |
| `requests==2.32.3` | exact | resolves the urllib3 cookie-leak fix; current stable |
| `beautifulsoup4==4.12.3` | exact | current |
| `deep-translator==1.11.4` | exact | unofficial Google Translate wrapper |
| `anthropic>=0.45.0,<0.46.0` | minor band | tight |
| `google-genai>=1.70.0,<2.0.0` | major band | wider than other LLM SDKs |
| `openai>=2.0.0,<3.0.0` | major band | wider than `anthropic` band |
| `schedule==1.2.1` | exact | |
| `pytz>=2024.1` | floor only | new minors auto-adopted |
| `python-telegram-bot==21.10` | exact | |
| `curl_cffi==0.15.0` | exact | older — released mid-2024 |
| `python-dotenv` | UNPINNED | **Finding L-1** |

`freezegun` correctly lives in `requirements-dev.txt` only — NOT in
production `requirements.txt`. ✓

No CVE alerts visible in pinned versions to my knowledge cutoff. A live
`pip-audit` was attempted but blocked by the read-only audit policy (no
network calls / no env modifications during the audit) — operator should
run `pip-audit -r requirements.txt` periodically as part of CI hygiene.

---

## Findings

### Medium

#### M-1 — SSH `known_hosts` is built fresh per workflow run via `ssh-keyscan` (TOFU)

**File:** `.github/workflows/deploy.yml:79–91`

**Issue:** Every deploy run does:

```bash
ssh-keyscan -H "$SSH_HOST" > /tmp/known_hosts.new
cat /tmp/known_hosts.new >> ~/.ssh/known_hosts
```

This trusts whatever host key is presented to the runner at the moment of
`ssh-keyscan`. If an attacker can MITM the runner's network (rare but not
impossible — GitHub-hosted runners share infrastructure), they can present a
key that `ssh-keyscan` records and that the subsequent `scp` / `ssh` then
trusts for that job. The deploy then pushes `news_bot.py`, `_llm_common.py`,
the LLM engines, AND writes the `.env` (containing every secret) to whatever
host responded.

**Impact:** Confidentiality + integrity. A successful attack would extract all
secrets in the `.env` write step and could substitute Trojan code. Severity is
Medium because the attack requires active MITM at runner-network level, which
is rare in practice.

**Fix:** Pin the production server's SSH host key fingerprint as a repo
secret and verify before connect. Replace the `ssh-keyscan` step with:

```yaml
- name: Add host to known_hosts
  env:
    SSH_HOST: ${{ secrets.SSH_HOST }}
    SSH_HOST_KEY: ${{ secrets.SSH_HOST_KEY }}   # ssh-ed25519 AAAA... root@server
  run: |
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    echo "$SSH_HOST_KEY" >> ~/.ssh/known_hosts
    chmod 644 ~/.ssh/known_hosts
```

The fingerprint is captured once (`ssh-keyscan -H your.host` from a trusted
machine), stored as `secrets.SSH_HOST_KEY`, and never refreshed automatically.
Server key rotation becomes a manual-secret-update event.

**CWE:** CWE-322 (Key Exchange Without Entity Authentication)

---

### Low

#### L-1 — `python-dotenv` is unpinned in `requirements.txt`

**File:** `requirements.txt:12`

**Issue:** `python-dotenv` has no version constraint. A fresh deploy on a new
server pulls the latest at install time; subsequent deploys may pull a
different latest if upstream has released. This violates reproducible-build
hygiene and means a malicious release of `python-dotenv` (typo-squat or
account-takeover) could be auto-pulled with no signal.

**Impact:** Supply-chain integrity. Low severity because `python-dotenv` is
simple and stable, used only for `.env` parsing at startup; an attacker would
need to compromise the upstream PyPI package or the maintainer's account.

**Fix:**

```diff
-python-dotenv
+python-dotenv==1.0.1
```

Use the version your CI is currently testing against (run `pip show python-dotenv`
on the dev environment).

**CWE:** CWE-1357 (Reliance on Insufficiently Trustworthy Component)

---

#### L-2 — Brief TOCTOU window on `.env` mode after `mv`

**File:** `.github/workflows/deploy.yml:217–218`

**Issue:** The deploy writes `.env.tmp` (which inherits the umask, typically
mode 0644) then runs `mv .env.tmp .env` (line 217), then `chmod 600 .env`
(line 218). Between the `mv` and the `chmod`, the `.env` file exists with mode
0644 for a few milliseconds. On a single-tenant production server with no
other untrusted local users this is moot, but a co-tenant with read access to
`$DEPLOY_PATH` could win the race and read the secret.

**Impact:** Confidentiality. Very narrow window (~µs to ms) on a single-tenant
host.

**Fix:** Set the umask before the write so `.env.tmp` is created mode 600 from
the start, OR `chmod 600 .env.tmp` before `mv`, OR use `install -m 600 .env.tmp .env`
which atomically sets mode at copy time.

```diff
+chmod 600 .env.tmp
 mv .env.tmp .env
 chmod 600 .env
```

OR (cleaner):

```diff
-mv .env.tmp .env
+install -m 600 .env.tmp .env && rm -f .env.tmp
-chmod 600 .env
```

**CWE:** CWE-367 (TOCTOU Race Condition)

---

#### L-3 — Deploy verify step doesn't accept `OPEN_ROUTER_API_KEY` alias

**File:** `.github/workflows/deploy.yml:62–67`

**Issue:** The "Verify required secrets" check tests only `OPENROUTER_API_KEY`,
but `openrouter_transcreation._resolve_api_key()` also accepts the alias
`OPEN_ROUTER_API_KEY` (with underscore between `OPEN` and `ROUTER`). An
operator who sets only the alias would pass the runtime check on the server
but fail the deploy precondition.

**Impact:** Operational footgun — deploy aborts spuriously when alias is set.
Not a security issue but documented in the codebase as a supported alias and
should be honored end-to-end.

**Fix:**

```diff
-if [[ -z "$ANTHROPIC_API_KEY" && -z "$OPENAI_API_KEY" && -z "$GEMINI_API_KEY" && -z "$OPENROUTER_API_KEY" ]]; then
+if [[ -z "$ANTHROPIC_API_KEY" && -z "$OPENAI_API_KEY" && -z "$GEMINI_API_KEY" && -z "$OPENROUTER_API_KEY" && -z "$OPEN_ROUTER_API_KEY" ]]; then
```

And add `OPEN_ROUTER_API_KEY: ${{ secrets.OPEN_ROUTER_API_KEY }}` to the
step's `env:` map. Same change for the env-write step (lines 142–159) and
the strip-existing grep (line 204).

**CWE:** N/A (operational consistency)

---

#### L-4 — Heredoc inlines secrets via single-quoted shell substitution; breaks on a literal `'` in any secret value

**File:** `.github/workflows/deploy.yml:181–219`

**Issue:** The heredoc body contains lines like `ANTHROPIC_API_KEY='$ANTHROPIC_API_KEY'`.
The runner's shell expands `$ANTHROPIC_API_KEY` and substitutes it inside the
single quotes in the literal text. If a future LLM key shape ever contained a
literal single-quote character `'`, the resulting line would break apart and
either misassign or fail in the remote `read -r` / variable assignment step.

**Impact:** None today — current LLM key formats (`sk-ant-*`, `sk-or-v1-*`,
`sk-proj-*`, `AIza*`, Telegram bot token) are all alphanumeric + `[_=.-]`,
no quotes. Theoretical regression risk if a provider changes their key alphabet.

**Fix:** Switch the env-write step to read values off stdin opaquely instead
of embedding them in the heredoc body. Example sketch:

```yaml
run: |
  printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
    "$DEPLOY_PATH" "$ANTHROPIC_API_KEY" "$ANTHROPIC_MODEL" \
    "$GEMINI_API_KEY" "$GEMINI_MODEL" \
    "$OPENAI_API_KEY" "$OPENAI_MODEL" \
    "$OPENROUTER_API_KEY" "$OPENROUTER_MODEL" \
    "$LLM_PROVIDER" "$TZ" \
    | ssh -o StrictHostKeyChecking=yes "$SSH_USER@$SSH_HOST" 'bash -s' << 'REMOTE'
  set -e
  read -r DEPLOY_PATH
  read -r ANTHROPIC_API_KEY
  read -r ANTHROPIC_MODEL
  # ... rest of reads ...
  cd "$DEPLOY_PATH"
  # build .env via printf into .env.tmp ...
  REMOTE
```

Note the `<< 'REMOTE'` (quoted heredoc → no runner-shell expansion of the
script body) and the values arriving on stdin lines BEFORE the script body
runs `bash -s`. Each value is opaque to the shell. This is what the comment
on lines 171–174 actually describes — the code today doesn't fully match the
comment.

**CWE:** CWE-78 (OS Command Injection — defensive)

---

#### L-5 — `_load_prompt(path)` accepts a caller-controlled file path

**File:** `gemini_transcreation.py:89–118`, `openai_transcreation.py:86–115`,
`openrouter_transcreation.py:116–145`, `claude_transcreation.py` (same pattern)

**Issue:** Each engine's `_load_prompt(path: str = _PROMPT_PATH)` opens
`path` and reads it into memory as the system prompt. If an attacker could
supply `path="/etc/passwd"` (or any file), its contents would become part of
the LLM system prompt and be sent to the remote LLM provider — confidentiality
loss.

**Impact:** None in current architecture. The function is reachable only via
`transcreate_via_claude(article, *, prompt_path=...)`, and the only call site
in production code (`news_bot.py:838`) passes `row` only — `prompt_path`
defaults to `_PROMPT_PATH`. There's no public/HTTP/IPC/CLI path that lets an
attacker influence `prompt_path`. **This is an informational hardening note,
not an exploitable bug.**

**Fix (defense in depth):** Either drop the parameter, or whitelist
acceptable prompt paths inside the function:

```python
def _load_prompt(path: str = _PROMPT_PATH) -> str:
    if os.path.realpath(path) not in (os.path.realpath(_PROMPT_PATH),
                                       os.path.realpath(os.path.join(_MODULE_DIR, "ux-guidelines.md"))):
        raise ValueError(f"prompt path {path!r} is not in the allow-list")
    ...
```

**CWE:** CWE-22 (Path Traversal — defensive only)

---

### Info

#### INFO-1 — `OPENROUTER_BASE_URL` is operator-configurable

**File:** `openrouter_transcreation.py:359–362`

The base URL can be overridden via `OPENROUTER_BASE_URL` or
`OPEN_ROUTER_BASE_URL` env vars. On a hostile-operator threat model this would
be SSRF. On the actual threat model (operator owns the secrets and the
server), this is a knob for testing against `httpbin.org` or a local proxy.
Acceptable as-is.

#### INFO-2 — `health_check()` makes a live `ping` call to the LLM provider

**File:** `gemini_transcreation.py:430–455`, `openai_transcreation.py:431–460`,
`openrouter_transcreation.py:459–486`

Each engine's `health_check` performs a real `client.models.generate_content` /
`client.chat.completions.create` with `max_tokens=10` and a "ping" message. On
startup this consumes a small number of tokens (and credits / rate-limit
budget). Mention noted because it's not a free no-op probe.

#### INFO-3 — Deploy step writes empty `ANTHROPIC_API_KEY=` / `*_MODEL=` lines unconditionally

**File:** `.github/workflows/deploy.yml:206–215`

Lines 206 (`ANTHROPIC_API_KEY`), 207 (`ANTHROPIC_MODEL`), 209 (`GEMINI_MODEL`),
211 (`OPENAI_MODEL`), 213 (`OPENROUTER_MODEL`), and 215 (`TZ`) are written
unconditionally — even if their source values are empty. Result: `.env` gets
e.g. `ANTHROPIC_API_KEY=` (blank) lines for unused providers. Python's dotenv
parses empty values as empty strings, and the dispatcher's `_has_key()` check
correctly treats those as "absent" (it strips). Cosmetic only.

#### INFO-4 — `printf '%s\n'` does not escape `\n` or `\t` inside secret values

**File:** `.github/workflows/deploy.yml:206–215`

`printf 'KEY=%s\n' "$VALUE"` will faithfully interpolate `\n`, `\\`, etc. if
the secret contains them. Most dotenv parsers fold this into a multi-line
record or fail. Today's LLM keys are single-line alphanumerics + `[_=.-]`, so
no risk in practice. Pairs with **L-4**'s recommendation to use `read -r`
opaquely on stdin.

---

## Verification appendix

Commands run during the audit (read-only, no network):

```bash
# File-permission and tracking checks
ls -la .env .env.example .gitignore .pre-commit-config.yaml
git ls-files | grep -E '\.env'
git log --all --diff-filter=A -- .env
git log --all --diff-filter=A -- '*.env'

# Hardcoded-secret scan
grep -nIE 'sk-ant-[A-Za-z0-9]|sk-or-v|sk-proj|sk-svcacct|AIza[A-Za-z0-9]|[0-9]{8,11}:AAH|password\s*=|api[_-]?key\s*=' \
  --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' --include='*.toml' -r .

# History-deep secret scan (216 commits)
git log --all -p | grep -nIE 'sk-ant-[A-Za-z0-9]|sk-or-v[0-9]|sk-proj-[A-Za-z0-9]|sk-svcacct-[A-Za-z0-9]|AIza[A-Za-z0-9_-]{30,}|^\+[0-9]{8,11}:AAH[A-Za-z0-9_-]{30,}'

# Regex correctness for each LLM key shape (in-process Python; no network)
python3 -c '<see audit transcript: tested all 8 shapes through the live _redact_text pipeline>'

# SQL safety
grep -nE 'execute.*f["'\'']|execute.*\.format|execute.*%[sd]' news_bot.py hw_review.py pending_articles_repo.py outage_state.py

# Inbound-listener scan
grep -nE "Flask|FastAPI|aiohttp\.web|http\.server|HTTPServer|socket\.bind|bind\(\(|listen\(\)|Updater|create_server|run_polling|run_webhook|set_webhook|webhook|TCPServer" *.py

# os.environ leak surface
grep -nE 'os\.environ\b' *.py

# Logger that might log api_key
grep -nE 'logger\.(info|warning|error|debug|exception)\(.*api_key' *.py
```

All commands ran read-only against the local working tree. No real API call
was made; no secret value was read or printed.

---

## Final verdict: **PASS_WITH_NOTES**

The multi-LLM additions inherit the strong secret-management invariants
established in Wave 9, and the new code (engines + dispatcher + redaction
extensions + pre-commit hook) is well-engineered. No critical or high
findings. The one Medium finding (M-1: SSH-host TOFU) and the five Low
findings are hardening opportunities that should be tracked but do not block
the deploy.
