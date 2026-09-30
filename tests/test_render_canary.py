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


def test_conventional_icon_paths_serve_the_dashboard_icons():
    C = harness.load()
    cl = C.app.test_client()
    paths = ("/favicon.ico", "/apple-touch-icon.png",
             "/apple-touch-icon-precomposed.png")
    for path in paths:
        r = cl.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert r.content_type == "image/png", (path, r.content_type)
        assert r.data.startswith(b"\x89PNG\r\n\x1a\n"), path
    return "browser icon probes are answered"


def test_icon_paths_stay_public_when_authentication_is_enabled():
    C = harness.load()
    C.CFG["ui"]["password_hash"] = C.set_ui_password("test-password")
    cl = C.app.test_client()
    for path in ("/favicon.ico", "/apple-touch-icon.png",
                 "/apple-touch-icon-precomposed.png"):
        assert cl.get(path).status_code == 200, path
    return "wall-display icons remain public"


# -------------------------------------------------------------- the page

def test_the_canary_cannot_break_the_page():
    """It exists to notice a broken render, so it must not become one."""
    html = squashed()
    i = html.index("functioncanary(s)")
    body = html[i:i + 1400]
    assert "try{" in body and "catch(e)" in body, (
        "the canary is not wrapped")
    return "wrapped"


def test_a_render_report_is_not_swallowed_by_a_script_error():
    """SAME TEXT, DIFFERENT MEANING. Deduplication keyed on the message
    alone let whichever arrived second be discarded - so an FC-208
    could be silently eaten by an ordinary script error that happened
    to say the same thing, and the render failure would never reach the
    log. Reported by review of 3ea51b3.
    """
    C = harness.load()
    cl = C.app.test_client()
    cl.post("/api/clienterror", json={"msg": "same failure"})
    cl.post("/api/clienterror",
            json={"msg": "same failure", "kind": "render"})
    msgs = [e["msg"] for e in C.load_events()]
    coded = [m for m in msgs if m.startswith(C.FC_RENDER_EMPTY)]
    plain = [m for m in msgs
             if "same failure" in m and not m.startswith(C.FC_RENDER_EMPTY)]
    assert coded, "the render report was swallowed: %r" % msgs
    assert plain, "the script error was swallowed: %r" % msgs
    return "both survive"


def test_the_other_order_works_too():
    """The bug was symmetric; a fix that only handles one order is not
    a fix."""
    C = harness.load()
    cl = C.app.test_client()
    cl.post("/api/clienterror",
            json={"msg": "same failure", "kind": "render"})
    cl.post("/api/clienterror", json={"msg": "same failure"})
    msgs = [e["msg"] for e in C.load_events()]
    assert any(m.startswith(C.FC_RENDER_EMPTY) for m in msgs), msgs
    assert any("same failure" in m and not m.startswith(C.FC_RENDER_EMPTY)
               for m in msgs), msgs
    return "either order"


def test_a_repeat_of_the_same_kind_is_still_deduplicated():
    """The quiet period still has to work, or a page in a render loop
    fills the log it was meant to protect."""
    C = harness.load()
    cl = C.app.test_client()
    for _ in range(3):
        cl.post("/api/clienterror",
                json={"msg": "the same thing", "kind": "render"})
    msgs = [e["msg"] for e in C.load_events() if "the same thing" in e["msg"]]
    assert len(msgs) == 1, "logged %d times" % len(msgs)
    return "repeats still collapse"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
