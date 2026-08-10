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
| `co2_enabled` | true | Use CO2 as a trigger. Absent in configs written before this existed, and read as true |
| `temp_enabled` | false | Use temperature as a trigger |
| `temp_on` | 26.0 | Degrees C that starts the fan |
| `temp_off` | 24.5 | Degrees C that releases it |
| `humidity_enabled` | false | Use humidity as a trigger |
| `humidity_on` | 70.0 | Percent RH that starts the fan |
| `humidity_off` | 60.0 | Percent RH that releases it |
| `stale_minutes` | 0 | Minutes without a reading before the fan is turned on as a fail-safe; 0 disables it |

Every rule's on level must be above its off level - `on_at` over
`off_at`, `temp_on` over `temp_off`, `humidity_on` over `humidity_off`.
The API rejects a save otherwise, since equal or inverted values leave
the fan running permanently. Only enabled rules are checked.

Disabling every rule is allowed. Auto then has nothing to act on and
never starts the fan; the save returns a notice saying so, and only the
stale-reading fail-safe can still run it.

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

`watts_min` (15.0) and `watts_max` (25.0) bound the draw the fan is
expected to pull while running. While the fan is running and past its
settling period, the dashboard shows the wattage green inside that band
and red outside it, including when there is no reading at all, since a
plug that stopped answering is the case worth catching. With the fan off
it is neutral: 0 W is expected then, not a fault. `dashboard.md` covers
what each colour means.

Two bounds rather than a rating and a tolerance, because a two-speed fan
has no single correct draw: the TT Mixflow 125 pulls 23 W on low and 37 W
on high, and both are healthy.

`watts_min` must be below `watts_max`, and both at least 1, so that no
valid setting places zero draw inside the healthy band. Otherwise the fan
indicator could read "Fan on, no draw" while the wattage sat green.

Measure the band rather than copying the nameplate. Motors often draw more
than their rating - the Bravo 125 here is rated 16 W and pulls 19-20 W.

`watts_enabled` (false) turns power monitoring into control. While it is on
and the fan runs, a draw outside the band for `watts_fault_seconds` (60)
stops the fan and latches a fault. Off, the wattage is still shown and
coloured but nothing is acted on.

Out of band means anything that is not a fresh reading inside the band:
below `watts_min` (0 W included, which is why the minimum is at least 1),
above `watts_max`, or no reading at all because the plug is unreachable or
its last value is older than 15 seconds.

The time must be continuous. A single reading back inside the band resets
the timer to zero, so a fan flicking briefly out of range never
accumulates toward a fault across an hour. That is the intent: a sustained
wrong draw indicates the device itself - a stalled motor, a seized
bearing, a failed capacitor - and is worth stopping. A momentary
excursion is usually measurement noise, and stopping ventilation over it
would be the worse failure.

The consequence is that an intermittent fault which keeps recovering will
not trip it. Shortening the timer toward its 5 second minimum makes it
sensitive enough that flapping can, at the cost of tripping on transients.

Off by default deliberately: a band that does not match the fan would
otherwise stop ventilation altogether, which is worse than no detection.

Force on overrides a fault - it is how a repaired fan gets tested, so the
detector stands down in that mode rather than fighting it. A fault does
block the `stale_minutes` failsafe: that turns the fan on when readings
stop, and a fan believed broken should not be started blind.

Clearing is manual, from the dashboard or `POST /api/fault/clear`. A timed
retry would switch a seized motor on every few minutes forever.

A fault survives restarts and is enforced continuously, not once. While it
stands, every draw cycle re-checks the plug and switches the fan off again
if something turned it back on, so an external switch or the stale-reading
failsafe cannot leave a faulted fan running.

If the plug does not confirm the stop, the dashboard says "stop
unconfirmed, retrying" rather than claiming the fan was stopped. That is
the state to worry about: faulted, possibly still running, and the plug
not answering to say which.

Tasmota accepts credentials only as query parameters on `/cm`, so they
travel in the URL. This service never logs request URLs, but it is a
limitation of the device rather than a choice.

The HTTP command API must be enabled on the plug. If `/cm` returns an
empty reply while the web UI still works, it is disabled - the service
reports this explicitly.

Switching is confirmed from the plug's own reply, and its HLW8032 power
reading is polled every 5 seconds on its own thread, independent of
`poll_seconds`, so a relay that closed without the fan drawing current is
visible rather than silent.

## Choosing thresholds

CO2 outdoors is roughly 420 ppm. Under 800 indoors is good, 1000+ is where
ventilation is measurably inadequate. A gap of at least 150-200 ppm between
on and off avoids short-cycling; 300 works well if the fan can shift that
much within a reasonable run.

## Notifications

Not implemented. There is no notification configuration, and no section
for it in `config.json`. A fault is visible on the dashboard and in the
activity log and nowhere else.

When it is added it will be its own section rather than more keys under
`control`, and its `enabled` flag will default to false so that an
unconfigured install sends nothing.

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
