# Troubleshooting

Start with:

```sh
systemctl status office-fan-controller
journalctl -u office-fan-controller -n 100
curl -s http://127.0.0.1:8096/api/state
```

The dashboard banner shows the current error. The activity log preserves errors
that have recovered.

## Service refuses to start

Read the first configuration error in the journal. An existing `config.json`
must be valid JSON made of known sections and settings; invalid present values
are not replaced with defaults. Correct the named field rather than deleting
the live configuration. A missing field in an otherwise valid older file is
filled from the current defaults.

An FC-206 persisted-fault message means `fault.json` exists but could not be
read safely. Do not delete it merely to make the service start: inspect its
ownership, mode and JSON structure, then acknowledge the resulting safety
fault through the normal operator flow once the underlying condition is
understood.

## Protect and sensor problems

### CO2 or another metric stops resolving

A Protect firmware update may have changed the private bootstrap payload. Open
`/api/debug/sensor`, inspect the selected sensor's `airQuality` data, and
update `CO2_HINTS` or `EXTRA_HINTS` in `controller.py` if required.

### Protect is unreachable immediately after boot

The network may have an address before DNS is ready. The poll thread waits up to
30 seconds for the configured Protect hostname to resolve, then continues into
normal retry and error reporting. A literal IP skips this wait.

If the error persists, verify the hostname and resolver:

```sh
getent hosts <protect-host>
resolvectl status
```

### HTTP 429 from UniFi

Protect is rate-limiting authentication. The controller reuses sessions and
backs off automatically. Repeated 429 responses can indicate another controller
instance or a configuration that keeps rebuilding the client.

### HTTP 499 on login

The UniFi account has two-factor authentication. Use a separate local account
without two-factor authentication.

## Plug and fan problems

### Plug returns HTTP 401

The configured Tasmota web credentials are missing or incorrect. Re-enter them
under **Fan plug** and run **Test plug**.

Changing the plug address deliberately clears the saved password.

### Plug returns an empty response

The Tasmota HTTP command API is probably disabled even if its web UI works.
Enable the command API in the device settings. Do not replace explicit
`Power ON` and `Power OFF` with a toggle URL; a lost response would leave the
desired state unknown.

### Fan will not stop

Check the current mode, latch values and minimum-run lease in `/api/state`.

- Force on deliberately reasserts the relay.
- An enabled latch remains active until its reading crosses the off threshold.
- A missing reading does not release a latch.
- The minimum-run lease may keep an Auto-mode fan running after every latch has
  released.

### Fan stopped for no apparent reason

A stop that breaks the latch and lease rules - the fan going off at a reading
well above `off_at`, seconds into a minimum-run lease - is usually a restart
that did not come through `./restart.sh`. Latches and the minimum-run lease
live in memory and are carried only by the requested-restart handover, so a
systemd restart, a crash or a loss of power correctly begins with neither.

Look for `FC-301` immediately below the stop in the activity log. It names the
cold start and says what was not carried. A requested restart logs "Resumed
after restart" instead, and carries both.

If neither line is present the process did not restart, and the mode, latches
and lease in `/api/state` are the next thing to read.

### Fan is on but reports no power

Confirm that the fan is connected, free to turn, and not tripped. Zero watts is
normal while the relay is off.

Very small loads can fall below the HLW8032/Tasmota measurement floor. A
ventilation fan expected to draw tens of watts should remain well above it.

### Fan stopped with a draw fault

The measured draw remained missing or outside `watts_min` to `watts_max`
for `watts_fault_seconds`.

1. Confirm the configured band matches the actual fan and selected speed.
2. Inspect the plug and fan.
3. Use Force on to test repaired hardware.
4. Clear the fault only after the draw is healthy.

If the dashboard says the stop is unconfirmed, the controller could not verify
the relay state and will retry. Check plug connectivity immediately.

## Dashboard problems

### Page loads but does not update

The browser should request `/api/state` every five seconds. If only `GET /`
appears in the service log, inspect the browser console and activity log for a
client error.

Flask is loopback-only. Access it through nginx over HTTPS or through an SSH
tunnel; port 8096 is not intended to be reachable from the LAN.

### Chart is empty or stops updating

Charts use `/api/history`, not the history embedded in state. Check the
browser's network requests and confirm `history_rev` advances with successful
polls. A hard refresh forces an initial full-history request.

`FC-208` means the page rendered no chart or activity entries even though the
server supplied data. It is a display fault and does not affect the controller.

### Settings appear to revert while editing

The page refreshes regularly and protects controls marked dirty. A newly added
control must participate in that dirty-state handling or live refreshes can
replace an unfinished edit.

### Home-screen icon is stale

iOS caches the icon with the shortcut. Delete the home-screen shortcut and add
it again from Safari.

## Email problems

### No alert arrived

Check **Fault Handling**:

- **Send alerts** is enabled
- relay host, sender and recipients are filled in
- stored credentials are correct
- **Send test email** succeeds

A test proves SMTP transport but not the transition that produces a real alert.
Search the activity log for `FC-203` or `Alert email failed`.

Some messages are intentionally suppressed:

- stale-reading alerts require `stale_minutes > 0` and Auto mode
- an alert is sent only when entering a state, not on every retry
- an all-clear sends only if the original warning was delivered

### SMTP authentication is refused

The controller uses verified STARTTLS when available. Without STARTTLS it
allows CRAM-MD5 only and refuses cleartext PLAIN or LOGIN authentication.
Correct the relay configuration rather than enabling an insecure fallback.

## Persistence and restart problems

### "Saved and in use, but the disk did not confirm..."

The new setting is active and the file was replaced, but the directory sync
could not confirm survival across a sudden power loss. Save again after checking
the filesystem:

```sh
df -h .
dmesg | tail -20
touch testfile && rm testfile
```

This differs from a 500 response saying nothing changed. It also differs from
**Fault not saved to disk**, which blocks a requested restart.

### `restart.sh` is refused

A live draw fault has not been persisted. Clear the fault only if it is safe, or
repair the storage problem and allow the fault retry to succeed. Do not bypass
the refusal with systemd unless you understand that the replacement may not
know about the unsaved fault.

### Service will not remain active

Inspect the unit and journal:

```sh
systemctl cat office-fan-controller
systemctl status office-fan-controller
journalctl -u office-fan-controller -b
```

Confirm the unit's user, working directory and `controller.py` path match the
current checkout. Reinstall the local unit if the tracked example changed.

Do not start `run.sh` while systemd already owns the service.

## Finding an earlier incident

Search `events.json` by stable fault code:

```sh
grep FC-003 events.json
```

`FC-0xx` opens an emailed alarm, `FC-1xx` closes one, `FC-2xx` records an
operational failure without email, and `FC-3xx` records useful information.
Searching `FC-301` lists every cold start, which is the quickest way to
separate a power or restart event from a control problem.
Recovery of a `2xx` problem uses the same code, such as
`FC-201 Poll recovered`.

The complete code table is in [configuration.md](configuration.md).
