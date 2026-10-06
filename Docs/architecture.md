# Architecture

The controller is one Flask process managed by systemd.

```text
UniFi Protect --HTTPS--> Fan Controller --HTTP--> Tasmota plug
                         | poll and decide
                         | monitor draw
                         | serve dashboard/API
```

Flask serves the UI and API. A polling thread reads Protect and applies the
control rules. A draw thread reads the plug more frequently, enforces faults,
and reasserts Force on. A restart watcher handles `restart.request`.

## Protect data

Protect's documented sensor endpoint does not provide the required air-quality
metrics. The controller therefore authenticates a local UniFi user against
`/api/auth/login` and reads `/proxy/protect/api/bootstrap`.

This private endpoint may change with firmware. Metrics are discovered by
searching the sensor's `airQuality` subtree for exact known keys after
case/punctuation normalization, rather than relying on one fixed path. Exact
aliases prevent metadata such as a CO2 threshold from being mistaken for a
live reading. Non-finite values and unexpected bootstrap shapes are rejected
at this boundary. The selected paths are exposed for diagnosis.

An explicit `isConnected: false` rejects retained sensor measurements before
they can refresh `reading_at`, so the configured stale-reading fail-safe still
operates. Payloads without connectivity metadata remain supported. Raw sensor
diagnostics remain available even while the sensor is disconnected.

A partial payload is also unhealthy if any enabled trigger lacks its
measurement. Other valid metrics cannot refresh `reading_at` or end that
outage; the existing stale-reading deadline, alert and recovery rules apply.

Protect and plug clients are cached and rebuilt when their relevant settings
change. An expired Protect session is retried after authentication.

## Network boundary

Flask binds only to `127.0.0.1:8096`. nginx on the same host terminates TLS and
proxies over loopback. `ProxyFix` trusts exactly that one proxy hop. Do not
expose Flask directly or bind it to all interfaces without revisiting the
forwarded-header trust model.

Optional HTTP Basic authentication protects settings and diagnostics, not
read-only dashboard data. Non-public API writes also check browser origin
headers even when authentication is off. Headerless JSON scripts remain
supported. See [API access rules](api.md) for the exact boundary; these checks
do not make an unauthenticated installation safe for an untrusted network.

## Configuration generations

A polling or draw cycle can spend seconds waiting for a network request while a
settings update replaces the configured sensor or plug. Each configuration
replacement increments a generation. Client snapshots carry the generation
they were created from.

Before recording data or touching the plug, a cycle rechecks its generation
under the actuation lock. A stale cycle is discarded. This prevents readings
from an old sensor refreshing the current safety timer and prevents new rules
from being applied through a client connected to an old physical plug.

Manual's pending stop is retained when a stale generation is detected so the
next valid cycle still owes the stop.

Unrelated and no-op saves carry the current draw-fault timer into the new
generation without accepting stale clients. Plug or power-monitoring changes
reset that timer, as do transitions into or out of Force on. Email, dashboard,
sensor and automatic-trigger changes cannot continually postpone a draw fault.

## Locks

The important lock order is:

```text
_cfg_lock   -> _act_lock
_cfg_lock   -> _clients_lock
_act_lock   -> _state_lock
_act_lock   -> _fault_lock
_act_lock   -> _plug_request_lock
_fault_lock -> _state_lock
```

No path may acquire `_act_lock` while already holding `_fault_lock`, and no
path may acquire `_cfg_lock` while holding `_act_lock`.

`_act_lock` serialises all plug actions and mode changes. Every action
rechecks both mode and configuration generation while holding it.

`_plug_request_lock` is a counted request gate, not a normal-request mutex.
HTTP requests from different client instances may overlap. Each request
registers while active; restart takes exclusive access, waiting for all active
requests and preventing new ones through handover. The HTTP path never acquires
controller locks, and meter reads unregister before taking `_act_lock` to commit
their result. The gate's internal condition is not held during network I/O.

`_fault_lock` treats the in-memory fault and `fault.json` as one transaction.
Draw evaluation holds `_act_lock` before `_fault_lock` so the configured
power band, fault decision and stored fault remain coherent.

`_cfg_lock` and `_state_lock` are reentrant locks because their helpers may be
nested. Configuration snapshots are copied under `_cfg_lock`. Event and
history writes normally remain outside actuation and fault locks so disk I/O
does not unnecessarily delay a mode change.

## Restart model

`./restart.sh` creates `restart.request`. The watcher verifies that restarting
is safe and replaces the process with `execv`, without changing the PID or
asking systemd to start a second process.

The requested-restart handover preserves latch state, the record of which
rules have latched during the current run, the minimum-run lease, draw-fault
timing, stale-reading timing and the original `START_TIME` used for grace
periods. The API's `process_started_at` is a separate, fresh process-generation
marker used to verify replacement. Draw-fault elapsed time and the active
draw spin-up grace use monotonic clocks, carried through requested restart;
successful power/sensor acquisition ages, startup age and minimum-run leases
also use monotonic clocks. Initial spin-up grace is calculated from elapsed physical-start age,
including before the first draw check. Startup DNS waiting uses a monotonic
deadline; dashboard heartbeat and refresh indicators use elapsed API ages.
Physical switching time is retained when leaving
Force on; only its lease is cancelled. Restart triggers are compared with the
file observed at startup, not with adjustable wall-clock timestamps;
wall timestamps remain available for logs. A crash or systemd restart starts with clean
transient state and logs `FC-301 Started cold`; a saved fault and durable pending
Manual stop are still loaded from disk. A fault-free Auto cold start may hold
an already-ON relay for `cold_start_minutes` (default 15, 0 disables). Only the
first confirmed observation qualifies; it does not turn an OFF relay ON. The
hold uses a monotonic deadline and never delays fault-stop enforcement. Fresh
trigger demand takes over with a new normal minimum-run lease.
The lease uses `min_run_at` separately from physical relay timing (`changed_at`),
so demand handover does not renew motor spin-up grace or reset draw protection.
A preserving restart carries the existing deadline without extending or creating a hold.
Changes to plug, sensor or control settings cancel it; unrelated saves do not.
Events are bounded and best-effort, so the absence
of a start event does not prove no restart occurred.

A requested restart is refused while a live fault has not been saved. It takes
`_cfg_lock`, then `_act_lock`, then `_plug_request_lock`, then `_fault_lock`,
retaining all four through
handover capture and `execv`. This prevents settings, plug actions or fault
transactions from changing the promised state between capture and replacement.
The request lock also freezes meter connections so their recently used ports
cannot be omitted from handover. A busy plug request refuses restart after a
bounded wait; retry once the request has finished.
Lock waits are bounded and refusal leaves the running process in place.
Startup reconciles a durable pending Manual stop even after handover; a carried
false flag cannot erase a committed stop promise.

Before `execv`, inherited file descriptors above standard input/output/error
are closed so the replacement can bind port 8096. A SIGTERM/systemd restart
cannot refuse; it makes one final fault-persistence attempt and reports failure
to the journal.

## Runtime state

| File | Purpose | Persistence |
|---|---|---|
| `config.json` | Settings and credentials | Durable, mode 0600 |
| `history.json` | Last 24 hours of readings | Atomic; flushed periodically and on SIGTERM |
| `daily.json` | Two years of daily low/high values | Durable |
| `events.json` | Activity log, up to 1000 entries | Atomic |
| `fault.json` | Latched draw fault | Durable |
| `manual-off.pending` | Manual stop promised but not confirmed | Durable |

These files are local runtime data and are excluded from Git.

Configuration, faults, pending Manual stops and daily summaries use a temporary
file, file sync, atomic rename and directory sync. History and events use atomic
replacement without a directory sync because a small loss is recoverable and
does not control the fan.

Latch state and timing leases are normally memory-only. Only a requested restart
hands them to the replacement process.

Confirmed switch replies and relay reads establish the fan state. Normal
polling observes changes made at the device or by another controller; safety
confirmation reads do not rely on the background relay cache.

## Tasmota plug

Tasmota provides explicit `Power ON` and `Power OFF` HTTP commands and returns
the relay state for confirmation. Power is read on a separate short cadence so
the controller can distinguish a closed relay from a fan that is actually
drawing current.

The draw loop supplies fallback relay checks when state is unknown/unverified
or normal observations stop, including when Protect fails. Auto and Manual
reuse healthy normal checks and confirmed switch replies for this fallback.
Normal control checks read the relay afresh; Force on also checks every five
seconds independently of sensor acquisition.
This lets power protection judge a fan already running at startup during a
sensor outage. Relay adoption is serialized with settings and actuation,
rejects stale client generations, and shares the polling loop's external-switch
and run-record handling. Startup observation does not invent a new run lease.

Tasmota sends web credentials in the `/cm` query string. Request URLs and
exception text that could contain them must never be logged.
