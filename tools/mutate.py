#!/usr/bin/env python3
"""Break the code on purpose and check something notices.

WHY THIS EXISTS. Six external reviews in three days found the same
defect repeatedly: tests that assert the shape of the source rather
than the behaviour of the system. Every one passed. Two examples, both
from tests written the same day they were caught - a deep-copy test
that did its own deepcopy, and a test that searched controller.py for
the word "try". Neither could fail. The suite going green had stopped
being evidence.

A mutation is a CLAIM: break this line, and that test module must
fail. If it does not, the test is decoration. Curated by hand rather
than generated, because the point is not coverage arithmetic - it is
that the twenty things which actually matter are each guarded by
something that bites.

    ./venv/bin/python tools/mutate.py            # run them all
    ./venv/bin/python tools/mutate.py --list     # what is claimed
    ./venv/bin/python tools/mutate.py -k fault   # a subset

Each mutation runs in a COPY of the tree. The real one is never
touched, and the copy is deleted afterwards.

Only the named module is run, not the whole suite: a mutation whose
consequence shows up somewhere unrelated is usually a sign the claim
was written too loosely.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable


# name, file, old, new, the module that must fail
MUTATIONS = [
    # ---------------------------------------------------- log classes
    ("prefix-not-stripped", "controller.py",
     '    m = _CODE_PREFIX.sub("", raw)',
     '    m = raw',
     "test_log_classes.py"),
    ("3xx-ignores-mode", "controller.py",
     '            return "ok" if m.endswith("(Manual)") else "m-manual"',
     '            return "m-manual"',
     "test_log_classes.py"),
    ("2xx-not-red", "controller.py",
     '        if ser in ("0", "2"):',
     '        if ser in ("0",):',
     "test_log_classes.py"),
    ("class-not-served", "controller.py",
     '''    s["events"] = [dict(e, cls=event_class(e.get("msg")))
                   for e in s["events"][:EVENTS_SHOWN]]''',
     '    s["events"] = s["events"][:EVENTS_SHOWN]',
     "test_log_classes.py"),

    # ------------------------------------------------------ the fault
    ("stop-not-confirmed-claimed", "controller.py",
     '''                     ("Action taken",
                      "Plug switched off, fault latched" if confirmed else
                      "Stop commanded, NOT confirmed - fault latched"),''',
     '''                     ("Action taken",
                      "Plug switched off, fault latched"),''',
     "test_fault_codes.py"),
    ("fault-code-not-stored", "controller.py",
     '''            f = {"at": now, "msg": msg, "stopped": False, "code": code,''',
     '''            f = {"at": now, "msg": msg, "stopped": False,''',
     "test_fault_codes.py"),
    ("external-change-drops-mode", "controller.py",
     '''                    ext_msg = ("%s Fan %s \\u2014 changed outside (%s)"
                               % (FC_EXTERNAL_CHANGE,
                                  "on" if actual else "off",
                                  _mode_now()))''',
     '''                    ext_msg = ("%s Fan %s \\u2014 changed outside"
                               % (FC_EXTERNAL_CHANGE,
                                  "on" if actual else "off"))''',
     "test_fault_codes.py"),

    # ------------------------------------------------------- the daily
    ("daily-fold-unguarded", "controller.py",
     '''        try:
            _daily_note(now, *_daily_pending)
        except Exception as e:      # pragma: no cover - must never escape
            reset_daily()
            _log("%s Daily summary reset after an error: %s: %s"
                 % (FC_DISK_FAILED, type(e).__name__, e))''',
     '        _daily_note(now, *_daily_pending)',
     "test_daily.py"),
    ("daily-snapshot-shallow", "controller.py",
     '''        rows = copy.deepcopy(STATE.get("daily") or [])
        _daily_dirty = False''',
     '''        rows = STATE.get("daily") or []
        _daily_dirty = False''',
     "test_daily.py"),
    ("daily-accepts-nan", "controller.py",
     '''    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))''',
     '''    return isinstance(v, (int, float)) and not isinstance(v, bool)''',
     "test_daily.py"),
    ("daily-logs-on-signal", "controller.py",
     '        if report_error:\n',
     '        if True:\n',
     "test_daily.py"),

    # ------------------------------------------------------ the scanner
    ("scanner-prints-the-value", "tools/scan_secrets.py",
     '''    print("%s:%s: credential-looking assignment (value redacted)"
          % (where, lineno))''',
     '''    print("%s:%s: credential-looking assignment: %s"
          % (where, lineno, line.strip()[:120]))''',
     "test_secret_scanner.py"),
    # A FAITHFUL REGRESSION, not a token edit. The first version of
    # this mutation removed only the leading "^" - which leaves the
    # trailing "$" doing the anchoring, so it barely changed the
    # behaviour and "SURVIVED" said nothing about the tests. A
    # mutation has to be the bug you are actually afraid of: here,
    # the substring list that once contained "password" and excused
    # Password123!.
    ("scanner-placeholder-substring", "tools/scan_secrets.py",
     'PLACEHOLDER = re.compile(\n    r"^(?:"\n    r"changeme|change[-_]me|redacted|dummy|sample|fake|todo|tbd|"\n    r"xxx+|placeholder|example|test[-_]value|not[-_]a[-_]real"\n    r")(?:[-_.].*)?$"\n    r"|^your[-_].*$"\n    r"|^\\.\\.\\.$",\n    re.IGNORECASE)',
     'PLACEHOLDER = re.compile(\n    r"(placeholder|example|changeme|redacted|dummy|sample|fake|"\n    r"todo|tbd|password|secret)",\n    re.IGNORECASE)',
     "test_secret_scanner.py"),
    ("scanner-yaml-gets-code-exemption", "tools/scan_secrets.py",
     '            if not yaml_style and CODE.search(v):',
     '            if CODE.search(v):',
     "test_secret_scanner.py"),
    ("scanner-ignores-git-failure", "tools/scan_secrets.py",
     '''    if r.returncode != 0:''',
     '''    if False:''',
     "test_secret_scanner.py"),
]


IGNORE = ("venv", ".git", "__pycache__", "*.log", "config.json",
          "history.json", "daily.json", "events.json", "fault.json",
          "*.json.tmp")


def _copy(dest):
    """A copy of the tree that is a REAL git repository.

    .git is not copied - it is large and its history is irrelevant here
    - but the scanner reads the tree THROUGH git, so a copy without one
    is not a working tree at all. The control run found this: the
    secret-scanner tests failed in a plain copy before anything was
    mutated, which would have reported those mutations as caught when
    they were not.

    The ignore list is deliberately specific. An earlier version
    excluded "*.json" wholesale, which took config.example.json with it
    - the harness then failed to load and EVERY mutation was reported
    as caught, by a FileNotFoundError rather than by a test.
    """
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(*IGNORE))
    quiet = {"capture_output": True, "cwd": dest}
    subprocess.run(["git", "init", "-q", "."], **quiet)
    subprocess.run(["git", "config", "user.email", "mutate@invalid"], **quiet)
    subprocess.run(["git", "config", "user.name", "mutate"], **quiet)
    subprocess.run(["git", "add", "-A"], **quiet)
    subprocess.run(["git", "commit", "-qm", "control"], **quiet)
    return dest


def _apply(tree, old, new, path):
    p = os.path.join(tree, path)
    with io.open(p, encoding="utf-8") as f:
        s = f.read()
    if s.count(old) != 1:
        return s.count(old)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(s.replace(old, new))
    return 1


def run_one(name, path, old, new, module, verbose=False):
    tmp = tempfile.mkdtemp(prefix="mutate-")
    try:
        tree = _copy(os.path.join(tmp, "t"))
        hits = _apply(tree, old, new, path)
        if hits != 1:
            # The anchor no longer matches: the code moved and the
            # claim was never re-checked. That is a finding in itself.
            return "STALE", "anchor matched %d times in %s" % (hits, path)
        r = subprocess.run([PY, os.path.join(tree, "tests", module)],
                           capture_output=True, text=True, cwd=tree)
        failed = [ln.strip() for ln in r.stdout.splitlines()
                  if ln.startswith("FAIL") or ln.startswith("ERROR")]
        if r.returncode != 0 and failed:
            return "CAUGHT", failed[0][:100]
        if r.returncode != 0:
            return "CAUGHT", "%s exited %d" % (module, r.returncode)
        return "SURVIVED", "%s still passes" % module
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def control_run(module):
    """The module must PASS before anything is broken.

    Without this a mutation looks caught when the harness itself is
    broken - a missing file, an import error, a stale path. Every
    "CAUGHT" below means "passed clean, failed mutated", not merely
    "failed".
    """
    tmp = tempfile.mkdtemp(prefix="control-")
    try:
        tree = _copy(os.path.join(tmp, "t"))
        r = subprocess.run([PY, os.path.join(tree, "tests", module)],
                           capture_output=True, text=True, cwd=tree)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-k", metavar="SUBSTRING",
                    help="only mutations whose name contains this")
    a = ap.parse_args()

    picked = [m for m in MUTATIONS if not a.k or a.k in m[0]]
    if a.list:
        for name, path, _, _, module in picked:
            print("%-34s %-22s %s" % (name, path, module))
        return 0

    # One control run per module, before anything is broken.
    for module in sorted({m[4] for m in picked}):
        ok, tail = control_run(module)
        if not ok:
            print("CONTROL FAILED: %s does not pass unmutated" % module)
            print(tail)
            print("")
            print("Fix that first. Until it passes clean, a mutation "
                  "failing proves nothing.")
            return 2
        print("control  %-28s passes clean" % module)
    print("")

    bad = 0
    for name, path, old, new, module in picked:
        verdict, detail = run_one(name, path, old, new, module)
        print("%-34s %-9s %s" % (name, verdict, detail))
        sys.stdout.flush()
        if verdict != "CAUGHT":
            bad += 1

    print("")
    if bad:
        print("%d of %d mutations were not caught." % (bad, len(picked)))
        print("A mutation that survives means the test named beside it "
              "does not")
        print("actually guard the behaviour it claims to. Fix the TEST, "
              "not this file.")
        return 1
    print("all %d mutations caught" % len(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
