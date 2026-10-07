"""Exercise the fake plug through the real controller HTTP transport."""
import importlib.util
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness

spec = importlib.util.spec_from_file_location("plug_emulator", pathlib.Path(__file__).resolve().parents[1] / "tools/plug_emulator.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)


def with_server(check):
    server = E.EmulatorServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        check(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_transport_switches_and_reads_adjustable_load():
    C = harness.load()
    def check(server):
        server.state.update(dict(fan_profile=False))
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        assert plug.state() is False and plug.power_w() == 0
        assert plug.set(True) is True and plug.power_w() == 23
        server.state.update(dict(on_watts=100))
        assert plug.power_w() == 100
        server.state.update(dict(relay=False))
        assert plug.state() is False
        server.state.update(dict(off_watts=3))
        assert plug.power_w() == 3
        assert server.state.snapshot()["requests"] == dict(relay=2, power=4, switch=1)
    with_server(check)


def test_failures_are_targeted_and_rejected_switch_does_not_change_relay():
    C = harness.load()
    def check(server):
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        for failure in ("http_error", "malformed", "reject_switch"):
            server.state.update(dict(failure=failure, target="switch"))
            try:
                plug.set(True)
            except C.PlugError:
                pass
            else:
                raise AssertionError("Failed switch accepted")
            assert plug.state() is False and plug.power_w() == 0
        server.state.update(dict(failure="none"))
        assert plug.set(True) is True
    with_server(check)


def test_controls_validate_atomically_and_reject_cross_origin_forms():
    def check(server):
        url = "http://127.0.0.1:%d/api/emulator" % server.server_port
        for body in (dict(relay=True, on_watts=-1), dict(on_watts=float("nan")),
                     dict(on_watts=10**400), dict(relay=1), dict(unknown=1)):
            request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(request)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            else:
                raise AssertionError("Invalid settings accepted")
            assert server.state.snapshot()["relay"] is False
        for headers, status in (({"Content-Type": "application/json", "Origin": "http://untrusted.example"}, 403),
                                ({"Content-Type": "application/x-www-form-urlencoded"}, 415)):
            try:
                urllib.request.urlopen(urllib.request.Request(url, data=b'{"relay":true}', headers=headers))
            except urllib.error.HTTPError as exc:
                assert exc.code == status
            else:
                raise AssertionError("Unsafe control accepted")
        assert server.state.snapshot()["relay"] is False
        request = urllib.request.Request(url, data=b'{"on_watts":19}', headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["on_watts"] == 19
    with_server(check)
    try:
        E.EmulatorServer(("0.0.0.0", 0))
    except ValueError:
        pass
    else:
        raise AssertionError("Emulator exposed outside loopback")


def test_delay_targets_only_selected_operations_and_browser_switches_are_protected():
    C = harness.load()
    def check(server):
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        server.state.update(dict(delay_seconds=0.06, target="power"))
        started = time.monotonic()
        assert plug.power_w() == 0
        assert time.monotonic() - started >= 0.06
        for path, headers in (("/cm?cmnd=Power+ON", {"Origin":"http://untrusted.example"}),
                              ("/cm?cmnd=Power+ON", {"Referer":"http://untrusted.example/page"}),
                              ("/api/emulator", {"Host":"untrusted.example"})):
            request = urllib.request.Request("http://127.0.0.1:%d%s" % (server.server_port, path), headers=headers)
            try:
                urllib.request.urlopen(request)
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            else:
                raise AssertionError("Cross-origin/DNS-rebinding request accepted")
        assert plug.state() is False
    with_server(check)


def test_fan_profile_starts_runs_and_warms_without_restarting_on_repeated_commands():
    C = harness.load()
    def check(server):
        now = [100.0]
        server.state = E.PlugState(clock=lambda: now[0])
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        assert plug.power_w() == 0
        assert plug.set(True) is True
        assert plug.power_w() == 27 and server.state.snapshot()["phase"] == "startup"
        now[0] += 1.5
        assert plug.power_w() == 25
        now[0] += 1.5
        assert plug.power_w() == 23 and server.state.snapshot()["phase"] == "running"
        now[0] += 900
        assert plug.set(True) is True  # Reasserting ON is not a new motor start.
        server.state.update(dict(relay=True))  # Nor is saving the same relay state.
        assert plug.power_w() == 23
        now[0] += 900
        assert plug.power_w() == 22 and server.state.snapshot()["phase"] == "warmed"
        server.state.update(dict(on_watts=24))
        assert server.state.snapshot()["run_seconds"] == 1803
        server.state.update(dict(fan_profile=False))
        assert plug.power_w() == 24
        server.state.update(dict(fan_profile=True, startup_watts=55, startup_seconds=2,
                                 warm_watts=21, warm_after_minutes=1))
        assert plug.set(False) is False and plug.power_w() == 0
        assert plug.set(True) is True and plug.power_w() == 55
        now[0] += 2
        assert plug.power_w() == 24
        now[0] += 60
        assert plug.power_w() == 21
        server.state.update(dict(warm_watts=20))
        assert plug.power_w() == 20
        server.state.update(dict(relay=False))
        server.state.update(dict(relay=True))  # External relay start also gets the profile.
        assert plug.power_w() == 55
    with_server(check)


def test_profile_settings_reject_invalid_patches_atomically():
    state = E.PlugState()
    before = state.snapshot()
    for patch in (dict(relay=True, startup_seconds=61), dict(fan_profile=1),
                  dict(warm_after_minutes=-1), dict(warm_watts=float("inf")),
                  dict(startup_watts=10**400), dict(on_watts=23.6)):
        try:
            state.update(patch)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid profile accepted")
        assert state.snapshot() == before
    state.update(dict(relay=True, startup_seconds=0, warm_after_minutes=0))
    assert state.snapshot()["current_watts"] == 22


def test_emulated_meter_and_controls_report_integer_watts():
    def check(server):
        server.state.update(dict(fan_profile=False, relay=True, on_watts=23.0))
        base = "http://127.0.0.1:%d" % server.server_port
        with urllib.request.urlopen(base + "/cm?cmnd=Status+8") as response:
            assert type(json.load(response)["StatusSNS"]["ENERGY"]["Power"]) is int
        with urllib.request.urlopen(base + "/api/emulator") as response:
            settings = json.load(response)
            for key in ("current_watts", "on_watts", "off_watts", "startup_watts", "warm_watts"):
                assert type(settings[key]) is int, key
    with_server(check)


def test_timeout_and_connection_drop_leave_switch_unchanged_and_recover():
    C = harness.load()
    def check(server):
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        # Keep this check fast while using the real requests/parser/error path.
        C._plug_http_get = lambda url, params, timeout: C.requests.get(
            url, params=params, timeout=(0.1, 0.03))
        for failure, reason in (("timeout", "ReadTimeout"), ("drop_connection", "ConnectionError")):
            server.state.update(dict(failure=failure, target="switch", timeout_seconds=0.1))
            try:
                plug.set(True)
            except C.PlugError as exc:
                assert reason in str(exc)
            else:
                raise AssertionError("Simulated connection failure was accepted")
            assert plug.state() is False and plug.power_w() == 0
            with urllib.request.urlopen("http://127.0.0.1:%d/api/emulator" % server.server_port) as r:
                assert json.load(r)['relay'] is False
        time.sleep(0.12)  # The timed-out handler must not actuate later.
        assert server.state.snapshot()['relay'] is False
        server.state.update(dict(failure="none"))
        assert plug.set(True) is True
    with_server(check)


def test_brief_outage_expires_without_changing_relay_or_extending_on_unrelated_save():
    C = harness.load()
    def check(server):
        now = [100.0]
        server.state = E.PlugState(clock=lambda: now[0])
        server.state.update(dict(relay=True, failure="outage", target="all", outage_seconds=30))
        plug = C.Tasmota("127.0.0.1:%d" % server.server_port)
        for operation in (plug.state, plug.power_w, lambda: plug.set(False)):
            try:
                operation()
            except C.PlugError:
                pass
            else:
                raise AssertionError("Request succeeded during outage")
        now[0] += 29
        server.state.update(dict(on_watts=24))
        assert server.state.snapshot()['outage_remaining_seconds'] == 1
        with urllib.request.urlopen("http://127.0.0.1:%d/api/emulator" % server.server_port) as r:
            assert json.load(r)['active_failure'] == 'outage'
        now[0] += 1
        assert server.state.snapshot()['active_failure'] == 'none'
        assert plug.state() is True and plug.power_w() == 24
        assert plug.set(False) is False
        server.state.update(dict(failure='outage', target='power', outage_seconds=10))
        assert plug.state() is False
        try:
            plug.power_w()
        except C.PlugError:
            pass
        else:
            raise AssertionError('Targeted outage did not fail meter read')
        server.state.update(dict(failure='none'))
        assert plug.power_w() == 0
    with_server(check)


def test_network_fault_settings_reject_invalid_values_atomically():
    state = E.PlugState()
    for body in (dict(relay=True, timeout_seconds=0), dict(outage_seconds=601),
                 dict(timeout_seconds=True), dict(outage_seconds=float('inf')),
                 dict(timeout_seconds=10**400), dict(failure='unknown')):
        before = state.snapshot()
        try:
            state.update(body)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid network simulation setting accepted')
        assert state.snapshot() == before


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
