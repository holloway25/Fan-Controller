"""Settings saves cannot repeatedly postpone a draw fault; mocked devices only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    def __init__(self):
        self.commands = []

    def state(self):
        return True

    def set(self, on):
        self.commands.append(on)


def setup():
    C = harness.load()
    clock = [10000.0]
    C.time.time = lambda: clock[0]
    C.CFG["control"]["mode"] = "auto"
    C.CFG["plug"].update(watts_enabled=True, watts_min=10.0,
                         watts_max=20.0, watts_fault_seconds=60)
    C.STATE.update(fan=True, watts=0.0, watts_at=clock[0],
                   changed_at=clock[0] - 1000, fault=None)
    C._notify = lambda *args, **kwargs: None
    plug = Plug()
    C._draw_check(plug, C._cfg_generation)
    assert C._oob_since == clock[0]
    return C, clock, plug


def save(C, body):
    response = C.app.test_client().post("/api/config", json=body)
    assert response.status_code == 200, response.get_json()


def tick(C, clock, plug, seconds):
    clock[0] += seconds
    C.STATE["watts_at"] = clock[0]
    C._draw_check(plug, C._cfg_generation)


def test_repeated_email_and_noop_saves_cannot_postpone_fault():
    for body in ({"email": {"sender": "test@example.com"}}, {}):
        C, clock, plug = setup()
        start = C._oob_since
        for _ in range(2):
            clock[0] += 30
            save(C, body)
            assert C._oob_since == start, "unrelated save reset draw timer"
            C.STATE["watts_at"] = clock[0]
            C._draw_check(plug, C._cfg_generation)
        assert C.STATE["fault"] is not None, "fault was postponed beyond 60 seconds"
        assert plug.commands == [False], plug.commands


def test_unrelated_sections_preserve_timer_and_reject_old_clients():
    for body in ({"ui": {"password": "example-ui-password"}},
                 {"protect": {"host": "protect.example.com"}},
                 {"control": {"on_at": 1200}}):
        C, clock, plug = setup()
        old = C._cfg_generation
        start = C._oob_since
        save(C, body)
        assert C._cfg_generation == old + 1
        assert C._stale_client(old)
        clock[0] += 120
        C.STATE["watts_at"] = clock[0]
        C._draw_check(plug, old)
        assert C.STATE["fault"] is None
        assert not plug.commands
        assert C._oob_since == start
        C._draw_check(plug, C._cfg_generation)
        assert C.STATE["fault"] is not None


def test_each_relevant_plug_setting_resets_timer():
    changes = {"host": "plug.example.com", "user": "test-user",
               "password": "", "watts_min": 11, "watts_max": 21,
               "watts_fault_seconds": 90, "watts_enabled": False}
    for key, value in changes.items():
        C, clock, plug = setup()
        if key == "password":
            C.CFG["plug"]["password"] = "example-secret"
        clock[0] += 30
        save(C, {"plug": {key: value}})
        assert C._oob_since is None, key
        tick(C, clock, plug, 0)
        assert C.STATE["fault"] is None, key
        if key != "watts_enabled":
            assert C._oob_since == clock[0], key


def test_failed_save_preserves_timer_and_generation():
    C, clock, plug = setup()
    old, start = C._cfg_generation, C._oob_since
    def fail(_):
        raise OSError("test disk failure")
    C.save_cfg = fail
    response = C.app.test_client().post("/api/config", json={"plug": {"watts_min": 11}})
    assert response.status_code == 500
    assert C._cfg_generation == old
    assert C._oob_since == start
    tick(C, clock, plug, 60)
    assert C.STATE["fault"] is not None


def test_force_on_transition_discards_timer_without_a_draw_cycle():
    C, clock, plug = setup()
    save(C, {"control": {"mode": "on"}})
    assert C._oob_since is None
    save(C, {"control": {"mode": "auto"}})
    tick(C, clock, plug, 120)
    assert C.STATE["fault"] is None
    assert C._oob_since == clock[0]


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
