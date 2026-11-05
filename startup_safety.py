"""Local, read-only production gates. No application or network imports."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
import uuid

VOLUME_ID = 'my-hw-moscow-production-v1'
TABLES = frozenset(('processed_news', 'pending_articles', 'published_articles',
                    'failed_articles', 'bot_state', 'fetch_failures'))


class StartupError(RuntimeError):
    def __init__(self, gate, reason, field=None):
        self.gate, self.reason, self.field = gate, reason, field
        super().__init__(f'[startup] fatal gate={gate} reason={reason}' +
                         (f' field={field}' if field else ''))


def mode(env):
    selected = env.get('APP_MODE', '') or 'local'
    if selected not in ('production', 'local', 'test'):
        raise StartupError('CONFIG', 'unknown_mode', 'APP_MODE')
    if selected != 'production' and (
        env.get('INSTANCE_LABEL', '').lower().strip() == 'prod' or
        os.path.abspath(env.get('DB_FILE', '').strip() or 'news.db') == '/data/news.db'
    ):
        raise StartupError('CONFIG', 'partial_prod_contract')
    return selected


def config(env):
    def required(key):
        value = env.get(key, '')
        if not value.strip():
            raise StartupError('CONFIG', 'missing_field', key)
        if value != value.strip():
            raise StartupError('CONFIG', 'invalid_field', key)
        return value

    for key, expected in (('INSTANCE_LABEL', 'prod'), ('DB_FILE', '/data/news.db'),
                          ('TZ', 'Europe/Moscow'), ('PROD_VOLUME_ID', VOLUME_ID)):
        if required(key) != expected:
            raise StartupError('CONFIG', 'invalid_field', key)
    patterns = {'TELEGRAM_BOT_TOKEN': r'[0-9]+:[A-Za-z0-9_-]+',
                'TELEGRAM_CHANNEL_ID': r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-?[0-9]+)',
                'TELEGRAM_ADMIN_ID': r'[0-9]+'}
    for key, pattern in patterns.items():
        value = required(key)
        if not re.fullmatch(pattern, value) or (
            key.endswith('_ID') and not value.startswith('@') and
            not value.lstrip('-0')
        ):
            raise StartupError('CONFIG', 'invalid_field', key)
    if any(c.isspace() for c in required('TELEGRAPH_ACCESS_TOKEN')):
        raise StartupError('CONFIG', 'invalid_field', 'TELEGRAPH_ACCESS_TOKEN')
    provider = required('LLM_PROVIDER')
    prefixes = {'claude': 'ANTHROPIC', 'openai': 'OPENAI',
                'gemini': 'GEMINI', 'openrouter': 'OPENROUTER'}
    if provider not in prefixes:
        raise StartupError('CONFIG', 'provider_mismatch', 'LLM_PROVIDER')
    prefix = prefixes[provider]
    key = prefix + '_API_KEY'
    if provider == 'openrouter' and not env.get(key):
        key = 'OPEN_ROUTER_API_KEY'
    required(key)
    required(prefix + '_MODEL')
    root = Path(__file__).resolve().parent
    if not any(path.is_file() and os.access(path, os.R_OK) for path in (
        root / 'prompts' / 'ux-guidelines.md', root / 'ux-guidelines.md',
    )):
        raise StartupError('CONFIG', 'missing_field', 'RUNTIME_PROMPT')


def route(path=Path('/proc/net/route'), ipv6file=Path('/proc/net/ipv6_route')):
    try:
        defaults = [line.split() for line in path.read_text().splitlines()[1:]
                    if len(line.split()) >= 8 and line.split()[1] == '00000000'
                    and line.split()[7] == '00000000' and int(line.split()[3], 16) & 1]
    except (OSError, ValueError):
        defaults = []
    if not defaults:
        raise StartupError('ROUTE', 'missing_default')
    if any(row[2].upper() != '02001CAC' for row in defaults):
        raise StartupError('ROUTE', 'wrong_gateway')
    try:
        for line in ipv6file.read_text().splitlines():
            row = line.split()
            if len(row) >= 10 and row[0] == '0' * 32 and row[1] == '00':
                flags = int(row[8], 16)
                if flags & 1 and not flags & 0x200:
                    raise StartupError('ROUTE', 'wrong_gateway')
    except FileNotFoundError:
        pass  # IPv6 is not available in this kernel.
    except (OSError, ValueError):
        raise StartupError('ROUTE', 'wrong_gateway') from None


def volume(data=Path('/data'), mountinfo=Path('/proc/self/mountinfo')):
    try:
        mounts = [line.split()[4] for line in mountinfo.read_text().splitlines()]
    except OSError:
        mounts = []
    if str(data) not in mounts:
        raise StartupError('VOLUME', 'mount_missing')
    if data.is_symlink() or data.resolve() != data.absolute():
        raise StartupError('VOLUME', 'path_escape')
    marker = data / '.hw-news-volume-id'
    if marker.is_symlink():
        raise StartupError('VOLUME', 'path_escape')
    try:
        if not marker.is_file():
            raise StartupError('VOLUME', 'identity_missing')
        if marker.read_text().strip() != VOLUME_ID:
            raise StartupError('VOLUME', 'identity_mismatch')
    except UnicodeError:
        raise StartupError('VOLUME', 'identity_mismatch') from None
    except OSError:
        raise StartupError('VOLUME', 'identity_missing') from None
    db = data / 'news.db'
    if db.is_symlink():
        raise StartupError('VOLUME', 'path_escape')
    if db.exists() and not db.is_file():
        raise StartupError('VOLUME', 'wrong_file_type')


def database(path):
    """Return 'empty' or 'populated'; never create/recover a DB or sidecar."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StartupError('VOLUME', 'wrong_file_type')
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise StartupError('DB', 'sidecar_present')
    try:
        if not path.exists():
            return 'empty'
        if not os.access(path, os.R_OK) or not path.stat().st_mode & 0o444:
            raise StartupError('DB', 'unreadable')
        if path.stat().st_size == 0:
            return 'empty'
        conn = sqlite3.connect('file:' + quote(str(path.absolute()), safe='/') +
                               '?mode=ro&immutable=1', uri=True)
        try:
            if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise StartupError('DB', 'corrupt')
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                return 'empty'
            if tables != TABLES:
                raise StartupError('DB', 'foreign_or_partial_schema')
            if not {'link', 'title', 'pub_date'}.issubset(
                {row[1] for row in conn.execute('PRAGMA table_info(processed_news)')}
            ):
                raise StartupError('DB', 'foreign_or_partial_schema')
            if conn.execute('SELECT COUNT(*) FROM processed_news').fetchone()[0]:
                return 'populated'
            if any(conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
                   for table in sorted(TABLES - {'processed_news'})):
                raise StartupError('DB', 'unsafe_empty_ledger')
            return 'empty'
        finally:
            conn.close()
    except sqlite3.Error:
        raise StartupError('DB', 'corrupt') from None
    except OSError:
        raise StartupError('DB', 'unreadable') from None


def _permit(data):
    folder = data / '.bootstrap'
    permit = folder / 'permit'
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()) or permit.is_symlink():
        raise StartupError('BOOTSTRAP', 'invalid_permit')
    try:
        if not permit.exists():
            raise StartupError('BOOTSTRAP', 'missing_permit')
        if not permit.is_file() or permit.stat().st_size > 100:
            raise ValueError()
        value = permit.read_text().strip()
        if str(uuid.UUID(value)) != value:
            raise ValueError()
        consumed = folder / 'consumed'
        if consumed.is_symlink() or (consumed.exists() and not consumed.is_dir()):
            raise ValueError()
        if os.path.lexists(consumed / value):
            raise StartupError('BOOTSTRAP', 'replayed_permit')
        return value
    except (OSError, ValueError):
        raise StartupError('BOOTSTRAP', 'invalid_permit') from None


def bootstrap(env, data, state):
    flag = env.get('ALLOW_EMPTY_PROD_DB', '')
    present = os.path.lexists(data / '.bootstrap' / 'permit')
    if state == 'populated':
        if flag or present:
            raise StartupError('BOOTSTRAP', 'unexpected_controls')
        return False
    if flag != '1':
        raise StartupError('BOOTSTRAP', 'missing_flag')
    _permit(data)
    return True


def preflight(env=None, *, data=Path('/data'), mountinfo=Path('/proc/self/mountinfo'),
              routefile=Path('/proc/net/route'), ipv6file=Path('/proc/net/ipv6_route')):
    env = os.environ if env is None else env
    if mode(env) != 'production':
        return False
    route(routefile, ipv6file)
    volume(data, mountinfo)
    config(env)
    state = database(data / 'news.db')
    if not os.access(data, os.W_OK | os.X_OK) or (
        (data / 'news.db').exists() and not os.access(data / 'news.db', os.W_OK)
    ):
        raise StartupError('DB', 'unreadable')
    return bootstrap(env, data, state)


def claim(env=None, *, data=Path('/data')):
    """Single winner; durable tombstone precedes the first application DB write."""
    env = os.environ if env is None else env
    folder = data / '.bootstrap'
    if folder.is_symlink():
        raise StartupError('BOOTSTRAP', 'invalid_permit')
    try:
        fd = os.open(folder / 'claim.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StartupError('BOOTSTRAP', 'concurrent_claim') from None
            if not bootstrap(env, data, database(data / 'news.db')):
                raise StartupError('BOOTSTRAP', 'unexpected_controls')
            value = _permit(data)
            consumed = folder / 'consumed'
            consumed.mkdir(mode=0o700, exist_ok=True)
            try:
                proof = os.open(consumed / value, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                raise StartupError('BOOTSTRAP', 'replayed_permit') from None
            try:
                os.fsync(proof)
            finally:
                os.close(proof)
            for directory in (consumed, folder):
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            (folder / 'permit').unlink()
            directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError:
        raise StartupError('BOOTSTRAP', 'invalid_permit') from None
