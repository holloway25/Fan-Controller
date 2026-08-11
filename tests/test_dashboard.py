"""Dashboard wiring: the decisions the browser makes, not the server's.

These are SOURCE assertions against templates/index.html. There is no
JavaScript engine on the deployment host, so nothing here executes the
dashboard - it only checks that the wiring the server's behaviour
depends on is still present.

Be clear about the limit: MOST of these catch a line being DELETED
rather than being wrong. Two exceptions, added later, check the
OPERATORS in the fetch guard - that it compares for difference rather
than equality, and that the two halves are OR'd rather than AND'd -
because those are the two ways that guard has actually been wrong. The
limit stands everywhere else: a line referencing the right names and
using them backwards still passes the rest of this file.

They exist because the alternative for this particular defect was a
test named after the dashboard that never opened the file, which is
worse than no test.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(SRC, "templates", "index.html")


def page():
    return open(PAGE, encoding="utf-8").read()


def squashed():
    """The page with all whitespace removed.

    Token checks should survive re-indenting and line rewrapping, which
    change nothing about behaviour. Matching raw source would make this
    fail on a reformat and pass on a rename, which is backwards.
    """
    return re.sub(r"\s+", "", page())


def guard_condition(html):
    """The condition guarding the history fetch.

    Located by the HIST_BUSY flag rather than by line number, and
    returned as everything between the opening if( and that flag.
    """
    at = html.find("HIST_BUSY)")
    assert at != -1, "could not find the history fetch guard at all"
    start = html.rfind("if(", 0, at)
    assert start != -1, "the guard is not an if()"
    return html[start:at]


def test_the_fetch_guard_watches_the_generation_as_well():
    """The whole point of putting the generation in /api/state.

    The server refuses a cross-generation delta, but only if a request
    is made. A tab holding a revision the replacement has climbed back
    to sends none, so the refusal never fires and the chart stays stale.
    """
    cond = guard_condition(squashed())

    assert "HIST_REV" in cond, "the guard ignores the revision"
    assert "HIST_GEN" in cond, (
        "the guard decides using only the revision, so a tab that slept "
        "through a restart never asks and never learns the trace is from "
        "a process that no longer exists")
    return "guard watches both"


def test_the_fetch_guard_compares_for_difference_not_equality():
    """The inversion the test above cannot see.

    That one proves both names are MENTIONED. A guard that mentions
    them and compares with === refetches only while the client is
    already up to date, and stops the moment it falls behind - exactly
    backwards, and green against every other assertion in this file.

    Checks the OPERATOR rather than the operand order, so rewriting
    HIST_REV!==s.history_rev the other way round does not fail a test
    about something else.
    """
    cond = guard_condition(squashed())
    assert "===" not in cond, (
        "the fetch guard compares for equality, so it refetches while "
        "the trace is current and stops when it goes stale")
    assert cond.count("!==") == 2, (
        "expected two inequality comparisons, revision and generation, "
        "found %d" % cond.count("!=="))
    return "two !== and no ==="


def test_the_revision_and_generation_are_ored_not_anded():
    """Either one differing has to be enough on its own.

    Requiring BOTH is the 3e0aca4 defect reached by a different route:
    a tab holding a revision the replacement has climbed back to
    matches on the revision, so an AND never fires, nothing refetches,
    and the next delta is appended to a trace from a process that no
    longer exists.
    """
    cond = guard_condition(squashed())
    assert "||" in cond, (
        "the revision and generation checks are not OR'd, so one of "
        "them differing on its own does not trigger a fetch")
    return "revision OR generation"


def test_the_delta_request_sends_both_since_and_gen():
    """A delta the server cannot validate is one it will refuse."""
    html = squashed()
    assert "since=" in html, "the client never asks for a delta at all"
    assert "gen=" in html, (
        "the client asks for deltas without saying which process issued "
        "the revision, so every request falls back to a full copy")
    return "since and gen both sent"


def test_the_unsaved_fault_warning_is_rendered():
    """It is in /api/state and was invisible for months."""
    html = squashed()
    assert 'id="unsavedmsg"' in html, "no element to show it in"
    assert "s.fault_unsaved" in html, (
        "nothing reads the flag, so a fault that cannot reach disk shows "
        "only in the activity log")
    return "element present and driven from the flag"


def test_the_history_fetch_stores_what_the_response_carried():
    """Storing the revision seen in /api/state instead would lose any
    change that landed while the fetch was in flight."""
    html = squashed()
    assert "HIST_REV=h.history_rev" in html, (
        "the revision is not taken from the history response")
    assert "HIST_GEN=h.history_generation" in html, (
        "the generation is not taken from the history response")
    return "both taken from the response"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
