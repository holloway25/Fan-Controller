"""Atomic, episode-specific Force on confirmation; disposable state only."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def setup():
    C = harness.load()
    C.CFG["control"]["mode"] = "auto"
    C.save_cfg(C.CFG)
    return C


def fault(C, at=1000):
    C._store_fault({"at": at, "msg": "test draw fault", "code": "FC-003", "stopped": True})


def test_healthy_force_on_is_prompt_free():
    C = setup()
    r = C.app.test_client().post("/api/config", json={"control": {"mode": "on"}})
    assert r.status_code == 200, r.get_json()
    assert C.CFG["control"]["mode"] == "on"


def test_live_fault_requires_confirmation_before_any_save():
    C = setup()
    fault(C)
    before = open(C.CFG_PATH).read()
    C._wake.clear()
    r = C.app.test_client().post("/api/config", json={"control": {"mode": "on", "on_at": 1100}})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["fault_override_required"] is True
    assert isinstance(r.get_json()["fault_override_token"], str)
    assert C.CFG["control"]["mode"] == "auto"
    assert open(C.CFG_PATH).read() == before
    assert not C._wake.is_set()


def test_confirmation_is_bound_to_current_fault_episode():
    C = setup()
    client = C.app.test_client()
    fault(C)
    r = client.post("/api/config", json={"control": {"mode": "on"}})
    assert r.status_code == 409, r.get_json()
    token = r.get_json()["fault_override_token"]
    fault(C, at=2000)
    r = client.post("/api/config", json={"control": {"mode": "on"}},
                    headers={"X-Fault-Override": token})
    assert r.status_code == 409, r.get_json()
    assert C.CFG["control"]["mode"] == "auto"
    new_token = r.get_json()["fault_override_token"]
    assert new_token != token
    r = client.post("/api/config", json={"control": {"mode": "on"}},
                    headers={"X-Fault-Override": new_token})
    assert r.status_code == 200, r.get_json()
    assert C.CFG["control"]["mode"] == "on"
    assert C.STATE["fault"]["at"] == 2000, "override must not acknowledge fault"


def test_invalid_confirmation_cannot_override_a_fault():
    C = setup()
    fault(C)
    for token in ("", "true", "unrelated-token"):
        r = C.app.test_client().post("/api/config", json={"control": {"mode": "on"}},
                                    headers={"X-Fault-Override": token})
        assert r.status_code == 409, r.get_json()
        assert C.CFG["control"]["mode"] == "auto"


def test_fault_check_and_commit_hold_actuation_and_fault_locks():
    C = setup()
    seen = {}
    save = C.save_cfg

    def watched(value):
        def probe():
            for name in ("_act_lock", "_fault_lock"):
                lock = getattr(C, name)
                free = lock.acquire(blocking=False)
                seen[name] = free
                if free:
                    lock.release()
        t = threading.Thread(target=probe)
        t.start()
        t.join(2)
        assert not t.is_alive()
        return save(value)

    C.save_cfg = watched
    r = C.app.test_client().post("/api/config", json={"control": {"mode": "on"}})
    assert r.status_code == 200, r.get_json()
    assert seen == {"_act_lock": False, "_fault_lock": False}, seen


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
