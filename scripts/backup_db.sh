#!/bin/bash
# Daily backup of news.db with 7-day rotation.
#
# Paths are env-overridable (fall back to the legacy NL/systemd layout):
#   DB_FILE     — DB to back up (matches news_bot's DB_FILE / the container's).
#   BACKUP_DIR  — where dumps land.
# ``$1`` is also accepted as the DB path (DB_FILE wins if both are set).
#
# NL/systemd host (legacy, test bot):
#   (crontab -l 2>/dev/null; echo '0 2 * * * /home/hwbot/bot/backup_db.sh') | crontab -
#
# Moscow Docker host (prod) — back up the bind-mount source directly on the
# HOST (``sqlite3 .backup`` is consistent even while the container writes):
#   (crontab -l 2>/dev/null; echo '0 2 * * * DB_FILE=/root/hw-news/data/news.db BACKUP_DIR=/root/hw-news/backups /root/hw-news/scripts/backup_db.sh') | crontab -
#   # then copy /root/hw-news/backups OFF-box periodically (rsync/scp to another host).
#
# Runs before the 10:00 МСК tick so the backup captures the previous day's
# final state. Uses ``sqlite3 .backup`` (not ``cp``) for an atomic, consistent
# copy of the page cache.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/hwbot/backup}"
DB="${DB_FILE:-${1:-/home/hwbot/bot/news.db}}"
DATE=$(date +%F)
TARGET="$BACKUP_DIR/news_${DATE}.db"

# Fail loudly if the DB is missing/empty rather than dumping a useless 0-byte
# file — an empty/absent DB is itself the disaster this backup guards against.
if [[ ! -s "$DB" ]]; then
    echo "[backup] DB not found or empty: $DB" >&2
    exit 1
fi

# Idempotent: ensure dir exists with safe perms (sqlite dump may contain
# Telegraph URLs that should not leak to other users on the box).
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Atomic sqlite-level dump.
sqlite3 "$DB" ".backup '$TARGET'"

# Rotation: drop dumps older than 7 days. ``-mtime +7`` matches files
# modified more than 7*24h ago. ``-delete`` is POSIX-y enough on
# GNU/BSD finds; if the target distro removes it later, switch to
# ``-exec rm -f {} +``.
find "$BACKUP_DIR" -name "news_*.db" -type f -mtime +7 -delete

# Optional: log so journalctl/cron mail surface failures.
echo "$(date -Is) backup OK: $TARGET ($(stat -c%s "$TARGET") bytes)"
