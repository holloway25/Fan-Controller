"""Email alerts: what sends, what does not, and what must never happen.

No mail leaves this file. _send_mail is replaced with a recorder in
every test, so the assertions are about WHEN a send is attempted and
what it says - never about reaching a relay.

The rule the whole feature rests on: an alert failing must be invisible
to the fan. Several tests here assert that a broken mail path leaves
fault handling completely intact.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def wire(C, **cfg):
    """Configure email and capture sends instead of making them.

    Returns the list that receives (subject, body) tuples. _notify
    normally hands off to a thread; here it runs inline so tests do not
    race a background send.
    """
    C.CFG["email"].update(enabled=True, host="relay.invalid", port=25,
                          sender="fan@invalid",
                          recipients="someone@invalid")
    C.CFG["email"].update(**cfg)
    sent = []

    def fake_notify(subject, body, on_complete=None):
        if not C._email_ready():
            if on_complete:
                on_complete(False)
            return
        sent.append((subject, body))
        # Delivery is simulated by default. Tests that care about a
        # FAILED send use wire_failing() instead - the distinction
        # matters, because recording delivery optimistically is what
        # produced an all-clear for a warning nobody received.
        if on_complete:
            on_complete(True)

    C._notify = fake_notify
    return sent


def wire_failing(C, **cfg):
    """As wire(), but the relay never accepts the message."""
    C.CFG["email"].update(enabled=True, host="relay.invalid", port=25,
                          sender="fan@invalid",
                          recipients="someone@invalid")
    C.CFG["email"].update(**cfg)
    attempts = []

    def fake_notify(subject, body, on_complete=None):
        if C._email_ready():
            attempts.append((subject, body))
        # Completed, but NOT delivered.
        if on_complete:
            on_complete(False)

    C._notify = fake_notify
    return attempts


def raise_draw_fault(C, watts, lo=15.0, hi=22.0):
    """Drive the draw checker to a fault with a chosen reading."""
    C.CFG["plug"].update(watts_enabled=True, watts_min=lo, watts_max=hi,
                         watts_fault_seconds=0)

    class Plug:
        def state(self):
            return True

        def set(self, on):
            return None

        def power_w(self):
            return watts

    now = C.time.time()
    with C._state_lock:
        C.STATE.update(fan=True, watts=watts, watts_at=now,
                       changed_at=now - 10000, fault=None)
    # Twice: the first out-of-band cycle only starts the clock, and the
    # second is what raises with watts_fault_seconds at 0.
    C._draw_check(Plug())
    C._draw_check(Plug())
    return C.STATE["fault"]


# --------------------------------------------------------------- off

def test_nothing_is_sent_while_disabled():
    """enabled False must beat every other setting."""
    C = harness.load()
    sent = wire(C, enabled=False)
    raise_draw_fault(C, 0.0)
    assert sent == [], "sent while switched off: %r" % sent
    return "silent when disabled"


def test_nothing_is_sent_without_recipients():
    C = harness.load()
    sent = wire(C, recipients="")
    raise_draw_fault(C, 0.0)
    assert sent == [], "sent with nobody to send to"
    return "silent with no recipients"


# ------------------------------------------------------------- edges

def test_draw_fault_sends_once_on_the_transition():
    """The alert hangs off the edge, not off the fault standing.

    _enforce_stop runs every draw cycle while a fault stands and the
    retry runs every 5s. Sending from the state rather than the
    transition would mail every few seconds for as long as the fault
    lasted.
    """
    C = harness.load()
    sent = wire(C)
    raise_draw_fault(C, 0.0)
    assert len(sent) == 1, "expected one send, got %d" % len(sent)

    # Further cycles with the fault still standing must stay quiet.
    plug = type("P", (), {"state": lambda s: True,
                          "set": lambda s, o: None,
                          "power_w": lambda s: 0.0})()
    C._draw_check(plug)
    C._draw_check(plug)
    assert len(sent) == 1, "sent again while the fault still stood"
    return "one send per fault"


def test_unsaved_fault_sends_once_and_again_after_clearing():
    """Each write failure is a fresh window where a restart loses it."""
    C = harness.load()
    sent = wire(C)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"msg": "draw fault", "at": 1})
    finally:
        os.replace = real
    subjects = [s for s, _ in sent]
    assert any("could not be saved" in s for s in subjects), \
        "no unsaved alert: %r" % subjects

    before = len(sent)
    C._unsaved_edge()
    assert len(sent) == before, "unsaved alert repeated while still true"

    # Clearing resets the edge, so a later failure alerts again.
    assert C._retry_fault_persist() is True
    os.replace = boom
    try:
        C._store_fault({"msg": "another", "at": 2})
    finally:
        os.replace = real
    assert len(sent) > before, "second write failure stayed silent"
    return "once per window, not once ever"


def test_restart_notice_is_rate_limited():
    """A crash loop must not mail every ten seconds."""
    C = harness.load()
    sent = wire(C)
    now = C.time.time()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "at": now,
                            "attempted_at": now, "delivered_at": now}
    C._notify_restart_fault()
    assert sent == [], "notified inside the suppression window"

    with C._state_lock:
        C.STATE["fault"]["delivered_at"] = \
            now - C.RESTART_NOTICE_SUPPRESS - 1
    C._notify_restart_fault()
    assert len(sent) == 1, "no notice once the window had passed"
    assert "not" in sent[0][1].lower() and "clear" in sent[0][1].lower(), \
        "the notice does not say a restart will not clear it"
    return "one an hour at most"


def test_a_failed_restart_notice_does_not_buy_an_hour_of_silence():
    """Rate limiting on the ATTEMPT meant broken mail silenced itself.

    A fault outliving a restart could then go unreported precisely
    because the relay was down - the moment the notice matters most.
    """
    C = harness.load()
    attempts = wire_failing(C)
    now = C.time.time()
    with C._state_lock:
        C.STATE["fault"] = {"msg": "draw fault", "at": now,
                            "attempted_at": None, "delivered_at": None}
    C._notify_restart_fault()
    assert len(attempts) == 1, "did not try"
    C._notify_restart_fault()
    assert len(attempts) == 2, \
        "a failed send suppressed the next attempt"
    return "retries while undelivered"


def test_restart_notice_stays_silent_with_no_fault():
    C = harness.load()
    sent = wire(C)
    with C._state_lock:
        C.STATE["fault"] = None
    C._notify_restart_fault()
    assert sent == [], "notified with nothing wrong"
    return "quiet on a clean start"


# ------------------------------------------------------------- cause

def test_the_cause_text_distinguishes_the_four_cases():
    """Someone reading this at 2am needs to know where to go.

    "not drawing" and "plug not answering" send a person to different
    places with different tools, so they must not read alike.
    """
    C = harness.load()
    no_read = C._draw_fault_cause(None, 20.0, 27.0).lower()
    dead = C._draw_fault_cause(0.0, 20.0, 27.0).lower()
    weak = C._draw_fault_cause(12.0, 20.0, 27.0).lower()
    high = C._draw_fault_cause(40.0, 20.0, 27.0).lower()

    assert "plug" in no_read and "network" in no_read
    assert "not start" in dead or "did not start" in dead
    assert "less" in weak or "weakly" in weak
    assert "band" in high, "the stale-band false positive is not mentioned"
    assert len({no_read, dead, weak, high}) == 4, "cases read alike"
    return "four distinct explanations"


def test_high_draw_names_its_own_false_positive():
    """A fan swap against an old band reads as a broken fan and is not.

    This happened here: a 23W fan against a 22W ceiling. An alert that
    does not name its most likely false positive gets mistrusted, and a
    mistrusted alert gets muted.
    """
    C = harness.load()
    text = C._draw_fault_cause(40.0, 20.0, 27.0).lower()
    assert "band" in text and ("changed" in text or "recently" in text), \
        "high-draw text does not mention a stale band: %r" % text
    return "names the stale band"


# ------------------------------------------------- must not break the fan

def test_a_failing_send_never_reaches_the_fault_path():
    """The fan being stopped matters more than the mail going out."""
    C = harness.load()
    C.CFG["email"].update(enabled=True, host="relay.invalid",
                          sender="fan@invalid", recipients="x@invalid")

    def explode(*a, **k):
        raise RuntimeError("mail subsystem on fire")

    C._send_mail = explode
    fault = raise_draw_fault(C, 0.0)
    assert fault is not None, "the fault was lost when mail failed"
    assert C.STATE["fault"] is not None
    return "fault survives a broken mailer"


def test_notify_returns_immediately():
    """_notify runs on the draw thread with a fault just written."""
    C = harness.load()
    C.CFG["email"].update(enabled=True, host="127.0.0.1", port=9,
                          sender="fan@invalid", recipients="x@invalid")
    t0 = C.time.time()
    C._notify("subject", "body")
    lag = C.time.time() - t0
    assert lag < 0.5, "blocked the caller for %.2fs" % lag
    return "returns in %.3fs" % lag


# ------------------------------------------------------------ parsing

def test_recipients_split_on_commas_and_spaces():
    C = harness.load()
    C.CFG["email"]["recipients"] = "a@x , b@y  c@z,,"
    assert C._recipients() == ["a@x", "b@y", "c@z"], C._recipients()
    C.CFG["email"]["recipients"] = ""
    assert C._recipients() == []
    return "splits and discards blanks"


def test_ready_needs_every_essential_field():
    C = harness.load()
    C.CFG["email"].update(enabled=True, host="", sender="s@x",
                          recipients="r@x")
    assert C._email_ready() is False, "ready with no host"
    C.CFG["email"]["host"] = "relay"
    C.CFG["email"]["sender"] = ""
    assert C._email_ready() is False, "ready with no sender"
    C.CFG["email"]["sender"] = "s@x"
    assert C._email_ready() is True
    return "all four required"


# ------------------------------------------------------------- config

def test_password_is_redacted_and_cleared_with_the_host():
    """The SMTP password is the third secret and follows the same rules."""
    C = harness.load()
    C.CFG["email"].update(host="relay.one",
                         password="placeholder-not-a-real-password")
    app = C.app.test_client()
    body = app.get("/api/state").get_json()
    assert body["config"]["email"]["password"] == "SET", "password echoed"

    r = app.post("/api/config", json={"email": {"host": "relay.two"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert C.CFG["email"]["password"] == "", \
        "password survived a mail server change"
    return "redacted, and cleared on host change"


# ------------------------------------------------------- authentication
#
# The review found that smtplib.login() iterates CRAM-MD5, PLAIN, LOGIN
# and CONTINUES past an SMTPAuthenticationError - so a failed CRAM-MD5
# silently retried as PLAIN, in clear, on a link with no STARTTLS. These
# tests exist so that cannot come back.


class FakeSMTP(object):
    """Enough of smtplib.SMTP to record what a send actually does."""

    def __init__(self, auth="CRAM-MD5 PLAIN LOGIN", starttls=False, refused=None):
        self.esmtp_features = {"auth": auth} if auth else {}
        self._starttls = starttls
        self.calls = []
        self.sent = None
        self.refused = refused or {}
        self.user = None
        self.password = None
        # Real code passes s.auth_cram_md5 to s.auth(), so the fake needs
        # it to exist. Its return value is never inspected here - what is
        # under test is WHICH mechanism gets chosen, not the HMAC.
        self.auth_cram_md5 = lambda *a, **k: "challenge-response"

    def has_extn(self, name):
        return name.lower() == "starttls" and self._starttls

    def starttls(self, context=None):
        # The context is the point: without one, smtplib builds an
        # unverified context and any relay can impersonate the real one.
        assert context is not None, "starttls called without a context"
        assert context.check_hostname, "context does not check the hostname"
        assert context.verify_mode == __import__("ssl").CERT_REQUIRED, \
            "context does not verify the certificate"
        self.calls.append("starttls")

    def ehlo(self, *a):
        self.calls.append("ehlo")
        return 250, b"ok"

    def auth(self, mechanism, authobject, **kw):
        self.calls.append("auth:" + mechanism)
        return 235, b"ok"

    def login(self, user, password):
        self.calls.append("login")
        return 235, b"ok"

    def sendmail(self, sender, to, msg):
        self.sent = (sender, to, msg)
        return self.refused

    def quit(self):
        self.calls.append("quit")


def with_fake(C, fake, **cfg):
    C.CFG["email"].update(enabled=True, host="relay.invalid", port=25,
                          username="user", password="pw",
                          sender="fan@invalid", recipients="a@invalid")
    C.CFG["email"].update(**cfg)
    C.smtplib.SMTP = lambda *a, **k: fake
    return fake


def test_partial_recipient_rejection_is_not_complete_success():
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP(refused={"b@example.net": (550, b"private reply")}),
                     recipients="a@example.com, b@example.net")
    try:
        why = C._send_mail("s", "b")
    finally:
        C.smtplib.SMTP = real
    assert why and "partial" in why.lower(), why
    assert "1 of 2" in why and "1 rejected" in why, why
    assert "private reply" not in why and "b@example.net" not in why, why
    assert "quit" in fake.calls, fake.calls
    return "partial acceptance reported without relay response or addresses"


def test_email_test_endpoint_reports_partial_recipient_rejection():
    C = harness.load()
    real = C.smtplib.SMTP
    with_fake(C, FakeSMTP(refused={"b@example.net": (550, b"rejected")}),
              recipients="a@example.com, b@example.net")
    try:
        response = C.app.test_client().post("/api/email/test", json={})
    finally:
        C.smtplib.SMTP = real
    result = response.get_json()
    assert response.status_code == 400, (response.status_code, result)
    assert "partial" in result["error"].lower(), result
    assert "sent_to" not in result and not result.get("ok"), result
    return "test endpoint does not claim all recipients received mail"


def test_partial_delivery_does_not_mark_alert_fully_delivered():
    import threading
    C = harness.load()
    real = C.smtplib.SMTP
    with_fake(C, FakeSMTP(refused={"b@example.net": (550, b"rejected")}),
              recipients="a@example.com, b@example.net")
    done = threading.Event()
    calls = []

    def complete(ok):
        calls.append(ok)
        done.set()

    try:
        C._notify("alarm", "body", on_complete=complete)
        assert done.wait(5), "notification worker did not finish"
    finally:
        C.smtplib.SMTP = real
    assert calls == [False], calls
    assert any("FC-203" in e["msg"] and "Partial" in e["msg"]
               for e in C.STATE["events"]), C.STATE["events"]
    return "partial delivery is a failed completion, not an all-clear prerequisite"


def test_email_test_recipient_report_uses_send_snapshot():
    C = harness.load()
    real = C.smtplib.SMTP
    fake = FakeSMTP()
    send = fake.sendmail

    def change_during_send(sender, to, msg):
        with C._cfg_lock:
            C.CFG["email"]["recipients"] = "new@example.net"
        return send(sender, to, msg)

    fake.sendmail = change_during_send
    with_fake(C, fake, recipients="old@example.com")
    try:
        response = C.app.test_client().post("/api/email/test", json={})
    finally:
        C.smtplib.SMTP = real
    assert response.status_code == 200, response.get_json()
    assert fake.sent[1] == ["old@example.com"]
    assert response.get_json()["sent_to"] == fake.sent[1], response.get_json()
    return "reported recipients are the ones passed to SMTP, not the later config"


def test_cram_md5_is_used_and_login_is_never_called():
    """login() would fall back to PLAIN. It must not be used."""
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP())
    try:
        why = C._send_mail("s", "b")
    finally:
        C.smtplib.SMTP = real
    assert why is None, why
    assert "auth:CRAM-MD5" in fake.calls, fake.calls
    assert "login" not in fake.calls, \
        "used login(), which falls back to PLAIN in the clear"
    return "CRAM-MD5 chosen explicitly"


def test_refuses_when_only_clear_mechanisms_are_offered():
    """No STARTTLS and no CRAM-MD5 means refuse, not degrade.

    A refused send is a logged failure. A downgraded one is a leaked
    password that looks like success.
    """
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP(auth="PLAIN LOGIN"))
    try:
        why = C._send_mail("s", "b")
    finally:
        C.smtplib.SMTP = real
    assert why and "refusing" in why.lower(), why
    assert fake.sent is None, "sent anyway after refusing to authenticate"
    assert not any(c.startswith("auth") for c in fake.calls), fake.calls
    return "refuses rather than sending the password in clear"


def test_starttls_is_used_when_offered():
    """A relay that offers encryption gets it, and login is then safe."""
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP(auth="PLAIN LOGIN", starttls=True))
    try:
        why = C._send_mail("s", "b")
    finally:
        C.smtplib.SMTP = real
    assert why is None, why
    assert "starttls" in fake.calls, fake.calls
    assert "login" in fake.calls, "did not authenticate over TLS"
    return "encrypts when it can"


def test_no_username_means_no_authentication():
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP(auth="PLAIN"), username="")
    try:
        why = C._send_mail("s", "b")
    finally:
        C.smtplib.SMTP = real
    assert why is None, why
    assert not any(c.startswith("auth") or c == "login" for c in fake.calls)
    assert fake.sent is not None, "did not send"
    return "open relay needs no credentials"


# -------------------------------------------------- message construction

def test_the_message_has_the_headers_a_mail_server_needs():
    C = harness.load()
    real = C.smtplib.SMTP
    fake = with_fake(C, FakeSMTP(), recipients="a@example.com, b@example.net")
    try:
        C._send_mail("Fan stopped", "the body")
    finally:
        C.smtplib.SMTP = real
    sender, to, msg = fake.sent
    assert sender == "fan@invalid", sender
    assert to == ["a@example.com", "b@example.net"], to
    assert "Subject: Fan stopped" in msg, msg[:200]
    assert "From: fan@invalid" in msg
    assert "To: a@example.com, b@example.net" in msg
    assert "charset=utf-8" in msg
    head, _, body = msg.partition("\r\n\r\n")
    assert body == "the body", repr(body)
    return "headers and body separated correctly"


# ---------------------------------------------------------- stale alerts

def stale_setup(C, fan_already_on):
    """Drive _failsafe with readings long stale."""
    sent = wire(C)
    C.CFG["control"].update(mode="auto", stale_minutes=1)

    class Plug:
        def state(self):
            return fan_already_on

        def set(self, on):
            return None

    C._clients = lambda: (None, Plug())
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["reading_at"] = C.time.time() - 3600
    C._failsafe()
    return sent


def test_stale_alert_fires_when_the_fan_is_already_running():
    """The review found this sent nothing at all.

    The alert sat under the branch that switches the plug, so readings
    stopping while the fan happened to be running was silent - and that
    is the COMMON case, because high CO2 starts the fan and the fan is
    then on when the sensor dies.
    """
    C = harness.load()
    sent = stale_setup(C, fan_already_on=True)
    assert len(sent) == 1, "no alert with the fan already on: %r" % sent
    assert "already running" in sent[0][1], sent[0][1]
    return "alerts even when nothing was switched"


def test_stale_alert_fires_when_the_fan_had_to_be_switched_on():
    C = harness.load()
    sent = stale_setup(C, fan_already_on=False)
    assert len(sent) == 1, "no alert when the fail-safe switched: %r" % sent
    assert "switched on" in sent[0][1], sent[0][1]
    return "alerts when it ventilates"


def test_stale_alert_is_sent_once_not_every_cycle():
    """_failsafe runs every poll while readings stay stale."""
    C = harness.load()
    sent = stale_setup(C, fan_already_on=True)
    C._failsafe()
    C._failsafe()
    assert len(sent) == 1, "sent %d times for one outage" % len(sent)
    return "one alert per outage"


def test_the_failsafe_records_the_fan_as_on_when_it_was_already_on():
    """The relay was on while /api/state said off.

    _failsafe returned early when the plug reported on, without
    updating STATE - so the dashboard showed a stopped fan, and the
    draw check, which only runs when STATE says the fan is on, stayed
    asleep over a running one.
    """
    C = harness.load()
    with C._state_lock:
        C.STATE["fan"] = False
    stale_setup(C, fan_already_on=True)
    assert C.STATE["fan"] is True, \
        "cached state still says off while the relay is on"
    return "cached state matches the relay"


def test_no_stale_alert_outside_auto_or_with_the_failsafe_off():
    """Two documented silences, asserted so they stay deliberate."""
    C = harness.load()
    sent = wire(C)
    C.CFG["control"].update(mode="manual", stale_minutes=1)
    C._clients = lambda: (None, None)
    with C._state_lock:
        C.STATE["fault"] = None
        C.STATE["reading_at"] = C.time.time() - 3600
    C._failsafe()
    assert sent == [], "alerted in Manual"

    C.CFG["control"].update(mode="auto", stale_minutes=0)
    C._failsafe()
    assert sent == [], "alerted with the fail-safe disabled"
    return "silent in Manual and at 0"


# ------------------------------------------------------- recovery alerts

def feed_a_reading(C):
    """Drive a real poll with a usable reading, as recovery requires."""

    class Sensor:
        def readings(self, sid):
            return {"co2": 700.0}, "path"

    class Plug:
        def state(self):
            return False

        def set(self, v):
            return v

        def power_w(self):
            return 0.0

    C._clients = lambda: (Sensor(), Plug())
    C.CFG["protect"]["sensor_id"] = "x"
    C.CFG["control"].update(mode="auto", co2_enabled=True,
                            on_at=1000, off_at=700)
    C.tick()


def test_a_real_reading_produces_the_all_clear():
    """The recovery alert follows an actual poll, not a flag flip.

    The previous version of this test cleared the flag by hand and
    called _notify_stale again, so it asserted nothing about recovery
    and would have passed with the recovery path deleted.
    """
    C = harness.load()
    sent = wire(C)
    C._notify_stale(30, True)
    assert len(sent) == 1, "no stale warning to recover from"

    feed_a_reading(C)
    subjects = [s for s, _ in sent]
    assert len(sent) == 2, "no all-clear after a real reading: %r" % subjects
    assert "returned" in subjects[1].lower(), subjects[1]
    return "a reading clears it"


def test_no_all_clear_when_the_warning_was_never_delivered():
    """An all-clear for a warning nobody received is worse than silence.

    This is the bug: _stale_notified was set before the send, so a
    failed warning still armed the recovery message. Someone would be
    told the sensor was fine without ever being told it had stopped.
    """
    C = harness.load()
    attempts = wire_failing(C)
    C._notify_stale(30, True)
    assert len(attempts) == 1, "did not attempt the warning"
    assert C._stale_delivered is False, "recorded delivery of a failed send"

    feed_a_reading(C)
    subjects = [s for s, _ in attempts]
    assert not any("returned" in s.lower() for s in subjects), \
        "sent an all-clear for a warning that never arrived: %r" % subjects
    return "no all-clear without a delivered warning"


def test_a_failed_warning_is_retried_on_the_next_cycle():
    C = harness.load()
    attempts = wire_failing(C)
    C._notify_stale(30, True)
    C._notify_stale(30, True)
    assert len(attempts) == 2, "gave up after one failed attempt"
    return "retries during the outage"


# ------------------------------------------------ real asynchronous timing
#
# Every test above replaces _notify with something synchronous, which
# makes "sent once" pass even when production can queue two. These use
# the REAL _notify and a _send_mail held open on an Event, so the window
# between starting a send and finishing it is a window the test controls.


class Gate(object):
    """A _send_mail that blocks until released, recording each call."""

    def __init__(self):
        self.started = __import__("threading").Event()
        self.release = __import__("threading").Event()
        self.calls = []
        self.result = None          # None means delivered

    def __call__(self, subject, body):
        self.calls.append(subject)
        self.started.set()
        self.release.wait(5)
        return self.result


def live_email(C):
    """Configure email for real _notify use, with no relay involved."""
    C.CFG["email"].update(enabled=True, host="relay.invalid", port=25,
                          username="", password="",
                          sender="fan@invalid", recipients="a@invalid")


def test_a_second_warning_cannot_queue_while_the_first_is_in_flight():
    """_notify returns as soon as the thread starts.

    Clearing the inflight flag after that call - rather than from the
    worker - let the next fail-safe cycle queue a second warning while
    the first was still talking to the relay. Every fake in this file is
    synchronous, so none of them could see it.
    """
    C = harness.load()
    live_email(C)
    gate = Gate()
    C._send_mail = gate

    C._notify_stale(30, True)
    assert gate.started.wait(5), "first send never started"
    C._notify_stale(30, True)
    C._notify_stale(30, True)
    assert len(gate.calls) == 1, \
        "queued %d sends while one was in flight" % len(gate.calls)

    gate.release.set()
    for _ in range(50):
        if C._stale_delivered:
            break
        C.time.sleep(0.05)
    assert C._stale_delivered is True, "delivery never recorded"
    assert C._stale_inflight is False, "inflight never cleared"
    return "one in flight at a time"


def test_a_failed_send_clears_inflight_so_the_next_cycle_retries():
    C = harness.load()
    live_email(C)
    gate = Gate()
    gate.result = "relay refused"
    C._send_mail = gate

    C._notify_stale(30, True)
    assert gate.started.wait(5)
    gate.release.set()
    for _ in range(50):
        if not C._stale_inflight:
            break
        C.time.sleep(0.05)
    assert C._stale_inflight is False, "stuck in flight after a failure"
    assert C._stale_delivered is False, "recorded delivery of a failure"
    return "retries after a failure"


def test_an_unconfigured_alert_does_not_stick_in_flight():
    """_notify does nothing when alerts are off, so no worker runs."""
    C = harness.load()
    C.CFG["email"]["enabled"] = False
    C._notify_stale(30, True)
    assert C._stale_inflight is False, \
        "inflight stuck on with alerts disabled - no worker to clear it"
    return "no worker, no stuck flag"


def test_an_acknowledged_fault_is_not_resurrected_by_a_late_callback():
    """The callback writes back a fault the operator has cleared.

    Snapshotting the fault, releasing the lock and storing the copy
    afterwards meant an acknowledgement landing in between was undone:
    the dashboard said clear, the file said faulted, and automatic
    running stayed blocked.
    """
    C = harness.load()
    now = C.time.time()
    fault = {"at": now, "msg": "draw fault", "stopped": True,
             "attempted_at": now, "delivered_at": None}
    C._store_fault(fault)

    # The operator acknowledges while the send is still in flight.
    C._clear_fault()
    assert C.STATE["fault"] is None, "precondition: fault not cleared"

    # The callback lands afterwards, referring to the fault it was
    # raised for.
    C._mark_fault_delivered(now)
    assert C.STATE["fault"] is None, \
        "a late callback resurrected an acknowledged fault"
    assert C.load_fault() is None, "resurrected it on disk too"
    return "acknowledgement wins"


def test_a_replacement_fault_is_not_marked_delivered():
    """Without an identity check the callback stamps the wrong fault.

    The new fault would then look as though someone had been told about
    it, and its own restart notice would be suppressed for an hour.
    """
    C = harness.load()
    first = C.time.time() - 100
    C._store_fault({"at": first, "msg": "old", "stopped": True,
                    "attempted_at": first, "delivered_at": None})
    C._clear_fault()

    second = C.time.time()
    C._store_fault({"at": second, "msg": "new", "stopped": True,
                    "attempted_at": second, "delivered_at": None})

    C._mark_fault_delivered(first)          # the OLD fault's callback
    assert C.STATE["fault"]["delivered_at"] is None, \
        "stamped a different fault as delivered"
    assert C.STATE["fault"]["msg"] == "new", "overwrote the newer fault"

    C._mark_fault_delivered(second)
    assert C.STATE["fault"]["delivered_at"] is not None, \
        "the matching callback did not record delivery"
    return "identity checked on at"


# ------------------------------------------ the attempt write is guarded
#
# _mark_fault_delivered was fixed first; the ATTEMPT write on the restart
# path kept the same bug for a round longer. Both now match on "at".


def test_an_acknowledged_fault_is_not_resurrected_by_the_attempt_write():
    """The restart notice stamps attempted_at before it sends.

    It used to snapshot the fault and call _store_fault, so an
    acknowledgement landing in between was written back over: the
    operator cleared it, and the notice put it back.
    """
    C = harness.load()
    now = C.time.time()
    C._store_fault({"at": now, "msg": "draw fault", "stopped": True,
                    "attempted_at": None, "delivered_at": None})
    C._clear_fault()
    assert C.STATE["fault"] is None, "precondition"

    assert C._mark_fault_attempted(now) is False, \
        "stamped a fault that had been acknowledged"
    assert C.STATE["fault"] is None, "resurrected it in memory"
    assert C.load_fault() is None, "resurrected it on disk"
    return "acknowledgement wins"


def test_the_attempt_write_ignores_a_replacement_fault():
    C = harness.load()
    first = C.time.time() - 100
    C._store_fault({"at": first, "msg": "old", "stopped": True})
    C._clear_fault()
    second = C.time.time()
    C._store_fault({"at": second, "msg": "new", "stopped": True})

    assert C._mark_fault_attempted(first) is False, \
        "stamped a different fault"
    assert C.STATE["fault"]["msg"] == "new", "overwrote the newer fault"
    assert C.STATE["fault"].get("attempted_at") is None
    return "identity checked"


def test_the_restart_notice_stays_quiet_if_the_fault_goes_first():
    """No fault, nothing to report - and nothing to write back."""
    C = harness.load()
    sent = wire(C)
    with C._state_lock:
        C.STATE["fault"] = None
    C._notify_restart_fault()
    assert sent == [], "notified about a fault that was not there"
    return "quiet when it has gone"


# ------------------------------------- on_complete is always called once

def test_on_complete_fires_when_alerts_are_switched_off():
    """Otherwise an in-flight flag sticks on for the life of the process.

    Readiness used to be checked in a wrapper and again inside _notify.
    Alerts going off between the two meant no worker ran and nothing
    called back, so the stale warning was silenced permanently - even
    after email was switched back on.
    """
    C = harness.load()
    C.CFG["email"]["enabled"] = False
    seen = []
    C._notify("subject", "body", on_complete=seen.append)
    assert seen == [False], "on_complete not called: %r" % seen
    return "called with False"


def test_on_complete_fires_when_the_thread_cannot_start():
    C = harness.load()
    live_email(C)
    seen = []

    real = C.threading.Thread

    class Broken(object):
        def __init__(self, *a, **k):
            raise RuntimeError("cannot start new thread")

    C.threading.Thread = Broken
    try:
        C._notify("subject", "body", on_complete=seen.append)
    finally:
        C.threading.Thread = real

    assert seen == [False], "on_complete not called on thread failure"
    return "called with False"


def test_a_stuck_inflight_flag_cannot_silence_later_warnings():
    """The end-to-end version of the two tests above."""
    C = harness.load()
    C.CFG["email"]["enabled"] = False
    C._notify_stale(30, True)
    assert C._stale_inflight is False, "stuck in flight with alerts off"

    sent = wire(C)                       # alerts back on
    C._notify_stale(30, True)
    assert len(sent) == 1, "later warning was silenced: %r" % sent
    return "recovers when alerts return"


# -------------------------- a lost timestamp is not a lost safety fault

def test_a_failed_delivery_stamp_does_not_raise_the_unsaved_flag():
    """fault_unsaved means "this fault would be lost on restart".

    Only the alert bookkeeping is at risk here - the fault itself is
    already durable. Raising the flag produced an alarming alert of its
    own, warned the fault could vanish, and blocked requested restarts,
    all for a timestamp whose loss costs one duplicate notice.
    """
    C = harness.load()
    now = C.time.time()
    C._store_fault({"at": now, "msg": "draw fault", "stopped": True})
    assert C.STATE["fault_unsaved"] is False, "precondition: durable"

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    real = os.replace
    os.replace = boom
    try:
        C._mark_fault_delivered(now)
    finally:
        os.replace = real

    assert C.STATE["fault_unsaved"] is False, \
        "a lost delivery stamp was reported as a lost fault"
    return "timestamp loss is not fault loss"


def test_an_already_unsaved_fault_stays_flagged():
    """Restoring the previous value must not mask a real problem."""
    C = harness.load()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"at": 1.0, "msg": "draw fault", "stopped": True})
        assert C.STATE["fault_unsaved"] is True, "precondition: unsaved"
        C._mark_fault_delivered(1.0)
        assert C.STATE["fault_unsaved"] is True, \
            "cleared the unsaved flag on a fault that is still not on disk"
    finally:
        os.replace = real
    return "still flagged"


# --------------------------- alerts off means nothing happens at all

def test_a_restart_with_alerts_off_writes_nothing():
    """Checking readiness late meant a disk write for no reason.

    _mark_fault_attempted writes and fsyncs fault.json. Stamping first
    and checking email afterwards meant EVERY restart with a standing
    fault wrote the file even with alerts switched off - recording an
    attempt that never happened, and on a Pi in a crash loop, writing
    to the SD card every ten seconds for nothing.
    """
    C = harness.load()
    C.CFG["email"]["enabled"] = False
    now = C.time.time()
    C._store_fault({"at": now, "msg": "draw fault", "stopped": True,
                    "attempted_at": None, "delivered_at": None})

    writes = []
    real = C.save_fault

    def counting(f):
        writes.append(f)
        return real(f)

    C.save_fault = counting
    try:
        C._notify_restart_fault()
    finally:
        C.save_fault = real

    assert writes == [], "wrote fault.json with alerts switched off"
    assert C.STATE["fault"]["attempted_at"] is None, \
        "recorded an attempt that never happened"
    return "no write, no false attempt"


def test_a_restart_with_alerts_on_does_stamp_the_attempt():
    """The guard above must not silence the notice when it is wanted."""
    C = harness.load()
    sent = wire(C)
    now = C.time.time()
    C._store_fault({"at": now, "msg": "draw fault", "stopped": True,
                    "attempted_at": None, "delivered_at": None})
    C._notify_restart_fault()
    assert len(sent) == 1, "no notice with alerts on: %r" % sent
    assert C.STATE["fault"]["attempted_at"] is not None, "no attempt stamp"
    return "still notifies when configured"


# ------------------------------------------- exactly once, even on error

def test_on_complete_is_called_once_even_when_it_raises():
    """The contract says exactly once. It used to mean at most twice.

    The disabled-email path called the callback inside the outer try, so
    a callback that raised was caught by the queueing handler, which
    called it again.
    """
    C = harness.load()
    C.CFG["email"]["enabled"] = False
    calls = []

    def angry(ok):
        calls.append(ok)
        raise RuntimeError("bookkeeping exploded")

    C._notify("subject", "body", on_complete=angry)
    assert calls == [False], "called %d times: %r" % (len(calls), calls)
    return "once, and the raise is swallowed"


def test_a_raising_callback_does_not_escape_the_worker():
    C = harness.load()
    live_email(C)
    gate = Gate()
    C._send_mail = gate
    calls = []

    def angry(ok):
        calls.append(ok)
        raise RuntimeError("bookkeeping exploded")

    C._notify("subject", "body", on_complete=angry)
    assert gate.started.wait(5)
    gate.release.set()
    for _ in range(50):
        if calls:
            break
        C.time.sleep(0.05)
    assert calls == [True], "called %d times: %r" % (len(calls), calls)
    return "once from the worker too"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
