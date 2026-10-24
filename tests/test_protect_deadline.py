"""Normal requests' DNS time is bounded and cannot hold up Manual enforcement."""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class SlowDNS:
    def __init__(self, C):
        self.C = C
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0
    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        # Never proceed to any actual connection, even after cleanup.
        raise self.C.socket.gaierror('simulated resolver failure')


def test_login_and_bootstrap_bound_actual_requests_dns_and_do_not_stack_workers():
    for operation in ('login', 'bootstrap'):
        C = harness.load()
        C.PROTECT_REQUEST_SECONDS = 0.1
        p, dns = C.Protect('console.invalid', '', ''), SlowDNS(C)
        factory = C.requests.Session
        def session():
            value = factory(); value.trust_env = False
            return value
        if operation == 'bootstrap': p._s = session()
        errors = []
        def request():
            try: p.login() if operation == 'login' else p.sensors()
            except C.ProtectError as error: errors.append(str(error))
        with patch.object(C.requests, 'Session', session), patch.object(C.socket, 'getaddrinfo', dns):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                assert dns.entered.wait(1.5), 'Did not reach real requests DNS path'
                thread.join(1.5)
                assert not thread.is_alive(), 'HTTP timeout did not bound DNS'
                assert errors and 'deadline' in errors[0]
                for _ in range(3):
                    try: p.sensors()
                    except C.ProtectError as error: assert 'in progress' in str(error)
                    else: raise AssertionError('Started another request while DNS was blocked')
                assert dns.calls == 1
                assert C.STATE['reading_at'] is None
            finally:
                C._stopping.set(); dns.release.set(); thread.join(2)
                if p._pending_request: assert p._pending_request['done'].wait(2)


def test_late_success_is_discarded_and_a_fresh_request_can_recover():
    C = harness.load()
    C.PROTECT_REQUEST_SECONDS = 0.1
    p = C.Protect('console.invalid', '', '')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(path, retry=True):
        calls.append(path)
        if len(calls) == 1:
            entered.set(); release.wait(5)
            return {'sensors': [{'id':'obsolete-result'}]}
        return {'sensors': []}
    with patch.object(p, '_get_direct', fetch):
        try:
            try: p.sensors()
            except C.ProtectError: pass
            else: raise AssertionError('Late request was not timed out')
            assert entered.is_set()
            release.set(); assert p._pending_request['done'].wait(2)
            assert C.STATE['reading_at'] is None
            assert p.sensors() == [], 'Late response was returned as a fresh reading'
            assert len(calls) == 2
        finally:
            release.set()


def test_worker_start_failure_does_not_leave_a_permanent_busy_request():
    C = harness.load()
    p = C.Protect('console.invalid', '', '')
    with patch.object(C.threading.Thread, 'start', side_effect=RuntimeError('simulated unavailable thread')):
        try: p.sensors()
        except C.ProtectError as error: assert 'worker' in str(error)
        else: raise AssertionError('Worker failure was not reported')
    assert p._pending_request['done'].is_set()
    with patch.object(p, '_get_direct', return_value={'sensors': []}):
        assert p.sensors() == []


def test_manual_stop_runs_on_meter_thread_while_normal_protect_dns_is_blocked():
    C = harness.load()
    C.CFG['control']['mode'] = 'auto'
    C.CFG['protect'].update(host='console.invalid', sensor_id='example-sensor')
    p, dns = C.Protect('console.invalid', '', ''), SlowDNS(C)
    p._s = C.requests.Session(); p._s.trust_env = False
    class Plug:
        def __init__(self): self.on, self.commands = True, []
        def state(self): return self.on
        def set(self, value): self.on=value; self.commands.append(value)
        def power_w(self): return 23 if self.on else 0
    plug = Plug()
    C._clients = lambda: (p, plug)
    errors = []
    def poll():
        try: C.tick()
        except C.ProtectError as error: errors.append(str(error))
    with patch.object(C.socket, 'getaddrinfo', dns), \
         patch.object(C.time, 'sleep', lambda _: C._stopping.set()):
        polling = threading.Thread(target=poll)
        polling.start()
        metering = None
        try:
            assert dns.entered.wait(1.5)
            response = C.app.test_client().post('/api/config', json={'control': {'mode':'manual'}})
            assert response.status_code == 200, response.data
            assert C.STATE['manual_off_pending']
            metering = threading.Thread(target=C.watts_loop)
            metering.start(); metering.join(1.5)
            assert not metering.is_alive(), 'Manual path waited on Protect session'
            assert plug.commands == [False], 'Manual stop waited for sensor DNS/deadline'
            assert C.STATE['fan'] is False and not C.STATE['manual_off_pending']
            assert not dns.release.is_set() and dns.calls == 1
            assert C.STATE['reading_at'] is None
        finally:
            C._stopping.set(); C._wake.set(); dns.release.set()
            polling.join(2)
            if metering: metering.join(2)
            if p._pending_request: assert p._pending_request['done'].wait(2)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
