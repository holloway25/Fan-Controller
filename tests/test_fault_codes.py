"""Fault codes: the identifiers, and that they are actually attached.

Codes exist so that an episode can be found later across events.json,
the dashboard and the mail archive with one grep. That only works if
the code appears in all three, and if the numbers never move.

WHY THIS FILE EXISTS SEPARATELY from test_email.py: those tests pin the
EDGES - when a send happens - and they pass regardless of what the
message says. Every template in the feature was rewritten on 2026-08-16
and the whole email suite passed unchanged. That is the gap this file
closes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import test_email as E


# The numbers, written out. A test that reads the constants from the
# module would pass through any renumbering, which is the one mistake
# these codes exist to prevent.
EXPECTED = {
    "FC_SENSOR_COMMS": "FC-001",
    "FC_PLUG_COMMS": "FC-002",
    "FC_UNDER_POWER": "FC-003",
    "FC_OVER_POWER": "FC-004",
    "FC_FAULT_UNSAVED": "FC-005",
    "FC_RESTART_FAULT": "FC-006",
    "FC_SENSOR_RETURNED": "FC-100",
}


def test_the_codes_are_what_they_have_always_been():
    """Renumbering silently merges two faults in someone's archive."""
    C = harness.load()
    for name, value in sorted(EXPECTED.items()):
        got = getattr(C, name, None)
        assert got == value, "%s is %r, expected %r" % (name, got, value)
    return "%d codes pinned" % len(EXPECTED)


def test_no_two_codes_are_the_same():
    C = harness.load()
    seen = {}
    for name in EXPECTED:
        v = getattr(C, name)
        assert v not in seen, "%s and %s share %s" % (name, seen[v], v)
        seen[v] = name
    return "all distinct"


def test_alarms_are_0xx_and_all_clears_are_1xx():
    """A mail rule splits on the series, not on the wording."""
    C = harness.load()
    alarms = ["FC_SENSOR_COMMS", "FC_PLUG_COMMS", "FC_UNDER_POWER",
              "FC_OVER_POWER", "FC_FAULT_UNSAVED", "FC_RESTART_FAULT"]
    for name in alarms:
        n = int(getattr(C, name).split("-")[1])
        assert n < 100, "%s is not in the alarm series" % name
    n = int(C.FC_SENSOR_RETURNED.split("-")[1])
    assert n >= 100, "the all-clear is not in the 1xx series"
    return "0xx alarms, 1xx clears"


# ------------------------------------------------- draw fault classification

def test_a_silent_plug_is_a_plug_fault_not_a_fan_fault():
    """No reading cannot be under or over power - nothing was read."""
    C = harness.load()
    assert C._draw_fault_code(None, 15.0, 22.0) == C.FC_PLUG_COMMS
    return "silence is FC-002"


def test_under_and_over_are_judged_against_the_live_band():
    C = harness.load()
    assert C._draw_fault_code(0.0, 15.0, 22.0) == C.FC_UNDER_POWER
    assert C._draw_fault_code(8.2, 15.0, 22.0) == C.FC_UNDER_POWER
    assert C._draw_fault_code(27.6, 15.0, 22.0) == C.FC_OVER_POWER
    # A band the dashboard has widened moves the boundary with it.
    assert C._draw_fault_code(27.6, 15.0, 30.0) == C.FC_UNDER_POWER
    return "under, over, and the band that moved"


def test_the_code_mirrors_the_cause_text():
    """_draw_fault_code and _draw_fault_cause must not drift apart.

    They are two functions reading the same reading. If one says plug
    and the other says motor, the email contradicts itself.
    """
    C = harness.load()
    cause = C._draw_fault_cause(None, 15.0, 22.0)
    assert "plug" in cause.lower(), cause
    assert C._draw_fault_code(None, 15.0, 22.0) == C.FC_PLUG_COMMS
    cause = C._draw_fault_cause(27.6, 15.0, 22.0)
    assert "more power" in cause.lower(), cause
    assert C._draw_fault_code(27.6, 15.0, 22.0) == C.FC_OVER_POWER
    return "code and prose agree"


# ------------------------------------------------------ attached to the fault

def test_the_fault_carries_its_code_on_disk():
    """A restart notice can then say WHICH fault survived."""
    C = harness.load()
    E.wire(C)
    f = E.raise_draw_fault(C, 0.0)
    assert f.get("code") == C.FC_UNDER_POWER, f
    return "code stored on the fault"


def test_the_activity_log_carries_the_code():
    """One grep of events.json has to find the episode."""
    C = harness.load()
    E.wire(C)
    f = E.raise_draw_fault(C, 0.0)
    assert f["msg"].startswith(C.FC_UNDER_POWER), f["msg"]
    return "code leads the log line"


def test_a_silent_plug_raises_fc002_end_to_end():
    C = harness.load()
    sent = E.wire(C)
    f = E.raise_draw_fault(C, None)
    assert f.get("code") == C.FC_PLUG_COMMS, f
    subject, body = sent[0]
    assert subject.startswith(C.FC_PLUG_COMMS), subject
    assert C.FC_PLUG_COMMS in body, body[:120]
    return "FC-002 in the fault, the subject and the body"


# ------------------------------------------------------------ the subjects

def test_every_alert_subject_starts_with_its_code():
    """Filing rules key on the code, so it leads. Not buried mid-line."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 27.6, lo=15.0, hi=22.0)
    subject, _ = sent[0]
    assert subject.startswith(C.FC_OVER_POWER), subject
    assert "over power" in subject.lower(), subject
    return "code first, then the fact"


def test_the_subject_says_what_happened_not_just_that_something_did():
    """Phone notifications truncate. The useful words go early."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    subject, _ = sent[0]
    assert "8.2" in subject, subject
    assert subject.index("8.2") < 45, "the reading is past the fold: %r" % subject
    return "reading visible in the first 45 characters"


# --------------------------------------------------------------- the body

def test_the_body_carries_the_fields_someone_acts_on():
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    _, body = sent[0]
    for field in ("Expected range", "Actual reading", "Action taken",
                  "Manual action", "How to clear", "Controller mode",
                  "Likely causes"):
        assert field in body, "%s missing from the body: %r" % (field, body)
    assert "15 - 22 W" in body, body
    assert "8.2 W" in body, body
    return "all seven fields present"


def test_a_latching_fault_says_how_to_clear_it():
    """An alert read at 2am has to carry its own remedy."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 0.0)
    _, body = sent[0]
    assert "Manual action:" in body and "Yes" in body, body
    assert "Acknowledge" in body, body
    return "remedy in the message"


def test_the_body_does_not_carry_a_site_address():
    """Site detail is not repo content - there is a CI check for this.

    The dashboard is named as a place, never as an address.
    """
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 0.0)
    _, body = sent[0]
    for leak in ("http://", "https://", ".au", ".local", "192.168."):
        assert leak not in body, "%r leaked into the body: %r" % (leak, body)
    return "no address in the message"


def test_the_timestamp_carries_a_zone():
    """16/08/2026 16:42 is ambiguous; the logs are epoch."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 0.0)
    _, body = sent[0]
    stamp = body.split("\n")[1]
    assert C.time.strftime("%Y"), "no clock"
    assert len(stamp.split()) == 3, "expected date, time and zone: %r" % stamp
    return "zone on the timestamp"


# ------------------------------------------------------------- the clears

def test_the_clear_codes_are_pinned_too():
    C = harness.load()
    assert C.FC_HARD_CLEARED == "FC-101", C.FC_HARD_CLEARED
    assert C.FC_FAULT_PERSISTED == "FC-102", C.FC_FAULT_PERSISTED
    for c in (C.FC_HARD_CLEARED, C.FC_FAULT_PERSISTED):
        assert int(c.split("-")[1]) >= 100, "%s is not an all-clear" % c
    return "FC-101 and FC-102 pinned"


def _ack(C):
    """Acknowledge through the endpoint, as the dashboard does."""
    with C.app.test_request_context():
        return C.api_fault_clear()


def test_clearing_a_delivered_fault_sends_the_all_clear():
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    assert len(sent) == 1, sent
    _ack(C)
    assert len(sent) == 2, "no all-clear: %r" % [s for s, _ in sent]
    subject, body = sent[1]
    assert subject.startswith(C.FC_HARD_CLEARED), subject
    assert C.FC_UNDER_POWER in body, body
    assert "Fault duration" in body, body
    return "FC-101 names the fault it closed"


def test_no_all_clear_for_a_fault_nobody_was_told_about():
    """The same rule FC-100 follows.

    A closure notice for an episode with no opening notice reads as a
    message that went missing.
    """
    C = harness.load()
    attempts = E.wire_failing(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    assert len(attempts) == 1, attempts
    _ack(C)
    subjects = [s for s, _ in attempts]
    assert not any(s.startswith(C.FC_HARD_CLEARED) for s in subjects), \
        "stood down from a warning nobody received: %r" % subjects
    return "undelivered warning, no all-clear"


def test_clearing_with_no_fault_standing_says_nothing():
    C = harness.load()
    sent = E.wire(C)
    _ack(C)
    assert sent == [], "sent an all-clear for nothing: %r" % sent
    return "silent when there was no fault"


def test_the_cleared_log_line_carries_the_code():
    C = harness.load()
    E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    _ack(C)
    msgs = [e["msg"] for e in C.load_events()]
    assert any(m.startswith(C.FC_HARD_CLEARED) for m in msgs), msgs[:5]
    return "FC-101 in the activity log"


def test_a_persisted_fault_reports_itself_saved():
    """FC-102 is the all-clear for FC-005, and only for that."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    assert any(s.startswith(C.FC_FAULT_UNSAVED) for s, _ in sent), sent
    with C._state_lock:
        C.STATE["fault_unsaved"] = False
    C._unsaved_edge()
    subjects = [s for s, _ in sent]
    assert any(s.startswith(C.FC_FAULT_PERSISTED) for s in subjects), \
        "no FC-102 after the retry succeeded: %r" % subjects
    return "FC-102 on the falling edge"


def test_acknowledging_does_not_also_report_the_fault_persisted():
    """One event, one message.

    Acknowledging clears fault_unsaved as a side effect. Reporting that
    as "saved after all" would describe a fault that no longer exists,
    alongside the FC-101 that already said so.
    """
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    _ack(C)
    C._unsaved_edge()
    subjects = [s for s, _ in sent]
    assert not any(s.startswith(C.FC_FAULT_PERSISTED) for s in subjects), \
        "reported persistence of an acknowledged fault: %r" % subjects
    return "no FC-102 when the fault went away"


def test_a_duration_reads_as_a_span_not_a_float():
    C = harness.load()
    assert C._duration(0) == "0 seconds"
    assert C._duration(45) == "45 seconds"
    assert C._duration(1260) == "21 minutes"
    assert C._duration(7200) == "2.0 hours"
    assert C._duration(-5) == "0 seconds"
    return "seconds, minutes, hours"


# ------------------------------------------------ the message reads right

def test_nothing_drawing_and_drawing_weakly_get_different_causes():
    """FC-003 covers both, and they look nothing alike at the fan.

    The prose already distinguished them. A causes list that says "fan
    failed to start" under a sentence saying it is running weakly sends
    someone looking for the wrong thing.
    """
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 0.0, lo=15.0, hi=22.0)
    _, dead = sent[0]

    C2 = harness.load()
    sent2 = E.wire(C2)
    E.raise_draw_fault(C2, 8.2, lo=15.0, hi=22.0)
    _, weak = sent2[0]

    assert "failed to start" in dead, dead
    assert "failed to start" not in weak, weak
    assert "Obstruction slowing" in weak, weak
    assert "Obstruction slowing" not in dead, dead
    return "nothing drawing and drawing weakly differ"


def test_the_causes_agree_with_the_sentence_above_them():
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    _, body = sent[0]
    assert "running weakly or stalling" in body, body
    assert "unplugged" not in body, "cause contradicts the prose: %r" % body
    return "prose and causes agree"


def test_the_unsaved_alert_names_the_fault_it_could_not_save():
    """The most alarming message in the set was the least specific."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    subject, body = sent[-1]
    assert subject.startswith(C.FC_FAULT_UNSAVED), subject
    assert "Standing fault:" in body, body
    assert C.FC_UNDER_POWER in body, body
    return "FC-005 names the standing fault"


def test_the_restart_notice_does_not_say_the_code_twice():
    C = harness.load()
    sent = E.wire(C)
    f = E.raise_draw_fault(C, 27.6, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault"]["delivered_at"] = C.time.time() - 7200
    C._notify_restart_fault()
    _, body = sent[-1]
    assert body.count(C.FC_OVER_POWER) == 1, \
        "code appears %d times: %r" % (body.count(C.FC_OVER_POWER), body)
    assert "Standing fault:" in body, body
    return "code once, in its own field"


def test_stripping_the_code_leaves_the_message_intact():
    C = harness.load()
    assert C._without_code("FC-003 Draw fault - 8.2 W") == \
        "Draw fault - 8.2 W"
    # A message without a code, from a fault.json written by an older
    # version, passes through unchanged rather than losing its first word.
    assert C._without_code("Draw fault - 8.2 W") == "Draw fault - 8.2 W"
    assert C._without_code("") == "A fault is standing."
    assert C._without_code(None) == "A fault is standing."
    return "prefix stripped, older messages intact"


def test_one_minute_is_not_one_minutes():
    C = harness.load()
    assert C._plural(1, "minute") == "1 minute"
    assert C._plural(0, "minute") == "0 minutes"
    assert C._plural(30, "minute") == "30 minutes"
    return "singular and plural"


def test_the_stale_alert_reads_as_english():
    C = harness.load()
    sent = E.wire(C)
    C.CFG["control"].update(mode="auto", stale_minutes=1)
    C._notify_stale(1, False)
    _, body = sent[0]
    assert "1 minute," in body, body
    assert "1 minutes" not in body, body
    return "no 1 minutes"


def test_fc102_does_not_say_no_action_while_a_fault_stands():
    """The persistence needs nothing. The standing fault does.

    A field that is true about the event and misleading about the
    situation is the same failure as FC-002 claiming a confirmed stop.
    """
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    with C._state_lock:
        C.STATE["fault_unsaved"] = False
    C._unsaved_edge()
    subject, body = sent[-1]
    assert subject.startswith(C.FC_FAULT_PERSISTED), subject
    line = [ln for ln in body.split("\n") if ln.startswith("Manual action")]
    assert line, body
    assert "Yes" in line[0], line[0]
    assert "still stands" in line[0], line[0]
    return "FC-102 agrees with its own body"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
