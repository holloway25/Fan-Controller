"""Update context must be explicit, optional and independent of fan control."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Client:
    def __init__(self):
        self.apps = [{'name': 'protect', 'version': '7.3.74', 'state': 'active',
                      'updateProgress': 100, 'updateAvailable': '7.4.0'}]
        self.clients = [{'ip': '192.0.2.10', 'ap_mac': 'test-ap'}]
        self.devices = [{'mac': 'test-ap', 'type': 'uap', 'version': '8.8.13', 'state': 1}]
        self.calls = []

    def _get(self, path, retry=True):
        assert retry is False, 'View-only permissions must not trigger repeated logins'
        self.calls.append(path)
        if path == '/api/apps':
            return {'controllers': self.apps}
        if path.endswith('/stat/sta'):
            return {'data': self.clients}
        if path.endswith('/stat/device'):
            return {'data': self.devices}
        raise AssertionError(path)


def test_completed_progress_available_update_and_initial_versions_are_silent():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, '192.0.2.10')
    assert observer.poll() == []
    assert observer.poll() == []
    assert len(client.calls) == 6
    client.apps[0]['version'] = '7.4.0'
    assert observer.poll() == ['FC-303 UniFi Protect updated']
    assert observer.poll() == []


def test_explicit_update_state_is_reported_once_and_version_change_is_separate():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, '192.0.2.10')
    observer.poll()
    client.apps[0]['state'] = 'updating'
    assert observer.poll() == ['FC-303 UniFi Protect update in progress']
    assert observer.poll() == []
    client.apps[0].update(version='7.4.0', state='active')
    assert observer.poll() == ['FC-303 UniFi Protect updated']


def test_only_associated_ap_and_actual_firmware_change_are_reported():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, '192.0.2.10')
    observer.poll()
    client.devices.append({'mac': 'other-ap', 'type': 'uap', 'version': '9.0', 'state': 4})
    client.devices[0]['uptime'] = 1
    assert observer.poll() == []  # A reboot is not proof of an update.
    client.clients = []  # The plug can be absent while its AP updates.
    client.devices[0]['state'] = 4
    assert observer.poll() == ['FC-303 UniFi AP update in progress']
    client.devices[0].update(version='8.9.0', state=1)
    assert observer.poll() == ['FC-303 UniFi AP updated']
    assert observer.poll() == []


def test_permission_failures_and_malformed_payloads_do_not_change_control():
    C = harness.load()
    client = Client()
    client.apps = [None, {'name': 'protect', 'version': 'secret\nvalue'},
                   {'name': 'network', 'version': 123}]
    client.clients = None
    observer = C._UniFiUpdates(client, '192.0.2.10')
    assert observer.poll() == []
    def denied(path, retry=True):
        raise C.ProtectError('HTTP 403 secret')
    client._get = denied
    before = dict(C.STATE)
    assert observer.poll() == []
    assert C.STATE == before


def test_hostname_plugs_do_not_add_a_dns_lookup_or_guess_ap_identity():
    C = harness.load()
    client = Client()
    observer = C._UniFiUpdates(client, 'plug.example')
    assert observer.poll() == []
    assert client.calls == ['/api/apps']


def test_update_session_recovers_from_expiry_without_reauthenticating_permission_denials():
    C = harness.load()
    from types import SimpleNamespace
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


def test_monitor_discards_old_configuration_results_and_stops_without_waiting():
    C = harness.load()
    C.CFG['protect']['host'] = 'old.example'
    logs = []
    C._log = logs.append
    clients = []
    class FakeProtect:
        def __init__(self, *args, **kwargs):
            clients.append(self)
    C._UniFiUpdateClient = FakeProtect
    class Observer:
        def __init__(self, client, host):
            assert client in clients
        def poll(self):
            C.CFG['protect']['host'] = 'new.example'
            return ['FC-303 obsolete update']
    C._UniFiUpdates = Observer
    waits = []
    C._stopping.wait = lambda seconds: waits.append(seconds) or True
    C.unifi_updates_loop()
    assert logs == [] and waits == [60]


def test_monitor_publishes_current_results_without_holding_control_locks_during_io():
    C = harness.load()
    logs = []
    C._log = logs.append
    class FakeProtect:
        def __init__(self, *args, **kwargs):
            pass
    C._UniFiUpdateClient = FakeProtect
    class Observer:
        def __init__(self, client, host):
            pass
        def poll(self):
            import threading
            def probe():
                assert C._cfg_lock.acquire(timeout=0.2)
                C._cfg_lock.release()
                assert C._act_lock.acquire(timeout=0.2)
                C._act_lock.release()
                locks_free.append(True)
            locks_free = []
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(1)
            assert locks_free == [True]
            return ['FC-303 UniFi Protect update in progress']
    C._UniFiUpdates = Observer
    C._stopping.wait = lambda seconds: True
    C.unifi_updates_loop()
    assert logs == ['FC-303 UniFi Protect update in progress']


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
