"""Real trickling HTTP headers/bodies cannot hold the actuation lock forever."""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_trickling_headers_and_body_release_mode_save_without_server_release():
    for phase in ('headers', 'body'):
        C = harness.load()
        entered, release = threading.Event(), threading.Event()
        paths, errors, saves = [], [], []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                entered.set()
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                    if phase == 'headers':
                        self.wfile.write(b'X-Trickle: ')
                        chunk = b'x'
                    else:
                        self.wfile.write(b'Content-Length: 100000\r\n\r\n{"POWER":"ON","pad":"')
                        chunk = b' '
                    self.wfile.flush()
                    while not release.wait(0.02):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, ValueError):
                    pass
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        host = '127.0.0.1:' + str(server.server_port)
        C.CFG['plug']['host'] = host
        original_get = C._plug_http_get
        def short_get(url, **kwargs):
            kwargs['timeout'] = 0.3
            return original_get(url, **kwargs)
        C._plug_http_get = short_get
        def read():
            try:
                with C._act_lock:
                    C.Tasmota(host).state()
            except C.PlugError as exc:
                errors.append(exc)
        def save():
            saves.append(C.app.test_client().post('/api/config', json={
                'control': {'mode': 'manual'}}))
        reader, saver = threading.Thread(target=read), threading.Thread(target=save)
        start = time.monotonic()
        try:
            reader.start()
            assert entered.wait(1)
            saver.start()
            reader.join(1.5)
            saver.join(1.5)
            assert not reader.is_alive(), phase + ' trickle exceeded deadline'
            assert not saver.is_alive(), 'Mode save remained blocked'
            assert not release.is_set(), 'Test released response before completion'
            assert len(errors) == 1 and errors[0].reason == 'Timeout', errors
            assert saves[0].status_code == 200, saves[0].get_json()
            assert time.monotonic() - start < 2
            assert len(paths) == 1, 'Transport retried the command'
            assert C._plug_request_lock.acquire(timeout=0.1), 'Request barrier leaked'
            C._plug_request_lock.release()
            assert C._plug_ports, 'Timed-out connection lost source-port protection'
        finally:
            release.set()
            reader.join(2)
            if saver.ident is not None:
                saver.join(2)
            server.shutdown()
            server.server_close()
            serving.join(2)


def test_switch_stays_on_calling_thread_and_healthy_request_recovers():
    C = harness.load()
    calls, commands = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            commands.append(self.path)
            body = b'{"POWER":"ON"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    original = C._PlugHTTPConnection.request
    def request(self, *args, **kwargs):
        calls.append(threading.get_ident())
        return original(self, *args, **kwargs)
    C._PlugHTTPConnection.request = request
    try:
        plug = C.Tasmota('127.0.0.1:' + str(server.server_port))
        plug.set(True)
        assert plug.state() is True
        assert calls == [threading.get_ident()] * 2, 'Command moved into background worker'
        assert len(commands) == 2
        assert getattr(C._plug_request_context, 'deadline', None) is None
    finally:
        C._PlugHTTPConnection.request = original
        server.shutdown()
        server.server_close()
        serving.join(2)


def test_overall_deadline_includes_dns_and_discards_late_resolution():
    C = harness.load()
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []
    def resolve(*args):
        entered.set()
        release.wait(3)
        return [(C.socket.AF_INET, C.socket.SOCK_STREAM, 0, '', ('127.0.0.1', 9))]
    def send():
        try:
            C._plug_http_get('http://plug.invalid/cm', params={'cmnd': 'Power ON'}, timeout=0.15)
        except C.requests.Timeout as exc:
            errors.append(exc)
    original = C._PlugHTTPConnection.request
    def request(self, *args, **kwargs):
        calls.append(threading.get_ident())
        return original(self, *args, **kwargs)
    worker = threading.Thread(target=send)
    try:
        with patch.object(C.socket, 'getaddrinfo', resolve), \
                patch.object(C._PlugHTTPConnection, 'request', request):
            worker.start()
            assert entered.wait(1)
            worker.join(1)
            assert not worker.is_alive() and len(errors) == 1
            assert not release.is_set(), 'Request waited for resolver'
            # urllib3 entered request on the caller, but no socket was opened.
            assert calls == [worker.ident]
            assert not C._plug_ports
            release.set()
            outcome = next(iter(C._plug_dns_pending.values()))
            assert outcome['done'].wait(1)
            assert not C._plug_ports, 'Late resolver opened a connection'
    finally:
        release.set()
        worker.join(2)


def test_slow_connection_close_holds_port_from_body_teardown_on_success_and_timeout():
    for timeout in (False, True):
        C = harness.load()
        C.PLUG_PORT_HOLD_SECONDS = 0.2
        release = threading.Event()
        ports = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ports.append(self.client_address[1])
                body = b'{"POWER":"ON"}'
                self.send_response(200)
                self.send_header('Connection', 'close')
                self.send_header('Content-Length', str(100000 if timeout else len(body)))
                self.end_headers()
                try:
                    if timeout:
                        self.wfile.write(body[:1])
                        self.wfile.flush()
                        # Also bound the fixture when mutation testing removes
                        # the client deadline; report a failure instead of hang.
                        stop = time.monotonic() + 1.2
                        while time.monotonic() < stop and not release.wait(0.02):
                            self.wfile.write(b' ')
                            self.wfile.flush()
                    else:
                        release.wait(0.35)
                        self.wfile.write(body)
                except (OSError, ValueError):
                    pass
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        class ReusedPort:
            family = C.socket.AF_INET
            def bind(self, _): pass
            def getsockname(self): return ('127.0.0.1', ports[0])
        try:
            try:
                reply = C._plug_http_get('http://127.0.0.1:%d/cm' % server.server_port,
                                         timeout=0.4 if timeout else 1)
            except C.requests.Timeout:
                assert timeout, 'Healthy slow response timed out'
            else:
                assert not timeout, 'Trickling body escaped deadline'
                assert reply.json() == {'POWER': 'ON'}
            ended = time.monotonic()
            key = (int(C.socket.AF_INET), ports[0])
            assert ended - C._plug_ports[key] < 0.1, 'Hold started before body teardown'
            assert C._bind_plug_socket(ReusedPort()) is None, 'Port was reusable too early'
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(2)


def test_timer_start_failure_is_a_plug_error_and_manual_fault_without_http():
    C = harness.load()
    attempted = []
    def fail_start(_):
        raise RuntimeError('No thread capacity')
    def unexpected_http(*args, **kwargs):
        attempted.append(True)
        raise AssertionError('HTTP ran without deadline protection')
    C.CFG['plug']['host'] = '127.0.0.1:9'
    plug = C.Tasmota(C.CFG['plug']['host'])
    C._clients = lambda: (None, plug)
    C.CFG['control']['mode'] = 'manual'
    C.STATE['manual_off_pending'] = True
    with patch.object(C.threading.Timer, 'start', fail_start), \
            patch.object(C.requests.Session, 'get', unexpected_http):
        response = C.app.test_client().post('/api/plug/test', json={})
        assert response.status_code == 400 and response.is_json, response.data
        assert 'ConnectionError' in response.get_json()['error']
        try:
            C._manual_off(plug, C._cfg_generation)
        except C.PlugError as exc:
            assert exc.reason == 'ConnectionError'
        else:
            raise AssertionError('Failed Manual OFF was not reported')
    assert not attempted
    assert C.STATE['manual_off_pending']
    assert C.STATE['fault']['code'] == 'FC-009'
    assert not C.STATE['fault']['stopped']
    assert C.load_fault()['code'] == 'FC-009'
    assert getattr(C._plug_request_context, 'deadline', None) is None
    assert C._plug_request_lock.acquire(timeout=0.1)
    C._plug_request_lock.release()


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
