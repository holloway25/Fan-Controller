# Configuration

All settings are editable in the web UI and stored in `config.json`
(mode 0600, never committed). Copy `config.example.json` to start.

## control

| Key | Default | Meaning |
|---|---|---|
| `mode` | `auto` | auto / manual / on / off |
| `on_at` | 1000 | CO2 ppm that starts the fan |
| `off_at` | 700 | CO2 ppm that releases it |
| `poll_seconds` | 30 | Seconds between readings |
| `min_run_minutes` | 10 | Minimum run once started |
| `temp_enabled` | false | Use temperature as a second trigger |
| `temp_on` | 26.0 | Degrees C that starts the fan |
| `temp_off` | 24.5 | Degrees C that releases it |

`on_at` must be above `off_at`, and `temp_on` above `temp_off`; the API
rejects a save otherwise, since equal or inverted values leave the fan
running permanently.

## protect

`host` is the UniFi console. `username` / `password` must be a **local**
account without two-factor - a UI Cloud login will not authenticate against
the private API, and a 2FA account returns HTTP 499, which the service
reports explicitly. `sensor_id` is chosen in the UI via Find sensors.

## hue

`bridge` is the bridge IP. `key` is created by pressing the round button on
the bridge and clicking Pair - it is an application key, not a password.
`lights` is a list of light IDs; Hue smart plugs appear as lights.

The client prefers the CLIP v2 API and falls back to v1 automatically.

## Choosing thresholds

CO2 outdoors is roughly 420 ppm. Under 800 indoors is good, 1000+ is where
ventilation is measurably inadequate. A gap of at least 150-200 ppm between
on and off avoids short-cycling; 300 works well if the fan can shift that
much within a reasonable run.
