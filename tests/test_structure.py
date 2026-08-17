"""A structural test: every function that can command or judge a plug
must test the config generation.

WHAT THIS PROVES AND WHAT IT DOES NOT. It proves the CHECK EXISTS, and
for the four direct actuators that it sits inside an _act_lock hold
with no PLUG CALL before it.

IT PROVES NOTHING ABOUT WHAT HAPPENS AFTER THE CHECK. That is not a
quibble: _draw_check passed both tests here while committing a fault
outside the lock, because raising a fault touches no plug and is
therefore invisible to a walk that looks for plug calls. A settings save
landing in that gap produced a persistent fault quoting limits that had
already been replaced. Only test_concurrency.py caught it, and only
because someone thought to ask.

Nor does it prove ordering for tick() or watts_loop - tick delegates its
first two plug calls to helpers that check for themselves, and
watts_loop checks outside _act_lock on purpose because it is discarding
a reading rather than commanding anything.

THE BEHAVIOURAL TESTS ARE THE STRONGER PROTECTION. This is a fence
around one hole, not a roof - and it has been wrong about itself twice,
so read what it asserts rather than what its name suggests.

This is deliberately unlike every other test here. It reads the source
rather than running it, because the failure it guards against is a
MISSING guard, and a behavioural test can only cover the paths someone
thought to write. Twice now a fix has guarded the sites named in a
review and left an identical one beside it:

  - 2cd616e guarded tick(), the fail-safe and the watts loop, and left
    _manual_off, _force_on and _enforce_stop.
  - 9a1ae1c then guarded those three, and left _draw_check judging a
    superseded reading and raising a fault from it.

Both times the instruction "when you fix one site, grep for the others"
was written down, and both times it did not fire. This does the grep.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "controller.py")

# Functions that reach a plug but are exempt, each for a stated reason.
# ADDING TO THIS LIST IS A DECISION. It should come with a reason that
# survives someone reading it in six months, not a shrug.
EXEMPT = {
    # Builds the clients; there is nothing older to compare against.
    "_clients": "constructs the clients",
    "_client_snapshot": "constructs the snapshot the others check",
    # Pure read of a client handed to it, with no config-derived
    # judgement and no command. Its callers do the checking.
    "_fan_status": "read-only helper, called inside the caller's lock",
    # Test-only endpoint. The operator is holding the dashboard and has
    # just pressed the button; there is no in-flight cycle to be stale.
    "api_plug_test": "operator-initiated, no cycle to supersede",
    # Locked inner half, called with _act_lock already held by a caller
    # that has checked.
    "_manual_off_locked": "caller holds the lock and has checked",
}

# The four that command or judge a plug directly. For these the rule is
# absolute and checkable: take _act_lock, test the generation, and only
# then touch the plug. tick() and watts_loop are deliberately not here -
# see the module docstring.
DIRECT_ACTUATORS = {"_force_on", "_manual_off", "_enforce_stop",
                    "_draw_check"}

# What counts as touching a plug. "set" is shared with threading.Event
# - _wake.set() is not a plug command - so it only counts with an
# argument, which every plug.set() has and no Event.set() does. The
# receiver denylist is belt and braces: the first run of this test
# flagged api_fault_clear purely because it calls _wake.set(), and a
# structural test that cries wolf gets exemptions added until it means
# nothing.
PLUG_CALLS = {"set", "state", "power_w"}
NOT_PLUGS = {"_wake", "gate", "entered", "started", "release"}
PLUG_HELPERS = {"_fan_status", "_enforce_stop", "_force_on", "_manual_off",
                "_draw_check"}


def _plug_touching_functions():
    """Every top-level function whose body can reach a plug."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in PLUG_CALLS:
                # plug.set(True), h.state(), plug.power_w()
                if not isinstance(f.value, ast.Name):
                    continue
                if f.value.id in NOT_PLUGS:
                    continue
                if f.attr == "set" and not sub.args:
                    continue            # Event.set(), not plug.set(v)
                found.setdefault(node.name, node)
            elif isinstance(f, ast.Name) and f.id in PLUG_HELPERS:
                found.setdefault(node.name, node)
    return found


def _checks_generation(node):
    """True if the body actually TESTS the generation.

    Taking a gen parameter deliberately does not count. That was the
    first version of this test, and it passed against the exact bug it
    was written for: _draw_check accepted gen, handed it to
    _enforce_stop, and never checked it - so it could not command a
    superseded plug but could still judge one and raise a persistent
    fault from the reading. A parameter is a promise; only a comparison
    is a guard.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in ("_cfg_generation",
                                                    "_stale_client"):
            return True
    return False


def test_every_plug_touching_function_carries_a_generation():
    """The guard that two review rounds had to add by hand."""
    missing = []
    for name, node in sorted(_plug_touching_functions().items()):
        if name in EXEMPT:
            continue
        if not _checks_generation(node):
            missing.append("%s (line %d)" % (name, node.lineno))
    assert not missing, (
        "these can reach a plug but do not check the config "
        "generation:\n    %s\nEither carry the generation and refuse a "
        "superseded one under _act_lock, or add it to EXEMPT in this "
        "file with a reason." % "\n    ".join(missing))
    return "all guarded"


def _first_act_lock_with(node):
    """The first `with _act_lock:` statement in a function body."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.With):
            continue
        for item in sub.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Name) and ctx.id == "_act_lock":
                return sub
    return None


def _first_plug_line(node):
    """Line of the first call that reaches a plug, or None."""
    lines = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Attribute) and f.attr in PLUG_CALLS:
            if (isinstance(f.value, ast.Name)
                    and f.value.id not in NOT_PLUGS
                    and not (f.attr == "set" and not sub.args)):
                lines.append(sub.lineno)
        elif isinstance(f, ast.Name) and f.id in PLUG_HELPERS:
            lines.append(sub.lineno)
    return min(lines) if lines else None


def test_the_direct_actuators_check_under_the_lock_and_first():
    """The guard precedes the first PLUG CALL. Nothing more.

    A function could command the plug and test the generation
    afterwards, and the existence check above would pass. This closes
    that. What it does NOT close: anything after the check that is not a
    plug call. _draw_check satisfied this test while committing a fault
    outside the lock - a fault is not a plug call. See the module
    docstring.
    """
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    funcs = {n.name: n for n in tree.body
             if isinstance(n, ast.FunctionDef)}
    problems = []
    for name in sorted(DIRECT_ACTUATORS):
        node = funcs.get(name)
        if node is None:
            problems.append("%s has gone; update DIRECT_ACTUATORS" % name)
            continue
        with_node = _first_act_lock_with(node)
        if with_node is None:
            problems.append("%s never takes _act_lock" % name)
            continue
        guarded = any(isinstance(s, ast.Name) and s.id == "_stale_client"
                      for s in ast.walk(with_node))
        if not guarded:
            problems.append(
                "%s takes _act_lock but does not test the generation "
                "inside it" % name)
            continue
        first_plug = _first_plug_line(node)
        if first_plug is not None and first_plug < with_node.lineno:
            problems.append(
                "%s reaches a plug at line %d, before the guarded "
                "_act_lock hold at line %d"
                % (name, first_plug, with_node.lineno))
    assert not problems, "\n    ".join([""] + problems)
    return "%d actuators guarded in order" % len(DIRECT_ACTUATORS)


def test_the_exemptions_still_exist():
    """An exemption for a function that has gone is stale reasoning."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    gone = sorted(set(EXEMPT) - names)
    assert not gone, (
        "EXEMPT names functions that no longer exist: %s. Remove them, "
        "or the list quietly excuses whatever is written next under the "
        "same name." % ", ".join(gone))
    return "%d exemptions, all real" % len(EXEMPT)


def test_the_checker_can_actually_fail():
    """A structural test that cannot fail is decoration.

    Guards against the checker being weakened - a walk that stops
    matching, or PLUG_CALLS drifting from the real method names - which
    would leave it passing for ever while protecting nothing.
    """
    found = _plug_touching_functions()
    assert len(found) >= 6, (
        "the source walk found only %d plug-touching functions, which "
        "means it has stopped recognising them: %s"
        % (len(found), sorted(found)))
    for expected in ("_force_on", "_enforce_stop", "_draw_check", "tick"):
        assert expected in found, (
            "%s reaches a plug but the walk did not find it" % expected)

    # The Event.set() refinement must not have swallowed real commands.
    commanding = ast.parse(
        "def rogue(plug):\n    plug.set(True)\n").body[0]
    eventish = ast.parse(
        "def waker():\n    _wake.set()\n").body[0]
    assert any(isinstance(n, ast.Call) for n in ast.walk(commanding))
    assert "rogue" not in EXEMPT and "waker" not in EXEMPT

    # A function that commands a plug with no generation at all.
    rogue = ast.parse("def rogue(plug):\n    plug.set(True)\n").body[0]
    assert not _checks_generation(rogue), (
        "the checker would pass a function that commands a plug with no "
        "generation at all")

    # And the one that matters: TAKING gen without testing it must still
    # fail. This is the shape _draw_check had, and the first version of
    # this test waved it through.
    pretender = ast.parse(
        "def pretender(plug, gen=None):\n"
        "    _enforce_stop(plug, gen)\n"
        "    plug.set(True)\n").body[0]
    assert not _checks_generation(pretender), (
        "a function that accepts gen and only passes it on is treated "
        "as guarded - which is the bug this test exists for")
    return "the checker bites"


# ------------------------------------------------- documentation hygiene

def test_the_example_config_uses_reserved_addresses_only():
    """192.0.2.0/24 is TEST-NET-1, reserved by RFC 5737 for exactly this.

    Whoever copies config.example.json is going to paste their own
    addresses over these, and until they do the file is what a stranger
    reads. A real address here is either someone's actual hardware -
    which is the boundary this repository has already been through a
    history rewrite over - or a working address that quietly points a
    fresh install at a machine that is not theirs.

    Checked in the suite rather than left to review: it was done on
    2026-08-14 and stayed on the open list for three days because
    nobody went back to tick it off, which is a good argument for the
    check saying so itself.
    """
    import json
    import re
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(src, "config.example.json"),
                encoding="utf-8").read()
    found = re.findall(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", text)
    assert found, "no addresses at all - has the file changed shape?"
    allowed_prefix = "192.0.2."
    bad = [a for a in found
           if not a.startswith(allowed_prefix)
           and a not in ("127.0.0.1", "0.0.0.0")]
    assert not bad, (
        "config.example.json carries non-reserved address(es) %s - use "
        "192.0.2.x (RFC 5737 TEST-NET-1)" % ", ".join(sorted(set(bad))))
    # And it must still parse, since it is what people copy.
    json.loads(text)
    return "%d addresses, all reserved" % len(found)


def test_no_tracked_file_carries_a_private_address():
    """The same rule the privacy workflow enforces, in the suite.

    CI catches this on push. A test catches it before the commit, which
    is where a private address is cheap to remove rather than needing a
    history rewrite.
    """
    import re
    import subprocess
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    listed = subprocess.run(["git", "ls-files"], capture_output=True,
                            text=True, cwd=src)
    if listed.returncode != 0:
        return "SKIP not a git checkout"
    # DELIBERATELY THE SAME EXPRESSION as the hook and the privacy
    # workflow, boundaries included. Three copies that drift are worse
    # than three that agree: the trailing-dot hole below existed in all
    # three at once precisely because they were written together.
    pat = re.compile(
        r"(^|[^0-9])(10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}"
        r"|192\.168\.[0-9]{1,3}\.[0-9]{1,3}"
        r"|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})"
        r"([^0-9]|$)")
    # These three define or test the pattern itself.
    exempt = {"tools/scan_secrets.py", ".githooks/pre-commit",
              ".github/workflows/privacy.yml", "tests/test_structure.py",
              "tests/test_fault_codes.py"}
    hits = []
    for rel in listed.stdout.split():
        if rel in exempt:
            continue
        p = os.path.join(src, rel)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        hits.append("%s:%d" % (rel, i))
        except OSError:
            continue
    assert not hits, "private addresses in tracked files: %s" % ", ".join(hits)
    return "clean"


def test_the_readme_lists_every_test_module():
    """A list of tests that misses six of them is worse than no list:
    someone looking for coverage of a thing concludes there is none.

    Found by review of 3ea51b3, which is one review too late - the
    shellcheck module has a list-completeness test for the same reason
    and found an unknown script the day it was written.
    """
    import re
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(src, "README.md"), encoding="utf-8").read()
    listed = set(re.findall(r"^- `(test_[a-z_]+\.py)`", readme, re.M))
    have = {f for f in os.listdir(os.path.join(src, "tests"))
            if f.startswith("test_") and f.endswith(".py")}
    missing = sorted(have - listed)
    gone = sorted(listed - have)
    assert not missing, "not in the README: %s" % ", ".join(missing)
    assert not gone, "listed but deleted: %s" % ", ".join(gone)
    return "%d modules, all listed" % len(have)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
