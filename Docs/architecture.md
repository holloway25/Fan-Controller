# Architecture

A single Flask process run under systemd. One background thread polls
the sensor and switches the fan; the Flask routes serve the UI and settings.

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

## State

- `config.json` - settings, written atomically, mode 0600. Not in git.
- `history.json` - readings from the last 24 hours, pruned by timestamp so
  the window does not change with the poll interval. Written at most every
  5 minutes rather than every poll, and flushed on SIGTERM so a restart
  loses nothing. Not in git.
- `events.json` - the activity log, written on each entry and reloaded
  at startup so switching history survives a restart. 300 kept, the
  most recent 60 returned to the UI each poll. Not in git.
- In-memory only - latch state and current fan state.

Fan state is re-read from the plug every poll rather than trusted
from memory, so switching it from Google Home or the plug itself is
noticed and does not desynchronise the controller.

## Why a Tasmota plug

The fan previously ran from a Hue smart plug via the bridge. The Tasmota
plug is a direct HTTP call with explicit `Power ON` / `Power OFF` rather
than a toggle, and every switch is confirmed from the device's own reply,
so a rejected command cannot be recorded as a success.

It also meters. Draw from the CSE7766 is polled on its own 5 second
cadence rather than with the sensor, since the Protect bootstrap is the
expensive call and tying watts to `poll_seconds` left it up to 30 seconds
stale while the UI refreshed every 5. This
separates "the relay closed" from "the fan is actually running" - a
seized motor or unplugged fan becomes visible rather than silently doing
nothing while CO2 climbs.

Credentials go in the query string because Tasmota's `/cm` accepts no
other form. The URL is never logged, and errors report only the exception
type, since `requests` includes the full URL in its exception text.
