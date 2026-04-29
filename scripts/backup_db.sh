#!/bin/bash
# Daily backup of news.db with 7-day rotation.
#
# Installed once on the server via:
#   scp scripts/backup_db.sh hwbot@<host>:/home/hwbot/bot/
#   ssh hwbot@<host> "chmod +x /home/hwbot/bot/backup_db.sh && \
#       (crontab -l 2>/dev/null; echo '0 2 * * * /home/hwbot/bot/backup_db.sh') | crontab -"
#
# Runs daily at 02:00 server time (BST in production = 04:00 МСК), well
# before the 10:00 МСК cron tick so the backup captures the previous day's
# final state.
#
# Uses ``sqlite3 .backup`` (not ``cp``) so the file is consistent even if
# the bot is mid-write — sqlite copies the page cache atomically.
set -euo pipefail

BACKUP_DIR="/home/hwbot/backup"
DB="/home/hwbot/bot/news.db"
DATE=$(date +%F)
TARGET="$BACKUP_DIR/news_${DATE}.db"

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
