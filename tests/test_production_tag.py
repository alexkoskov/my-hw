"""Execute the real release-tag step against an isolated GitHub CLI double."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OTHER = 'b' * 40
BASE = 'prod-2026-11-05'
SUFFIX = BASE + '-' + SHA[:7]


@pytest.mark.parametrize('initial,expected,success', [
    ({}, {BASE: SHA}, True),
    ({BASE: SHA}, {BASE: SHA}, True),
    ({BASE: OTHER}, {BASE: OTHER, SUFFIX: SHA}, True),
    ({BASE: OTHER, SUFFIX: SHA}, {BASE: OTHER, SUFFIX: SHA}, True),
    ({BASE: OTHER, SUFFIX: OTHER}, {BASE: OTHER, SUFFIX: OTHER}, False),
])
def test_production_tag_creation_reuse_and_collision(tmp_path, initial, expected, success):
    state = tmp_path / 'tags.json'
    state.write_text(json.dumps(initial))
    bin_path = tmp_path / 'commands'
    bin_path.mkdir()
    gh = bin_path / 'gh'
    gh.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
        import json,os,pathlib,sys
        args=sys.argv[1:]
        state=pathlib.Path(os.environ['TEST_TAG_STATE'])
        tags=json.loads(state.read_text())
        assert args[0]=='api'
        if 'POST' not in args:
            route=args[1]
            prefix='repos/alexkoskov/my-hw/commits/'
            assert route.startswith(prefix)
            name=route[len(prefix):]
            if name not in tags:
                # gh api emits the error response body on stdout even when
                # the request fails. It is not a successful SHA lookup.
                print(json.dumps({'message':'No commit found for SHA','status':'422'}))
                raise SystemExit(1)
            print(tags[name])
        else:
            assert 'repos/alexkoskov/my-hw/git/refs' in args
            fields=dict(x.split('=',1) for x in args if '=' in x)
            assert fields['ref'].startswith('refs/tags/')
            name=fields['ref'][len('refs/tags/'):]
            if name in tags:
                raise SystemExit(1)
            tags[name]=fields['sha']
            state.write_text(json.dumps(tags))
            print('{}')
    '''))
    gh.chmod(0o755)
    date = bin_path / 'date'
    date.write_text(f'#!{sys.executable}\nimport os\nassert os.environ["TZ"]=="Europe/Moscow"\nprint("2026-11-05")\n')
    date.chmod(0o755)
    workflow = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())
    script = workflow['jobs']['record']['steps'][0]['run']
    env = os.environ.copy()
    env.update(PATH=str(bin_path) + os.pathsep + env['PATH'],
               TEST_TAG_STATE=str(state), GITHUB_REPOSITORY='alexkoskov/my-hw',
               DEPLOY_SHA=SHA, GH_TOKEN='isolated-test-token')
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True,
                            text=True, timeout=10)
    assert (result.returncode == 0) is success, result.stderr + result.stdout
    assert json.loads(state.read_text()) == expected
