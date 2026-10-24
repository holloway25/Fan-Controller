"""Proxy access uses the original transport peer, never forwarded headers."""
import os
import sys
import http.client
import threading
from unittest.mock import patch
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def load(allowed='', trust='1'):
    with patch.dict(os.environ, FAN_CONTROLLER_ALLOWED_PROXY_IPS=allowed,
                    FAN_CONTROLLER_TRUST_PROXY=trust):
        return harness.load(plug_access_disabled=True)


def get(C, peer, path='/api/state', **kwargs):
    return C.app.test_client().get(path, environ_overrides={'REMOTE_ADDR': peer}, **kwargs)


def test_unset_preserves_existing_access():
    C = load()
    assert get(C, '198.51.100.20').status_code == 200


def test_only_original_peer_is_allowed_even_with_spoofed_headers():
    for trust in ('0', '1'):
        C = load('192.0.2.20', trust)
        initial_mode = C.CFG['control']['mode']
        spoof = {'X-Forwarded-For': '192.0.2.20', 'X-Real-IP': '192.0.2.20',
                 'Forwarded': 'for=192.0.2.20'}
        for path in ('/', '/api/state', '/api/history', '/static/style.css', '/healthz'):
            r = get(C, '198.51.100.20', path, headers=spoof)
            assert r.status_code == 403, (trust, path, r.status_code)
            assert r.is_json and 'config' not in r.json
        assert get(C, '192.0.2.20', headers={'X-Forwarded-For': '198.51.100.20'}).status_code == 200
        r = C.app.test_client().post('/api/config', json={'control': {'mode': 'on'}},
                                    environ_overrides={'REMOTE_ADDR': '198.51.100.20'}, headers=spoof)
        assert r.status_code == 403
        assert C.CFG['control']['mode'] == initial_mode
        assert get(C, None).status_code == 403
        assert get(C, 'invalid').status_code == 403


def test_loopback_exception_is_health_only_and_auth_independent():
    C = load('192.0.2.20')
    C.CFG['ui']['password_hash'] = C.set_ui_password('example-only')
    for peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        for method in ('GET', 'HEAD'):
            r = C.app.test_client().open('/healthz', method=method,
                                        environ_overrides={'REMOTE_ADDR': peer})
            assert r.status_code == 200
            if method == 'GET':
                assert r.json == {'ok': True}
        assert get(C, peer).status_code == 403
        assert get(C, peer, '/').status_code == 403
        assert C.app.test_client().post('/healthz', environ_overrides={'REMOTE_ADDR': peer}).status_code == 403


def test_ipv4_ipv6_and_mapped_peers_are_exact_and_normalized():
    C = load(' 192.0.2.20, 2001:db8::20 ')
    for peer in ('192.0.2.20', '::ffff:192.0.2.20', '2001:db8:0:0::20'):
        assert get(C, peer).status_code == 200
    for peer in ('192.0.2.21', '2001:db8::21'):
        assert get(C, peer).status_code == 403


def test_invalid_config_fails_closed_at_startup():
    for value in (' ', ',', '192.0.2.20,', 'proxy.example', '*', '0.0.0.0/0',
                  '192.0.2.20,broken', 'fe80::1%eth0'):
        try:
            load(value)
        except ValueError as exc:
            assert 'FAN_CONTROLLER_ALLOWED_PROXY_IPS' in str(exc)
        else:
            raise AssertionError('Invalid allowlist accepted: '+value)


def test_real_socket_cannot_forge_a_proxy_connection():
    if os.name != 'posix':
        harness.skip('Linux loopback source-address integration check')
    C = load('127.0.0.2')
    server = make_server('127.0.0.1', 0, C.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, source='127.0.0.1', headers=None):
        c = http.client.HTTPConnection('127.0.0.1', server.server_port,
                                       timeout=3, source_address=(source, 0))
        try:
            c.request('GET', path, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()
    try:
        assert request('/healthz') == (200, b'{"ok":true}\n')
        assert request('/api/state')[0] == 403
        assert request('/api/state', headers={'X-Forwarded-For': '127.0.0.2'})[0] == 403
        assert request('/api/state', '127.0.0.2', {'X-Forwarded-For': '198.51.100.20'})[0] == 200
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
