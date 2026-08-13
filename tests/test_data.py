"""Validation, retention and metric discovery.

_coerce guards every settings write, prune_hist decides what the 24h
trace keeps, and find_metric is how readings survive Ubiquiti renaming
fields. All pure.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

C = harness.load()


def refused(spec, value, name="field"):
    """True if _coerce rejects the value."""
    try:
        C._coerce(name, spec, value)
        return False
    except ValueError:
        return True


def test_numeric_strings_are_stored_coerced():
    """A threshold posted as "1000" must not reach decide() as text.

    Storing it raw made decide() raise TypeError every poll, disabling
    automation while the UI still looked healthy.
    """
    out = C._coerce("on_at", ("num", 0, 5000), "1000")
    assert out == 1000.0 and isinstance(out, float), repr(out)


def test_booleans_are_not_numbers():
    assert refused(("num", 0, 10), True), "bool accepted as a number"
    assert refused(("num", 0, 10), False)


def test_numeric_range_is_enforced():
    assert refused(("num", 15, 22), 14.9)
    assert refused(("num", 15, 22), 22.1)
    assert C._coerce("w", ("num", 15, 22), 22) == 22.0


def test_non_finite_is_refused():
    assert refused(("num", 0, 10), float("nan"))
    assert refused(("num", 0, 10), float("inf"))


def test_int_fields_reject_fractions():
    assert refused(("int", 0, 60), 1.5)
    assert C._coerce("m", ("int", 0, 60), 15.0) == 15


def test_enum_and_bool_are_strict():
    assert refused(("enum", ("auto", "manual", "on")), "off")
    assert refused(("bool",), "true"), "the string 'true' must be refused"
    assert C._coerce("b", ("bool",), False) is False


def test_prune_keeps_by_timestamp_not_count():
    now = time.time()
    hist = [{"t": now - 25 * 3600, "v": 1},
            {"t": now - 23 * 3600, "v": 2},
            {"t": now, "v": 3}]
    kept = C.prune_hist(hist, now)
    assert [p["v"] for p in kept] == [2, 3], kept


def test_prune_drops_malformed_points():
    now = time.time()
    kept = C.prune_hist(["nonsense", None, {"v": 1}, {"t": now, "v": 2}], now)
    assert kept == [{"t": now, "v": 2}], kept


def test_prune_caps_memory():
    now = time.time()
    kept = C.prune_hist([{"t": now, "v": i} for i in range(C.HIST_CAP + 50)],
                        now)
    assert len(kept) == C.HIST_CAP
    assert kept[-1]["v"] == C.HIST_CAP + 49, "must keep the newest"


def test_find_metric_searches_by_hint_not_path():
    payload = {"airQuality": {"nested": {"co2": 812}}}
    assert C.find_metric(payload, C.CO2_HINTS) == (812.0,
                                                   "airQuality.nested.co2")


def test_find_metric_unwraps_value_objects():
    payload = {"carbon_Dioxide": {"value": 640}}
    hit = C.find_metric(payload, C.CO2_HINTS)
    assert hit is not None and hit[0] == 640.0, hit


def test_find_metric_ignores_booleans_and_misses():
    assert C.find_metric({"co2Enabled": True}, C.CO2_HINTS) is None
    assert C.find_metric({"humidity": 55}, C.CO2_HINTS) is None


def test_a_malformed_timestamp_does_not_crash_the_load():
    """One bad point used to take the whole service down, repeatedly.

    prune_hist compared every timestamp with a number, so a string, a
    list or a None raised TypeError - during module import, which with
    Restart=on-failure means a crash loop. A graph point is not worth
    that: drop it and keep serving.
    """
    C = harness.load()
    now = C.time.time()
    good = {"t": now, "v": 800}
    bad = [
        {"t": "bad"},
        {"t": None},
        {"t": [1]},
        {"t": {"nested": 1}},
        {"t": True},                     # bool is an int subclass
        {"t": float("nan")},
        {"t": float("inf")},
        {"t": float("-inf")},
        "not a dict",
        None,
        42,
    ]
    kept = C.prune_hist(bad + [good], now)
    assert kept == [good], "kept something unusable: %r" % kept
    return "%d malformed points dropped" % len(bad)


def test_a_good_point_survives_alongside_rubbish():
    C = harness.load()
    now = C.time.time()
    pts = [{"t": now - 10, "v": 1}, {"t": "x"}, {"t": now - 5, "v": 2}]
    kept = C.prune_hist(pts, now)
    assert [p["v"] for p in kept] == [1, 2], kept
    return "order and content preserved"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
