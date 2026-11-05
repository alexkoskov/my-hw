#!/usr/bin/env python3
"""One-time marker preparation; inspect secrets on the host, never return them."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

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


def ensure_marker(data, *, inspect_only=False):
    """Publish complete content atomically without overwriting another identity."""
    if data.is_symlink() or not data.is_dir():
        raise ReadinessError('invalid_data_directory')
    marker = data / '.hw-news-volume-id'
    created = False
    if inspect_only and not os.path.lexists(marker):
        raise ReadinessError('marker_missing')
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
        if not inspect_only:
            os.fchmod(fd, 0o444)
            os.fsync(fd)
    finally:
        os.close(fd)
    if inspect_only:
        return 'verified_existing'
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return 'created' if created else 'verified_existing'


PROBE = r'''
import ast,json,os,re,sqlite3,sys,time
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
    # Evaluate only the deployed dispatcher's two environment-only helpers.
    # No engine import, health check, API request, or application entrypoint.
    dispatcher=ast.parse(namespace['Path']('/app/llm_transcreation.py').read_text())
    functions=[node for node in dispatcher.body if isinstance(node,ast.FunctionDef)
               and node.name in ('_has_key','_auto_select_by_key_presence')]
    scope={'os':os}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<deployed-dispatcher>','exec'),scope)
    provider=os.getenv('LLM_PROVIDER','').strip().lower()
    prefixes={'claude':'ANTHROPIC','openai':'OPENAI','gemini':'GEMINI','openrouter':'OPENROUTER'}
    if provider not in prefixes:provider=scope['_auto_select_by_key_presence']()
    assert provider in prefixes
    engine=ast.parse(namespace['Path']('/app/'+provider+'_transcreation.py').read_text())
    default=next(ast.literal_eval(node.value) for node in engine.body
                 if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and
                 t.id=='_DEFAULT_MODEL' for t in node.targets))
    model_field=prefixes[provider]+'_MODEL'
    model=os.getenv(model_field,default)
    safe_model=(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:+-]{0,199}',model))
                and not model.startswith(('sk-','sk_','ghp_','github_pat_','AIza','xoxb-')))
    credentials=[value for key,value in dict(os.environ,**env).items() if value and
                 any(word in key.upper() for word in ('TOKEN','API_KEY','SECRET','PASSWORD'))]
    safe_model=safe_model and not any(value in model for value in credentials)
    result['current_effective']={'provider':provider,'model':model if safe_model else '<withheld>'}
    result['current_choice_missing_fields']=[key for key in ('LLM_PROVIDER',model_field) if not env.get(key,'').strip()]
    proposed=dict(env)
    if not proposed.get('LLM_PROVIDER','').strip():proposed['LLM_PROVIDER']=provider
    if not proposed.get(model_field,'').strip() and safe_model:proposed[model_field]=model
    result['configuration_with_current_choice_explicit']={'status':'ready'}
    try:
        namespace['config'](proposed)
        namespace['bootstrap'](proposed,namespace['Path']('/data'),'populated')
    except namespace['StartupError'] as error:
        result['configuration_with_current_choice_explicit']={
            'status':'attention_required','gate':error.gate,'reason':error.reason,'field':error.field}
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


def candidate_service(payload, env_file=None):
    body=payload['compose']
    if env_file is not None:
        needle='      - .env\n'
        if body.count(needle)!=1:
            raise ReadinessError('unexpected_environment_manifest')
        body=body.replace(needle,'      - '+json.dumps(str(env_file))+'\n')
    with tempfile.TemporaryDirectory(prefix='hw-production-readiness-') as directory:
        compose=Path(directory)/'candidate.yml'
        compose.write_text(body)
        document=json.loads(command(['docker','compose','--project-directory',str(APP),
                                     '-f',str(compose),'config','--format','json']))
    return document['services']['news-bot']


def sync_directory(directory):
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def install_provider(app, inspect_environment, verify_installed):
    """Append one approved non-secret field, with private inode rollback."""
    target=app/'.env'
    source_fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW)
    temporary=None; backup=None; installed=None
    try:
        fcntl.flock(source_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        original=os.fstat(source_fd)
        if not stat.S_ISREG(original.st_mode) or original.st_nlink!=1:
            raise ReadinessError('unexpected_environment_type')
        before=fingerprint(target)
        baseline=inspect_environment(None)
        existing=baseline.get('LLM_PROVIDER') or ''
        if existing not in ('','openrouter'):
            raise ReadinessError('provider_configuration_changed')
        if existing=='openrouter':
            os.fchmod(source_fd,0o600)
            return False,verify_installed()
        with os.fdopen(os.dup(source_fd),'rb') as stream:raw=stream.read()
        updated=raw+(b'' if raw.endswith(b'\n') else b'\n')+b'LLM_PROVIDER=openrouter\n'
        fd,name=tempfile.mkstemp(prefix='.env.readiness-',dir=app)
        temporary=Path(name)
        with os.fdopen(fd,'wb') as stream:
            os.fchown(stream.fileno(),original.st_uid,original.st_gid)
            os.fchmod(stream.fileno(),0o600)
            stream.write(updated);stream.flush();os.fsync(stream.fileno())
        candidate=inspect_environment(temporary)
        strip=lambda values:{k:v for k,v in values.items() if k!='LLM_PROVIDER'}
        if candidate.get('LLM_PROVIDER')!='openrouter' or strip(candidate)!=strip(baseline):
            raise ReadinessError('unapproved_environment_change')
        if fingerprint(target)!=before:
            raise ReadinessError('private_environment_changed')
        # Harden the original before linking the rollback inode: neither copy
        # ever exposes keys through broader permissions during this operation.
        os.fchmod(source_fd,0o600)
        backup=app/('.env.before-readiness-'+uuid.uuid4().hex)
        os.link(target,backup,follow_symlinks=False)
        installed=(temporary.stat().st_dev,temporary.stat().st_ino)
        os.replace(temporary,target);temporary=None
        try:
            sync_directory(app)
            actual=inspect_environment(None)
            if actual!=candidate:
                raise ReadinessError('installed_environment_mismatch')
            result=verify_installed()
            if result.get('status')!='ready':
                raise ReadinessError('installed_configuration_not_ready')
        except Exception:
            current=target.stat()
            if (current.st_dev,current.st_ino)!=installed:
                raise ReadinessError('environment_changed_before_rollback') from None
            os.replace(backup,target);backup=None
            sync_directory(app)
            raise
        backup.unlink();backup=None
        sync_directory(app)
        return True,result
    finally:
        os.close(source_fd)
        if temporary is not None:temporary.unlink()
        if backup is not None and backup.exists() and target.exists():
            if (backup.stat().st_dev,backup.stat().st_ino)==(target.stat().st_dev,target.stat().st_ino):
                backup.unlink()
        # A backup survives only if a concurrent writer prevented safe rollback.


def apply_approved_config(payload):
    before=run_host(dict(payload,inspect_only=True))
    if before.get('current_effective')!={'provider':'openrouter','model':'google/gemini-2.5-flash'}:
        raise ReadinessError('current_model_changed')
    if before.get('configuration_with_current_choice_explicit',{}).get('status')!='ready':
        raise ReadinessError('additional_configuration_required')
    def inspect_environment(env_file):
        env=candidate_service(payload,env_file)['environment']
        if env.get('OPENROUTER_MODEL')!='google/gemini-2.5-flash':
            raise ReadinessError('model_configuration_changed')
        return env
    changed,result=install_provider(APP,inspect_environment,
                                  lambda:run_host(dict(payload,inspect_only=True)))
    result['env_file_changed']=changed
    result['credential_values_changed']=False
    result['model_changed']=False
    return result


def run_host(payload):
    expected=payload['expected_revision']
    live=inspect_live(expected)
    data=APP/'data'; database=data/'news.db'
    original_db=(database.stat().st_dev,database.stat().st_ino)
    original_env=fingerprint(APP/'.env')
    service=candidate_service(payload)
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
    marker=ensure_marker(data,inspect_only=payload.get('inspect_only',False))
    command(['docker','exec','--user','10001:10001',CONTAINER,'python3','-c',
             "from pathlib import Path; assert Path('/data/.hw-news-volume-id').read_text().strip() == " + repr(VOLUME_ID)])
    if (database.stat().st_dev,database.stat().st_ino)!=original_db:
        raise ReadinessError('database_identity_changed')
    if fingerprint(APP/'.env')!=original_env:
        raise ReadinessError('private_environment_changed')
    final=inspect_live(expected)
    if final['Id']!=live['Id'] or final['RestartCount']!=live['RestartCount']:
        raise ReadinessError('production_container_changed')
    private_environment = not (APP/'.env').stat().st_mode & 0o077
    ready = details['configuration']['status']=='ready' and private_environment
    return dict(status='ready' if ready else 'attention_required',
                private_environment_permissions='private' if private_environment else 'attention_required',
                marker=marker,marker_readable_by_worker=True,production_revision=expected,
                database=details['database'],configuration=details['configuration'],
                missing_fields=details['missing_fields'],
                current_effective=details.get('current_effective'),
                current_choice_missing_fields=details.get('current_choice_missing_fields'),
                configuration_with_current_choice_explicit=details.get('configuration_with_current_choice_explicit'),
                database_replaced=False,
                env_file_changed=False,bot_restarted=False)


def remote_main(payload):
    try:
        result=apply_approved_config(payload) if payload.get('apply_approved_config') else run_host(payload)
    except ReadinessError as error:
        result={'status':'failed','reason':str(error)}
    except Exception:
        result={'status':'failed','reason':'unexpected_readiness_failure'}
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['status']=='ready' else 1


def emit(expected, *, inspect_only=False, apply_approved_config=False):
    root=Path(__file__).resolve().parents[1]
    payload=dict(expected_revision=expected,inspect_only=inspect_only,apply_approved_config=apply_approved_config,contract=(root/'startup_safety.py').read_text(),
                 compose=(root/'.github/ops/production-compose.yml').read_text())
    source=Path(__file__).read_text().split('\nif __name__ ==',1)[0]
    return source+'\nraise SystemExit(remote_main('+repr(payload)+'))\n'


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emit',required=True,metavar='EXPECTED_PRODUCTION_SHA')
    parser.add_argument('--inspect-only',action='store_true')
    parser.add_argument('--apply-approved-config',action='store_true')
    args=parser.parse_args()
    print(emit(args.emit,inspect_only=args.inspect_only,apply_approved_config=args.apply_approved_config))
