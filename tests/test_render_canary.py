"""The render canary: the server had data and the screen showed nothing.

A BROKEN PAGE LOOKS LIKE A WORKING PAGE. On 2026-08-16 the activity log
lost its colours and nobody noticed for a day - every test green, the
page rendering happily, wrong. This does not check that the drawing is
correct; that needs eyes, or a browser in CI, which this repository has
decided against. It checks the one thing a machine can see from here.

The page half is JavaScript and is asserted structurally, as the rest
of the page is. The server half - the code, the log line, the fact that
a render report is not confused with a script error - is tested
properly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page():
    return open(os.path.join(SRC, "templates", "index.html"),
                encoding="utf-8").read()


def squashed():
    import re
    return re.sub(r"\s+", "", page())


# ------------------------------------------------------------ the server

def test_a_render_report_is_logged_with_its_own_code():
    """FC-208 says the DISPLAY failed. A reader must not go looking for
    a fault in the controller that is not there."""
    C = harness.load()
    cl = C.app.test_client()
    r = cl.post("/api/clienterror",
                json={"msg": "the CO2 trace rendered nothing for 900 points",
                      "src": "render", "line": 0, "kind": "render"})
    assert r.status_code == 200, r.status_code
    msgs = [e["msg"] for e in C.load_events()]
    assert any(m.startswith(C.FC_RENDER_EMPTY) for m in msgs), msgs[:3]
    return "FC-208 in the activity log"


def test_a_script_error_is_not_given_the_render_code():
    """They are different animals: one failed, the other produced
    nothing. Conflating them would make the code useless for finding
    either."""
    C = harness.load()
    C.app.test_client().post("/api/clienterror",
                             json={"msg": "TypeError: x is not a function",
                                   "src": "index.html", "line": 12})
    msgs = [e["msg"] for e in C.load_events()]
    assert msgs, "nothing logged"
    assert not msgs[0].startswith(C.FC_RENDER_EMPTY), msgs[0]
    assert "UI error" in msgs[0], msgs[0]
    return "script errors stay uncoded"


def test_an_unknown_kind_is_treated_as_a_script_error():
    """The field comes from a browser, so it is input."""
    C = harness.load()
    C.app.test_client().post("/api/clienterror",
                             json={"msg": "something", "kind": "nonsense"})
    msgs = [e["msg"] for e in C.load_events()]
    assert not msgs[0].startswith(C.FC_RENDER_EMPTY), msgs[0]
    return "unknown kinds are not promoted"


def test_the_render_code_is_in_the_failure_series():
    """2xx: something failed and nobody was emailed. A render failure
    must not send mail - it is a screen, and the fan is fine."""
    C = harness.load()
    assert C.FC_RENDER_EMPTY == "FC-208", C.FC_RENDER_EMPTY
    cls = C.event_class(C.FC_RENDER_EMPTY + " UI error: nothing drawn")
    assert cls == "fault", cls
    return "FC-208, 2xx, no mail"


def test_a_malformed_report_does_not_raise():
    C = harness.load()
    cl = C.app.test_client()
    for body in ({"kind": "render"}, {"msg": None, "kind": "render"},
                 {"msg": "x", "kind": ["render"]}):
        r = cl.post("/api/clienterror", json=body)
        assert r.status_code == 200, (body, r.status_code)
    return "junk reports are survivable"


# -------------------------------------------------------------- the page

def test_the_page_checks_that_it_drew_something():
    html = squashed()
    assert "functioncanary(s)" in html, "no canary"
    assert "canary(s);" in html, "the canary is never called"
    assert '"render")' in html, "reports are not marked as render failures"
    return "the page reports drawing nothing"


def test_the_canary_reports_once_per_condition():
    """A dashboard left open on a wall must not turn a broken render
    into a request every five seconds."""
    html = squashed()
    assert "constCANARY={log:false,trace:false}" in html, "no latch"
    assert "CANARY.log=true" in html and "CANARY.trace=true" in html, (
        "a condition can report repeatedly")
    return "latched per condition"


def test_the_canary_cannot_break_the_page():
    """It exists to notice a broken render, so it must not become one."""
    html = squashed()
    i = html.index("functioncanary(s)")
    body = html[i:i + 1400]
    assert "try{" in body and "catch(e)" in body, (
        "the canary is not wrapped")
    return "wrapped"


def test_the_empty_state_is_not_reported_as_a_failure():
    """"Nothing yet." is a correct render of no events. Reporting it
    would train everyone to ignore the code."""
    html = squashed()
    assert "!el.textContent" in html, (
        "the log check does not allow for the empty-state text")
    return "an empty log is allowed to be empty"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
