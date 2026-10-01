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

## Locks

The important lock order is:

```text
_cfg_lock   -> _act_lock
_cfg_lock   -> _clients_lock
_act_lock   -> _state_lock
_act_lock   -> _fault_lock
_fault_lock -> _state_lock
```

No path may acquire `_act_lock` while already holding `_fault_lock`, and no
path may acquire `_cfg_lock` while holding `_act_lock`.

`_act_lock` serialises all plug actions and mode changes. Every action
rechecks both mode and configuration generation while holding it.

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
marker used to verify replacement. A crash or systemd restart starts with clean
transient state and logs `FC-301`; a saved fault and durable pending Manual stop
are still loaded from disk. Events are bounded and best-effort, so the absence
of a start event does not prove no restart occurred.

A requested restart is refused while a live fault has not been saved. The fault
lock is held from the final check through `execv`, preventing the replacement
from landing midway through a fault transaction.

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

The relay state is read from the plug instead of trusted from memory, so changes
made at the device or by another controller are visible.

## Tasmota plug

Tasmota provides explicit `Power ON` and `Power OFF` HTTP commands and returns
the relay state for confirmation. Power is read on a separate short cadence so
the controller can distinguish a closed relay from a fan that is actually
drawing current.

Tasmota sends web credentials in the `/cm` query string. Request URLs and
exception text that could contain them must never be logged.
