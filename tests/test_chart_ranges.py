"""Chart ranges: the observed values, not the drawn scale.

The Low/High figures under each chart were computed in the browser, so
the one thing that matters about them - that they report what the room
DID, not where the chart's axis was stretched to - could only be
asserted by reading the page for a variable name.

The distinction is not academic. The plot stretches its scale to keep
the fan-on and fan-off guides visible, so on 2026-08-17 the dashboard
showed a humidity high of 70.0 while the real 24h high was 63.0: the 70
was the guide, not a reading. That is what moved this into Python.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def _hist(*rows):
    """rows of (t, co2, temp, humidity); None means the sensor was silent."""
    return [{"t": t, "v": v, "tc": tc, "rh": rh} for t, v, tc, rh in rows]


def test_the_range_is_the_lowest_and_highest_seen():
    C = harness.load()
    r = C.history_ranges(_hist((1, 900.0, 21.0, 50.0),
                               (2, 1200.0, 19.0, 63.0),
                               (3, 700.0, 26.5, 45.1)))
    assert r["v"] == {"lo": 700.0, "hi": 1200.0}, r
    assert r["tc"] == {"lo": 19.0, "hi": 26.5}, r
    assert r["rh"] == {"lo": 45.1, "hi": 63.0}, r
    return "low and high per metric"


def test_a_threshold_cannot_widen_the_range():
    """THE BUG THIS EXISTS FOR. The chart's scale is stretched to keep
    the guides on screen; the range must not follow it.

    Here the room never passes 63 while the humidity fan-on guide sits
    at 70 - the case that was actually wrong on screen.
    """
    C = harness.load()
    cfg = C.CFG["control"]
    cfg.update(humidity_enabled=True, humidity_on=70.0, humidity_off=60.0,
               co2_enabled=True, on_at=1000.0, off_at=740.0)
    r = C.history_ranges(_hist((1, 500.0, 20.0, 45.1),
                               (2, 900.0, 21.0, 63.0)))
    assert r["rh"]["hi"] == 63.0, "a guide leaked into the range: %r" % r
    assert r["v"]["hi"] == 900.0, r
    assert r["v"]["lo"] == 500.0, r
    return "guides are not readings"


def test_a_metric_with_no_readings_is_absent():
    """A sensor reporting no humidity has no humidity range. Absent, so
    the page draws nothing - a zero would be drawn, and wrongly."""
    C = harness.load()
    r = C.history_ranges(_hist((1, 900.0, None, None),
                               (2, 950.0, None, None)))
    assert "v" in r and "tc" not in r and "rh" not in r, r
    return "absent, not zero"


def test_non_finite_readings_are_skipped():
    """A NaN low is unserialisable AND unbeatable by any later
    comparison, so one bad reading would freeze the range forever."""
    C = harness.load()
    r = C.history_ranges(_hist((1, float("nan"), float("inf"), 50.0),
                               (2, 900.0, 21.0, 55.0)))
    assert r["v"] == {"lo": 900.0, "hi": 900.0}, r
    assert r["tc"] == {"lo": 21.0, "hi": 21.0}, r
    assert r["rh"] == {"lo": 50.0, "hi": 55.0}, r
    return "NaN and infinity ignored"


def test_an_empty_history_has_no_ranges():
    C = harness.load()
    assert C.history_ranges([]) == {}
    assert C.history_ranges(None) == {}
    return "nothing in, nothing out"


def test_a_malformed_point_does_not_raise():
    """History is read from disk at startup, so it is input."""
    C = harness.load()
    r = C.history_ranges([{"t": 1, "v": 900.0}, "not a point", None,
                          {"v": "hot"}, {"t": 2, "v": 950.0}])
    assert r["v"] == {"lo": 900.0, "hi": 950.0}, r
    return "junk skipped"


def test_the_endpoint_sends_ranges_with_a_full_copy():
    C = harness.load()
    with C._state_lock:
        C.STATE["history"] = _hist((1, 900.0, 21.0, 50.0),
                                   (2, 1200.0, 19.0, 63.0))
        C.STATE["history_rev"] = 2
    body = C.app.test_client().get("/api/history").get_json()
    assert body["full"] is True, body
    assert body["ranges"]["v"] == {"lo": 900.0, "hi": 1200.0}, body["ranges"]
    return "full copy carries them"


def test_the_endpoint_sends_ranges_with_a_delta():
    """The page holds a delta response for most of its life. Ranges
    sent only with a full copy would freeze at page load - wrong in a
    way that looks right for hours."""
    C = harness.load()
    with C._state_lock:
        C.STATE["history"] = _hist((1, 900.0, 21.0, 50.0),
                                   (2, 1200.0, 19.0, 63.0))
        C.STATE["history_rev"] = 2
    url = "/api/history?since=1&gen=%s" % C.PROC_GENERATION
    body = C.app.test_client().get(url).get_json()
    assert body["full"] is False, body
    assert "ranges" in body, body
    assert body["ranges"]["v"]["hi"] == 1200.0, body["ranges"]
    return "deltas carry them too"


def test_the_range_covers_the_whole_trace_not_just_the_delta():
    """A delta carries the new points only; the range must describe
    everything still held, or it would report the last few minutes."""
    C = harness.load()
    with C._state_lock:
        C.STATE["history"] = _hist((1, 400.0, 10.0, 30.0),
                                   (2, 900.0, 21.0, 50.0),
                                   (3, 950.0, 22.0, 52.0))
        C.STATE["history_rev"] = 3
    url = "/api/history?since=2&gen=%s" % C.PROC_GENERATION
    body = C.app.test_client().get(url).get_json()
    assert len(body["add"]) == 1, body["add"]
    assert body["ranges"]["v"]["lo"] == 400.0, (
        "the range describes only the delta: %r" % body["ranges"])
    return "whole trace, not the delta"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
