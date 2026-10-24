"""The dashboard's JavaScript, actually executed.

Everything else about the page is asserted from its SOURCE - that a
line is present - which catches a deletion and not a mistake. Three
regressions got past that: log colours that stopped matching when
messages gained a fault-code prefix, a version footer whose assignment
could be made unreachable while the string stayed, and a range read off
the drawn scale rather than the readings.

These run the page under jsdom with the server stubbed, so the tests
see what a browser would build.

SKIPPED WHEN node OR node_modules IS ABSENT. It is a development
dependency, never a runtime one: the controller needs Python and
nothing else, and the Pi has no Node at all. CI installs it, and every
CI job fails on a SKIP, so the skip cannot quietly become the normal
case.
"""
import os
import subprocess
import sys
from shutil import which

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                            # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "run.cjs")


def _why_not():
    if which("node") is None:
        return "node is not installed"
    if not os.path.isdir(os.path.join(ROOT, "node_modules", "jsdom")):
        return "jsdom is not installed - run npm install"
    return None


def test_the_javascript_tests_pass():
    why = _why_not()
    if why:
        harness.skip(why)
    r = subprocess.run(["node", RUNNER], capture_output=True, text=True,
                       cwd=ROOT, timeout=300)
    if r.returncode != 0:
        fails = [ln for ln in r.stdout.splitlines()
                 if ln.startswith("FAIL") or ln.startswith("RUNNER")]
        raise AssertionError("\n".join(fails or [r.stdout[-600:],
                                                 r.stderr[-300:]]))
    passed = [ln for ln in r.stdout.splitlines() if ln.startswith("PASS")]
    assert passed, "the runner reported no tests: %s" % r.stdout[-300:]
    return "%d browser tests" % len(passed)


def test_an_empty_run_is_a_failure():
    """A runner that finds no tests and exits zero is a green step doing
    no work - the same defect as a test that cannot fail, which this
    repository has now met in four different disguises."""
    why = _why_not()
    if why:
        harness.skip(why)
    import shutil
    import tempfile
    # An EMPTY DIRECTORY passed to the real runner, rather than a copy
    # of the runner somewhere else: copying it moved node out of reach
    # of node_modules, so the test failed on a require error and proved
    # nothing about the empty case.
    tmp = tempfile.mkdtemp()
    try:
        r = subprocess.run(["node", RUNNER, tmp], capture_output=True,
                           text=True, cwd=ROOT, timeout=60)
        assert r.returncode == 2, (r.returncode, r.stdout[-200:],
                                   r.stderr[-200:])
        assert "no tests found" in r.stdout, r.stdout[-200:]
        return "an empty run exits 2"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_completed_browser_deadlines_are_cleared_and_hangs_still_fail():
    why = _why_not()
    if why:
        harness.skip(why)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        fixture = Path(folder)/'fixture.test.cjs'
        fixture.write_text('module.exports = { quick: async () => {} };')
        # An unused 20-second deadline must not keep a completed runner alive.
        r = subprocess.run(['node', RUNNER, folder], capture_output=True,
                           text=True, cwd=ROOT, timeout=8)
        assert r.returncode == 0 and 'all 1 passed' in r.stdout, r.stdout
        fixture.write_text('module.exports = { stuck: () => new Promise(() => {}) };')
        preload = Path(folder)/'deadline.cjs'
        preload.write_text('const original = global.setTimeout; '
                           'global.setTimeout = (fn, ms, ...args) => '
                           'original(fn, ms === 20000 ? 50 : ms, ...args);')
        r = subprocess.run(['node', '--require', str(preload), RUNNER, folder],
                           capture_output=True, text=True, cwd=ROOT, timeout=8)
        assert r.returncode == 1 and 'FAIL  stuck: timed out' in r.stdout, r.stdout


def test_the_dependency_is_declared_and_dev_only():
    """jsdom must never become a runtime dependency: the controller
    needs Python and nothing else, and the board has no Node."""
    import json
    pkg = json.load(open(os.path.join(ROOT, "package.json")))
    assert "jsdom" in pkg.get("devDependencies", {}), pkg
    assert not pkg.get("dependencies"), (
        "a runtime dependency appeared in package.json: %s"
        % pkg.get("dependencies"))
    assert os.path.exists(os.path.join(ROOT, "package-lock.json")), (
        "the lock file is not committed, so the version is not pinned")
    return "dev dependency, pinned"


def test_missing_browser_dependencies_are_reported_as_skips():
    import contextlib
    import io
    original = globals()["_why_not"]
    try:
        for reason in ("node is not installed", "jsdom is not installed - run npm install"):
            globals()["_why_not"] = lambda: reason
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = harness.run({
                    "test_the_javascript_tests_pass": test_the_javascript_tests_pass,
                    "test_an_empty_run_is_a_failure": test_an_empty_run_is_a_failure})
            assert code == 0
            text = output.getvalue()
            assert text.count("SKIP  ") == 2, text
            assert "PASS  " not in text, text
    finally:
        globals()["_why_not"] = original


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
