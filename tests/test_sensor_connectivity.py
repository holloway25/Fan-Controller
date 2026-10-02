"""Disconnected Protect measurements must not refresh safety timestamps."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    def __init__(self):
        self.commands = []

    def state(self):
        return False

    def set(self, on):
        self.commands.append(on)


def setup(connected=False):
    C = harness.load()
    sensor = {"id": "selected", "airQuality": {"co2": 500}}
    if connected is not None:
        sensor["isConnected"] = connected
    protect = C.Protect("console.example.com", "", "", False)
    protect._get = lambda _: {"sensors": [sensor]}
    plug = Plug()
    C.CFG["protect"]["sensor_id"] = "selected"
    C.CFG["control"].update(mode="auto", co2_enabled=True,
                             temp_enabled=False, humidity_enabled=False,
                             stale_minutes=1)
    C.STATE.update(reading_at=C.time.time() - 120, fan=False, fault=None)
    C._client_snapshot = lambda: (protect, plug, C._cfg_generation)
    C._notify = lambda *args, **kwargs: None
    return C, protect, plug, sensor


def test_disconnected_retained_measurements_do_not_refresh_poll_state():
    C, protect, plug, sensor = setup()
    before = {key: C.STATE[key] for key in
              ("reading_at", "co2", "history", "temperature", "humidity")}
    before["history"] = list(before["history"])
    try:
        C.tick()
    except C.ProtectError as exc:
        assert "disconnected" in str(exc)
    else:
        assert False, "disconnected sensor readings were accepted"
    for key, value in before.items():
        assert C.STATE[key] == value, key
    assert not plug.commands
    C._failsafe()
    assert plug.commands == [True], "stale fail-safe was suppressed"
    assert C.STATE["fan"] is True
    assert C.STATE["reading_at"] == before["reading_at"]


def test_connected_and_legacy_payloads_remain_usable():
    for connectivity in (True, None):
        C, protect, plug, sensor = setup(connectivity)
        old = C.STATE["reading_at"]
        C.tick()
        assert C.STATE["reading_at"] > old
        assert C.STATE["co2"] == 500
        C._failsafe()
        assert not plug.commands, connectivity


def test_disconnected_co2_helper_rejects_retained_measurement():
    C, protect, plug, sensor = setup()
    try:
        protect.co2("selected")
    except C.ProtectError as exc:
        assert "disconnected" in str(exc)
    else:
        assert False, "legacy CO2 helper accepted disconnected readings"
    assert protect.sensor("selected") is sensor, "raw diagnostics must remain available"


def test_poll_loop_records_disconnection_and_runs_failsafe():
    C, protect, plug, sensor = setup()
    before = C.STATE["reading_at"]
    class Finished(Exception):
        pass
    class Wake:
        def wait(self, _):
            raise Finished()
        def clear(self):
            pass
        def set(self):
            pass
    C._wait_for_dns = lambda: True
    C._wake = Wake()
    try:
        C.loop()
    except Finished:
        pass
    assert C.STATE["reading_at"] == before
    assert "disconnected" in C.STATE["error"]
    assert "disconnected" in C.STATE["sensor_error"]
    assert plug.commands == [True]


def test_fresh_reading_remains_healthy_when_plug_command_fails():
    C, protect, plug, sensor = setup(True)
    sensor["airQuality"]["co2"] = 1500
    C.CFG["control"].update(on_at=1000, off_at=740, min_run_minutes=0)
    # A recovered sensor must clear its own error even if actuation fails later.
    C.STATE["sensor_error"] = "Previous sensor outage"
    before = C.STATE["reading_at"]
    def fail(_):
        raise C.PlugError("Plug rejected switching")
    plug.set = fail
    class Finished(Exception):
        pass
    class Wake:
        def wait(self, _):
            raise Finished()
        def clear(self):
            pass
        def set(self):
            pass
    C._wait_for_dns = lambda: True
    C._wake = Wake()
    try:
        C.loop()
    except Finished:
        pass
    assert C.STATE["reading_at"] > before
    assert C.STATE["co2"] == 1500
    assert C.STATE["history"][-1]["v"] == 1500
    assert "Plug rejected" in C.STATE["error"]
    assert C.STATE["sensor_error"] is None
    body = C.app.test_client().get("/api/state").get_json()
    assert body["sensor_error"] is None
    assert "Plug rejected" in body["error"]


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
