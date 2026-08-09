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
import importlib.util
import os
import shutil
import sys
import tempfile

_LOADED = []

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
    # Unique module name, not a plain import: a second import in one
    # process returns the module cached from the first call, still
    # pointed at the first directory.
    name = "controller_under_test_%d" % len(_LOADED)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(tmp, "controller.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _LOADED.append(mod)
    assert mod.DIR == tmp, "tests must not run against the live install"
    return mod


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
        except Exception as exc:
            # Anything else is still a failure, not a reason to abandon
            # the rest of the module. A missing attribute or a KeyError
            # is exactly what a regression looks like when a test is run
            # against older code.
            failures += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d failed" % failures if failures else "\nall passed")
    return 1 if failures else 0
