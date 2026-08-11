"""The Tasmota reply parsing, which every other test stubs past.

Every plug stub in this suite - test_actuation, test_concurrency,
test_durability, test_failures - replaces the whole client OBJECT,
implementing state() and set() and handing back Python bools. That is
right for what those modules are testing, but it means the translation
UNDERNEATH them has never been exercised: the POWER/POWER1 key handling
and the StatusSNS.ENERGY.Power shape. A firmware update that reshaped
either would leave every one of those modules green.

So these drive the real Tasmota methods and stub only _cmd, the HTTP
boundary. No device, no credentials, no network.

The limit, stated rather than implied: these pin the parser against
shapes Tasmota is DOCUMENTED to send. They cannot confirm those are the
shapes this particular plug actually sends - that needs one captured
/cm reply, which needs the plug password, and the password is not
something to read off disk to satisfy a test. Until such a capture
exists these say "matches documented Tasmota", not "matches the device".
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

C = harness.load()

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "plug_capture.json")


def capture():
    """The replies captured from the real plug, or a skip.

    Skipping rather than failing so a fork with no device is not stuck
    with a red suite. Note CI treats a SKIP as a failure, which is what
    keeps the fixture from quietly disappearing from the repo.
    """
    try:
        return json.load(open(FIXTURE, encoding="utf-8"))["replies"]
    except (OSError, KeyError, ValueError):
        harness.skip("no capture fixture - run tests/capture_plug_fixture.py")


def plug(reply=None, record=None):
    """A Tasmota client whose HTTP call is replaced.

    reply may be a dict, or a callable taking the command string, so a
    test can answer differently per command or capture what was asked.
    """
    p = C.Tasmota("192.0.2.1")

    def _cmd(cmnd):
        if record is not None:
            record.append(cmnd)
        return reply(cmnd) if callable(reply) else reply

    p._cmd = _cmd
    return p


# ---------------------------------------------------------------- relay

def test_the_plain_power_key_is_read():
    assert C.Tasmota._power({"POWER": "ON"}) is True
    assert C.Tasmota._power({"POWER": "OFF"}) is False
    return "POWER ON/OFF"


def test_the_indexed_power_key_is_read():
    """Some builds answer POWER1 even with one relay."""
    assert C.Tasmota._power({"POWER1": "ON"}) is True
    assert C.Tasmota._power({"POWER1": "OFF"}) is False
    return "POWER1 ON/OFF"


def test_the_plain_key_wins_when_both_are_present():
    """Only a fixed order stops the answer depending on dict ordering."""
    assert C.Tasmota._power({"POWER": "ON", "POWER1": "OFF"}) is True
    assert C.Tasmota._power({"POWER": "OFF", "POWER1": "ON"}) is False
    return "POWER before POWER1"


def test_the_value_is_read_case_insensitively():
    assert C.Tasmota._power({"POWER": "on"}) is True
    assert C.Tasmota._power({"POWER": "Off"}) is False
    return "case folded"


def test_an_absent_power_field_is_unknown_and_not_off():
    """The distinction the whole client rests on.

    None means "could not tell". Returning False here would let an
    unreadable plug be recorded as a fan that is off, which is the
    defect item 22 fixed in _force_on. Anything that turns this into a
    bool re-opens it.
    """
    assert C.Tasmota._power({}) is None
    assert C.Tasmota._power({"WARNING": "Need user and password"}) is None
    assert C.Tasmota._power({"StatusSNS": {}}) is None
    return "absent field reads as None"


def test_a_json_boolean_is_believed():
    """A JSON true means the relay is ON.

    This replaces a test that asserted the opposite. The old one said a
    boolean reads as False and called that safe, reasoning that set()
    compares what it got against what it asked for and raises. set()
    does. state() does not - it hands the False straight back, and the
    callers of state() take False as a fact. The old test was wrong,
    and being a test it made the wrong behaviour look decided.
    """
    assert C.Tasmota._power({"POWER": True}) is True
    assert C.Tasmota._power({"POWER": False}) is False
    assert C.Tasmota._power({"POWER1": True}) is True
    return "JSON booleans read as themselves"


def test_a_value_that_is_neither_bool_nor_on_off_is_unknown():
    """Unknown, not off. The distinction the client rests on.

    An int 1 is NOT read as on: Tasmota does not send one, and inventing
    the mapping would be guessing in the one place guessing is unsafe.
    None here means state() raises and the caller switches anyway.
    """
    for junk in (1, 0, "ONLINE", "", "1", "0", None, {}, [], 1.0):
        assert C.Tasmota._power({"POWER": junk}) is None, repr(junk)
    return "10 unusable values all None"


def test_surrounding_whitespace_is_tolerated():
    assert C.Tasmota._power({"POWER": " on "}) is True
    assert C.Tasmota._power({"POWER": "OFF\n"}) is False
    return "trimmed before comparing"


def test_an_unusable_key_falls_through_to_the_next():
    """POWER present but junk must not shadow a good POWER1.

    Short-circuiting on the mere PRESENCE of POWER would report unknown
    for a reply that plainly says what the relay is doing.
    """
    assert C.Tasmota._power({"POWER": "ONLINE", "POWER1": "ON"}) is True
    assert C.Tasmota._power({"POWER": 1, "POWER1": False}) is False
    return "falls through to POWER1"


def test_state_raises_rather_than_returning_false_when_unknown():
    p = plug({})
    try:
        p.state()
    except C.PlugError:
        return "unknown state raises"
    raise AssertionError("an unreadable power field was reported as a state")


def test_state_asks_for_the_power_command():
    """A wrong command returns a shape the parser cannot read, which
    would look like a broken plug rather than a broken call."""
    asked = []
    p = plug({"POWER": "ON"}, record=asked)
    p.state()
    assert asked == ["Power"], asked
    return "cmnd=Power"


def test_set_confirms_from_the_same_reply():
    asked = []
    p = plug({"POWER": "ON"}, record=asked)
    assert p.set(True) is True
    assert asked == ["Power ON"], asked
    return "switch confirmed"


def test_set_refuses_to_report_success_when_the_plug_disagrees():
    """A rejected command must not be recorded as a switch."""
    p = plug({"POWER": "OFF"})
    try:
        p.set(True)
    except C.PlugError:
        return "disagreement raises"
    raise AssertionError("the plug said OFF and set(True) reported success")


def test_set_raises_when_the_reply_carries_no_power_field():
    p = plug({})
    try:
        p.set(False)
    except C.PlugError:
        return "unconfirmed switch raises"
    raise AssertionError("a switch with no confirmation was reported as done")


# ----------------------------------------------------------------- draw

DRAW_SHAPE = {"StatusSNS": {"ENERGY": {"Power": 19.4}}}


def test_the_draw_is_read_from_the_status_8_shape():
    asked = []
    p = plug(DRAW_SHAPE, record=asked)
    assert p.power_w() == 19.4
    assert asked == ["Status 8"], asked
    return "19.4 W from StatusSNS.ENERGY.Power"


def test_a_string_watt_value_is_accepted():
    """Tasmota has been seen to quote numerics depending on build."""
    p = plug({"StatusSNS": {"ENERGY": {"Power": "19.4"}}})
    assert p.power_w() == 19.4
    return "quoted numeric coerced"


def test_zero_watts_is_a_reading_and_not_a_miss():
    """0.0 and None mean different things to the fault detector.

    Out of band includes 0 W; no reading at all is a separate case that
    also counts as out of band but for a different reason. Collapsing
    zero into None would make an unreachable plug and a stopped fan
    indistinguishable.
    """
    p = plug({"StatusSNS": {"ENERGY": {"Power": 0}}})
    v = p.power_w()
    assert v == 0.0 and v is not None, repr(v)
    return "0 W survives as 0.0"


def test_a_bare_integer_is_accepted():
    """What the real plug actually sends when the draw is a whole number.

    Recorded because the table above was written with 19.4 and a quoted
    "19.4", and the device sends 18. float() takes all three, so this
    was never a defect - it is the difference between modelling the
    shapes Tasmota documents and the shapes this plug emits.
    """
    p = plug({"StatusSNS": {"ENERGY": {"Power": 18}}})
    v = p.power_w()
    assert v == 18.0 and isinstance(v, float), repr(v)
    return "bare int coerced to float"


def test_a_draw_that_cannot_be_read_is_none():
    """Every malformed shape returns None rather than raising, because
    the draw thread must keep running when the plug answers oddly."""
    for shape in ({},
                  {"StatusSNS": {}},
                  {"StatusSNS": {"ENERGY": {}}},
                  {"StatusSNS": {"ENERGY": {"Power": None}}},
                  {"StatusSNS": {"ENERGY": {"Power": ""}}},
                  {"StatusSNS": {"ENERGY": {"Power": "n/a"}}},
                  {"StatusSNS": {"ENERGY": None}},
                  {"StatusSNS": None}):
        assert plug(shape).power_w() is None, shape
    return "8 malformed shapes all None"


# ------------------------------------------------------- the real plug
#
# Half B. Everything above models shapes Tasmota is DOCUMENTED to send.
# These three drive the same parsers over replies actually captured
# from the deployment's plug, so the module can claim the device rather
# than the documentation. Reads only - see capture_plug_fixture.py.


def test_the_captured_relay_reply_is_readable():
    """The parser must get a definite answer from the real device.

    None here would mean the plug names its relay something the client
    does not look for, which reads as an unknown state and stops the
    fan being switched at all.
    """
    v = C.Tasmota._power(capture()["Power"])
    assert v is not None, (
        "the relay reply from the real plug parses as unknown")
    assert isinstance(v, bool), type(v)
    return "relay reply parses to %s" % v


def test_the_captured_draw_reply_is_readable():
    """Same for the meter. None would mean the fault detector is blind
    on this device while every unit test above still passes."""
    v = plug(capture()["Status 8"]).power_w()
    assert v is not None, (
        "the draw reply from the real plug parses as no reading")
    assert isinstance(v, float), type(v)
    return "%.1f W" % v


def test_the_capture_uses_shapes_the_table_above_models():
    """The one that will speak up when the firmware moves.

    If a plug update starts sending a relay key the client does not
    look for, or a draw value of a type float() will not take, this
    fails and names it - rather than the unit tests carrying on green
    against shapes the device stopped using. The detail it returns is
    printed by the runner, so a passing run still records WHICH shapes
    were seen.
    """
    reps = capture()
    keys = [k for k in ("POWER", "POWER1") if k in reps["Power"]]
    assert keys, (
        "the captured relay reply carries neither POWER nor POWER1, so "
        "the client cannot read this firmware at all")
    raw = reps["Status 8"]["StatusSNS"]["ENERGY"]["Power"]
    assert isinstance(raw, (int, float, str)), (
        "draw arrived as %s, which float() will not take" % type(raw).__name__)
    return "relay key %s, draw as %s" % (keys[0], type(raw).__name__)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
