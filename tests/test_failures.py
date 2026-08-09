"""Failure handling: persistence errors and malformed request bodies.

Everything here injects a failure rather than waiting for one, so no
disk is filled and no permissions are changed.
"""
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

    r = client.post("/api/ui/password", json={"password": "hunter2!x"})

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
    ]
    for path, body in cases:
        r = client.post(path, json=body)
        assert r.status_code == 400, "%s %r gave %s" % (
            path, body, r.status_code)
    return "%d bodies rejected with 400" % len(cases)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
