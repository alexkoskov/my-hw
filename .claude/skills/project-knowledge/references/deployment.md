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

These must be present on the production server (systemd EnvironmentFile or `source .env` in the cron wrapper). Operator-side `hw_review.py` also reads them locally from `.env` when publishing manually. The deploy workflow (`.github/workflows/deploy.yml` step "Write runtime env vars to server `.env`") writes `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, and `TZ` to the server's `.env` idempotently on every deploy — repeated deploys do not duplicate lines, and any pre-existing keys (TELEGRAM_*, TELEGRAPH_ACCESS_TOKEN) are preserved verbatim.

---

## Deployment Triggers

**Production (default — GitHub Actions CI):** `git push origin main` → `.github/workflows/ci.yml` runs pytest → on green, `.github/workflows/deploy.yml` triggers via `workflow_run`, SCPs the FILES list to the VPS, runs `pip install --user -r requirements.txt` on the server. One concurrent deploy at a time; queued runs replace pending. Manual override: `Actions → Deploy → Run workflow` (UI button only appears once `deploy.yml` lives on `main`, so the first deploy MUST go through merge-and-push).

**GitHub Secrets required** (Settings → Secrets and variables → Actions → New repository secret):
- `SSH_HOST` — VPS hostname or IP (e.g. `bot.example.com`).
- `SSH_USER` — SSH login user on the VPS.
- `DEPLOY_PATH` — absolute path on the VPS where files land (e.g. `/home/user/bot`).
- `SSH_PRIVATE_KEY` — full PEM-encoded private key (including `-----BEGIN…END-----` lines) for the deploy account. Generate a dedicated key for CI: `ssh-keygen -t ed25519 -f ~/.ssh/hwbot_deploy -C "github-actions-hwbot"`; append the `.pub` half to the VPS account's `~/.ssh/authorized_keys`; paste the private half into the secret.
- `ANTHROPIC_API_KEY` — Claude API key (format `sk-ant-api03-…`). Get from https://console.anthropic.com → API Keys → Create Key, copy the value, paste it as a new repository secret with name `ANTHROPIC_API_KEY`. The deploy workflow forwards it to the server's `.env` over ssh stdin; values never appear on a command line and are auto-redacted in workflow logs.

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

**Rollback:** `git revert HEAD && git push origin main` — the deploy workflow redeploys the parent commit. ~2-3 min total. For schema rollback, restore `news.db` backup separately (the workflow never touches the DB).

**Staging:** Not configured. For test publishes without touching the prod channel, operator temporarily swaps `TELEGRAM_CHANNEL_ID` in `.env` to a personal chat ID.

---

## Scheduling

Daily fixed-time cron at **12:00 МСК** via `schedule.every().day.at("12:00", tz=pytz.timezone("Europe/Moscow")).do(job)` inside `news_bot.main()` (since the llm-transcreation feature; was `every(12).hours` before). One `job()` call also fires immediately on `python3 news_bot.py` startup so a deploy doesn't wait until midday for the next tick. The crash-loop guard prevents burst posting on rapid restarts.

After fetch, the publish loop distributes the day's articles across the **13:00–20:00 МСК** window with a **40-minute floor between publishes** and a **max of 11 publishes/day**. Excess articles carry over to the next day's pending queue (no hard cap on backlog; AC20 admin-warning fires when `len(pending) > 50`).

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

## Cost Monitoring

The auto-publish path uses Anthropic Claude API (added by the llm-transcreation feature). Cost varies with the `ANTHROPIC_MODEL` choice and the article volume.

**Where to watch:** https://console.anthropic.com → Usage → daily breakdown.

**Expected cost (default Haiku 4.5):** ~$3/month at ~10 articles/day. Each transcreation call uses ~3,200 system-prompt tokens (`ux-guidelines.md`) + ~1,000–2,000 user-message tokens (one English article) + ~1,000–2,500 output tokens. Prompt caching is intentionally NOT used (slot interval ≥ 40 min ≫ 5-min cache TTL — Decision 6).

**Sonnet 4.6 override:** ~$15/month for higher quality. Set `ANTHROPIC_MODEL=claude-sonnet-4-6` in repo `vars` and redeploy.

**Sanity threshold:** if daily cost exceeds **~$1/day** for more than a week, something is wrong. Likely cause: a source has gone runaway (loops, dupes through dedup) and `pending_articles` is full. The bot caps publishes at 11/day so token cost is bounded, but the AC20 admin-warning at `len(pending) > 50` should already have fired. Operator can `sqlite3 news.db 'SELECT COUNT(*) FROM pending_articles'` on the server to confirm and `DELETE` problematic rows manually.

**Per-call observability** (AC19): every Claude call logs `input_tokens`, `output_tokens`, `latency_ms`, `model_version` at INFO. Cross-check against Anthropic console without instrumentation:

```
ssh user@vps "journalctl -u newsbot -S today | grep 'transcreate.*tokens'"
```
