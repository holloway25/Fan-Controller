"""Inside the built image, network-isolated simulated devices and real processes."""
import importlib.util
import json
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

data=Path('/data')
phase=sys.argv[1]
co2=[1005 if phase=='seed' else 850]
spec=importlib.util.spec_from_file_location('emulator','/fixtures/plug_emulator.py')
emulator=importlib.util.module_from_spec(spec)
spec.loader.exec_module(emulator)
plug=emulator.EmulatorServer(('127.0.0.1',18080))
plug.state.update({'relay':phase=='resume','fan_profile':False,'on_watts':23,'off_watts':0})
threading.Thread(target=plug.serve_forever,daemon=True).start()


class Protect(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,value):
        body=json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.reply({})
    def do_GET(self):
        self.reply({'sensors':[{'id':'test-sensor','isConnected':True,
                               'airQuality':{'co2':co2[0],'temperature':21,'humidity':45}}]})


server=ThreadingHTTPServer(('127.0.0.1',18081),Protect)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixtures/cert.pem','/fixtures/key.pem')
server.socket=context.wrap_socket(server.socket,server_side=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
if phase=='seed':
    config=json.loads(Path('/app/config.example.json').read_text())
    config['protect'].update(host='127.0.0.1:18081',sensor_id='test-sensor')
    config['plug'].update(host='127.0.0.1:18080',watts_min=19,watts_max=27)
    config['control'].update(mode='auto',poll_seconds=5,min_run_minutes=10,cold_start_minutes=0,
                             on_at=1000,off_at=800,stale_minutes=0)
    (data/'config.json').write_text(json.dumps(config))
elif phase=='fault':
    co2[0]=1200


def request(path='/api/state',body=None):
    r=urllib.request.Request('http://127.0.0.1:8096'+path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(r,timeout=4) as response:
        return json.load(response)


def until(predicate,timeout=60):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            last=request()
            if predicate(last): return last
        except OSError: pass
        time.sleep(0.25)
    raise AssertionError('Runtime condition timed out: '+str(last))


log=open('/tmp/controller-test.log','w+')
process=subprocess.Popen([sys.executable,'/app/start.py'],stdout=log,stderr=log)
try:
    if phase=='seed':
        until(lambda s:s['fan'] is True and s['latch']['co2'])
        co2[0]=850
        before=until(lambda s:s['co2']==850 and s['fan'] is True)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60)==0
        assert (data/'resume.json').exists()
        (data/'proof.json').write_text(json.dumps({'remaining':before['min_run_remaining']}))
    elif phase=='resume':
        current=until(lambda s:s['co2']==850 and s['fan'] is True and s['latch']['co2'])
        assert 0<current['min_run_remaining']<=json.loads((data/'proof.json').read_text())['remaining']
        assert not (data/'resume.json').exists()
        assert any('Resumed after restart' in e['msg'] for e in current['events'])
        plug.state.update({'on_watts':100})
        request('/api/config',{'plug':{'watts_enabled':True,'watts_fault_seconds':5}})
        fault=until(lambda s:s['fault'] is not None and s['fault']['stopped'],timeout=65)
        assert fault['fault']['code']=='FC-004' and fault['fan'] is False
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60)==0
        assert (data/'resume.json').exists()
    elif phase=='fault':
        current=until(lambda s:s['co2']==1200 and s['fan'] is False)
        assert current['fault']['code']=='FC-004'
        assert not plug.state.snapshot()['relay'], 'Standing fault allowed Auto ON'
        assert request('/api/fault/clear',{})['cleared']
        until(lambda s:s['fan'] is True and s['fault'] is None)
        process.kill()  # Forced exit must not create a clean-stop checkpoint.
        process.wait(timeout=5)
        assert not (data/'resume.json').exists()
        # Prepare the actual Docker stop/start smoke test with offline Manual.
        config=json.loads((data/'config.json').read_text())
        config['control']['mode']='manual'
        config['plug']['watts_enabled']=False
        (data/'config.json').write_text(json.dumps(config))
    else: raise AssertionError('Unknown integration phase')
    print('PASS container phase',phase,flush=True)
except BaseException:
    log.flush()
    log.seek(0)
    print(log.read()[-6000:],file=sys.stderr)
    raise
finally:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
