"""The durable promise of Manual's one-time stop, and the fsync that
makes a fault's durability honest.

Both are about a failure nobody sees at the time: a fan left running in
Manual after a power cut, and a fault reported saved that was not.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def boom(*a, **k):
    raise OSError(5, "Input/output error")


# ------------------------------------------------- the manual-off promise

def test_the_promise_is_on_disk_before_manual_is_committed():
    """A crash between the two would otherwise store Manual with the
    stop already forgotten."""
    C = harness.load()
    C.CFG["control"]["mode"] = "auto"   # the example config starts manual
    seen = {}
    real_save = C.save_cfg

    def watched(cfg):
        seen["promised_first"] = C.manual_off_promised()
        return real_save(cfg)

    C.save_cfg = watched
    try:
        app = C.app.test_client()
        r = app.post("/api/config", json={"control": {"mode": "manual"}})
    finally:
        C.save_cfg = real_save

    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen.get("promised_first") is True, (
        "Manual was committed before the promise reached disk")
    return "promise written first"


def test_a_promise_that_cannot_be_written_refuses_the_mode_change():
    """No promise and no Manual beats Manual with the promise lost."""
    C = harness.load()
    C.CFG["control"]["mode"] = "auto"   # the example config starts manual
    real = C.promise_manual_off
    C.promise_manual_off = boom
    try:
        r = C.app.test_client().post("/api/config",
                                     json={"control": {"mode": "manual"}})
    finally:
        C.promise_manual_off = real

    assert r.status_code == 500, "the mode change was allowed anyway"
    assert C.CFG["control"]["mode"] != "manual", (
        "Manual was committed despite the promise failing")
    return "refused, and nothing changed"


def test_a_promise_survives_to_the_next_start():
    """The crash case: Manual stored, stop never made."""
    C = harness.load()
    C.promise_manual_off()
    C.CFG["control"]["mode"] = "manual"

    assert C.manual_off_promised() is True, "the marker did not persist"
    return "still owed after the process is gone"


def test_the_promise_is_discharged_only_once_the_plug_confirms():
    C = harness.load()

    class Plug:
        def __init__(self):
            self.calls = []

        def state(self):
            return True

        def set(self, v):
            self.calls.append(v)
            return v

    C.promise_manual_off()
    C.CFG["control"]["mode"] = "manual"
    with C._state_lock:
        C.STATE["manual_off_pending"] = True

    plug = Plug()
    C._manual_off(plug)

    assert plug.calls == [False], "the fan was not stopped: %r" % plug.calls
    assert C.manual_off_promised() is False, (
        "the promise outlived the stop, so the next start would stop the "
        "fan a second time")
    return "discharged on confirmation"


def test_a_failed_stop_leaves_the_promise_standing():
    """A stop the plug never confirmed is still owed."""
    C = harness.load()

    class BrokenPlug:
        def __init__(self, err):
            self._err = err

        def state(self):
            return True

        def set(self, v):
            raise self._err("unreachable")

    C.promise_manual_off()
    C.CFG["control"]["mode"] = "manual"
    with C._state_lock:
        C.STATE["manual_off_pending"] = True

    try:
        C._manual_off(BrokenPlug(C.PlugError))
    except C.PlugError:
        pass

    assert C.manual_off_promised() is True, (
        "the promise was discharged by a stop that never happened")
    assert C.STATE["manual_off_pending"] is True, "the flag was cleared too"
    return "still owed after a failed stop"


# ------------------------------------------------ the directory fsync

def failing_dir_open(C):
    """Break only the directory open, leaving ordinary file writes alone.

    Deliberately NOT a stub for _fsync_dir. Replacing that function
    proves "if it raises, the fault stays flagged" - which was true even
    when it swallowed everything, so the test passed against the defect.
    Breaking the syscall underneath it tests the function instead of
    assuming it.
    """
    real_open = C.os.open

    def picky(path, flags, *a):
        if flags & os.O_DIRECTORY:
            raise OSError(5, "Input/output error")
        return real_open(path, flags, *a)

    return real_open, picky


def test_the_directory_sync_reports_failure_rather_than_hiding_it():
    """Directly: a sync that cannot happen must raise."""
    C = harness.load()
    not_a_directory = os.path.join(C.DIR, "controller.py")

    raised = False
    try:
        C._fsync_dir(not_a_directory)
    except OSError:
        raised = True

    assert raised, (
        "syncing something that is not a directory reported success, so "
        "a real durability failure would too")
    return "raises instead of pretending"


def test_a_fault_is_not_reported_saved_if_the_directory_sync_fails():
    """The whole point of syncing the directory.

    Swallowing the error reported success, _store_fault cleared
    fault_unsaved, and a restart went ahead on a fault that might never
    have reached disk.
    """
    C = harness.load()
    real_open, picky = failing_dir_open(C)
    C.os.open = picky
    try:
        C._store_fault({"msg": "draw fault", "stopped": False})
    finally:
        C.os.open = real_open

    assert C.STATE["fault_unsaved"] is True, (
        "a fault whose directory entry was never committed was reported "
        "as saved")
    return "still flagged unsaved"


def test_a_restart_is_refused_after_a_failed_directory_sync():
    """The consequence that matters: the fault must not be exec'd away."""
    C = harness.load()
    real_open, picky = failing_dir_open(C)
    real_exec = C.os.execv
    calls = []

    def fake_exec(*a, **k):
        calls.append(a)
        raise AssertionError("exec should not have been reached")

    C.os.open = picky
    try:
        C._store_fault({"msg": "draw fault", "stopped": False})
    finally:
        C.os.open = real_open

    assert C.STATE["fault_unsaved"] is True, "set-up did not flag it"

    C.os.execv = fake_exec
    try:
        result = C._restart_now()
    finally:
        C.os.execv = real_exec

    assert not calls, "restarted on a fault that may not be on disk"
    assert result is False, "the restart was not reported as refused"
    return "refused"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
