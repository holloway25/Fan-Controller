"""Only name resolution may outlive a plug request, never socket commands."""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_stalled_dns_releases_mode_save_and_never_sends_a_late_command():
    C = harness.load()
    C.PLUG_DNS_SECONDS = 0.2
    C.CFG['control']['mode'] = 'on'
    C.CFG['plug']['host'] = 'plug.invalid'
    entered, release, finished = (threading.Event() for _ in range(3))
    requests, errors, calls = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"POWER":"ON"}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    def resolve(*args):
        calls.append(args[0])
        entered.set()
        release.wait(3)
        finished.set()
        return [(C.socket.AF_INET, C.socket.SOCK_STREAM, 0, '',
                 ('127.0.0.1', server.server_port))]
    def force():
        try:
            C._force_on(C.Tasmota('plug.invalid'))
        except C.PlugError as error:
            errors.append(error)
    worker = threading.Thread(target=force)
    try:
        with patch.object(C.socket, 'getaddrinfo', resolve):
            worker.start()
            assert entered.wait(1)
            response = C.app.test_client().post('/api/config', json={
                'control': {'mode': 'manual'},
                'plug': {'host': '127.0.0.1:' + str(server.server_port)}})
            assert response.status_code == 200, response.get_json()
            assert not release.is_set(), 'Mode save waited for DNS result'
            assert not finished.is_set(), 'DNS completed before the mode save was released'
            worker.join(1)
            assert not worker.is_alive() and errors
            assert calls == ['plug.invalid'], 'Abandoned lookup was duplicated'
            assert requests == []
            release.set()
            assert finished.wait(1)
            server.shutdown()
            assert requests == [], 'Late DNS result sent an HTTP request'
    finally:
        release.set()
        worker.join(2)
        server.shutdown()
        server.server_close()
        serving.join(2)


def test_healthy_dns_overlap_shares_lookup_but_expired_workers_do_not_stack():
    for expires in (False, True):
        C = harness.load()
        C.PLUG_DNS_SECONDS = 0.15 if expires else 2
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []
        addresses = [(C.socket.AF_INET, C.socket.SOCK_STREAM, 0, '', ('127.0.0.1', 80))]
        def resolve(*args):
            calls.append(args)
            entered.set()
            release.wait(3)
            return addresses
        def lookup():
            try:
                results.append(C._plug_addresses('plug.invalid', 80, C.socket.AF_UNSPEC))
            except C.socket.timeout:
                results.append('timeout')
        first, second = threading.Thread(target=lookup), threading.Thread(target=lookup)
        with patch.object(C.socket, 'getaddrinfo', resolve):
            try:
                first.start()
                assert entered.wait(1)
                if expires:
                    first.join(1)
                    assert not first.is_alive()
                if expires:
                    second.start()
                    second.join(1)
                    assert not second.is_alive()
                else:
                    joined = threading.Event()
                    done = next(iter(C._plug_dns_pending.values()))['done']
                    original_wait = done.wait
                    def observe_wait(timeout=None):
                        if threading.current_thread() is second:
                            joined.set()
                        return original_wait(timeout)
                    with patch.object(done, 'wait', observe_wait):
                        second.start()
                        assert joined.wait(1), 'Second caller did not join lookup'
                        release.set()
                        second.join(1)
                    release.set()
                first.join(1)
                second.join(1)
                assert len(calls) == 1, calls
                assert results == (['timeout', 'timeout'] if expires else [addresses, addresses])
            finally:
                release.set()
                first.join(2)
                second.join(2)
                for outcome in C._plug_dns_pending.values():
                    assert outcome['done'].wait(2)


def test_host_changes_cannot_accumulate_unlimited_stalled_workers():
    C = harness.load()
    C.PLUG_DNS_SECONDS = 0.04
    release = threading.Event()
    calls = []
    def resolve(*args):
        calls.append(args[0])
        release.wait(3)
        return []
    with patch.object(C.socket, 'getaddrinfo', resolve):
        try:
            for i in range(5):
                try:
                    C._plug_addresses('plug%d.invalid' % i, 80, C.socket.AF_UNSPEC)
                except C.socket.timeout:
                    pass
                else:
                    raise AssertionError('Stalled lookup unexpectedly succeeded')
            assert len(calls) == 4 and len(C._plug_dns_pending) == 4
        finally:
            release.set()
            for outcome in C._plug_dns_pending.values():
                assert outcome['done'].wait(2)
        assert C._plug_addresses('recovered.invalid', 80, C.socket.AF_UNSPEC) == []


def test_literal_addresses_never_consult_dns():
    C = harness.load()
    with patch.object(C.socket, 'getaddrinfo', side_effect=AssertionError('DNS called')):
        v4 = C._plug_addresses('127.0.0.1', 80, C.socket.AF_UNSPEC)
        v6 = C._plug_addresses('::1', 80, C.socket.AF_UNSPEC)
        assert v4[0][-1] == ('127.0.0.1', 80)
        assert v6[0][-1] == ('::1', 80, 0, 0)
    assert C._plug_dns_pending == {}


def test_worker_start_failure_does_not_poison_future_lookups():
    C = harness.load()
    with patch.object(C.threading.Thread, 'start', side_effect=RuntimeError('no thread')):
        try:
            C._plug_addresses('plug.invalid', 80, C.socket.AF_UNSPEC)
        except OSError:
            pass
        else:
            raise AssertionError('Worker failure was ignored')
    assert C._plug_dns_pending == {}
    with patch.object(C.socket, 'getaddrinfo', return_value=[]):
        assert C._plug_addresses('plug.invalid', 80, C.socket.AF_UNSPEC) == []


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
