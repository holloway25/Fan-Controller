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
    C = harness.load()
    open(C.RESTART_PATH, "w").close()
    os.utime(C.RESTART_PATH, (time.time(), time.time()))

    assert C._consume_restart_request() is True, \
        "a fresh trigger was not taken"
    assert not os.path.exists(C.RESTART_PATH), \
        "the trigger survived, so the next process would restart again"
    return "taken once, file removed"


def test_a_taken_request_does_not_fire_twice():
    C = harness.load()
    open(C.RESTART_PATH, "w").close()

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


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
