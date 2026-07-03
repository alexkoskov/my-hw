# ❌ CANCELLED (2026-07-02)

Migration to 45.90.216.165 was cancelled: that VPS is in **Moscow, Russia** (Firstbyte) — Telegram is blocked there, so the bot couldn't reliably reach `api.telegram.org` to post. Prod stays on the **Netherlands** server `148.135.207.54` (DeluxHost). New server cleaned up / decommissioned. Rule: host the bot OUTSIDE Russia. Runbook kept below for reference only.

---

# Migration runbook — prod bot → new VPS 45.90.216.165 (2026-06-30)

Old prod: `hwbot@148.135.207.54` (DeluxHost, still reachable). New: `45.90.216.165`
(fresh Ubuntu VPS, operator logs in as `root`). Bot uses `python-dotenv` →
`.env` in `/home/hwbot/bot`. Python 3.11+ (CI uses 3.13).

**Critical invariants**
- **No spam:** the new bot MUST start with the OLD `news.db` (holds `processed_news`).
  A blank DB re-posts months of backlog.
- **No double-post:** old + new run the SAME bot token → SAME channel. FREEZE the
  old bot BEFORE starting the new one.
- **Secrets:** copied from the old server's `.env`; operator handles them, never pasted here.

---

## Part A — pull config off the OLD server (run on your Mac)
```bash
scp hwbot@148.135.207.54:/home/hwbot/bot/.env             ~/hw-env
scp hwbot@148.135.207.54:/home/hwbot/.ssh/authorized_keys ~/hw-authkeys
# news.db is copied later, at cutover (Part D), so it's the freshest state.
```

## Part B — bootstrap the NEW server (SSH in: `ssh root@45.90.216.165`)
```bash
apt-get update && apt-get install -y python3 python3-pip git rsync
id hwbot || adduser --disabled-password --gecos "" hwbot
sudo -u hwbot git clone https://github.com/alexkoskov/my-hw.git /home/hwbot/bot
sudo -u hwbot git -C /home/hwbot/bot checkout main
sudo -u hwbot pip3 install --user -r /home/hwbot/bot/requirements.txt \
  || sudo -u hwbot pip3 install --user --break-system-packages -r /home/hwbot/bot/requirements.txt
```

### systemd service (Restart=on-failure, auto-start on boot)
```bash
cat >/etc/systemd/system/news_bot.service <<'UNIT'
[Unit]
Description=Hot Wheels News Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hwbot
WorkingDirectory=/home/hwbot/bot
ExecStart=/usr/bin/python3 /home/hwbot/bot/news_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable news_bot.service     # start on boot; do NOT start yet (cutover in Part D)
```

### sudoers — NOPASSWD for hwbot (now includes stop/start, fixing the old "can't pause" gap)
```bash
cat >/etc/sudoers.d/news_bot <<'SUDO'
hwbot ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart news_bot.service, /usr/bin/systemctl stop news_bot.service, /usr/bin/systemctl start news_bot.service, /usr/bin/systemctl status news_bot.service, /usr/bin/journalctl -u news_bot.service
SUDO
chmod 0440 /etc/sudoers.d/news_bot
visudo -c        # must print "parsed OK"
```

### cron for hwbot — nightly DB backup (02:00) + heartbeat watchdog (19:00)
```bash
mkdir -p /home/hwbot/backup && chown hwbot:hwbot /home/hwbot/backup
sudo -u hwbot bash -c '(crontab -l 2>/dev/null | grep -vE "backup_db.sh|watchdog.sh"; \
  echo "0 2 * * * /bin/bash /home/hwbot/bot/backup_db.sh"; \
  echo "0 19 * * * /bin/bash /home/hwbot/bot/watchdog.sh") | crontab -'
```

### push the copied SSH keys (so GitHub CI + you can log in as hwbot)
Run on your **Mac**:
```bash
ssh root@45.90.216.165 'mkdir -p /home/hwbot/.ssh && chmod 700 /home/hwbot/.ssh'
scp ~/hw-authkeys root@45.90.216.165:/home/hwbot/.ssh/authorized_keys
scp ~/hw-env      root@45.90.216.165:/home/hwbot/bot/.env
```

### fix ownership/permissions (on NEW server as root)
```bash
chown -R hwbot:hwbot /home/hwbot/bot /home/hwbot/.ssh
chmod 600 /home/hwbot/.ssh/authorized_keys /home/hwbot/bot/.env
```

## Part C — point auto-deploy at the new server
GitHub → repo **Settings → Secrets and variables → Actions** → edit secret
**`SSH_HOST`** → `45.90.216.165`. (This — not the server `.env` — is what the
deploy workflow uses. `SSH_USER`/`DEPLOY_PATH`/`SSH_PRIVATE_KEY` stay the same.)

## Part D — CUTOVER (do these back-to-back to avoid double-posting)
```bash
# 1) FREEZE the old bot so only one instance posts to the channel:
ssh hwbot@148.135.207.54 'kill -STOP $(systemctl show -p MainPID --value news_bot.service) && echo OLD_FROZEN'
# 2) copy the FRESHEST news.db from old → new (via your Mac):
scp hwbot@148.135.207.54:/home/hwbot/bot/news.db ~/hw-news.db
scp ~/hw-news.db root@45.90.216.165:/home/hwbot/bot/news.db
ssh root@45.90.216.165 'chown hwbot:hwbot /home/hwbot/bot/news.db'
# 3) start the NEW bot (fires an immediate tick):
ssh root@45.90.216.165 'systemctl start news_bot.service && systemctl is-active news_bot.service'
```

## Part E — verify + finish
```bash
# new bot healthy + posting?
ssh hwbot@45.90.216.165 "systemctl is-active news_bot.service; sudo journalctl -u news_bot.service | tail -30"
```
- Expect a startup tick + an admin `[E008]`/`[E009]` ping (INSTANCE_LABEL=prod from the copied .env).
- Trigger a real deploy to confirm CI reaches the new box: push any trivial commit to
  `main` (or GitHub → Actions → Deploy → Re-run) → Deploy must go green against 45.90.216.165.
- Only after the new bot is confirmed posting: decommission old (leave it FROZEN, or
  cancel the DeluxHost VPS).
- Delete the temp secret files on your Mac: `rm ~/hw-env ~/hw-authkeys ~/hw-news.db`.

**Known-good after this:** Restart=on-failure + boot-enable, watchdog `[E099]`, DB
backups, sudoers now allows stop/start (pausing no longer needs SIGSTOP).
