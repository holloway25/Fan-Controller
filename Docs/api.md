# HTTP API

Served by Flask on 127.0.0.1:8096 and fronted by nginx on the same host.
All responses are JSON except `/` and `/signin`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | open | Web UI |
| GET | `/api/state` | open | Readings, fan state, latches, events, config, `history_rev` |
| GET | `/api/history` | open | The 24h trace, with the `history_rev` it was taken at |
| POST | `/api/clienterror` | open | Browser JS errors, logged server side |
| GET | `/static/*` | open | Icons: browser tab and iOS home screen |
| GET | `/signin` | required | Empty page whose only job is to raise the browser auth prompt |
| POST | `/api/config` | required | Partial settings update; validates, then commits |
| POST | `/api/protect/sensors` | required | Adopted sensors with their current CO2 value |
| POST | `/api/plug/test` | required | Check the plug answers; returns relay state and watts |
| POST | `/api/ui/password` | required | Set or clear the UI password |
| GET | `/api/debug/sensor` | required | Raw Protect payload for the selected sensor |

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

## Why history is a separate endpoint

The trace was ~99% of `/api/state` (188KB of 191KB), and the UI polls that
every 5 seconds while history only changes once per poll interval. Serving
it inline meant re-sending and re-parsing 2500 points to redraw an
unchanged chart, which is real battery cost on a phone left on the
dashboard.

`/api/state` now carries `history_rev`, a counter bumped whenever the trace
is appended to or pruned. The client refetches `/api/history` only when the
value differs from the copy it holds. State dropped to ~2.4KB.

The response repeats `history_rev` so the client stores the revision it
actually received. Storing the one it saw in `/api/state` would lose any
change that landed while the fetch was in flight.
