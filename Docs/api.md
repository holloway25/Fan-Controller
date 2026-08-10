# HTTP API

Served by Flask on 127.0.0.1:8096 and fronted by nginx on the same host.
All responses are JSON except `/` and `/signin`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | open | Web UI |
| GET | `/api/state` | open | Readings, fan state, latches, events, config, `history_rev` |
| GET | `/api/history` | open | The 24h trace, or a delta with `?since=REV`, and the `history_rev` it was taken at |
| POST | `/api/clienterror` | open | Browser JS errors, logged server side |
| GET | `/static/*` | open | Icons: browser tab and iOS home screen |
| GET | `/signin` | required | Empty page whose only job is to raise the browser auth prompt |
| POST | `/api/config` | required | Partial settings update; validates, then commits |
| POST | `/api/fault/clear` | required | Acknowledge a draw fault and allow automatic runs again |
| POST | `/api/protect/sensors` | required | Adopted sensors with their current CO2 value |
| POST | `/api/plug/test` | required | Check the plug answers; returns relay state and watts |
| POST | `/api/ui/password` | required | Set or clear the UI password |
| GET | `/api/debug/sensor` | required | Raw Protect payload for the selected sensor |

Every request body is capped at 8KB and a larger one is rejected with
413. Nothing here legitimately posts more than a few hundred bytes, and
`/api/clienterror` stays open even once auth is on, so without a cap one
LAN device could push arbitrary volume into the journal and
`events.json`. Repeated identical client errors are also written to the
activity log at most once a minute, so a script failing in a render loop
cannot evict the 300 entries of real fan history behind it. The journal
still gets every report.

"Auth" applies only once a UI password is set; until then every endpoint
is open. Reading stays open even when auth is on, so a wall display needs
no credentials. `/api/debug/sensor` is protected because it returns the
raw Protect payload, which is diagnostic rather than dashboard data.

## Secrets

`protect.password`, `plug.password` and `ui.password_hash` return the
literal `SET` when present, and a posted `SET` is ignored rather than
written back, so a client can round-trip the config without erasing them.

Error text in `/api/state` passes through a redactor that strips stored
secrets. That endpoint is public and the plug's credentials travel as
query parameters, so an unfiltered connection error would otherwise
publish the password.

## Validation

Every field is coerced and range-checked against a schema before being
stored, and unknown keys are rejected. State-changing endpoints require an
`application/json` content type; a form-encoded post returns 415.

A save is all or nothing. One bad field rejects the whole request with a
400, including sections that were valid, so a post that sets thresholds
and plug limits together changes neither if the thresholds are refused.
Read the response body rather than assuming a 200.

A save that changes the mode waits for any switch already in progress, so
it can block for as long as the plug takes to answer. This is deliberate:
committing a mode midway through an actuation is what let Force on land
after Manual had already stopped the fan. See `architecture.md`.

## Why history is a separate endpoint

The trace was ~99% of `/api/state` (188KB of 191KB), and the UI polls
that every 5 seconds while history changes once per poll interval.
Serving it inline meant re-sending and re-parsing thousands of points to
redraw an unchanged chart - real battery cost on a phone left on the
dashboard. State is now ~2.4KB.

`/api/state` carries `history_rev`, bumped whenever the trace is
appended to, and `history_generation`, identifying the process. The
client refetches only when either differs from the copy it holds.

Responses repeat both, so the client stores what it actually received.
Storing the revision it saw in `/api/state` would lose a change that
landed while the fetch was in flight.

### Deltas

`GET /api/history?since=REV&gen=GENERATION` returns only what the caller
is missing. Measured on the live service: **110 bytes against 189,960**
for one poll's worth of new data.

Both parameters are required for a delta. `gen` is the
`history_generation` from a previous response.

`history_rev` increases by exactly one per appended point, and pruning
only ever removes from the front. So a caller is missing `rev - since`
points and they are the last that many - the single property the whole
endpoint rests on. If either half stops being true the delta goes
silently wrong rather than failing, which is why `test_history.py`
compares a simulated client's list against the server's point for point.

| Field | Meaning |
|---|---|
| `full` | `true` if the whole trace was sent, `false` for a delta |
| `history` | the whole trace. Only present when `full` is `true` |
| `add` | points appended since `since`. Only present when `full` is `false` |
| `first_t` | timestamp of the oldest point still held |
| `history_rev` | the revision this response was taken at |
| `history_generation` | which process answered. Send it back as `gen` |

Why the generation exists: `history_rev` restarts at 1 with the process.
A dashboard left in a background tab can hold revision 100 from a
process that no longer exists, and if the replacement climbs back to 100
the revisions match, nothing refetches, and the next delta is appended
to a trace from the previous process - a silently spliced graph. So the
dashboard compares the generation in `/api/state` to decide whether to
fetch at all, and the server refuses a mismatched delta. Both halves are
needed: refusing a request only helps if one is made.

`/api/state` also carries `process_started_at`, which `restart.sh` uses
to prove a restart happened. Both derive from the same instant but are
formatted differently - a float and a six-decimal string - so compare
each only against its own kind, never one to the other.

Removals are not listed. `first_t` is the oldest point still held, and
the caller drops anything older - which describes a prune of any size,
including one that happened while the caller was away, in one number.

A full copy is sent whenever the delta reasoning does not hold: no
`since`, one that will not parse, a negative one (never issued), a
caller ahead of the server, more points missing than are still held
because pruning has overtaken them, or a `gen` that does not match the
process answering. The caller is told which it received rather than
having to infer it.

