"""Run the Actions transport with fake SSH and real backup encryption."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64


@pytest.fixture
def transport(tmp_path):
    if not shutil.which("age") or not shutil.which("ssh-keygen"):
        pytest.skip("deployment transport check requires age and OpenSSH")
    runner = tmp_path / "runner"
    keys = runner / "prod-ssh"
    keys.mkdir(parents=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / "key")], check=True)
    (keys / "recipient").write_text((keys / "key.pub").read_text())
    (keys / "known_hosts").write_text("host identity fixture\n")
    (runner / "image.tar.gz").write_bytes(b"image")
    (runner / "source.bundle").write_bytes(b"bundle")
    (runner / "unrelated").write_text("must survive cleanup")
    commands = tmp_path / "commands"
    commands.mkdir()
    programs = {
        "ssh": """
            import json,os,pathlib,sys
            with open(os.environ['CALL_LOG'],'a') as out:out.write(json.dumps(sys.argv[1:])+'\\n')
            if 'prepare' in sys.argv:
                print(json.dumps({'status':'prepared','sha':os.environ['DEPLOY_SHA']}))
            elif 'activate' in sys.argv:
                failed=os.getenv('ACTIVATE_FAIL')=='1'
                print(json.dumps({'status':'failed' if failed else 'deployed',
                                  'sha':os.environ['DEPLOY_SHA'],'image':os.environ['TEST_IMAGE']}))
                raise SystemExit(1 if failed else 0)
            else:print(json.dumps({'status':'cleaned'}))
        """,
        "scp": """
            import pathlib,sys
            target=pathlib.Path(sys.argv[-1])
            if target.name in ('before.db','cutover.db'):
                target.write_bytes(b'private synthetic database bytes')
        """,
        "docker": """
            import os
            print(os.environ['TEST_IMAGE'])
        """,
        "gh": """
            import os
            print(os.environ.get('CURRENT_MAIN',os.environ['DEPLOY_SHA']))
        """,
    }
    for name, body in programs.items():
        path = commands / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
        path.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=str(commands) + os.pathsep + env["PATH"], DEPLOY_SHA=SHA,
               GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", PROD_HOST="45.90.216.165",
               GITHUB_REPOSITORY="alexkoskov/my-hw", RUNNER_TEMP=str(runner),
               CALL_LOG=str(tmp_path / "ssh-calls"), TEST_IMAGE=IMAGE)
    return runner, keys, env


def run(mode, env):
    return subprocess.run(["bash", "scripts/deploy_actions.sh", mode], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=20)


def test_encrypted_backup_and_successful_transport_use_pinned_host(transport):
    runner, keys, env = transport
    assert run("prepare", env).returncode == 0
    encrypted = runner / "before.db.age"
    assert encrypted.read_bytes().startswith(b"age-encryption.org/")
    assert not (runner / "before.db").exists()
    plain = subprocess.check_output(["age", "-d", "-i", str(keys / "key"), str(encrypted)], env=env)
    assert plain == b"private synthetic database bytes"
    activated = run("activate", env)
    assert activated.returncode == 0, activated.stderr
    result = json.loads((runner / "production-result.json").read_text())
    assert result["status"] == "deployed" and result["sha"] == SHA
    calls = [json.loads(line) for line in Path(env["CALL_LOG"]).read_text().splitlines()]
    assert len(calls) == 2
    assert all("StrictHostKeyChecking=yes" in call and "IdentitiesOnly=yes" in call for call in calls)
    assert all("root@45.90.216.165" in call for call in calls)
    assert (runner / "cutover.db.age").exists() and not (runner / "cutover.db").exists()
    assert run("cleanup", env).returncode == 0
    assert not (keys / "key").exists() and not (keys / "known_hosts").exists()
    assert (runner / "unrelated").read_text() == "must survive cleanup"
    assert encrypted.exists()


def test_stale_main_never_contacts_server(transport):
    _, _, env = transport
    env["CURRENT_MAIN"] = "c" * 40
    assert run("prepare", env).returncode != 0
    assert not Path(env["CALL_LOG"]).exists()


def test_remote_failure_retains_encrypted_recovery_without_reporting_success(transport):
    runner, _, env = transport
    assert run("prepare", env).returncode == 0
    env["ACTIVATE_FAIL"] = "1"
    assert run("activate", env).returncode != 0
    assert json.loads((runner / "production-result.json").read_text())["status"] == "failed"
    assert (runner / "cutover.db.age").exists()


def test_retry_of_healthy_current_revision_does_not_activate_again(transport):
    runner, _, env = transport
    prepared = runner / "production-prepared.json"
    prepared.write_text(json.dumps({"status": "already_deployed", "sha": SHA, "image": IMAGE}))
    assert run("activate", env).returncode == 0
    assert not Path(env["CALL_LOG"]).exists()
    assert json.loads((runner / "production-result.json").read_text())["already_current"]
