#!/usr/bin/env python3
"""GitHub release gate and checkpointed deployment for the single Docker host.

The remote modes run only on the production host, as root. They never edit .env,
reset Git history, restore an older database, or manage the shared VPN service.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote


APP = Path("/opt/hw-news")
RELEASES = Path("/opt/hw-news-deployments")
CONTAINER = "hw-news-bot"
REPOSITORY = "alexkoskov/my-hw"
STATE_NAMES = ("news.db", "news.db-wal", "news.db-shm", "news.db-journal",
               "last_tick.ts", ".news_bot.lock")


class DeployError(RuntimeError):
    pass


def invocation(args):
    env = os.environ.copy()
    if args[0] == "docker":
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            env.pop(name, None)
        args = ["docker", "--host=unix:///var/run/docker.sock", *args[1:]]
    return args, env


def command(args, *, cwd=None, input_data=None):
    actual, env = invocation(args)
    result = subprocess.run(actual, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=240, input=input_data)
    if result.returncode:
        # Docker/SSH errors can contain environment-file values. Never relay
        # arbitrary command output into Actions logs or checkpoint metadata.
        raise DeployError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout


def api(route):
    return json.loads(command(["gh", "api", route]))


def select_revision(event, name, repository, ref, sha):
    if repository != REPOSITORY:
        raise DeployError("unexpected repository")
    if name == "workflow_run":
        parent = event.get("workflow_run", {})
        if not (parent.get("event") == "push"
                and parent.get("head_branch") == "main"
                and parent.get("head_repository", {}).get("full_name") == repository
                and parent.get("conclusion") == "success"):
            raise DeployError("only successful main push CI may deploy")
        sha = parent.get("head_sha", "")
    elif name != "workflow_dispatch" or ref != "refs/heads/main":
        raise DeployError("manual deployment must use main")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeployError("invalid revision")
    return sha


def gate():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = select_revision(event, os.environ["GITHUB_EVENT_NAME"], repository,
                          os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"])
    if api(f"repos/{repository}/git/ref/heads/main")["object"]["sha"] != sha:
        return {"deploy": False, "sha": sha, "reason": "newer_main_exists"}
    if os.environ["GITHUB_EVENT_NAME"] == "workflow_run":
        run = api(f"repos/{repository}/actions/runs/{event['workflow_run']['id']}")
    else:
        runs = api(f"repos/{repository}/actions/runs?head_sha={sha}&event=push&per_page=100")
        run = next((r for r in runs["workflow_runs"]
                    if r.get("path") == ".github/workflows/ci.yml"
                    and r.get("conclusion") == "success"), {})
    if not (run.get("head_sha") == sha and run.get("head_branch") == "main"
            and run.get("event") == "push" and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("path") == ".github/workflows/ci.yml"):
        raise DeployError("successful CI for the exact main revision is required")
    jobs = api(f"repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    if not any(j.get("name") == "test" and j.get("conclusion") == "success"
               and any(s.get("name") == "Run tests" and s.get("conclusion") == "success"
                       for s in j.get("steps", [])) for j in jobs):
        raise DeployError("the actual CI test step did not pass")
    return {"deploy": True, "sha": sha, "ci_run": run["id"]}


def ledger(path):
    try:
        connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise DeployError("existing production ledger cannot be opened read-only") from error
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise DeployError("database integrity check failed")
        count = connection.execute("SELECT COUNT(*) FROM processed_news").fetchone()[0]
        if count == 0:
            raise DeployError("production ledger must not be empty")
        return count
    except sqlite3.Error as error:
        raise DeployError("production database lacks a valid processed ledger") from error
    finally:
        connection.close()


def backup(source, target):
    if target.exists() or target.is_symlink():
        raise DeployError("refusing to overwrite a recovery checkpoint")
    with contextlib.closing(sqlite3.connect("file:" + quote(str(source), safe="/") + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    target.chmod(0o600)
    ledger(target)


def backup_live(target, uid, gid):
    """Back up as the existing worker while it is still running.

    A root host-side SQLite reader could create root-owned WAL/SHM files under
    the non-root worker. Stream a private SQLite backup from the container.
    """
    if target.exists() or target.is_symlink():
        raise DeployError("refusing to overwrite a recovery checkpoint")
    identity = f"{uid}:{gid}"
    program = """
import contextlib,os,pathlib,sqlite3,sys,tempfile
fd,name=tempfile.mkstemp(prefix='deployment-backup-');os.close(fd)
try:
 with contextlib.closing(sqlite3.connect('file:/data/news.db?mode=ro',uri=True)) as src:
  with contextlib.closing(sqlite3.connect(name)) as dst:src.backup(dst)
 with open(name,'rb') as data:
  while block:=data.read(1024*1024):sys.stdout.buffer.write(block)
finally:pathlib.Path(name).unlink(missing_ok=True)
"""
    with target.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        args, env = invocation(["docker", "exec", "--user", identity, CONTAINER,
                                "python3", "-c", program])
        result = subprocess.run(args, env=env, stdout=output,
                                stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        raise DeployError("consistent live database backup failed")
    return ledger(target)


def check_worktree():
    """Bootstrap exact state exclusions on pre-automation production checkouts.

    Only the two known untracked state directories may precede this change.
    Keep all tracked changes and unrelated untracked paths as release blockers.
    """
    status = command(["git", "status", "--porcelain", "-z", "--untracked-files=normal"], cwd=APP)
    if any(row not in ("?? data/", "?? backups/") for row in status.split("\0") if row):
        raise DeployError("production checkout has local changes")
    if command(["git", "ls-files", "--", "data", "backups"], cwd=APP).strip():
        raise DeployError("production state directories must not be tracked")
    info = APP / ".git/info"
    exclude = info / "exclude"
    if info.is_symlink() or exclude.is_symlink():
        raise DeployError("Git local exclusions must not be symlinks")
    info.mkdir(exist_ok=True)
    existing = exclude.read_bytes() if exclude.exists() else b""
    missing = [p for p in (b"/data/", b"/backups/") if p not in existing.splitlines()]
    if missing:
        with exclude.open("ab") as output:
            if existing and not existing.endswith(b"\n"):
                output.write(b"\n")
            output.write(b"\n".join(missing) + b"\n")
    if command(["git", "status", "--porcelain"], cwd=APP).strip():
        raise DeployError("production checkout has local changes")


def inspect_live():
    data = APP / "data"
    if APP.is_symlink() or data.is_symlink() or (APP / ".git").is_symlink() or not (APP / ".git").is_dir():
        raise DeployError("unexpected production checkout or state path")
    if command(["git", "branch", "--show-current"], cwd=APP).strip() != "main":
        raise DeployError("production checkout must be on main")
    origin = command(["git", "remote", "get-url", "origin"], cwd=APP).strip()
    if origin not in (f"https://github.com/{REPOSITORY}.git", f"git@github.com:{REPOSITORY}.git"):
        raise DeployError("production checkout has an unexpected origin")
    check_worktree()
    if not (APP / ".env").is_file() or (APP / ".env").is_symlink():
        raise DeployError("existing private runtime environment is required")
    states = {}
    for path in (data, *(data / name for name in STATE_NAMES)):
        if path.is_symlink():
            raise DeployError("state symlinks require operator inspection")
        if path.exists():
            info = path.stat()
            if path == data and not stat.S_ISDIR(info.st_mode):
                raise DeployError("state directory is invalid")
            if path != data and not stat.S_ISREG(info.st_mode):
                raise DeployError("unexpected state file type")
            states[str(path)] = [info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
    if not (data / "news.db").is_file() or (data / "news.db").stat().st_size == 0:
        raise DeployError("existing nonempty production database is required")
    live = json.loads(command(["docker", "inspect", CONTAINER]))[0]
    labels = live["Config"].get("Labels") or {}
    if live["State"]["Status"] != "running":
        raise DeployError("the existing production container is not running")
    if labels.get("com.docker.compose.project.working_dir") != str(APP):
        raise DeployError("container belongs to a different checkout")
    if not any(m.get("Type") == "bind" and m.get("Source") == str(data)
               and m.get("Destination") == "/data" for m in live["Mounts"]):
        raise DeployError("container does not use the expected persistent database")
    project = labels.get("com.docker.compose.project", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise DeployError("invalid Compose project")
    if shutil.disk_usage(APP).free < 1024 ** 3:
        raise DeployError("less than 1 GiB free; deployment needs operator attention")
    runtime = json.loads(command(["docker", "exec", CONTAINER, "python3", "-c",
        "import json,os,pathlib;s=dict(line.split(':',1) for line in pathlib.Path('/proc/1/status').read_text().splitlines() if ':' in line);print(json.dumps({'db':os.getenv('DB_FILE','news.db'),'uid':int(s['Uid'].split()[0]),'gid':int(s['Gid'].split()[0])}))"]))
    if runtime.get("db") != "/data/news.db":
        raise DeployError("running worker does not use the expected persistent database")
    if (runtime.get("uid"), runtime.get("gid")) not in ((0, 0), (10001, 10001)):
        raise DeployError("unexpected running worker identity requires operator inspection")
    return {"old_head": command(["git", "rev-parse", "HEAD"], cwd=APP).strip(),
            "old_image": live["Image"], "old_container": live["Id"],
            "project": project, "states": states,
            "worker_uid": runtime["uid"], "worker_gid": runtime["gid"],
            "revision": labels.get("org.opencontainers.image.revision")}


def checkpoint(stage, data):
    path = stage / "checkpoint.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


@contextlib.contextmanager
def host_lock():
    if RELEASES.is_symlink():
        raise DeployError("deployment directory must not be a symlink")
    RELEASES.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = RELEASES.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeployError("deployment checkpoints require a private root-owned directory")
    with (RELEASES / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("another host deployment is active") from exc
        yield


def prepare(sha, stage):
    live = inspect_live()
    stage.mkdir(mode=0o700)
    shutil.copy2(APP / "docker-compose.yml", stage / "rollback-compose.yml")
    count = backup_live(stage / "before.db", live["worker_uid"], live["worker_gid"])
    state = {**live, "sha": sha, "phase": "prepared", "ledger_rows": count}
    checkpoint(stage, state)
    if live["revision"] == sha:
        verify_worker(live["old_image"], stage / "before.db", require_startup=False)
        return {"status": "already_deployed", "sha": sha,
                "image": live["old_image"], "backup": str(stage / "before.db")}
    return {"status": "prepared", "sha": sha, "backup": str(stage / "before.db")}


def compose(state, path=None):
    return ["docker", "compose", "--project-directory", str(APP), "-p", state["project"],
            "-f", str(path or APP / "docker-compose.yml")]


def validate_compose(state, path):
    config = json.loads(command(compose(state, path) + ["config", "--format", "json"]))
    service = config.get("services", {}).get("news-bot", {})
    environment = service.get("environment", {})
    expected = {"INSTANCE_LABEL": "prod", "DB_FILE": "/data/news.db",
                "NEWS_BOT_LOCK_FILE": "/data/.news_bot.lock",
                "VPN_GATEWAY": "172.28.0.2", "HEARTBEAT_FILE": "/data/last_tick.ts"}
    if (service.get("container_name") != CONTAINER
            or service.get("image") != "hw-news-bot:latest"
            or service.get("ports")
            or set(service.get("cap_add", [])) != {"NET_ADMIN"}
            or service.get("cap_drop")
            or "no-new-privileges:true" not in service.get("security_opt", [])
            or any(environment.get(k) != v for k, v in expected.items())
            or not any(v.get("type") == "bind" and v.get("source") == str(APP / "data")
                       and v.get("target") == "/data" and not v.get("read_only", False)
                       for v in service.get("volumes", []))):
        raise DeployError("candidate Compose changes the protected production target/state contract")


def verify_rollback(image, timeout=30):
    deadline = time.monotonic() + timeout
    stable = None
    observations = 0
    while time.monotonic() < deadline:
        old = json.loads(command(["docker", "inspect", CONTAINER]))[0]
        if old["Image"] == image and old["State"]["Status"] == "running":
            identity = (old["Id"], old["RestartCount"])
            observations = observations + 1 if stable == identity else 1
            stable = identity
            if observations >= 3:
                return
        else:
            observations = 0
        time.sleep(2)
    raise DeployError("previous container did not recover")


def verify_worker(image, baseline, timeout=180, require_startup=True):
    with contextlib.closing(sqlite3.connect(
        "file:" + quote(str(baseline), safe="/") + "?mode=ro", uri=True,
    )) as prior:
        prior_links = [r[0] for r in prior.execute("SELECT link FROM processed_news")]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = json.loads(command(["docker", "inspect", CONTAINER]))[0]
        if live["State"]["Status"] == "running" and live["RestartCount"] == 0:
            probe = json.loads(command(["docker", "exec", CONTAINER, "python3", "-c", """
import json,pathlib,subprocess
s={k:v.strip() for k,v in (line.split(':',1) for line in pathlib.Path('/proc/1/status').read_text().splitlines() if ':' in line)}
print(json.dumps({'uid':s['Uid'].split()[0],'gid':s['Gid'].split()[0],
 'caps':[s[k] for k in ('CapEff','CapPrm','CapInh','CapAmb','CapBnd')],
 'nnp':s['NoNewPrivs'],'route':subprocess.check_output(['ip','route','show','default'],text=True)}))
"""]))
            # Docker sends Python logging to stderr; retrieve it for the marker
            # only, never print raw application logs in the deployment job.
            args, env = invocation(["docker", "logs", "--since", live["State"]["StartedAt"], "--tail", "2000", CONTAINER])
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
            logs = result.stdout + result.stderr
            if (live["Image"] == image and probe["uid"] == probe["gid"] == "10001"
                    and probe["nnp"] == "1" and set(probe["caps"]) == {"0000000000000000"}
                    and "via 172.28.0.2" in probe["route"]
                    and (not require_startup or ("News bot started." in logs
                                                and "Starting daily cron tick..." in logs))):
                # Read the live WAL database as the worker, avoiding root-owned
                # WAL/SHM files being created by a host-side diagnostic reader.
                saved = json.loads(command(["docker", "exec", "-i", "--user", "10001:10001", CONTAINER,
                    "python3", "-c", """
import json,sqlite3,sys
expected=set(json.load(sys.stdin))
with sqlite3.connect('file:/data/news.db?mode=ro',uri=True) as db:
 actual={r[0] for r in db.execute('SELECT link FROM processed_news')}
 ok=db.execute('PRAGMA quick_check').fetchone()==('ok',)
print(json.dumps({'preserved':bool(actual) and expected.issubset(actual) and ok}))
"""], input_data=json.dumps(prior_links)))
                if not saved["preserved"]:
                    raise DeployError("existing ledger rows were lost")
                return
        if live["RestartCount"] > 0:
            raise DeployError("new worker restarted during startup")
        time.sleep(3)
    raise DeployError("worker startup verification timed out")


def activate(sha, stage, image_id, image_hash, bundle_hash):
    state = json.loads((stage / "checkpoint.json").read_text())
    live = inspect_live()
    if state["sha"] != sha or any(live[k] != state[k] for k in ("old_head", "old_image", "old_container")):
        raise DeployError("production changed after the recovery checkpoint")
    for name, expected in (("image.tar.gz", image_hash), ("source.bundle", bundle_hash)):
        h = hashlib.sha256()
        with (stage / name).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                h.update(block)
        if h.hexdigest() != expected:
            raise DeployError("uploaded release checksum mismatch")
    command(["docker", "load", "--input", str(stage / "image.tar.gz")])
    candidate = json.loads(command(["docker", "image", "inspect", f"hw-news-bot:{sha}"]))[0]
    if candidate["Id"] != image_id or candidate["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha:
        raise DeployError("image identity does not match the tested revision")
    command(["git", "fetch", "--no-tags", str(stage / "source.bundle"), f"refs/tags/deploy-build-{sha}"], cwd=APP)
    if command(["git", "rev-parse", "FETCH_HEAD"], cwd=APP).strip() != sha:
        raise DeployError("source bundle revision mismatch")
    command(["git", "merge-base", "--is-ancestor", state["old_head"], sha], cwd=APP)
    candidate_compose = stage / "candidate-compose.yml"
    candidate_compose.write_text(command(["git", "show", f"{sha}:docker-compose.yml"], cwd=APP))
    validate_compose(state, candidate_compose)
    # Offline as the existing worker: root diagnostics must not create root
    # WAL/SHM files alongside an already non-root production writer.
    command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3",
             "--user", f"{state['worker_uid']}:{state['worker_gid']}",
             "--env-file", str(APP / ".env"), "-e", "INSTANCE_LABEL=prod", "-e", "DB_FILE=/data/news.db",
             "--mount", f"type=bind,src={APP / 'data'},dst=/data", image_id,
             "-c", "import news_bot; news_bot._production_preflight()"])
    stopped = False
    try:
        stopped = True
        command(["docker", "stop", "--time", "30", CONTAINER])
        backup(APP / "data/news.db", stage / "cutover.db")
        command(["git", "merge", "--ff-only", sha], cwd=APP)
        for path in (APP / "data", *(APP / "data" / name for name in STATE_NAMES)):
            if path.is_symlink():
                raise DeployError("state path became a symlink")
            if path.exists():
                os.chown(path, 10001, 10001)
                path.chmod(0o700 if path.is_dir() else 0o600)
        command(["docker", "tag", state["old_image"], f"hw-news-bot:rollback-{stage.name}"])
        command(["docker", "tag", image_id, "hw-news-bot:latest"])
        command(compose(state) + ["up", "-d", "--no-build", "--no-deps", "news-bot"])
        verify_worker(image_id, stage / "cutover.db")
    except Exception as original:
        if stopped:
            try:
                command(["docker", "stop", "--time", "30", CONTAINER])
                # A stopped-database reader or the candidate may have created
                # WAL/SHM/lock files absent from the original snapshot. Give
                # those back to the previous worker before restoring old modes.
                for name in STATE_NAMES:
                    path = APP / "data" / name
                    if str(path) not in state["states"] and path.exists() and not path.is_symlink():
                        os.chown(path, state["worker_uid"], state["worker_gid"])
                        path.chmod(0o600)
                for name, (uid, gid, mode) in state["states"].items():
                    path = Path(name)
                    if path.exists() and not path.is_symlink():
                        os.chown(path, uid, gid)
                        path.chmod(mode)
                command(["docker", "tag", state["old_image"], "hw-news-bot:latest"])
                command(compose(state, stage / "rollback-compose.yml") + ["up", "-d", "--no-build", "--no-deps", "news-bot"])
                verify_rollback(state["old_image"])
                state["phase"] = "rolled_back"
            except Exception:
                state["phase"] = "rollback_failed"
            checkpoint(stage, state)
        raise DeployError(f"deployment failed; recovery state: {state['phase']}") from original
    state.update(phase="deployed", image=image_id)
    checkpoint(stage, state)
    return {"status": "deployed", "sha": sha, "image": image_id,
            "backup": str(stage / "cutover.db"), "rollback": str(stage / "checkpoint.json")}


def main():
    if sys.argv[1:] == ["gate"]:
        result = gate()
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"sha={result['sha']}\ndeploy={str(result['deploy']).lower()}\n")
        print(json.dumps(result))
        return
    if len(sys.argv) < 3 or not re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+", sys.argv[2]):
        raise DeployError("invalid release identifier")
    mode, release = sys.argv[1:3]
    if os.geteuid() != 0:
        raise DeployError("remote deployment requires the configured root account")
    sha = release[:40]
    stage = RELEASES / release
    with host_lock():
        if stage.is_symlink():
            raise DeployError("release directory must not be a symlink")
        if mode == "prepare":
            result = prepare(sha, stage)
        elif mode == "activate" and len(sys.argv) == 6:
            image_id, image_hash, bundle_hash = sys.argv[3:]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) or not all(re.fullmatch(r"[a-f0-9]{64}", h) for h in (image_hash, bundle_hash)):
                raise DeployError("invalid release identity")
            result = activate(sha, stage, image_id, image_hash, bundle_hash)
        elif mode == "cleanup":
            for name in ("image.tar.gz", "source.bundle"):
                (stage / name).unlink(missing_ok=True)
            result = {"status": "incoming_archives_removed"}
        else:
            raise DeployError("invalid deployment operation")
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        def interrupted(_signum, _frame):
            # SSH disconnects must enter the same rollback path as a failed
            # startup. Ignore a repeated signal while recovery is running.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise DeployError("deployment session interrupted")
        signal.signal(signal.SIGHUP, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": str(error) if isinstance(error, DeployError) else type(error).__name__}))
        raise SystemExit(1)
