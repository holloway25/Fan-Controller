"""Failure handling: persistence errors and malformed request bodies.

Everything here injects a failure rather than waiting for one, so no
disk is filled and no permissions are changed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def boom(*a, **k):
    raise OSError(28, "No space left on device")


def test_unwritable_fault_stays_live_and_is_flagged():
    """A fault that cannot be persisted must not be forgotten.

    save_fault used to swallow every OSError, so the fan was stopped and
    marked faulted in memory while nothing reached disk. A restart then
    reloaded no fault and automatic running resumed on a fan already
    judged faulty.
    """
    C = harness.load()
    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"msg": "19 W outside 15-22 W for 30s", "at": 1})
    finally:
        os.replace = real

    assert C.STATE["fault"] is not None, "fault lost from memory"
    assert C.STATE["fault_unsaved"] is True, "not flagged as unpersisted"
    assert any("could not be saved" in e["msg"] for e in C.STATE["events"]), \
        "no warning logged"
    return "kept live and flagged"


def test_unwritable_fault_is_retried_until_it_lands():
    C = harness.load()
    real = os.replace
    os.replace = boom
    try:
        C._store_fault({"msg": "draw fault", "at": 1})
    finally:
        os.replace = real

    assert C._retry_fault_persist() is True, "retry did not save"
    assert C.STATE["fault_unsaved"] is False
    assert (C.load_fault() or {}).get("msg") == "draw fault", \
        "fault would not survive a restart"
    assert C._retry_fault_persist() is False, "retried with nothing owed"
    return "recovered on retry"


def test_failed_config_write_changes_nothing():
    """A 500 must not leave the controller running on unsaved settings."""
    C = harness.load()
    client = C.app.test_client()
    before = C.CFG["control"]["on_at"]
    C.save_cfg = boom

    r = client.post("/api/config", json={"control": {"on_at": 1025}})

    assert r.status_code == 500, r.status_code
    assert C.CFG["control"]["on_at"] == before, (
        "live settings changed despite a failed write: %r"
        % (C.CFG["control"]["on_at"],))
    return "nothing changed"


def test_failed_password_write_changes_nothing():
    C = harness.load()
    client = C.app.test_client()
    C.CFG["ui"]["password_hash"] = ""
    C.save_cfg = boom

    r = client.post("/api/ui/password", json={"password": "placeholder-not-a-real-password"})

    assert r.status_code == 500, r.status_code
    assert C.CFG["ui"]["password_hash"] == "", \
        "authentication changed in memory only"
    return "nothing changed"


def test_malformed_bodies_are_rejected_not_crashed():
    """Structurally invalid JSON is a client error, not a 500."""
    C = harness.load()
    client = C.app.test_client()
    cases = [
        ("/api/config", {"control": "bad"}),
        ("/api/config", {"plug": [1]}),
        ("/api/config", "bad"),
        ("/api/config", [1, 2]),
        ("/api/ui/password", {"password": 123}),
        ("/api/ui/password", "bad"),
        ("/api/clienterror", "bad"),
    ]
    for path, body in cases:
        r = client.post(path, json=body)
        assert r.status_code == 400, "%s %r gave %s" % (
            path, body, r.status_code)
    # Raw bodies, not json=: a truncated "{" parsed to None under
    # get_json(silent=True) and read as an empty request, so the save
    # answered 200 Saved and changed nothing.
    raw = [("/api/config", "{", 400),
           ("/api/config", "null", 400),
           ("/api/config", "[1,2]", 400),
           ("/api/config", "{}", 200)]
    for path, data, want in raw:
        r = client.post(path, data=data, content_type="application/json")
        assert r.status_code == want, "%s %r gave %s, wanted %s" % (
            path, data, r.status_code, want)
    # Right shape, wrong content type, is still a 415.
    assert client.post("/api/config", data="{}").status_code == 415
    return "%d bodies rejected, %d raw checked" % (len(cases), len(raw))

class failing_os(object):
    """Force one os call to fail, and always put it back."""

    def __init__(self, name, err=OSError(28, "No space left on device")):
        self.name, self.err, self.real = name, err, None

    def __enter__(self):
        self.real = getattr(os, self.name)
        def blow(*a, **k):
            raise self.err
        setattr(os, self.name, blow)
        return self

    def __exit__(self, *exc):
        setattr(os, self.name, self.real)
        return False


FAULT = {"msg": "19 W outside 15-22 W for 30s", "at": 1, "stopped": False}


def test_retry_does_not_resurrect_an_acknowledged_fault():
    """The retry snapshotted outside _fault_lock.

    An acknowledgement landing between the snapshot and the write was
    overwritten: memory clear, disk faulted, and the next restart
    brought back a fault the operator had already dismissed.
    """
    C = harness.load()
    with failing_os("replace"):
        C._store_fault(dict(FAULT))
    C._clear_fault()

    assert C._retry_fault_persist() is False, "retried a cleared fault"
    assert C.STATE["fault"] is None
    assert not os.path.exists(C.FAULT_PATH), "cleared fault written back"
    return "stayed cleared"


def test_failed_clear_keeps_the_fault_live():
    """Clearing memory first meant a failed delete still reported gone."""
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("remove"):
        had, ok = C._clear_fault()

    assert ok is False, "reported success on a failed delete"
    assert had is not None
    assert C.STATE["fault"] is not None, "fault dropped from memory"
    assert os.path.exists(C.FAULT_PATH), "file gone but call failed"
    return "fault kept"


def test_failed_clear_endpoint_answers_503():
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("remove"):
        r = C.app.test_client().post("/api/fault/clear", json={})

    assert r.status_code == 503, r.status_code
    assert C.STATE["fault"] is not None, "fault cleared despite the error"
    return "503, fault still live"


def test_failed_stop_confirmation_is_retried():
    """stopped=True in memory before the write made the miss permanent.

    The next call saw it already true and returned early, so disk and
    memory disagreed for good.
    """
    C = harness.load()
    C._store_fault(dict(FAULT))
    with failing_os("replace"):
        first = C._confirm_fault_stopped()

    assert first is False, "claimed confirmed after a failed write"
    assert C.STATE["fault"].get("stopped") is not True, \
        "memory moved ahead of disk"
    assert C.STATE["fault_unsaved"] is True, "not flagged for retry"

    assert C._confirm_fault_stopped() is True, "never retried"
    assert json.load(open(C.FAULT_PATH)).get("stopped") is True
    return "retried and landed"


def test_an_unreachable_console_does_not_paste_the_retry_stack():
    """THE LINE FROM 2026-08-21, shortened.

    requests renders a connection failure as the pool, the port, the
    full request URL, the wrapped urllib3 error and the errno under it.
    That went into the activity log verbatim, once per poll, for as
    long as the UDM took to finish its OS update - four wrapped lines
    on a phone to say the console was not answering.

    The assertion is on LENGTH and on the absence of the machinery,
    because the failure mode is verbosity itself; a test that only
    checked for the words "no connection" would pass with the whole
    stack appended after them.
    """
    import requests as R
    C = harness.load()
    e = R.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='console.invalid', port=443): Max retries "
        "exceeded with url: /proxy/protect/api/bootstrap (Caused by "
        "NameResolutionError(\"<urllib3.connection.HTTPSConnection object at "
        "0x7f00>: Failed to resolve 'console.invalid' ([Errno -3] Temporary "
        "failure in name resolution)\"))")
    why = C._why(e)
    assert len(why) < 40, "still long: %r" % why
    for junk in ("HTTPSConnectionPool", "Max retries", "urllib3", "Errno",
                 "/proxy/", "port=443"):
        assert junk not in why, "%r survived into %r" % (junk, why)
    return "the stack does not reach the log"


def test_a_name_that_does_not_resolve_says_so():
    """DNS gets its own phrase because it has its own remedy.

    troubleshooting.md sends people to `getent hosts` and
    `resolvectl status` for this, which is wasted advice for a console
    that resolved fine and refused the connection.

    Built as a real wrapped exception rather than by matching text, so
    this pins the chain walk and not a string.
    """
    import socket as S
    import requests as R
    C = harness.load()
    inner = S.gaierror(-3, "Temporary failure in name resolution")
    outer = R.exceptions.ConnectionError(inner)
    assert C._dns_failure(outer), "a wrapped gaierror was not recognised"
    assert "resolve" in C._why(outer), C._why(outer)
    # And an ordinary refusal is NOT reported as a name problem.
    plain = R.exceptions.ConnectionError("connection refused")
    assert not C._dns_failure(plain)
    assert "resolve" not in C._why(plain), C._why(plain)
    return "DNS distinguished from every other refusal"


def test_a_cause_chain_that_loops_does_not_hang_the_poll_thread():
    """The walk is bounded on purpose.

    requests wraps urllib3, urllib3 wraps the socket error, and nothing
    guarantees the chain is acyclic. An unbounded walk here hangs the
    poll thread INSIDE ITS OWN ERROR HANDLER - the controller stops
    reading and stops reporting, which is a worse outage than whatever
    it was trying to describe.

    UNDER AN ALARM, because the failure mode is non-termination and a
    plain assert cannot observe that: an unbounded loop never reaches
    the assertion, so the test hangs and the run reports nothing rather
    than a failure. This was not theoretical - the bound was removed
    accidentally while mutation-testing this change, and the only
    symptom was a suite that stopped producing output. The alarm turns
    that into a named failure.
    """
    import signal
    import requests as R
    if not hasattr(signal, "SIGALRM"):
        harness.skip("SIGALRM is POSIX-only")

    def ring(signum, frame):
        raise AssertionError(
            "_dns_failure did not terminate on a cyclic cause chain; "
            "the poll thread would hang in its own error handler")

    C = harness.load()
    a = R.exceptions.ConnectionError("a")
    b = R.exceptions.ConnectionError("b")
    a.__cause__ = b
    b.__cause__ = a

    old = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        assert C._dns_failure(a) is False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    return "bounded walk returns"


def test_a_login_http_failure_is_a_protect_failure():
    """raise_for_status raises HTTPError, which is not a ProtectError.

    The poll loop catches ProtectError and PlugError by name and files
    everything else under FC-200 unclassified. So a console returning
    503 - a UniFi service restarting is the ordinary case - was
    reported as an unclassified fault in this controller rather than
    as the Protect outage it is. requests also puts the full login URL
    in that message, which then reached the activity log.

    Both halves are asserted: the class, and the absence of the URL.
    """
    import requests as R
    C = harness.load()

    class Reply(object):
        status_code = 503
        headers = {}

        def raise_for_status(self):
            # The real thing, URL included. Without this the stub only
            # proves that SOMETHING non-ProtectError escapes; the
            # regression being guarded is specifically HTTPError, and
            # the leaked URL is half of what makes it a defect.
            raise R.exceptions.HTTPError(
                "503 Server Error: Service Unavailable for url: "
                "https://console.invalid/api/auth/login")

    class Session(object):
        verify = False

        def post(self, *a, **k):
            return Reply()

    C.requests.Session = Session
    try:
        C.Protect("console.invalid", "u", "p", False).login()
    except C.ProtectError as exc:
        assert "503" in str(exc), exc
        assert "https://" not in str(exc), exc
        assert "console.invalid" not in str(exc), exc
    except Exception as exc:
        assert False, (
            "a login HTTP failure escaped as %s, so the poll loop files "
            "it as unclassified: %r" % (type(exc).__name__, exc))
    else:
        assert False, "HTTP 503 was accepted as a successful login"
    return "a login HTTP failure reads as FC-201"


def test_a_console_serving_html_is_a_protect_fault_not_an_unclassified_one():
    """A UDM mid OS update answers 200 with a holding page.

    r.json() then raised JSONDecodeError out of the poll loop's
    catch-all, and the log said "FC-200 JSONDecodeError: Expecting
    value: line 1 column 1" - the unclassified series, pointing at this
    controller, for something Protect did and recovered from on its
    own.
    """
    C = harness.load()

    class Reply(object):
        status_code = 200
        content = b"<html>Updating</html>"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    p = C.Protect("console.invalid", "u", "p", False)
    p._s = type("S", (), {"get": lambda *a, **k: Reply()})()
    p._csrf = "x"
    try:
        p._get("/proxy/protect/api/bootstrap")
    except C.ProtectError as exc:
        assert "JSON" in str(exc), exc
        assert "Expecting value" not in str(exc), exc
    except Exception as exc:
        # THE POINT OF THE TEST. Anything other than ProtectError means
        # the decode error reached the poll loop's catch-all, which
        # files it as FC-200 unclassified and blames this controller
        # for a console that was restarting. Caught here so that
        # regression reads as a failure rather than as a broken test.
        assert False, (
            "a non-JSON reply escaped as %s, so the poll loop records it "
            "as unclassified: %r" % (type(exc).__name__, exc))
    else:
        assert False, "a non-JSON reply was accepted"
    return "HTML holding page reads as a Protect problem"


def test_readings_survive_a_payload_without_co2():
    """CO2 can be disabled, so a missing CO2 field is not fatal.

    readings() used to raise, which took temperature and humidity down
    with it and stopped the poll entirely.
    """
    C = harness.load()

    class Stub:
        def sensor(self, sid):
            return {"airQuality": {"temperature": 21.5, "humidity": 72.0}}

    vals, path = C.Protect.readings(Stub(), "x")
    assert vals["co2"] is None and path is None, (vals, path)
    assert vals["temperature"] == 21.5 and vals["humidity"] == 72.0

    c = dict(C.CFG["control"])
    c.update(mode="auto", co2_enabled=False, temp_enabled=False,
             humidity_enabled=True, humidity_on=70.0, humidity_off=60.0,
             min_run_minutes=0)
    latch = {"co2": False, "temp": False, "humidity": False}
    want, why = C.decide(None, None, False, c, None, latch, humidity=72.0)
    assert want is True and "72% RH" in why, (want, why)
    return "humidity drives it with no CO2 present"


def test_poll_fails_only_when_no_enabled_trigger_has_a_reading():
    C = harness.load()
    c = dict(C.CFG["control"])

    def usable(vals, **flags):
        """Mirrors the guard in tick()."""
        c.update(flags)
        live = C.active_rules(c)
        if live:
            return any(vals.get(r["reading"]) is not None for r in live)
        return any(vals.get(k) is not None
                   for k in ("co2", "temperature", "humidity", "pm25"))

    no_co2 = {"co2": None, "temperature": 21.5, "humidity": 72.0}
    assert usable(no_co2, co2_enabled=True, temp_enabled=True,
                  humidity_enabled=False), "temperature alone is enough"
    assert not usable({"co2": None}, co2_enabled=True, temp_enabled=False,
                      humidity_enabled=False), "nothing live had a reading"
    # With NOTHING enabled the fail-safe is the only automatic action
    # left, and it is armed by reading_at going stale. Accepting an
    # empty payload kept reading_at fresh forever, so stale_minutes
    # never expired and the last protection was silently disabled.
    off = dict(co2_enabled=False, temp_enabled=False,
               humidity_enabled=False)
    assert not usable({"co2": None}, **off), \
        "empty payload accepted with no triggers enabled"
    assert usable({"co2": None, "humidity": 52.0}, **off), \
        "a real reading still counts as a live sensor"
    return "judged per enabled rule, and per any metric when none are"

def test_empty_payload_never_refreshes_reading_at():
    """Drives the real tick(), not a copy of its guard.

    With no triggers enabled the fail-safe is the only automatic action
    left, and it is armed by reading_at going stale. Accepting a payload
    with nothing in it kept reading_at fresh forever, so stale_minutes
    never expired and the last protection was silently disabled.
    """
    C = harness.load()

    class Sensor:
        def readings(self, sid):
            return {"co2": None}, None

    class Plug:
        def state(self):
            return False

        def set(self, v):
            return v

        def power_w(self):
            return 0.0

    C._clients = lambda: (Sensor(), Plug())
    C.CFG["protect"]["sensor_id"] = "x"
    C.CFG["control"].update(mode="auto", co2_enabled=False,
                            temp_enabled=False, humidity_enabled=False)
    with C._state_lock:
        C.STATE["reading_at"] = 123.0
        C.STATE["fault"] = None

    raised = None
    try:
        C.tick()
    except C.ProtectError as exc:
        raised = str(exc)

    assert raised, "an empty payload was accepted as a reading"
    assert C.STATE["reading_at"] == 123.0, \
        "reading_at advanced, so stale_minutes would never expire"
    return "fail-safe stays armed"
if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
