"""The restart trigger: taken once, never twice, never a loop.

_restart_now is never called here. It ends in execv, which would
replace the test process itself - so what is tested is the decision to
restart, not the exec. The exec path is four lines and is exercised for
real every time the trigger is used.
"""
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


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
