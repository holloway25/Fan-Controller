"""Capture two real Tasmota replies, to pin test_plug_parse.py to the device.

    ./venv/bin/python tests/capture_plug_fixture.py

Writes tests/fixtures/plug_capture.json. Run it yourself - it reads the
plug password out of config.json at RUNTIME so the credential never
reaches a shell history, a process list, or anyone's screen.

Named capture_* rather than test_* on purpose: run_all.py globs test_*.py,
and this touches the network, so it must never join the suite.

BOTH COMMANDS ARE READS. "Power" with no argument queries the relay
rather than switching it - the same call state() makes every poll.
"Status 8" reads the energy meter. Neither changes anything.

It rebuilds the request shape rather than importing controller.py,
because importing that module against the live directory loads
history.json and the rest of the running service's state. The shape is
copied from Tasmota._cmd; if that ever changes, this needs the same
change, and the capture is only honest if they match.
"""
import datetime
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("run this with ./venv/bin/python - requests is in the venv")

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(SRC, "config.json")
OUT_DIR = os.path.join(SRC, "tests", "fixtures")
OUT = os.path.join(OUT_DIR, "plug_capture.json")

COMMANDS = ["Power", "Status 8"]


def ask(host, user, password, cmnd):
    """One /cm call, shaped exactly as Tasmota._cmd shapes it."""
    params = {"cmnd": cmnd}
    if password:
        params["user"] = user
        params["password"] = password
    try:
        r = requests.get("http://%s/cm" % host, params=params, timeout=12)
    except requests.RequestException as e:
        # Type only. requests puts the full URL, credentials included,
        # in the exception text.
        sys.exit("could not reach the plug (%s)" % type(e).__name__)
    if r.status_code >= 400:
        sys.exit("plug returned HTTP %d" % r.status_code)
    if not r.content:
        sys.exit("plug returned an empty reply - the HTTP command API "
                 "is disabled on the device")
    try:
        return r.json()
    except ValueError:
        sys.exit("plug returned a non-JSON reply")


def main():
    try:
        cfg = json.load(open(CFG))
    except OSError:
        sys.exit("no config.json at %s" % CFG)
    plug = cfg.get("plug") or {}
    host = plug.get("host")
    if not host:
        sys.exit("no plug host in config.json")
    password = plug.get("password") or ""
    user = plug.get("user") or "admin"

    captured = {}
    for cmnd in COMMANDS:
        data = ask(host, user, password, cmnd)
        if "WARNING" in data:
            sys.exit("plug rejected the credentials")
        captured[cmnd] = data

    blob = json.dumps(captured, indent=2, sort_keys=True)

    # Refuse to write anything containing the credential or the address.
    # The address is network detail and this file goes to the public
    # mirror; the password should never be near a file at all.
    for secret, what in ((password, "the plug password"), (host, "the plug address")):
        if secret and secret in blob:
            sys.exit("REFUSING TO WRITE: the reply contains %s" % what)

    out = {
        "_comment": ("Real replies from the deployment's Tasmota plug, for "
                     "test_plug_parse.py. Reads only. No credentials and no "
                     "host address - see capture_plug_fixture.py."),
        "captured_utc": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%d"),
        "replies": captured,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")

    print("wrote %s\n" % OUT)
    print(json.dumps(out, indent=2, sort_keys=True))
    print("\nRead it before committing. Two things worth noting:")
    print("  - which relay key came back, POWER or POWER1")
    print("  - whether ENERGY.Power is a bare number or a quoted string")


if __name__ == "__main__":
    main()
