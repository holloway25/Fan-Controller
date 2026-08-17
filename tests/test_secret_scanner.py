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
    # Review of 085e454: the placeholder list was a SUBSTRING search and
    # included the words "password" and "secret", so a real value
    # containing either was waved through.
    _f('{k} = "Password123!"'),
    _f('{k} = "my{s}value9"'),
    # Same review: the identifier exemption is for code references and
    # was being applied to YAML, where there is nothing to refer to.
    _f("{k}: CorrectHorseBatteryStaple"),
    _f("{a}: SomeLongAlphabeticKey"),
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


def test_a_real_value_containing_the_word_password_is_still_caught():
    """Review of 085e454. The placeholder list was a substring search
    and had "password" in it, so Password123! excused itself."""
    C = _scanner()
    assert C.suspicious(_f('{k} = "Password123!"')), "Password123! passed"
    assert C.suspicious(_f('{k} = "my{s}value9"')), "mysecretvalue9 passed"
    # And the marker values must still pass, or the fix has just made
    # the scanner unusable.
    assert not C.suspicious(_f('{k} = "placeholder-not-a-real-password"'))
    assert not C.suspicious(_f('{k} = "CHANGEME"'))
    assert not C.suspicious(_f('{k} = "your-password-here"'))
    return "the word is not a licence"


def test_a_yaml_passphrase_is_caught():
    """The identifier exemption exists for "password = stored_secret",
    a reference in code. YAML has the same shape and no variables."""
    C = _scanner()
    assert C.suspicious(_f("{k}: CorrectHorseBatteryStaple")), \
        "an alphabetic YAML passphrase passed"
    # A Python reference must still pass: that is what correct code
    # looks like and flagging it makes the scanner noise.
    assert not C.suspicious(_f("{k} = stored_{s}"))
    return "YAML is data, code is a reference"


def test_a_failing_git_command_is_not_reported_as_clean():
    """Empty output from a failed command scanned clean, which is
    indistinguishable from a genuinely clean tree - the worst failure
    mode a check like this can have."""
    C = _scanner()
    try:
        C._git("show", "definitely-not-a-ref:nope")
    except RuntimeError:
        return "a git failure raises"
    raise AssertionError("a failing git command returned quietly")


def test_the_scanner_scans_its_own_test_file():
    """This file used to be exempt. Its fixtures assemble their keys at
    runtime instead, so the exemption was removed - an exemption list is
    a thing that grows until the check means nothing."""
    C = _scanner()
    assert "tests/test_secret_scanner.py" not in C.SKIP_PATHS, \
        "the test file is exempt again"
    return "no exemption for this file"


def test_prose_describing_an_assignment_is_not_a_secret():
    """The unquoted arm matches up to whitespace, so a sentence with an
    assignment quoted inside it left a trailing quote stuck to the
    value. The identifier test then failed on it and the scanner
    flagged its own documentation - the fifth time a check here has
    blocked its own commit."""
    C = _scanner()
    line = '# exists because "{k} = stored_{s}" is a reference,'
    assert not C.suspicious(_f(line)), "prose about a reference flagged"
    # And the real thing is still caught, quote or no quote.
    assert C.suspicious(_f('{k} = "Password123!"'))
    assert C.suspicious(_f('{k}="abcdef123456"'))
    return "prose passes, values do not"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
