# Architecture

A single Flask process run under systemd, with two background threads.
The poll thread reads the sensor and decides; the draw thread reads the
plug's energy meter on a shorter cadence, enforces a latched fault, and
re-asserts Force on. Both switch the plug, which is why the locking below
matters. The Flask routes serve the UI and settings.

```
UniFi Protect  --HTTPS-->  Fan Controller  --HTTP-->   Tasmota plug
(UP-AirQuality)            (poll, decide)            (relay + power)
```

## Why the private Protect API

Protect 7.1 exposes the sensor through the documented endpoint
`/proxy/protect/integration/v1/sensors`, but that response contains no air
quality data at all - `stats` carries only humidity, light and temperature,
all null. Verified directly against the console.

The readings live on `/proxy/protect/api/bootstrap`, the private endpoint the
Protect web UI itself uses. It needs a UniFi OS session cookie from
`/api/auth/login`, so a local user account is required. An API key returns
HTTP 500 there - it is not a valid credential for that path.

This is undocumented and may change on any firmware update. See
troubleshooting.md.

## Metric discovery

CO2, temperature, humidity and PM2.5 are located by searching the
`airQuality` subtree for a numeric field whose key matches a hint, rather
than by a fixed path, because Ubiquiti has not published the schema. A
rename therefore survives. The path actually used is shown in the UI and
returned as `co2_path`.

The search is scoped to `airQuality` so sibling config blocks such as
`humiditySettings` cannot be mistaken for live readings.

## Session reuse

Protect and plug clients are cached and rebuilt only when the relevant
settings change. An earlier version constructed a new client per poll,
which meant a fresh `/api/auth/login` every 30 seconds and eventually
HTTP 429 rate limiting from UniFi. The 401 retry path handles genuine
session expiry.

## Behind a reverse proxy

The service serves plain HTTP and binds `127.0.0.1` only. nginx runs on the
same host, terminates TLS and proxies to it over loopback, so the whole
deployment is self contained on one machine.

The bind address is the security boundary. ProxyFix trusts a hop count, not
a peer, so on a socket bound to 0.0.0.0 any client could forge
X-Forwarded-For and be believed. Binding loopback removes that: nginx is
the only process that can connect, and it overwrites the header with
$remote_addr rather than appending, so no client-supplied value survives.

ProxyFix is therefore correct at exactly one hop. If another proxy is ever
placed in front, the count must change to match or the logged address
silently reverts to the nearest proxy.

Icons live under /static and are served by Flask directly. nginx also
compresses application/json, which matters most for /api/history - the
trace is the one large payload left, and it is fetched only when the
revision changes rather than on every 5 second poll.

## Threads and locks

Three things can drive the plug: the poll thread through `tick()`, the
draw thread through the fault detector and the Force on re-assertion, and
a settings save that changes the mode. Every actuation therefore follows
one rule:

> take `_act_lock`, re-check the mode, then touch the plug.

The re-check is not defensive padding. Checking the mode before taking the
lock and acting after it is a race: Manual's stop runs exactly once and
then clears its flag, so a Force on that had already passed its check
could land afterwards and leave the fan running in the one mode whose
purpose is to stop touching it.

Settings saves commit the mode under the same lock, so an in-flight switch
completes before a new mode takes effect. A save can therefore block for
as long as the plug takes to answer - bounded by its 12s timeout, and only
when the plug is already misbehaving. A slow save is visible and
recoverable; a fan stuck on in Manual is neither.

The fault is a separate transaction. `_fault_lock` covers the in-memory
value and `fault.json` together, because updating one and then the other
let an acknowledgement land in the gap and be overwritten by a stale
snapshot - clearing the fault on screen while leaving it on disk, to
reappear at the next restart.

```
_act_lock   -> _state_lock      three actuation paths
_fault_lock -> _state_lock      three fault writers
_act_lock and _fault_lock are never held together
```

`_state_lock` is an RLock because nested acquisition previously deadlocked
the poll thread. Nothing takes `_act_lock` or `_fault_lock` while holding
`_state_lock`, and keeping it that way is what makes the ordering above
deadlock-free. Disk writes and event logging stay outside both, so they
cannot extend a hold something else is waiting on.

## State

- `config.json` - settings, written atomically, mode 0600. Not in git.
- `history.json` - readings from the last 24 hours, pruned by timestamp so
  the window does not change with the poll interval. Written at most every
  5 minutes rather than every poll, and flushed on SIGTERM so a restart
  loses nothing. Not in git.
- `events.json` - the activity log, written on each entry and reloaded
  at startup so switching history survives a restart. 300 kept, the
  most recent 30 returned to the UI each poll. Not in git.
- `fault.json` - a latched draw fault. Raised, confirmed and acknowledged
  as one transaction with the in-memory copy, never as two steps. Persisted rather than held in memory because
  a restart would otherwise skip the acknowledgement it is waiting for and
  re-arm a fan already judged faulty. Not in git.
- In-memory only - latch state and current fan state.

Fan state is re-read from the plug every poll rather than trusted
from memory, so switching it from Google Home or the plug itself is
noticed and does not desynchronise the controller.

## Why a Tasmota plug

The fan previously ran from a Hue smart plug via the bridge. The Tasmota
plug is a direct HTTP call with explicit `Power ON` / `Power OFF` rather
than a toggle, and every switch is confirmed from the device's own reply,
so a rejected command cannot be recorded as a success.

It also meters. Draw from the HLW8032 is polled on its own 5 second
cadence rather than with the sensor, since the Protect bootstrap is the
expensive call and tying watts to `poll_seconds` left it up to 30 seconds
stale while the UI refreshed every 5. This
separates "the relay closed" from "the fan is actually running" - a
seized motor or unplugged fan becomes visible rather than silently doing
nothing while CO2 climbs.

Credentials go in the query string because Tasmota's `/cm` accepts no
other form. The URL is never logged, and errors report only the exception
type, since `requests` includes the full URL in its exception text.
