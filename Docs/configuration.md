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

`watts_min` must be below `watts_max`. Measure the actual fan at its intended
speeds rather than copying its nameplate rating.

Power monitoring is off by default. The dashboard still shows power and its
relationship to the configured band. When monitoring is enabled, a missing,
stale, low or high reading starts the fault timer while the relay is on. A
healthy reading resets the timer.

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
| `FC-001` | Readings stopped and the Auto fail-safe activated |
| `FC-002` | Draw fault raised with no plug reading |
| `FC-003` | Draw fault raised below the configured band |
| `FC-004` | Draw fault raised above the configured band |
| `FC-005` | A fault could not be saved |
| `FC-006` | A saved fault remained after restart |
| `FC-100` | Readings returned after a delivered stale-reading alert |
| `FC-101` | A previously delivered draw-fault alert was cleared |
| `FC-102` | A previously delivered unsaved-fault alert is now persisted |
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

`FC-301` records a start that did not come through `./restart.sh`: a systemd
start, a crash, or a loss of power. Those starts deliberately begin with no
rule latch and no minimum-run lease, so the line names what was dropped and
whether a saved fault was found. It is informational and is never emailed.

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
when using this endpoint; the dashboard can manage the change after sign-in.

An empty password disables authentication. Once enabled, state-changing and
diagnostic endpoints require HTTP Basic authentication. The dashboard and its
read-only data remain open for wall displays.

nginx receives Basic credentials over TLS and forwards them to loopback-only
Flask. Treat this as access control for a trusted local network, not as an
internet-facing security boundary.

Non-public API writes additionally reject cross-origin browser requests even
without a password. See [API access rules](api.md) for JSON and origin checks.
