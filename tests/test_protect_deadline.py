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


def test_healthy_polling_and_find_sensors_wait_for_separate_fresh_responses():
    C = harness.load()
    C.PROTECT_REQUEST_SECONDS = 3
    C.CFG['control']['mode'] = 'auto'
    C.CFG['protect']['sensor_id'] = 'example-sensor'
    p = C.Protect('console.invalid', '', '')
    entered, release, discovery_checked = (threading.Event() for _ in range(3))
    calls, responses, errors = [], [], []
    real_lock = p._worker_lock
    class WatchedLock:
        def __enter__(self): real_lock.acquire()
        def __exit__(self, *args):
            real_lock.release()
            if threading.current_thread().name == 'discovery': discovery_checked.set()
    p._worker_lock = WatchedLock()
    def fetch(path, retry=True):
        calls.append(path)
        sample = 700 if len(calls) == 1 else 800
        if len(calls) == 1:
            entered.set(); release.wait(5)
        return {'sensors':[{'id':'example-sensor','name':'Example',
                           'isConnected':True,'airQuality':{'co2':sample}}]}
    class Plug:
        def state(self): return False
        def set(self, _): raise AssertionError('No fan demand in this test')
    C._clients = lambda: (p, Plug())
    def poll():
        try: C.tick()
        except Exception as error: errors.append(error)
    def discover():
        try:
            with C.app.test_client() as client:
                responses.append(client.post('/api/protect/sensors', json={}))
        except Exception as error: errors.append(error)
    with patch.object(p, '_get_direct', fetch):
        polling = threading.Thread(target=poll, name='polling')
        discovery = threading.Thread(target=discover, name='discovery')
        polling.start()
        try:
            assert entered.wait(1.5)
            discovery.start()
            assert discovery_checked.wait(1.5), 'Discovery did not reach request admission'
            assert len(calls) == 1, 'Concurrent discovery started another worker'
            release.set()
            polling.join(2); discovery.join(2)
            assert not polling.is_alive() and not discovery.is_alive()
            assert not errors, errors
            assert len(responses) == 1 and responses[0].status_code == 200
            assert responses[0].json['sensors'][0]['co2'] == 800
            assert C.STATE['co2'] == 700 and len(calls) == 2
        finally:
            C._stopping.set(); release.set(); polling.join(2)
            if discovery.ident is not None: discovery.join(2)


def test_waiting_caller_uses_its_original_deadline_without_abandoning_healthy_owner():
    C = harness.load()
    C.PROTECT_REQUEST_SECONDS = 3
    p = C.Protect('console.invalid', '', '')
    entered, release = threading.Event(), threading.Event()
    calls, values, errors = [], [], []
    def fetch(path, retry=True):
        calls.append(path); entered.set(); release.wait(5)
        return {'sensors': []}
    def owner():
        try: values.append(p.sensors())
        except Exception as error: errors.append(error)
    with patch.object(p, '_get_direct', fetch):
        thread = threading.Thread(target=owner)
        thread.start()
        try:
            assert entered.wait(1.5)
            original_deadline = p._pending_request['deadline']
            C.PROTECT_REQUEST_SECONDS = 0.1
            try: p.sensors()
            except C.ProtectError as error: assert 'deadline' in str(error)
            else: raise AssertionError('Waiting caller ignored its deadline')
            assert len(calls) == 1 and not p._pending_request['done'].is_set()
            assert p._pending_request['deadline'] == original_deadline
            release.set(); thread.join(1.5)
            assert values == [[]] and not errors, errors
        finally:
            C._stopping.set(); release.set(); thread.join(2)


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
