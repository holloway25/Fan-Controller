# HTTP API

Flask serves HTTP on `127.0.0.1:8096`. nginx should provide external HTTPS.
JSON requests are limited to 8 KB. State-changing endpoints require
`Content-Type: application/json`.

Authentication is optional. When a UI password is configured, endpoints marked
**protected** require HTTP Basic authentication.

An optional `FAN_CONTROLLER_ALLOWED_PROXY_IPS` source allowlist applies before
authentication and forwarded headers to every endpoint. Only loopback GET/HEAD
`/healthz` is exempt. See [proxy restrictions](docker.md#restrict-access-to-a-lan-reverse-proxy).

Non-public API writes also reject browser requests from another origin,
regardless of whether a password is set. `Origin`, or `Referer` when Origin is absent, must
match the request's scheme, host and port; cross-site and same-site fetches
from another origin are rejected. Scripts without browser origin headers
remain supported. Fault acknowledgement requires a valid JSON object (`{}`
is sufficient); rejected requests leave the safety latch intact.

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/` | Open | Dashboard |
| GET | `/healthz` | Open | HTTP process health and `process_started_at`; no fan state/configuration |
| GET | `/api/state` | Open | Current readings, fan state, latches, events, loop health and public configuration |
| GET | `/api/history` | Open | Full 24-hour history or a revision delta |
| GET | `/api/daily` | Open | Daily low/high summaries; `?days=N` limits the result |
| POST | `/api/clienterror` | Open | Log a browser-side error |
| GET | `/static/*` | Open | Static dashboard assets |
| GET | `/favicon.ico` | Open | Conventional browser alias for the dashboard icon |
| GET | `/apple-touch-icon*.png` | Open | Conventional iOS aliases for the dashboard icon |
| GET | `/signin` | Protected | Trigger the browser authentication prompt |
| POST | `/api/config` | Protected | Validate and apply a partial settings update |
| POST | `/api/fault/clear` | Protected | Acknowledge a draw fault |
| POST | `/api/protect/sensors` | Protected | List adopted sensors and current CO2 values |
| POST | `/api/plug/test` | Protected | Read relay state and power from the plug |
| POST | `/api/email/test` | Protected | Send a test email |
| POST | `/api/ui/password` | Protected | Set or clear the UI password |
| GET | `/api/debug/sensor` | Protected | Return the selected sensor's raw Protect payload |

Framework-generated errors may be HTML or plain text rather than JSON.

`/api/state` includes `cold_start_remaining`, the non-negative seconds remaining
in an active cold-start hold (0 otherwise). Its internal monotonic deadline is
not exposed as a wall-clock timestamp. Configure the duration through
`control.cold_start_minutes`; see [configuration](configuration.md#control).
`min_run_at` is an optional minimum-run lease start, separate from the physical
relay-switch time `changed_at`. If null, the normal lease uses `changed_at`.
Cold-start demand handover can renew the lease without resetting draw protection.

Common rejection statuses are 400 for invalid JSON objects or settings, 401
when Basic credentials are required, 403 for a rejected browser origin, and
415 for a missing or incorrect JSON content type. A rejected fault clear does
not acknowledge the fault. See [request troubleshooting](troubleshooting.md#settings-or-fault-clear-request-is-rejected).

## Validation and persistence

Setting `control.mode` to `on` checks the live fault under the actuation and
fault locks. Without a fault it succeeds normally. With an active fault it
returns 409 with `fault_override_required: true` and a `fault_override_token`,
without saving any settings. After the operator explicitly confirms that
override, repeat the request with `X-Fault-Override` set to that token. A token
from another fault episode or process is rejected with a new 409 challenge.
The check remains locked through settings persistence and mode commit.
Confirmation permits Force on; it does not acknowledge or clear the fault.
The token is not authentication and does not replace Basic/origin checks.

Configuration fields are type-checked and range-checked. Unknown keys are
rejected. Validation is all-or-nothing: one invalid field rejects the complete
request.

A failed write returns 500 and leaves the active configuration unchanged. If
the file was replaced but the directory sync could not be confirmed, the API
returns 200 with a `notice`: the setting is active and present on disk, but
power-loss durability was not confirmed. Clients must read the response body.

A mode change may wait for an in-progress plug command to finish. This prevents
a command from the old mode landing after the new mode has been committed.

Once fault acknowledgement has succeeded, an optional notification error
is logged and does not turn the successful clear into a failed response.

`/api/email/test` reports success only when the SMTP relay accepts all
configured recipients. Partial acceptance returns 400 with an explicit
partial-delivery error, not `ok` or a complete `sent_to` list. Some recipients
may already have received the message; repeating the test can send duplicates.
Relay acceptance is not proof of final inbox delivery.
Validation, sending and the `sent_to` response use one email-settings snapshot,
so changing recipients during the send cannot change the reported destination.
Only one unfinished test send is allowed across all clients; repeats return
409 immediately. The request has a 30-second elapsed-time limit including DNS
and returns 504 if delivery is still unconfirmed. That worker retains its slot
until it finishes, so a stalled resolver cannot create an unlimited backlog.
Late DNS completion is checked before sending SMTP commands. A message already
being transmitted at expiry may still be accepted; a timeout is not proof of
non-delivery. A waiting request returns 503 if it can still respond after
shutdown begins, but normal shutdown closes active connections first: clients
may receive a connection error instead of an HTTP response. A worker that
cannot be started returns 503. The dashboard disables its test button until
the current request completes.

`/api/protect/sensors` includes `isConnected` (`true`, `false`, or `null` when
the payload omits it). CO2 values may be retained for disconnected sensors;
they are diagnostic values, not live readings. Discovery labels disconnected
devices separately and excludes them from its reporting-CO2 count. Missing
connectivity metadata remains compatible with older payloads.

## Secrets

`protect.password`, `plug.password`, `email.password` and
`ui.password_hash` are returned as `SET` when configured. Posting `SET`
preserves the stored value. Public error text is redacted before it reaches
`/api/state`.

## Events

`/api/state` also reports elapsed seconds as `sensor_age`, `draw_loop_age`
and `switch_age`, plus `min_run_remaining`. Use these for freshness and
countdowns; wall timestamps are for display, not comparison with a client's
clock. The refresh countdown is `poll_seconds - sensor_age`, reduced by
elapsed local time since receiving the response.
Reading ages start when acquisition finishes, including any later relay I/O or
lock wait before committing the sample. Power expiry uses the same elapsed age.

Each event in `/api/state` includes a server-selected `cls` value:

| `cls` | Meaning |
|---|---|
| `fault` | Alarm or operational failure |
| `ok` | All-clear, normal switch, or a Manual-mode external change |
| `notice` | Amber notice: fault acknowledged (FC-101), fault durably saved (FC-102), or OFF confirmed while the hard fault remains latched (FC-103). It does not mean all faults are cleared. |
| `m-manual` | Manual mode or an unexpected external switch |
| `m-auto` | Auto mode |
| `m-on` | Force on |
| empty | Neutral event |

The value is computed when served and is not stored in `events.json`.

## History

`/api/state` supplies `history_rev` and `history_generation`. A client
fetches history only when either differs from its local copy.

For an initial or recovery fetch:

```text
GET /api/history
```

For an incremental fetch:

```text
GET /api/history?since=REV&gen=GENERATION
```

| Field | Meaning |
|---|---|
| `full` | Whether the response contains the full trace |
| `history` | Full trace; present when `full` is true |
| `add` | Points added since `since`; present for a delta |
| `first_t` | Timestamp of the oldest retained point |
| `history_rev` | Revision represented by this response |
| `history_generation` | History epoch represented by this response |

The server returns a full copy when the revision cannot be reconciled, the
caller has fallen behind pruning, or the history epoch differs. The epoch
changes on process replacement and when a clock rollback inserts a point
before the trace's tail. Full traces are sorted by timestamp, including
restored history. Clients
drop locally held points older than `first_t` before appending a delta.

`process_started_at` in `/api/state` is used by `restart.sh` to verify that
a replacement process answered.

## Daily summaries

`/api/daily` returns up to two years of local-date summaries from
`daily.json`. Each row includes the date, sample count, and available
low/high values for CO2, temperature and humidity. Days or metrics without
readings are omitted rather than represented as zero.

The daily archive is not included in `/api/state`; fetch it separately when
needed.

## Loop health

`/api/state` includes `draw_loop_at`, the last draw-monitor cycle wall timestamp,
`draw_loop_age`, elapsed seconds since that cycle (or this process's startup
until its first heartbeat), and `draw_loop_error`, the latest exception
category or `null`. Clients detect a stalled monitor using `draw_loop_age > 30`;
keep wall timestamps for display, not age comparisons with another device's
clock. These fields describe loop execution, not proof that
the fan or plug is healthy; check fault, power and error fields as well.
