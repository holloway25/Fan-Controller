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
        assert plug.power_w() == 22.5
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
                  dict(startup_watts=10**400)):
        try:
            state.update(patch)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid profile accepted")
        assert state.snapshot() == before
    state.update(dict(relay=True, startup_seconds=0, warm_after_minutes=0))
    assert state.snapshot()["current_watts"] == 22


if __name__ == "__main__":
    harness.run(dict(globals()))
