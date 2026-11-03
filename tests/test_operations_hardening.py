"""Exercise operator scripts and notification state without live services."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _stub_transports(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
        import json, os, pathlib, sys
        root = pathlib.Path(os.environ['TEST_STATE'])
        calls = root / 'curl-calls'
        n = int(calls.read_text()) if calls.exists() else 0
        calls.write_text(str(n + 1))
        mode = os.environ.get('CURL_MODE', 'ok')
        if mode == 'transport':
            raise SystemExit(22)
        path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
        path.write_text('not-json' if mode == 'malformed' else
                        json.dumps({'ok': mode != 'api-failure'}))
    """))
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
        import json, os, pathlib, sys
        path = pathlib.Path(os.environ['TEST_STATE']) / 'issues.json'
        issues = json.loads(path.read_text()) if path.exists() else {}
        args = sys.argv[1:]
        action = args[1]
        if action == 'list':
            title = args[args.index('--search') + 1].removesuffix(' in:title')
            print(issues.get(title, ''))
        elif action == 'create':
            title = args[args.index('--title') + 1]
            issues[title] = max(issues.values(), default=0) + 1
        elif action == 'close':
            issues = {k:v for k,v in issues.items() if str(v) != args[2]}
        else:
            raise SystemExit('unexpected gh operation')
        path.write_text(json.dumps(issues))
    """))
    curl.chmod(0o755)
    gh.chmod(0o755)
    return {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TEST_STATE": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
    }


def _alert_script() -> str:
    workflow = (ROOT / ".github/workflows/uptime.yml").read_text()
    step = workflow.split("      - name: Alert or recover\n", 1)[1]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def _watch(tmp_path: Path, *, mode="ok", pub="stale", host=False,
           credentials=True) -> subprocess.CompletedProcess:
    env = _stub_transports(tmp_path)
    env.update(CURL_MODE=mode, PUB_STATE=pub, HOST_DOWN=str(host).lower())
    if credentials:
        env.update(BOT_TOKEN="fake-bot-credential", CHAT_ID="42")
    return subprocess.run(["bash", "-c", _alert_script()], env=env,
                          text=True, capture_output=True, timeout=10)


def _issues(tmp_path: Path) -> dict:
    path = tmp_path / "issues.json"
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.mark.parametrize("mode", ["transport", "api-failure", "malformed", "missing"])
def test_uptime_retries_undelivered_alarm_and_recovery(tmp_path, mode):
    failed = _watch(tmp_path, mode=mode, credentials=mode != "missing")
    assert failed.returncode != 0
    assert _issues(tmp_path) == {}
    assert not list(tmp_path.glob("uptime-send.*"))

    assert _watch(tmp_path).returncode == 0
    assert list(_issues(tmp_path)) == ["🟡 Прод не публикует"]
    calls = (tmp_path / "curl-calls").read_text()
    assert _watch(tmp_path).returncode == 0
    assert (tmp_path / "curl-calls").read_text() == calls

    failed_recovery = _watch(tmp_path, mode=mode, pub="fresh",
                             credentials=mode != "missing")
    assert failed_recovery.returncode != 0
    assert list(_issues(tmp_path)) == ["🟡 Прод не публикует"]
    assert _watch(tmp_path, pub="fresh").returncode == 0
    assert _issues(tmp_path) == {}


def test_uptime_preserves_inconclusive_and_host_suppression(tmp_path):
    assert _watch(tmp_path).returncode == 0
    assert _watch(tmp_path, pub="inconclusive").returncode == 0
    assert list(_issues(tmp_path)) == ["🟡 Прод не публикует"]
    assert _watch(tmp_path, host=True, pub="fresh").returncode == 0
    assert set(_issues(tmp_path)) == {"🟡 Прод не публикует", "🔴 Прод не отвечает"}
    assert _watch(tmp_path, pub="inconclusive").returncode == 0
    assert list(_issues(tmp_path)) == ["🟡 Прод не публикует"]


@pytest.mark.parametrize("mode,expected", [
    ("ok", 0), ("transport", 1), ("api-failure", 1), ("malformed", 1),
])
def test_watchdog_reports_real_delivery_result(tmp_path, mode, expected):
    script = tmp_path / "watchdog.sh"
    shutil.copyfile(ROOT / "watchdog.sh", script)
    env = _stub_transports(tmp_path)
    env.update(TELEGRAM_BOT_TOKEN="fake-token", TELEGRAM_ADMIN_ID="42",
               HEARTBEAT_FILE=str(tmp_path / "missing-heartbeat"), CURL_MODE=mode)
    result = subprocess.run(["bash", str(script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected
    assert (tmp_path / "curl-calls").read_text() == "1"


def test_missing_heartbeat_is_incomplete_health_check(monkeypatch):
    spec = importlib.util.spec_from_file_location("prod_check", ROOT / "scripts/prod_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rep = module.Report()
    monkeypatch.setattr(module.os.path, "exists", lambda _path: False)
    module.check_heartbeat(rep, None)
    assert rep.worst == 1
    assert any("не проверен" in line for line in rep.lines)


def test_legacy_deploy_cannot_start_scp_or_ssh(tmp_path):
    env = _stub_transports(tmp_path)
    for name in ("scp", "ssh"):
        script = tmp_path / "bin" / name
        script.write_text("#!/bin/sh\ntouch \"$TEST_STATE/unexpected-transport\"\n")
        script.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "deploy.sh")], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "Docker" in result.stderr
    assert not (tmp_path / "unexpected-transport").exists()


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI required for shell backup")
def test_shell_backup_restores_ledger(tmp_path):
    source = tmp_path / "news.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE processed_news(link TEXT PRIMARY KEY)")
        db.execute("INSERT INTO processed_news VALUES ('synthetic-ledger-row')")
    backup_dir = tmp_path / "backups"
    env = {"PATH": os.environ["PATH"], "DB_FILE": str(source),
           "BACKUP_DIR": str(backup_dir)}
    subprocess.run(["bash", str(ROOT / "scripts/backup_db.sh")], env=env,
                   capture_output=True, text=True, check=True, timeout=10)
    backups = list(backup_dir.glob("news_*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT link FROM processed_news").fetchone() == ("synthetic-ledger-row",)
