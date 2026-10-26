"""Real two-address Waitress startup, preserving exec and bounded shutdown."""
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


CHILD = r'''
import importlib.util,json,os,socket,threading
from pathlib import Path
root=Path(os.environ['FAN_CONTROLLER_DATA_DIR'])
spec=importlib.util.spec_from_file_location('fan',root/'controller.py')
C=importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
resolve=socket.getaddrinfo
def addresses(host,port,*args,**kwargs):
    if host=='multi.invalid':
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(ip,int(port)))
                for ip in ('127.0.0.1','127.0.0.2')]
    return resolve(host,port,*args,**kwargs)
socket.getaddrinfo=addresses
C.loop=lambda:C._stopping.wait(30)
C.watts_loop=lambda:C._stopping.wait(30)
C.RESTART_POLL_SECONDS=0.02
C._notify=lambda *a,**k:None
if C.RESUME_ENV not in os.environ:
    C.STATE['fan']=True
    C.STATE['latch']['co2']=True
    with C._state_lock:
        C._stamp_elapsed_locked('changed_at')
original=C._make_http_server
def make():
    result=original('multi.invalid',0)
    server=result[0]
    assert type(server).__name__=='MultiSocketServer'
    tmp=root/'listeners.tmp'
    tmp.write_text(json.dumps(dict(listeners=server.effective_listen,
        generation=C.PROC_GENERATION,pid=os.getpid())))
    os.replace(tmp,root/'listeners.json')
    return result
C._make_http_server=make
@C.app.route('/blocked-worker')
def blocked():
    (root/'worker-entered').touch()
    threading.Event().wait(30)
    return 'done'
C._run_controller()
'''


def get(address, path):
    conn = http.client.HTTPConnection(address[0], int(address[1]), timeout=1)
    try:
        conn.request('GET', path)
        reply = conn.getresponse()
        assert reply.status == 200
        return json.loads(reply.read())
    finally:
        conn.close()


def test_multi_address_startup_preserving_restart_and_busy_shutdown():
    if os.name != 'posix':
        harness.skip('POSIX signals and Linux loopback addresses')
    with tempfile.TemporaryDirectory(prefix='fan-wsgi-multi-') as folder:
        root = Path(folder)
        shutil.copy2(Path(harness.SRC)/'controller.py', root/'controller.py')
        script = root/'run.py'
        script.write_text(CHILD)
        env = dict(os.environ, FAN_CONTROLLER_DATA_DIR=str(root),
                   FAN_CONTROLLER_PERSIST_STATE='1', FAN_CONTROLLER_DISABLE_PLUG='1')
        with (root/'process.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(script)], env=env,
                                       stdout=log, stderr=log)
            idle, busy = [], None
            def wait_ready(previous=None):
                deadline = time.monotonic()+6
                while time.monotonic() < deadline:
                    assert process.poll() is None, (root/'process.log').read_text()
                    try:
                        record = json.loads((root/'listeners.json').read_text())
                        if previous == record['generation']:
                            time.sleep(0.01)
                            continue
                        assert len(record['listeners']) == 2
                        for address in record['listeners']:
                            assert get(address, '/healthz')['ok'] is True
                        return record
                    except (OSError, ValueError, http.client.HTTPException):
                        time.sleep(0.01)
                raise AssertionError('Multi-address server did not become ready: '+
                                     (root/'process.log').read_text())
            try:
                before = wait_ready()
                state = get(before['listeners'][0], '/api/state')
                assert state['latch']['co2'] and state['min_run_remaining'] > 0
                (root/'restart.request').touch()
                after = wait_ready(before['generation'])
                assert after['pid'] == before['pid'] == process.pid
                for address in after['listeners']:
                    resumed = get(address, '/api/state')
                    assert resumed['latch']['co2'], resumed['events'][:3]
                    # Fan state is deliberately unknown until a fresh relay
                    # observation; this interlocked fixture never polls a plug.
                    assert resumed['fan'] is None
                    assert 0 < resumed['min_run_remaining'] <= state['min_run_remaining']
                    assert any('Resumed after restart' in e['msg'] for e in resumed['events'])
                    conn = socket.create_connection(tuple(address), timeout=1)
                    conn.sendall(b'GET /idle HTTP/1.1\r\nHost: localhost\r\n')
                    idle.append(conn)
                def block():
                    try: get(after['listeners'][0], '/blocked-worker')
                    except (OSError, http.client.HTTPException): pass
                busy = threading.Thread(target=block, daemon=True)
                busy.start()
                deadline = time.monotonic()+2
                while not (root/'worker-entered').exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=7) == 0, (root/'process.log').read_text()
                assert time.monotonic()-started < 5, 'Multi-server shutdown bypassed cleanup bound'
                checkpoint = json.loads((root/'resume.json').read_text())
                assert checkpoint['payload']['latch']['co2'] is True
                for address in after['listeners']:
                    try:
                        conn = socket.create_connection(tuple(address), timeout=0.2)
                    except OSError:
                        pass
                    else:
                        conn.close()
                        raise AssertionError('Listener survived shutdown')
            finally:
                for conn in idle: conn.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                if busy is not None: busy.join(2)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
