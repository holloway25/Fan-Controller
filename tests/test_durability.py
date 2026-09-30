"""The durable promise of Manual's one-time stop, and the fsync that
makes a fault's durability honest.

Both are about a failure nobody sees at the time: a fan left running in
Manual after a power cut, and a fault reported saved that was not.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def boom(*a, **k):
    raise OSError(5, "Input/output error")


def need_dir_sync():
    """Skip where a directory cannot be opened for syncing.

    Everything in this module reaches _fsync_dir, directly or through a
    durable write, and that needs O_DIRECTORY. It exists on Linux, which
    is what this runs on; on a Windows review host these would raise
    AttributeError and read as failures rather than as untested.
    """
    if not hasattr(os, "O_DIRECTORY"):
        harness.skip("directory fsync needs Linux O_DIRECTORY")


# -------------------------------------------- durable temporary permissions

def test_a_permissive_stale_temp_cannot_expose_the_replacement():
    """A leftover path must not decide the mode of the next durable file.

    os.open(..., O_CREAT, 0600) applies the mode only when it creates a file.
    Reusing a stale 0644 temp therefore made the replacement 0644 too, which
    is unsafe for config.json because it contains credentials.
    """
    need_dir_sync()
    C = harness.load()
    stale = C.CFG_PATH + ".tmp"
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("stale")
    os.chmod(stale, 0o644)

    C.write_json_durable(C.CFG_PATH, {"test": True},
                         mode=0o600)

    actual = os.stat(C.CFG_PATH).st_mode & 0o777
    assert actual == 0o600, "replacement mode is %04o" % actual
    with open(stale, encoding="utf-8") as fh:
        stale_content = fh.read()
    assert stale_content == "stale", "the fixed stale path was reused"
    return "unique replacement stayed 0600"


# ------------------------------------------------- the manual-off promise

def test_the_promise_is_on_disk_before_manual_is_committed():
    """A crash between the two would otherwise store Manual with the
    stop already forgotten."""
    need_dir_sync()
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
    need_dir_sync()
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
    need_dir_sync()
    C = harness.load()
    C.promise_manual_off()
    C.CFG["control"]["mode"] = "manual"

    assert C.manual_off_promised() is True, "the marker did not persist"
    return "still owed after the process is gone"


def test_the_promise_is_discharged_only_once_the_plug_confirms():
    need_dir_sync()
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
    need_dir_sync()
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
    need_dir_sync()
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
    need_dir_sync()
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
    need_dir_sync()
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


def test_a_save_that_lands_but_cannot_be_synced_is_not_called_a_failure():
    """The divided state: file replaced, directory sync failed.

    Reporting "Nothing was changed" while config.json already holds the
    new settings means a restart applies a change the caller was told
    had been rejected.
    """
    need_dir_sync()
    C = harness.load()
    real_open, picky = failing_dir_open(C)

    C.os.open = picky
    try:
        r = C.app.test_client().post("/api/config",
                                     json={"control": {"on_at": 1234}})
    finally:
        C.os.open = real_open

    body = r.get_json()
    assert "Nothing was changed" not in str(body), (
        "claimed nothing changed: %r" % body)
    assert r.status_code == 200, "reported a failure: %s" % r.status_code
    assert "power cut" in (body.get("notice") or ""), (
        "no warning that durability was not confirmed: %r" % body)

    on_disk = json.load(open(C.CFG_PATH))["control"]["on_at"]
    assert on_disk == 1234, "the file does not hold the new value"
    assert C.CFG["control"]["on_at"] == 1234, (
        "memory kept the old value while disk holds the new one - a "
        "restart would apply it unannounced")
    return "applied, and reported as not-durable"


def test_a_save_that_never_lands_still_changes_nothing():
    """The other half: a write that failed BEFORE the replace."""
    need_dir_sync()
    C = harness.load()
    before = C.CFG["control"]["on_at"]
    real = C.write_json_durable
    C.write_json_durable = boom
    try:
        r = C.app.test_client().post("/api/config",
                                     json={"control": {"on_at": 1234}})
    finally:
        C.write_json_durable = real

    assert r.status_code == 500, "a failed write reported success"
    assert "Nothing was changed" in r.get_data(as_text=True)
    assert C.CFG["control"]["on_at"] == before, "memory changed anyway"
    return "refused, and nothing changed"


def test_a_clear_that_cannot_be_synced_keeps_the_fault_live():
    """Acknowledging a fault deletes the file, then syncs the directory.

    If that sync fails the file is already gone, so fault.json can no
    longer vouch for the fault - but it is still live in memory. Without
    flagging it unsaved, the restart guard sees a clean flag, execs, and
    the replacement loads no fault at all: Auto is free to run a fan
    that was stopped for drawing wrong.
    """
    need_dir_sync()
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = False
    C.save_fault(C.STATE["fault"])

    real_open, picky = failing_dir_open(C)
    C.os.open = picky
    try:
        had, ok = C._clear_fault()
    finally:
        C.os.open = real_open

    assert had is not None, "there was no fault to clear"
    assert ok is False, "reported the acknowledgement as successful"
    assert C.STATE["fault"] is not None, "the fault was cleared anyway"
    assert C.STATE["fault_unsaved"] is True, (
        "the fault is live and no longer on disk, but was not flagged - "
        "a restart would exec it away")
    return "still live, and flagged"


def test_a_restart_is_refused_after_a_clear_that_could_not_be_synced():
    """The consequence the flag exists for."""
    need_dir_sync()
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = False
    C.save_fault(C.STATE["fault"])

    real_open, picky = failing_dir_open(C)
    C.os.open = picky
    try:
        C._clear_fault()
    finally:
        C.os.open = real_open

    real_exec = C.os.execv
    calls = []

    def fake_exec(*a, **k):
        calls.append(a)
        raise AssertionError("exec should not have been reached")

    C.os.execv = fake_exec
    try:
        result = C._restart_now()
    finally:
        C.os.execv = real_exec

    assert not calls, "restarted with a fault that is no longer on disk"
    assert result is False, "the restart was not reported as refused"
    return "refused"


def test_an_undurable_manual_promise_refuses_the_mode_change():
    """This marker exists for exactly one failure: a power cut.

    So a promise that only probably survives a power cut is not a
    promise. Refusing costs a retry; accepting risks Manual stored with
    nothing owed and the fan still running.
    """
    need_dir_sync()
    C = harness.load()
    C.CFG["control"]["mode"] = "auto"
    real_open, picky = failing_dir_open(C)

    C.os.open = picky
    try:
        r = C.app.test_client().post("/api/config",
                                     json={"control": {"mode": "manual"}})
    finally:
        C.os.open = real_open

    assert r.status_code == 500, "accepted an unconfirmed promise"
    assert "Nothing was changed" in r.get_data(as_text=True)
    assert C.CFG["control"]["mode"] != "manual", "Manual was committed"
    return "refused, and Manual not stored"


def test_a_password_change_that_lands_but_is_not_synced_is_not_denied():
    """The password route had the same divided state as /api/config.

    Disabling authentication is the case that matters: the file already
    holds the empty hash, so a restart turns auth off - after the caller
    was told nothing had changed.
    """
    need_dir_sync()
    C = harness.load()
    C.CFG["ui"]["password_hash"] = C.set_ui_password("initial-password")
    # Setting a password turns auth on, so this route now needs it. Only
    # the password is checked; the username is ignored.
    creds = base64.b64encode(b"x:initial-password").decode()
    auth = {"Authorization": "Basic %s" % creds}

    real_open, picky = failing_dir_open(C)
    C.os.open = picky
    try:
        r = C.app.test_client().post("/api/ui/password",
                                     json={"password": ""}, headers=auth)
    finally:
        C.os.open = real_open

    body = r.get_json()
    assert r.status_code == 200, "reported a failure: %s" % r.status_code
    assert "Nothing was changed" not in str(body), body
    assert "power cut" in (body.get("notice") or ""), (
        "no warning that durability was not confirmed: %r" % body)
    assert C.CFG["ui"]["password_hash"] == "", (
        "memory still has authentication on while the file has it off - "
        "a restart would disable it unannounced")
    return "applied, and reported as not-durable"


def test_setting_a_password_that_is_not_synced_is_also_not_denied():
    """The enabling direction. No auth needed yet - there is no password."""
    need_dir_sync()
    C = harness.load()
    C.CFG["ui"]["password_hash"] = ""

    real_open, picky = failing_dir_open(C)
    C.os.open = picky
    try:
        r = C.app.test_client().post("/api/ui/password",
                                     json={"password": "a-long-password"})
    finally:
        C.os.open = real_open

    body = r.get_json()
    assert r.status_code == 200, "reported a failure: %s" % r.status_code
    assert body.get("enabled") is True, body
    assert "power cut" in (body.get("notice") or ""), (
        "no durability warning: %r" % body)
    assert C.CFG["ui"]["password_hash"] != "", (
        "memory has auth off while the file has it on")
    return "enabled, and reported as not-durable"


def test_a_second_clear_does_not_skip_the_sync_the_first_one_failed():
    """The file is already gone, so there is nothing to unlink - but the
    earlier removal was never committed.

    Guarding the sync on the file existing meant the second attempt
    found nothing, skipped it, and reported a durable acknowledgement
    that had never reached the directory.
    """
    need_dir_sync()
    C = harness.load()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "stopped": True}
        C.STATE["fault_unsaved"] = False
    C.save_fault(C.STATE["fault"])

    real_open = C.os.open
    syncs = []

    def counting(path, flags, *a):
        if flags & os.O_DIRECTORY:
            syncs.append(path)
            raise OSError(5, "Input/output error")
        return real_open(path, flags, *a)

    C.os.open = counting
    try:
        first = C._clear_fault()
        assert not os.path.exists(C.FAULT_PATH), (
            "set-up wrong: the first attempt did not remove the file")
        second = C._clear_fault()
    finally:
        C.os.open = real_open

    assert first[1] is False, "the first acknowledgement claimed success"
    assert second[1] is False, (
        "the second acknowledgement reported success without committing "
        "the removal the first one left unsynced")
    assert len(syncs) == 2, (
        "the sync was attempted %d times, not twice - the second call "
        "skipped it because the file was already gone" % len(syncs))
    assert C.STATE["fault"] is not None, "the fault was cleared anyway"
    assert C.STATE["fault_unsaved"] is True, "not flagged after the retry"
    return "retried, and still refused"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
