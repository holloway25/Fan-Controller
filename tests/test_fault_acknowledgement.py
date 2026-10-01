"""Request and timestamp boundaries for safety-fault acknowledgement.

All state is disposable; these tests never contact the plug or send email.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def _latched():
    C = harness.load()
    C.CFG["ui"]["password_hash"] = ""
    fault = {"at": C.time.time(), "msg": "test draw fault",
             "code": C.FC_UNDER_POWER, "stopped": True,
             "attempted_at": None, "delivered_at": None}
    C._store_fault(fault)
    C._oob_since = 123.0
    C._wake.clear()
    return C, fault


def _still_latched(C, fault):
    assert C.STATE["fault"] == fault, C.STATE["fault"]
    assert C.load_fault() == fault, "disk fault was changed"
    assert C._oob_since == 123.0, "draw timer was cleared"
    assert not C._wake.is_set(), "rejected request woke control"


def test_fault_clear_requires_a_valid_json_object_before_changing_state():
    C, fault = _latched()
    client = C.app.test_client()
    cases = (
        ({}, 415),
        ({"data": {"confirm": "yes"}}, 415),
        ({"data": "{}", "content_type": "text/plain"}, 415),
        ({"data": "{", "content_type": "application/json"}, 400),
        ({"data": "null", "content_type": "application/json"}, 400),
        ({"json": []}, 400),
        ({"data": " " * 9000, "content_type": "application/json"}, 413),
    )
    for args, status in cases:
        response = client.post("/api/fault/clear", **args)
        assert response.status_code == status, (args, response.status_code)
        _still_latched(C, fault)
    return "forms, missing bodies and malformed JSON preserve the fault"


def test_cross_origin_fault_clear_preserves_the_safety_latch():
    C, fault = _latched()
    client = C.app.test_client()
    headers = (
        {"Origin": "https://untrusted.example.invalid"},
        {"Origin": "null"},
        {"Origin": "http://localhost:8097"},
        {"Origin": "https://localhost"},
        {"Origin": "http://localhost:bad"},
        {"Referer": "https://untrusted.example.invalid/form"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    )
    for head in headers:
        response = client.post("/api/fault/clear", json={}, headers=head)
        assert response.status_code == 403, (head, response.status_code)
        _still_latched(C, fault)
    response = client.post("/api/fault/clear", data={"confirm": "yes"},
                           headers=headers[0])
    assert response.status_code == 403, response.status_code
    _still_latched(C, fault)
    return "foreign origins, ports, schemes and opaque origins cannot clear"


def test_same_origin_and_headerless_json_clients_can_acknowledge():
    cases = (
        ({}, "http://localhost"),
        ({"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
         "http://localhost"),
        ({"Origin": "http://LOCALHOST:80"}, "http://localhost"),
        ({"Referer": "http://localhost/dashboard"}, "http://localhost"),
        ({"Origin": "https://fan.example.invalid",
          "X-Forwarded-Proto": "https",
          "X-Forwarded-Host": "fan.example.invalid"}, "http://localhost"),
    )
    for headers, base_url in cases:
        C, _fault = _latched()
        response = C.app.test_client().post(
            "/api/fault/clear", json={}, headers=headers, base_url=base_url)
        assert response.status_code == 200, (headers, response.get_data(as_text=True))
        assert response.get_json() == {"ok": True, "cleared": True}
        assert C.STATE["fault"] is None
        assert not os.path.lexists(C.FAULT_PATH)
        assert C._wake.is_set()
    return "dashboard, forwarded HTTPS and script requests still clear"


def test_same_origin_guard_also_protects_configuration_writes():
    C = harness.load()
    before = C.CFG["control"]["mode"]
    response = C.app.test_client().post(
        "/api/config", json={"control": {"mode": "on"}},
        headers={"Origin": "https://untrusted.example.invalid"})
    assert response.status_code == 403, response.status_code
    assert C.CFG["control"]["mode"] == before
    return "foreign pages cannot bypass the guard through another API write"


def test_unrepresentable_fault_timestamps_fail_closed_and_clear_cleanly():
    for key in ("at", "attempted_at", "delivered_at"):
        for stamp in (1e300, -1e300, 10 ** 400, 1e15):
            C, fault = _latched()
            fault[key] = stamp
            with open(C.FAULT_PATH, "w") as f:
                json.dump(fault, f)
            loaded = C.load_fault()
            assert loaded.get("code") == "FC-206", (key, stamp, loaded)
            assert loaded.get("delivered_at") is None, loaded
            C.STATE["fault"] = loaded
            response = C.app.test_client().post("/api/fault/clear", json={})
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["cleared"] is True
            assert C.STATE["fault"] is None
            assert not os.path.lexists(C.FAULT_PATH)
    return "extreme floats and overflowing integers become FC-206"


def test_fault_notification_failure_cannot_turn_a_committed_clear_into_500():
    for injected in ("timestamp", "formatter", "queue"):
        C, fault = _latched()
        fault["delivered_at"] = C.time.time()
        if injected == "timestamp":
            fault["at"] = 1e300  # defensive coverage for already-live state
        C._store_fault(fault)
        def fail(*args, **kwargs):
            raise ValueError("injected notification failure")
        if injected == "formatter":
            C._alert_body = fail
        elif injected == "queue":
            C._notify = fail
        response = C.app.test_client().post("/api/fault/clear", json={})
        assert response.status_code == 200, (injected, response.get_data(as_text=True))
        assert response.get_json()["cleared"] is True
        assert C.STATE["fault"] is None
        assert not os.path.lexists(C.FAULT_PATH)
        assert C._wake.is_set(), "successful clear did not wake control"
        assert any("Fault-clear notification could not be prepared" in e["msg"]
                   for e in C.STATE["events"]), C.STATE["events"]
    return "formatting and notification failures leave successful clears successful"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
