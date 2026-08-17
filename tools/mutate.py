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
     "test_log_classes.py",
     "test_the_code_prefix_does_not_hide_the_text_rules"),
    ("3xx-ignores-mode", "controller.py",
     '            return "ok" if m.endswith("(Manual)") else "m-manual"',
     '            return "m-manual"',
     "test_log_classes.py",
     "test_every_line_gets_the_class_it_should"),
    ("2xx-not-red", "controller.py",
     '        if ser in ("0", "2"):',
     '        if ser in ("0",):',
     "test_log_classes.py",
     "test_every_line_gets_the_class_it_should"),
    ("class-not-served", "controller.py",
     '''    s["events"] = [dict(e, cls=event_class(e.get("msg")))
                   for e in s["events"][:EVENTS_SHOWN]]''',
     '    s["events"] = s["events"][:EVENTS_SHOWN]',
     "test_log_classes.py",
     "test_the_endpoint_serves_the_class_with_each_event"),

    # ---------------------------------------------------- chart ranges
    # The bug that was on screen: a fan guide widening the reported
    # range, so the humidity row claimed a 70% high on a day the room
    # never passed 55.
    ("range-follows-the-scale", "controller.py",
     '        vals = [p[key] for p in (hist or [])\n                if isinstance(p, dict) and _finite(p.get(key))]',
     '        vals = [p[key] for p in (hist or [])\n                if isinstance(p, dict) and _finite(p.get(key))]\n        if key == "rh" and CFG["control"].get("humidity_enabled"):\n            vals = vals + [CFG["control"]["humidity_on"]]',
     "test_chart_ranges.py",
     "test_a_threshold_cannot_widen_the_range"),
    # A delta is what the page holds for most of its life.
    ("ranges-missing-from-deltas", "controller.py",
     '                        "ranges": history_ranges(hist),\n                        "first_t": first_t})',
     '                        "first_t": first_t})',
     "test_chart_ranges.py",
     "test_the_endpoint_sends_ranges_with_a_delta"),

    # ------------------------------------------------------ the fault
    ("stop-not-confirmed-claimed", "controller.py",
     '''                     ("Action taken",
                      "Plug switched off, fault latched" if confirmed else
                      "Stop commanded, NOT confirmed - fault latched"),''',
     '''                     ("Action taken",
                      "Plug switched off, fault latched"),''',
     "test_fault_codes.py",
     "test_an_unconfirmed_stop_is_not_reported_as_a_stop"),
    ("fault-code-not-stored", "controller.py",
     '''            f = {"at": now, "msg": msg, "stopped": False, "code": code,''',
     '''            f = {"at": now, "msg": msg, "stopped": False,''',
     "test_fault_codes.py",
     "test_a_silent_plug_raises_fc002_end_to_end"),
    ("external-change-drops-mode", "controller.py",
     '''                    ext_msg = ("%s Fan %s \\u2014 changed outside (%s)"
                               % (FC_EXTERNAL_CHANGE,
                                  "on" if actual else "off",
                                  _mode_now()))''',
     '''                    ext_msg = ("%s Fan %s \\u2014 changed outside"
                               % (FC_EXTERNAL_CHANGE,
                                  "on" if actual else "off"))''',
     "test_fault_codes.py",
     "test_an_external_change_records_the_mode"),

    # ------------------------------------------------------- the daily
    ("daily-fold-unguarded", "controller.py",
     '''        try:
            _daily_note(now, *_daily_pending)
        except Exception as e:      # pragma: no cover - must never escape
            reset_daily()
            _log("%s Daily summary reset after an error: %s: %s"
                 % (FC_DISK_FAILED, type(e).__name__, e))''',
     '        _daily_note(now, *_daily_pending)',
     "test_daily.py",
     "test_a_broken_summary_cannot_abort_a_poll"),
    ("daily-snapshot-shallow", "controller.py",
     '''        rows = copy.deepcopy(STATE.get("daily") or [])
        _daily_dirty = False''',
     '''        rows = STATE.get("daily") or []
        _daily_dirty = False''',
     "test_daily.py",
     "test_the_writer_snapshot_is_deep"),
    ("daily-accepts-nan", "controller.py",
     '''    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))''',
     '''    return isinstance(v, (int, float)) and not isinstance(v, bool)''',
     "test_daily.py",
     "test_a_non_finite_reading_never_reaches_the_file"),
    ("daily-logs-on-signal", "controller.py",
     '        if report_error:\n',
     '        if True:\n',
     "test_daily.py",
     "test_the_shutdown_path_does_not_log"),

    # ------------------------------------------------------ the scanner
    ("scanner-prints-the-value", "tools/scan_secrets.py",
     '''    print("%s:%s: credential-looking assignment (value redacted)"
          % (where, lineno))''',
     '''    print("%s:%s: credential-looking assignment: %s"
          % (where, lineno, line.strip()[:120]))''',
     "test_secret_scanner.py",
     "test_a_finding_never_prints_the_value"),
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
     "test_secret_scanner.py",
     "test_a_real_value_containing_the_word_password_is_still_caught"),
    ("scanner-yaml-gets-code-exemption", "tools/scan_secrets.py",
     '            if not yaml_style and CODE.search(v):',
     '            if CODE.search(v):',
     "test_secret_scanner.py",
     "test_a_yaml_value_that_merely_looks_like_code_is_caught"),
    ("scanner-ignores-git-failure", "tools/scan_secrets.py",
     '''    if r.returncode != 0:''',
     '''    if False:''',
     "test_secret_scanner.py",
     "test_a_failing_git_command_is_not_reported_as_clean"),
]


def _copy(dest):
    """A copy of the tree that is a REAL git repository.

    GIT DECIDES WHAT COMES, not a hand-maintained list. The list was
    wrong in both directions: it copied config.local.json,
    config.json.bak, the concrete systemd unit and a whole .venv if one
    existed - real credentials and machine paths, duplicated fifteen
    times per run - while an earlier version excluded "*.json"
    wholesale and took config.example.json with it, which made every
    mutation "caught" by a FileNotFoundError.

    ls-files --cached --others --exclude-standard is exactly "what git
    would take", so the copy matches what a clone gets and cannot drift
    from .gitignore. Reported by review of fdda51b.

    .git itself is not copied - large, and the history is irrelevant -
    but the scanner reads the tree THROUGH git, so the copy is
    initialised as its own repository. Without that the secret-scanner
    tests fail before anything is mutated, which the control run
    caught.
    """
    os.makedirs(dest, exist_ok=True)
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "-z"],
        capture_output=True, cwd=ROOT, check=True).stdout
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        src = os.path.join(ROOT, rel)
        # A path git knows but that is gone, or a submodule directory.
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    quiet = {"capture_output": True, "cwd": dest}
    subprocess.run(["git", "init", "-q", "."], **quiet)
    subprocess.run(["git", "config", "user.email", "mutate@invalid"], **quiet)
    subprocess.run(["git", "config", "user.name", "mutate"], **quiet)
    subprocess.run(["git", "add", "-A"], **quiet)
    subprocess.run(["git", "commit", "-qm", "control"], **quiet)
    return dest


def classify(returncode, out, expect):
    """The verdict, as a pure function of what the run produced.

    SEPARATED SO IT CAN BE TESTED. Everything else here copies trees
    and spawns processes; this is the part that decides, and it has
    been wrong twice - once accepting any non-zero exit, once accepting
    printed text without an exit code to match. Both were found by
    review rather than by this tool, which is the joke it deserves.

    BOTH SIGNALS ARE REQUIRED. A test module that prints a FAIL line
    and exits zero has not failed - its runner has swallowed the
    result, which is a broken harness rather than a caught mutation,
    and reporting CAUGHT there would hide exactly the defect this tool
    exists to find. Reported by review of 04ad1d7.
    """
    named = any(mark in out for mark in
                ("FAIL  %s:" % expect, "ERROR %s:" % expect,
                 "FAIL  %s " % expect, "ERROR %s " % expect))
    if named and returncode == 0:
        return "BROKEN-RUNNER", (
            "%s printed a failure but %s exited 0" % (expect, "the module"))
    if named:
        line = [ln.strip() for ln in out.splitlines()
                if (ln.startswith("FAIL") or ln.startswith("ERROR"))
                and expect in ln]
        return "CAUGHT", (line[0][:100] if line else expect)

    others = [ln.strip() for ln in out.splitlines()
              if ln.startswith("FAIL") or ln.startswith("ERROR")]
    if others:
        return "WRONG-TEST", "%s passed; %s" % (expect, others[0][:70])
    if returncode != 0:
        return "WRONG-TEST", ("%s passed; module exited %d with no named "
                              "failure" % (expect, returncode))
    return "SURVIVED", "%s still passes" % expect


def _apply(tree, old, new, path):
    p = os.path.join(tree, path)
    with io.open(p, encoding="utf-8") as f:
        s = f.read()
    if s.count(old) != 1:
        return s.count(old)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(s.replace(old, new))
    return 1


def run_one(name, path, old, new, module, expect, verbose=False):
    tmp = tempfile.mkdtemp(prefix="mutate-")
    try:
        tree = _copy(os.path.join(tmp, "t"))
        hits = _apply(tree, old, new, path)
        if hits != 1:
            # The anchor no longer matches: the code moved and the
            # claim was never re-checked. That is a finding in itself.
            return "STALE", "anchor matched %d times in %s" % (hits, path)

        # THE MUTATION MUST STILL BE VALID CODE. A syntax error makes
        # every test in the module blow up, which the old runner scored
        # as a catch - it accepted ANY non-zero exit. That proves
        # nothing about the behaviour under test, and it is the same
        # defect this tool exists to find, inside the tool. Reported by
        # review of fdda51b.
        mutated = os.path.join(tree, path)
        if path.endswith(".py"):
            try:
                with io.open(mutated, encoding="utf-8") as f:
                    compile(f.read(), mutated, "exec")
            except SyntaxError as e:
                return "INVALID", "mutation does not compile: %s" % e

        r = subprocess.run([PY, os.path.join(tree, "tests", module)],
                           capture_output=True, text=True, cwd=tree)
        return classify(r.returncode, r.stdout + r.stderr, expect)
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
    if not picked:
        # A MISTYPED FILTER MUST NOT LOOK LIKE SUCCESS. "all 0 mutations
        # caught" and an exit of zero is a CI step that does no work and
        # goes green - the same failure mode as a test that cannot fail.
        # Reported by review of 04ad1d7.
        print("No mutation matches %r." % a.k)
        print("Nothing was run. %d mutations exist; --list shows them."
              % len(MUTATIONS))
        return 2
    if a.list:
        for name, path, _, _, module, expect in picked:
            print("%-34s %-22s %s" % (name, module, expect))
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
    for name, path, old, new, module, expect in picked:
        verdict, detail = run_one(name, path, old, new, module, expect)
        print("%-34s %-9s %s" % (name, verdict, detail))
        sys.stdout.flush()
        if verdict != "CAUGHT":
            bad += 1

    print("")
    if bad:
        print("%d of %d mutations were not caught." % (bad, len(picked)))
        print("")
        print("SURVIVED   - the named test does not guard the behaviour "
              "it claims to.")
        print("             Fix the TEST, not this file.")
        print("WRONG-TEST - something failed, but not the test that "
              "claims this.")
        print("             The claim is mis-attributed.")
        print("INVALID    - the mutation does not compile, so it proves "
              "nothing.")
        print("             Fix the mutation.")
        print("BROKEN-RUNNER - a failure was printed and the module still "
              "exited 0.")
        print("             The harness is not propagating failure. Fix "
              "that first.")
        print("STALE      - the code moved and nobody re-checked the "
              "claim.")
        return 1
    print("all %d mutations caught" % len(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
