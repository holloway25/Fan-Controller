"""Test the actual image using separate containers and one persistent volume."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


def run(*args,**kwargs):
    return subprocess.run(list(args),check=True,**kwargs)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('image')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    run('docker','run','--rm','--network','none','--entrypoint','python',args.image,'-c',
        "import os,sys; from pathlib import Path; assert os.getuid()==10001; "
        "assert sys.version_info[:2]==(3,14); assert not Path('/app/config.json').exists(); "
        "assert not Path('/app/.git').exists(); print('PASS non-root image without live configuration')")
    name='fan-integration-'+uuid.uuid4().hex[:12]
    container=name+'-service'
    run('docker','volume','create',name)
    try:
        with tempfile.TemporaryDirectory() as folder:
            fixtures=Path(folder)
            fixtures.chmod(0o755)
            shutil.copy(root/'tests/container/runtime.py',fixtures/'runtime.py')
            shutil.copy(root/'tools/plug_emulator.py',fixtures/'plug_emulator.py')
            run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-subj','/CN=localhost','-keyout',str(fixtures/'key.pem'),
                '-out',str(fixtures/'cert.pem'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            for file in fixtures.iterdir(): file.chmod(0o644)
            for phase in ('seed','resume','fault'):
                run('docker','run','--rm','--network','none','--read-only',
                    '--cap-drop','ALL','--security-opt','no-new-privileges',
                    '--tmpfs','/tmp:size=16m,mode=1777',
                    '--mount','type=volume,src='+name+',dst=/data',
                    '--mount','type=bind,src='+str(fixtures)+',dst=/fixtures,readonly',
                    '--entrypoint','python',args.image,'/fixtures/runtime.py',phase)
            run('docker','run','-d','--name',container,'--network','none',
                '--mount','type=volume,src='+name+',dst=/data',args.image)
            probe="import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8096/api/state'))['config']['control']['mode'])"
            for _ in range(40):
                r=subprocess.run(['docker','exec',container,'python','-c',probe],capture_output=True)
                if r.returncode==0:
                    assert r.stdout.strip()==b'manual'
                    break
                time.sleep(0.25)
            else: raise AssertionError('Default container entrypoint did not start')
            run('docker','stop','--timeout','60',container)
            status=json.loads(subprocess.check_output(['docker','inspect',container]))[0]
            assert status['State']['ExitCode']==0, status['State']
            run('docker','start',container)
            check="from pathlib import Path; import json,urllib.request; s=json.load(urllib.request.urlopen('http://127.0.0.1:8096/api/state')); assert not Path('/data/resume.json').exists(); assert any('Resumed after restart' in e['msg'] for e in s['events'])"
            for _ in range(40):
                r=subprocess.run(['docker','exec',container,'python','-c',check],capture_output=True)
                if r.returncode==0: break
                time.sleep(0.25)
            else: raise AssertionError('Docker stop/start did not restore its checkpoint')
            print('PASS actual Docker SIGTERM stop/start and persistent-volume replacement')
    finally:
        subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        run('docker','volume','rm',name)


if __name__=='__main__':
    main()
