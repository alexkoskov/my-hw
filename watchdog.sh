#!/bin/bash
# news_bot heartbeat watchdog. Runs once daily via cron AFTER the
# expected cron tick + publish window. If the heartbeat file is older
# than THRESHOLD_SECONDS, pings the operator via Telegram Bot API.
#
# Motivation (prod 2026-06-08): news_bot hung in `feedparser.parse`
# without timeout. The service stayed `active (running)` per systemd
# (no crash → no `Restart=on-failure` rescue), the daily admin ping
# never went out, and the operator only noticed by chance ~3 hours
# later. This watchdog catches the alive-but-stuck class — any path
# where job() doesn't reach `_record_heartbeat()` at the end produces
# a stale heartbeat the next morning.
#
# Wire up (operator does once per instance):
#   $ crontab -e
#   # 22:00 МСК = 19:00 BST = 19:00 system time (VPS is BST)
#   0 19 * * *  /home/hwbot/bot/watchdog.sh
#
# To verify wired correctly:
#   $ /home/hwbot/bot/watchdog.sh && echo "OK (heartbeat fresh)"
#   $ touch -d "2 days ago" ~/.cache/news_bot/last_tick.ts
#   $ /home/hwbot/bot/watchdog.sh  # should send a [E099] ping
#   $ rm ~/.cache/news_bot/last_tick.ts && /usr/bin/python3 -c "import news_bot; news_bot._record_heartbeat()"

set -uo pipefail

# Load TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_ID, INSTANCE_LABEL from the
# instance's .env (colocated with this script).
ENV_FILE="$(dirname "$(readlink -f "$0")")/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "[watchdog] missing env file: $ENV_FILE" >&2
    exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

HEARTBEAT="$HOME/.cache/news_bot/last_tick.ts"
THRESHOLD_SECONDS=$((26 * 60 * 60))   # 26h — covers a quiet day yesterday + any
                                      # publish-loop running long today; tighter
                                      # would false-positive on legitimate slow
                                      # publish days.

LABEL_PREFIX=""
if [[ -n "${INSTANCE_LABEL:-}" ]]; then
    LABEL_PREFIX="[${INSTANCE_LABEL}] "
fi

if [[ ! -f "$HEARTBEAT" ]]; then
    msg="${LABEL_PREFIX}🚨 [E099] news_bot heartbeat missing — service may have never completed a tick"
else
    now=$(date +%s)
    mtime=$(stat -c %Y "$HEARTBEAT")
    age=$((now - mtime))
    if (( age <= THRESHOLD_SECONDS )); then
        exit 0   # fresh — nothing to do
    fi
    hours=$((age / 3600))
    msg="${LABEL_PREFIX}🚨 [E099] news_bot last tick ${hours}h ago — service hung, dead, or never fired today"
fi

curl -fsS --max-time 20 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_ADMIN_ID}" \
    --data-urlencode "text=${msg}" \
    -o /dev/null \
    || echo "[watchdog] curl to Telegram failed (exit $?)" >&2

exit 0
