"""Opt-in timing never records secrets or changes request/error semantics."""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from test_plug_requests import Reply


def test_diagnostic_records_are_safe_and_measure_each_request():
    C = harness.load()
    class Log:
        rows = []
        def info(self, value):
            self.rows.append(json.loads(value))
    log = Log()
    C._plug_diagnostics = log
    C._plug_diagnostics_until = C.time.time()+60
    old = C._plug_http_get
    try:
        C._plug_http_get = lambda *a, **kw: Reply()
        assert C.Tasmota('private.example', 'secret-user', 'secret-password').power_w() == 23
        def fail(*a, **kw):
            raise C.requests.ConnectTimeout('secret-password private.example')
        C._plug_http_get = fail
        try:
            C.Tasmota('private.example', '', 'secret-password').state()
        except C.PlugError:
            pass
        else:
            raise AssertionError('Timeout swallowed')
    finally:
        C._plug_http_get = old
    assert [r['phase'] for r in log.rows] == ['queued', 'start', 'ok', 'queued', 'start', 'error']
    assert log.rows[-1]['reason'] == 'ConnectTimeout'
    assert log.rows[1]['wait_s'] >= 0 and log.rows[2]['elapsed_s'] >= 0
    assert len({r['request_id'] for r in log.rows}) == 2
    for secret in ('secret-password', 'secret-user', 'private.example', 'http', 'StatusSNS'):
        assert secret not in json.dumps(log.rows)


def test_expired_or_failed_diagnostics_do_not_change_control():
    C = harness.load()
    class BrokenLog:
        def info(self, value):
            raise OSError('No space')
    old = C._plug_http_get
    C._plug_http_get = lambda *a, **kw: Reply()
    try:
        for until in (C.time.time()-1, C.time.time()+60):
            C._plug_diagnostics = BrokenLog()
            C._plug_diagnostics_until = until
            assert C.Tasmota('192.0.2.10').state() is True
    finally:
        C._plug_http_get = old


def test_local_opt_in_has_private_bounded_files_and_expiry():
    C = harness.load()
    C.time.time = lambda: 1000000.0
    assert C._open_plug_diagnostics() == (None, 0)
    with open(os.path.join(C.DIR, 'plug-diagnostics.enabled'), 'w') as stream:
        json.dump({'until': C.time.time()+1000000}, stream)
    logger, until = C._open_plug_diagnostics()
    assert logger is not None and until == C.time.time()+7*24*3600
    handler = logger.handlers[0]
    assert handler.maxBytes == 1048576 and handler.backupCount == 3
    assert os.stat(handler.baseFilename).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(handler.baseFilename)).st_mode & 0o777 == 0o700
    handler.close()
    requested = C.time.time()+3*24*3600
    with open(os.path.join(C.DIR, 'plug-diagnostics.enabled'), 'w') as stream:
        json.dump({'until': requested}, stream)
    logger, until = C._open_plug_diagnostics()
    assert until == requested
    logger.handlers[0].close()


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
