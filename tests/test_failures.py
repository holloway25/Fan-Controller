"""Failure handling: persistence errors and malformed request bodies.

Everything here injects a failure rather than waiting for one, so no
disk is filled and no permissions are changed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def boom(*a, **k):
    raise OSError(28, "No space left on device")


def test_unwritable_fault_stays_live_and_is_flagged():
    """A fault that cannot be persisted must not be forgotten.

    save_fault used to swallow every OSError, so the fan was stopped and
    marked faulted in memory while nothing reached disk. A restart then
    reloaded no fault and automatic running resumed on a fan already
    judged faulty.
    """
    C = harness.load()
    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"msg": "19 W outside 15-22 W for 30s", "at": 1})
    finally:
        os.replace = real

    assert C.STATE["fault"] is not None, "fault lost from memory"
    assert C.STATE["fault_unsaved"] is True, "not flagged as unpersisted"
    assert any("could not be saved" in e["msg"] for e in C.STATE["events"]), \
        "no warning logged"
    return "kept live and flagged"


def test_unwritable_fault_is_retried_until_it_lands():
    C = harness.load()
    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"msg": "draw fault", "at": 1})
    finally:
        os.replace = real

    assert C._retry_fault_persist() is True, "retry did not save"
    assert C.STATE["fault_unsaved"] is False
    assert (C.load_fault() or {}).get("msg") == "draw fault", \
        "fault would not survive a restart"
    assert C._retry_fault_persist() is False, "retried with nothing owed"
    return "recovered on retry"


def test_failed_config_write_changes_nothing():
    """A 500 must not leave the controller running on unsaved settings."""
    C = harness.load()
    client = C.app.test_client()
    before = C.CFG["control"]["on_at"]
    C.save_cfg = boom

    r = client.post("/api/config", json={"control": {"on_at": 1025}})

    assert r.status_code == 500, r.status_code
    assert C.CFG["control"]["on_at"] == before, (
        "live settings changed despite a failed write: %r"
        % (C.CFG["control"]["on_at"],))
    return "nothing changed"


def test_failed_password_write_changes_nothing():
    C = harness.load()
    client = C.app.test_client()
    C.CFG["ui"]["password_hash"] = ""
    C.save_cfg = boom

    r = client.post("/api/ui/password", json={"password": "placeholder-not-a-real-password"})

    assert r.status_code == 500, r.status_code
    assert C.CFG["ui"]["password_hash"] == "", \
        "authentication changed in memory only"
    return "nothing changed"


def test_malformed_bodies_are_rejected_not_crashed():
    """Structurally invalid JSON is a client error, not a 500."""
    C = harness.load()
    client = C.app.test_client()
    cases = [
        ("/api/config", {"control": "bad"}),
        ("/api/config", {"plug": [1]}),
        ("/api/config", "bad"),
        ("/api/config", [1, 2]),
        ("/api/ui/password", {"password": 12345678}),
        ("/api/ui/password", "bad"),
        ("/api/clienterror", "bad"),
    ]
    for path, body in cases:
        r = client.post(path, json=body)
        assert r.status_code == 400, "%s %r gave %s" % (
            path, body, r.status_code)
    # Raw bodies, not json=: a truncated "{" parsed to None under
    # get_json(silent=True) and read as an empty request, so the save
    # answered 200 Saved and changed nothing.
    raw = [("/api/config", "{", 400),
           ("/api/config", "null", 400),
           ("/api/config", "[1,2]", 400),
           ("/api/config", "{}", 200)]
    for path, data, want in raw:
        r = client.post(path, data=data, content_type="application/json")
        assert r.status_code == want, "%s %r gave %s, wanted %s" % (
            path, data, r.status_code, want)
    # Right shape, wrong content type, is still a 415.
    assert client.post("/api/config", data="{}").status_code == 415
    return "%d bodies rejected, %d raw checked" % (len(cases), len(raw))

class failing_os(object):
    """Force one os call to fail, and always put it back."""

    def __init__(self, name, err=OSError(28, "No space left on device")):
        self.name, self.err, self.real = name, err, None

    def __enter__(self):
        self.real = getattr(os, self.name)
        def blow(*a, **k):
            raise self.err
        setattr(os, self.name, blow)
        return self

    def __exit__(self, *exc):
        setattr(os, self.name, self.real)
        return False


FAULT = {"msg": "19 W outside 15-22 W for 30s", "at": 1, "stopped": False}


def test_retry_does_not_resurrect_an_acknowledged_fault():
    """The retry snapshotted outside _fault_lock.

    An acknowledgement landing between the snapshot and the write was
    overwritten: memory clear, disk faulted, and the next restart
    brought back a fault the operator had already dismissed.
    """
    C = harness.load()
    with failing_os("replace"):
        C._store_fault(dict(FAULT))
    C._clear_fault()

    assert C._retry_fault_persist() is False, "retried a cleared fault"
    assert C.STATE["fault"] is None
    assert not os.path.exists(C.FAULT_PATH), "cleared fault written back"
    return "stayed cleared"


def test_failed_clear_keeps_the_fault_live():
    """Clearing memory first meant a failed delete still reported gone."""
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("remove"):
        had, ok = C._clear_fault()

    assert ok is False, "reported success on a failed delete"
    assert had is not None
    assert C.STATE["fault"] is not None, "fault dropped from memory"
    assert os.path.exists(C.FAULT_PATH), "file gone but call failed"
    return "fault kept"


def test_failed_clear_endpoint_answers_503():
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("remove"):
        r = C.app.test_client().post("/api/fault/clear")

    assert r.status_code == 503, r.status_code
    assert C.STATE["fault"] is not None, "fault cleared despite the error"
    return "503, fault still live"


def test_failed_stop_confirmation_is_retried():
    """stopped=True in memory before the write made the miss permanent.

    The next call saw it already true and returned early, so disk and
    memory disagreed for good.
    """
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("replace"):
        first = C._confirm_fault_stopped()

    assert first is False, "claimed confirmed after a failed write"
    assert C.STATE["fault"].get("stopped") is not True, \
        "memory moved ahead of disk"
    assert C.STATE["fault_unsaved"] is True, "not flagged for retry"

    assert C._confirm_fault_stopped() is True, "never retried"
    assert json.load(open(C.FAULT_PATH)).get("stopped") is True
    return "retried and landed"


def test_readings_survive_a_payload_without_co2():
    """CO2 can be disabled, so a missing CO2 field is not fatal.

    readings() used to raise, which took temperature and humidity down
    with it and stopped the poll entirely.
    """
    C = harness.load()

    class Stub:
        def sensor(self, sid):
            return {"airQuality": {"temperature": 21.5, "humidity": 72.0}}

    vals, path = C.Protect.readings(Stub(), "x")
    assert vals["co2"] is None and path is None, (vals, path)
    assert vals["temperature"] == 21.5 and vals["humidity"] == 72.0

    c = dict(C.CFG["control"])
    c.update(mode="auto", co2_enabled=False, temp_enabled=False,
             humidity_enabled=True, humidity_on=70.0, humidity_off=60.0,
             min_run_minutes=0)
    latch = {"co2": False, "temp": False, "humidity": False}
    want, why = C.decide(None, None, False, c, None, latch, humidity=72.0)
    assert want is True and "72% RH" in why, (want, why)
    return "humidity drives it with no CO2 present"


def test_poll_fails_only_when_no_enabled_trigger_has_a_reading():
    C = harness.load()
    c = dict(C.CFG["control"])

    def usable(vals, **flags):
        """Mirrors the guard in tick()."""
        c.update(flags)
        live = C.active_rules(c)
        if live:
            return any(vals.get(r["reading"]) is not None for r in live)
        return any(vals.get(k) is not None
                   for k in ("co2", "temperature", "humidity", "pm25"))

    no_co2 = {"co2": None, "temperature": 21.5, "humidity": 72.0}
    assert usable(no_co2, co2_enabled=True, temp_enabled=True,
                  humidity_enabled=False), "temperature alone is enough"
    assert not usable({"co2": None}, co2_enabled=True, temp_enabled=False,
                      humidity_enabled=False), "nothing live had a reading"
    # With NOTHING enabled the fail-safe is the only automatic action
    # left, and it is armed by reading_at going stale. Accepting an
    # empty payload kept reading_at fresh forever, so stale_minutes
    # never expired and the last protection was silently disabled.
    off = dict(co2_enabled=False, temp_enabled=False,
               humidity_enabled=False)
    assert not usable({"co2": None}, **off), \
        "empty payload accepted with no triggers enabled"
    assert usable({"co2": None, "humidity": 52.0}, **off), \
        "a real reading still counts as a live sensor"
    return "judged per enabled rule, and per any metric when none are"

def test_empty_payload_never_refreshes_reading_at():
    """Drives the real tick(), not a copy of its guard.

    With no triggers enabled the fail-safe is the only automatic action
    left, and it is armed by reading_at going stale. Accepting a payload
    with nothing in it kept reading_at fresh forever, so stale_minutes
    never expired and the last protection was silently disabled.
    """
    C = harness.load()

    class Sensor:
        def readings(self, sid):
            return {"co2": None}, None

    class Plug:
        def state(self):
            return False

        def set(self, v):
            return v

        def power_w(self):
            return 0.0

    C._clients = lambda: (Sensor(), Plug())
    C.CFG["protect"]["sensor_id"] = "x"
    C.CFG["control"].update(mode="auto", co2_enabled=False,
                            temp_enabled=False, humidity_enabled=False)
    with C._state_lock:
        C.STATE["reading_at"] = 123.0
        C.STATE["fault"] = None

    raised = None
    try:
        C.tick()
    except C.ProtectError as exc:
        raised = str(exc)

    assert raised, "an empty payload was accepted as a reading"
    assert C.STATE["reading_at"] == 123.0, \
        "reading_at advanced, so stale_minutes would never expire"
    return "fail-safe stays armed"
if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
