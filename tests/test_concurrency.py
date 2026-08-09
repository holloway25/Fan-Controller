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


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
