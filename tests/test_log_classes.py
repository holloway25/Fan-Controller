"""Log line classification: what an entry MEANS, decided in Python.

This lived in the dashboard's JavaScript until 2026-08-17, where the
only possible assertion was searching the page source for regexes. On
2026-08-16 the rules broke silently - they matched "^Draw fault" and
"^Fan ", and both stopped working the moment log lines gained an FC-
prefix. Faults rendered as ordinary text, every test stayed green, and
it was found because someone looked at a screen.

Moved so that breaking a rule breaks a test. The page now assigns one
field.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

# Every case, written as (message, expected class). Ordered by series
# so a gap is visible.
CASES = [
    # 0xx alarms are red.
    ("FC-001 Fan on \u2014 no reading for 30 min", "fault"),
    ("FC-002 Fan fault latched \u2014 plug not answering", "fault"),
    ("FC-003 Fan off \u2014 stop confirmed", "fault"),
    ("FC-004 Draw fault - 27.6 W outside 14-22 W for 30s", "fault"),
    ("FC-005 WARNING: fault could not be saved (disk full).", "fault"),
    ("FC-006 Fault still standing after restart \u2014 FC-003", "fault"),
    # 1xx all-clears are green.
    ("FC-100 Readings returned \u2014 normal control resumed", "ok"),
    ("FC-101 Fault cleared \u2014 FC-003", "ok"),
    ("FC-102 Fault saved \u2014 will survive a restart", "ok"),
    # 2xx failures are red: something went wrong, nobody was emailed.
    ("FC-200 TypeError: something odd", "fault"),
    ("FC-201 Protect unreachable", "fault"),
    ("FC-207 Fail-safe failed: PlugError: no route", "fault"),
    # 3xx depends on the mode recorded IN THE LINE.
    ("FC-300 Fan off \u2014 changed outside (Manual)", "ok"),
    ("FC-300 Fan on \u2014 changed outside (Auto)", "m-manual"),
    ("FC-300 Fan on \u2014 changed outside (Force on)", "m-manual"),
    # Uncoded lines fall through to the text rules.
    ("Fan on \u2014 1000 ppm", "ok"),
    ("Fan off \u2014 manual", "ok"),
    ("Fan on \u2014 forced on", "m-on"),
    ("Mode: Force on \u2014 fan on", "m-on"),
    ("Mode: Manual \u2014 fan off", "m-manual"),
    ("Mode: Manual", "m-manual"),
    ("Mode: Auto", "m-auto"),
    ("Draw fault - no reading outside 20-27 W for 32s", "fault"),
    ("Fan off \u2014 draw fault", "fault"),
    # And the ones that must NOT be coloured.
    ("Changed: watts_fault_seconds", ""),
    ("Changed: on_at, off_at", ""),
    ("Restarting on request", ""),
    ("Resumed after restart - no latch held", ""),
    ("Restart refused - the active fault is not saved to disk", ""),
]


def test_every_line_gets_the_class_it_should():
    C = harness.load()
    wrong = []
    for msg, want in CASES:
        got = C.event_class(msg)
        if got != want:
            wrong.append("%r -> %r, wanted %r" % (msg, got, want))
    assert not wrong, "\\n".join(wrong)
    return "%d messages classified" % len(CASES)


def test_a_settings_edit_naming_a_fault_is_not_a_fault():
    """"Changed: watts_fault_seconds" contains the word. A substring
    match turned settings edits red."""
    C = harness.load()
    assert C.event_class("Changed: watts_fault_seconds") == ""
    assert C.event_class("Changed: watts_min, watts_max") == ""
    return "settings stay neutral"


def test_the_code_prefix_does_not_hide_the_text_rules():
    """THE 2026-08-16 REGRESSION. Rules anchored on "^Draw fault" and
    "^Fan " stopped matching when the prefix arrived."""
    C = harness.load()
    # A code that is not one of the known series still gets its text
    # read, rather than falling through to no colour at all.
    assert C.event_class("FC-999 Fan on \u2014 1000 ppm") == "ok"
    assert C.event_class("FC-999 Draw fault - something") == "fault"
    return "the prefix is stripped, not obeyed blindly"


def test_an_unknown_line_is_not_coloured():
    """A colour has to mean something. Everything unrecognised takes
    the default rather than guessing."""
    C = harness.load()
    for msg in ("", "something entirely new", None, 42, {"a": 1}):
        assert C.event_class(msg) == "", repr(msg)
    return "unknown stays neutral"


def test_the_endpoint_serves_the_class_with_each_event():
    """The page assigns e.cls and nothing else, so the field has to be
    there."""
    C = harness.load()
    C._log("FC-003 Draw fault - 8.2 W outside 14-22 W for 30s")
    C._log("Changed: on_at")
    body = C.app.test_client().get("/api/state").get_json()
    evs = body["events"]
    assert evs, "no events served"
    bymsg = {e["msg"]: e.get("cls") for e in evs}
    assert bymsg["Changed: on_at"] == "", bymsg
    fault = [v for k, v in bymsg.items() if k.startswith("FC-003")]
    assert fault == ["fault"], bymsg
    return "every served event carries its class"


def test_the_class_is_not_written_to_disk():
    """The colour is a presentation decision that can change. Rewriting
    events.json to recolour a log would be absurd."""
    import json
    C = harness.load()
    C._log("FC-003 Draw fault - something")
    on_disk = json.load(open(C.EVENTS_PATH))
    assert on_disk and "cls" not in on_disk[0], on_disk[0]
    return "stored plain, served coloured"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
