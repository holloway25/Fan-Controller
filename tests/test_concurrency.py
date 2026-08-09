"""Concurrency regression tests.

    ./venv/bin/python tests/test_concurrency.py

Setup, isolation and the runner live in harness.py. These tests stub the
plug and drive the real threading code; nothing here contacts a plug or
a Protect console.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class GatedPlug:
    """A plug whose first read from a named thread blocks until released.

    Timing alone does not reproduce these races: _manual_off does its own
    plug read, which reorders things so the stop lands last and the bug
    hides. Gating makes the interleaving deterministic.
    """

    def __init__(self, hold_thread):
        self.calls = []
        self.on = False
        self._hold = hold_thread
        self._held = False
        self.entered = threading.Event()
        self.gate = threading.Event()

    def state(self):
        if threading.current_thread().name == self._hold and not self._held:
            self._held = True
            self.entered.set()
            self.gate.wait(5)
        return self.on

    def set(self, value):
        self.calls.append(value)
        self.on = value
        return value

    def power_w(self):
        return 0.0


def enter_manual(C):
    """What api_config does when the mode changes: commit under _act_lock."""
    with C._act_lock:
        C.CFG["control"]["mode"] = "manual"
        with C._state_lock:
            C.STATE["manual_off_pending"] = True


def test_failsafe_cannot_undo_manual():
    """The stale-reading fail-safe must not switch the fan on in Manual.

    It re-reads the plug before switching, so its window spans that call's
    timeout. Before _act_lock covered this path, a Manual selected inside
    the window ran its one-time stop, cleared the flag, and only then did
    the fail-safe switch the fan on - leaving it running with nothing left
    to correct it.
    """
    C = harness.load()
    plug = GatedPlug("failsafe")
    C._clients = lambda: (None, plug)

    C.CFG["control"].update(mode="auto", stale_minutes=1)
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = False
        C.STATE["reading_at"] = time.time() - 9999
        C.STATE["manual_off_pending"] = False

    blocked = []

    def user():
        started = time.time()
        enter_manual(C)
        C._manual_off(plug)
        blocked.append(time.time() - started)

    fail = threading.Thread(target=C._failsafe, name="failsafe")
    fail.start()
    plug.entered.wait(5)

    saver = threading.Thread(target=user, name="user")
    saver.start()
    time.sleep(0.4)
    plug.gate.set()

    fail.join(10)
    saver.join(10)

    assert C.CFG["control"]["mode"] == "manual"
    assert not plug.on, "fan left ON in Manual: %r" % (plug.calls,)
    assert blocked and blocked[0] >= 0.3, (
        "the save did not wait for the in-flight switch (%r) - _act_lock "
        "is not covering the fail-safe" % (blocked,))
    return "save blocked %.2fs, calls %r" % (blocked[0], plug.calls)



class StubProtect:
    """Protect client that returns a fixed reading without a console."""

    def __init__(self, co2):
        self.co2 = co2

    def readings(self, sid):
        return {"co2": self.co2, "temperature": 20.0,
                "humidity": 50.0, "pm25": 1.0}, "co2"


def test_tick_decides_on_the_config_in_force():
    """A save landing mid-tick must not switch on superseded thresholds.

    decide() latches in place, and the latch is sticky inside the
    hysteresis band. Deciding on thresholds a save has already replaced
    therefore sets a latch from numbers no longer in force, which would
    start the fan on the next tick as well - so the decision, not just
    the switch, has to happen under _act_lock.
    """
    C = harness.load()
    plug = GatedPlug("poll")
    C._clients = lambda: (StubProtect(830.0), plug)

    C.CFG["control"].update(mode="auto", on_at=800.0, off_at=700.0,
                            min_run_minutes=15, temp_enabled=False,
                            temp_on=26.5, temp_off=24.0)
    C.CFG["protect"]["sensor_id"] = "stub"
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = False
        C.STATE["latch"] = {"co2": False, "temp": False}

    def save():
        """Raise on_at above the reading, as api_config does."""
        with C._act_lock:
            fresh = dict(C.CFG["control"])
            fresh["on_at"] = 1000.0
            C.CFG["control"] = fresh

    poll = threading.Thread(target=C.tick, name="poll")
    poll.start()
    plug.entered.wait(5)
    saver = threading.Thread(target=save)
    saver.start()
    saver.join(5)
    plug.gate.set()
    poll.join(10)

    assert not plug.on, "fan started on superseded thresholds: %r" % (
        plug.calls,)
    assert C.STATE["latch"]["co2"] is False, (
        "latch set from thresholds no longer in force: %r"
        % (C.STATE["latch"],))
    return "no switch, latch clean"


def test_failsafe_rechecks_config_after_waiting_for_the_lock():
    """Disabling the fail-safe must cancel one already queued.

    stale_minutes is read before _act_lock is taken. A save setting it
    to 0 while this call waits for the lock would otherwise ventilate on
    a setting no longer in force, so the value is re-read inside.
    """
    C = harness.load()
    plug = GatedPlug("never")
    C._clients = lambda: (None, plug)

    C.CFG["control"].update(mode="auto", stale_minutes=1)
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = False
        C.STATE["reading_at"] = time.time() - 9999

    with C._act_lock:
        fail = threading.Thread(target=C._failsafe, name="failsafe")
        fail.start()
        time.sleep(0.4)          # it is now blocked on the lock
        fresh = dict(C.CFG["control"])
        fresh["stale_minutes"] = 0
        C.CFG["control"] = fresh
    fail.join(10)

    assert not plug.on, "ventilated after being disabled: %r" % (plug.calls,)
    return "cancelled while queued"


def test_password_change_survives_a_concurrent_save():
    """A settings save must not commit its stale "ui" section.

    api_config deep-copies every section, so before _cfg_lock a save
    that started first would write back the ui section as it looked
    then - silently reverting a password set in the meantime.
    """
    C = harness.load()
    C.CFG["ui"]["password_hash"] = ""
    C.CFG["control"].update(mode="auto", on_at=1000.0, off_at=700.0)

    inside = threading.Event()
    release = threading.Event()
    real = C._guard_credentials

    def gated(cand, body):
        """Runs after the snapshot, before the commit."""
        inside.set()
        release.wait(5)
        return real(cand, body)

    C._guard_credentials = gated
    client = C.app.test_client()
    result = {}

    def save():
        r = client.post("/api/config", json={"control": {"on_at": 900}})
        result["status"] = r.status_code

    def set_password():
        r = client.post("/api/ui/password", json={"password": "hunter2!x"})
        result["pw_status"] = r.status_code

    saver = threading.Thread(target=save)
    saver.start()
    inside.wait(5)                     # snapshot taken, not yet committed
    pw = threading.Thread(target=set_password)
    pw.start()
    time.sleep(0.3)                    # it must be queued on _cfg_lock
    release.set()
    saver.join(10)
    pw.join(10)

    assert result.get("status") == 200, result
    assert result.get("pw_status") == 200, result
    assert C.CFG["control"]["on_at"] == 900.0, C.CFG["control"]["on_at"]
    assert C.CFG["ui"]["password_hash"], (
        "password reverted by a save that had already copied CFG")
    return "both changes survived"


def test_events_on_disk_match_memory_under_concurrent_logs():
    """An older snapshot must not overwrite a newer one.

    _log built its snapshot under _state_lock but wrote outside it, and
    save_events shares one events.json.tmp. Two threads could therefore
    interleave so the earlier, shorter snapshot landed last, leaving the
    activity log on disk behind memory - which is what a restart reloads.
    """
    import json

    C = harness.load()
    with C._state_lock:
        C.STATE["events"] = []

    entered = threading.Event()
    release = threading.Event()
    real = C.save_events
    held = []

    def gated(events):
        if threading.current_thread().name == "slow" and not held:
            held.append(True)
            entered.set()
            release.wait(5)
        return real(events)

    C.save_events = gated

    slow = threading.Thread(target=C._log, args=("first",), name="slow")
    slow.start()
    entered.wait(5)                    # snapshot ["first"], mid-write

    fast = threading.Thread(target=C._log, args=("second",), name="fast")
    fast.start()
    time.sleep(0.3)                    # queued on _events_lock, or racing
    release.set()
    slow.join(10)
    fast.join(10)

    with open(C.EVENTS_PATH) as f:
        on_disk = json.load(f)
    in_memory = C.STATE["events"]

    assert [e["msg"] for e in in_memory] == ["second", "first"], in_memory
    assert [e["msg"] for e in on_disk] == ["second", "first"], (
        "disk is behind memory: %r" % ([e["msg"] for e in on_disk],))
    return "disk matches memory"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
