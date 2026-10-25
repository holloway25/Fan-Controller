"""SMTP DNS must not consume the web worker pool or send after expiry."""
import http.client
import json
import os
import socketserver
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def configured():
    C = harness.load()
    C.CFG['email'].update(enabled=True, host='smtp.invalid', port=25,
                          username='', password='', sender='fan@example.com',
                          recipients='a@example.com')
    return C


class Session:
    sock = None
    def __init__(self): self.calls = []
    def ehlo(self): self.calls.append('ehlo')
    def has_extn(self, name): return False
    def sendmail(self, *args): self.calls.append('send'); return {}
    def quit(self): self.calls.append('quit')
    def close(self): self.calls.append('close')


def request(port, path, body=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        conn.request('GET' if body is None else 'POST', path,
                     body=None if body is None else json.dumps(body),
                     headers={'Content-Type':'application/json'})
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        conn.close()


def test_smtp_dns_timeout_and_repeat_clicks_do_not_block_waitress_or_send_late():
    C = configured()
    C.EMAIL_TEST_SECONDS = 2.5
    entered, release, smtp_closed = (threading.Event() for _ in range(3))
    commands, dns_calls = [], []
    class SMTPHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                self.wfile.write(b'220 fixture ready\r\n')
                self.wfile.flush()
                for line in self.rfile:
                    commands.append(line.decode('ascii', errors='replace'))
                    self.wfile.write(b'250 OK\r\n')
                    self.wfile.flush()
            finally:
                smtp_closed.set()
    smtp = socketserver.ThreadingTCPServer(('127.0.0.1', 0), SMTPHandler)
    smtp.daemon_threads = True
    C.CFG['email']['port'] = smtp.server_address[1]
    smtp_serving = threading.Thread(target=smtp.serve_forever, daemon=True)
    smtp_serving.start()
    web, channels, dispatcher = C._make_http_server('127.0.0.1', 0)
    web_serving = threading.Thread(target=web.run, daemon=True)
    web_serving.start()
    port = int(web.effective_port)
    original_dns, original_smtp = C.socket.getaddrinfo, C.smtplib.SMTP
    def dns(host, *args):
        if host != 'smtp.invalid': return original_dns(host, *args)
        dns_calls.append(host)
        entered.set()
        release.wait(10)
        return [(C.socket.AF_INET, C.socket.SOCK_STREAM, 0, '', smtp.server_address)]
    replies, errors = [], []
    busy = threading.Event()
    def send():
        try:
            reply = request(port, '/api/email/test', {})
            replies.append(reply)
            if sum(code == 409 for code, _ in replies) == 3: busy.set()
        except Exception as exc: errors.append(exc)
    workers = [threading.Thread(target=send) for _ in range(4)]
    try:
        with patch.object(C.socket, 'getaddrinfo', dns), \
             patch.object(C.smtplib, 'SMTP', lambda *args, **kw:
                          original_smtp(*args, local_hostname='fixture.invalid', **kw)):
            workers[0].start()
            assert entered.wait(1)
            for worker in workers[1:]: worker.start()
            assert busy.wait(1), 'Repeated email tests occupied web workers'
            assert request(port, '/healthz')[0] == 200
            assert request(port, '/api/state')[0] == 200
            assert request(port, '/api/config', {'control':{'mode':'auto'}})[0] == 200
            for worker in workers: worker.join(4)
            assert not errors and all(not worker.is_alive() for worker in workers), errors
            assert sorted(code for code, _ in replies) == [409,409,409,504], replies
            assert not release.is_set() and dns_calls == ['smtp.invalid']
            assert request(port, '/api/email/test', {})[0] == 409, 'Timed-out worker was duplicated'
            release.set()
            assert C._email_test_pending['done'].wait(2)
            assert smtp_closed.wait(2)
            assert commands == [], 'Late DNS result sent SMTP commands: '+repr(commands)
        # Completion frees the slot, and the next test uses a fresh worker.
        session = Session()
        with patch.object(C.smtplib, 'SMTP', return_value=session):
            status, reply = request(port, '/api/email/test', {})
            assert status == 200 and reply['sent_to'] == ['a@example.com'], reply
            assert 'send' in session.calls
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None: worker.join(5)
        if C._email_test_pending is not None:
            C._email_test_pending['done'].wait(3)
        C._close_http_server(web, channels, dispatcher)
        web_serving.join(3)
        smtp.shutdown(); smtp.server_close(); smtp_serving.join(3)


def test_email_test_stopping_abandons_the_wait_and_cancels_after_connect():
    C = configured()
    entered, release = threading.Event(), threading.Event()
    session, responses = Session(), []
    def connect(*args, **kw):
        entered.set()
        release.wait(3)
        return session
    def send():
        responses.append(C.app.test_client().post('/api/email/test', json={}))
    worker = threading.Thread(target=send)
    with patch.object(C.smtplib, 'SMTP', connect):
        try:
            worker.start()
            assert entered.wait(1)
            C._stopping.set()
            worker.join(1)
            assert not worker.is_alive() and responses[0].status_code == 503
        finally:
            release.set(); worker.join(3)
            assert C._email_test_pending['done'].wait(2)
    assert session.calls == ['close'], session.calls


def test_email_test_thread_failure_releases_slot_for_retry():
    C = configured()
    with patch.object(C.threading.Thread, 'start', side_effect=RuntimeError('no thread')):
        reply = C.app.test_client().post('/api/email/test', json={})
    assert reply.status_code == 503 and C._email_test_pending is None
    session = Session()
    with patch.object(C.smtplib, 'SMTP', return_value=session):
        reply = C.app.test_client().post('/api/email/test', json={})
    assert reply.status_code == 200 and 'send' in session.calls


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
