"""shellcheck, when it is available.

bash -n catches a syntax error and nothing else. Every shell bug this
project has actually had - a pipeline hiding a failing exit status, an
unquoted expansion, a variable that outlived the code that used it -
parses perfectly.

SKIPPED WHEN shellcheck IS ABSENT, because it is a package rather than
a Python dependency and the suite must still run on a board where it is
not installed. CI installs it, so the skip is not a way of quietly
covering less: the x86 job treats a skip as a failure.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPTS = ["install.sh", "restart.sh", "run.sh",
           ".githooks/pre-commit", ".githooks/install.sh"]


def _have():
    from shutil import which
    return which("shellcheck") is not None


def test_every_shell_script_is_listed_here():
    """A new script that nothing checks is the failure this file is for.

    Found by looking for shebangs rather than by trusting the list -
    the list is what goes stale.
    """
    found = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("venv", ".git", "node_modules", "__pycache__")]
        for name in files:
            p = os.path.join(base, name)
            rel = os.path.relpath(p, ROOT)
            if rel in SCRIPTS:
                continue
            try:
                with open(p, "rb") as f:
                    first = f.readline(80)
            except OSError:
                continue
            if first.startswith(b"#!") and b"sh" in first.split(b"\n")[0]:
                if b"python" in first:
                    continue
                found.append(rel)
    assert not found, "shell scripts not covered by SCRIPTS: %s" % found
    return "%d scripts listed, none missed" % len(SCRIPTS)


def test_the_scripts_pass_shellcheck():
    if not _have():
        return "SKIP shellcheck is not installed"
    bad = []
    for rel in SCRIPTS:
        r = subprocess.run(["shellcheck", "-f", "gcc", rel],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            bad.append("%s:\n%s" % (rel, r.stdout.strip()))
    assert not bad, "\n\n".join(bad)
    return "%d scripts clean" % len(SCRIPTS)


def test_the_checker_can_actually_fail():
    """A checker that cannot fail proves nothing - the lesson this
    repository has now learned six times."""
    if not _have():
        return "SKIP shellcheck is not installed"
    import tempfile
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "bad.sh")
    with open(p, "w") as f:
        # Unquoted expansion in a test: the classic word-splitting bug.
        f.write("#!/usr/bin/env bash\nx=$1\nif [ $x = y ]; then :; fi\n")
    r = subprocess.run(["shellcheck", p], capture_output=True, text=True)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    assert r.returncode != 0, "shellcheck passed a script it should reject"
    return "the checker bites"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
