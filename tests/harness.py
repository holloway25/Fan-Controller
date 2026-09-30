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
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

_LOADED = []
# Every directory this process created, removed when it exits.
#
# These used to be left behind. One is 300KB and nobody notices; a day
# of running the suite is thousands, and /tmp on the VM is TMPFS - so
# they are not disk, they are RAM. 5,178 of them reached 1.4GB on a box
# with 1GB, the OOM killer took the test process, and then it took the
# FAN SERVICE, which systemd restarted eight times. A systemd restart
# drops the latch and the minimum-run lease, so a leaking test harness
# had become something that could stop a running fan.
#
# Removed at exit rather than after each load(): a test that fails
# leaves its directory intact for the length of the run, which is what
# makes a failure investigable.
_TMPDIRS = []


def _clean_tmpdirs():
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_clean_tmpdirs)


class Skip(Exception):
    """Raised by a test that cannot run in this environment.

    Not a failure. Some tests need things that only exist on the
    deployment host - /proc/self/fd, a working bash - and failing them
    on a review machine teaches people to ignore a red line, which is
    worse than not running them.
    """


def skip(reason):
    raise Skip(reason)

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    """Import controller.py against a disposable data directory."""
    tmp = tempfile.mkdtemp(prefix="fanctl-test-")
    _TMPDIRS.append(tmp)
    shutil.copy(os.path.join(SRC, "controller.py"),
                os.path.join(tmp, "controller.py"))
    shutil.copy(os.path.join(SRC, "config.example.json"),
                os.path.join(tmp, "config.json"))
    os.makedirs(os.path.join(tmp, "templates"), exist_ok=True)
    open(os.path.join(tmp, "templates", "index.html"), "w").close()
    shutil.copytree(os.path.join(SRC, "static"),
                    os.path.join(tmp, "static"))
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
    _stub_dir_fsync(mod)
    return mod


_ANNOUNCED = []


def _stub_dir_fsync(mod):
    """Make _fsync_dir a no-op where O_DIRECTORY does not exist.

    Not a convenience. write_json_durable calls _fsync_dir, and
    save_cfg, promise_manual_off and save_fault all call
    write_json_durable - so on Windows a plain config save raises
    AttributeError from os.O_DIRECTORY, and dozens of tests with
    nothing to do with durability fail. AttributeError is not OSError,
    so write_json_durable's own handler does not even catch it.

    The README used to say a Windows run went green with three named
    skips. It did not, and could not, and this is the half of the fix
    that makes the claim true.

    WHAT THIS COSTS, stated rather than buried: on such a platform the
    directory fsync is not exercised at all. test_durability.py skips
    before it ever loads the controller, so nothing here claims
    durability was tested - but every other module then passes without
    the sync happening, which is a weaker run than the same modules on
    Linux. It says so out loud, once, rather than going quietly green.
    """
    if hasattr(os, "O_DIRECTORY"):
        return
    mod._fsync_dir = lambda path: None
    if not _ANNOUNCED:
        _ANNOUNCED.append(True)
        print("NOTE  no O_DIRECTORY on this platform: directory fsync is "
              "stubbed out.\n      Durability is NOT exercised by this "
              "run. Linux or CI is the real check.")


def run(namespace):
    """Run every test_* callable in a module namespace."""
    failures = 0
    skipped = 0
    for name, fn in sorted(namespace.items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            detail = fn()
            print("PASS  %s%s" % (name, "  (%s)" % detail if detail else ""))
        except Skip as exc:
            # Counted as neither pass nor failure, and named so that a
            # suite run somewhere unusual says what it did not cover.
            skipped += 1
            print("SKIP  %s: %s" % (name, exc))
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
    tail = " (%d skipped)" % skipped if skipped else ""
    print("\n%d failed%s" % (failures, tail) if failures
          else "\nall passed%s" % tail)
    return 1 if failures else 0
