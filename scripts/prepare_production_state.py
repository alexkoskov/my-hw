#!/usr/bin/env python3
"""One-time marker preparation; inspect secrets on the host, never return them."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

APP = Path('/opt/hw-news')
CONTAINER = 'hw-news-bot'
VOLUME_ID = 'my-hw-moscow-production-v1'


class ReadinessError(Exception):
    pass


def command(args, *, input_text=None):
    env = os.environ.copy()
    if args[0] == 'docker':
        for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
            env.pop(key, None)
        args = ['docker', '--host=unix:///var/run/docker.sock', *args[1:]]
    result = subprocess.run(args, env=env, input=input_text, capture_output=True,
                            text=True, timeout=45)
    if result.returncode:
        raise ReadinessError('command_failed')
    return result.stdout


def ensure_marker(data):
    """Publish complete content atomically without overwriting another identity."""
    if data.is_symlink() or not data.is_dir():
        raise ReadinessError('invalid_data_directory')
    marker = data / '.hw-news-volume-id'
    created = False
    if not os.path.lexists(marker):
        fd, name = tempfile.mkstemp(prefix='.hw-news-volume-id.', dir=data)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o444)
                stream.write((VOLUME_ID + '\n').encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, marker, follow_symlinks=False)
                created = True
            except FileExistsError:
                pass
        finally:
            os.unlink(name)
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReadinessError('invalid_marker_type')
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ReadinessError('marker_changed')
        if info.st_size > 128 or os.read(fd, 128).strip() != VOLUME_ID.encode():
            raise ReadinessError('marker_identity_mismatch')
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return 'created' if created else 'verified_existing'


PROBE = r'''
import json,os,sqlite3,sys,time
try:
    payload=json.load(sys.stdin)
    assert os.getuid()==os.getgid()==10001
    namespace={'__name__':'readiness_contract','__file__':'/app/startup_safety.py'}
    exec(compile(payload['contract'],'/app/startup_safety.py','exec'),namespace)
    env={key:str(value) for key,value in payload['environment'].items() if value is not None}
    # Read the running DB as its worker, not root. A memory backup gives a
    # consistent view without restoring, migrating, or replacing any disk DB.
    source=sqlite3.connect('file:/data/news.db?mode=ro',uri=True,timeout=5)
    snapshot=sqlite3.connect(':memory:')
    deadline=time.monotonic()+15
    def progress(*args):
        if time.monotonic()>deadline:raise TimeoutError()
    try:
        source.execute('PRAGMA query_only=ON')
        source.backup(snapshot,pages=256,progress=progress,sleep=0.05)
    finally:
        source.close()
    try:
        assert snapshot.execute('PRAGMA quick_check').fetchall()==[('ok',)]
        tables={r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables==namespace['TABLES']
        count=snapshot.execute('SELECT COUNT(*) FROM processed_news').fetchone()[0]
        assert count>0
    finally:
        snapshot.close()
    result={'database':'verified_populated','configuration':{'status':'ready'}}
    try:
        assert namespace['mode'](env)=='production'
        namespace['config'](env)
        namespace['bootstrap'](env,namespace['Path']('/data'),'populated')
    except namespace['StartupError'] as error:
        result['configuration']={'status':'attention_required','gate':error.gate,
                                 'reason':error.reason,'field':error.field}
    required=['TELEGRAM_BOT_TOKEN','TELEGRAM_CHANNEL_ID','TELEGRAM_ADMIN_ID',
              'TELEGRAPH_ACCESS_TOKEN','LLM_PROVIDER']
    prefixes={'claude':'ANTHROPIC','openai':'OPENAI','gemini':'GEMINI','openrouter':'OPENROUTER'}
    prefix=prefixes.get(env.get('LLM_PROVIDER'))
    if prefix:
        required.append(prefix+'_MODEL')
        key=prefix+'_API_KEY'
        if prefix=='OPENROUTER' and not env.get(key):key='OPEN_ROUTER_API_KEY'
        required.append(key)
    result['missing_fields']=[key for key in required if not env.get(key,'').strip()]
    print(json.dumps(result))
except Exception:
    print(json.dumps({'database':'not_verified','reason':'worker_probe_failed'}))
    sys.exit(1)
'''


def inspect_live(expected):
    if not re.fullmatch('[0-9a-f]{40}', expected):
        raise ReadinessError('invalid_expected_revision')
    data = APP / 'data'
    database = data / 'news.db'
    for path in (APP, data, database, APP / '.env'):
        if path.is_symlink() or path.resolve() != path:
            raise ReadinessError('unexpected_production_path')
    if not data.is_dir() or not database.is_file() or not database.stat().st_size:
        raise ReadinessError('existing_database_required')
    if not (APP / '.env').is_file():
        raise ReadinessError('private_environment_required')
    if (APP / '.env').stat().st_mode & 0o077:
        raise ReadinessError('private_environment_permissions')
    if command(['git','-C',str(APP),'branch','--show-current']).strip() != 'main':
        raise ReadinessError('unexpected_production_branch')
    if command(['git','-C',str(APP),'rev-parse','HEAD']).strip() != expected:
        raise ReadinessError('production_revision_changed')
    origin = command(['git','-C',str(APP),'remote','get-url','origin']).strip()
    if origin not in ('https://github.com/alexkoskov/my-hw.git','git@github.com:alexkoskov/my-hw.git'):
        raise ReadinessError('unexpected_repository')
    live = json.loads(command(['docker','inspect',CONTAINER]))[0]
    labels = live['Config'].get('Labels') or {}
    if (live['State']['Status'] != 'running' or
        labels.get('com.docker.compose.project.working_dir') != str(APP) or
        labels.get('org.opencontainers.image.revision') != expected):
        raise ReadinessError('unexpected_production_container')
    if not any(m.get('Type')=='bind' and m.get('Source')==str(data) and
               m.get('Destination')=='/data' and m.get('RW') is True for m in live['Mounts']):
        raise ReadinessError('unexpected_data_bind')
    return live


def fingerprint(path):
    s=path.stat()
    return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode


def run_host(payload):
    expected=payload['expected_revision']
    live=inspect_live(expected)
    data=APP/'data'; database=data/'news.db'
    original_db=(database.stat().st_dev,database.stat().st_ino)
    original_env=fingerprint(APP/'.env')
    with tempfile.TemporaryDirectory(prefix='hw-production-readiness-') as directory:
        compose=Path(directory)/'candidate.yml'
        compose.write_text(payload['compose'])
        document=json.loads(command(['docker','compose','--project-directory',str(APP),
                                     '-f',str(compose),'config','--format','json']))
    service=document['services']['news-bot']
    if not any(v.get('type')=='bind' and v.get('source')==str(data) and
               v.get('target')=='/data' and not v.get('read_only',False) and
               v.get('bind',{}).get('create_host_path',False) is False
               for v in service.get('volumes',[])):
        raise ReadinessError('unexpected_candidate_bind')
    details=json.loads(command(['docker','exec','-i','--user','10001:10001',CONTAINER,
                                'python3','-c',PROBE],input_text=json.dumps({
                                    'contract':payload['contract'],
                                    'environment':service['environment']})))
    if details.get('database') != 'verified_populated':
        raise ReadinessError('database_not_verified')
    current=inspect_live(expected)
    if current['Id']!=live['Id'] or current['RestartCount']!=live['RestartCount']:
        raise ReadinessError('production_container_changed')
    marker=ensure_marker(data)
    command(['docker','exec','--user','10001:10001',CONTAINER,'python3','-c',
             "from pathlib import Path; assert Path('/data/.hw-news-volume-id').read_text() == " + repr(VOLUME_ID+'\n')])
    if (database.stat().st_dev,database.stat().st_ino)!=original_db:
        raise ReadinessError('database_identity_changed')
    if fingerprint(APP/'.env')!=original_env:
        raise ReadinessError('private_environment_changed')
    final=inspect_live(expected)
    if final['Id']!=live['Id'] or final['RestartCount']!=live['RestartCount']:
        raise ReadinessError('production_container_changed')
    return dict(status='ready' if details['configuration']['status']=='ready' else 'attention_required',
                marker=marker,marker_readable_by_worker=True,production_revision=expected,
                database=details['database'],configuration=details['configuration'],
                missing_fields=details['missing_fields'],database_replaced=False,
                env_file_changed=False,bot_restarted=False)


def remote_main(payload):
    try:
        result=run_host(payload)
    except ReadinessError as error:
        result={'status':'failed','reason':str(error)}
    except Exception:
        result={'status':'failed','reason':'unexpected_readiness_failure'}
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['status']=='ready' else 1


def emit(expected):
    root=Path(__file__).resolve().parents[1]
    payload=dict(expected_revision=expected,contract=(root/'startup_safety.py').read_text(),
                 compose=(root/'.github/ops/production-compose.yml').read_text())
    source=Path(__file__).read_text().split('\nif __name__ ==',1)[0]
    return source+'\nraise SystemExit(remote_main('+repr(payload)+'))\n'


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emit',required=True,metavar='EXPECTED_PRODUCTION_SHA')
    print(emit(parser.parse_args().emit))
