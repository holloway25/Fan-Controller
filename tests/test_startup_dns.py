"""A blocking libc resolver must not delay startup controls or its deadline."""
import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def configured(mode='auto'):
    C = harness.load()
    C.CFG['control']['mode'] = mode
    C.CFG['protect'].update(host='console.invalid', sensor_id='example-sensor')
    return C


class SlowResolver:
    def __init__(self):
        self.entered, self.release, self.finished = (threading.Event() for _ in range(3))
        self.calls = []

    def __call__(self, host, *args, **kwargs):
        self.calls.append(host)
        self.entered.set()
        self.release.wait(5)
        self.finished.set()
        return [(2, 1, 6, '', ('192.0.2.1', 0))]


def test_slow_lookup_obeys_deadline_without_spawning_retries():
    C, resolver = configured(), SlowResolver()
    results = []
    with patch.object(C.socket, 'getaddrinfo', resolver):
        thread = threading.Thread(target=lambda: results.append(C._wait_for_dns(timeout=0.35, interval=0.01)))
        thread.start()
        try:
            assert resolver.entered.wait(1.5)
            thread.join(1.5)
            assert not thread.is_alive(), 'Deadline waited for blocking resolver'
            assert results == [False]
            assert resolver.calls == ['console.invalid'], 'Overlapping startup lookups'
            assert not resolver.release.is_set()
        finally:
            C._stopping.set(); C._wake.set(); resolver.release.set()
            thread.join(2)
            resolver.finished.wait(2)
    assert results == [False], 'Late successful lookup changed the result'


def test_settings_and_shutdown_interrupt_a_blocked_lookup():
    for action in ('settings', 'shutdown'):
        C, resolver = configured(), SlowResolver()
        results = []
        with patch.object(C.socket, 'getaddrinfo', resolver):
            thread = threading.Thread(target=lambda: results.append(C._wait_for_dns()))
            thread.start()
            try:
                assert resolver.entered.wait(1.5)
                (C._wake if action == 'settings' else C._stopping).set()
                thread.join(1.5)
                assert not thread.is_alive(), action+' waited for resolver'
                assert results == [False]
                assert not resolver.release.is_set()
            finally:
                C._stopping.set(); C._wake.set(); resolver.release.set()
                thread.join(2)
                resolver.finished.wait(2)


def test_pending_manual_stop_runs_without_waiting_for_startup_dns():
    for initial in ('manual', 'auto'):
        C, resolver = configured(initial), SlowResolver()
        commands = []
        class Plug:
            def state(self): return True
            def set(self, value): commands.append(value)
        class Protect:
            def readings(self, _):
                # End this isolated poll after observing the real Manual path.
                C._stopping.set(); C._wake.set()
                raise C.ProtectError('simulated unavailable sensor')
        C._clients = lambda: (Protect(), Plug())
        if initial == 'manual':
            C.promise_manual_off()
            C._restore_manual_stop_promise()
        with patch.object(C.socket, 'getaddrinfo', resolver):
            thread = threading.Thread(target=C.loop)
            thread.start()
            try:
                if initial == 'auto':
                    assert resolver.entered.wait(1.5)
                    r = C.app.test_client().post('/api/config', json={'control': {'mode': 'manual'}})
                    assert r.status_code == 200, r.data
                thread.join(1.5)
                assert not thread.is_alive(), 'Manual stop blocked behind startup DNS'
                assert commands == [False]
                assert C.STATE['fan'] is False and not C.STATE['manual_off_pending']
                assert not resolver.release.is_set()
                if initial == 'manual':
                    assert resolver.calls == [], 'Manual startup consulted Protect DNS'
            finally:
                C._stopping.set(); C._wake.set(); resolver.release.set()
                thread.join(2)


def test_literal_addresses_do_not_call_the_resolver():
    C = configured()
    with patch.object(C.socket, 'getaddrinfo', side_effect=AssertionError('DNS called')):
        for host in ('192.0.2.1', '2001:db8::1'):
            C.CFG['protect']['host'] = host
            assert C._wait_for_dns() is True


def test_force_on_also_skips_startup_dns():
    C, resolver = configured('on'), SlowResolver()
    results = []
    with patch.object(C.socket, 'getaddrinfo', resolver):
        thread = threading.Thread(target=lambda: results.append(C._wait_for_dns()))
        thread.start()
        try:
            thread.join(1.5)
            assert not thread.is_alive() and results == [False]
            assert resolver.calls == []
        finally:
            C._stopping.set(); C._wake.set(); resolver.release.set()
            thread.join(2)


def test_success_and_failed_lookup_retry_still_work():
    for fail_first in (False, True):
        C = configured()
        calls = []
        def resolver(host, *args):
            calls.append(host)
            if fail_first and len(calls) == 1:
                raise C.socket.gaierror('simulated temporary failure')
            return [(2, 1, 6, '', ('192.0.2.1', 0))]
        with patch.object(C.socket, 'getaddrinfo', resolver):
            assert C._wait_for_dns(timeout=2, interval=0.01) is True
        assert calls == ['console.invalid'] * (2 if fail_first else 1)


def test_fast_startup_dns_is_quiet_but_delays_and_failures_are_reported():
    for delayed in (False, True):
        C = configured()
        messages, calls = [], []
        clock = [100.0]
        C._log = messages.append
        def resolver(*args):
            calls.append(1)
            if delayed and len(calls) == 1:
                clock[0] += 4
                raise C.socket.gaierror('temporary startup outage')
            return []
        with patch.object(C.time, 'monotonic', lambda: clock[0]), \
                patch.object(C.socket, 'getaddrinfo', resolver):
            assert C._wait_for_dns(timeout=30, interval=0)
        if delayed:
            assert len(messages) == 2, messages
            assert 'Waiting for DNS' in messages[0]
            assert 'Resolved console.invalid' in messages[1]
        else:
            assert messages == [], messages
    C = configured()
    messages = []
    C._log = messages.append
    assert C._wait_for_dns(timeout=0) is False
    assert len(messages) == 1 and 'Could not resolve' in messages[0]


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
