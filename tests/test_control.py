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
    assert not any(latch.values()), latch


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

def test_co2_rule_can_be_disabled():
    latch = fresh()
    want, _ = C.decide(2000, None, False, cfg(co2_enabled=False), None, latch)
    assert latch["co2"] is False, "disabled rule must not latch"
    assert want is False, (want, latch)


def test_missing_co2_enabled_defaults_to_on():
    """An upgraded config has no co2_enabled key. It must still ventilate."""
    c = cfg()
    c.pop("co2_enabled", None)
    latch = fresh()
    C.decide(1200, None, False, c, None, latch)
    assert latch["co2"] is True, "absent flag must read as enabled"


def test_humidity_latches_above_and_releases_below():
    latch = fresh()
    c = cfg(humidity_enabled=True, humidity_on=70.0, humidity_off=60.0)
    want, why = C.decide(400, None, False, c, None, latch, humidity=72.0)
    assert latch["humidity"] is True and want is True, (want, latch)
    assert "72% RH" in why, why
    C.decide(400, None, True, c, 9999, latch, humidity=65.0)
    assert latch["humidity"] is True, "must hold inside the band"
    want, _ = C.decide(400, None, True, c, 9999, latch, humidity=59.0)
    assert latch["humidity"] is False and want is False


def test_humidity_disabled_by_default():
    latch = fresh()
    C.decide(400, None, False, cfg(), None, latch, humidity=95.0)
    assert latch["humidity"] is False, "humidity must be opt-in"


def test_three_rules_are_independent():
    """Any one rule holds the fan; none vetoes another's stop."""
    latch = fresh()
    c = cfg(temp_enabled=True, humidity_enabled=True,
            humidity_on=70.0, humidity_off=60.0)
    C.decide(1200, 20.0, False, c, None, latch, humidity=50.0)
    assert (latch["co2"], latch["temp"], latch["humidity"]) == \
        (True, False, False), latch
    want, _ = C.decide(650, 20.0, True, c, 9999, latch, humidity=50.0)
    assert want is False, "untriggered rules must not veto the stop"


def test_every_rule_disabled_means_auto_never_starts():
    """Allowed on purpose - the UI warns. The fan just never starts."""
    latch = fresh()
    c = cfg(co2_enabled=False, temp_enabled=False, humidity_enabled=False)
    assert C.active_rules(c) == []
    want, _ = C.decide(5000, 40.0, False, c, None, latch, humidity=99.0)
    assert want is False, "nothing enabled must not start the fan"
    assert not any(latch.values()), latch
def test_a_dropped_reading_holds_every_latch():
    """The three rules used to disagree about this.

    CO2 held its latch when the reading vanished; temperature and
    humidity released theirs. Now all three hold, because a missing
    reading says nothing about whether the condition cleared.
    """
    for flag, hot, extra in (
            ("co2_enabled", 1200, {}),
            ("temp_enabled", 30.0, {}),
            ("humidity_enabled", 80.0,
             {"humidity_on": 70.0, "humidity_off": 60.0})):
        over = {flag: True}
        over.update(extra)
        c = cfg(**over)
        latch = {"co2": False, "temp": False, "humidity": False}

        if flag == "co2_enabled":
            C.decide(hot, None, False, c, None, latch)
        elif flag == "temp_enabled":
            C.decide(None, hot, False, c, None, latch)
        else:
            C.decide(None, None, False, c, None, latch, humidity=hot)
        assert any(latch.values()), "%s never latched on" % flag

        # Same call, every reading now missing.
        want, _ = C.decide(None, None, True, c, 9999, latch)
        assert any(latch.values()), (
            "%s released its latch on a missing reading, so the fan "
            "would stop on the strength of not knowing" % flag)
        assert want is True, "%s stopped the fan on a dropped reading" % flag
    return "co2, temp and humidity all hold"


def test_a_reading_that_returns_low_still_releases():
    """Holding through a gap must not mean holding forever."""
    c = cfg(temp_enabled=True)
    latch = {"co2": False, "temp": False, "humidity": False}

    C.decide(None, 30.0, False, c, None, latch)
    assert latch["temp"] is True, "did not latch on a hot reading"

    C.decide(None, None, True, c, 9999, latch)
    assert latch["temp"] is True, "released during the gap"

    C.decide(None, 22.0, True, c, 9999, latch)
    assert latch["temp"] is False, (
        "a reading below the off level did not release the latch")
    return "the gap holds, a cool reading releases"


def test_a_disabled_rule_still_releases_when_its_reading_is_missing():
    """Turning a rule off must release it even with nothing to read."""
    c = cfg(temp_enabled=True)
    latch = {"co2": False, "temp": False, "humidity": False}
    C.decide(None, 30.0, False, c, None, latch)
    assert latch["temp"] is True, "did not latch on"

    C.decide(None, None, True, cfg(temp_enabled=False), 9999, latch)
    assert latch["temp"] is False, (
        "a rule switched off kept its latch, so the fan would keep "
        "running on a rule nobody asked for")
    return "the enable flag still wins"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
