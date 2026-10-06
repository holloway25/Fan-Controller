# Configuration

Settings are stored in `config.json`, which must remain mode 0600 and outside
Git. The dashboard edits all normal settings. `ui.password_hash` is managed
through the password endpoint.

Defaults below come from `controller.py` and are applied when a key is absent.
`config.example.json` is a safe starting configuration and intentionally
starts in Manual.

Keep each installation's live settings independent. The example and defaults
are not a profile to copy over an existing device. Follow
[first setup](installation.md#configure-and-verify) before enabling control.

An existing `config.json` is treated as safety-critical input. Invalid JSON,
an unexpected object shape, an unknown key, or an invalid present value stops
the service instead of silently substituting a default. This is deliberate:
replacing a damaged Manual setting with the Auto default could start control
unexpectedly. Check the service journal for the named field, correct the file,
and restart. Missing keys in an otherwise valid older configuration still
receive upgrade defaults.

Startup applies the same relationship checks as a dashboard save. CO2
turn-on must be above turn-off, and expected draw minimum must be below
maximum, even when their respective checks are disabled. Temperature and
humidity bands are checked when their trigger is enabled; reversed bands
may be stored while disabled, but must be corrected before enabling.

## Control

| Key | Default | Valid range or values | Meaning |
|---|---:|---|---|
| `mode` | `auto` | `auto`, `manual`, `on` | Operating mode |
| `on_at` | 1000 | 300–10000 | CO2 ppm that latches on |
| `off_at` | 800 | 300–10000 | CO2 ppm that releases |
| `poll_seconds` | 30 | 5–3600 | Sensor polling interval |
| `min_run_minutes` | 5 | 0–240 | Minimum automatic run |
| `cold_start_minutes` | 15 | 0–240 | Hold an already-ON relay after a fault-free Auto cold start; 0 disables |
| `co2_enabled` | true | Boolean | Enable CO2 trigger |
| `temp_enabled` | false | Boolean | Enable temperature trigger |
| `temp_on` | 26.0 | -20–60 | Temperature latch-on point |
| `temp_off` | 24.5 | -20–60 | Temperature release point |
| `humidity_enabled` | false | Boolean | Enable humidity trigger |
| `humidity_on` | 70.0 | 0–100 | Humidity latch-on point |
| `humidity_off` | 60.0 | 0–100 | Humidity release point |
| `stale_minutes` | 0 | 0–1440 | Auto fail-safe delay; 0 disables it |

Each enabled rule requires its on value to be higher than its off value. CO2
thresholds are always checked because CO2 defaults on for upgraded
configurations. Disabling every trigger is allowed; Auto then relies only on
the stale-reading fail-safe, if enabled.

## Protect

| Key | Default | Meaning |
|---|---|---|
| `host` | `192.0.2.1` | UniFi console hostname or address |
| `username` | empty | Local UniFi username |
| `password` | empty | Local UniFi password |
| `verify_tls` | false | Verify the console certificate |
| `sensor_id` | empty | Sensor selected through **Find sensors** |

Use a local UniFi account without two-factor authentication. Enable TLS
verification only when the configured hostname matches the certificate.

## Plug

| Key | Default | Valid range | Meaning |
|---|---:|---|---|
| `host` | `192.0.2.10` | — | Tasmota hostname or address |
| `user` | `admin` | — | Tasmota web username |
| `password` | empty | — | Tasmota web password |
| `watts_enabled` | false | Boolean | Allow draw monitoring to stop the fan |
| `watts_min` | 15.0 | 1–5000 | Minimum healthy running draw |
| `watts_max` | 25.0 | 1–5000 | Maximum healthy running draw |
| `watts_fault_seconds` | 60 | 5–3600 | Continuous out-of-band time before fault |
| `watts_off_max` | 1 | 0–5000 W | Maximum permitted draw while relay reports OFF |

`watts_min` must be below `watts_max`. Measure the actual fan at its intended
speeds rather than copying its nameplate rating.

Power monitoring is off by default. The dashboard still shows power and its
relationship to the configured band. When monitoring is enabled, a missing,
stale, low or high reading starts the fault timer while the relay is on. A
healthy reading resets the timer.
When the relay reports OFF, fresh readings above `watts_off_max` must persist
for `watts_fault_seconds`, after a five-second stop-settling allowance, to raise
FC-007. The current OFF state is checked again before faulting. Standby draw
at or below the limit is allowed; missing/stale readings cannot establish an
OFF-draw fault. Force on overrides this protection too. Unrelated settings
saves and preserving restarts retain the elapsed fault timer.

With monitoring enabled, a fault stop needs an OFF reply and a fresh meter
reading at or below the OFF limit. Continued or unknown draw leaves the stop
unconfirmed; a previously confirmed stop is invalidated if draw
returns. This checks reported power, not mechanical contact position.
If the initial Manual OFF command fails, FC-009 latches even when running
watts are normal or power monitoring is disabled. The observed relay remains
ON or unknown; this is an unconfirmed stop, not proof of welded contacts.
The original Manual stop promise remains owed, but cannot bypass the retry
budget. Its first OFF retry waits 30 seconds after the failed command.
For FC-007, the first OFF retry starts immediately. In both cases three retries are spaced
30 seconds apart. If power removal remains unconfirmed, FC-008 latches and
sends a hard-fault alert. After a 60-second wait, three final retries are
spaced 30 seconds apart. Failure of the final group sends another hard-fault
alert and ends automatic OFF commands. Read-only monitoring continues.
The first retry sends one warning; confirmed OFF with fresh low draw sends
one FC-103 recovery alert and completes any outstanding Manual stop promise.
Recovery never clears the standing fault: Auto
remains blocked until acknowledgement. Force on overrides stopping without
clearing the fault or renewing the retry budget. Retry counts are saved
before commands; preserving restarts retain elapsed deadlines, while a cold
start retains the count and conservatively waits 30 or 60 seconds before
the next remaining attempt. A storage failure pauses commands until saved.
Other draw faults retain their existing stop-enforcement behaviour.
For meaningful physical detection, verify the plug meters power while OFF:
Tasmota's [SetOption21](https://tasmota.github.io/docs/Commands/) controls this
and is disabled by default. The controller does not change plug firmware or
options. A welded relay cannot be isolated by repeatedly commanding that same
relay; the alert requires independent safe isolation and inspection.
Non-finite plug values such as NaN or infinity are treated as missing readings,
not stored as wattage or exposed as invalid JSON to the dashboard.

A draw fault stops the fan, survives restart, and blocks Auto until cleared.
If an existing `fault.json` is malformed, only partly valid, unreadable, or a
dangling symbolic link, startup creates an FC-206 safety fault in memory rather
than interpreting the damaged path as an acknowledgement. The stop is retried
until confirmed. Force on overrides the fault to permit testing after a repair.

Changing the plug address clears the stored plug password. Re-enter it and test
the plug after an address change. The Tasmota HTTP command API must be enabled.

Tasmota places credentials in the `/cm` query string. The controller redacts
secrets and does not log request URLs.

## Email

Email alerts are configured in the dashboard's **Fault Handling** card and are
disabled by default.

| Key | Default | Valid range | Meaning |
|---|---:|---|---|
| `enabled` | false | Boolean | Master alert switch |
| `host` | empty | — | SMTP relay hostname |
| `port` | 25 | 1–65535 | SMTP relay port |
| `username` | empty | — | Optional SMTP username |
| `password` | empty | — | Optional SMTP password |
| `sender` | empty | — | From address |
| `recipients` | empty | — | Comma- or space-separated recipients |

Changing the SMTP host clears its stored password. Use **Send test email** to
verify DNS, connectivity, authentication and delivery. A test message proves
transport only; it does not exercise the state transitions that generate real
alerts.

### Fault codes

| Code | Meaning |
|---|---|
| `FC-001` | Readings stopped and Auto fail-safe ventilation was attempted or already running |
| `FC-002` | Draw fault raised with no plug reading |
| `FC-003` | Draw fault raised below the configured band |
| `FC-004` | Draw fault raised above the configured band |
| `FC-005` | A fault could not be saved |
| `FC-006` | A saved fault remained after restart |
| `FC-007` | Relay reports OFF but sustained power draw exceeds the OFF limit |
| `FC-008` | OFF retries failed; hard fault, with bounded final attempts |
| `FC-009` | Manual OFF failed; relay remains ON or unknown, with bounded retries |
| `FC-100` | Readings returned after a delivered stale-reading alert |
| `FC-101` | A draw fault was acknowledged |
| `FC-102` | A previously unsaved fault was successfully persisted |
| `FC-103` | OFF and low draw confirmed after a relay-release fault; acknowledgement still required |
| `FC-200` | Unclassified polling failure |
| `FC-201` | Protect communication failure or recovery |
| `FC-202` | Plug communication or command failure |
| `FC-203` | Alert delivery failure |
| `FC-204` | Persistent write failure |
| `FC-205` | Restart refusal or degraded restart |
| `FC-206` | Startup, DNS, or unreadable persisted-fault condition |
| `FC-207` | Stale-reading fail-safe failed to ventilate |
| `FC-208` | Dashboard rendered no data despite server data |
| `FC-300` | Plug state changed outside this controller |
| `FC-301` | The process started cold, carrying no latch and no lease |
| `FC-302` | Already-ON relay given a bounded fault-free Auto cold-start hold |

Fault persistence and restart notices distinguish a requested stop from a
relay confirmed off at the last check. A standing fault alone does not prove
the fan stopped. Force on overrides fault-stop enforcement and is identified
in the notice; a development plug interlock prevents stop commands entirely.

The code series are stable:

- `0xx`: alarm condition or notification category
- `1xx`: recovery or all-clear category
- `2xx`: operational status or failure
- `3xx`: informational event

Codes classify events, not email delivery. An alarm such as `FC-003` can be
logged while email is disabled, unconfigured or rejected by the relay. Do not
treat an activity entry as a delivery receipt; check alert configuration,
delivery failures (`FC-203`) and the mail system separately.

Alerts are sent once when entering the relevant state. All-clear messages send
only when the matching original warning was successfully delivered. Normal
switching, settings changes and mode changes are logged but not emailed.
The sensor-outage warning is still sent if its fail-safe ON command fails.
Its subject/body then say ventilation is unconfirmed, not that the fan is
running, and advise checking the plug/fan. Command errors retain the normal
failure log and retry/backoff. A delivered warning is not repeated every
failed ON attempt; a failed email remains retryable. The later FC-100 reports
sensor readings returning, not proof that an earlier ON command succeeded.

`FC-301` records a start that did not come through `./restart.sh`: a systemd
start, a crash, or a loss of power. Those starts deliberately begin with no
rule latch and no restored minimum-run lease. The compact entry is
`FC-301 Started cold`. A saved fault is named separately and still blocks Auto.
If the first confirmed relay observation is ON in fault-free Auto, the
controller logs `FC-302 Latched on — Fan on (15 min)` (using the configured duration)
and holds it for `cold_start_minutes`. OFF or unknown is not assumed ON.
After the hold, fresh readings decide normally; missing enabled measurements
do not prove demand has cleared. Fresh trigger demand takes over with the
normal minimum-run time. These startup entries are not emailed.

Three intentional alert silences are:

1. `stale_minutes = 0`
2. the controller is not in Auto
3. a restart notice falls inside the fault's rate-limit window

### SMTP security

When the relay advertises STARTTLS, the controller uses it with certificate
verification before authenticating. Without STARTTLS, authenticated delivery
uses CRAM-MD5 only. It refuses PLAIN or LOGIN authentication over an
unencrypted connection.

Alert delivery runs outside the control path. A send is recorded as successful
only when the relay accepts every configured recipient; this is relay
acceptance, not proof that the message reached each inbox. Partial recipient
rejection is reported as partial delivery and does not mark the alert fully
delivered. Accepted recipients may already have received it, and a retry can
send them a duplicate. Failed alerts do not create false all-clears.

## UI authentication

`ui.password_hash` stores a salted PBKDF2-SHA256 hash; plaintext is never
stored. Set or clear it with:

```sh
curl -X POST https://<your-host>/api/ui/password \
  -H 'Content-Type: application/json' \
  -d '{"password":"your-password"}'
```

If authentication is already enabled, include the current Basic credentials
when using this endpoint (for example, add `-u '<username>'` to curl and enter
the current password at its prompt). The username is ignored by the controller.
The dashboard has no password-setting control; use this API endpoint to change
or clear the password.

An empty password disables authentication. Once enabled, state-changing and
diagnostic endpoints require HTTP Basic authentication. The dashboard and its
read-only data remain open for wall displays.

nginx receives Basic credentials over TLS and forwards them to loopback-only
Flask. Treat this as access control for a trusted local network, not as an
internet-facing security boundary.

Non-public API writes additionally reject cross-origin browser requests even
without a password. See [API access rules](api.md) for JSON and origin checks.
