"""install.sh: run it for real, in a copy, and check what it produced.

The install steps lived in the README and in one person's memory. A
script that is never executed by anything is a README with a shebang,
so this runs it end to end in a throwaway tree - creating a real venv,
installing real dependencies - and asserts the things that are easy to
get wrong and invisible until the service will not start.

It is the slowest module in the suite by a distance, because a venv and
a pip install are genuinely slow. That is the price of testing the
thing rather than reading it.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                            # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install.sh")


def _tree():
    """A copy with the files install.sh needs and nothing it should find."""
    tmp = tempfile.mkdtemp(prefix="install-")
    dest = os.path.join(tmp, "t")
    os.makedirs(dest)
    for name in ("install.sh", "requirements.txt", "config.example.json",
                 "office-fan-controller.service.example"):
        shutil.copy2(os.path.join(ROOT, name), dest)
    return tmp, dest


def _run(dest, *args):
    return subprocess.run(["bash", os.path.join(dest, "install.sh")] + list(args),
                          capture_output=True, text=True, cwd=dest)


def test_it_is_valid_shell():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "parses"


def test_it_refuses_to_run_as_root():
    """Everything it creates would be root-owned - the venv, config.json,
    the state files - and the service runs as an ordinary user, which is
    the most likely way to produce an install that looks fine and cannot
    start."""
    src = open(SCRIPT, encoding="utf-8").read()
    assert 'if [ "$(id -u)" = "0" ]' in src, "no root check"
    i = src.index('if [ "$(id -u)" = "0" ]')
    assert "die" in src[i:i + 300], "the root check does not stop"
    return "root is refused"


def test_a_fresh_install_produces_a_working_tree():
    tmp, dest = _tree()
    try:
        r = _run(dest)
        assert r.returncode == 0, r.stdout + r.stderr
        py = os.path.join(dest, "venv", "bin", "python")
        assert os.path.exists(py), "no venv: %s" % r.stdout
        # The dependencies are actually importable, not merely installed.
        c = subprocess.run([py, "-c", "import flask, requests, urllib3"],
                           capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        assert os.path.exists(os.path.join(dest, "config.json"))
        return "venv, dependencies and config"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_config_is_not_world_readable():
    """It holds the Protect password, the plug password, the SMTP
    password and the UI hash."""
    tmp, dest = _tree()
    try:
        _run(dest)
        mode = os.stat(os.path.join(dest, "config.json")).st_mode & 0o777
        assert mode == 0o600, "config.json is %o" % mode
        return "0600"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_it_tightens_a_config_that_arrives_loose():
    """A config copied in by hand or restored from a backup carries
    whatever mode it had. Chmod on every run, not only on creation."""
    tmp, dest = _tree()
    try:
        p = os.path.join(dest, "config.json")
        shutil.copy2(os.path.join(dest, "config.example.json"), p)
        os.chmod(p, 0o644)
        _run(dest)
        mode = os.stat(p).st_mode & 0o777
        assert mode == 0o600, "left at %o" % mode
        return "loosened config is tightened"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_it_never_overwrites_an_existing_config():
    tmp, dest = _tree()
    try:
        p = os.path.join(dest, "config.json")
        with open(p, "w") as f:
            f.write('{"mine": true}')
        r = _run(dest)
        assert open(p).read() == '{"mine": true}', "config was overwritten"
        assert "left alone" in r.stdout, r.stdout
        return "existing config survives"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_unit_names_this_user_and_this_directory():
    """The example carries a generic user and path on purpose - the
    concrete ones are site detail and stay out of the repository."""
    tmp, dest = _tree()
    try:
        _run(dest)
        unit = open(os.path.join(dest, "office-fan-controller.service"),
                    encoding="utf-8").read()
        who = subprocess.run(["id", "-un"], capture_output=True,
                             text=True).stdout.strip()
        assert re.search(r"^User=%s$" % re.escape(who), unit, re.M), unit
        assert re.search(r"^WorkingDirectory=%s$" % re.escape(dest), unit,
                         re.M), unit
        assert re.search(r"^ExecStart=%s/venv/bin/python controller\.py$"
                         % re.escape(dest), unit, re.M), unit
        # And it must not still carry the example's placeholder.
        assert "fanctl" not in unit, "the placeholder user survived"
        return "unit is site-specific"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_running_it_twice_changes_nothing():
    """Idempotent, because it will be run on existing installs."""
    tmp, dest = _tree()
    try:
        _run(dest)
        p = os.path.join(dest, "config.json")
        with open(p, "a") as f:
            f.write("\\n")
        before = open(p).read()
        r = _run(dest)
        assert r.returncode == 0, r.stdout + r.stderr
        assert open(p).read() == before, "second run touched config.json"
        assert "reusing it" in r.stdout, r.stdout
        return "second run is a no-op"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_it_does_not_install_the_unit_without_being_asked():
    """Writing to /etc/systemd/system and enabling a service is not
    something a setup script should do because it can."""
    tmp, dest = _tree()
    try:
        r = _run(dest)
        assert "sudo cp" in r.stdout, "does not say what to run"
        assert "not installed" in r.stdout, r.stdout
        return "privileged steps are opt-in"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_it_rejects_an_unknown_option():
    tmp, dest = _tree()
    try:
        r = _run(dest, "--frobnicate")
        assert r.returncode == 2, r.returncode
        return "unknown options fail"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
