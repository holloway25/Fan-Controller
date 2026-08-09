"""Concurrency regression tests.

Run with the project venv, from anywhere:

    ./venv/bin/python tests/test_concurrency.py

The module is imported against a throwaway directory, so config.json,
history.json, events.json and fault.json in the real install are never
read or written. That also exercises the DIR-from-__file__ behaviour: if
these paths ever stop deriving from the module's own location, the assert
below fails rather than the tests quietly using live data.

No plug and no Protect console are contacted; both clients are stubbed.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    """Import controller.py against a disposable data directory."""
    tmp = tempfile.mkdtemp(prefix="fanctl-test-")
    shutil.copy(os.path.join(SRC, "controller.py"), os.path.join(tmp, "controller.py"))
    shutil.copy(os.path.join(SRC, "config.example.json"),
                os.path.join(tmp, "config.json"))
    os.makedirs(os.path.join(tmp, "templates"), exist_ok=True)
    open(os.path.join(tmp, "templates", "index.html"), "w").close()
    sys.path.insert(0, tmp)
    import controller as C
    assert C.DIR == tmp, "tests must not run against the live install"
    return C


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
    C = load()
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


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            detail = fn()
            print("PASS  %s  (%s)" % (name, detail))
        except AssertionError as exc:
            failures += 1
            print("FAIL  %s: %s" % (name, exc))
    print("\n%d failed" % failures if failures else "\nall passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
