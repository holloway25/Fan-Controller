# Configuration

All settings are editable in the web UI and stored in `config.json`
(mode 0600, never committed). Copy `config.example.json` to start.

## control

| Key | Default | Meaning |
|---|---|---|
| `mode` | `auto` | auto / manual / on |
| `on_at` | 1000 | CO2 ppm that starts the fan |
| `off_at` | 700 | CO2 ppm that releases it |
| `poll_seconds` | 30 | Seconds between readings |
| `min_run_minutes` | 10 | Minimum run once started |
| `temp_enabled` | false | Use temperature as a second trigger |
| `temp_on` | 26.0 | Degrees C that starts the fan |
| `temp_off` | 24.5 | Degrees C that releases it |
| `watts_min` | 15.0 | Expected draw, lower bound. Display only, no control effect |
| `watts_max` | 22.0 | Expected draw, upper bound |
| `stale_minutes` | 0 | Minutes without a reading before the fan is turned on as a fail-safe; 0 disables it |

`on_at` must be above `off_at`, `temp_on` above `temp_off`, and `watts_min`
below `watts_max`; the API rejects a save otherwise, since equal or inverted
values leave the fan running permanently.

## protect

`host` is the UniFi console. Set `verify_tls` only when that address is a
hostname the certificate covers - an IP will not validate. `username` / `password` must be a **local**
account without two-factor - a UI Cloud login will not authenticate against
the private API, and a 2FA account returns HTTP 499, which the service
reports explicitly. `sensor_id` is chosen in the UI via Find sensors.

## plug

`host` is the Tasmota device running the fan. `user` is its web username
(usually `admin`) and `password` its web admin password; leave the
password blank if none is set.

Tasmota accepts credentials only as query parameters on `/cm`, so they
travel in the URL. This service never logs request URLs, but it is a
limitation of the device rather than a choice.

The HTTP command API must be enabled on the plug. If `/cm` returns an
empty reply while the web UI still works, it is disabled - the service
reports this explicitly.

Switching is confirmed from the plug's own reply, and its CSE7766 power
reading is polled every 5 seconds on its own thread, independent of
`poll_seconds`, so a relay that closed without the fan drawing current is
visible rather than silent.

## Choosing thresholds

CO2 outdoors is roughly 420 ppm. Under 800 indoors is good, 1000+ is where
ventilation is measurably inadequate. A gap of at least 150-200 ppm between
on and off avoids short-cycling; 300 works well if the fan can shift that
much within a reasonable run.

## ui

`password_hash` holds a salted PBKDF2-SHA256 hash; the plaintext is never
stored and the API returns only `SET`. Set it with:

    curl -X POST https://<your-host>/api/ui/password \
      -H 'Content-Type: application/json' -d '{"password":"your-password"}'

While it is empty there is no authentication at all. Setting it protects
everything that changes state or exposes internals, while leaving the
dashboard readable without credentials, so a wall display needs no login.

Open without a password: `GET /`, `GET /api/state`, `GET /static/*` and
`POST /api/clienterror`. Everything else requires it, including
`/api/debug/sensor`, which returns the raw Protect payload. Icons are open
for the same reason the dashboard is: a wall display would otherwise show a
broken tab icon with no way to authenticate.

Browsers do not reliably raise the Basic auth dialog for a fetch, so the
UI opens `/signin` when a save is refused. Authenticate there once and the
browser caches the credentials. Post an empty password to disable auth.

Basic auth travels over the TLS connection nginx terminates, so it is not
exposed on the wire. Flask itself binds `127.0.0.1`, so port 8096 is not
reachable from the network at all and credentials never traverse it. Treat
the password as access control for a trusted network, not as a hard
security boundary.
