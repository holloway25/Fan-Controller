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


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
