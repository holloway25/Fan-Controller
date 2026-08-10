"""_force_on and _enforce_stop: the two relay paths nothing pinned.

Five paths switch the plug. These are the two that had no tests at all,
and both were verified only by hand on hardware in August. Each one
carries an argument in its docstring about WHY it is shaped the way it
is - a re-check under the lock, a write kept outside one, a log emitted
before the thing it describes is undone. Those arguments are what is
asserted here, because they are what a later edit would quietly remove.

Nothing here contacts a plug or a Protect console.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    """A plug that answers from memory and records what it was told."""

    def __init__(self, on=False, readable=True):
        self.on = on
        self.calls = []
        self.readable = readable
        self.reads = 0

    def state(self):
        self.reads += 1
        return self.on if self.readable else None

    def set(self, value):
        self.calls.append(value)
        self.on = value
        return value

    def power_w(self):
        return 0.0


class BrokenPlug(Plug):
    """A plug that cannot be commanded."""

    def __init__(self, C, on=True, fail_on_read=False):
        Plug.__init__(self, on=on)
        self._err = C.PlugError
        self._fail_on_read = fail_on_read

    def state(self):
        if self._fail_on_read:
            raise self._err("unreachable")
        return Plug.state(self)

    def set(self, value):
        raise self._err("unreachable")


def events(C):
    return " | ".join(e["msg"] for e in C.STATE["events"])


def setup(C, mode="on", fan=False):
    C.CFG["control"]["mode"] = mode
    with C._state_lock:
        C.STATE["fan"] = fan
        C.STATE["fault"] = None
        C.STATE["events"] = []
        C.STATE["changed_at"] = None
        C.STATE["manual_off_pending"] = False


# --------------------------------------------------------------- _force_on

def test_force_on_rechecks_the_mode_under_the_lock():
    """The caller checked the mode; that is not enough.

    Manual's stop is one-shot. If it has already run and cleared its
    flag, switching the fan on here would leave it going with nothing
    left to correct it.
    """
    C = harness.load()
    setup(C, mode="manual", fan=False)
    plug = Plug(on=False)

    C._force_on(plug)

    assert plug.calls == [], (
        "forced the fan on in %s mode: %r"
        % (C.CFG["control"]["mode"], plug.calls))
    return "no switch outside Force on"


def test_force_on_leaves_a_fan_that_is_already_on_alone():
    """Idempotent: it runs every cycle, so it must not restart the lease
    or log on every pass."""
    C = harness.load()
    setup(C, mode="on", fan=True)
    with C._state_lock:
        C.STATE["changed_at"] = 12345.0
    plug = Plug(on=True)

    C._force_on(plug)

    assert plug.calls == [], "switched a fan that was already on"
    assert C.STATE["fan"] is True, "lost track of a fan that is on"
    assert C.STATE["changed_at"] == 12345.0, (
        "restarted the minimum-run lease on a no-op pass")
    assert "forced on" not in events(C), (
        "logged a switch that did not happen: %s" % events(C))
    return "no switch, no lease reset, no event"


def test_force_on_switches_a_fan_that_was_turned_off_elsewhere():
    C = harness.load()
    setup(C, mode="on", fan=True)
    plug = Plug(on=False)

    C._force_on(plug)

    assert plug.calls == [True], "did not put the fan back on: %r" % plug.calls
    assert C.STATE["fan"] is True
    assert C.STATE["changed_at"] is not None, "no lease started"
    assert "Fan on" in events(C), events(C)
    return "switched back on and logged"


def test_an_external_switch_off_is_recorded_before_it_is_undone():
    """Forcing it back must not swallow the evidence that it happened."""
    C = harness.load()
    setup(C, mode="on", fan=True)

    seen = {}
    plug = Plug(on=False)
    real_set = plug.set

    def watched(value):
        # How many events existed at the moment of the switch.
        seen["before_switch"] = len(C.STATE["events"])
        return real_set(value)

    plug.set = watched
    C._force_on(plug)

    log = events(C)
    assert "switched to off outside this service" in log, (
        "an external switch-off left no trace: %s" % log)
    assert seen.get("before_switch", 0) >= 1, (
        "the evidence was logged after the switch that undid it")
    return "recorded first, then undone"


def test_an_unreadable_plug_is_not_reported_as_a_switch_off():
    """Unknown is not off. Reporting it invents an event that never
    happened, and this path runs every cycle."""
    C = harness.load()
    setup(C, mode="on", fan=True)
    plug = Plug(on=False, readable=False)   # state() returns None

    C._force_on(plug)

    assert "switched to off outside" not in events(C), (
        "invented an external switch-off from an unreadable plug: %s"
        % events(C))
    assert plug.calls == [True], (
        "an unreadable plug stopped the override working: %r" % plug.calls)
    return "no invented event, still forced on"


class GatedPlug(Plug):
    """A plug whose read blocks the FIRST caller until it is released.

    Sleeping only widens the window and hopes the other thread runs in
    it, which on a loaded Pi it may not - so a missing lock could pass.

    The read returns the value seen BEFORE the block, not after. A
    version that re-read on release still passed with _act_lock removed:
    by then the overtaking caller had switched the plug, so the first
    caller saw True and returned early, and the test proved nothing.
    Verified by neutralising _act_lock and watching this fail.
    """

    def __init__(self, on=False):
        Plug.__init__(self, on=on)
        self.entered = threading.Event()
        self.gate = threading.Event()
        self._held = False

    def state(self):
        if not self._held:
            self._held = True
            # Captured BEFORE blocking, and returned after. This is the
            # whole trick: a caller that reads, is overtaken, and then
            # acts on what it read is exactly the race _act_lock exists
            # to prevent. Re-reading here would hide it, because the
            # value would have been corrected by the overtaking caller.
            stale = self.on
            self.entered.set()
            self.gate.wait(10)
            self.reads += 1
            return stale
        return Plug.state(self)


def test_two_callers_cannot_both_switch_and_both_log():
    """tick() and the draw thread both call this. _act_lock is what
    stops them both acting on the same off fan."""
    C = harness.load()
    setup(C, mode="on", fan=True)
    plug = GatedPlug(on=False)

    first = threading.Thread(target=C._force_on, args=(plug,), name="first")
    first.start()
    assert plug.entered.wait(10), "the first caller never reached the read"

    # Started only once the first caller is provably inside the read and
    # holding _act_lock, so the second has to contend for it.
    second = threading.Thread(target=C._force_on, args=(plug,), name="second")
    second.start()
    second_ready = threading.Event()
    second_ready.wait(0.2)      # let it reach the lock; correctness does
                                # not depend on this having been enough
    plug.gate.set()

    first.join(10)
    second.join(10)

    assert plug.calls == [True], (
        "both callers switched the fan: %r" % plug.calls)
    assert events(C).count("forced on") == 1, (
        "the same switch was logged twice: %s" % events(C))
    return "one switch, one event"


# ----------------------------------------------------------- _enforce_stop

def test_enforce_stop_without_a_plug_does_nothing():
    C = harness.load()
    setup(C, mode="auto", fan=True)
    C._enforce_stop(None)
    return "no plug, no crash"


def test_force_on_overrides_a_fault_stop():
    """The mode can move between _draw_check reading it and this point.

    Without the re-check the stop lands just after _force_on switched the
    fan on, flapping it off until the next draw cycle puts it back.
    """
    C = harness.load()
    setup(C, mode="on", fan=True)
    plug = Plug(on=True)

    C._enforce_stop(plug)

    assert plug.calls == [], (
        "stopped a fan that Force on is holding: %r" % plug.calls)
    assert plug.on is True, "the fan was flapped off"
    return "Force on still wins"


def test_enforce_stop_switches_a_running_fan_off():
    C = harness.load()
    setup(C, mode="auto", fan=True)
    plug = Plug(on=True)

    C._enforce_stop(plug)

    assert plug.calls == [False], "did not stop the fan: %r" % plug.calls
    assert C.STATE["fan"] is False
    return "stopped and recorded"


def test_enforce_stop_does_not_re_command_a_fan_already_off():
    """It runs every cycle while a fault stands."""
    C = harness.load()
    setup(C, mode="auto", fan=False)
    plug = Plug(on=False)

    C._enforce_stop(plug)

    assert plug.calls == [], (
        "re-commanded a fan already confirmed off: %r" % plug.calls)
    assert C.STATE["fan"] is False
    return "confirmed off is left alone"


def test_a_plug_that_cannot_be_commanded_leaves_the_stop_unconfirmed():
    """An unconfirmed stop must be retried, so nothing may record it as
    done - not the fan state, and not the fault."""
    C = harness.load()
    setup(C, mode="auto", fan=True)
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": False}
    plug = BrokenPlug(C, on=True)

    C._enforce_stop(plug)

    assert C.STATE["fault"]["stopped"] is False, (
        "recorded a stop the plug never confirmed - it would never be "
        "retried")
    assert "stop confirmed" not in events(C), (
        "logged a confirmation that did not happen: %s" % events(C))
    return "left unconfirmed for the next cycle"


def test_an_unreadable_plug_leaves_the_stop_unconfirmed():
    C = harness.load()
    setup(C, mode="auto", fan=True)
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": False}
    plug = BrokenPlug(C, fail_on_read=True)

    C._enforce_stop(plug)

    assert C.STATE["fault"]["stopped"] is False, (
        "confirmed a stop against a plug it could not even read")
    return "an unreadable plug confirms nothing"


def test_the_fault_write_happens_outside_the_actuation_lock():
    """A disk write must not extend a hold a settings save may be
    waiting on. The docstring says so; nothing checked it."""
    C = harness.load()
    setup(C, mode="auto", fan=True)
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": False}
    plug = Plug(on=True)

    seen = {}
    real_confirm = C._confirm_fault_stopped

    def watched():
        free = C._act_lock.acquire(blocking=False)
        seen["act_lock_free"] = free
        if free:
            C._act_lock.release()
        return real_confirm()

    C._confirm_fault_stopped = watched
    try:
        C._enforce_stop(plug)
    finally:
        C._confirm_fault_stopped = real_confirm

    assert seen.get("act_lock_free") is True, (
        "the fault was written while holding _act_lock, so a disk stall "
        "would block every other actuation path")
    assert C.STATE["fault"]["stopped"] is True, "the stop was not recorded"
    assert "stop confirmed" in events(C), events(C)
    return "written outside the lock, and recorded"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
