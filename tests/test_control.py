"""Control logic: decide(), the per-rule latches and the min-run lease.

decide() is where both historical bugs lived, so it carries most of the
cases here. All pure functions - no plug, no console, no threads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

C = harness.load()

CTL = {"mode": "auto", "on_at": 1000.0, "off_at": 700.0,
       "min_run_minutes": 15, "temp_enabled": False,
       "temp_on": 26.5, "temp_off": 24.0}


def cfg(**over):
    c = dict(CTL)
    c.update(over)
    return c


def fresh():
    return {"co2": False, "temp": False}


def test_forced_on_overrides_everything():
    """Force on wins over a fault: it is how a repaired fan is tested."""
    want, why = C.decide(400, 20, False, cfg(mode="on"), None, fresh(),
                         fault={"msg": "draw"})
    assert want is True and why == "forced on", (want, why)


def test_fault_blocks_automatic_start():
    want, why = C.decide(2000, 20, False, cfg(), None, fresh(),
                         fault={"msg": "draw"})
    assert want is False and why == "draw fault", (want, why)


def test_manual_clears_latches_and_acts_on_nothing():
    latch = {"co2": True, "temp": True}
    want, why = C.decide(2000, 30, True, cfg(mode="manual"), None, latch)
    assert want is None and why is None, (want, why)
    assert latch == {"co2": False, "temp": False}, latch


def test_co2_latches_on_and_holds_through_the_band():
    latch = fresh()
    C.decide(1000, None, False, cfg(), None, latch)
    assert latch["co2"] is True, "should latch at on_at"
    C.decide(850, None, True, cfg(), None, latch)
    assert latch["co2"] is True, "must hold between off_at and on_at"
    C.decide(700, None, True, cfg(), 9999, latch)
    assert latch["co2"] is False, "should release at off_at"


def test_threshold_change_does_not_release_a_latch():
    """The trap that left the fan running during live testing.

    A latch lives in memory. Raising on_at back above the reading does
    not release it; only co2 <= off_at does.
    """
    latch = fresh()
    C.decide(830, None, False, cfg(on_at=800), None, latch)
    assert latch["co2"] is True
    want, _ = C.decide(830, None, True, cfg(on_at=1000), 9999, latch)
    assert latch["co2"] is True, "raising on_at must not release the latch"
    assert want is True, "fan should still be held on by the latch"


def test_untriggered_rule_cannot_veto_the_stop():
    """The independence bug: a CO2 run was held on for hours by a
    temperature condition that had never been met."""
    latch = fresh()
    c = cfg(temp_enabled=True)
    C.decide(1200, 20.0, False, c, None, latch)
    assert latch["co2"] is True and latch["temp"] is False
    want, why = C.decide(650, 20.0, True, c, 9999, latch)
    assert want is False, (want, why, latch)


def test_either_rule_alone_holds_the_fan():
    latch = fresh()
    c = cfg(temp_enabled=True)
    C.decide(400, 27.0, False, c, None, latch)
    assert latch["temp"] is True and latch["co2"] is False
    want, _ = C.decide(400, 25.0, True, c, 9999, latch)
    assert want is True, "temp latched between temp_off and temp_on"


def test_disabled_temp_rule_never_latches():
    latch = fresh()
    C.decide(400, 40.0, False, cfg(temp_enabled=False), None, latch)
    assert latch["temp"] is False, "temp must not latch while disabled"


def test_min_run_lease_holds_then_expires():
    latch = fresh()
    c = cfg()
    C.decide(1200, None, False, c, None, latch)
    C.decide(650, None, True, c, 60, latch)
    want, why = C.decide(650, None, True, c, 60, latch)
    assert want is None and why is None, "lease should hold the fan on"
    want, _ = C.decide(650, None, True, c, 15 * 60 + 1, latch)
    assert want is False, "lease expired, fan should stop"


def test_no_reading_is_reported_not_guessed():
    latch = {"co2": False, "temp": False}
    want, why = C.decide(None, None, True, cfg(), 9999, latch)
    assert want is False and why == "no reading", (want, why)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
