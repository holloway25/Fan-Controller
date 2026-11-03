"""Recovery-only update context never changes control or invents an update."""
import os
import sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Client:
    def __init__(self):
        self.apps = [{'name': 'protect', 'version': '7.3.74', 'updateProgress': 100,
                      'updateAvailable': '7.4.0'}]
        self.system = {'mac': 'console', 'hardware': {'firmwareVersion': '6.0.16'}}
        self.sensors = [{'id': 'air', 'firmwareVersion': '1.0.16',
                         'previousFirmwareVersion': '1.0.12', 'isUpdating': False}]
        self.devices = [{'mac': 'ap', 'type': 'uap'}, {'mac': 'switch', 'type': 'usw'}]
        self.rows = []
        self.calls = []

    def _get(self, path, retry=True):
        assert retry is False
        self.calls.append(path)
        if path == '/api/apps':
            return {'controllers': self.apps}
        if path == '/api/system':
            return self.system
        if path.endswith('/bootstrap'):
            return {'sensors': self.sensors}
        if path.endswith('/stat/device'):
            return {'data': self.devices}
        raise AssertionError(path)

    def update_history(self, since, until, page=0):
        self.calls.append(('history', since, until, page))
        return {'data': self.rows, 'total_page_count': 1}


def event(device, stamp=1000000, name='FIRMWARE_UPDATED'):
    return {'category': 'SOFTWARE_UPDATES', 'event': name, 'timestamp': stamp,
            'parameters': {'DEVICE': {'id': device}}}


def test_completed_progress_available_update_and_initial_versions_are_silent():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    assert observer.check() == []
    assert len(client.calls) == 3  # One startup baseline, no history/live AP polling.
    assert observer.check(990, 1010) == []
    client.apps[0]['version'] = '7.4.0'
    assert observer.check(990, 1010) == ['FC-303 UniFi Protect updated']
    assert observer.check(990, 1010) == []
    assert C._UniFiUpdates(client, 'air').check(990, 1010) == []


def test_os_and_selected_air_sensor_versions_are_compared_after_recovery():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    client.system['hardware']['firmwareVersion'] = '6.0.17'
    client.sensors.append({'id': 'other', 'firmwareVersion': '9.0'})
    client.sensors[0]['firmwareVersion'] = '1.0.17'
    assert observer.check(990, 1010) == ['FC-303 UniFi OS updated', 'FC-303 Air Sensor updated']
    assert observer.check(990, 1010) == []


def test_history_is_time_bounded_classified_and_deduplicated():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    client.rows = [event('ap'), event('switch'), event('console'),
                   event('', name='PROTECT_UPDATED'), event('', name='NETWORK_UPDATED'),
                   event('ap'), event('unrelated'), event('ap', name='DEVICE_RESTARTED')]
    assert observer.check(990, 1010) == ['FC-303 UniFi AP updated', 'FC-303 UniFi switch updated',
        'FC-303 UniFi OS updated', 'FC-303 UniFi Protect updated', 'FC-303 UniFi Network updated']
    assert observer.check(990, 1010) == []
    client.rows = [event('ap', stamp=1), event('switch', stamp=2000000)]
    assert observer.check(990, 1010) == []


def test_permission_failures_malformed_records_and_rollback_do_not_affect_control():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    client.rows = [None, event('ap', stamp=True), event('ap', stamp=float('inf')),
                   {'category': 'SOFTWARE_UPDATES', 'timestamp': 1000000, 'parameters': None},
                   event('ap', name={'bad': 'type'})]
    before = dict(C.STATE)
    assert observer.check(990, 1010) == []
    assert observer.check(1010, 990) == []
    def denied(*a, **kw):
        raise C.ProtectError('HTTP 403 private')
    client._get = denied
    client.update_history = denied
    assert observer.check(990, 1010) == []
    assert C.STATE == before


def test_observer_waits_for_recovery_without_live_requests():
    C = harness.load()
    checks = []
    C._UniFiUpdateClient = lambda *a, **kw: object()
    class Observer:
        def __init__(self, *a):
            pass
        def check(self, *args):
            checks.append(args)
            return []
    C._UniFiUpdates = Observer
    def idle_wait(seconds):
        assert seconds == 1
        C._stopping.set()
        return False
    C._unifi_update_event.wait = idle_wait
    C.unifi_updates_loop()
    assert checks == [()]


def test_recoveries_coalesce_and_obsolete_config_is_discarded():
    C = harness.load()
    C.time.time = lambda: 1010
    C._queue_unifi_recovery(995)
    C._queue_unifi_recovery(990)
    assert C._unifi_update_pending[1:] == (990, 1010)
    C.CFG['protect']['host'] = 'replacement.example'
    C._UniFiUpdateClient = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('obsolete request'))
    def idle_wait(seconds):
        C._stopping.set()
        return False
    C._unifi_update_event.wait = idle_wait
    C.unifi_updates_loop()
    assert C._unifi_update_pending is None


def test_recovery_check_logs_current_results_outside_control_locks():
    C = harness.load()
    C._queue_unifi_recovery(990)
    C._UniFiUpdateClient = lambda *a, **kw: object()
    logs = []
    C._log = logs.append
    class Observer:
        def __init__(self, *a):
            pass
        def check(self, *args):
            import threading
            free = []
            def probe():
                assert C._act_lock.acquire(timeout=0.2)
                C._act_lock.release()
                assert C._cfg_lock.acquire(timeout=0.2)
                C._cfg_lock.release()
                free.append(True)
            t = threading.Thread(target=probe)
            t.start()
            t.join(1)
            assert free == [True] and len(args) == 2
            return ['FC-303 UniFi Protect updated']
    C._UniFiUpdates = Observer
    def idle_wait(seconds):
        C._stopping.set()
        return False
    C._unifi_update_event.wait = idle_wait
    C.unifi_updates_loop()
    assert logs == ['FC-303 UniFi Protect updated']


def test_history_transport_is_bounded_and_only_queries_update_records():
    C = harness.load()
    calls = []
    class Session:
        def post(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(status_code=200, json=lambda: {'data': []})
    client = C._UniFiUpdateClient('console.example', 'reader', '')
    client._s = Session()
    client._csrf = 'dummy-token'
    assert client.update_history(990, 1010) == {'data': []}
    url, options = calls[0]
    assert url.endswith('/system-log/all')
    assert options['json'] == {'timestampFrom': 990000, 'timestampTo': 1010000,
        'categories': ['SOFTWARE_UPDATES'], 'pageNumber': 0, 'pageSize': 100}
    assert options['headers']['X-CSRF-Token'] == 'dummy-token'
    assert options['timeout'] == 15 and options['allow_redirects'] is False


def test_update_session_recovers_from_expiry_without_reauthenticating_permission_denials():
    C = harness.load()
    session = SimpleNamespace(hooks={'response': []})
    C.Protect._login_direct = lambda self: setattr(self, '_s', session)
    client = C._UniFiUpdateClient('console.example', 'reader', '')
    client._login_direct()
    hook = session.hooks['response'][0]
    hook(SimpleNamespace(status_code=403))
    assert client._s is session
    hook(SimpleNamespace(status_code=401))
    assert client._s is None
    replacement = object()
    client._s = replacement
    hook(SimpleNamespace(status_code=401))
    assert client._s is replacement


def test_real_poll_and_meter_recovery_queue_context_without_fetching_it():
    C = harness.load()
    C._wait_for_dns = lambda: True
    C._failsafe = lambda: None
    ticks = []
    def tick():
        ticks.append(True)
        if len(ticks) == 1:
            raise C.ProtectError('console restarting')
    C.tick = tick
    class Wake:
        def wait(self, seconds):
            if len(ticks) == 2:
                C._stopping.set()
        def clear(self):
            pass
    C._wake = Wake()
    C.loop()
    assert C._unifi_update_pending is not None and len(ticks) == 2
    assert any(e['msg'] == 'FC-201 Poll recovered' for e in C.STATE['events'])

    C = harness.load()
    C.CFG['control']['mode'] = 'auto'
    reads = []
    def power():
        reads.append(True)
        if len(reads) == 1:
            raise C.PlugError('temporary failure')
        return 23
    C._client_snapshot = lambda: (None, SimpleNamespace(power_w=power), None)
    C._relay_fallback_needed = lambda *a: False
    C._retry_fault_persist = lambda: False
    C._retry_relay_notice = lambda: None
    C._draw_check = lambda *a: None
    def sleep(seconds):
        if len(reads) == 2:
            C._stopping.set()
    C.time.sleep = sleep
    C.watts_loop()
    assert C._unifi_update_pending is not None and len(reads) == 2
    assert any(e['msg'] == 'FC-200 Draw loop recovered' for e in C.STATE['events'])


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
