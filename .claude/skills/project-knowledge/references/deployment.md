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
- `TELEGRAM_ADMIN_ID` — personal Telegram numeric chat_id for admin pings (queue-pressure, idle-fallback heads-up, error digests). User must `/start` the bot first for DMs to work.
- `TELEGRAPH_ACCESS_TOKEN` — auto-created by `telegraph_publisher.ensure_access_token` on first run and persisted to `.env`.

**Optional (tunable defaults):**

- `QUEUE_CAP` (default `10`) — max size of `pending_articles`.
- `IDLE_TIMEOUT_HOURS` (default `2`) — age threshold before first admin ping on a stale row.
- `GRACE_WINDOW_HOURS` (default `2`) — delay after ping before auto-fallback publishes.

These must be present on the production server (systemd EnvironmentFile or `source .env` in the cron wrapper). Operator-side `hw_review.py` also reads them locally from `.env` when publishing manually.

---

## Deployment Triggers

**Production:** `bash deploy.sh` with `SSH_HOST` + `DEPLOY_PATH` env overrides. SCP-based — see `FILES` list in [deploy.sh](../../../../deploy.sh). Server-side `pip install -r requirements.txt` runs after copy.

**Files deployed** (cron-path only; operator-side modules excluded): `news_bot.py`, `pending_articles_repo.py`, `telegraph_publisher.py`, source parsers (`autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py`), `feeds.json`, `requirements.txt`, `.env.example`.

**Files NOT deployed**: `hw_review.py`, `preview_renderer.py` — operator runs these locally in Claude Code session, not on the VPS.

**Staging:** Not configured. For test publishes without touching the prod channel, operator temporarily swaps `TELEGRAM_CHANNEL_ID` in `.env` to a personal chat ID.

---

## Scheduling

Every 12 hours via `schedule.every(12).hours.do(job)` inside `news_bot.main()`. One job() call also fires immediately on `python3 news_bot.py` startup, then waits 12h for the next.

For production, prefer systemd-managed long-running process over raw `nohup` — `schedule` runs in-process.

---

## Pre-Deploy Checklist

- [ ] `git pull` latest `dev` (or whichever branch being deployed)
- [ ] `python3 -m pytest tests/ -q` green locally
- [ ] `.env` on server has all 4 required vars + optional tuning vars
- [ ] `news.db` present on server (if fresh VPS — it auto-creates via `init_db()` on first run, but 4-table schema migration must succeed; see `tests/test_migration.py` for invariants)
- [ ] Any currently running `news_bot.py` process stopped before file copy
- [ ] `bash deploy.sh` — verify SCP output + `pip install` output

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
