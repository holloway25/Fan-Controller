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
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_overlapping_flushes_cannot_overwrite_newer_daily_samples():
    C = harness.load()
    now = time.time()
    C._daily_note(now, 1000, 20, 40)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    real = C.write_json_durable
    calls, results = [], []
    def delayed(path, data):
        calls.append(data[0]['n'])
        if data[0]['n'] == 1:
            entered.set()
            assert release.wait(5)
        return real(path, data)
    C.write_json_durable = delayed
    first = threading.Thread(target=lambda: results.append(C.save_daily(force=True)))
    def flush_second():
        second_started.set()
        results.append(C.save_daily(force=True))
    second = threading.Thread(target=flush_second)
    first.start()
    try:
        assert entered.wait(5)
        C._daily_note(now, 2000, 25, 50)
        second.start()
        assert second_started.wait(5)
        # No second snapshot may reach disk while the first write is pending.
        second.join(0.1)
        assert calls == [1]
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results == [True, True] and calls == [1, 2]
    rows = C.load_daily()
    assert rows[0]['n'] == 2 and rows[0]['v']['hi'] == 2000
    assert not C._daily_dirty


def test_backward_date_updates_existing_row_and_survives_reload():
    C = harness.load()
    now = time.time()
    C._daily_note(now - 86400, 700, 19, 40)
    C._daily_note(now, 1000, 25, 60)
    C._daily_note(now - 86400, 900, 22, 50)
    rows = C.STATE['daily']
    assert len(rows) == 2 and [r['d'] for r in rows] == sorted(r['d'] for r in rows)
    assert rows[0]['n'] == 2 and rows[0]['v'] == {'lo': 700, 'hi': 900}
    assert rows[0]['tc'] == {'lo': 19, 'hi': 22}
    assert C.save_daily(force=True)
    assert C.load_daily() == rows


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
    """DRIVEN THROUGH tick(), not by reading the source.

    The first version searched controller.py for a "try:" near the call
    - which passes on a file that happens to contain the right
    characters and says nothing about behaviour. It now makes the fold
    raise and asserts the control decision still ran. Reported by
    review of 099b249.

    The position is what makes this matter: the fold runs after
    reading_at is refreshed, so an escape marks the poll failed, skips
    the decision, and leaves the stale-reading fail-safe looking at a
    fresh clock. The fan would sit off with nothing left to notice.
    """
    C = harness.load()
    import time as _t

    decided = []
    real_decide = C.decide

    def watched(*a, **k):
        decided.append(True)
        return real_decide(*a, **k)

    C.decide = watched

    def explode(*a, **k):
        raise RuntimeError("summary is broken")

    C._daily_note = explode
    C.CFG["control"].update(mode="auto", stale_minutes=0)

    class Plug(object):
        def state(self):
            return False

        def set(self, on):
            return None

        def power_w(self):
            return None

    class Protect(object):
        def readings(self, sid):
            return {"co2": 700.0, "temp": 21.0, "humidity": 50.0}, "fake"

    C.CFG["protect"]["sensor_id"] = "fake-sensor-id"
    C._client_snapshot = lambda: (Protect(), Plug(), C._cfg_generation)

    before = _t.time()
    C.tick()          # must not raise

    assert decided, "the control decision was skipped"
    with C._state_lock:
        assert C.STATE["reading_at"] >= before, "the reading was discarded"
        assert C.STATE["error"] is None, (
            "the poll was marked failed: %r" % C.STATE["error"])
    assert C.STATE["daily"] == [], "the broken summary was not discarded"
    return "the poll survives a broken summary"


def test_the_reset_marks_the_file_for_writing():
    """The recovery branch assigned _daily_dirty without declaring it
    global, so Python made a local and the reset was never persisted."""
    C = harness.load()
    import time as _t
    C._daily_note(_t.time(), 900.0, 22.0, 51.0)
    C.save_daily(force=True)
    with C._daily_lock:
        C._daily_dirty = False
    C.reset_daily()
    assert C._daily_dirty is True, "the reset was not marked for writing"
    assert C.STATE["daily"] == []
    return "reset is remembered"


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


def test_the_writer_snapshot_is_deep():
    """THROUGH save_daily, not through a deepcopy of my own.

    The first version of this test called copy.deepcopy itself and
    asserted the copy held still - which it always would. It would have
    passed with the production code reverted to a shallow copy, which
    is the entire bug it was written for. Reported by review of
    099b249.
    """
    C = harness.load()
    import time as _t
    C._daily_note(_t.time(), 900.0, 22.0, 51.0)

    # Mutate the live rows DURING the write, as a poll would.
    real = C.write_json_durable
    seen = {}

    def slow_write(path, data, **kw):
        C._daily_note(_t.time(), 1500.0, 22.0, 51.0)   # a poll lands
        seen["hi"] = data[-1]["v"]["hi"]
        return real(path, data, **kw)

    C.write_json_durable = slow_write
    try:
        assert C.save_daily(force=True)
    finally:
        C.write_json_durable = real

    assert seen["hi"] == 900.0, (
        "the writer saw the live rows change under it: %r" % seen)
    return "the writer holds a still snapshot"


def test_the_api_snapshot_is_deep():
    """Same property, through the endpoint."""
    C = harness.load()
    import time as _t
    C._daily_note(_t.time(), 900.0, 22.0, 51.0)
    with C.app.test_client() as cl:
        body = cl.get("/api/daily").get_json()
    C._daily_note(_t.time(), 1500.0, 22.0, 51.0)
    assert body["days"][-1]["v"]["hi"] == 900.0, (
        "the served rows moved with the live data: %r" % body)
    return "the response holds still"


def test_a_non_finite_reading_never_reaches_the_file():
    """NaN IS A FLOAT AND IT POISONS BOTH ENDS.

    json.dumps writes a bare NaN, which is not JSON and which browser
    JSON.parse rejects - so one bad reading takes the whole dashboard
    down rather than one row. And every comparison against NaN is
    false, so once stored as an extreme the range never updates again.
    Reported by review of 099b249.
    """
    import json as _json
    import math as _m
    import time as _t
    C = harness.load()
    C._daily_note(_t.time(), float("nan"), float("inf"), 51.0)
    row = C.STATE["daily"][-1]
    assert "v" not in row and "tc" not in row, row
    assert row["rh"] == {"lo": 51.0, "hi": 51.0}, row
    # And the file it writes is parseable JSON.
    C.save_daily(force=True)
    text = open(C.DAILY_PATH).read()
    assert "NaN" not in text and "Infinity" not in text, text
    _json.loads(text)
    return "non-finite values are dropped"


def test_a_non_finite_value_on_disk_is_dropped_on_load():
    C = harness.load()
    open(C.DAILY_PATH, "w").write(
        '[{"d": "2026-08-16", "v": {"lo": NaN, "hi": NaN}, '
        '"rh": {"lo": 40, "hi": 60}}]')
    rows = C.load_daily()
    assert rows and "v" not in rows[0], rows
    assert rows[0]["rh"] == {"lo": 40, "hi": 60}, rows
    return "poisoned ranges do not survive a reload"


def test_a_day_that_is_not_a_date_is_rejected():
    """"not-a-date" is ten characters, which was the entire check."""
    C = harness.load()
    open(C.DAILY_PATH, "w").write(
        '[{"d": "not-a-date"}, {"d": "2026-13-45"}, {"d": "2026-08-16"}]')
    rows = C.load_daily()
    assert [r["d"] for r in rows] == ["2026-08-16"], rows
    return "only real dates survive"


def test_rows_come_back_oldest_first_and_once_each():
    """The endpoint promises oldest-first and the fold assumes the last
    row is today. A hand-edited file can break both without being
    malformed."""
    C = harness.load()
    open(C.DAILY_PATH, "w").write(
        '[{"d": "2026-08-16", "n": 1}, {"d": "2026-08-14", "n": 2},'
        ' {"d": "2026-08-16", "n": 3}, {"d": "2026-08-15", "n": 4}]')
    rows = C.load_daily()
    days = [r["d"] for r in rows]
    assert days == sorted(days), days
    assert len(days) == len(set(days)), days
    assert rows[-1]["n"] == 3, "the later duplicate did not win: %r" % rows
    return "sorted, deduplicated, latest wins"


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
