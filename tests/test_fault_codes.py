"""Fault codes: the identifiers, and that they are actually attached.

Codes exist so that an episode can be found later across events.json,
the dashboard and the mail archive with one grep. That only works if
the code appears in all three, and if the numbers never move.

WHY THIS FILE EXISTS SEPARATELY from test_alert_delivery.py: those tests
pin the EDGES - when a send happens, and what it does to fault state -
and they pass regardless of what the message says. Every template in the
feature was rewritten on 2026-08-16 and that whole suite passed
unchanged. That is the gap this file closes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
# Imported for its fixtures, not its assertions. The delivery module
# owns the SMTP stub and the fault-state helpers these tests drive.
import test_alert_delivery as E


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
    "FC_OFF_DRAW": "FC-007",
    "FC_SENSOR_RETURNED": "FC-100",
    "FC_EXTERNAL_CHANGE": "FC-300",
    "FC_COLD_START": "FC-301",
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
              "FC_OVER_POWER", "FC_FAULT_UNSAVED", "FC_RESTART_FAULT", "FC_OFF_DRAW"]
    for name in alarms:
        n = int(getattr(C, name).split("-")[1])
        assert n < 100, "%s is not in the alarm series" % name
    n = int(C.FC_SENSOR_RETURNED.split("-")[1])
    assert n >= 100, "the all-clear is not in the 1xx series"
    return "0xx alarms, 1xx clears"


def test_a_cold_start_is_informational_not_a_failure():
    """The series is the mail rule. 0xx and 2xx say something went
    wrong; a cold start did not.

    Getting this wrong is not cosmetic. The board is powered over
    Ethernet, so a switch reboot or a PoE renegotiation produces one of
    these - and in the 0xx series that would be an alert per switch
    event, which is how an address gets muted before the next real
    fault.
    """
    C = harness.load()
    n = int(C.FC_COLD_START.split("-")[1])
    assert 300 <= n < 400, \
        "FC_COLD_START is %s, which is not the informational series" \
        % C.FC_COLD_START
    return "cold start is 3xx"


# ------------------------------------------------- draw fault classification

def test_draw_fault_classification_and_causes():
    """Each reading carries the right code and actionable explanation."""
    C = harness.load()
    cases = (
        (None, 15.0, 22.0, C.FC_PLUG_COMMS, "plug"),
        (0.0, 15.0, 22.0, C.FC_UNDER_POWER, "did not start"),
        (8.2, 15.0, 22.0, C.FC_UNDER_POWER, "running weakly"),
        (27.6, 15.0, 22.0, C.FC_OVER_POWER, "more power"),
    )
    for reading, lo, hi, code, text in cases:
        assert C._draw_fault_code(reading, lo, hi) == code, reading
        cause = C._draw_fault_cause(reading, lo, hi)
        assert text in cause.lower(), (reading, cause)
    # A dashboard change to the band must change classification.
    assert C._draw_fault_code(27.6, 15.0, 30.0) == C.FC_UNDER_POWER
    return "draw classification and causes"
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
    with C.app.test_request_context("/api/fault/clear", method="POST", json={}):
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


# --------------------------------------------- the message tells the truth
# Both of these reproduce findings from an external review of 3b30c6c.

class RefusingPlug(object):
    """Reports the fan on, and will not switch it off."""

    def state(self):
        return True

    def set(self, on):
        raise Exception("plug refused")

    def power_w(self):
        return 8.2


def _fault_with(C, plug, lo=15.0, hi=22.0, watts=8.2):
    C.CFG["plug"].update(watts_enabled=True, watts_min=lo, watts_max=hi,
                         watts_fault_seconds=0)
    now = C.time.time()
    with C._state_lock:
        C.STATE.update(fan=True, watts=watts, watts_at=now,
                       changed_at=now - 10000, fault=None)
    C._draw_check(plug)
    C._draw_check(plug)


def test_an_unconfirmed_stop_is_not_reported_as_a_stop():
    """The email used to be queued BEFORE the stop was attempted.

    Every alert therefore claimed "Plug switched off" even when the
    relay then refused, and the subject said the fan was stopped while
    it was still running.
    """
    C = harness.load()
    sent = E.wire(C)
    C._enforce_stop = lambda plug, gen=None: False
    _fault_with(C, RefusingPlug())
    subject, body = sent[0]
    assert "Fan stopped" not in subject, subject
    assert "fault latched" in subject.lower(), subject
    assert "NOT confirmed" in body, body
    assert "Plug switched off" not in body, body
    return "unconfirmed reads as unconfirmed"


def test_a_confirmed_stop_still_says_so():
    C = harness.load()
    sent = E.wire(C)
    class Obedient(object):
        on = True
        def state(self):
            return self.on

        def set(self, on):
            self.on = on
            return None

        def power_w(self):
            return 8.2 if self.on else 0

    _fault_with(C, Obedient())
    subject, body = sent[0]
    assert "Fan stopped" in subject, subject
    assert "Plug switched off, fault latched" in body, body
    return "confirmed reads as confirmed"


def test_a_plug_that_explodes_does_not_suppress_the_alert():
    """The old order had one virtue: nothing between the fault and the
    mail. Enforcing first must not let a broken plug buy silence."""
    C = harness.load()
    sent = E.wire(C)

    def boom(plug, gen=None):
        raise RuntimeError("plug layer exploded")

    C._enforce_stop = boom
    _fault_with(C, RefusingPlug())
    assert len(sent) == 1, "the alert was suppressed: %r" % sent
    assert "NOT confirmed" in sent[0][1], sent[0][1]
    return "alert survives a broken plug"


def test_a_late_callback_cannot_vouch_for_the_next_fault():
    """One process-global flag let fault A authorise fault B's all-clear.

    A's send completes after A is acknowledged; B's send then fails; B's
    falling edge reported "saved after all" for a warning nobody had
    received.
    """
    C = harness.load()
    sent = E.wire(C)

    # Episode A: raised, unsaved, and its FC-005 delivered.
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    assert C._unsaved_delivered is not None, "A never recorded delivery"

    # A is acknowledged and a NEW fault is raised.
    with C.app.test_request_context("/api/fault/clear", method="POST", json={}):
        C.api_fault_clear()
    E.raise_draw_fault(C, 27.6, lo=15.0, hi=22.0)

    # B goes unsaved, and its own send fails.
    C._notify = lambda subject, body, on_complete=None: (
        on_complete(False) if on_complete else None)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()

    # B is written after all. Nobody was told about B.
    sent[:] = []
    C._notify = lambda subject, body, on_complete=None: (
        sent.append((subject, body)),
        on_complete(True) if on_complete else None)
    with C._state_lock:
        C.STATE["fault_unsaved"] = False
    C._unsaved_edge()

    subjects = [s for s, _ in sent]
    assert not any(s.startswith(C.FC_FAULT_PERSISTED) for s in subjects), \
        "A's delivery vouched for B: %r" % subjects
    return "delivery is bound to the episode"


def test_the_all_clear_still_sends_for_its_own_episode():
    """The identity check must not silence the legitimate case."""
    C = harness.load()
    sent = E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C._state_lock:
        C.STATE["fault_unsaved"] = True
    C._unsaved_edge()
    with C._state_lock:
        C.STATE["fault_unsaved"] = False
    C._unsaved_edge()
    subjects = [s for s, _ in sent]
    assert any(s.startswith(C.FC_FAULT_PERSISTED) for s in subjects), \
        "the real all-clear went missing: %r" % subjects
    return "same episode still stands down"


# ------------------------------------------------- the log side of the deal
# Docs/troubleshooting.md tells people to grep events.json for a code and
# find the episode. That promise is only as good as the log lines.

def test_the_whole_draw_episode_carries_one_code():
    """Raise, stop confirmation and clear must all come back together."""
    C = harness.load()
    E.wire(C)
    E.raise_draw_fault(C, 8.2, lo=15.0, hi=22.0)
    with C.app.test_request_context("/api/fault/clear", method="POST", json={}):
        C.api_fault_clear()
    hits = [e["msg"] for e in C.load_events()
            if C.FC_UNDER_POWER in e["msg"]]
    assert len(hits) >= 2, "episode is not searchable: %r" % [
        e["msg"] for e in C.load_events()]
    assert any("cleared" in h.lower() for h in hits), hits
    return "%d lines under one code" % len(hits)


def test_the_failsafe_and_its_all_clear_are_both_logged():
    """The recovery was mailed and never written down: events.json
    showed a fail-safe starting and nothing ending it."""
    C = harness.load()
    E.wire(C)
    C._log("%s FAIL-SAFE: no reading for 30 min, ventilating"
           % C.FC_SENSOR_COMMS)
    C._log("%s Sensor readings have returned, normal control resumed"
           % C.FC_SENSOR_RETURNED)
    msgs = [e["msg"] for e in C.load_events()]
    assert any(m.startswith(C.FC_SENSOR_COMMS) for m in msgs), msgs
    assert any(m.startswith(C.FC_SENSOR_RETURNED) for m in msgs), msgs
    return "alarm and all-clear both in the log"


def test_the_log_holds_a_thousand_entries():
    """An episode is several lines now. A depth that evicts half of it
    while the codes promise a searchable history is worse than no
    promise."""
    C = harness.load()
    assert C.EVENTS_MAX == 1000, C.EVENTS_MAX
    for i in range(1100):
        C.STATE["events"].insert(0, {"at": C.time.time(), "msg": "e%d" % i})
    del C.STATE["events"][C.EVENTS_MAX:]
    assert len(C.STATE["events"]) == 1000, len(C.STATE["events"])
    return "1000 kept"


# ----------------------------------------------- folding the duplicate pair

def test_a_mode_change_and_its_actuation_are_one_line():
    C = harness.load()
    C._log("Mode: Force on")
    C._log("Fan on \u2014 forced on",
           absorb="Mode: Force on",
           merged="Mode: Force on \u2014 fan on")
    msgs = [e["msg"] for e in C.load_events()]
    assert len(msgs) == 1, msgs
    assert msgs[0] == "Mode: Force on \u2014 fan on", msgs
    return "one line, not two"


def test_the_folded_line_keeps_the_original_time():
    """The mode changed when the person clicked, not when the plug
    got round to it."""
    C = harness.load()
    C._log("Mode: Force on")
    first = C.load_events()[0]["at"]
    C.time.sleep(1.1)
    C._log("Fan on", absorb="Mode: Force on", merged="Mode: Force on - fan on")
    assert abs(C.load_events()[0]["at"] - first) < 0.01, "timestamp moved"
    return "timestamp is the click"


def test_anything_in_between_stops_the_fold():
    """If something happened between them they are not the same event,
    and collapsing would hide the thing in the middle."""
    C = harness.load()
    C._log("Mode: Force on")
    C._log("Fan off \u2014 changed outside")
    C._log("Fan on", absorb="Mode: Force on", merged="folded")
    msgs = [e["msg"] for e in C.load_events()]
    assert len(msgs) == 3, msgs
    assert "folded" not in msgs, msgs
    return "an interruption keeps both lines"


def test_a_stale_line_is_not_absorbed():
    C = harness.load()
    C.STATE["events"].insert(0, {"at": C.time.time() - C.ABSORB_SECONDS - 60,
                                 "msg": "Mode: Force on"})
    C._log("Fan on", absorb="Mode: Force on", merged="folded")
    msgs = [e["msg"] for e in C.load_events()]
    assert len(msgs) == 2, msgs
    return "old lines are left alone"


def test_the_action_still_logs_when_there_was_no_mode_change():
    """Force on re-asserted after an external switch has no mode line
    above it, and must still record that it acted."""
    C = harness.load()
    C._log("Fan off \u2014 changed outside")
    C._log("Fan on \u2014 forced on",
           absorb="Mode: Force on",
           merged="Mode: Force on \u2014 fan on")
    msgs = [e["msg"] for e in C.load_events()]
    assert len(msgs) == 2, msgs
    assert msgs[0] == "Fan on \u2014 forced on", msgs
    return "standalone text when it stands alone"


def test_an_external_change_carries_fc300():
    """The one line where "how often did this happen" is a real
    question - it is also what a second controller looks like."""
    C = harness.load()
    assert C.FC_EXTERNAL_CHANGE == "FC-300", C.FC_EXTERNAL_CHANGE
    C._log("%s Fan off \u2014 changed outside" % C.FC_EXTERNAL_CHANGE)
    msgs = [e["msg"] for e in C.load_events()]
    assert msgs[0].startswith("FC-300"), msgs
    return "external changes are countable"


def test_normal_operation_stays_uncoded():
    """Exercise both Manual stop messages; neither is a fault."""
    C = harness.load()
    C.CFG["control"]["mode"] = "manual"
    with C._state_lock:
        C.STATE["manual_off_pending"] = True
    C.promise_manual_off()
    C._manual_off_locked(_Plug(True))
    lines = [e["msg"] for e in C.load_events()]
    assert any("Fan off" in line and "manual" in line for line in lines), lines
    assert not any(line.startswith("FC-") for line in lines), lines

    C2 = harness.load()
    C2._announce_owed_manual_stop()
    lines = [e["msg"] for e in C2.load_events()]
    assert any("manual stop owed from before restart" in line
               for line in lines), lines
    assert not any(line.startswith("FC-") for line in lines), lines
    return "manual paths carry no code"
class _Plug(object):
    """A plug that reports a state the controller did not command."""

    def __init__(self, on):
        self._on = on

    def state(self):
        return self._on

    def set(self, on):
        self._on = on
        return None

    def power_w(self):
        return None


class _Protect(object):
    def readings(self, sid):
        return {"co2": 700.0, "temp": 21.0, "humidity": 50.0}, "fake"


def _external_switch(C, mode, plug_on):
    """Make the plug disagree with STATE and run a real poll."""
    C.CFG["protect"]["sensor_id"] = "fake-sensor-id"
    C.CFG["control"].update(mode=mode, stale_minutes=0, min_run_minutes=0)
    with C._state_lock:
        C.STATE.update(fan=not plug_on, fault=None,
                       changed_at=C.time.time() - 10000)
    C._client_snapshot = lambda: (_Protect(), _Plug(plug_on),
                                  C._cfg_generation)
    C.tick()
    return [e["msg"] for e in C.load_events()]


def test_an_external_change_records_the_mode():
    """DRIVEN THROUGH tick(), not by handing _log a string.

    The first version of this test built the expected message itself
    and logged it, so it would have passed with the mode removed from
    both production paths - which was the only thing it existed to
    check. Reported by review of ec8be8f.
    """
    C = harness.load()
    E.wire(C)
    msgs = _external_switch(C, "manual", True)
    hits = [m for m in msgs if m.startswith(C.FC_EXTERNAL_CHANGE)]
    assert hits, "no external change logged: %r" % msgs[:4]
    assert hits[0].endswith("(Manual)"), hits[0]
    assert "Fan on" in hits[0], hits[0]
    return "the poll records the mode"


def test_the_mode_recorded_is_the_mode_it_was_in():
    """Auto and Force on must not be reported as Manual - the whole
    point is that the same event means different things."""
    C = harness.load()
    E.wire(C)
    msgs = _external_switch(C, "auto", True)
    hits = [m for m in msgs if m.startswith(C.FC_EXTERNAL_CHANGE)]
    assert hits and hits[0].endswith("(Auto)"), hits[:2]

    C2 = harness.load()
    E.wire(C2)
    C2.CFG["protect"]["sensor_id"] = "fake-sensor-id"
    C2.CFG["control"].update(mode="on", stale_minutes=0)
    with C2._state_lock:
        C2.STATE.update(fan=True, fault=None)
    # Force on notices an external switch-off through _force_on.
    C2._force_on(_Plug(False))
    hits2 = [e["msg"] for e in C2.load_events()
             if e["msg"].startswith(C2.FC_EXTERNAL_CHANGE)]
    assert hits2, "Force on logged no external change"
    assert hits2[0].endswith("(Force on)"), hits2[0]
    return "Manual, Auto and Force on each say so"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
