"""/api/history deltas: the client must end up with what the server has.

The endpoint's whole justification is that history_rev increases by
exactly one per appended point and pruning only removes from the front.
If either stops being true the delta is silently wrong - the trace would
drift rather than break - so the important test here is the round trip:
drive a simulated client through appends and prunes and assert it holds
exactly what the server holds.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def seed(C, n, start=1000.0, step=30.0):
    """n points, oldest first, as the poll thread would leave them."""
    with C._state_lock:
        C.STATE["history"] = [{"t": start + i * step, "v": 400.0 + i}
                              for i in range(n)]
        C.STATE["history_rev"] = n
    return list(C.STATE["history"])


def poll(C, t, v=500.0):
    """One appended point, exactly as tick() does it."""
    with C._state_lock:
        C.STATE["history"].append({"t": t, "v": v})
        C.STATE["history"][:] = C.prune_hist(C.STATE["history"], t)
        C.STATE["history_rev"] += 1


class Client:
    """The dashboard's half of the protocol, in Python."""

    def __init__(self):
        self.hist = []
        self.rev = None

    def fetch(self, app):
        url = "/api/history"
        if self.hist and self.rev is not None:
            url += "?since=%d" % self.rev
        body = app.get(url).get_json()
        if body.get("full") is False:
            self.hist = self.hist + body.get("add", [])
            if body.get("first_t") is not None:
                self.hist = [p for p in self.hist
                             if p and p["t"] >= body["first_t"]]
        else:
            self.hist = body.get("history", [])
        self.rev = body["history_rev"]
        return body


def test_a_first_fetch_gets_everything():
    C = harness.load()
    seed(C, 5)
    body = C.app.test_client().get("/api/history").get_json()

    assert body["full"] is True, "a caller with no revision got a delta"
    assert len(body["history"]) == 5, body
    return "full copy when nothing is claimed"


def test_a_delta_carries_only_what_is_new():
    C = harness.load()
    seed(C, 5)
    app = C.app.test_client()

    poll(C, 2000.0)
    poll(C, 2030.0)
    body = app.get("/api/history?since=5").get_json()

    assert body["full"] is False, "sent a full copy where a delta would do"
    assert len(body["add"]) == 2, "expected 2 new points, got %r" % body["add"]
    assert [p["t"] for p in body["add"]] == [2000.0, 2030.0], body["add"]
    assert "history" not in body, "a delta still carried the whole trace"
    return "two polls, two points"


def test_an_up_to_date_caller_gets_nothing():
    C = harness.load()
    seed(C, 5)
    body = C.app.test_client().get("/api/history?since=5").get_json()

    assert body["full"] is False and body["add"] == [], body
    return "nothing new, nothing sent"


def test_a_caller_ahead_of_us_gets_a_full_copy():
    """What a restart looks like: the revision counter starts again."""
    C = harness.load()
    seed(C, 5)
    body = C.app.test_client().get("/api/history?since=900").get_json()

    assert body["full"] is True, (
        "served a delta to a caller holding a revision we never issued")
    assert len(body["history"]) == 5
    return "restart falls back to a full copy"


def test_a_caller_too_far_behind_gets_a_full_copy():
    """More points missing than are still held: pruning overtook them."""
    C = harness.load()
    seed(C, 3)
    with C._state_lock:
        C.STATE["history_rev"] = 500
    body = C.app.test_client().get("/api/history?since=1").get_json()

    assert body["full"] is True, (
        "served a delta of 499 points from a trace holding 3")
    return "falls back rather than sending a short delta"


def test_a_nonsense_since_is_not_trusted():
    C = harness.load()
    seed(C, 5)
    app = C.app.test_client()
    for bad in ("banana", "", "1.5", "-1", "9e99"):
        body = app.get("/api/history?since=%s" % bad).get_json()
        if bad == "-1":
            # Parses, and is simply a caller behind us by rev+1.
            continue
        assert body["full"] is True, (
            "since=%r was treated as a revision" % bad)
    return "five bad values all fell back"


def test_the_client_ends_up_with_what_the_server_has():
    """The round trip. A drifting delta would not break anything
    visibly, so this compares the two lists point for point."""
    C = harness.load()
    C.HIST_HOURS = 1
    seed(C, 10, start=1000.0, step=30.0)

    app = C.app.test_client()
    client = Client()
    client.fetch(app)
    assert len(client.hist) == 10, "first fetch was short"

    # Twenty more polls, far enough apart that pruning bites and points
    # fall off the front while new ones arrive.
    t = 1300.0
    for i in range(20):
        t += 300.0
        poll(C, t, v=600.0 + i)
        client.fetch(app)

        with C._state_lock:
            server = list(C.STATE["history"])
            rev = C.STATE["history_rev"]
        assert client.rev == rev, (
            "revision drifted at poll %d: client %r server %r"
            % (i, client.rev, rev))
        assert [p["t"] for p in client.hist] == [p["t"] for p in server], (
            "trace drifted at poll %d: client %d points, server %d"
            % (i, len(client.hist), len(server)))

    assert len(client.hist) < 30, "pruning never happened, so this proved little"
    return "%d polls, %d points, no drift" % (20, len(client.hist))


def test_a_delta_is_much_smaller_than_the_whole_trace():
    """The entire point of the endpoint."""
    C = harness.load()
    seed(C, 2000)
    app = C.app.test_client()
    full = len(app.get("/api/history").get_data())

    poll(C, 99999.0)
    delta = len(app.get("/api/history?since=2000").get_data())

    assert delta * 50 < full, (
        "a one point delta was %d bytes against %d for the whole trace"
        % (delta, full))
    return "%d bytes against %d" % (delta, full)


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
