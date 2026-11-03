#!/bin/sh
# The route needs root; the worker and its persistent state do not.
set -eu

check_state() {
    python3 - <<'PY'
import os
from pathlib import Path
import sys

def refuse(reason):
    print(f"[startup] {reason}", file=sys.stderr)
    raise SystemExit(1)

if not os.access('/app/prompts/ux-guidelines.md', os.R_OK):
    refuse('runtime prompt is not readable by the worker')

if os.getenv('INSTANCE_LABEL', '').strip().lower() == 'prod':
    if not os.getenv('TELEGRAPH_ACCESS_TOKEN', '').strip():
        refuse('TELEGRAPH_ACCESS_TOKEN must be supplied by the runtime environment')
    db = Path(os.getenv('DB_FILE', ''))
    if str(db) != '/data/news.db' or db.is_symlink():
        refuse('production DB_FILE must be the existing /data/news.db')
    if not db.is_file() or db.stat().st_size == 0:
        refuse('production database is missing or empty; restore the existing ledger')
    if not os.access(db, os.R_OK | os.W_OK):
        refuse('production database must be readable and writable by uid/gid 10001')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(db) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and not os.access(sidecar, os.R_OK | os.W_OK)):
            refuse('SQLite sidecar permissions are incompatible with uid/gid 10001')
    paths = [db, Path(os.getenv('NEWS_BOT_LOCK_FILE', '/data/.news_bot.lock')),
             Path(os.getenv('HEARTBEAT_FILE', '/data/last_tick.ts'))]
    for path in paths:
        if path.is_symlink() or not os.access(path.parent, os.W_OK | os.X_OK):
            refuse('state directories must be writable by uid/gid 10001; check volume ownership')
        if path.exists() and not os.access(path, os.R_OK | os.W_OK):
            refuse('existing state files must be writable by uid/gid 10001')
PY
}

# The parent invokes this check with the final uid and capabilities before it
# changes the route. A legacy root-owned volume must fail without chmod/chown.
if [ "${1:-}" = --check-state ]; then
    check_state
    exit 0
fi

if [ "$#" -eq 0 ]; then
    echo '[startup] no worker command supplied' >&2
    exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
    setpriv --reuid=10001 --regid=10001 --clear-groups \
        --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
        "$0" --check-state
    if [ -n "${VPN_GATEWAY:-}" ]; then
        ip route replace default via "$VPN_GATEWAY"
    fi
    exec setpriv --reuid=10001 --regid=10001 --clear-groups \
        --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
        "$@"
fi

# An explicitly rootless invocation cannot change the route. It is useful for
# offline probes; production's route bootstrap uses the root branch above.
if [ -n "${VPN_GATEWAY:-}" ]; then
    echo '[startup] VPN route bootstrap requires the root entrypoint' >&2
    exit 1
fi
check_state
exec "$@"
