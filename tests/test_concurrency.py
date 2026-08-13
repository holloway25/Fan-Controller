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


class GatedProtect:
    """Protect client whose read blocks until released.

    The Protect request is the slow part of a cycle and happens OUTSIDE
    _act_lock, which is what makes it the honest place to land a
    concurrent save. Gating the PLUG read instead deadlocks the test
    itself now that _fan_status runs inside the lock: the saver waits
    for _act_lock, the poll thread waits for the gate, and which one
    wins depends on the join timeouts. That is what made this module
    fail on 3.10 in CI and pass everywhere else.
    """

    def __init__(self, co2):
        self.co2 = co2
        self.entered = threading.Event()
        self.gate = threading.Event()

    def readings(self, sid):
        self.entered.set()
        self.gate.wait(10)
        return {"co2": self.co2, "temperature": 20.0,
                "humidity": 50.0, "pm25": 1.0}, "co2"


def test_a_save_mid_tick_neither_switches_nor_latches():
    """A save landing mid-cycle must not decide on superseded thresholds.

    decide() latches in place, and the latch is sticky inside the
    hysteresis band, so a decision made on numbers a save has already
    replaced would set a latch from thresholds no longer in force - and
    start the fan on the NEXT tick too, after the save had taken effect.

    Since every save bumps the config generation, the cycle is now
    discarded outright rather than re-deciding, which is the stronger
    guarantee: nothing from a superseded cycle reaches the plug or the
    latch.

    This replaces a test that committed CFG without bumping the
    generation - something api_config cannot do - and that gated the
    plug read, which now sits inside _act_lock and so could not be
    reached while the saver held it.
    """
    C = harness.load()
    protect = GatedProtect(830.0)
    plug = GatedPlug("never")
    C._clients = lambda: (protect, plug)

    C.CFG["control"].update(mode="auto", on_at=800.0, off_at=700.0,
                            min_run_minutes=15, temp_enabled=False,
                            temp_on=26.5, temp_off=24.0)
    C.CFG["protect"]["sensor_id"] = "stub"
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = False
        C.STATE["latch"] = {"co2": False, "temp": False}

    def save():
        """Raise on_at above the reading, exactly as api_config does."""
        with C._act_lock:
            fresh = dict(C.CFG["control"])
            fresh["on_at"] = 1000.0
            C.CFG["control"] = fresh
            C._cfg_generation += 1

    poll = threading.Thread(target=C.tick, name="poll")
    poll.start()
    assert protect.entered.wait(10), "the poll never reached the read"

    saver = threading.Thread(target=save)
    saver.start()
    saver.join(10)
    assert not saver.is_alive(), "the save could not commit"

    protect.gate.set()
    poll.join(10)
    assert not poll.is_alive(), "tick did not finish"

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


def test_a_settings_change_mid_cycle_does_not_command_the_old_plug():
    """The cycle re-read the settings but kept the client it started with.

    tick() builds its Protect and plug clients, then makes a network
    request that can take seconds. It re-reads CFG["control"] under
    _act_lock before deciding - but a plug ADDRESS change in that window
    left the new rules commanding the old physical plug. Reproduced by
    holding the sensor read open and saving a new plug host.
    """
    C = harness.load()
    started = threading.Event()
    release = threading.Event()
    old_commands = []
    new_commands = []

    class SlowSensor:
        def readings(self, sid):
            started.set()
            release.wait(5)
            return {"co2": 1500.0}, "path"

    class OldPlug:
        def state(self):
            return False

        def set(self, v):
            old_commands.append(v)
            return v

        def power_w(self):
            return 0.0

    class NewPlug(OldPlug):
        def set(self, v):
            new_commands.append(v)
            return v

    C.CFG["protect"]["sensor_id"] = "x"
    C.CFG["control"].update(mode="auto", co2_enabled=True,
                            on_at=1000, off_at=700, min_run_minutes=0)
    C._clients = lambda: (SlowSensor(), OldPlug())

    done = []
    th = threading.Thread(target=lambda: done.append(C.tick()))
    th.start()
    assert started.wait(5), "the poll never reached the sensor"

    # A settings save lands while the read is in flight.
    with C._cfg_lock:
        with C._act_lock:
            C.CFG["plug"] = dict(C.CFG["plug"], host="plug-new")
            C._cfg_generation += 1
    C._clients = lambda: (SlowSensor(), NewPlug())

    release.set()
    th.join(10)

    assert old_commands == [], \
        "commanded the plug that was replaced: %r" % old_commands
    assert new_commands == [], "commanded anything at all this cycle"
    return "stale cycle discarded"


def test_history_writes_are_serialised():
    """Two writers share one history.json.tmp.

    The poll thread and the restart or shutdown path can both call
    maybe_save_hist. save_hist writes through one fixed temporary path,
    so interleaved writers can lose the graph or make the restart path
    report a failure that did not happen.
    """
    C = harness.load()
    overlap = []
    inside = []
    real = C.save_hist

    def watched(hist):
        inside.append(1)
        if len(inside) > 1:
            overlap.append(1)
        time.sleep(0.05)
        inside.pop()
        return real(hist)

    C.save_hist = watched
    C._last_flush = 0

    threads = [threading.Thread(
        target=lambda: C.maybe_save_hist([{"t": time.time(), "v": 1}],
                                         force=True))
        for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)

    assert overlap == [], "two writers were inside save_hist at once"
    return "one writer at a time"


def test_the_actuating_helpers_refuse_a_superseded_plug():
    """The generation check in tick() was not the whole story.

    _manual_off, _force_on and _enforce_stop each take _act_lock and
    each re-check the MODE there - but they did not re-check the CLIENT.
    So a save landing after a cycle captured its plug still reached the
    OLD physical device through them: tick() calls two of them BEFORE
    the sensor read and before any generation check, and watts_loop
    calls the others in separate lock holds, seconds after its own
    check.

    Each helper is given the generation its plug was built from and
    refuses under the lock if CFG has moved on. Checked under _act_lock
    because the config commit bumps the generation while holding it, so
    nothing can slip between the check and the switch.
    """
    C = harness.load()

    class Plug:
        def __init__(self):
            self.commands = []

        def state(self):
            return False

        def set(self, on):
            self.commands.append(on)

    old = Plug()
    stale = C._cfg_generation
    C._cfg_generation += 1              # a save lands

    C.CFG["control"]["mode"] = "on"
    C._force_on(old, stale)
    assert old.commands == [], \
        "_force_on commanded a superseded plug: %r" % old.commands

    C.CFG["control"]["mode"] = "manual"
    C._manual_off(old, stale)
    assert old.commands == [], \
        "_manual_off commanded a superseded plug: %r" % old.commands

    C.CFG["control"]["mode"] = "auto"
    with C._state_lock:
        C.STATE["fault"] = {"at": C.time.time(), "msg": "draw fault"}
    C._enforce_stop(old, stale)
    assert old.commands == [], \
        "_enforce_stop commanded a superseded plug: %r" % old.commands

    # And with a current generation they still work, or the guard would
    # be silently disabling the feature rather than protecting it.
    C.CFG["control"]["mode"] = "on"
    C._force_on(old, C._cfg_generation)
    assert old.commands == [True], \
        "refused a current plug too: %r" % old.commands
    return "refuses stale, acts on current"


def test_a_helper_with_no_generation_still_acts():
    """gen=None means "no window to guard", not "refuse"."""
    C = harness.load()

    class Plug:
        def __init__(self):
            self.commands = []

        def state(self):
            return False

        def set(self, on):
            self.commands.append(on)

    plug = Plug()
    C._cfg_generation += 5
    C.CFG["control"]["mode"] = "on"
    C._force_on(plug)
    assert plug.commands == [True], "a caller with no generation was refused"
    return "None means unguarded, not blocked"


def test_a_superseded_cycle_records_nothing_at_all():
    """A discarded cycle must leave no trace, not just skip the switch.

    The generation was checked only at the actuation block, so
    everything before it acted on a superseded cycle: reading_at was
    refreshed from the OLD sensor - resetting the stale timer, and
    delaying the fail-safe for a newly configured sensor that may be
    unreachable - the OLD plug's relay was read and a difference adopted
    as an external switch, and the readings-returned all-clear could be
    sent because the old sensor answered.

    The save lands DURING the Protect read, which is where a real one
    would: that request is the slow part of the cycle.
    """
    C = harness.load()

    class Plug:
        def __init__(self):
            self.commands = []
            self.asked = 0

        def state(self):
            self.asked += 1
            return True             # differs from cached False

        def set(self, on):
            self.commands.append(on)

        def power_w(self):
            return 0.0

    plug = Plug()

    class Sensor:
        def readings(self, sid):
            # The save happens while this request is in flight.
            C._cfg_generation += 1
            return {"co2": 1500.0}, "path"

    C._clients = lambda: (Sensor(), plug)
    C.CFG["protect"]["sensor_id"] = "x"
    C.CFG["control"].update(mode="auto", co2_enabled=True, on_at=1000,
                            off_at=700)

    before_reading_at = 12345.0
    with C._state_lock:
        C.STATE["reading_at"] = before_reading_at
        C.STATE["fan"] = False
        C.STATE["history"] = []
        C.STATE["co2"] = None
    C._stale_delivered = True

    sent = []
    C._notify = lambda s, b, on_complete=None: sent.append(s)

    C.tick()

    assert C.STATE["reading_at"] == before_reading_at, \
        "a superseded reading reset the stale timer"
    assert C.STATE["history"] == [], "recorded a superseded reading"
    assert C.STATE["co2"] is None, "published a superseded reading"
    assert C.STATE["fan"] is False, \
        "adopted the old plug's relay state as an external switch"
    assert plug.commands == [], "commanded a superseded plug: %r" % plug.commands
    assert sent == [], "announced recovery from a superseded cycle: %r" % sent
    assert C._stale_delivered is True, \
        "consumed the recovery edge without announcing it"
    return "no reading, no adoption, no all-clear"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
