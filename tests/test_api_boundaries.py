"""Small API and draw-loop regressions using disposable controller state."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_daily_is_public_and_state_does_not_duplicate_it():
    C = harness.load()
    with C._state_lock:
        C.STATE["daily"] = [{"d": "2026-09-30", "v": {"lo": 700,
                                                  "hi": 900}}]
    C.CFG["ui"]["password_hash"] = C.set_ui_password("test-only-password")
    client = C.app.test_client()
    daily = client.get("/api/daily")
    state = client.get("/api/state")
    assert daily.status_code == 200, daily.status_code
    assert state.status_code == 200, state.status_code
    assert len(daily.get_json()["days"]) == 1
    assert "daily" not in state.get_json(), "state duplicates the daily archive"
    assert client.post("/api/fault/clear").status_code == 401, \
        "making daily public must not open write routes"
    return "daily is public; writes stay protected; state stays small"


def test_draw_loop_reports_an_unexpected_check_failure():
    C = harness.load()
    class Plug:
        def state(self):
            return False
        def power_w(self):
            return 18.0
    C._client_snapshot = lambda: (None, Plug(), C._cfg_generation)
    C._retry_fault_persist = lambda: False
    def broken_check(*args):
        raise RuntimeError("programming defect")
    C._draw_check = broken_check
    def stop_after_one_cycle(_seconds):
        raise StopIteration()
    C.time.sleep = stop_after_one_cycle
    try:
        C.watts_loop()
    except StopIteration:
        pass
    else:
        assert False, "loop did not complete its first cycle"
    assert C.STATE["draw_loop_at"] is not None
    assert C.STATE["draw_loop_error"] == "draw check: RuntimeError", \
        C.STATE["draw_loop_error"]
    assert any("Draw loop error" in e["msg"] for e in C.STATE["events"])
    return "draw-check failure reaches state and activity log"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
