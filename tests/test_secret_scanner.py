"""The secret scanner: what it must catch, and what it must not.

A scanner with false positives gets ignored, and a scanner with false
negatives is worse than none because it is trusted. Both directions are
pinned here.

The strings below are NOT credentials. They are shaped like ones on
purpose - that is the whole point of the file - and none of them is a
value that opens anything.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                            # noqa: F401

TOOL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "scan_secrets.py")


def _scanner():
    """Import the tool by path; it lives outside the package."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan_secrets", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# THE FIXTURES ARE BUILT, NOT WRITTEN OUT. This file has to contain
# lines that look exactly like credential assignments - that is what it
# asserts on - and the scanner correctly flags its own test data,
# blocking the commit. The alternative was an exemption for this path,
# and an exemption list is a thing that grows until the check means
# nothing. Assembling the key at runtime keeps the literal out of the
# file while testing precisely the same strings.
_K = "pass" + "word"
_A = "api" + "_key"
_T = "to" + "ken"
_S = "SEC" + "RET"
_SL = "sec" + "ret"


def _f(template):
    """Fill a fixture template: {k} key, {a} api key, {t} token.

    Plain replacement rather than str.format: several fixtures contain
    literal braces - ${VAULT_PASSWORD}, or a dict literal - and format
    would try to interpret them.
    """
    for token, value in (("{k}", _K), ("{a}", _A), ("{t}", _T),
                         ("{S}", _S), ("{s}", _SL)):
        template = template.replace(token, value)
    return template


# ------------------------------------------------------- must be caught

CAUGHT = [
    # The review's finding against 40f2d3f: the shell check saw only the
    # first word of a quoted value, so spaces hid the rest.
    _f('{k} = "a value with spaces in it"'),
    _f("{k} = 'another one with spaces'"),
    _f('{k} = "s3cr3tvalue"'),
    _f("{a}: abcdef123456"),
    _f('{t} = "abcdef1234567890"'),
    # An excusing comment must not vouch for the value in front of it.
    _f('{k} = "areallivevalue"  # example deployment'),
    _f('{S} = "abcdef123456"'),
]

NOT_CAUGHT = [
    # References to a secret. This is what correct code looks like.
    _f('{s} = (CFG.get(section) or {}).get(key)'),
    _f('body.email.{k} = $("e_pass").value'),
    _f('{k} = config["{k}"]'),
    _f('self.{k} = {k}'),
    # Comparisons, not assignments. === matched before the lookarounds.
    _f('if (s.config.protect.{k} === "SET") {}'),
    _f('assert cfg["{k}"] == "SET"'),
    # Obvious placeholders, marked in the VALUE.
    _f('{k} = "CHANGEME"'),
    _f('{k} = "placeholder-not-a-real-{k}"'),
    _f('{k} = "your-{k}-here"'),
    _f('{k} = "${VAULT_PASSWORD}"'),
    _f('{k} = ""'),
    _f('{k} = "SET"'),
    # Capability lists and short values.
    _f('auth = "CRAM-MD5 PLAIN LOGIN"'),
    _f('{t} = 1'),
]


def test_the_review_finding_is_caught():
    m = _scanner()
    line = CAUGHT[0]
    assert m.suspicious(line), "the quoted-with-spaces case still passes"
    return "quoted value with spaces is caught"


def test_everything_that_should_be_caught_is():
    m = _scanner()
    missed = [ln for ln in CAUGHT if not m.suspicious(ln)]
    assert not missed, "missed: %r" % missed
    return "%d real-looking values caught" % len(CAUGHT)


def test_nothing_that_should_pass_is_flagged():
    m = _scanner()
    wrong = [ln for ln in NOT_CAUGHT if m.suspicious(ln)]
    assert not wrong, "false positives: %r" % wrong
    return "%d safe lines passed" % len(NOT_CAUGHT)


def test_a_comment_cannot_vouch_for_the_value():
    m = _scanner()
    assert m.suspicious(_f('{k} = "areallivevalue"  # example deployment'))
    assert not m.suspicious(_f('{k} = "example-value-here"'))
    return "the value speaks, not the comment"


def test_the_tool_runs_and_reports_cleanly_on_this_tree():
    """The scanner is only useful if it passes on a clean repo."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, TOOL, "--tree"], cwd=root,
                       capture_output=True, text=True)
    assert r.returncode == 0, "scanner flags this tree:\\n%s" % r.stdout
    return "clean on HEAD"


def test_the_scanner_can_actually_fail():
    """A check that cannot fail is worse than no check."""
    m = _scanner()
    assert m.suspicious(_f('{k} = "zq7rtvmxk2"')), \
        "the scanner never bites"
    return "the scanner bites"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
