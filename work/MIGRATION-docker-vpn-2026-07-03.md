# Migration runbook — news bot → RU VPS 45.90.216.165 via shared VPN (Docker)

Continue-at-home guide. Move the Hot Wheels news bot off the flaky NL server
(`148.135.207.54`) onto the Moscow VPS `45.90.216.165`, running as a **Docker
container routed through the existing `shared-vpn` gateway** (sing-box VLESS at
`172.28.0.2` on the `vpnnet` network — Telegram is filtered from the RU host, so
egress MUST go through the tunnel). Same pattern as the existing `intake-bot`.

Artifacts already in the repo (branch `dev`): `Dockerfile`, `docker-compose.yml`,
`.dockerignore`.

---

## Step 0 — confirm Telegram is reachable through the VPN (once)
```bash
ssh root@45.90.216.165 'docker run --rm --network vpnnet --cap-add NET_ADMIN alpine:latest sh -c "ip route replace default via 172.28.0.2 >/dev/null 2>&1; apk add --no-cache curl >/dev/null 2>&1; echo EXIT_IP:; curl -s --max-time 15 http://ip-api.com/json/?fields=query,country,city; echo; for u in https://api.telegram.org https://api.telegra.ph; do curl -s --max-time 15 -o /dev/null -w \"%{http_code}  \$u\n\" \"\$u\"; done"'
```
Expect: exit IP country **not Russia**, and telegram/telegra.ph return an HTTP
code (not `000`). (The intake-bot already posts through this VPN, so this should
pass.) If `000` → the gateway/tunnel needs a look before continuing.

## Step 1 — get the code + Docker files on the host
```bash
ssh root@45.90.216.165
git clone https://github.com/alexkoskov/my-hw.git /root/hw-news
cd /root/hw-news && git checkout dev      # branch that has Dockerfile + docker-compose.yml
mkdir -p /root/hw-news/data
```

## Step 2 — .env (secrets) from the OLD server + DB path
From your **Mac**:
```bash
scp hwbot@148.135.207.54:/home/hwbot/bot/.env /tmp/hwenv
scp /tmp/hwenv root@45.90.216.165:/root/hw-news/.env
rm /tmp/hwenv
```
Then on the host, append the container DB path (idempotent):
```bash
grep -q '^DB_FILE=' /root/hw-news/.env || echo 'DB_FILE=/data/news.db' >> /root/hw-news/.env
```
(The .env already carries TELEGRAM_*, OPENROUTER_API_KEY, LLM_PROVIDER=openrouter,
INSTANCE_LABEL=prod, TELEGRAPH_ACCESS_TOKEN, TZ=Europe/Moscow — copied verbatim.)

## Step 3 — CUTOVER (back-to-back: no double-posting, freshest DB)
```bash
# 1) FREEZE the old NL bot so only one instance posts to the channel:
ssh hwbot@148.135.207.54 'kill -STOP $(systemctl show -p MainPID --value news_bot.service) && echo OLD_FROZEN'
# 2) copy the freshest news.db from OLD → new /data (via your Mac):
scp hwbot@148.135.207.54:/home/hwbot/bot/news.db /tmp/news.db
scp /tmp/news.db root@45.90.216.165:/root/hw-news/data/news.db && rm /tmp/news.db
# 3) build + start the container (on the host):
ssh root@45.90.216.165 'cd /root/hw-news && docker compose up -d --build'
```

## Step 4 — verify
```bash
ssh root@45.90.216.165 'cd /root/hw-news && docker compose ps && docker logs --tail 40 hw-news-bot'
```
Expect: `Starting daily cron tick`, an admin `[E008]`/`[E009]` Telegram ping
(INSTANCE_LABEL=prod), and — if there's fresh HW content — a `Posted to Telegram`
line. A post should appear in the channel.

## Step 5 — finish
- Only after the new container is confirmed posting: the OLD NL bot stays FROZEN;
  once you're happy, cancel the DeluxHost VPS.
- **Leave GitHub `SSH_HOST` alone / repoint later:** the old `deploy.yml` (scp to
  a host + systemd) does NOT fit the Docker host. For now redeploys = `git pull &&
  docker compose up -d --build` on the host. (A proper Docker CI can come later,
  like the intake-bot's own deploy workflow.)

## Notes / open items
- Redeploy = on the host: `cd /root/hw-news && git pull && docker compose up -d --build`.
- Don't rebuild/restart inside the 10:00–20:00 МСК publish window (restart resets
  the in-process day schedule — same rule as before).
- Monitoring: in-bot `[E008]/[E009]/[E017]` pings still work (via Telegram). The
  host `watchdog.sh` `[E099]` and nightly `backup_db.sh` don't apply to the
  container as-is — wire container equivalents later if wanted (e.g. a cron on the
  host that `docker exec`s the DB backup, and a heartbeat check).
