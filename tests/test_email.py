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

    def fake_notify(subject, body):
        if C._email_ready():
            sent.append((subject, body))

    C._notify = fake_notify
    return sent


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
                            "notified_at": now}
    C._notify_restart_fault()
    assert sent == [], "notified inside the suppression window"

    with C._state_lock:
        C.STATE["fault"]["notified_at"] = now - C.RESTART_NOTICE_SUPPRESS - 1
    C._notify_restart_fault()
    assert len(sent) == 1, "no notice once the window had passed"
    assert "not" in sent[0][1].lower() and "clear" in sent[0][1].lower(), \
        "the notice does not say a restart will not clear it"
    return "one an hour at most"


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
    C.CFG["email"].update(host="relay.one", password="hunter2")
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

    def __init__(self, auth="CRAM-MD5 PLAIN LOGIN", starttls=False):
        self.esmtp_features = {"auth": auth} if auth else {}
        self._starttls = starttls
        self.calls = []
        self.sent = None
        self.user = None
        self.password = None
        # Real code passes s.auth_cram_md5 to s.auth(), so the fake needs
        # it to exist. Its return value is never inspected here - what is
        # under test is WHICH mechanism gets chosen, not the HMAC.
        self.auth_cram_md5 = lambda *a, **k: "challenge-response"

    def has_extn(self, name):
        return name.lower() == "starttls" and self._starttls

    def starttls(self):
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
        return {}

    def quit(self):
        self.calls.append("quit")


def with_fake(C, fake, **cfg):
    C.CFG["email"].update(enabled=True, host="relay.invalid", port=25,
                          username="user", password="pw",
                          sender="fan@invalid", recipients="a@invalid")
    C.CFG["email"].update(**cfg)
    C.smtplib.SMTP = lambda *a, **k: fake
    return fake


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
    fake = with_fake(C, FakeSMTP(), recipients="a@x.com, b@y.com")
    try:
        C._send_mail("Fan stopped", "the body")
    finally:
        C.smtplib.SMTP = real
    sender, to, msg = fake.sent
    assert sender == "fan@invalid", sender
    assert to == ["a@x.com", "b@y.com"], to
    assert "Subject: Fan stopped" in msg, msg[:200]
    assert "From: fan@invalid" in msg
    assert "To: a@x.com, b@y.com" in msg
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

def test_recovery_alert_only_follows_an_alert_that_was_sent():
    """A blip that never alerted must not produce an all-clear."""
    C = harness.load()
    sent = wire(C)

    C._stale_notified = False
    C._notify_stale(30, True)
    assert len(sent) == 1

    # Recovery resets the edge and announces.
    C._stale_notified = False
    assert C._stale_notified is False
    C._notify_stale(30, True)
    assert len(sent) == 2, "a later outage did not alert again"
    return "one all-clear per outage"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
