# Architecture

A single Flask process on the dashboard server. One background thread polls
the sensor and switches the fan; the Flask routes serve the UI and settings.

```
UniFi Protect  --HTTPS-->  Fan Controller  --HTTPS-->  Hue bridge
(UP-AirQuality)            (poll, decide)             (light / plug)
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

Protect and Hue clients are cached and rebuilt only when the relevant
settings change. An earlier version constructed a new client per poll,
which meant a fresh `/api/auth/login` every 30 seconds and eventually
HTTP 429 rate limiting from UniFi. The 401 retry path handles genuine
session expiry.

## State

- `config.json` - settings, written atomically, mode 0600. Not in git.
- `history.json` - 24h of readings at 30s, so a restart does not blank
  the charts. Not in git.
- In-memory only - latch state, event log, current fan state.

Fan state is re-read from the Hue bridge every poll rather than trusted
from memory, so switching the light in the Hue app or Google Home is
noticed and does not desynchronise the controller.
