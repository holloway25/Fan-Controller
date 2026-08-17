"""The mutation harness, tested like anything else.

tools/mutate.py exists because tests were passing without proving
anything. Its own verdict logic has now been wrong twice in the same
direction - once accepting ANY non-zero exit as a catch, once accepting
printed failure text with a zero exit - and both were found by review
rather than by the tool. That is the joke it deserves, and this file is
the answer to it.

classify() is a pure function of (returncode, output, expected test),
which is the whole reason it was pulled out of the runner.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                            # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "mutate.py")


def _tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mutate", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_named_failure_with_a_failing_exit_is_caught():
    m = _tool()
    out = "PASS  test_other  (fine)\nFAIL  test_wanted: the reason\n"
    verdict, detail = m.classify(1, out, "test_wanted")
    assert verdict == "CAUGHT", (verdict, detail)
    assert "the reason" in detail, detail
    return "the ordinary case"


def test_printed_failure_with_a_zero_exit_is_not_a_catch():
    """THE REVIEW'S CASE. A module that prints FAIL and exits 0 has not
    failed - its runner swallowed the result. Reporting CAUGHT there
    would hide exactly the defect this tool exists to find."""
    m = _tool()
    out = "FAIL  test_wanted: deliberately printed without failure\n"
    verdict, _ = m.classify(0, out, "test_wanted")
    assert verdict == "BROKEN-RUNNER", verdict
    return "printed text alone proves nothing"


def test_an_unrelated_failure_is_not_a_catch():
    m = _tool()
    out = "FAIL  test_something_else: unrelated\n"
    verdict, detail = m.classify(1, out, "test_wanted")
    assert verdict == "WRONG-TEST", (verdict, detail)
    assert "test_wanted passed" in detail, detail
    return "the claim must be the one that fails"


def test_a_bare_nonzero_exit_is_not_a_catch():
    """A module that dies without naming a test - an import error, a
    crash - says nothing about the behaviour under test."""
    m = _tool()
    verdict, _ = m.classify(1, "Traceback (most recent call last):\\n",
                            "test_wanted")
    assert verdict == "WRONG-TEST", verdict
    return "a crash is not evidence"


def test_a_clean_pass_is_a_survivor():
    m = _tool()
    verdict, _ = m.classify(0, "PASS  test_wanted  (fine)\\n", "test_wanted")
    assert verdict == "SURVIVED", verdict
    return "green means the test did not notice"


def test_an_error_line_counts_as_a_catch():
    """ERROR and FAIL are both the named test not passing."""
    m = _tool()
    out = "ERROR test_wanted: RuntimeError: boom\\n"
    verdict, _ = m.classify(1, out, "test_wanted")
    assert verdict == "CAUGHT", verdict
    return "ERROR counts too"


def test_every_mutation_names_a_test_that_exists():
    """A claim pointing at a test that was renamed or deleted can never
    be caught, and would read as SURVIVED - blaming the test rather
    than the stale claim."""
    m = _tool()
    missing = []
    for name, path, _old, _new, module, expect in m.MUTATIONS:
        p = os.path.join(ROOT, "tests", module)
        if not os.path.exists(p):
            missing.append("%s: no module %s" % (name, module))
            continue
        if ("def %s(" % expect) not in open(p, encoding="utf-8").read():
            missing.append("%s: %s not in %s" % (name, expect, module))
    assert not missing, "\\n".join(missing)
    return "%d claims point at real tests" % len(m.MUTATIONS)


def test_every_mutation_anchor_still_matches_once():
    """A moved anchor makes a claim untestable. The runner reports
    STALE, but only when someone runs it - this fails in the ordinary
    suite instead."""
    m = _tool()
    bad = []
    for name, path, old, _new, _module, _expect in m.MUTATIONS:
        src = open(os.path.join(ROOT, path), encoding="utf-8").read()
        n = src.count(old)
        if n != 1:
            bad.append("%s: anchor matches %d times in %s" % (name, n, path))
    assert not bad, "\\n".join(bad)
    return "all anchors current"


def test_a_filter_matching_nothing_fails():
    """A mistyped filter in CI must not do no work and go green."""
    r = subprocess.run([sys.executable, TOOL, "-k", "no-such-mutation"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2, (r.returncode, r.stdout[-200:])
    assert "No mutation matches" in r.stdout, r.stdout[-200:]
    return "an empty run is a failure"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
