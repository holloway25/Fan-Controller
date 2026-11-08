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
        self.system = {'mac': 'console', 'hardware': {'firmwareVersion': '6.0.16'}, 'uptime': 50000}
        self.nvr = {'upSince': 800000}
        self.sensors = [{'id': 'air', 'firmwareVersion': '1.0.16',
                         'previousFirmwareVersion': '1.0.12', 'isUpdating': False,
                         'uptime': 50000, 'isConnected': True}]
        self.devices = [{'mac': 'ap', 'type': 'uap', 'state': 1, 'uptime': 50000},
                        {'mac': 'switch', 'type': 'usw', 'state': 1, 'uptime': 50000}]
        self.rows = []
        self.protect_rows = []
        self.calls = []

    def _get(self, path, retry=True):
        assert retry is False
        self.calls.append(path)
        if path == '/api/apps':
            return {'controllers': self.apps}
        if path == '/api/system':
            return self.system
        if path.endswith('/bootstrap'):
            return {'sensors': self.sensors, 'nvr': self.nvr}
        if path.endswith('/stat/device'):
            return {'data': self.devices}
        raise AssertionError(path)

    def update_history(self, since, until, page=0):
        self.calls.append(('history', since, until, page))
        return {'data': self.rows, 'total_page_count': 1}

    def protect_history(self, since, until, page=0):
        self.calls.append(('protect-history', since, until, page))
        return self.protect_rows


def event(device, stamp=1000000, name='FIRMWARE_UPDATED'):
    return {'category': 'SOFTWARE_UPDATES', 'event': name, 'timestamp': stamp,
            'parameters': {'DEVICE': {'id': device}}}


def protect_event(kind, end=1000000, sensor='air'):
    metadata = {'toVersion': {'text': '1.0.17'}}
    if kind == 'update':
        metadata.update(deviceModelKey='sensor', deviceId={'text': sensor})
    return {'type': kind, 'start': end-10000, 'end': end, 'metadata': metadata,
            'description': {'eventMetadata': {'title': 'Protect Updated'}}}


def test_completed_progress_available_update_and_initial_versions_are_silent():
    C = harness.load()
    C.time.time = lambda: 1010
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    assert observer.check() == []
    assert len(client.calls) == 4  # One startup baseline, no history/live polling.
    assert observer.check(990, 1010) == []
    client.apps[0]['version'] = '7.4.0'
    assert observer.check(990, 1010) == []  # An undated version change is not current context.
    client.protect_rows = [protect_event('applicationUpdate')]
    assert observer.check(990, 1010) == ['FC-303 UniFi Protect updated']
    assert observer.check(990, 1010) == []
    client.protect_rows = []
    assert C._UniFiUpdates(client, 'air').check(990, 1010) == []
    assert C._UniFiUpdates(client, 'air').check(1010, 990) == []


def test_os_and_selected_air_sensor_versions_are_compared_after_recovery():
    C = harness.load()
    C.time.time = lambda: 1010
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    client.system['hardware']['firmwareVersion'] = '6.0.17'
    client.sensors.append({'id': 'other', 'firmwareVersion': '9.0'})
    client.sensors[0]['firmwareVersion'] = '1.0.17'
    assert observer.check(990, 1010) == []
    client.protect_rows = [protect_event('fwUpdate'), protect_event('update')]
    assert observer.check(990, 1010) == ['FC-303 UniFi OS updated', 'FC-303 Air Sensor updated']
    assert observer.check(990, 1010) == []


def test_clock_rollback_preserves_version_and_restart_evidence_without_history():
    C = harness.load()
    C.time.time = lambda: 1010
    C.time.monotonic = lambda: 100
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    C.time.monotonic = lambda: 200
    client.apps[0]['version'] = '7.4.0'
    client.system['uptime'] += 100
    client.sensors[0]['uptime'] += 100
    for row in client.devices:
        row['uptime'] += 100
    client.devices[0]['uptime'] = 1
    assert observer.check(1010, 990) == ['FC-303 UniFi Protect updated', 'FC-304 UniFi AP restarted']
    assert not any(isinstance(call, tuple) and call[0] == 'history' for call in client.calls)
    assert observer.check(990, 1010) == []  # Evidence already delivered, not silently lost.


def test_all_five_restart_indicators_are_reported_once_without_an_update():
    C = harness.load()
    C.time.time = lambda: 1010
    C.time.monotonic = lambda: 100
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    assert observer.check() == []
    C.time.monotonic = lambda: 200
    client.system['uptime'] = 30
    client.sensors[0]['uptime'] = 30
    client.nvr['upSince'] += 100000
    for row in client.devices:
        row['uptime'] = 30
    messages = observer.check(990, 1010)
    assert set(messages) == {'FC-304 UniFi OS restarted', 'FC-304 Air Sensor restarted',
        'FC-304 UniFi Protect restarted', 'FC-304 UniFi AP restarted', 'FC-304 UniFi switch restarted'}
    assert observer.check(990, 1010) == []


def test_reconnects_offline_uptime_clock_changes_and_jitter_are_not_restarts():
    C = harness.load()
    C.time.time = lambda: 1010
    C.time.monotonic = lambda: 100
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    C.time.monotonic = lambda: 110
    C.time.time = lambda: 1  # Host wall-clock change is irrelevant to uptime.
    client.system['uptime'] += 10
    client.sensors[0].update(uptime=50010, connectedSince=999999)
    for row in client.devices:
        row['uptime'] += 10
    assert observer.check(990, 1010) == []
    C.time.monotonic = lambda: 500
    client.system['uptime'] = 50400
    client.sensors[0]['isConnected'] = False
    for row in client.devices:
        row['state'] = 0
    assert observer.check(990, 1010) == []  # Retained OFFLINE counters are not live evidence.
    C.time.monotonic = lambda: 510
    client.system['uptime'] = 50410
    client.sensors[0].update(isConnected=True, uptime=50409)
    for row in client.devices:
        row.update(state=1, uptime=50409)
    assert observer.check(990, 1010) == []
    assert observer._uptime('junk', 'UniFi AP', True) == []
    assert observer._boot_time('junk', 'UniFi Protect', float('inf')) == []


def test_restart_is_detected_even_when_new_uptime_exceeds_previous_sample():
    C = harness.load()
    C.time.time = lambda: 1010
    observer = C._UniFiUpdates(Client(), 'air')
    C.time.monotonic = lambda: 100
    assert observer._uptime('ap', 'UniFi AP', 10) == []
    C.time.monotonic = lambda: 400
    assert observer._uptime('ap', 'UniFi AP', 20) == ['FC-304 UniFi AP restarted']
    assert observer._uptime('ap', 'UniFi AP', 20) == []


def test_history_is_time_bounded_classified_and_deduplicated():
    C = harness.load()
    C.time.time = lambda: 1010
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


def test_ap_recovery_does_not_replay_delayed_os_and_protect_evidence():
    C = harness.load()
    clock = {'wall': 10000, 'mono': 100}
    C.time.time = lambda: clock['wall']
    C.time.monotonic = lambda: clock['mono']
    client = Client()
    client.system['uptime'] = 500
    client.nvr['upSince'] = 9500000
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    # At first recovery, retained snapshots still describe the previous boot.
    clock.update(wall=11000, mono=1100)
    client.system['uptime'] = 1500
    client.sensors[0]['uptime'] += 1000
    for row in client.devices:
        row['uptime'] += 1000
    client.rows = [event('console', stamp=10980000)]
    assert observer.check(10900, 11000) == ['FC-303 UniFi OS updated']
    # Much later, the new OS/Protect boot finally appears in the snapshots,
    # alongside a real AP restart/update during this second interruption.
    clock.update(wall=20000, mono=10100)
    client.system.update(uptime=9050, hardware={'firmwareVersion': '6.0.17'})
    client.nvr['upSince'] = 10950000
    client.apps[0]['version'] = '7.4.0'
    client.sensors[0]['uptime'] += 9000
    client.devices[0]['uptime'] = 20
    client.devices[1]['uptime'] += 9000
    client.rows.append(event('ap', stamp=19995000))
    client.protect_rows = [protect_event('fwUpdate', end=10980000),
                           protect_event('applicationUpdate', end=10980000)]
    assert observer.check(19900, 20000) == ['FC-304 UniFi AP restarted', 'FC-303 UniFi AP updated']
    assert observer.check(19900, 20000) == []


def test_protect_history_requires_completed_selected_device_updates():
    C = harness.load()
    C.time.time = lambda: 1010
    client = Client()
    observer = C._UniFiUpdates(client, 'air')
    observer.check()
    pending = protect_event('update')
    pending['end'] = None
    bad_time = protect_event('applicationUpdate')
    bad_time['end'] = True
    unrelated = protect_event('update')
    unrelated['metadata']['deviceModelKey'] = 'camera'
    client.protect_rows = [protect_event('update', sensor='other'), pending, bad_time,
                           unrelated, protect_event('applicationUpdate', end=1),
                           protect_event('fwUpdate', end=2000000)]
    assert observer.check(990, 1010) == []
    client.protect_rows.append(protect_event('update'))
    assert observer.check(990, 1010) == ['FC-303 Air Sensor updated']
    assert observer.check(990, 1010) == []


def test_permission_failures_malformed_records_and_rollback_do_not_affect_control():
    C = harness.load()
    C.time.time = lambda: 1010
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
    C.time.time = lambda: 1010
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


def test_stalled_recovery_activity_does_not_block_control():
    import threading
    C = harness.load()
    C.time.time = lambda: 1010
    C._queue_unifi_recovery(990)
    C._UniFiUpdateClient = lambda *a, **kw: object()
    entered, release, control_done = (threading.Event() for _ in range(3))
    save = C.save_events

    def stall(snapshot):
        entered.set()
        assert release.wait(5)
        save(snapshot)

    class Observer:
        def __init__(self, *args):
            pass

        def check(self, *args):
            return ['FC-303 UniFi Protect updated']

    def idle_wait(seconds):
        C._stopping.set()
        return False

    def control():
        C._client_snapshot()
        C._log('Fan control event')
        control_done.set()

    C.save_events = stall
    C._UniFiUpdates = Observer
    C._unifi_update_event.wait = idle_wait
    worker = threading.Thread(target=C.unifi_updates_loop)
    tick = threading.Thread(target=control)
    worker.start()
    try:
        assert entered.wait(2)
        tick.start()
        assert control_done.wait(2)
    finally:
        release.set()
        worker.join(5)
        if tick.ident is not None:
            tick.join(5)
    assert not worker.is_alive() and not tick.is_alive()
    assert [e['msg'] for e in C.load_events()][:2] == [
        'Fan control event', 'FC-303 UniFi Protect updated']



def test_history_transport_is_bounded_and_only_queries_update_records():
    C = harness.load()
    C.time.time = lambda: 1010
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


def test_protect_history_transport_is_filtered_paginated_and_bounded():
    C = harness.load()
    calls = []
    class Session:
        def get(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(status_code=200, json=lambda: [])
    client = C._UniFiUpdateClient('console.example', 'reader', '')
    client._s = Session()
    assert client.protect_history(10000, 11000, 1) == []
    url, options = calls[0]
    assert url.endswith('/proxy/protect/api/events')
    assert options['params'] == {'start': 6400000, 'end': 11000000,
        'types[]': ['applicationUpdate', 'fwUpdate', 'update'],
        'orderDirection': 'desc', 'limit': 100, 'offset': 100}
    assert options['timeout'] == 15 and options['allow_redirects'] is False


def test_update_session_recovers_from_expiry_without_reauthenticating_permission_denials():
    C = harness.load()
    C.time.time = lambda: 1010
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
    C.time.time = lambda: 1010
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
    C.time.time = lambda: 1010
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
