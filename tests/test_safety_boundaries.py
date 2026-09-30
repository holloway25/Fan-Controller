"""Fail-safe boundaries for persisted state and Protect input.

These tests use only disposable files and stub HTTP replies. They do not
contact Protect, the plug, or either deployed controller.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _configuration_error(text):
    C = harness.load()
    _write(C.CFG_PATH, text)
    try:
        C.load_cfg()
    except C.ConfigurationError as exc:
        assert "refusing to start" in str(exc), exc
        return str(exc)
    except Exception as exc:
        assert False, "escaped as %s instead of ConfigurationError" % type(exc).__name__
    assert False, "unsafe configuration was accepted"


def test_missing_config_fields_receive_upgrade_defaults():
    C = harness.load()
    _write(C.CFG_PATH, json.dumps({"control": {"mode": "manual"}}))
    cfg = C.load_cfg()
    assert cfg["control"]["mode"] == "manual"
    assert cfg["control"]["poll_seconds"] == C.DEFAULTS["control"]["poll_seconds"]
    assert cfg["plug"] == C.DEFAULTS["plug"]
    return "missing fields default without replacing present values"


def test_malformed_config_refuses_startup():
    _configuration_error('{"control":')
    _configuration_error('["not", "an", "object"]')
    return "syntax and top-level shape rejected"


def test_invalid_mode_never_falls_back_to_auto():
    msg = _configuration_error(json.dumps({"control": {"mode": "automatic"}}))
    assert "control.mode" in msg, msg
    return "bad mode named and rejected"


def test_mistyped_safety_setting_never_falls_back_to_default():
    msg = _configuration_error(json.dumps({"control": {"mod": "manual"}}))
    assert "control.mod" in msg, msg
    return "unknown control key named and rejected"


def test_unreadable_config_refuses_startup():
    C = harness.load()
    C.CFG_PATH = C.DIR  # opening a directory is a portable OSError
    try:
        C.load_cfg()
    except C.ConfigurationError as exc:
        assert "refusing to start" in str(exc), exc
    else:
        assert False, "an unreadable config was replaced with defaults"
    return "read failure rejected"


def _assert_startup_fault(C):
    fault = C.load_fault()
    assert isinstance(fault, dict), fault
    assert fault.get("code") == "FC-206", fault
    assert fault.get("stopped") is False, fault
    assert "persisted fault state" in fault.get("msg", ""), fault


def test_damaged_fault_file_fails_closed():
    for text in ('{"msg":', '[]', '{}', '{"msg": 42}'):
        C = harness.load()
        _write(C.FAULT_PATH, text)
        _assert_startup_fault(C)
    return "truncated and wrong-shaped fault state remains latched"


def test_unreadable_fault_file_fails_closed():
    C = harness.load()
    C.FAULT_PATH = C.DIR
    _assert_startup_fault(C)
    return "read failure becomes a startup fault"


def test_absent_fault_file_is_not_a_fault():
    C = harness.load()
    assert not os.path.exists(C.FAULT_PATH)
    assert C.load_fault() is None


def test_non_finite_sensor_values_are_not_numbers():
    C = harness.load()
    for value in (float("nan"), float("inf"), float("-inf")):
        assert C._num(value) is None, value
        assert C._num({"value": value}) is None, value
    return "NaN and infinities rejected at ingestion"


def test_non_finite_reading_never_reaches_history():
    C = harness.load()

    class Sensor:
        def sensor(self, sid):
            return {"id": sid, "airQuality": {"co2": float("nan")}}

        def readings(self, sid):
            return C.Protect.readings(self, sid)

    class Plug:
        def state(self):
            return False

    C.CFG["protect"]["sensor_id"] = "sensor"
    C.CFG["control"].update(mode="auto", co2_enabled=True,
                             temp_enabled=False, humidity_enabled=False)
    C._client_snapshot = lambda: (Sensor(), Plug(), C._cfg_generation)
    before = list(C.STATE["history"])
    try:
        C.tick()
    except C.ProtectError:
        pass
    else:
        assert False, "a NaN reading was accepted by tick()"
    assert C.STATE["history"] == before, C.STATE["history"]
    assert not any(math.isnan(p.get("v")) for p in C.STATE["history"]
                   if isinstance(p, dict) and isinstance(p.get("v"), float))
    return "poll rejected NaN before recording"


def test_metric_matching_is_exact_after_normalisation():
    C = harness.load()
    payload = {"co2Threshold": 650,
               "nested": {"carbon_dioxide": {"value": 812}}}
    assert C.find_metric(payload, C.CO2_HINTS) == (
        812.0, "nested.carbon_dioxide")
    assert C.find_metric({"co2Threshold": 650}, C.CO2_HINTS) is None
    return "metadata cannot beat the live reading"


class _Reply:
    status_code = 200
    content = b"json"

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


def _protect_with_reply(C, value):
    protect = C.Protect("console.invalid", "u", "p", False)
    protect._s = type("Session", (), {
        "get": lambda *args, **kwargs: _Reply(value)})()
    protect._csrf = "token"
    return protect


def test_protect_rejects_non_object_json():
    C = harness.load()
    for value in ([], "ok", 42, None):
        try:
            _protect_with_reply(C, value)._get("/proxy/protect/api/bootstrap")
        except C.ProtectError as exc:
            assert "object" in str(exc), exc
        else:
            assert False, "accepted Protect JSON %r" % (value,)
    return "scalar and list replies classified as Protect faults"


def test_protect_rejects_invalid_sensor_shapes():
    C = harness.load()
    for value in ({"sensors": {}}, {"sensors": ["not-an-object"]}):
        try:
            _protect_with_reply(C, value).sensors()
        except C.ProtectError as exc:
            assert "sensors list" in str(exc), exc
        else:
            assert False, "accepted sensors shape %r" % (value,)

    class Stub:
        def sensor(self, sid):
            return {"airQuality": "not-an-object"}

    try:
        C.Protect.readings(Stub(), "sensor")
    except C.ProtectError as exc:
        assert "airQuality" in str(exc), exc
    else:
        assert False, "accepted a scalar airQuality value"
    return "bootstrap members validated before use"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
