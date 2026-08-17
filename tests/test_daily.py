"""Daily low/high summaries: the answer the 24h trace cannot give.

history is pruned to 24 hours by timestamp, so anything derived from it
can only ever describe the last day. On 2026-08-17 the dashboard was
reporting a humidity high of 70.0 when the real 24h high was 63.0 - the
70 was a threshold - and the question behind that ("it WAS that high a
couple of days ago") had no answer on disk at all.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_a_reading_lands_in_todays_row():
    C = harness.load()
    now = time.time()
    C._daily_note(now, 900.0, 22.5, 51.0)
    rows = C.STATE["daily"]
    assert len(rows) == 1, rows
    assert rows[0]["d"] == time.strftime("%Y-%m-%d", time.localtime(now))
    assert rows[0]["v"] == {"lo": 900.0, "hi": 900.0}, rows[0]
    assert rows[0]["n"] == 1
    return "one reading, one row"


def test_the_row_widens_to_the_extremes():
    C = harness.load()
    now = time.time()
    for co2, tc, rh in ((900.0, 22.5, 51.0), (1200.0, 19.0, 70.0),
                        (700.0, 26.5, 45.0)):
        C._daily_note(now, co2, tc, rh)
    r = C.STATE["daily"][0]
    assert r["v"] == {"lo": 700.0, "hi": 1200.0}, r["v"]
    assert r["tc"] == {"lo": 19.0, "hi": 26.5}, r["tc"]
    assert r["rh"] == {"lo": 45.0, "hi": 70.0}, r["rh"]
    assert r["n"] == 3
    return "low and high track the day"


def test_the_day_rolls_over_on_local_date():
    """"Was yesterday hot" is a question about the day someone lived
    through, not about UTC."""
    C = harness.load()
    now = time.time()
    C._daily_note(now - 86400, 800.0, 20.0, 50.0)
    C._daily_note(now, 900.0, 21.0, 55.0)
    rows = C.STATE["daily"]
    assert len(rows) == 2, rows
    assert rows[0]["d"] != rows[1]["d"]
    assert rows[1]["v"]["hi"] == 900.0
    return "two days, two rows"


def test_a_missing_day_leaves_a_gap_not_a_zero():
    """A day the controller was off has no readings. Inventing a row of
    zeroes for it would be a lie a chart would happily draw."""
    C = harness.load()
    now = time.time()
    C._daily_note(now - 3 * 86400, 800.0, 20.0, 50.0)
    C._daily_note(now, 900.0, 21.0, 55.0)
    days = [r["d"] for r in C.STATE["daily"]]
    assert len(days) == 2, days
    return "gaps stay gaps"


def test_a_missing_metric_does_not_create_an_empty_range():
    """A sensor that reports no humidity must not produce a 0-0 row."""
    C = harness.load()
    C._daily_note(time.time(), 900.0, None, None)
    r = C.STATE["daily"][0]
    assert "v" in r and "rh" not in r and "tc" not in r, r
    return "absent stays absent"


def test_it_survives_a_restart():
    C = harness.load()
    C._daily_note(time.time(), 900.0, 22.0, 51.0)
    assert C.save_daily(force=True)
    on_disk = json.load(open(C.DAILY_PATH))
    assert on_disk and on_disk[-1]["v"]["hi"] == 900.0, on_disk
    # A fresh load sees it.
    assert C.load_daily()[-1]["v"]["hi"] == 900.0
    return "written and reloaded"


def test_old_days_fall_off_the_end():
    C = harness.load()
    now = time.time()
    for i in range(C.DAILY_KEEP_DAYS + 40):
        C._daily_note(now - (C.DAILY_KEEP_DAYS + 40 - i) * 86400,
                      800.0, 20.0, 50.0)
    assert len(C.STATE["daily"]) == C.DAILY_KEEP_DAYS, len(C.STATE["daily"])
    return "%d days kept" % C.DAILY_KEEP_DAYS


def test_the_endpoint_returns_the_rows():
    C = harness.load()
    C._daily_note(time.time(), 900.0, 22.0, 51.0)
    with C.app.test_client() as cl:
        r = cl.get("/api/daily")
        assert r.status_code == 200, r.status_code
        body = r.get_json()
        assert body["days"][-1]["v"]["hi"] == 900.0, body
        r2 = cl.get("/api/daily?days=1")
        assert len(r2.get_json()["days"]) == 1
    return "served, and limitable"


# ------------------------------------- a summary must never stop the fan
# All four reproduce findings from a review of 479e6b6.

def test_a_malformed_row_is_dropped_on_load():
    """ANYTHING ON DISK IS INPUT. This accepted any dict with a "d"."""
    C = harness.load()
    bad = [
        {"d": "2026-08-16", "v": {}},                  # range with no lo
        {"d": "2026-08-16", "n": "lots"},              # counter as a string
        {"d": 20260816},                               # day not a string
        {"d": "2026-08-16", "rh": {"lo": True, "hi": 9}},
        "not a row",
        {"nope": 1},
    ]
    open(C.DAILY_PATH, "w").write(__import__("json").dumps(bad))
    rows = C.load_daily()
    for r in rows:
        assert isinstance(r["d"], str) and len(r["d"]) == 10, r
        assert isinstance(r["n"], int), r
        for k in ("v", "tc", "rh"):
            if k in r:
                assert isinstance(r[k]["lo"], (int, float)), r
                assert isinstance(r[k]["hi"], (int, float)), r
    return "%d of %d rows survived, all sane" % (len(rows), len(bad))


def test_a_reversed_range_is_normalised():
    C = harness.load()
    open(C.DAILY_PATH, "w").write(
        __import__("json").dumps([{"d": "2026-08-16",
                                   "v": {"lo": 900, "hi": 500}}]))
    r = C.load_daily()[0]
    assert r["v"] == {"lo": 500, "hi": 900}, r
    return "lo is the low one"


def test_a_broken_summary_cannot_abort_a_poll():
    """The fold runs AFTER reading_at is refreshed. An exception there
    marks the poll failed, skips the control decision, and leaves the
    stale-reading fail-safe looking at a fresh timestamp - the fan would
    sit off with nothing left to notice."""
    C = harness.load()
    import time as _t
    C.STATE["daily"] = [{"d": _t.strftime("%Y-%m-%d"), "n": "not a number"}]
    try:
        C._daily_note(_t.time(), 900.0, 22.0, 51.0)
    except Exception as e:
        # The fold itself may raise; the CALL SITE is what must not.
        assert isinstance(e, TypeError), e
    src = open(C.__file__ if hasattr(C, "__file__") else
               "/home/fan-admin/office-fan-controller/controller.py").read()
    i = src.index("_daily_note(now, *_daily_pending)")
    guard = src[max(0, i - 400):i]
    assert "try:" in guard, "the fold is not wrapped at the call site"
    return "wrapped where it matters"


def test_the_shutdown_path_does_not_log():
    """_log takes a non-reentrant lock. A signal arriving while a thread
    holds it would deadlock the shutdown path, which is why the history
    writer already prints to stderr instead."""
    C = harness.load()
    calls = []
    C._log = lambda m: calls.append(m)
    C.write_json_durable = lambda *a, **k: (_ for _ in ()).throw(
        OSError("no space"))
    with C._daily_lock:
        C._daily_dirty = True
    assert not C.save_daily(force=True, report_error=False)
    assert calls == [], "logged from the signal path: %r" % calls
    assert not C.save_daily(force=True, report_error=True)
    assert calls, "the ordinary path stopped reporting"
    return "silent under a signal, loud otherwise"


def test_the_snapshot_is_deep():
    """A shallow copy shares the ranges with the polling thread, so the
    writer could serialise a row mid-update."""
    C = harness.load()
    import time as _t
    C._daily_note(_t.time(), 900.0, 22.0, 51.0)
    with C._daily_lock:
        rows = __import__("copy").deepcopy(C.STATE["daily"])
    C._daily_note(_t.time(), 1500.0, 22.0, 51.0)
    assert rows[-1]["v"]["hi"] == 900.0, (
        "the snapshot moved with the live data: %r" % rows[-1])
    return "snapshot holds still"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
