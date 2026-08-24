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


def test_a_stop_names_only_the_rule_that_was_holding_it_on():
    """THE STOP LINE IS ABOUT ONE RULE, not every enabled metric.

    With CO2, temperature and humidity all enabled but only CO2 ever
    latching, the log read "Fan off - 672 ppm, 21.9 C, 54% RH". The
    two extra numbers were never part of the decision, and their
    presence implies they were.

    Asserted by ABSENCE as well as presence: a test that only checked
    the CO2 figure was there would pass against the old line too.
    """
    latch = fresh()
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0,
            humidity_enabled=True, humidity_on=70.0, humidity_off=60.0)
    latch["humidity"] = False
    # CO2 alone takes the fan on.
    want, _ = C.decide(1200, 21.9, False, c, None, latch, humidity=54.0)
    assert want is True and latch["co2"] is True, (want, latch)
    # ...and CO2 alone releasing is what stops it.
    want, why = C.decide(672, 21.9, True, c, 9999, latch, humidity=54.0)
    assert want is False, (want, why)
    assert "672 ppm" in why, why
    assert "21.9 C" not in why, "an unlatched rule appeared in the stop: %r" % why
    assert "54% RH" not in why, "an unlatched rule appeared in the stop: %r" % why
    return "the stop names CO2 only"


def test_a_stop_names_every_rule_that_was_holding_it_on():
    """Two latched rules releasing together are both the reason."""
    latch = fresh()
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0)
    want, _ = C.decide(1200, 27.0, False, c, None, latch)
    assert want is True and latch["co2"] and latch["temp"], latch
    want, why = C.decide(672, 23.0, True, c, 9999, latch)
    assert want is False, (want, why)
    assert "672 ppm" in why and "23.0 C" in why, why
    return "both released rules named"


def test_a_stop_names_rules_that_released_earlier_in_the_run():
    """THE WHOLE RUN, not whichever rule happened to let go last.

    Temperature and humidity take the fan on alongside CO2, then both
    fall back inside their bands while CO2 keeps it running. Snapshotting
    the latches at the stop names CO2 alone, which is true of that
    instant and misleading about the run: the fan ran on three rules.

    The mid-run cycle returns want=True with no message, so the only
    place this can be recorded is the accumulator - and the only way to
    catch a regression is a stop that happens SEVERAL cycles after the
    releases.
    """
    latch = fresh()
    ran = {}
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0,
            humidity_enabled=True, humidity_on=70.0, humidity_off=60.0)
    C.decide(1200, 27.0, False, c, None, latch, humidity=75.0, ran_with=ran)
    # Temperature and humidity release; CO2 alone holds the fan on.
    C.decide(1200, 23.0, True, c, 9999, latch, humidity=55.0, ran_with=ran)
    assert latch["temp"] is False and latch["humidity"] is False, latch
    want, why = C.decide(672, 23.0, True, c, 9999, latch,
                         humidity=55.0, ran_with=ran)
    assert want is False, (want, why)
    for expected in ("672 ppm", "23.0 C", "55% RH"):
        assert expected in why, \
            "a rule that ran was dropped from the stop: %r" % why
    return "the whole run is named"


def test_a_rule_that_joined_mid_run_is_named_at_the_stop():
    """Latching later still counts as having run the fan."""
    latch = fresh()
    ran = {}
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0)
    C.decide(1200, 20.0, False, c, None, latch, ran_with=ran)
    C.decide(1200, 27.0, True, c, 9999, latch, ran_with=ran)
    want, why = C.decide(672, 23.0, True, c, 9999, latch, ran_with=ran)
    assert want is False, (want, why)
    assert "672 ppm" in why and "23.0 C" in why, why
    return "a late latch is still part of the run"


def test_the_run_record_does_not_leak_into_the_next_run():
    """The accumulator is per-run, and the stop is what ends it.

    Without a reset, the next CO2-only run would report temperature and
    humidity it never touched - the original defect, made worse by
    being wrong about a different run.
    """
    latch = fresh()
    ran = {}
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0,
            humidity_enabled=True, humidity_on=70.0, humidity_off=60.0)
    C.decide(1200, 27.0, False, c, None, latch, humidity=75.0, ran_with=ran)
    C.decide(672, 23.0, True, c, 9999, latch, humidity=55.0, ran_with=ran)
    assert ran == {}, "the accumulator survived the stop: %r" % ran
    # A second run on CO2 alone.
    C.decide(1200, 20.0, False, c, None, latch, humidity=40.0, ran_with=ran)
    want, why = C.decide(672, 20.0, True, c, 9999, latch,
                         humidity=40.0, ran_with=ran)
    assert want is False, (want, why)
    assert "672 ppm" in why, why
    assert "C" not in why and "RH" not in why, \
        "the previous run's rules appeared in this stop: %r" % why
    return "each run is named on its own"


def test_manual_mode_ends_the_run_record():
    """Switching to Manual ends the run, so nothing carries out of it."""
    latch = fresh()
    ran = {}
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0)
    C.decide(1200, 27.0, False, c, None, latch, ran_with=ran)
    assert ran, "nothing was recorded to begin with"
    C.decide(1200, 27.0, True, cfg(mode="manual"), 9999, latch, ran_with=ran)
    assert ran == {}, "Manual left a run record behind: %r" % ran
    return "Manual clears the run record"


def test_a_stop_still_works_without_a_run_record():
    """ran_with is optional; callers testing one decision omit it."""
    latch = fresh()
    c = cfg(temp_enabled=True, temp_on=26.5, temp_off=24.0)
    C.decide(1200, 27.0, False, c, None, latch)
    want, why = C.decide(672, 23.0, True, c, 9999, latch)
    assert want is False and "672 ppm" in why, (want, why)
    return "falls back to the latches at the stop"


def test_a_stop_with_nothing_latched_still_says_something():
    """Auto taking back a fan it never started.

    No rule released, so there is nothing to name. The fallback must
    not produce the empty string the "Fan on" branch was already fixed
    for once.
    """
    latch = fresh()
    want, why = C.decide(500, None, True, cfg(), 9999, latch)
    assert want is False, (want, why)
    assert why and why.strip(), "a stop with no latch printed nothing"
    assert "500 ppm" in why, why
    return "falls back to the enabled readings"


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


def test_a_latch_without_a_reading_still_explains_itself():
    """A latch can outlive its reading, so it may have nothing to print.

    The activity log used to read "Fan on - " with no reason, which is
    worst exactly when the reason is unusual: a rule holding the fan on
    from a reading that stopped arriving.
    """
    c = cfg(temp_enabled=True)
    latch = {"co2": False, "temp": False, "humidity": False}

    C.decide(None, 30.0, False, c, None, latch)
    assert latch["temp"] is True, "did not latch on"

    # Someone switches the fan off at the wall; temperature has gone
    # quiet, but CO2 still reports so the poll is valid.
    want, why = C.decide(500, None, False, c, None, latch)

    assert want is True, "did not switch it back on"
    assert why, "switched the fan on and gave no reason"
    assert "temperature" in why and "no reading" in why, (
        "the reason did not name the rule holding it on: %r" % why)
    return "%r" % why


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
