"""restart.sh: the exit codes callers actually branch on.

The script is the supported way to restart the controller, and it is a
hundred lines of shell whose whole job is telling a real restart from a
failed one. Nothing here imports controller.py; it runs the script in a
sandbox against a stub curl and a stub watcher, so every path can be
driven, including the ones that need a broken service.

Timeouts are shortened through RESTART_TAKE_TIMEOUT and
RESTART_BACK_TIMEOUT, or each failure case would take a minute.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Stands in for the controller's HTTP endpoint. Prints whatever
# generation the sandbox currently claims, or fails like an unreachable
# service when the "down" marker exists.
CURL_STUB = """#!/usr/bin/env bash
if [ -e "{box}/down" ]; then exit 7; fi
printf '{{"process_started_at": %s}}' "$(cat "{box}/gen")"
"""

# Stands in for the restart watcher: waits for the trigger, takes it,
# then does whatever the scenario asked for. It cd's into the sandbox
# first so scenario actions can use plain relative paths.
WATCHER = """#!/usr/bin/env bash
cd "{box}" || exit 1
for _ in $(seq 1 100); do
  [ -e restart.request ] && break
  sleep 0.2
done
[ -e restart.request ] || exit 0
rm -f restart.request
{action}
"""

REFUSAL = ('[{"at": 2, "msg": "Restart refused - the active fault is not '
           'saved to disk and would be lost"}]')


def sandbox(action="", gen="1000", down=False):
    """A throwaway directory holding restart.sh, a stub curl and a stub
    watcher. `action` is shell run in that directory after the trigger
    is taken."""
    box = tempfile.mkdtemp(prefix="restartsh-test-")
    shutil.copy(os.path.join(SRC, "restart.sh"),
                os.path.join(box, "restart.sh"))
    os.chmod(os.path.join(box, "restart.sh"), 0o755)

    # Newest first, as the real events.json is.
    with open(os.path.join(box, "events.json"), "w") as f:
        f.write('[{"at": 1, "msg": "Fan off"}]')
    with open(os.path.join(box, "gen"), "w") as f:
        f.write(gen)
    if down:
        open(os.path.join(box, "down"), "w").close()

    os.makedirs(os.path.join(box, "bin"))
    curl = os.path.join(box, "bin", "curl")
    with open(curl, "w") as f:
        f.write(CURL_STUB.format(box=box))
    os.chmod(curl, 0o755)

    with open(os.path.join(box, "watcher.sh"), "w") as f:
        f.write(WATCHER.format(box=box, action=action or ":"))
    return box


def run(box, start_watcher=True):
    """Run restart.sh in the sandbox and return the completed process."""
    env = dict(os.environ)
    env["PATH"] = os.path.join(box, "bin") + os.pathsep + env["PATH"]
    env["RESTART_TAKE_TIMEOUT"] = "3"
    env["RESTART_BACK_TIMEOUT"] = "4"

    watcher = None
    if start_watcher:
        watcher = subprocess.Popen(["bash", os.path.join(box, "watcher.sh")])
    try:
        return subprocess.run(
            ["bash", os.path.join(box, "restart.sh")],
            cwd=box, env=env, capture_output=True, text=True, timeout=40)
    finally:
        if watcher is not None:
            watcher.wait(timeout=20)


def test_a_new_generation_reports_success():
    """The only outcome that should ever exit 0."""
    box = sandbox(action="echo 2000 > gen")
    try:
        r = run(box)
    finally:
        shutil.rmtree(box, ignore_errors=True)

    assert r.returncode == 0, (
        "a real restart exited %d, not 0. stderr: %s"
        % (r.returncode, r.stderr))
    assert "confirmed" in r.stdout, "success was not reported as proved"
    return "exit 0 on a changed generation"


def test_an_unchanged_generation_is_not_success():
    """The failure that started all this: a failed exec leaves the
    ORIGINAL process serving, so the port answering proves nothing."""
    box = sandbox()
    try:
        r = run(box)
    finally:
        shutil.rmtree(box, ignore_errors=True)

    assert r.returncode == 1, (
        "an unchanged generation exited %d, not 1" % r.returncode)
    assert "ORIGINAL process is still serving" in r.stderr, (
        "did not identify which of the two failures it was")
    return "exit 1, and names the failure"


def test_a_refusal_is_reported_as_a_refusal():
    """A refused restart needs a different fix from a failed one."""
    box = sandbox(action="printf '%s' '" + REFUSAL + "' > events.json")
    try:
        r = run(box)
    finally:
        shutil.rmtree(box, ignore_errors=True)

    assert r.returncode == 1, "a refusal exited %d, not 1" % r.returncode
    assert "REFUSED" in r.stderr, "the refusal was not reported as one"
    assert "Do not force it" in r.stderr, "no warning against forcing it"
    return "exit 1, reported as a refusal"


def test_a_trigger_nobody_takes_is_exit_2():
    """Nothing consumed it, so the watcher thread is probably missing."""
    box = sandbox()
    try:
        r = run(box, start_watcher=False)
        left = os.path.exists(os.path.join(box, "restart.request"))
    finally:
        shutil.rmtree(box, ignore_errors=True)

    assert r.returncode == 2, (
        "an unconsumed trigger exited %d, not 2" % r.returncode)
    assert "never read" in r.stderr, "did not say the trigger was ignored"
    assert not left, "left the trigger behind for a later start to act on"
    return "exit 2, and cleans up after itself"


def test_an_unreachable_controller_is_exit_2():
    """Nothing to restart, and it must not leave a trigger behind."""
    box = sandbox(down=True)
    try:
        r = run(box, start_watcher=False)
        left = os.path.exists(os.path.join(box, "restart.request"))
    finally:
        shutil.rmtree(box, ignore_errors=True)

    assert r.returncode == 2, (
        "an unreachable controller exited %d, not 2" % r.returncode)
    assert "not answering" in r.stderr, "did not say it was unreachable"
    assert not left, "wrote a trigger despite nothing being there to read it"
    return "exit 2, and no trigger written"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
