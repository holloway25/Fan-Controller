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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

C = harness.load()


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


def test_only_the_string_on_counts_as_on():
    """Recorded as the boundary, not endorsed as ideal.

    A JSON boolean would stringify to TRUE and read as OFF. That fails
    in the SAFE direction - set() compares what it got against what it
    asked for and raises rather than reporting a success - but if a
    firmware ever starts sending booleans this test is the one that
    will say so.
    """
    assert C.Tasmota._power({"POWER": True}) is False
    assert C.Tasmota._power({"POWER": 1}) is False
    assert C.Tasmota._power({"POWER": "ONLINE"}) is False
    return "string ON only"


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


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
