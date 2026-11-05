"""Exercise release authorization and rollback without live SSH/production."""

import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("production_deploy", ROOT / "scripts/deploy_production.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
IMAGE_INFO = {'Id': IMAGE, 'Os': 'linux', 'Architecture': 'amd64',
              'RootFS': {'Type': 'layers', 'Layers': ['sha256:' + 'f' * 64]},
              'Config': {'Labels': {'org.opencontainers.image.revision': SHA},
                         'Entrypoint': ['/app/scripts/container_entrypoint.sh'],
                         'Cmd': ['python3', 'news_bot.py'], 'Env': ['PYTHONUNBUFFERED=1']}}
IDENTITY = deploy.image_identity(IMAGE_INFO)


def event(**changes):
    parent = {"id": 7, "event": "push", "head_branch": "main", "head_sha": SHA,
              "head_repository": {"full_name": deploy.REPOSITORY}, "conclusion": "success"}
    parent.update(changes)
    return {"workflow_run": parent}


@pytest.mark.parametrize("changes", [
    {"event": "pull_request"}, {"head_branch": "dev"}, {"conclusion": "failure"},
    {"conclusion": "skipped"}, {"head_repository": {"full_name": "someone/fork"}},
    {"head_sha": "bad; command"},
])
def test_untrusted_or_unchecked_event_cannot_deploy(changes):
    with pytest.raises(deploy.DeployError):
        deploy.select_revision(event(**changes), "workflow_run", deploy.REPOSITORY,
                               "refs/heads/main", SHA)


def test_manual_dispatch_only_accepts_main():
    assert deploy.select_revision({}, "workflow_dispatch", deploy.REPOSITORY,
                                  "refs/heads/main", SHA) == SHA
    with pytest.raises(deploy.DeployError):
        deploy.select_revision({}, "workflow_dispatch", deploy.REPOSITORY,
                               "refs/heads/dev", SHA)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps(event()))
    for name, value in {"GITHUB_EVENT_PATH": str(payload), "GITHUB_EVENT_NAME": "workflow_run",
                        "GITHUB_REPOSITORY": deploy.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                        "GITHUB_SHA": SHA}.items():
        monkeypatch.setenv(name, value)
    run = {"id": 7, "head_sha": SHA, "head_branch": "main", "event": "push",
           "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml"}
    jobs = [{"name": "test", "conclusion": "success",
             "steps": [{"name": "Run tests", "conclusion": "success"}]}]
    def api(route):
        if "git/ref" in route:
            return {"object": {"sha": SHA}}
        return {"jobs": jobs} if "/jobs?" in route else run
    monkeypatch.setattr(deploy, "api", api)
    return run, jobs


def test_exact_green_main_is_selected(gate_env):
    assert deploy.gate() == {"deploy": True, "sha": SHA, "ci_run": 7}


@pytest.mark.parametrize("change", ["wrong_sha", "wrong_workflow", "skipped_job", "skipped_step"])
def test_green_summary_without_actual_test_success_is_rejected(gate_env, change):
    run, jobs = gate_env
    if change == "wrong_sha": run["head_sha"] = "c" * 40
    if change == "wrong_workflow": run["path"] = ".github/workflows/other.yml"
    if change == "skipped_job": jobs[0]["conclusion"] = "skipped"
    if change == "skipped_step": jobs[0]["steps"][0]["conclusion"] = "skipped"
    with pytest.raises(deploy.DeployError):
        deploy.gate()


def test_stale_ci_does_not_deploy(gate_env, monkeypatch):
    monkeypatch.setattr(deploy, "api", lambda _route: {"object": {"sha": "c" * 40}})
    assert deploy.gate()["deploy"] is False


def test_backup_is_consistent_and_does_not_overwrite_recovery(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE processed_news(link TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO processed_news VALUES ('existing-history')")
    deploy.backup(source, target)
    assert deploy.ledger(target) == 1 and target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(deploy.DeployError): deploy.backup(source, target)
    assert deploy.ledger(source) == 1


def test_command_failure_does_not_echo_credentials(monkeypatch):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="opaque-secret", stderr="opaque-secret"))
    with pytest.raises(deploy.DeployError) as caught:
        deploy.command(["docker", "run"])
    assert "opaque-secret" not in str(caught.value)
    assert "docker run operation failed" in str(caught.value)


@pytest.fixture
def legacy_checkout(tmp_path, monkeypatch):
    app = tmp_path / 'checkout'
    app.mkdir()
    subprocess.run(['git', 'init', '-q', str(app)], check=True)
    (app / 'worker.py').write_text('original source\n')
    subprocess.run(['git', '-C', str(app), 'add', 'worker.py'], check=True)
    subprocess.run(['git', '-C', str(app), '-c', 'user.name=Deployment test',
                    '-c', 'user.email=deployment@example.invalid', 'commit', '-qm', 'baseline'], check=True)
    for name in ('data', 'backups'):
        (app / name).mkdir()
        (app / name / 'history.db').write_bytes(b'existing database bytes')
    exclude = app / '.git/info/exclude'
    exclude.write_bytes(b'# Preserve operator rules\n/operator-local')
    monkeypatch.setattr(deploy, 'APP', app)
    return app, exclude


def test_legacy_runtime_dirs_are_excluded_without_changing_state(legacy_checkout):
    app, exclude = legacy_checkout
    snapshots = {p: (p.read_bytes(), p.stat().st_mode) for p in app.glob('*/history.db')}
    deploy.check_worktree()
    expected = b'# Preserve operator rules\n/operator-local\n/data/\n/backups/\n'
    assert exclude.read_bytes() == expected
    assert subprocess.check_output(['git', '-C', str(app), 'status', '--porcelain']) == b''
    assert all((p.read_bytes(), p.stat().st_mode) == saved for p, saved in snapshots.items())
    deploy.check_worktree()
    assert exclude.read_bytes() == expected


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'nested_state', 'tracked_state', 'state_symlink'])
def test_state_exclusions_do_not_hide_other_changes(legacy_checkout, change):
    app, exclude = legacy_checkout
    if change == 'tracked':
        (app / 'worker.py').write_text('operator changes\n')
    elif change == 'untracked':
        (app / 'operator.txt').write_text('operator work\n')
    elif change == 'nested_state':
        (app / 'nested/data').mkdir(parents=True)
        (app / 'nested/data/item').touch()
    elif change == 'tracked_state':
        subprocess.run(['git', '-C', str(app), 'add', 'data/history.db'], check=True)
        subprocess.run(['git', '-C', str(app), '-c', 'user.name=Deployment test',
                        '-c', 'user.email=deployment@example.invalid', 'commit', '-qm', 'tracked state'], check=True)
    else:
        (app / 'backups/history.db').unlink()
        (app / 'backups').rmdir()
        (app / 'backups').symlink_to(app / 'data', target_is_directory=True)
    prior = exclude.read_bytes()
    with pytest.raises(deploy.DeployError):
        deploy.check_worktree()
    assert exclude.read_bytes() == prior


def test_local_exclude_symlink_is_not_followed(legacy_checkout, tmp_path):
    app, exclude = legacy_checkout
    outside = tmp_path / 'outside-metadata'
    outside.write_bytes(b'preserve me')
    exclude.unlink()
    exclude.symlink_to(outside)
    with pytest.raises(deploy.DeployError, match='symlinks'):
        deploy.check_worktree()
    assert outside.read_bytes() == b'preserve me'


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    app = tmp_path / "app"
    data = app / "data"
    data.mkdir(parents=True)
    db = data / "news.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE processed_news(link TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO processed_news VALUES ('existing-history')")
    env = app / ".env"
    env.write_text("PRIVATE_SETTING=untouched\n")
    stage = tmp_path / "release"
    stage.mkdir()
    (stage / "rollback-compose.yml").write_text("old compose")
    snapshot = {"sha": SHA, "old_head": "c" * 40, "old_image": "old-image",
                "old_container": "old-container", "project": "hw-news", "phase": "prepared",
                "worker_uid": 0, "worker_gid": 0,
                "states": {str(db): [123, 456, 0o640]}, "ledger_rows": 1}
    deploy.checkpoint(stage, snapshot)
    (stage / "image.tar.gz").write_bytes(b"image")
    (stage / "source.bundle").write_bytes(b"bundle")
    image_hash = deploy.hashlib.sha256(b"image").hexdigest()
    bundle_hash = deploy.hashlib.sha256(b"bundle").hexdigest()
    monkeypatch.setattr(deploy, "APP", app)
    monkeypatch.setattr(deploy, "inspect_live", lambda: copy.deepcopy(snapshot))
    calls, owners = [], []
    def command(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([IMAGE_INFO])
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]: return SHA
        if "config" in args and "--format" in args:
            return json.dumps({"services": {"news-bot": {
                "container_name": deploy.CONTAINER, "image": "hw-news-bot:latest",
                "cap_add": ["NET_ADMIN"], "security_opt": ["no-new-privileges:true"],
                "environment": {"INSTANCE_LABEL": "prod", "DB_FILE": "/data/news.db",
                                "NEWS_BOT_LOCK_FILE": "/data/.news_bot.lock",
                                "VPN_GATEWAY": "172.28.0.2", "HEARTBEAT_FILE": "/data/last_tick.ts"},
                "volumes": [{"type": "bind", "source": str(data), "target": "/data"}],
            }}})
        return ""
    monkeypatch.setattr(deploy, "command", command)
    monkeypatch.setattr(deploy.os, "chown", lambda path, uid, gid: owners.append((path, uid, gid)))
    monkeypatch.setattr(deploy, "verify_worker", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "verify_rollback", lambda *a, **k: None)
    return app, stage, image_hash, bundle_hash, calls, owners


def test_success_preserves_ledger_and_env_and_uses_no_build(transaction):
    app, stage, ih, bh, calls, owners = transaction
    result = deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert result["status"] == "deployed"
    assert deploy.ledger(app / "data/news.db") == 1
    assert (app / ".env").read_text() == "PRIVATE_SETTING=untouched\n"
    assert (stage / "cutover.db").exists()
    assert any("--no-build" in c and c[-1] == "news-bot" for c in calls)
    assert not any("reset" in c or "systemctl" in c for c in calls)
    assert all(uid == gid == 10001 for _, uid, gid in owners)


def test_identity_handles_store_ids_and_empty_api_defaults():
    imported = copy.deepcopy(IMAGE_INFO)
    imported['Id'] = 'sha256:' + 'd' * 64
    imported['Config'].update(User='', Volumes=None, ExposedPorts={}, Tty=False)
    imported['Variant'] = ''
    assert deploy.image_identity(imported) == IDENTITY


def test_imported_image_uses_host_native_id_after_content_verification(transaction, monkeypatch):
    _, stage, ih, bh, calls, _ = transaction
    native = 'sha256:' + 'd' * 64
    original = deploy.command
    def command(args, **kwargs):
        result = original(args, **kwargs)
        if args[:3] == ['docker', 'image', 'inspect']:
            image = copy.deepcopy(IMAGE_INFO)
            image['Id'] = native
            return json.dumps([image])
        return result
    monkeypatch.setattr(deploy, 'command', command)
    assert deploy.activate(SHA, stage, IDENTITY, ih, bh)['image'] == native
    assert ['docker', 'tag', native, 'hw-news-bot:latest'] in calls
    assert any(c[:2] == ['docker', 'run'] and native in c for c in calls)


@pytest.mark.parametrize('tamper', ['layer', 'command', 'entrypoint', 'environment',
                                   'user', 'working_dir', 'revision', 'platform', 'missing_metadata'])
def test_imported_image_content_tampering_refuses_before_worker_stop(transaction, monkeypatch, tamper):
    _, stage, ih, bh, calls, owners = transaction
    original = deploy.command
    def command(args, **kwargs):
        result = original(args, **kwargs)
        if args[:3] == ['docker', 'image', 'inspect']:
            image = copy.deepcopy(IMAGE_INFO)
            if tamper == 'layer': image['RootFS']['Layers'] = ['sha256:' + 'e' * 64]
            if tamper == 'command': image['Config']['Cmd'] = ['unexpected']
            if tamper == 'entrypoint': image['Config']['Entrypoint'] = ['/unexpected']
            if tamper == 'environment': image['Config']['Env'] = ['DIFFERENT=1']
            if tamper == 'user': image['Config']['User'] = 'other'
            if tamper == 'working_dir': image['Config']['WorkingDir'] = '/other'
            if tamper == 'revision': image['Config']['Labels']['org.opencontainers.image.revision'] = 'e' * 40
            if tamper == 'platform': image['Architecture'] = 'arm64'
            if tamper == 'missing_metadata': del image['RootFS']
            return json.dumps([image])
        return result
    monkeypatch.setattr(deploy, 'command', command)
    with pytest.raises(deploy.DeployError):
        deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert not any(c[:2] == ['docker', 'stop'] for c in calls)
    assert not owners


def test_identity_cli_runs_without_host_mutation(monkeypatch, capsys):
    monkeypatch.setattr(deploy.sys, 'argv', ['deploy_production.py', 'image-identity', 'hw-news-bot:' + SHA])
    monkeypatch.setattr(deploy.os, 'geteuid', lambda: 10001)
    def inspect(args, **_kwargs):
        assert args == ['docker', 'image', 'inspect', 'hw-news-bot:' + SHA]
        return json.dumps([IMAGE_INFO])
    monkeypatch.setattr(deploy, 'command', inspect)
    deploy.main()
    assert capsys.readouterr().out.strip() == IDENTITY


def test_failed_candidate_preflight_never_stops_old_worker(transaction, monkeypatch):
    _, stage, ih, bh, calls, _ = transaction
    original = deploy.command
    def command(args, **kwargs):
        if args[:2] == ["docker", "run"]: raise deploy.DeployError("candidate rejected")
        return original(args, **kwargs)
    monkeypatch.setattr(deploy, "command", command)
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert not any(c[:2] == ["docker", "stop"] for c in calls)


def test_failed_startup_rolls_back_image_without_restoring_old_database(transaction, monkeypatch):
    app, stage, ih, bh, calls, owners = transaction
    def failed_start(*_args, **_kwargs):
        with sqlite3.connect(app / "data/news.db") as conn:
            conn.execute("INSERT INTO processed_news VALUES ('new-confirmed-post')")
        raise deploy.DeployError("startup verification failed")
    monkeypatch.setattr(deploy, "verify_worker", failed_start)
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert json.loads((stage / "checkpoint.json").read_text())["phase"] == "rolled_back"
    assert deploy.ledger(app / "data/news.db") == 2
    assert any(str(stage / "rollback-compose.yml") in c and "--no-build" in c for c in calls)
    assert any(uid == 123 and gid == 456 for _, uid, gid in owners)


def test_changed_production_or_corrupt_upload_never_stops_worker(transaction, monkeypatch):
    _, stage, ih, bh, calls, _ = transaction
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, "0" * 64, bh)
    assert not any(c[:2] == ["docker", "stop"] for c in calls)
    snapshot = json.loads((stage / "checkpoint.json").read_text())
    snapshot["old_image"] = "someone-else-deployed"
    monkeypatch.setattr(deploy, "inspect_live", lambda: snapshot)
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)


def test_rollback_restores_worker_access_to_new_state_files(transaction, monkeypatch):
    app, stage, ih, bh, _, owners = transaction
    state = json.loads((stage / "checkpoint.json").read_text())
    state.update(worker_uid=10001, worker_gid=10001)
    deploy.checkpoint(stage, state)
    created = app / "data/.news_bot.lock"
    def failed_start(*_args, **_kwargs):
        created.touch()
        raise deploy.DeployError("candidate failed")
    monkeypatch.setattr(deploy, "verify_worker", failed_start)
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert (created, 10001, 10001) in owners
    assert created.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["container", "mount", "database", "port", "read_only", "caps", "privileges"])
def test_invalid_candidate_compose_is_rejected_before_cutover(transaction, monkeypatch, change):
    _, stage, ih, bh, calls, _ = transaction
    original = deploy.command
    def command(args, **kwargs):
        result = original(args, **kwargs)
        if "config" in args and "--format" in args:
            document = json.loads(result)
            service = document["services"]["news-bot"]
            if change == "container": service["container_name"] = "another-bot"
            if change == "mount": service["volumes"][0]["source"] = "/unrelated/data"
            if change == "database": service["environment"]["DB_FILE"] = "new-empty.db"
            if change == "port": service["ports"] = ["8080:8080"]
            if change == "read_only": service["volumes"][0]["read_only"] = True
            if change == "caps": service["cap_add"] = ["ALL"]
            if change == "privileges": service["security_opt"] = []
            return json.dumps(document)
        return result
    monkeypatch.setattr(deploy, "command", command)
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert not any(c[:2] == ["docker", "stop"] for c in calls)


def test_failed_rollback_is_reported_honestly(transaction, monkeypatch):
    _, stage, ih, bh, _, _ = transaction
    monkeypatch.setattr(deploy, "verify_worker", lambda *_a, **_k: (_ for _ in ()).throw(deploy.DeployError("bad startup")))
    monkeypatch.setattr(deploy, "verify_rollback", lambda *_a, **_k: (_ for _ in ()).throw(deploy.DeployError("bad recovery")))
    with pytest.raises(deploy.DeployError): deploy.activate(SHA, stage, IDENTITY, ih, bh)
    assert json.loads((stage / "checkpoint.json").read_text())["phase"] == "rollback_failed"


def test_missing_ledger_is_not_created_by_preflight(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(deploy.DeployError): deploy.ledger(path)
    assert not path.exists()


def test_absent_ssh_secrets_fail_before_any_server_command(tmp_path):
    doc = yaml.load((ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in doc["jobs"]["deploy"]["steps"]
                if s.get("name") == "Configure production SSH identity")
    result = subprocess.run(["bash", "-c", step["run"]],
        env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path),
             "PROD_SSH_PRIVATE_KEY": "", "PROD_SSH_KNOWN_HOSTS": ""},
        capture_output=True, text=True)
    assert result.returncode != 0 and "Configure PROD_SSH_PRIVATE_KEY" in result.stdout
    assert not (tmp_path / "prod-ssh").exists()


def test_host_docker_commands_cannot_use_an_inherited_remote_context(monkeypatch):
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"):
        monkeypatch.setenv(name, "unrelated-remote")
    args, env = deploy.invocation(["docker", "inspect", deploy.CONTAINER])
    assert args[:2] == ["docker", "--host=unix:///var/run/docker.sock"]
    assert not any(name in env for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY"))


def test_live_backup_program_reads_committed_wal_and_uses_worker_identity(tmp_path, monkeypatch):
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE processed_news(link TEXT PRIMARY KEY)")
    writer.execute("INSERT INTO processed_news VALUES ('committed-in-wal')")
    writer.commit()
    original = subprocess.run
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        assert "--user" in args and "10001:10001" in args
        program = args[-1].replace("file:/data/news.db?mode=ro", "file:" + str(source) + "?mode=ro")
        return original([sys.executable, "-c", program], **kwargs)
    import sys
    monkeypatch.setattr(deploy.subprocess, "run", run)
    target = tmp_path / "consistent.db"
    try:
        assert deploy.backup_live(target, 10001, 10001) == 1
        assert target.stat().st_mode & 0o777 == 0o600
        assert seen[0][:2] == ["docker", "--host=unix:///var/run/docker.sock"]
    finally:
        writer.close()


@pytest.mark.parametrize("preserved", [True, False])
def test_worker_verification_reads_live_database_inside_container(tmp_path, monkeypatch, preserved):
    snapshot = tmp_path / "before.db"
    with sqlite3.connect(snapshot) as conn:
        conn.execute("CREATE TABLE processed_news(link TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO processed_news VALUES ('existing-history')")
    live = {"Id": "new-container", "Image": IMAGE, "RestartCount": 0,
            "State": {"Status": "running", "StartedAt": "2026-11-03T00:00:00Z"}}
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]: return json.dumps([live])
        if "-i" in args:
            assert "10001:10001" in args
            assert json.loads(kwargs["input_data"]) == ["existing-history"]
            return json.dumps({"preserved": preserved})
        return json.dumps({"uid": "10001", "gid": "10001", "caps": ["0" * 16] * 5,
                           "nnp": "1", "route": "default via 172.28.0.2"})
    monkeypatch.setattr(deploy, "command", command)
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="", stderr="News bot started.\nStarting daily cron tick..."))
    # Calling a root host-side live SQLite diagnostic would violate the WAL
    # ownership contract even though it appears read-only.
    monkeypatch.setattr(deploy, "ledger", lambda *_: (_ for _ in ()).throw(AssertionError("host read")))
    if preserved:
        deploy.verify_worker(IMAGE, snapshot)
    else:
        with pytest.raises(deploy.DeployError): deploy.verify_worker(IMAGE, snapshot)
    assert any("-i" in call for call in calls)


def test_workflow_targets_main_ci_and_separate_production_credentials():
    doc = yaml.load((ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
    assert doc["on"]["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}
    assert doc["concurrency"]["cancel-in-progress"] == "false"
    assert doc["jobs"]["select"]["outputs"]["sha"] == "${{ steps.gate.outputs.sha }}"
    assert doc["jobs"]["deploy"]["environment"] == "production"
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "PROD_SSH_PRIVATE_KEY" in text and "PROD_SSH_KNOWN_HOSTS" in text
    assert "secrets.SSH_PRIVATE_KEY" not in text and "secrets.SSH_HOST" not in text
    assert "systemctl" not in text and "148.135.207.54" not in text
    legacy = yaml.load((ROOT / ".github/workflows/deploy_test.yml").read_text(), Loader=yaml.BaseLoader)
    assert legacy["jobs"]["deploy"]["if"] == "false"
    assert "workflow_run" not in legacy["on"]


def test_scripts_parse_without_running_network_or_production():
    subprocess.run(["bash", "-n", str(ROOT / "scripts/deploy_actions.sh")], check=True)
    compile((ROOT / "scripts/deploy_production.py").read_text(), "deploy_production.py", "exec")
