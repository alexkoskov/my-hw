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
- Production: `ssh user@server-ip` (server‑specific; no default)

> If not configured, agent will request: server address, username, and port.

**Credentials location:** Server credentials are stored in a local password manager (e.g., 1Password, KeePass) or as SSH keys.

---

## Environment Variables

**See:** [.env.example](../../.env.example) in project root

**Required variables:**

- `TELEGRAM_BOT_TOKEN` – Token obtained from BotFather for authenticating with the Telegram Bot API.
- `TELEGRAM_CHANNEL_ID` – Username or ID of the target Telegram channel (e.g., `@my_hotwheels_news`).

These variables must be set in the environment where the script runs (e.g., in a systemd service file, cron environment, or shell profile).

---

## Deployment Triggers

**Production:** Manual deployment – copy updated files to the server and restart the cron job / systemd service.

**Staging:** Not configured (single‑environment project).

**Preview:** Not configured.

---

## Pre-Deploy Checklist

- [ ] Verify that all dependencies are installed (`pip install -r requirements.txt`).
- [ ] Ensure environment variables are correctly set on the target server.
- [ ] Test the script locally with a dry run (optional).
- [ ] Stop any currently running instance of the bot.
- [ ] Backup the SQLite database file (`news.db`) if it contains critical state.

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
