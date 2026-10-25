"""Real Waitress sockets and processes, with isolated data and simulated devices."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def get(port, path='/api/state', source='127.0.0.1', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3,
                                     source_address=(source, 0))
    try:
        conn.request('GET', path, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        conn.close()


def test_waitress_preserves_proxy_boundary_and_serves_concurrent_requests():
    if os.name != 'posix':
        harness.skip('Linux loopback source addresses')
    with patch.dict(os.environ, FAN_CONTROLLER_ALLOWED_PROXY_IPS='127.0.0.2',
                    FAN_CONTROLLER_TRUST_PROXY='1'):
        C = harness.load()
    entered, release = threading.Event(), threading.Event()
    @C.app.route('/wsgi-probe')
    def probe():
        return C.jsonify(remote=C.request.remote_addr, scheme=C.request.scheme,
                         host=C.request.host)
    @C.app.route('/wsgi-block')
    def block():
        entered.set()
        release.wait(3)
        return 'done'
    server, channels, dispatcher = C._make_http_server('127.0.0.1', 0)
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    port = int(server.effective_port)
    blocked = None
    try:
        status, headers, _ = get(port, '/healthz')
        assert status == 200 and headers.get('Server') == 'waitress', headers
        assert get(port)[0] == 403
        assert get(port, headers={'X-Forwarded-For': '127.0.0.2'})[0] == 403
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3,
                                         source_address=('127.0.0.2', 0))
        try:
            conn.request('POST', '/api/config', body=b'x'*8193,
                         headers={'Content-Type':'application/json'})
            reply = conn.getresponse()
            assert reply.status == 413
            reply.read()
        finally:
            conn.close()
        status, _, body = get(port, '/wsgi-probe', '127.0.0.2', {
            'X-Forwarded-For': '198.51.100.20', 'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'dashboard.invalid'})
        assert status == 200
        assert json.loads(body) == dict(remote='198.51.100.20', scheme='https',
                                        host='dashboard.invalid'), body
        blocked = threading.Thread(target=lambda: get(port, '/wsgi-block', '127.0.0.2'))
        blocked.start()
        assert entered.wait(1)
        assert get(port, '/healthz')[0] == 200, 'One busy request blocks health checks'
    finally:
        release.set()
        if blocked is not None:
            blocked.join(3)
        C._close_http_server(server, channels, dispatcher)
        serving.join(3)
        assert not serving.is_alive()


def test_http_cleanup_is_bounded_even_if_dispatcher_shutdown_stalls():
    C = harness.load()
    release, finished = threading.Event(), threading.Event()
    class Dispatcher:
        def shutdown(self, timeout):
            try:
                release.wait(10)
            finally:
                finished.set()
    started = time.monotonic()
    try:
        C._close_http_server(None, {}, Dispatcher())
        assert time.monotonic() - started < 4
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


def test_bind_failure_cleans_up_without_starting_controller_loops():
    C = harness.load()
    import socket
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with patch.object(C, '_start_controller', side_effect=AssertionError('Loops started')):
            with patch.dict(os.environ, FAN_CONTROLLER_BIND='127.0.0.1'):
                original = C._make_http_server
                C._make_http_server = lambda: original('127.0.0.1', occupied.getsockname()[1])
                try:
                    C._run_controller()
                except OSError:
                    pass
                else:
                    raise AssertionError('Occupied port was accepted')


CHILD = r'''
import importlib.util,json,os,threading
from pathlib import Path
root=Path(os.environ['FAN_CONTROLLER_DATA_DIR'])
spec=importlib.util.spec_from_file_location('isolated_controller',root/'controller.py')
C=importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
class Protect:
    def readings(self,sensor):
        if (root/'block-protect').exists():
            (root/'protect-entered').touch()
            threading.Event().wait(90)
        return dict(co2=float((root/'reading').read_text()),temperature=21,humidity=45), 'fixture'
C._clients=lambda:(Protect(),C.Tasmota(C.CFG['plug']['host']))
original=C._make_http_server
def make():
    result=original('127.0.0.1',0)
    (root/'http-port').write_text(str(result[0].effective_port))
    return result
C._make_http_server=make
if os.environ.get('WSGI_SIGNAL_IN_CALLBACK'):
    import signal
    from waitress.channel import HTTPChannel
    received=HTTPChannel.received
    def signal_callback(self,data):
        if data.startswith(b'GET /idle '):
            (root/'idle-entered').touch()
        if data.startswith(b'GET /signal-in-callback '):
            if os.environ.get('WSGI_CALLBACK_PHASE')=='write':
                self._signal_on_flush=True
                # Keep the request incomplete: this 100 response is flushed
                # by the main socket callback, never by a web worker.
                self.outbufs[-1].append(b'HTTP/1.1 100 Continue\r\n\r\n')
                self.total_outbufs_len+=25
                return
            os.kill(os.getpid(),getattr(signal,os.environ['WSGI_SIGNAL_IN_CALLBACK']))
        return received(self,data)
    HTTPChannel.received=signal_callback
    original_flush=HTTPChannel._flush_some
    def flush_callback(self,do_close=True):
        if getattr(self,'_signal_on_flush',False):
            self._signal_on_flush=False
            os.kill(os.getpid(),getattr(signal,os.environ['WSGI_SIGNAL_IN_CALLBACK']))
        return original_flush(self,do_close=do_close)
    HTTPChannel._flush_some=flush_callback
@C.app.route('/wsgi-threads')
def threads():
    return C.jsonify(pid=os.getpid(),names=[t.name for t in threading.enumerate()])
@C.app.route('/wsgi-block')
def block():
    (root/'web-entered').touch()
    threading.Event().wait(90)
    return 'done'
C._run_controller()
'''


def socket_callback_shutdown(phase):
    import socket
    for sig in ('SIGTERM', 'SIGINT'):
        C = harness.load(plug_access_disabled=True)
        root = Path(C.DIR)
        C.CFG['control']['mode'] = 'manual'
        (root/'config.json').write_text(json.dumps(C.CFG))
        (root/'reading').write_text('850')
        env = dict(os.environ, FAN_CONTROLLER_DATA_DIR=str(root),
                   FAN_CONTROLLER_PERSIST_STATE='1', FAN_CONTROLLER_TRUST_PROXY='0',
                   FAN_CONTROLLER_ALLOWED_PROXY_IPS='', WSGI_SIGNAL_IN_CALLBACK=sig,
                   WSGI_CALLBACK_PHASE=phase,
                   PYTHONUNBUFFERED='1')
        env.pop(C.RESUME_ENV, None)
        sockets = []
        with (root/'callback.log').open('w+') as log:
            process = subprocess.Popen([sys.executable, '-c', CHILD], env=env,
                                       stdout=log, stderr=log)
            def wait_for(path):
                deadline = time.monotonic()+5
                while not path.exists() and time.monotonic() < deadline:
                    assert process.poll() is None, 'Controller exited before callback'
                    time.sleep(0.01)
                assert path.exists(), path.name+' was not reached'
            try:
                wait_for(root/'http-port')
                port = int((root/'http-port').read_text())
                idle = socket.create_connection(('127.0.0.1', port), timeout=2)
                sockets.append(idle)
                # Incomplete headers keep a separate channel in the async map.
                idle.sendall(b'GET /idle HTTP/1.1\r\nHost: localhost\r\n')
                wait_for(root/'idle-entered')
                trigger = socket.create_connection(('127.0.0.1', port), timeout=2)
                sockets.append(trigger)
                started = time.monotonic()
                trigger.sendall(b'GET /signal-in-callback HTTP/1.1\r\nHost: localhost\r\n\r\n')
                assert process.wait(timeout=5) == 0
                assert time.monotonic()-started < 5
                assert (root/'resume.json').exists(), 'No clean-stop checkpoint'
                assert idle.recv(1) == b'', 'Idle connection survived shutdown'
                assert 'Orderly-stop state saved.' in (root/'callback.log').read_text()
            finally:
                for conn in sockets: conn.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


def test_signal_inside_socket_callback_exits_with_another_connection_open():
    socket_callback_shutdown('read')


def test_signal_inside_response_flush_exits_with_another_connection_open():
    socket_callback_shutdown('write')


def test_real_process_retains_latch_lease_and_fault_and_stops_during_busy_requests():
    C = harness.load()
    root = Path(C.DIR)
    shutil.copy(Path(harness.SRC)/'templates/index.html', root/'templates/index.html')
    spec = importlib.util.spec_from_file_location('wsgi_emulator',
                         Path(harness.SRC)/'tools/plug_emulator.py')
    emulator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emulator)
    plug = emulator.EmulatorServer(('127.0.0.1', 0))
    plug.state.update(dict(relay=False, fan_profile=False, on_watts=23, off_watts=0))
    serving = threading.Thread(target=plug.serve_forever, daemon=True)
    serving.start()
    config = C.CFG
    config['protect'].update(host='127.0.0.1', sensor_id='fixture')
    config['plug'].update(host='127.0.0.1:'+str(plug.server_port), watts_enabled=False)
    config['control'].update(mode='auto', poll_seconds=5, min_run_minutes=10,
                             cold_start_minutes=0, co2_enabled=True,
                             temp_enabled=False, humidity_enabled=False,
                             on_at=1000, off_at=800, stale_minutes=0)
    (root/'config.json').write_text(json.dumps(config))
    (root/'reading').write_text('1005')
    env = dict(os.environ, FAN_CONTROLLER_DATA_DIR=str(root),
               FAN_CONTROLLER_PERSIST_STATE='1', FAN_CONTROLLER_TRUST_PROXY='0',
               FAN_CONTROLLER_ALLOWED_PROXY_IPS='', PYTHONUNBUFFERED='1')
    env.pop(C.RESUME_ENV, None)
    process, log = None, None
    def start():
        nonlocal process, log
        (root/'http-port').unlink(missing_ok=True)
        log = (root/'process.log').open('a+')
        process = subprocess.Popen([sys.executable, '-c', CHILD], env=env,
                                   stdout=log, stderr=log)
    def until(predicate, timeout=15):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            assert process.poll() is None, (root/'process.log').read_text()[-3000:]
            try:
                port = int((root/'http-port').read_text())
                status, _, body = get(port)
                if status == 200:
                    state = json.loads(body)
                    if predicate(state): return port, state
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise AssertionError('Runtime condition timed out')
    def stop():
        before = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert time.monotonic()-before < 8, 'Shutdown exceeded bounded web cleanup'
        log.close()
    try:
        start()
        port, _ = until(lambda s:s['fan'] is True and s['latch']['co2'])
        assert get(port, '/')[0] == 200
        status, _, body = get(port, '/wsgi-threads')
        proof = json.loads(body)
        assert proof['pid'] == process.pid
        for name in ('fan-control','fan-power','fan-restart'):
            assert proof['names'].count(name) == 1, proof
        assert sum(n.startswith('waitress-') for n in proof['names']) == 4, proof
        (root/'reading-next').write_text('850')
        (root/'reading-next').replace(root/'reading')
        _, before = until(lambda s:s['co2'] == 850 and s['latch']['co2'])
        stop()
        assert (root/'resume.json').exists()
        assert plug.state.snapshot()['relay'], 'Shutdown commanded fan OFF'
        start()
        port, resumed = until(lambda s:s['co2'] == 850 and s['latch']['co2'])
        assert 0 < resumed['min_run_remaining'] <= before['min_run_remaining']
        assert not (root/'resume.json').exists()
        assert any('Resumed after restart' in e['msg'] for e in resumed['events'])
        # A blocked sensor read and blocked HTTP worker must not delay shutdown.
        (root/'block-protect').touch()
        until(lambda s:(root/'protect-entered').exists())
        def busy_request():
            try: get(port, '/wsgi-block')
            except OSError: pass  # Server closes the in-flight connection.
        busy = threading.Thread(target=busy_request, daemon=True)
        busy.start()
        until(lambda s:(root/'web-entered').exists())
        stop()
        busy.join(3)
        assert (root/'resume.json').exists()
        assert plug.state.snapshot()['relay']
        text = (root/'process.log').read_text()
        assert 'Orderly-stop state saved.' in text
        assert 'development server' not in text
        # A durable standing fault is not lost by starting the new web server.
        fault = dict(at=time.time(), msg='Simulated saved fault', code='FC-004', stopped=True)
        (root/'fault.json').write_text(json.dumps(fault))
        (root/'block-protect').unlink()
        plug.state.update(dict(relay=False))
        start()
        _, state = until(lambda s:s['fault'] is not None and s['fan'] is False)
        assert state['fault']['code'] == 'FC-004' and not plug.state.snapshot()['relay']
        stop()
        assert json.loads((root/'fault.json').read_text())['code'] == 'FC-004'
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if log is not None and not log.closed: log.close()
        plug.shutdown()
        plug.server_close()
        serving.join(3)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
