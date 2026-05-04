# Deployment & Operations

## Purpose
Deployment process, infrastructure, and production operations for AI agents.

---

## Deployment Platform

**Platform:** Any Linux server with Python 3.8+ (e.g., VPS, Raspberry Pi, cloud VM)

**Type:** Cron job (scheduled task) – the script runs as a standalone Python process.

**Why this platform:** Low cost, full control over scheduling, and no need for a web server or container orchestration.

---

## Access Information

**SSH Access:**
- Production: `ssh user@example.com` (deploy path: `/home/user/bot`)

> If not configured, agent will request: server address, username, and port.

**Credentials location:** Server credentials are stored in a local password manager (e.g., 1Password, KeePass) or as SSH keys.

---

## Environment Variables

**See:** [.env.example](../../.env.example) in project root

**Required for production cron:**

- `TELEGRAM_BOT_TOKEN` — bot token from BotFather. Sensitive — never echo/log. Code suppresses `httpx`/`httpcore` INFO to prevent URL-path leak.
- `TELEGRAM_CHANNEL_ID` — `@myhwchannel123` or numeric ID.
- `TELEGRAM_ADMIN_ID` — personal Telegram numeric chat_id for admin pings (daily schedule, outage protocol, backlog warning, error digests). User must `/start` the bot first for DMs to work.
- `TELEGRAPH_ACCESS_TOKEN` — auto-created by `telegraph_publisher.ensure_access_token` on first run and persisted to `.env`.
- `ANTHROPIC_API_KEY` — Claude API key for the auto-publish path's primary translator (llm-transcreation feature). Get from https://console.anthropic.com → API Keys → Create Key (format `sk-ant-api03-…`). **Sensitive** — redacted from logs by `_TokenRedactingFilter` (pattern `sk-ant-[A-Za-z0-9_=.-]{16,}`) and from admin Telegram pings by the shared `_redact_text` helper. NEVER echo/log/commit. Without this var, `claude_transcreation` hits `AuthenticationError` on the first API call → 2-ping outage protocol fires → bot drops into Google Translate fallback for the day.
- `TZ=Europe/Moscow` — process timezone. Cron triggers at 12:00 МСК via `pytz`-aware `schedule.Job.at(...)`, so the trigger fires correctly regardless of `TZ`. But `os.getenv('TZ')` is checked at startup (Decision 14 health check #2) — if it doesn't equal `'Europe/Moscow'`, the bot sends an admin warning ping ("log timestamps may show non-MSK times").

**Optional (tunable defaults):**

- `ANTHROPIC_MODEL` (default `claude-haiku-4-5`) — Claude model name. Override to `claude-sonnet-4-6` for higher quality at ~5× cost. Best stored as a GitHub Actions repo `var` rather than a secret; safe to log.
- `INSTANCE_LABEL` — short label distinguishing this bot instance in admin pings. When set (e.g. `prod` or `test`), `send_admin_notification` prepends `[<label>] ` to every admin-bound message. Empty / unset → no prefix (backward-compatible). Set ONCE manually in each instance's `.env` on the server; the deploy workflows do NOT manage this var (their regex strips only LLM-related keys + TZ, leaving INSTANCE_LABEL untouched). Used by the two-instance topology (see below).

These must be present on the production server (systemd EnvironmentFile or `source .env` in the cron wrapper). Operator-side `hw_review.py` also reads them locally from `.env` when publishing manually. The deploy workflow (`.github/workflows/deploy.yml` step "Write runtime env vars to server `.env`") writes `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, and `TZ` to the server's `.env` idempotently on every deploy — repeated deploys do not duplicate lines, and any pre-existing keys (TELEGRAM_*, TELEGRAPH_ACCESS_TOKEN) are preserved verbatim.

---

## Two-instance topology (prod + test on the same VPS)

Both bot instances run on `148.135.207.54` as separate systemd units, each with its own deploy directory, `.env`, `news.db`, and Telegram channel target. The single VPS hosts both.

| Instance | Deploy path | Service | Channel | Branch | Workflow |
|---|---|---|---|---|---|
| **prod** | `/home/hwbot/bot/` | `news_bot.service` | `-1004027529994` (prod) | `main` | `deploy.yml` |
| **test** | `/home/hwbot/bot_test/` | `news_bot_test.service` | `@myhwchannel123` (test) | `dev` | `deploy_test.yml` |

The bot Telegram TOKEN is shared (one bot account posts to both channels). The Anthropic / OpenRouter / etc. API keys are shared (CI writes them to both `.env` files via the respective deploy workflow). Per-instance values that differ:

- `TELEGRAM_CHANNEL_ID`
- `INSTANCE_LABEL` (`prod` / `test`) — prepended to every admin ping by `news_bot.send_admin_notification` so the operator can distinguish source.
- `news.db` — independent SQLite files, no contention.

`INSTANCE_LABEL` and `TELEGRAM_CHANNEL_ID` are set ONCE manually on the server and NOT managed by the deploy workflow's strip-then-append rewrite (the workflow's regex strips only LLM-related keys + TZ, leaving everything else untouched).

**Iteration cycle:** `git push origin dev` → CI on dev → `deploy_test.yml` → test instance updates → operator inspects test channel → on confirmation `git checkout main && git merge dev && git push origin main` → CI on main → `deploy.yml` → prod instance updates.

## Deployment Triggers

**Production (default — GitHub Actions CI):** `git push origin main` → `.github/workflows/ci.yml` runs pytest → on green, `.github/workflows/deploy.yml` triggers via `workflow_run`, SCPs the FILES list to the VPS at `$DEPLOY_PATH` (= `/home/hwbot/bot/`), runs `pip install --user -r requirements.txt` on the server, then `sudo systemctl restart news_bot.service`. One concurrent prod deploy at a time; queued runs replace pending.

**Test / staging (GitHub Actions CI):** `git push origin dev` → `ci.yml` runs pytest → on green, `.github/workflows/deploy_test.yml` triggers via `workflow_run`, SCPs the same FILES list to `$DEPLOY_PATH_TEST` (= `/home/hwbot/bot_test/`), `pip install`, then `sudo systemctl restart news_bot_test.service`. Independent concurrency group from prod (`deploy-test`). Manual run available via `Actions → Deploy test → Run workflow`.

**GitHub Secrets required** (Settings → Secrets and variables → Actions → New repository secret):
- `SSH_HOST` — VPS hostname or IP (e.g. `148.135.207.54`).
- `SSH_USER` — SSH login user on the VPS (`hwbot` for both deploy workflows).
- `DEPLOY_PATH` — prod deploy path on the VPS (= `/home/hwbot/bot/`). Used by `deploy.yml` (main branch).
- `DEPLOY_PATH_TEST` — test deploy path on the VPS (= `/home/hwbot/bot_test/`). Used by `deploy_test.yml` (dev branch). Required only for the two-instance topology; without it `deploy_test.yml` fails fast with a clear error.
- `SSH_PRIVATE_KEY` — full PEM-encoded private key (including `-----BEGIN…END-----` lines) for the deploy account. Generate a dedicated key for CI: `ssh-keygen -t ed25519 -f ~/.ssh/hwbot_deploy -C "github-actions-hwbot"`; append the `.pub` half to the VPS account's `~/.ssh/authorized_keys`; paste the private half into the secret. The same key is used by both prod and test deploy workflows.
- `ANTHROPIC_API_KEY` — Claude API key (format `sk-ant-api03-…`). Get from https://console.anthropic.com → API Keys → Create Key, copy the value, paste it as a new repository secret with name `ANTHROPIC_API_KEY`. The deploy workflow forwards it to the server's `.env` over ssh stdin; values never appear on a command line and are auto-redacted in workflow logs. Both prod and test instances share this key.

**GitHub Variables (optional, non-sensitive)** (Settings → Secrets and variables → Actions → Variables):
- `ANTHROPIC_MODEL` — defaults to `claude-haiku-4-5` if unset. Set this `var` to override (e.g. `claude-sonnet-4-6`).
- `TZ` — defaults to `Europe/Moscow` if unset.

Both are stored as `vars` (not `secrets`) because they aren't sensitive — visible in workflow logs is fine.

**Production (fallback — manual SCP):** `bash deploy.sh` with `SSH_HOST` + `DEPLOY_PATH` env overrides. Same FILES list as the workflow. Use when GitHub Actions is unavailable or for emergency hotfixes that can't go through `main`. Note: `deploy.sh` does NOT update `.env` — the operator must edit the server `.env` directly when bootstrapping a new VPS or rotating `ANTHROPIC_API_KEY`.

**Files deployed** (cron-path only; operator-side modules excluded):
- Core cron-path: `news_bot.py`, `pending_articles_repo.py`, `telegraph_publisher.py`, source parsers (`autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py`).
- llm-transcreation runtime modules (added by the llm-transcreation feature; all imported by `news_bot.py` at startup — without any of them, `news_bot` crashes with `ImportError` on the first cron tick): `claude_transcreation.py`, `compute_publish_slots.py`, `outage_state.py`.
- Config: `feeds.json`, `requirements.txt`, `.env.example`.
- Claude API system prompt: `.claude/skills/project-knowledge/references/ux-guidelines.md`. **Note (Decision 8 deploy quirk):** `scp` is invoked WITHOUT `-r`, so subdirs are flattened — on the server the file lands at `$DEPLOY_PATH/ux-guidelines.md` (NOT inside a subdir). `claude_transcreation._load_prompt` tries the original subdir path first, then falls back to the flat filename — both layouts work, so the operator should not be surprised to find the file at the top level of `DEPLOY_PATH`.

The list lives in two places — `.github/workflows/deploy.yml` and `deploy.sh` — and is asserted byte-for-byte identical by the headline comments. **INVARIANT:** any new first-party import added to `news_bot.py` MUST be mirrored into both FILES arrays. Otherwise the server hits `ImportError` on the next cron tick with no CI signal beforehand.

**Files NOT deployed**: `hw_review.py`, `preview_renderer.py` — operator runs these locally in Claude Code session, not on the VPS.

**Service auto-restart:** the deploy workflow ends with `ssh ... "sudo systemctl restart news_bot.service"` — code changes go live immediately, NOT deferred to the next 10:00 МСК cron tick. The SSH step depends on the sudoers NOPASSWD rule below; if it's missing, the deploy step prints a `::error::` hint pointing at `/etc/sudoers.d/news_bot`.

**Server-side sudoers** (`/etc/sudoers.d/news_bot`, mode **0440 — anything else and sudo silently ignores the file**):

```
hwbot ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart news_bot.service, /usr/bin/systemctl restart news_bot_test.service, /usr/bin/systemctl status news_bot.service, /usr/bin/systemctl status news_bot_test.service, /usr/bin/journalctl -u news_bot.service, /usr/bin/journalctl -u news_bot_test.service
```

The single rule covers BOTH instances — prod (`news_bot.service`) and test (`news_bot_test.service`). Install via `ssh root@<host>` (one-time): `visudo -f /etc/sudoers.d/news_bot`, paste the line, `chmod 0440 /etc/sudoers.d/news_bot`, `visudo -c` to verify. Without this rule the deploy's restart step fails with "terminal is required to read the password" — the only command path that needs explicit privilege escalation is the systemd restart.

**Rollback:** `git revert HEAD && git push origin main` (for prod) or `... origin dev` (for test) — the respective deploy workflow redeploys the parent commit and restarts the matching service. ~2-3 min total. For schema rollback, restore `news.db` from `/home/hwbot/backup/` (see Backups below).

**Staging:** Yes — the `news_bot_test.service` instance on the same VPS is the staging environment. Posts go to `@myhwchannel123` (operator's only-subscriber test channel), not to the prod channel `-1004027529994`. Activated by pushing to the `dev` branch, which triggers `deploy_test.yml`. See "Two-instance topology" section above.

---

## Scheduling

Daily fixed-time cron at **10:00 МСК** via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` inside `news_bot.main()`. One `job()` call also fires immediately on `python3 news_bot.py` startup so a deploy doesn't wait until 10:00 for the next tick. The crash-loop guard prevents burst posting on rapid restarts.

After fetch, the publish loop distributes the day's articles across the **10:00–20:00 МСК** window with a **40-minute floor between publishes** and a **max of 15 publishes/day**. Excess articles carry over to the next day's pending queue (no hard cap on backlog; AC20 admin-warning fires when `len(pending) > 50`).

**Window invariant**: `news_bot.WINDOW_START_TIME` and `WINDOW_END_TIME` MUST be passed explicitly to `compute_publish_slots(...)` — the function defaults to `13:00`/`20:00`, which silently shrinks the window to 7h (≤ 11 slots) if you forget. Regression-guarded by `tests/test_integration.py::test_recompute_schedule_with_window_kwargs`.

For production, prefer systemd-managed long-running process over raw `nohup` — `schedule` runs in-process. Container restart mid-window: the next `job()` recomputes slots from the current time to 20:00 МСК; already-published rows are skipped via Decision 9 idempotency (telegraph_url presence).

---

## Pre-Deploy Checklist

- [ ] `git pull` latest `dev` (or whichever branch being deployed)
- [ ] `python3 -m pytest tests/ -q` green locally
- [ ] `.env` on server has all required vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID`, `TELEGRAPH_ACCESS_TOKEN`, `ANTHROPIC_API_KEY`, `TZ=Europe/Moscow` (and optional `ANTHROPIC_MODEL` if overriding the Haiku default). The deploy workflow writes the last three idempotently on every deploy, so the operator only needs to seed them once on a fresh VPS.
- [ ] `ux-guidelines.md` reaches the server during deploy. Either path works — `_load_prompt` falls back from the subdir layout to the flat filename. After deploy, `ssh ... "ls $DEPLOY_PATH/ux-guidelines.md"` should return the file.
- [ ] `news.db` present on server (if fresh VPS — it auto-creates via `init_db()` on first run; the schema migration includes the new `bot_state(key, value)` table for the llm-transcreation outage state machine, also idempotent — see `tests/test_migration.py` for invariants).
- [ ] Any currently running `news_bot.py` process stopped before file copy.
- [ ] `bash deploy.sh` (manual fallback) or `gh workflow run deploy.yml` — verify SCP output + `pip install` output (anthropic + pytz pulled).

---

## Rollback Procedure

**Platform rollback:** Replace the current `news_bot.py` with the previous version (from git history or backup) and restart the service.

**Manual steps if needed:** If the database schema changed and caused issues, restore the backup of `news.db`.

**Approximate time:** ~2 minutes (file copy + service restart).

---

## Environments

**Production:** The script runs on a dedicated Linux server (no public URL). Deploys from `main` branch (manual copy).

---

## Monitoring & Observability

### Logging

**Where:** stdout (captured by systemd journal or cron mail)
**Format:** Plain text with timestamps (`%(asctime)s - %(name)s - %(levelname)s - %(message)s`)

**Secret hygiene:** `httpx` and `httpcore` loggers are forced to `WARNING` at startup (see `news_bot._configure_third_party_logging`) because their INFO-level records include full URLs — and Telegram Bot API puts the bot token directly in the URL path (`/bot<TOKEN>/sendMessage`). Without the suppression, every send would leak the token into journal. Regression test: `tests/test_no_token_leak_in_logs.py`.

### Error Tracking

**Tool:** None
**Config:** Not configured

### Health Checks

**Endpoint:** None (no web server)
**Checks:** Manual verification via Telegram channel posts and log inspection.

### Metrics

**Analytics:** None
**Key metrics:** N/A

### Alerts

**Tool:** None
**Rules:** N/A

---

## Backups

`news.db` holds three load-bearing tables (`pending_articles`, `published_articles`, `processed_news`) — losing it means the bot re-publishes every URL the RSS feeds still hold (typically months of backlog). Daily backups via `/home/hwbot/bot/backup_db.sh`:

- Runs at **02:00 server time** via hwbot's user crontab.
- Atomic `sqlite3 .backup` (consistent under concurrent writes).
- Output: `/home/hwbot/backup/news_<YYYY-MM-DD>.db` (mode 700 dir).
- Rotation: `find ... -mtime +7 -delete` keeps last 7 days.

**Reference copy** of the script: `scripts/backup_db.sh` in repo. **Not auto-deployed** (not in `FILES=()` list) — install once on a fresh VPS via the inline heredoc snippet in the same script's docstring.

**Restore from backup:**
```bash
ssh hwbot@<host>
systemctl stop news_bot.service   # via sudoers NOPASSWD if configured
cp /home/hwbot/backup/news_<DATE>.db /home/hwbot/bot/news.db
systemctl start news_bot.service
```

**Manual merge** (e.g. recovering history from a different machine's `news.db` after migration):
```sql
ATTACH '/tmp/other_news.db' AS other;
INSERT OR IGNORE INTO processed_news SELECT * FROM other.processed_news;
DELETE FROM pending_articles WHERE link IN (SELECT link FROM processed_news);
DETACH other;
```

---

## Cost Monitoring

Production runs the auto-publish path through **OpenRouter** (`LLM_PROVIDER=openrouter`, default model `openai/gpt-5.4-mini`). The dispatcher (`llm_transcreation.py`) auto-selects an engine in priority order Anthropic → OpenAI → Gemini → OpenRouter based on which API keys are present, but the operator override via `LLM_PROVIDER` env var pins it. Variant B+ second-pass adds ~$0.005 per long autoevolution article (when `blocks=null` triggers the focused caption-translation call).

**Where to watch:** https://openrouter.ai/activity → daily breakdown by model. (For the legacy Anthropic path: https://console.anthropic.com → Usage.)

**Expected cost (default Haiku 4.5):** ~$3/month at ~10 articles/day. Each transcreation call uses ~3,200 system-prompt tokens (`ux-guidelines.md`) + ~1,000–2,000 user-message tokens (one English article) + ~1,000–2,500 output tokens. Prompt caching is intentionally NOT used (slot interval ≥ 40 min ≫ 5-min cache TTL — Decision 6).

**Sonnet 4.6 override:** ~$15/month for higher quality. Set `ANTHROPIC_MODEL=claude-sonnet-4-6` in repo `vars` and redeploy.

**Sanity threshold:** if daily cost exceeds **~$1/day** for more than a week, something is wrong. Likely cause: a source has gone runaway (loops, dupes through dedup) and `pending_articles` is full. The bot caps publishes at 11/day so token cost is bounded, but the AC20 admin-warning at `len(pending) > 50` should already have fired. Operator can `sqlite3 news.db 'SELECT COUNT(*) FROM pending_articles'` on the server to confirm and `DELETE` problematic rows manually.

**Per-call observability** (AC19): every Claude call logs `input_tokens`, `output_tokens`, `latency_ms`, `model_version` at INFO. Cross-check against Anthropic console without instrumentation:

```
ssh user@vps "journalctl -u newsbot -S today | grep 'transcreate.*tokens'"
```
