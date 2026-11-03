import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='my-hw-backup-gate-') as directory:
    root = Path(directory)
    source = root / 'news.db'
    backup_dir = root / 'backups'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE processed_news(link TEXT PRIMARY KEY)')
        db.execute('INSERT INTO processed_news VALUES (?)', ('synthetic-ledger-row',))
    env = os.environ.copy()
    env.update(DB_FILE=str(source), BACKUP_DIR=str(backup_dir))
    subprocess.run(['bash', '/app/scripts/backup_db.sh'], env=env, check=True,
                   capture_output=True, text=True, timeout=10)
    backups = list(backup_dir.glob('news_*.db'))
    assert len(backups) == 1
    restored_path = root / "restored.db"
    shutil.copy2(backups[0], restored_path)
    with sqlite3.connect(restored_path) as restored:
        integrity = restored.execute('PRAGMA integrity_check').fetchone()[0]
        rows = restored.execute('SELECT link FROM processed_news').fetchall()
        assert integrity == 'ok' and rows == [('synthetic-ledger-row',)]
    print(json.dumps({'script': '/app/scripts/backup_db.sh', 'integrity': integrity,
                      'ledger_rows': len(rows), 'uid': os.getuid()}))
