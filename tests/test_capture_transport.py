"""Manual fixture reads never forward credentials through proxies or redirects."""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import capture_plug_fixture as capture


def test_fixture_capture_bypasses_environment_proxies_and_refuses_redirects():
    proxy_calls, direct_calls = [], []
    redirect = [False]
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            proxy_calls.append(self.path)
            self.send_response(502)
            self.end_headers()
        def log_message(self, *args): pass
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    class Plug(BaseHTTPRequestHandler):
        def do_GET(self):
            direct_calls.append(self.path)
            if redirect[0]:
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:%d/redirect' % proxy.server_port)
                self.end_headers()
                return
            body = b'{"POWER":"ON"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    plug = ThreadingHTTPServer(('127.0.0.1', 0), Plug)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (proxy, plug)]
    for thread in threads: thread.start()
    proxy_url = 'http://127.0.0.1:%d' % proxy.server_port
    try:
        with patch.dict(os.environ, HTTP_PROXY=proxy_url, http_proxy=proxy_url,
                        ALL_PROXY=proxy_url, all_proxy=proxy_url, NO_PROXY='', no_proxy=''):
            try:
                reply = capture.ask('127.0.0.1:%d' % plug.server_port,
                                    'test-user', 'CHANGEME', 'Power')
            except SystemExit as exc:
                raise AssertionError('Direct capture failed; proxy calls: %d' % len(proxy_calls)) from exc
            assert reply == {'POWER': 'ON'}
            assert not proxy_calls, 'Credential-bearing request reached environment proxy'
            query = parse_qs(urlsplit(direct_calls[0]).query)
            assert query['cmnd'] == ['Power'] and query['user'] == ['test-user']
            assert query['password'] == ['CHANGEME']
            assert set(query) == {'cmnd', 'user', 'password'}
            redirect[0] = True
            try:
                capture.ask('127.0.0.1:%d' % plug.server_port,
                            'test-user', 'CHANGEME', 'Status 8')
            except SystemExit as exc:
                assert str(exc) == 'plug returned HTTP 302'
            else:
                raise AssertionError('Capture followed or accepted redirect')
            assert not proxy_calls
            assert len(direct_calls) == 2
    finally:
        for server in (proxy, plug):
            server.shutdown()
            server.server_close()
        for thread in threads: thread.join(2)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
