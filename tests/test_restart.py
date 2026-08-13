"""The restart trigger: taken once, never twice, never a loop.

_restart_now is never called here. It ends in execv, which would
replace the test process itself - so what is tested is the decision to
restart, not the exec. The exec path is four lines and is exercised for
real every time the trigger is used.
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def boom(*a, **k):
    raise OSError(13, "Permission denied")


def test_no_request_is_not_a_restart():
    C = harness.load()
    assert not os.path.exists(C.RESTART_PATH), "test dir started dirty"
    assert C._consume_restart_request() is False, \
        "a restart was taken with no trigger present"
    return "absent trigger ignored"


def test_fresh_request_is_taken_and_removed():
    """Freshness is set explicitly, not left to the clock.

    _consume_restart_request compares the file's mtime against
    _PROC_START, so "fresh" has to be stated rather than assumed.
    Writing the file and hoping its timestamp lands after a value
    captured at import is a race with a margin of about a millisecond -
    the time harness.load() takes. time.time() here was no better: it
    re-reads the same clock that _PROC_START came from, so a backward
    step between the two puts it on the wrong side just the same.

    Anchoring to _PROC_START removes the clock from the test entirely.
    The stale case below has always done this; this is the pair being
    made symmetric.
    """
    C = harness.load()
    open(C.RESTART_PATH, "w").close()
    fresh = C._PROC_START + 1
    os.utime(C.RESTART_PATH, (fresh, fresh))

    assert C._consume_restart_request() is True, \
        "a fresh trigger was not taken"
    assert not os.path.exists(C.RESTART_PATH), \
        "the trigger survived, so the next process would restart again"
    return "taken once, file removed"


def test_a_taken_request_does_not_fire_twice():
    """See the note above on why the timestamp is set rather than taken.

    This one had no os.utime at all and relied on the filesystem
    stamping the new file later than _PROC_START. On ext4 it does, by
    roughly a millisecond. Under WSL it consistently does NOT - the
    reviewer measured 0.1 to 3 ms EARLY - so the first consume returned
    False and the test failed every run on that platform.
    """
    C = harness.load()
    open(C.RESTART_PATH, "w").close()
    fresh = C._PROC_START + 1
    os.utime(C.RESTART_PATH, (fresh, fresh))

    assert C._consume_restart_request() is True, "first call missed it"
    assert C._consume_restart_request() is False, \
        "the same trigger fired twice"
    return "second call is a no-op"


def test_stale_request_is_removed_without_restarting():
    """A trigger predating startup was handled by the previous process.

    exec keeps the working directory, so without the mtime check the
    replacement would find the same file and restart again immediately.
    """
    C = harness.load()
    open(C.RESTART_PATH, "w").close()
    old = C._PROC_START - 60
    os.utime(C.RESTART_PATH, (old, old))

    assert C._consume_restart_request() is False, \
        "a trigger older than the process caused a restart - boot loop"
    assert not os.path.exists(C.RESTART_PATH), \
        "the stale trigger was left behind to be found again"
    return "stale trigger dropped, no restart"


def test_unremovable_request_is_refused_not_retried():
    """If the file cannot be deleted, restarting would loop forever."""
    C = harness.load()
    open(C.RESTART_PATH, "w").close()
    real = C.os.unlink
    C.os.unlink = boom
    try:
        taken = C._consume_restart_request()
    finally:
        C.os.unlink = real

    assert taken is False, \
        "restarted on a trigger that could not be removed - boot loop"
    logged = " ".join(e["msg"] for e in C.STATE["events"])
    assert "Restart request ignored" in logged, \
        "a refused restart request was not logged"
    os.unlink(C.RESTART_PATH)
    return "refused and logged"


def test_watcher_is_started():
    """The trigger is useless if nothing looks for it."""
    C = harness.load()
    assert callable(C.restart_loop), "restart_loop is missing"
    src = open(os.path.join(C.DIR, "controller.py"), encoding="utf-8").read()
    assert "target=restart_loop" in src, \
        "restart_loop is never started, so the trigger is never read"
    return "watcher thread is wired up"


def test_open_sockets_do_not_survive_the_exec():
    """The bug that made the first live restart a crash.

    werkzeug marks its listening socket inheritable, so without this
    the replacement process inherits a socket already bound to 8096
    and dies with "Address already in use".
    """
    if not os.path.isdir("/proc/self/fd"):
        harness.skip("needs Linux /proc/self/fd; run this on the host")
    C = harness.load()
    s = socket.socket()
    try:
        s.set_inheritable(True)
        assert s.get_inheritable() is True, "test set-up did not take"
        before = (os.get_inheritable(1), os.get_inheritable(2))

        C._mark_fds_close_on_exec()

        assert s.get_inheritable() is False, \
            "an open socket would survive the exec and hold the port"
        assert (os.get_inheritable(1), os.get_inheritable(2)) == before, \
            "stdout/stderr were altered, so the replacement loses logging"
    finally:
        s.close()
    return "socket closed on exec, journal kept"


class ExecCalled(Exception):
    """Raised by the stub in place of execv, which never returns."""


def _stub_exec(C):
    """Replace execv so the restart path can be run without losing the
    test process. Returns the list the call is recorded in."""
    calls = []

    def fake(*a, **k):
        calls.append(a)
        raise ExecCalled()

    C.os.execv = fake
    return calls


def test_restart_is_refused_while_a_fault_is_unsaved():
    """The P1 from review 21.

    A fault that never reached fault.json exists only in this process.
    Restarting drops it, and Auto is then free to start a fan that was
    stopped for drawing wrong.
    """
    C = harness.load()
    real = C.os.execv
    calls = _stub_exec(C)
    try:
        with C._state_lock:
            C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
            C.STATE["fault_unsaved"] = True
        result = C._restart_now()
    finally:
        C.os.execv = real

    assert result is False, "a restart went ahead on an unsaved fault"
    assert not calls, "exec was reached despite the refusal"
    logged = " ".join(e["msg"] for e in C.STATE["events"])
    assert "Restart refused" in logged, "the refusal was not logged"
    assert C.RESUME_ENV not in C.os.environ, \
        "handover data was left behind by a refused restart"
    return "refused, logged, nothing left behind"


def test_restart_proceeds_once_the_fault_is_saved():
    """The guard must not block an ordinary restart."""
    C = harness.load()
    real = C.os.execv
    calls = _stub_exec(C)
    try:
        with C._state_lock:
            C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
            C.STATE["fault_unsaved"] = False
        raised = False
        try:
            C._restart_now()
        except ExecCalled:
            raised = True
    finally:
        C.os.execv = real
        C.os.environ.pop(C.RESUME_ENV, None)

    assert raised and calls, "a saved fault blocked a legitimate restart"
    return "a persisted fault does not block a restart"


def test_handover_carries_the_latches_and_the_lease():
    """The P2 from review 21: without this a restart re-decides from
    scratch, releasing a latch and cancelling the minimum-run lease."""
    C = harness.load()
    with C._state_lock:
        C.STATE["latch"]["co2"] = True
        C.STATE["latch"]["temp"] = False
        C.STATE["changed_at"] = 111.0
        C.STATE["reading_at"] = 222.0
    C._oob_since = 333.0

    carried = json.dumps(C._resume_payload())

    with C._state_lock:
        C.STATE["latch"]["co2"] = False
        C.STATE["changed_at"] = None
        C.STATE["reading_at"] = None
    C._oob_since = None

    C.os.environ[C.RESUME_ENV] = carried
    assert C._apply_resume() is True, "the handover was not taken"

    assert C.STATE["latch"]["co2"] is True, \
        "a latched rule released, so a fan running on demand would stop"
    assert C.STATE["latch"]["temp"] is False, "an unlatched rule was set"
    assert C.STATE["changed_at"] == 111.0, \
        "the minimum-run lease was lost, so the motor can short-cycle"
    assert C.STATE["reading_at"] == 222.0, "the fail-safe clock reset"
    assert C._oob_since == 333.0, "the draw fault restarted its clock"
    return "latches, lease, draw and fail-safe clocks all carried"


def test_handover_is_consumed_exactly_once():
    """Left in place it would resume a lease that expired long ago."""
    C = harness.load()
    C.os.environ[C.RESUME_ENV] = json.dumps(C._resume_payload())

    assert C._apply_resume() is True, "first call did not take it"
    assert C.RESUME_ENV not in C.os.environ, "the handover was left set"
    assert C._apply_resume() is False, "the same handover applied twice"
    return "taken once, then gone"


def test_malformed_handover_is_ignored_not_fatal():
    """A clean slate is the old behaviour; refusing to start is worse."""
    C = harness.load()
    for bad in ("not json at all", "[]", '{"v": 99}', '{"v": 1, "latch": 5}'):
        C.os.environ[C.RESUME_ENV] = bad
        C._apply_resume()
        assert C.RESUME_ENV not in C.os.environ, \
            "bad handover %r was left in the environment" % bad
    return "four malformed payloads survived"


def test_handover_rejects_values_that_are_not_numbers():
    """Garbage in a timestamp would break the arithmetic that reads it."""
    C = harness.load()
    C.os.environ[C.RESUME_ENV] = json.dumps({
        "v": 1, "latch": {}, "changed_at": "soon",
        "reading_at": True, "oob_since": [], "start_time": None})
    C._apply_resume()

    assert C.STATE["changed_at"] is None, "a string was taken as a time"
    assert C.STATE["reading_at"] is None, "a bool was taken as a time"
    assert C._oob_since is None, "a list was taken as a time"
    return "non-numeric times rejected"


def test_a_failed_history_write_is_reported():
    """The other P2: save_hist swallows OSError, so the restart path
    could not tell whether the flush landed."""
    C = harness.load()
    assert C.flush_hist() is True, "a healthy flush reported failure"

    C.HIST_PATH = "/nonexistent-directory-for-tests/history.json"
    assert C.flush_hist() is False, \
        "a failed history write reported success"
    return "success and failure both reported"


def test_non_finite_times_are_rejected():
    """json.loads parses bare NaN and Infinity, and both are floats.

    A NaN lease is the worst case: every comparison against it is
    False, so a minimum-run lease would neither hold nor expire, and
    nothing in the state dump would look wrong.
    """
    C = harness.load()
    for bad in ("NaN", "Infinity", "-Infinity"):
        C.os.environ[C.RESUME_ENV] = (
            '{"v": 1, "latch": {}, "changed_at": %s, "reading_at": %s,'
            ' "oob_since": %s, "start_time": %s}' % (bad, bad, bad, bad))
        start_before = C.START_TIME
        C._apply_resume()

        assert C.STATE["changed_at"] is None, "%s taken as a lease" % bad
        assert C.STATE["reading_at"] is None, "%s taken as a reading" % bad
        assert C._oob_since is None, "%s taken as a draw timer" % bad
        assert C.START_TIME == start_before, "%s taken as a start" % bad
    return "NaN, +inf and -inf all rejected"


def test_latch_restore_requires_real_booleans():
    """bool('false') is True, and a latch is what keeps the fan on."""
    C = harness.load()
    C.os.environ[C.RESUME_ENV] = json.dumps({
        "v": 1,
        "latch": {"co2": "false", "temp": 1, "humidity": None},
        "changed_at": None, "reading_at": None,
        "oob_since": None, "start_time": None})
    C._apply_resume()

    for k in ("co2", "temp", "humidity"):
        assert C.STATE["latch"][k] is False, \
            "latch %s was restored from a non-boolean" % k
    return "truthy non-booleans do not latch"


def test_unsaved_fault_is_written_on_shutdown():
    """A signalled shutdown cannot refuse, so it must try once more.

    The write that failed may have failed minutes ago, and the disk may
    since have recovered.
    """
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True

    assert C._persist_fault_on_exit() is True, \
        "the last-chance write did not happen"
    assert C.STATE["fault_unsaved"] is False, "the flag was left set"
    assert C.load_fault() is not None, \
        "the fault did not reach disk, so the next start would not hold"
    return "recovered disk means the fault survives"


def test_shutdown_without_an_unsaved_fault_writes_nothing():
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fault_unsaved"] = False
    assert C._persist_fault_on_exit() is False, \
        "wrote a fault when there was nothing owed"
    return "no fault, no write"


def test_shutdown_reports_a_fault_it_cannot_save():
    """If it still cannot be written the fault is genuinely lost, and
    that has to be said out loud rather than swallowed."""
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True
    C.FAULT_PATH = "/nonexistent-directory-for-tests/fault.json"

    assert C._persist_fault_on_exit() is False, \
        "claimed success on a write that could not land"
    assert C.STATE["fault_unsaved"] is True, \
        "cleared the flag despite the write failing"
    return "still unsaved, and flagged as such"


def test_a_clear_during_the_last_chance_write_is_not_undone():
    """The race the bounded lock exists to prevent.

    The first version read the fault without the lock. An
    acknowledgement landing between the read and the write meant the
    old snapshot was written back, and the fault returned from the dead
    at the next start with nothing to say why.
    """
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True
    C.save_fault(C.STATE["fault"])

    # Acknowledge it the ordinary way. _clear_fault takes _fault_lock,
    # so the last-chance write cannot interleave with it.
    had, ok = C._clear_fault()
    assert had is not None and ok, "the clear did not take"

    C._persist_fault_on_exit()

    assert C.load_fault() is None, \
        "a cleared fault was written back to disk and would return"
    return "acknowledgement survives the last-chance write"


def test_last_chance_write_gives_up_rather_than_hanging():
    """A signal handler must not wait until systemd escalates."""
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True

    C._fault_lock.acquire()
    started = time.time()
    try:
        result = C._persist_fault_on_exit()
    finally:
        C._fault_lock.release()
    took = time.time() - started

    assert result is False, "claimed a write it could not have made"
    assert took < 10, "waited %.1fs, long enough to be SIGKILLed" % took
    assert C.STATE["fault_unsaved"] is True, \
        "cleared the flag without writing"
    return "gave up after %.1fs" % took


def test_last_chance_write_still_works_when_the_lock_is_free():
    """The bound must not break the ordinary case."""
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True

    assert C._persist_fault_on_exit() is True, "the write did not happen"
    assert C.load_fault() is not None, "the fault did not reach disk"
    assert C._fault_lock.acquire(timeout=1), "the lock was not released"
    C._fault_lock.release()
    return "written, and the lock released"


def test_state_reports_a_process_generation():
    """restart.sh proves a restart with this. The port answering proves
    nothing: a failed exec leaves the original process serving."""
    C = harness.load()
    app = C.app.test_client()
    body = app.get("/api/state").get_json()

    assert "process_started_at" in body, \
        "no generation marker, so a restart cannot be proved"
    assert body["process_started_at"] == C._PROC_START, \
        "the marker does not match this process"
    assert isinstance(body["process_started_at"], (int, float)), \
        "the marker is not a number and cannot be compared"
    return "generation exposed and matches"


def test_the_last_chance_write_holds_the_fault_lock():
    """The real pin for the clear-during-write race.

    The end-state test above passes against the broken code too - by
    the time the clear has finished, the early return catches it. What
    actually went wrong was narrower: the snapshot was read, an
    acknowledgement landed, and the stale snapshot was written back.
    The only thing that makes that impossible is holding _fault_lock
    across the read and the write, so that is what is asserted.

    _fault_lock is a plain Lock, so a non-reentrant acquire from this
    same thread answers the question directly: it fails while held.
    """
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = True

    seen = {}
    real_save = C.save_fault

    def watched(f):
        got = C._fault_lock.acquire(blocking=False)
        seen["lock_was_free"] = got
        if got:
            C._fault_lock.release()
        return real_save(f)

    C.save_fault = watched
    try:
        C._persist_fault_on_exit()
    finally:
        C.save_fault = real_save

    assert seen.get("lock_was_free") is False, \
        ("the write ran without _fault_lock held, so an acknowledgement "
         "could land between the read and the write and be undone")
    return "lock held across read and write"


def test_a_restart_cannot_land_in_the_middle_of_raising_a_fault():
    """The window _fault_lock closes.

    _store_fault holds the lock across both the memory update and the
    write. Checking fault_unsaved under _state_lock alone let a restart
    read False - because the write had not failed YET - and exec away
    the only copy of a live fault. The replacement would load nothing
    and Auto would be free to start a fan judged faulty.

    Driven with an event rather than a sleep: the writer is provably
    inside save_fault, holding the lock, before the restart is tried.
    """
    C = harness.load()
    real = C.os.execv
    calls = _stub_exec(C)

    inside = threading.Event()
    release = threading.Event()

    def blocking_save(value):
        if value is not None:
            inside.set()
            release.wait(10)
            raise OSError(28, "No space left on device")

    C.save_fault = blocking_save

    def raise_fault():
        C._store_fault({"msg": "draw fault", "stopped": False})

    writer = threading.Thread(target=raise_fault, name="draw")
    writer.start()
    try:
        assert inside.wait(10), "the writer never reached save_fault"

        # The fault is live in memory and the flag is still False, which
        # is exactly the state the old check would have restarted on.
        with C._state_lock:
            assert C.STATE["fault"] is not None, "no fault in memory"
            assert C.STATE["fault_unsaved"] is False, (
                "the flag was already set, so this is not the window "
                "being tested")

        result = C._restart_now()
    finally:
        release.set()
        writer.join(10)
        C.os.execv = real

    assert not calls, (
        "exec was reached while a fault was mid-write - the fault would "
        "have been lost")
    assert result is False, "did not report the restart as refused"
    logged = " ".join(e["msg"] for e in C.STATE["events"])
    assert "Restart refused" in logged, "the refusal was not logged"
    return "refused while the write was in flight"


def test_the_restart_check_is_not_just_a_flag_read():
    """Pins the lock, not the symptom.

    A future edit could restore the cheap check and this suite would
    otherwise only notice through the timing test above.
    """
    C = harness.load()
    real = C.os.execv
    _stub_exec(C)
    seen = {}
    real_flush = C.flush_hist

    def watched():
        # Reached only on the path to the exec, and only with the lock
        # held, or the window above is open again.
        seen["fault_lock_free"] = C._fault_lock.acquire(blocking=False)
        if seen["fault_lock_free"]:
            C._fault_lock.release()
        return real_flush()

    C.flush_hist = watched
    try:
        with C._state_lock:
            C.STATE["fault"] = None
            C.STATE["fault_unsaved"] = False
        try:
            C._restart_now()
        except ExecCalled:
            pass
    finally:
        C.flush_hist = real_flush
        C.os.execv = real
        C.os.environ.pop(C.RESUME_ENV, None)

    assert seen.get("fault_lock_free") is False, (
        "the restart ran without _fault_lock held, so a fault being "
        "written could be exec'd away")
    return "lock held on the way to the exec"


def test_a_promised_manual_stop_survives_a_requested_restart():
    """Manual switches the fan off once and then hands over.

    A restart between selecting Manual and the stop landing used to drop
    the promise, leaving the fan running in the one mode whose whole
    purpose is to stop touching it.
    """
    C = harness.load()
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
    carried = json.dumps(C._resume_payload())

    with C._state_lock:
        C.STATE["manual_off_pending"] = False
    C.os.environ[C.RESUME_ENV] = carried

    assert C._apply_resume() is True, "the handover was not taken"
    assert C.STATE["manual_off_pending"] is True, (
        "the promised stop was dropped, so the fan keeps running in "
        "Manual with nothing left to correct it")
    return "the promise is kept across the exec"


def test_a_manual_stop_that_is_not_owed_is_not_invented():
    C = harness.load()
    C.os.environ[C.RESUME_ENV] = json.dumps({
        "v": 1, "latch": {}, "changed_at": None, "reading_at": None,
        "manual_off_pending": "yes", "oob_since": None,
        "start_time": None})
    C._apply_resume()

    assert C.STATE["manual_off_pending"] is False, (
        "a non-boolean promised a stop that was never owed")
    return "truthy is not a promise"


def test_the_restored_draw_timer_survives_the_first_draw_check():
    """Restoring the timer is not enough; it has to still be there.

    _apply_resume restored _oob_since, and the test for it stopped
    at that. But _oob_generation was left at None, which differs from
    generation 0, so the FIRST _draw_check treated the carried timer as
    belonging to another config and reset it. A fan thirty seconds from
    a draw fault got a fresh grace period on every ./restart.sh - the
    opposite of what a requested restart promises.

    So this asserts the timer after a draw check, not after the resume.
    """
    C = harness.load()
    now = C.time.time()

    class Plug:
        def state(self):
            return True

        def set(self, on):
            return on

        def power_w(self):
            return 99.0

    C.CFG["plug"].update(watts_enabled=True, watts_min=10.0,
                         watts_max=20.0, watts_fault_seconds=60)
    C.CFG["control"]["mode"] = "auto"
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = True
        C.STATE["watts"] = 99.0
        C.STATE["watts_at"] = now
        C.STATE["changed_at"] = now - 10000

    # Thirty seconds already accumulated, carried the way a real
    # handover carries it - through _apply_resume, not by assignment.
    # Setting the globals by hand would test the fix instead of the
    # path, and the bug was that the PATH left _oob_generation at None.
    os.environ[C.RESUME_ENV] = json.dumps({
        "v": 1,
        "latch": {"co2": True},
        "changed_at": now - 10000,
        "reading_at": now,
        "manual_off_pending": False,
        "oob_since": now - 30,
        "start_time": now - 10000,
    })
    assert C._apply_resume() is True, "the handover was not applied"
    assert C._oob_since is not None, "_apply_resume dropped the timer"

    C._draw_check(Plug(), C._cfg_generation)

    assert C._oob_since is not None, "the restored timer was discarded"
    elapsed = C.time.time() - C._oob_since
    assert elapsed > 25, (
        "the timer restarted: %.1fs accumulated, expected about 30" % elapsed)

    # And it still faults on the REMAINING delay rather than a fresh one.
    C._oob_since = C.time.time() - 61
    C._draw_check(Plug(), C._cfg_generation)
    assert C.STATE["fault"] is not None, \
        "no fault once the carried timer passed watts_fault_seconds"
    return "timer carried, fault still due"


def test_a_genuinely_new_generation_still_resets_the_timer():
    """The guard must not be disabled by the fix above.

    Seconds accumulated against one plug and one draw band must not be
    spent against another's limits.
    """
    C = harness.load()
    now = C.time.time()

    class Plug:
        def state(self):
            return True

        def set(self, on):
            return on

        def power_w(self):
            return 99.0

    C.CFG["plug"].update(watts_enabled=True, watts_min=10.0,
                         watts_max=20.0, watts_fault_seconds=60)
    C.CFG["control"]["mode"] = "auto"
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["fan"] = True
        C.STATE["watts"] = 99.0
        C.STATE["watts_at"] = now
        C.STATE["changed_at"] = now - 10000

    C._oob_since = now - 30
    C._oob_generation = C._cfg_generation
    C._cfg_generation += 1              # a save lands: new plug, new band

    C._draw_check(Plug(), C._cfg_generation)
    assert C._oob_since is None or (C.time.time() - C._oob_since) < 5, (
        "seconds accumulated under the old config carried into the new "
        "one")
    assert C.STATE["fault"] is None, "faulted on the old config's timer"
    return "new config, new timer"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
