"""Shared test harness.

Imports controller.py against a throwaway directory so config.json,
history.json, events.json and fault.json in the real install are never
read or written.

The DIR assert is not ceremony. CFG_PATH, HIST_PATH, FAULT_PATH and
EVENTS_PATH all derive from the module's own __file__, and the planned
module split moves that line a package level down. If that ever happens
without pinning the base path, load_cfg silently finds nothing and
returns DEFAULTS instead of raising - so the assert here fails loudly
rather than letting the tests run against defaults, or worse, live data.
"""
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    """Import controller.py against a disposable data directory."""
    tmp = tempfile.mkdtemp(prefix="fanctl-test-")
    shutil.copy(os.path.join(SRC, "controller.py"),
                os.path.join(tmp, "controller.py"))
    shutil.copy(os.path.join(SRC, "config.example.json"),
                os.path.join(tmp, "config.json"))
    os.makedirs(os.path.join(tmp, "templates"), exist_ok=True)
    open(os.path.join(tmp, "templates", "index.html"), "w").close()
    sys.path.insert(0, tmp)
    import controller as C
    assert C.DIR == tmp, "tests must not run against the live install"
    return C


def run(namespace):
    """Run every test_* callable in a module namespace."""
    failures = 0
    for name, fn in sorted(namespace.items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            detail = fn()
            print("PASS  %s%s" % (name, "  (%s)" % detail if detail else ""))
        except AssertionError as exc:
            failures += 1
            print("FAIL  %s: %s" % (name, exc))
    print("\n%d failed" % failures if failures else "\nall passed")
    return 1 if failures else 0
