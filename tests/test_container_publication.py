"""Out-of-order maintenance and approvals cannot regress latest's build order."""
import importlib.util
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication', ROOT/'tools/container_publication.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
spec = importlib.util.spec_from_file_location('artifacts', ROOT/'tools/container_artifacts.py')
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
SHA, DIGEST = 'a'*40, 'sha256:'+'b'*64


class Registry:
    def __init__(self):
        self.ref, self.tags, self.writes = None, {}, []
        self.approved = SHA
    def __call__(self, method, path, body=None):
        if method == 'GET':
            if path == 'git/ref/tags/container-approved': return {'object':{'sha':self.approved}}
            if path == 'git/ref/'+P.REF: return self.ref
            if path.startswith('git/tags/'): return self.tags[path.rsplit('/',1)[1]]
            raise AssertionError(path)
        self.writes.append((method,path,body))
        if path == 'git/tags':
            sha = str(len(self.tags)+1)
            self.tags[sha] = body
            return {'sha':sha}
        self.ref = {'object':{'type':'tag','sha':body['sha']}}
        return self.ref


def test_older_and_repeated_builds_cannot_replace_newer_publication():
    api = Registry()
    assert P.reserve(api,SHA,DIGEST,'maintenance',200,1)
    writes = len(api.writes)
    assert not P.reserve(api,SHA,DIGEST,'maintenance',100,9)
    assert not P.reserve(api,SHA,DIGEST,'maintenance',200,1)
    assert len(api.writes) == writes, 'Rejected builds changed publication state'
    assert P.reserve(api,SHA,DIGEST,'maintenance',200,2), 'A newer retry should be permitted'
    assert P.reserve(api,SHA,DIGEST,'approval',300,1)
    assert not P.reserve(api,SHA,DIGEST,'maintenance',250,1), 'Old maintenance superseded an approval'


def test_changed_source_and_invalid_metadata_fail_closed():
    api = Registry()
    api.approved = 'c'*40
    try: P.reserve(api,SHA,DIGEST,'maintenance',100,1)
    except ValueError: pass
    else: raise AssertionError('Changed approval accepted')
    assert not api.writes
    api.approved = SHA
    api.ref = {'object':{'type':'tag','sha':'bad'}}
    api.tags['bad'] = {'message':json.dumps({'version':1,'run':True,'attempt':1})}
    try: P.reserve(api,SHA,DIGEST,'maintenance',100,1)
    except ValueError: pass
    else: raise AssertionError('Invalid publication order accepted')
    assert not api.writes


def test_reservation_is_durable_before_channel_move_and_can_retry_after_failure():
    api = Registry()
    assert P.reserve(api,SHA,DIGEST,'maintenance',100,1)
    # Simulate failure moving latest after reservation. The marker must still
    # refuse older/repeated work; a newer attempt can repair publication.
    assert api.writes[-1][1] == 'git/refs'
    record = json.loads(api.tags[api.ref['object']['sha']]['message'])
    assert record['digest'] == DIGEST and record['revision'] == SHA
    assert not P.reserve(api,SHA,DIGEST,'maintenance',99,1)
    assert not P.reserve(api,SHA,DIGEST,'maintenance',100,1)
    assert P.reserve(api,SHA,DIGEST,'maintenance',100,2)
    assert api.writes[-1][0] == 'PATCH'


def test_actual_maintenance_script_skips_old_and_repeated_latest_moves():
    if not shutil.which('bash'):
        harness.skip('bash is required to execute the publication workflow')
    workflow = (ROOT/'.github/workflows/container.yml').read_text()
    lines = workflow.split('      - name: Publish only after both native platforms pass',1)[1].split('        run: |',1)[1].splitlines()
    script = '\n'.join(line[10:] for line in lines[1:] if line.startswith('          '))
    assert 'container_publication.py' in script, 'Actual publication guard is missing'
    with tempfile.TemporaryDirectory() as folder:
        box = Path(folder)
        shims = {
            'gh': "import os; print(os.environ['REVISION'])",
            'docker': '''import os,sys
from pathlib import Path
if 'inspect' in sys.argv:
    print('sha256:'+'b'*64)
elif 'create' in sys.argv and not any(arg.endswith(':latest') for arg in sys.argv):
    expected=[os.environ['IMAGE']+'@sha256:'+char*64 for char in ('b','c')]
    assert sys.argv[-2:] == expected, 'Assembly reconstructed tags instead of recorded digests'
elif any(arg.endswith(':latest') for arg in sys.argv):
    with Path(os.environ['FAKE_MOVES']).open('a') as stream: stream.write('latest\\n')
''',
            'python': '''import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(os.environ['FAKE_ROOT'])/'tests'))
import test_container_publication as fixture
if sys.argv[1].endswith('container_artifacts.py'):
    sys.argv.pop(1)
    fixture.A.main()
    raise SystemExit()
P=fixture.P
api=fixture.Registry()
state=Path(os.environ['FAKE_STATE'])
if state.exists(): api.__dict__.update(json.loads(state.read_text()))
def call(*args):
    result=api(*args)
    state.write_text(json.dumps(api.__dict__))
    return result
P.GitHub=lambda *args:call
sys.argv.pop(1)  # The shim stands in for `python tools/container_publication.py`.
P.main()
''',
        }
        for name, body in shims.items():
            path = box/name
            path.write_text('#!'+sys.executable+'\n'+body+'\n')
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(box)+os.pathsep+os.environ['PATH'],
                   IMAGE='example/fan', REVISION=SHA, CHANNEL='maintenance',
                   GH_TOKEN='placeholder', GITHUB_REPOSITORY='example/fan',
                   GITHUB_STEP_SUMMARY=str(box/'summary'), FAKE_ROOT=str(ROOT),
                   FAKE_STATE=str(box/'state'), FAKE_MOVES=str(box/'moves'))
        for run, attempt in ((200,1),(100,9),(200,1),(200,2)):
            env.update(GITHUB_RUN_ID=str(run), GITHUB_RUN_ATTEMPT=str(attempt))
            folder = box/'platform-artifacts'
            folder.mkdir(exist_ok=True)
            for arch,char in zip(A.ARCHES,('b','c')):
                # Simulate AMD64 preserved from attempt 1 while only ARM64 was retried.
                row = A.record(env['IMAGE'],SHA,'maintenance',arch,'sha256:'+char*64,run,
                               1 if arch=='amd64' else attempt)
                (folder/('platform-'+arch+'.json')).write_text(json.dumps(row))
            result = subprocess.run(['bash','-c',script], cwd=box,env=env,
                                    capture_output=True,text=True,timeout=15)
            assert result.returncode == 0, result.stdout+result.stderr
        assert (box/'moves').read_text().splitlines() == ['latest','latest'], 'Actual workflow moved latest for an old/repeated build'


def test_platform_artifacts_keep_successful_attempt_identity_and_validate_provenance():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for arch,char,attempt in (('amd64','b',1),('arm64','c',2)):
            row = A.record('example/fan',SHA,'candidate',arch,'sha256:'+char*64,100,attempt)
            (root/('platform-'+arch+'.json')).write_text(json.dumps(row))
        expected = ['example/fan@sha256:'+char*64 for char in ('b','c')]
        assert A.references(root,'example/fan',SHA,'candidate',100,2) == expected
        path = root/'platform-amd64.json'
        original = json.loads(path.read_text())
        for key,value in (('run',99),('revision','c'*40),('arch','arm64'),('channel','maintenance'),
                          ('attempt',3),('image','other/fan'),('digest','not-a-digest'),('version',True)):
            row = dict(original,**{key:value})
            path.write_text(json.dumps(row))
            try: A.references(root,'example/fan',SHA,'candidate',100,2)
            except ValueError: pass
            else: raise AssertionError('Wrong platform provenance accepted: '+key)
        path.unlink()
        try: A.references(root,'example/fan',SHA,'candidate',100,2)
        except FileNotFoundError: pass
        else: raise AssertionError('Missing architecture accepted')


def test_approval_can_read_the_workflow_provenance_it_checks():
    workflow = (ROOT/'.github/workflows/container-promote.yml').read_text()
    job = workflow.split('  promote:\n',1)[1]
    permissions = job.split('    permissions:\n',1)[1].split('    steps:',1)[0]
    assert '      actions: read\n' in permissions, 'Approval needs Actions read permission for supplying-run verification'


def test_metadata_anchor_does_not_replace_the_reviewed_source_identity():
    api = Registry()
    anchor = 'd'*40
    args = ['publication','--revision',SHA,'--digest',DIGEST,'--kind','approval']
    env = dict(GITHUB_REPOSITORY='example/fan',GH_TOKEN='placeholder',
               GITHUB_RUN_ID='200',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=anchor)
    with patch.dict(os.environ,env), patch.object(sys,'argv',args), \
            patch.object(P,'GitHub',return_value=api), contextlib.redirect_stdout(io.StringIO()) as out:
        P.main()
    assert out.getvalue().strip() == 'true'
    tag = api.tags[api.ref['object']['sha']]
    assert tag['object'] == anchor, 'Metadata must anchor to the workflow commit'
    record = json.loads(tag['message'])
    assert record['revision'] == SHA and record['digest'] == DIGEST
    assert api.approved == SHA


def test_three_day_schedule_crosses_month_end_and_does_not_gate_manual_builds():
    if not shutil.which('bash'):
        harness.skip('bash is required to execute the schedule gate')
    workflow = (ROOT/'.github/workflows/container.yml').read_text()
    assert "cron: '17 3 * * *'" in workflow
    lines = workflow.split('      - id: source',1)[1].split('  build:',1)[0].split('        run: |',1)[1].splitlines()
    script = '\n'.join(line[10:] for line in lines[1:] if line.startswith('          '))
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for name, body in {'date':"import os; print(os.environ['FAKE_NOW'])",
                           'gh':"import sys; print('true' if 'actions/workflows/' in sys.argv[2] else 'a'*40)", 'git':'pass'}.items():
            path = root/name
            path.write_text('#!'+sys.executable+'\n'+body+'\n')
            path.chmod(0o755)
        env = dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                   GITHUB_SHA=SHA,GITHUB_REPOSITORY='example/fan',GITHUB_REF='refs/heads/main',REVIEWED='true',
                   GH_TOKEN='placeholder',MAINTENANCE='false')
        days = [start+datetime.timedelta(days=i)
                for start in (datetime.date(2026,10,29),datetime.date(2026,12,29))
                for i in range(8)]
        selected = []
        for day in days:
            stamp = int(datetime.datetime.combine(day,datetime.time(3,17),datetime.timezone.utc).timestamp())
            for event,maintenance in (('schedule','false'),('workflow_dispatch','false'),('workflow_dispatch','true')):
                output = root/'output'
                output.write_text('')
                env.update(EVENT=event,MAINTENANCE=maintenance,FAKE_NOW=str(stamp),GITHUB_OUTPUT=str(output))
                result = subprocess.run(['bash','-c',script],cwd=ROOT,env=env,
                                        capture_output=True,text=True,timeout=10)
                assert result.returncode==0, result.stdout+result.stderr
                built = 'build=true\n' in output.read_text()
                expected = event!='schedule' or stamp//86400%3==0
                assert built == expected, (day,event,output.read_text())
                if event=='schedule' and built: selected.append(day)
        assert len(selected)>=2
        for month in (10,12):
            boundary = [day for day in selected if day.month in (month,month%12+1)]
            assert len(boundary)>=2
            assert all((b-a).days==3 for a,b in zip(boundary,boundary[1:])), boundary


def test_candidates_require_review_and_successful_exact_revision_checks():
    if not shutil.which('bash'):
        harness.skip('bash is required to execute the build gate')
    workflow = (ROOT/'.github/workflows/container.yml').read_text()
    assert 'workflow_run:' not in workflow, 'Source checks must not auto-trigger images'
    assert '  actions: read' in workflow.split('jobs:',1)[0]
    assert '      reviewed:' in workflow
    lines = workflow.split('      - id: source',1)[1].split('  build:',1)[0].split('        run: |',1)[1].splitlines()
    script = '\n'.join(line[10:] for line in lines[1:] if line.startswith('          '))
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for name,body in {
            'date':"print('259200')", 'git':'pass',
            'gh':'''import os,sys
from pathlib import Path
url=sys.argv[2]
if 'actions/workflows/' in url:
    with Path(os.environ['QUERY_LOG']).open('a') as f: f.write(url+'\\n')
    assert 'head_sha='+os.environ['GITHUB_SHA'] in url
    assert 'branch=main&event=push&status=completed' in url
    if os.environ['API_FAIL']=='true': raise SystemExit(1)
    print(os.environ['TESTS_PASS' if '/tests.yml/' in url else 'PRIVACY_PASS'])
else: print('a'*40)
''',
        }.items():
            path = root/name
            path.write_text('#!'+sys.executable+'\n'+body+'\n')
            path.chmod(0o755)
        base = dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                    GITHUB_SHA=SHA,GITHUB_REPOSITORY='example/fan',GH_TOKEN='placeholder',
                    GITHUB_REF='refs/heads/main',EVENT='workflow_dispatch',
                    REVIEWED='true',MAINTENANCE='false',TESTS_PASS='true',
                    PRIVACY_PASS='true',API_FAIL='false',QUERY_LOG=str(root/'queries'))
        cases = [({},True),({'REVIEWED':'false'},False),({'REVIEWED':''},False),
                 ({'TESTS_PASS':'false'},False),({'PRIVACY_PASS':'false'},False),
                 ({'API_FAIL':'true'},False),({'GITHUB_REF':'refs/heads/topic'},False),
                 ({'EVENT':'workflow_run'},False),
                 ({'MAINTENANCE':'true','REVIEWED':'false','TESTS_PASS':'false'},True),
                 ({'EVENT':'schedule','REVIEWED':'false','TESTS_PASS':'false'},True)]
        for changes,expected in cases:
            output,queries = root/'output',root/'queries'
            output.write_text(''); queries.write_text('')
            env = dict(base,**changes,GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash','-c',script],cwd=ROOT,env=env,
                                    capture_output=True,text=True,timeout=10)
            assert (result.returncode==0)==expected, (changes,result.stdout,result.stderr)
            assert ('build=true\n' in output.read_text())==expected
            if not changes:
                assert '/tests.yml/' in queries.read_text() and '/privacy.yml/' in queries.read_text()
            if changes.get('MAINTENANCE')=='true' or changes.get('EVENT')=='schedule':
                assert not queries.read_text(), 'Approved-source maintenance required a new review'


def test_scheduled_clock_failure_never_enables_build():
    if not shutil.which('bash'):
        harness.skip('bash is required to execute the schedule gate')
    workflow = (ROOT/'.github/workflows/container.yml').read_text()
    lines = workflow.split('      - id: source',1)[1].split('  build:',1)[0].split('        run: |',1)[1].splitlines()
    script = '\n'.join(line[10:] for line in lines[1:] if line.startswith('          '))
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for name,body in {
            'date':"import os,sys; print(os.environ['FAKE_NOW']); sys.exit(int(os.environ['FAKE_DATE_EXIT']))",
            'gh':"print('a'*40)", 'git':'pass',
        }.items():
            path = root/name
            path.write_text('#!'+sys.executable+'\n'+body+'\n')
            path.chmod(0o755)
        env = dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                   EVENT='schedule',MAINTENANCE='false',GITHUB_SHA=SHA,HEAD_SHA='',
                   GITHUB_REPOSITORY='example/fan',GH_TOKEN='placeholder')
        # 259200 is an eligible epoch day: ignoring a failed clock command
        # would incorrectly continue into a successful maintenance build.
        cases = (('259200','7'),('','7'),('','0'),('not-a-time','0'),
                 ('1+2','0'),('9'*100,'0'),('-1','0'),('123\n456','0'))
        for value,status in cases:
            output = root/'output'
            output.write_text('')
            env.update(FAKE_NOW=value,FAKE_DATE_EXIT=status,GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash','-c',script],cwd=ROOT,env=env,
                                    capture_output=True,text=True,timeout=10)
            assert result.returncode != 0, (value,status,result.stdout,result.stderr)
            assert 'build=true' not in output.read_text(), 'Failed clock enabled a build'
        # Base-10 arithmetic also handles a valid value with leading zeroes.
        env.update(FAKE_NOW='000259200',FAKE_DATE_EXIT='0')
        result = subprocess.run(['bash','-c',script],cwd=ROOT,env=env,
                                capture_output=True,text=True,timeout=10)
        assert result.returncode==0 and 'build=true' in output.read_text()


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
