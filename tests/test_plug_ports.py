"""Closed plug tuples are not reused inside the peer's retention window."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_recent_port_is_skipped_before_connecting_on_same_socket():
    C = harness.load()
    C._plug_ports[(int(C.socket.AF_INET), 41000)] = C.time.monotonic()
    sockets = []
    class Socket:
        family = C.socket.AF_INET
        def __init__(self, *args):
            self.port = 41000 if not sockets else 41002
            self.closed, self.connections = False, []
            sockets.append(self)
        def setsockopt(self, *args): pass
        def settimeout(self, value): self.timeout = value
        def bind(self, address): self.bound = address
        def getsockname(self): return ('127.0.0.1', self.port)
        def connect(self, address): self.connections.append(address)
        def close(self): self.closed = True
    old_socket, old_gai = C.socket.socket, C.socket.getaddrinfo
    C.socket.socket = Socket
    C.socket.getaddrinfo = lambda *a: [(C.socket.AF_INET, C.socket.SOCK_STREAM, 0, '', ('127.0.0.1', 80))]
    try:
        conn = C._PlugHTTPConnection('localhost', timeout=12)
        accepted = conn._new_conn()
        assert sockets[0].closed and not sockets[0].connections, 'Old tuple was reused'
        assert accepted is sockets[1] and accepted.connections == [('127.0.0.1',80)]
        assert accepted.bound == ('',0) and accepted.timeout == 12
    finally:
        C.socket.socket, C.socket.getaddrinfo = old_socket, old_gai


def test_transport_uses_guard_for_real_local_http_and_preserves_credentials():
    C = harness.load()
    ports, paths = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ports.append(self.client_address[1])
            paths.append(self.path)
            body = b'{"POWER":"ON","StatusSNS":{"ENERGY":{"Power":23}}}'
            self.send_response(200)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        for _ in range(3):
            plug = C.Tasmota('127.0.0.1:'+str(server.server_port), 'user', 'CHANGEME')
            assert plug.power_w() == 23
        assert len(set(ports)) == 3
        assert all((int(C.socket.AF_INET),p) in C._plug_ports for p in ports), 'Adapter bypasses guard'
        assert all('cmnd=Status+8' in p and 'password=CHANGEME' in p for p in paths)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_port_hold_expires_and_is_shared_across_preserving_restart():
    C = harness.load()
    key = (int(C.socket.AF_INET), 41000)
    C._plug_ports[key] = C.time.monotonic()-20
    payload = C._resume_payload()
    C._plug_ports.clear()
    old = os.environ.get(C.RESUME_ENV)
    os.environ[C.RESUME_ENV] = json.dumps(payload)
    try:
        assert C._apply_resume()
    finally:
        if old is None: os.environ.pop(C.RESUME_ENV,None)
        else: os.environ[C.RESUME_ENV] = old
    assert key in C._plug_ports
    assert 19 <= C.time.monotonic()-C._plug_ports[key] <= 22
    C._restore_plug_ports([[2,41001,10**400],[2,False,180],[999,41000,180],None])
    assert set(C._plug_ports) == {key}
    class Socket:
        family = C.socket.AF_INET
        def bind(self, address): pass
        def getsockname(self): return ('127.0.0.1',41000)
    C._plug_ports[key] = C.time.monotonic()-181
    assert C._bind_plug_socket(Socket()) == key


def test_failed_connect_holds_port_for_full_interval_after_close():
    C = harness.load()
    clock = [100.0]
    sockets = []
    class Socket:
        family = C.socket.AF_INET
        def __init__(self,*args):
            self.closed = False
            sockets.append(self)
        def setsockopt(self,*args): pass
        def settimeout(self,value): pass
        def bind(self,address): pass
        def getsockname(self): return ('127.0.0.1',41000)
        def connect(self,address):
            clock[0] += 12
            raise C.socket.timeout('connect timed out')
        def close(self): self.closed = True
    old_socket, old_gai, old_clock = C.socket.socket, C.socket.getaddrinfo, C.time.monotonic
    C.socket.socket = Socket
    C.socket.getaddrinfo = lambda *a: [(C.socket.AF_INET,C.socket.SOCK_STREAM,0,'',('127.0.0.1',80))]
    C.time.monotonic = lambda: clock[0]
    try:
        try:
            C._PlugHTTPConnection('localhost',timeout=12)._new_conn()
        except C.ConnectTimeoutError:
            pass
        else:
            raise AssertionError('Expected connect timeout')
        assert sockets[0].closed
        assert C._plug_ports[(int(C.socket.AF_INET),41000)] == 112
        assert C._plug_port_handover() == [[int(C.socket.AF_INET),41000,180]]
    finally:
        C.socket.socket, C.socket.getaddrinfo, C.time.monotonic = old_socket, old_gai, old_clock


def test_refused_real_connection_remains_a_classified_plug_error():
    C = harness.load()
    with C.socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        # Bound but not listening: no other process can claim this port.
        plug = C.Tasmota('127.0.0.1:'+str(sock.getsockname()[1]))
        try:
            plug.state()
        except C.PlugError as exc:
            assert exc.reason == 'ConnectionError'
        else:
            raise AssertionError('Refused connection was swallowed')
    assert C._plug_ports, 'Failed attempts must also hold their source port'
    assert C._plug_request_lock.acquire(blocking=False)
    C._plug_request_lock.release()


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
