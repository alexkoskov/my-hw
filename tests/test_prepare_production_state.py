"""Offline acceptance for the one-time, non-deploying production operation."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import scripts.prepare_production_state as prep


@pytest.fixture
def data(tmp_path):
    directory=tmp_path/'data';directory.mkdir()
    (directory/'news.db').write_bytes(b'existing state, do not replace')
    return directory


def test_marker_is_atomic_readable_idempotent_and_preserves_database(data):
    before=(data/'news.db').read_bytes(),(data/'news.db').stat()
    assert prep.ensure_marker(data)=='created'
    marker=data/'.hw-news-volume-id'
    assert marker.read_text()==prep.VOLUME_ID+'\n'
    assert marker.stat().st_mode & 0o777 == 0o444
    inode=marker.stat().st_ino
    assert prep.ensure_marker(data)=='verified_existing'
    assert marker.stat().st_ino==inode
    assert (data/'news.db').read_bytes()==before[0]
    assert (data/'news.db').stat()==before[1]
    assert sorted(p.name for p in data.iterdir())==['.hw-news-volume-id','news.db']


def test_correct_existing_marker_without_newline_is_preserved(data):
    marker=data/'.hw-news-volume-id';marker.write_text(prep.VOLUME_ID)
    marker.chmod(0o600)
    assert prep.ensure_marker(data)=='verified_existing'
    assert marker.read_text()==prep.VOLUME_ID
    assert marker.stat().st_mode & 0o777 == 0o444


@pytest.mark.parametrize('kind',['wrong','symlink','directory','hardlink'])
def test_conflicting_marker_is_never_overwritten(data,kind):
    marker=data/'.hw-news-volume-id'
    if kind=='wrong':marker.write_text('another volume')
    elif kind=='symlink':marker.symlink_to(data/'news.db')
    elif kind=='directory':marker.mkdir()
    else:marker.hardlink_to(data/'news.db')
    before=(data/'news.db').read_bytes()
    with pytest.raises(prep.ReadinessError):prep.ensure_marker(data)
    assert (data/'news.db').read_bytes()==before
    if kind=='wrong':assert marker.read_text()=='another volume'
    if kind=='symlink':assert marker.is_symlink()
    assert len(list(data.iterdir()))==2


def test_missing_directory_is_never_created(tmp_path):
    target=tmp_path/'missing'
    with pytest.raises(prep.ReadinessError):prep.ensure_marker(target)
    assert not target.exists()


@pytest.fixture
def host(data,monkeypatch):
    app=data.parent
    (app/'.env').write_text('private file must be preserved\n')
    (app/'.env').chmod(0o600)
    monkeypatch.setattr(prep,'APP',app)
    live={'Id':'original','RestartCount':0}
    monkeypatch.setattr(prep,'inspect_live',MagicMock(return_value=live))
    calls=[]
    result={'database':'verified_populated','configuration':{'status':'ready'},'missing_fields':[]}
    def command(args,**kwargs):
        calls.append((args,kwargs))
        if args[:2]==['docker','compose']:
            return json.dumps({'services':{'news-bot':{
                'environment':{'TELEGRAM_BOT_TOKEN':'private-test-value'},
                'volumes':[{'type':'bind','source':str(data),'target':'/data',
                            'bind':{'create_host_path':False}}]}}})
        if '-i' in args:return json.dumps(result)
        return ''
    monkeypatch.setattr(prep,'command',command)
    return dict(expected_revision='a'*40,compose='candidate',contract='contract'),calls,result


def test_ready_operation_does_not_restart_or_edit_private_environment(host,data):
    payload,calls,_=host
    env=(data.parent/'.env').read_bytes()
    output=prep.run_host(payload)
    assert output['status']=='ready' and output['marker']=='created'
    assert output['bot_restarted'] is False and output['env_file_changed'] is False
    assert (data.parent/'.env').read_bytes()==env
    assert all('stop' not in args and 'restart' not in args and 'up' not in args for args,_ in calls)
    assert 'private-test-value' not in json.dumps(output)
    probe=next((args,options) for args,options in calls if '-i' in args)
    assert 'private-test-value' not in str(probe[0])
    assert 'private-test-value' in probe[1]['input_text']


def test_missing_config_prepares_only_marker_and_reports_field_names(host,data):
    payload,_,result=host
    result['configuration']={'status':'attention_required','gate':'CONFIG',
                             'reason':'missing_field','field':'OPENROUTER_MODEL'}
    result['missing_fields']=['OPENROUTER_MODEL']
    report=prep.run_host(payload)
    assert report['status']=='attention_required'
    assert report['marker']=='created'
    assert report['missing_fields']==['OPENROUTER_MODEL']
    assert (data/'.hw-news-volume-id').is_file()


def test_unverified_database_never_gets_a_marker(host,data):
    payload,_,result=host
    result['database']='not_verified'
    with pytest.raises(prep.ReadinessError,match='database_not_verified'):
        prep.run_host(payload)
    assert not (data/'.hw-news-volume-id').exists()


def test_emitted_program_is_valid_python_and_has_no_local_secret_values(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','local-private-value')
    source=prep.emit('a'*40)
    compile(source,'remote-readiness','exec')
    assert 'local-private-value' not in source
    assert 'remote_main(' in source


def test_existing_environment_permissions_are_reported_without_modification(host,data):
    payload,_,_=host
    env=data.parent/'.env';env.chmod(0o644)
    report=prep.run_host(payload)
    assert report['marker']=='created'
    assert report['private_environment_permissions']=='attention_required'
    assert report['status']=='attention_required'
    assert env.stat().st_mode & 0o777 == 0o644


def test_inspection_never_creates_a_missing_marker(data):
    before=sorted(p.name for p in data.iterdir())
    with pytest.raises(prep.ReadinessError,match='marker_missing'):
        prep.ensure_marker(data,inspect_only=True)
    assert sorted(p.name for p in data.iterdir())==before


def test_inspection_preserves_marker_permissions_and_metadata(data):
    marker=data/'.hw-news-volume-id';marker.write_text(prep.VOLUME_ID+'\n')
    marker.chmod(0o644)
    before=marker.stat()
    assert prep.ensure_marker(data,inspect_only=True)=='verified_existing'
    after=marker.stat()
    assert (before.st_ino,before.st_mode,before.st_mtime_ns,before.st_ctime_ns)==(
        after.st_ino,after.st_mode,after.st_mtime_ns,after.st_ctime_ns)


def test_approved_provider_install_preserves_all_original_bytes_and_values(tmp_path):
    from dotenv import dotenv_values
    env=tmp_path/'.env'
    raw=b'SECRET_KEY="first\nsecond"\nOPENROUTER_MODEL=google/gemini-2.5-flash\n'
    env.write_bytes(raw);env.chmod(0o644)
    def inspect(path):return dict(dotenv_values(path or env))
    def verify():
        assert inspect(None)['LLM_PROVIDER']=='openrouter'
        assert env.stat().st_mode & 0o777==0o600
        return {'status':'ready'}
    changed,result=prep.install_provider(tmp_path,inspect,verify)
    assert changed and result['status']=='ready'
    assert env.read_bytes()==raw+b'LLM_PROVIDER=openrouter\n'
    assert sorted(p.name for p in tmp_path.iterdir())==['.env']


def test_provider_install_rolls_back_original_inode_on_failed_verification(tmp_path):
    from dotenv import dotenv_values
    env=tmp_path/'.env';env.write_bytes(b'KEY=private-placeholder\n');env.chmod(0o644)
    before=env.stat().st_ino,env.read_bytes()
    def inspect(path):return dict(dotenv_values(path or env))
    def fail():raise prep.ReadinessError('synthetic_postcheck_failure')
    with pytest.raises(prep.ReadinessError,match='synthetic_postcheck_failure'):
        prep.install_provider(tmp_path,inspect,fail)
    assert (env.stat().st_ino,env.read_bytes())==before
    assert env.stat().st_mode & 0o777==0o600
    assert sorted(p.name for p in tmp_path.iterdir())==['.env']


def test_candidate_secret_change_is_rejected_before_replacing_environment(tmp_path):
    env=tmp_path/'.env';env.write_text('KEY=original\n');env.chmod(0o644)
    before=env.stat().st_ino,env.read_bytes()
    def inspect(path):
        return {'KEY':'tampered','LLM_PROVIDER':'openrouter'} if path else {'KEY':'original'}
    with pytest.raises(prep.ReadinessError,match='unapproved_environment_change'):
        prep.install_provider(tmp_path,inspect,lambda: {'status':'ready'})
    assert (env.stat().st_ino,env.read_bytes())==before
    assert env.stat().st_mode & 0o777==0o644
    assert sorted(p.name for p in tmp_path.iterdir())==['.env']


def test_existing_explicit_provider_only_hardens_permissions(tmp_path):
    from dotenv import dotenv_values
    env=tmp_path/'.env';env.write_text('LLM_PROVIDER=openrouter\nKEY=original\n');env.chmod(0o644)
    before=env.stat().st_ino,env.read_bytes()
    changed,_=prep.install_provider(tmp_path,lambda path:dict(dotenv_values(path or env)),
                                    lambda: {'status':'ready'})
    assert changed is False
    assert (env.stat().st_ino,env.read_bytes())==before
    assert env.stat().st_mode & 0o777==0o600


def test_changed_current_model_refuses_approved_edit(host,monkeypatch):
    payload,_,_=host
    monkeypatch.setattr(prep,'run_host',lambda _: {'current_effective':{'provider':'openrouter','model':'other'}})
    install=MagicMock();monkeypatch.setattr(prep,'install_provider',install)
    with pytest.raises(prep.ReadinessError,match='current_model_changed'):
        prep.apply_approved_config(payload)
    install.assert_not_called()
