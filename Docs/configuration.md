# Configuration

Settings are stored in `config.json` (mode 0600, never committed). Copy
`config.example.json` to start. Everything below is editable in the web
UI except `ui.password_hash`, which is set through the API - see the
bottom of this file.

Defaults below are what `load_cfg` fills in for a key that is absent, so
they are also what an older `config.json` reads as after an upgrade.
`config.example.json` is a starting point rather than a copy of them and
differs in a few places on purpose.

## control

| Key | Default | Meaning |
|---|---|---|
| `mode` | `auto` | auto / manual / on |
| `on_at` | 1000 | CO2 ppm that starts the fan |
| `off_at` | 800 | CO2 ppm that releases it |
| `poll_seconds` | 30 | Seconds between readings |
| `min_run_minutes` | 5 | Minimum run once started |
| `co2_enabled` | true | Use CO2 as a trigger. Absent in configs written before this existed, and read as true |
| `temp_enabled` | false | Use temperature as a trigger |
| `temp_on` | 26.0 | Degrees C that starts the fan |
| `temp_off` | 24.5 | Degrees C that releases it |
| `humidity_enabled` | false | Use humidity as a trigger |
| `humidity_on` | 70.0 | Percent RH that starts the fan |
| `humidity_off` | 60.0 | Percent RH that releases it |
| `stale_minutes` | 0 | Minutes without a reading before the fan is turned on as a fail-safe; 0 disables it, **and disables the readings-stopped email with it** |

Every rule's on level must be above its off level - `on_at` over
`off_at`, `temp_on` over `temp_off`, `humidity_on` over `humidity_off`.
The API rejects a save otherwise, since equal or inverted values leave
the fan running permanently.

`on_at` and `off_at` are checked on every save whether or not
`co2_enabled` is set; temperature and humidity are checked only when
their own trigger is enabled. CO2 is the rule that defaults on and the
one an upgraded config inherits, so its levels are never allowed to be
nonsense even while it is switched off.

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

## email

Alerts by email when something needs a person. Its own section, and
`enabled` defaults to false so an unconfigured install sends nothing.

In the dashboard this is the **Fault Handling** card. The config key
stays `email` deliberately: renaming it would invalidate every existing
`config.json` for a cosmetic gain. The card is named for what it is
about - something going wrong - and the key for how it is delivered.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | Master switch. False sends nothing whatever else is set |
| `host` | "" | Relay hostname. Changing it clears the stored password |
| `port` | 25 | Relay port |
| `username` | "" | Blank if the relay needs no login |
| `password` | "" | Returned by the API as `SET`, never echoed back |
| `sender` | "" | From address |
| `recipients` | "" | One or more addresses, separated by commas or spaces |

### What sends

Each on the TRANSITION into the state, and once only. Every alert
carries a **fault code** in its subject, its body and the matching
activity log line, so one search finds an episode across `events.json`
and a mailbox.

| Code | Sends when |
|---|---|
| `FC-001` | Readings stop for long enough to trigger the fail-safe |
| `FC-002` | A draw fault is raised and the plug is not answering |
| `FC-003` | A draw fault is raised below the band, including nothing drawing at all |
| `FC-004` | A draw fault is raised above the band |
| `FC-005` | A fault could not be written to disk (`fault_unsaved`) |
| `FC-006` | A fault is still standing after a restart |
| `FC-100` | Readings return and the fail-safe stands down |
| `FC-101` | A hard fault is acknowledged and the fan is available again |
| `FC-102` | A fault that could not be written has now been saved by the retry |
| `FC-300` | The plug changed state without this service commanding it - **logged only, never emailed** |

**`0xx` are alarms and `1xx` are all-clears**, so a mail rule can
separate them without parsing subjects.

**`2xx` is something failed and nobody was emailed.** Operational
errors that reach the activity log and stop there. The distinction from
`0xx` is the point of the separate series: an `FC-0xx` means a message
was sent and someone may already be reading it, while an `FC-2xx` means
the log is the only place it exists.

| Code | Subsystem |
|---|---|
| `FC-200` | Unclassified - the poll loop caught something unexpected |
| `FC-201` | Protect unreachable, and its recovery |
| `FC-202` | The plug will not read or will not obey |
| `FC-203` | An alert could not be sent |
| `FC-204` | A write did not land |
| `FC-205` | A restart was refused, or came back degraded |
| `FC-206` | Startup conditions, DNS |
| `FC-207` | **The fail-safe itself failed** - the fan was not ventilated when it should have been |

Grouped by subsystem rather than one code per message: fifteen log
lines share these, because "which subsystem is unhappy" is the question
people actually ask. `FC-207` is separate from the rest because the
others describe something not working, while that one describes
protection not happening.

There is no `2xx` all-clear series. Recovery carries the code of the
failure it ends - `FC-201 Poll recovered` - because `1xx` exists so a
mail rule can split alarms from clears, and nothing in `2xx` is mailed.

**`3xx` is worth knowing, nothing failed.** It sends no mail and appears
only in the activity log. `FC-300` is there because "how often did
something else switch the plug" is a question worth being able to ask
months later - usually it is a person at the switch, but it is also what
a second controller running against the same plug looks like.

Normal operation stays uncoded. A manual stop and the owed stop after a
restart are the system doing what it was told, and coding those would
make a code stop meaning that something happened.

**Codes are stable and are never reused.** Wording changes; `FC-003`
does not. A retired code stays retired, because recycling a number
silently merges two different faults in an archive that may go back
years.

`FC-002`, `FC-003` and `FC-004` are one fault in the code with three
labels. The distinction is drawn from the reading at the moment the
fault raises: no reading at all is a plug problem, anything else is
judged against the live band. They close with a single `FC-101`,
which names which one it was.

The last one exists because **restarting does not clear a draw fault** -
it is reloaded from `fault.json` on purpose. Someone restarting the
service was quite possibly trying to clear one, and should be told it
did not work. It is rate limited to one an hour, because the unit is
`Restart=on-failure` with `RestartSec=10` and systemd's start limit does
not catch a loop spaced that widely.

The fault carries two timestamps for this. `attempted_at` records that a
send was tried; **`delivered_at` records that the relay accepted it, and
only `delivered_at` suppresses the next notice.** Rate limiting on the
attempt would mean a failed send bought an hour of silence at the moment
mail was already known to be broken.

Delivery is recorded against the fault's `at` value, which never
changes. A callback arriving after the fault has been acknowledged, or
after a different fault has replaced it, does nothing - otherwise it
would write a cleared fault back to disk, or stamp the wrong fault as
already reported.

### What does not send

Mode changes, threshold edits and normal switching. They are in the
activity log and nobody needs them at 2am.

Acknowledging a fault **does** now send, as `FC-101`. This reverses an
earlier decision, and the reasoning that changed it is the one this
paragraph used to end on: the person pressing the button learns
nothing, but **everyone else on the recipient list still believes the
fan is stopped**. The message is also what closes an episode in the
archive, carrying the code it cleared, when it was raised and how long
it stood.

`FC-101` and `FC-102` are **gated on delivery of the original alert,
not on the attempt**, the same rule `FC-100` follows. An all-clear for
a warning nobody received implies a first message that went missing.

### Three deliberate silences

These are choices, not oversights:

1. **`stale_minutes = 0` disables the fail-safe AND the readings-stopped
   email.** One number, two effects.
2. **The fail-safe only runs in Auto.** In Force on the fan runs
   regardless, and in Manual a person is driving, so a dead sensor
   changes nothing. The alert earns its place only when automation is
   relying on the reading.
3. **A restart within an hour of a fault being raised sends no restart
   notice.** Whoever restarted has almost certainly just read the
   original alert.

### The draw fault message

It says what the reading was and what that implies: no reading at all
means the plug stopped answering rather than the fan failing; about 0 W
means the fan did not start; below the band means it is running weakly;
above the band means strain, an obstruction, **or a draw band still set
for a fan that has since been replaced.**

The band is read live from `plug` at the moment the message is
composed. It is not copied onto the fault and not parsed back out of
the fault text, because the dashboard can change it and a copy would
drift.

### Sending

There is a **Send test email** button beside the settings. Unlike the
alerts, it sends synchronously and reports the actual error, because a
mail path that has never been tested is the kind that fails exactly
when it is needed.

Authentication is chosen explicitly, **not** left to `smtplib.login()`.

`login()` looks like it picks the strongest mechanism on offer. It does
not: it iterates CRAM-MD5, PLAIN, LOGIN, catching
`SMTPAuthenticationError` and continuing. A CRAM-MD5 attempt that fails
for any reason - a mistyped password included - silently retries as
PLAIN, which on an unencrypted link puts the password on the wire
base64-encoded.

The policy instead is:

- **STARTTLS when the relay offers it**, with a verified context, then
  ordinary authentication. The context matters: `starttls()` with no
  argument builds an unverified one, which stops a passive listener and
  does nothing about a relay impersonating yours.
- **Otherwise CRAM-MD5 only**, called directly, so a failure is a
  failure rather than a downgrade.
- **Otherwise refuse**, and log why. A refused send is a visible
  failure. A downgraded one is a leaked credential that looks like
  success.

If authentication fails, find out why rather than reaching for a
fallback.

Alerts never block the control path: they are dispatched on a throwaway
thread and a failure is logged and dropped. The fan being stopped
matters more than the mail going out.

**Delivery is recorded only once the relay has accepted the message.**
Marking an alert as sent when it was merely attempted causes two
specific faults, both of which existed here and were caught in review: a
failed stale warning still armed the all-clear, so someone could be told
the sensor had recovered without ever being told it had stopped; and a
failed restart notice still bought an hour of silence, suppressing the
next one at the moment mail was already known to be broken. Undelivered
alerts are retried on the next cycle.

## ui

`password_hash` holds a salted PBKDF2-SHA256 hash; the plaintext is never
stored and the API returns only `SET`. Set it with:

    curl -X POST https://<your-host>/api/ui/password \
      -H 'Content-Type: application/json' -d '{"password":"your-password"}'

While it is empty there is no authentication at all. Setting it protects
everything that changes state or exposes internals, while leaving the
dashboard readable without credentials, so a wall display needs no
login. `POST /api/clienterror` is the one deliberate exception: it stays
open so a page that cannot load its own script can still report why, and
it writes to the journal and the activity log. Body size and repeat
logging are capped in the application; rate limiting it is nginx's job,
and `deploy/nginx.conf.example` does it.

Open without a password: `GET /`, `GET /api/state`, `GET /api/history`,
`GET /static/*` and `POST /api/clienterror`. Everything else requires
it, including
`/api/debug/sensor`, which returns the raw Protect payload. Icons are open
for the same reason the dashboard is: a wall display would otherwise show a
broken tab icon with no way to authenticate.

Browsers do not reliably raise the Basic auth dialog for a fetch, so the
UI opens `/signin` when a save is refused. Authenticate there once and the
browser caches the credentials. Post an empty password to disable auth.

nginx receives Basic auth over TLS and forwards the `Authorization`
header to Flask over loopback, so the credentials do cross one plain
HTTP hop - on the machine itself. They never leave the host and never
travel the network unencrypted, which is the property that matters.
Flask binds `127.0.0.1`, so port 8096 is not reachable from the network
at all. Treat the password as access control for a trusted network, not
as a hard security boundary.
