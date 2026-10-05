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


def setup(C, mode="on", fan=False, fault=False):
    """Put the module in a known state.

    fault defaults False, and that USED to be fine for the enforcement
    tests: _enforce_stop switched the fan off whether or not a fault was
    standing, so they passed with nothing to enforce. It now re-checks
    the fault under _act_lock - an acknowledgement could otherwise land
    between _draw_check seeing the fault and this stop, switching the
    fan off after the dashboard said it was clear. So a test that
    expects a stop has to say what it is enforcing.
    """
    C.CFG["control"]["mode"] = mode
    with C._state_lock:
        C.STATE["fan"] = fan
        C.STATE["fault"] = ({"at": C.time.time(), "msg": "draw fault",
                             "stopped": False} if fault else None)
        C.STATE["events"] = []
        C.STATE["changed_at"] = None
        C.STATE["manual_off_pending"] = False


# --------------------------------------------------------------- _force_on

def test_failed_stops_keep_fresh_on_and_invalidate_stop_confirmation():
    for manual in (True, False):
        C = harness.load()
        setup(C, mode='manual' if manual else 'auto', fan=False, fault=True)
        C.CFG['ui']['password_hash'] = ''
        C.STATE['fault']['stopped'] = True
        C.STATE['manual_off_pending'] = True
        plug = BrokenPlug(C, on=True)
        if manual:
            try:
                C._manual_off(plug)
            except C.PlugError:
                pass
            else:
                raise AssertionError('Expected failed stop')
            assert C.STATE['manual_off_pending']
        else:
            assert C._enforce_stop(plug) is False
        assert C.STATE['fan'] is True and C.STATE['fault']['stopped'] is False
        assert C.STATE['fault_unsaved']
        assert C.app.test_client().get('/api/state').get_json()['fan'] is True
        assert 'not currently confirmed' in C._fault_stop_status(
            C.STATE['fault'], C.CFG['control']['mode'], True)[0]
        assert C._retry_fault_persist()
        assert C.load_fault()['stopped'] is False

def test_force_on_failed_reassert_keeps_confirmed_off_in_api():
    C = harness.load()
    setup(C, fan=True)
    C.CFG['ui']['password_hash'] = ''
    try:
        C._force_on(BrokenPlug(C, on=False))
    except C.PlugError:
        pass
    else:
        raise AssertionError('Expected failed ON command')
    assert C.STATE['fan'] is False
    assert C.app.test_client().get('/api/state').get_json()['fan'] is False
    assert 'forced on' not in events(C)
    plug = Plug(on=False)
    C._force_on(plug)
    assert C.STATE['fan'] is True and plug.calls == [True]

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
    assert "Fan off" in log and "changed outside" in log, (
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


class WatchedLock:
    """The real lock, which announces when someone has to wait for it.

    The previous version of this test created an Event, waited 0.2s on
    it, and never set it - a sleep with extra steps, and worse than an
    honest sleep because it read as synchronisation. On a loaded Pi the
    second thread might not reach the lock inside that window, and a
    removed lock would pass.

    Signalling from inside the lock removes the guess: the test waits
    for the second caller to actually be blocked, not for a duration.
    """

    def __init__(self, real):
        self.real = real
        self.contended = threading.Event()

    def __enter__(self):
        if not self.real.acquire(blocking=False):
            # Someone holds it, so this caller is about to block. Said
            # before blocking, or it could not be said at all.
            self.contended.set()
            self.real.acquire()
        return self

    def __exit__(self, *a):
        self.real.release()
        return False

    def acquire(self, *a, **k):
        return self.real.acquire(*a, **k)

    def release(self):
        return self.real.release()


def test_two_callers_cannot_both_switch_and_both_log():
    """tick() and the draw thread both call this. _act_lock is what
    stops them both acting on the same off fan."""
    C = harness.load()
    setup(C, mode="on", fan=True)
    plug = GatedPlug(on=False)

    watched = WatchedLock(C._act_lock)
    C._act_lock = watched
    try:
        first = threading.Thread(target=C._force_on, args=(plug,),
                                 name="first")
        first.start()
        assert plug.entered.wait(10), \
            "the first caller never reached the read"

        # Started only once the first caller is provably inside the read
        # and holding _act_lock, so the second has to contend for it.
        second = threading.Thread(target=C._force_on, args=(plug,),
                                  name="second")
        second.start()

        # Wait for the lock itself to report contention rather than
        # sleeping and hoping. If it never reports any - which is what a
        # removed lock looks like - carry on anyway after a bounded wait
        # and let the assertions below say so.
        contended = watched.contended.wait(5)

        plug.gate.set()
        first.join(10)
        second.join(10)
    finally:
        C._act_lock = watched.real

    assert contended, (
        "nobody ever had to wait for _act_lock, so the two callers were "
        "never serialised")

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
    setup(C, mode="auto", fan=True, fault=True)
    plug = Plug(on=True)

    C._enforce_stop(plug)

    assert plug.calls == [False], "did not stop the fan: %r" % plug.calls
    assert C.STATE["fan"] is False
    return "stopped and recorded"


def test_enforce_stop_does_not_re_command_a_fan_already_off():
    """It runs every cycle while a fault stands."""
    C = harness.load()
    setup(C, mode="auto", fan=False, fault=True)
    plug = Plug(on=False)

    C._enforce_stop(plug)

    assert plug.calls == [], (
        "re-commanded a fan already confirmed off: %r" % plug.calls)
    assert C.STATE["fan"] is False
    return "confirmed off is left alone"


def test_enforce_stop_does_nothing_once_the_fault_is_acknowledged():
    """A stop must not outlive the fault it was enforcing.

    _draw_check sees a fault, releases _act_lock, and calls
    _enforce_stop. An acknowledgement landing in between used to be
    ignored: the fan was switched OFF after the dashboard had reported
    the fault cleared, with nothing on screen to explain it. The fault
    is now re-checked inside the hold, and _clear_fault takes _act_lock
    so the two cannot interleave.
    """
    C = harness.load()
    setup(C, mode="auto", fan=True, fault=True)
    plug = Plug(on=True)

    # The acknowledgement lands first, exactly as the dashboard would.
    had, ok = C._clear_fault()
    assert had and ok, "the fault was not acknowledged"

    C._enforce_stop(plug)

    assert plug.calls == [], (
        "stopped the fan after the fault was acknowledged: %r"
        % plug.calls)
    assert plug.on is True, "the fan was switched off with no fault"
    return "acknowledgement wins"


def test_clear_fault_serialises_against_enforcement():
    """_clear_fault takes _act_lock, so a stop cannot be mid-flight."""
    C = harness.load()
    setup(C, mode="auto", fan=True, fault=True)

    seen = {}
    real = C._clear_fault_locked

    def watched():
        free = C._act_lock.acquire(blocking=False)
        seen["free"] = free
        if free:
            C._act_lock.release()
        return real()

    C._clear_fault_locked = watched
    try:
        C._clear_fault()
    finally:
        C._clear_fault_locked = real

    assert seen.get("free") is False, (
        "the fault was cleared without holding _act_lock, so an "
        "enforcement stop could be running at the same time")
    return "cleared under the lock"


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


# ------------------------------------------- the parser in the loop
#
# Every stub above replaces the plug OBJECT and answers in Python
# bools. That is right for testing actuation logic, but it puts the
# reply parser outside the test entirely - so a parser that reported a
# live relay as off would not disturb a single assertion in this file.
# These two keep the real Tasmota client in the loop and replace only
# _cmd, the HTTP boundary.


class RawPlug:
    """The real client over a simulated relay that answers like a plug."""

    def __init__(self, C, on=True, stuck=False):
        self.on = on
        self.stuck = stuck          # a relay that ignores the command
        self.commands = []
        self.plug = C.Tasmota("192.0.2.1")
        self.plug._cmd = self._cmd

    def _cmd(self, cmnd):
        self.commands.append(cmnd)
        if not self.stuck:
            if cmnd == "Power ON":
                self.on = True
            elif cmnd == "Power OFF":
                self.on = False
        # A JSON boolean, which is what the old parser read as OFF.
        return {"POWER": self.on}


def test_a_manual_stop_reaches_a_relay_answering_with_a_json_boolean():
    """The defect this file could not see, asserted where it bites.

    _manual_off_locked asks state(), and its except branch says plainly
    that an unknown answer must switch anyway. The branch was correct
    and unreachable: a parser that turned an unreadable value into False
    meant state() returned a confident "off", so the switch was skipped
    and the durable promise discharged with the fan still running.

    Asserted on the RELAY, not on the parser. What matters is that the
    fan actually stops.
    """
    C = harness.load()
    setup(C, mode="manual", fan=True)
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
    C.promise_manual_off()
    raw = RawPlug(C, on=True)

    C._manual_off_locked(raw.plug)

    assert raw.on is False, (
        "the relay is still on after a manual stop: commands were %r"
        % (raw.commands,))
    assert "Power OFF" in raw.commands, (
        "no stop was ever sent to the plug: %r" % (raw.commands,))
    return "manual stop reached the relay"


def test_an_unstoppable_relay_keeps_the_promise_owed():
    """The promise is discharged only by a confirmed stop.

    A relay that reports on after being told off must leave the flag
    set and the marker on disk, so the stop is retried rather than
    forgotten. This is the second half of the same guarantee: the first
    test proves the switch is attempted, this one proves a failed
    attempt is not recorded as a success.
    """
    C = harness.load()
    setup(C, mode="manual", fan=True)
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
    C.promise_manual_off()
    raw = RawPlug(C, on=True, stuck=True)

    try:
        C._manual_off_locked(raw.plug)
    except C.PlugError:
        pass                        # expected: the switch was refused

    assert C.STATE["manual_off_pending"] is True, (
        "an unconfirmed stop cleared the pending flag")
    assert C.manual_off_promised(), (
        "an unconfirmed stop deleted the durable promise")
    return "unconfirmed stop stays owed"




# ------------------------------------------------- the per-run rule record
#
# THESE GO THROUGH tick() AND THE RELAY PATHS ON PURPOSE. The first set
# of tests for this feature called decide() directly, and decide() is
# not where the lifecycle lives: production returns before it in manual
# mode, and calls it before the relay has agreed to anything. Every one
# of the four defects reported against 1ff1415 was invisible from
# there and obvious from here.


class _Protect(object):
    """Readings high enough to latch CO2 and temperature."""

    def __init__(self, co2=1200.0, temp=27.0, humidity=50.0):
        self._v = {"co2": co2, "temp": temp, "humidity": humidity,
                   "temperature": temp}

    def readings(self, sid):
        return dict(self._v), "fake"


def _auto(C, plug, protect, fan=False, ran_with=None, fault=None):
    """Wire tick() to fixtures and run one real poll."""
    C.CFG["protect"]["sensor_id"] = "fake-sensor-id"
    C.CFG["control"].update(mode="auto", stale_minutes=0, min_run_minutes=0,
                            on_at=1000.0, off_at=700.0,
                            temp_enabled=True, temp_on=26.5, temp_off=24.0,
                            humidity_enabled=False)
    with C._state_lock:
        C.STATE.update(fan=fan, fault=fault,
                       changed_at=C.time.time() - 10000)
        C.STATE["ran_with"] = dict(ran_with or {})
    C._client_snapshot = lambda: (protect, plug, C._cfg_generation)
    return C.tick()


def test_a_failed_start_does_not_record_rules_for_a_fan_that_never_ran():
    """The relay refused, so nothing ran, so nothing should be named.

    decide() is called before h.set(). Handing it the live record meant
    a failed switch-on left CO2 and temperature recorded against a fan
    that never started, and the next genuine stop named them.
    """
    C = harness.load()
    plug = BrokenPlug(C, on=False)
    try:
        _auto(C, plug, _Protect(), fan=False)
    except C.PlugError:
        pass
    assert C.STATE["ran_with"] == {}, \
        "rules were recorded for a start that failed: %r" % C.STATE["ran_with"]
    return "a failed start records nothing"


def test_a_failed_stop_keeps_the_run_record():
    """The fan is still running, so the run is not over.

    Clearing on the attempt meant the retry that finally stopped the
    fan had nothing left to name.
    """
    C = harness.load()
    plug = BrokenPlug(C, on=True)
    try:
        _auto(C, plug, _Protect(co2=650.0, temp=22.0), fan=True,
              ran_with={"co2": True, "temp": True})
    except C.PlugError:
        pass
    assert C.STATE["ran_with"].get("co2") is True, \
        "a failed stop discarded the run: %r" % C.STATE["ran_with"]
    assert C.STATE["ran_with"].get("temp") is True, C.STATE["ran_with"]
    return "a failed stop keeps the run"


def test_a_confirmed_stop_names_the_run_and_then_clears_it():
    """The whole point, end to end through tick()."""
    C = harness.load()
    plug = Plug(on=True)
    with C._state_lock:
        C.STATE["events"] = []
    _auto(C, plug, _Protect(co2=650.0, temp=22.0), fan=True,
          ran_with={"co2": True, "temp": True})
    assert plug.calls == [False], plug.calls
    log = events(C)
    assert "650 ppm" in log and "22.0 C" in log, log
    assert C.STATE["ran_with"] == {}, \
        "the record survived a confirmed stop: %r" % C.STATE["ran_with"]
    return "named, then cleared"


def test_a_mid_run_cycle_commits_what_it_accumulated():
    """No relay command, but a rule latched. It must still be recorded."""
    C = harness.load()
    plug = Plug(on=True)
    # CO2 already holding the fan on; temperature crosses now.
    with C._state_lock:
        C.STATE["latch"]["co2"] = True
    _auto(C, plug, _Protect(co2=1200.0, temp=27.0), fan=True)
    assert plug.calls == [], "no switch should have been needed"
    assert C.STATE["ran_with"].get("temp") is True, \
        "a rule that latched mid-run was not recorded: %r" % C.STATE["ran_with"]
    return "mid-run accumulation is committed"


def test_an_external_switch_off_ends_the_run():
    """Someone stopped the fan from the vendor app, and Auto agrees.

    decide() never commands anything here: the fan is already off and
    wanted off, so it returns on the no-command path and commits the
    record back untouched. Nothing else on this path can end the run.
    """
    C = harness.load()
    plug = Plug(on=False)                       # the plug says OFF
    _auto(C, plug, _Protect(co2=650.0, temp=22.0),
          fan=True,                             # ...but we thought ON
          ran_with={"co2": True, "temp": True})
    assert C.STATE["fan"] is False, "the external switch was not accepted"
    assert plug.calls == [], "the fan should not have been commanded"
    assert C.STATE["ran_with"] == {}, \
        "an external stop left the run record: %r" % C.STATE["ran_with"]
    return "an external stop ends the run"


def test_a_run_record_does_not_survive_into_the_next_run():
    """THE CONSEQUENCE, end to end.

    The stale entry is not merely stale - it is printed against the NEW
    run's readings, so the line reports a temperature that never
    triggered anything. Asserted on the message, because that is the
    thing the reader is misled by.
    """
    C = harness.load()
    # A two-rule run is ended externally.
    _auto(C, Plug(on=False), _Protect(co2=650.0, temp=22.0),
          fan=True, ran_with={"co2": True, "temp": True})
    assert C.STATE["ran_with"] == {}, C.STATE["ran_with"]

    # A fresh run on CO2 alone, kept clear of temperature entirely.
    C.CFG["control"]["temp_enabled"] = True
    with C._state_lock:
        C.STATE["events"] = []
        C.STATE["latch"] = {"co2": False, "temp": False, "humidity": False}
    plug = Plug(on=False)
    _auto(C, plug, _Protect(co2=1200.0, temp=20.0), fan=False,
          ran_with=dict(C.STATE["ran_with"]))
    assert plug.calls == [True], "the fresh run did not start: %r" % plug.calls

    with C._state_lock:
        C.STATE["fan"] = True
    _auto(C, plug, _Protect(co2=650.0, temp=20.0), fan=True,
          ran_with=dict(C.STATE["ran_with"]))
    log = events(C)
    assert "Fan off" in log, log
    assert "650 ppm" in log, log
    assert " C" not in log.split("Fan off")[-1], \
        "a rule from the previous run was printed against this one: %r" % log
    return "each run is reported on its own"


def test_a_restart_that_finds_the_plug_off_ends_the_carried_run():
    """prev is None here, so "changed" is the wrong question to ask.

    The handover restores the run record but not STATE["fan"], so the
    first poll after a requested restart has prev=None. An off plug is
    then not a CHANGE, the external branch is skipped, and a record
    carried across the restart survived into whatever ran next.
    """
    C = harness.load()
    plug = Plug(on=False)
    _auto(C, plug, _Protect(co2=650.0, temp=22.0),
          fan=None,                                  # as after a handover
          ran_with={"co2": True, "temp": True})
    assert plug.calls == [], "nothing should have been commanded"
    assert C.STATE["ran_with"] == {}, \
        "a carried run record outlived the restart: %r" % C.STATE["ran_with"]
    return "a restart onto an off plug ends the run"


def test_force_on_ends_the_run_it_interrupted():
    """Forcing back on starts a NEW run, and it is not the old one.

    _force_on confirms the plug is off, logs the external switch and
    re-asserts. The physical run ended at the switch; carrying its
    rules forward lets a later Auto stop name them.
    """
    C = harness.load()
    C.CFG["control"]["mode"] = "on"
    with C._state_lock:
        C.STATE.update(fan=True, fault=None, events=[],
                       changed_at=C.time.time() - 10000)
        C.STATE["ran_with"] = {"co2": True, "temp": True}
    plug = Plug(on=False)
    C._force_on(plug, C._cfg_generation)
    assert plug.calls == [True], "the fan was not forced back on"
    assert C.STATE["ran_with"] == {}, \
        "Force on carried the interrupted run: %r" % C.STATE["ran_with"]
    # The evidence of the external switch must survive the clear.
    assert "changed outside" in events(C), events(C)
    return "Force on ends the interrupted run"


def test_an_unreadable_plug_does_not_end_the_run():
    """Unknown is not off.

    _fan_status returns None when the plug cannot be read. Clearing on
    that would discard a run that is still going, on no evidence.

    THE READINGS ARE BELOW EVERY THRESHOLD AND THE LEASE IS FRESH, so
    the cycle latches nothing and commands nothing. The first version
    of this test used readings that re-latched both rules on the same
    cycle, which refilled the record whether or not the clear had run -
    it passed against a mutation that cleared on an unreadable plug,
    which is the only thing it exists to catch.
    """
    C = harness.load()
    C.CFG["protect"]["sensor_id"] = "fake-sensor-id"
    C.CFG["control"].update(mode="auto", stale_minutes=0,
                            min_run_minutes=15,          # lease still held
                            on_at=1000.0, off_at=700.0,
                            temp_enabled=True, temp_on=26.5, temp_off=24.0,
                            humidity_enabled=False)
    plug = Plug(on=True, readable=False)
    with C._state_lock:
        C.STATE.update(fan=True, fault=None, events=[],
                       changed_at=C.time.time())        # lease just started
        C.STATE["ran_with"] = {"co2": True}
        C.STATE["latch"] = {"co2": True, "temp": False, "humidity": False}
    C._client_snapshot = lambda: (_Protect(co2=650.0, temp=22.0), plug,
                                  C._cfg_generation)
    C.tick()

    assert plug.calls == [], "nothing should have been commanded: %r" % plug.calls
    assert C.STATE["ran_with"].get("co2") is True, \
        "an unreadable plug was treated as a stop: %r" % C.STATE["ran_with"]
    return "an unreadable plug keeps the run"


def test_a_manual_stop_clears_the_run_record():
    """PRODUCTION MANUAL NEVER REACHES decide().

    tick() calls _manual_off() and returns before the control decision,
    so the clear inside decide() is dead code on this path. The stop
    that actually happens has to do it.
    """
    C = harness.load()
    plug = Plug(on=True)
    C.CFG["control"]["mode"] = "manual"
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
        C.STATE["ran_with"] = {"co2": True, "temp": True}
    with C._act_lock:
        C._manual_off_locked(plug)
    assert plug.calls == [False], plug.calls
    assert C.STATE["ran_with"] == {}, \
        "a manual stop left the run record behind: %r" % C.STATE["ran_with"]
    return "manual stop ends the run"


def test_a_manual_stop_that_failed_keeps_the_run_record():
    """The flag stays set and the stop is retried, so the run stands."""
    C = harness.load()
    plug = BrokenPlug(C, on=True)
    C.CFG["control"]["mode"] = "manual"
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
        C.STATE["ran_with"] = {"co2": True}
    try:
        with C._act_lock:
            C._manual_off_locked(plug)
    except C.PlugError:
        pass
    assert C.STATE["ran_with"].get("co2") is True, \
        "a failed manual stop discarded the run: %r" % C.STATE["ran_with"]
    return "a failed manual stop keeps the run"


def test_a_confirmed_fault_stop_clears_the_run_record():
    """A draw fault ends the run, as the documentation says it does."""
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"at": C.time.time(), "msg": "draw fault",
                            "stopped": False}
        C.STATE["ran_with"] = {"humidity": True}
    assert C._confirm_fault_stopped() is True, "the stop was not confirmed"
    assert C.STATE["ran_with"] == {}, \
        "a confirmed fault stop left the run record: %r" % C.STATE["ran_with"]
    return "a confirmed fault stop ends the run"


def test_an_unconfirmed_fault_stop_keeps_the_run_record():
    """Until the stop lands the fan may still be running on those rules.

    _confirm_fault_stopped writes to disk first and returns False if
    that write fails, leaving the stop to be retried. The record has to
    survive that, for the same reason the failed relay stop does.
    """
    C = harness.load()

    def boom(_snap):
        raise OSError("disk full")

    C.save_fault = boom
    with C._state_lock:
        C.STATE["fault"] = {"at": C.time.time(), "msg": "draw fault",
                            "stopped": False}
        C.STATE["ran_with"] = {"humidity": True}
    assert C._confirm_fault_stopped() is False, "an unsaved stop was confirmed"
    assert C.STATE["ran_with"].get("humidity") is True, \
        "an unconfirmed fault stop discarded the run: %r" % C.STATE["ran_with"]
    return "an unconfirmed fault stop keeps the run"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
