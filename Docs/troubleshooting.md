# Troubleshooting

Start with:

```sh
systemctl status office-fan-controller
journalctl -u office-fan-controller -n 100
curl -s http://127.0.0.1:8096/api/state
```

The dashboard banner shows the current error. The activity log can show earlier
errors that have recovered, within its retained 1000 entries. It is not a
complete audit trail; use the service journal too.

## Service refuses to start

Read the first configuration error in the journal. An existing `config.json`
must be valid JSON made of known sections and settings; invalid present values
are not replaced with defaults. Correct the named field rather than deleting
the live configuration. A missing field in an otherwise valid older file is
filled from the current defaults. CO2 and draw relationships are always
validated; temperature and humidity bands are checked when enabled. See
[configuration](configuration.md) for the exact rules.

## Damaged persisted fault

An FC-206 persisted-fault message means the `fault.json` path exists but its
complete record could not be read safely. This includes a dangling symbolic
link or unrepresentable timestamps. The service treats it as a safety fault,
not as a cleared fault. Do not delete it to suppress the warning: inspect its ownership,
mode, link target and JSON structure, then acknowledge the resulting safety
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
Manual and Force on also skip it. A blocked resolver runs in a single daemon
worker; the startup wait still ends at its deadline or when settings change,
without waiting for that lookup to finish. Late results do not update state.
Normal Protect requests have a separate 30-second end-to-end deadline that
includes DNS; a timed-out request may finish in the background but cannot
refresh readings. While it remains blocked, further Protect requests report
that one is still in progress rather than starting more workers. Pending Manual
stops continue through the meter loop independently of these sensor requests.

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

If readings are stale and fail-safe ON failed, check `FC-207` in the activity
log and plug connectivity. The FC-001 outage email should say **fan ON
unconfirmed** and **Ventilation is NOT confirmed**. It is an outage warning,
not proof the relay switched; the controller retains its normal retries.

Check mode changes, draw faults and external plug changes first. A cold restart
can also explain a stop above the off threshold or during a minimum-run lease:
it drops transient latches and the lease. An already-ON relay can instead get
the bounded cold-start hold in fault-free Auto; look for `Latched on` and the
dashboard's Cold start countdown. This does not restore the old trigger latch.
Look for `FC-301` or, for a requested
restart, "Resumed after restart". See [restart behaviour](architecture.md#restart-model).

Missing activity entries do not rule out a restart: events can be pruned or
fail to persist. Check the journal, `process_started_at`, mode, latches and
lease in `/api/state` before drawing a conclusion.

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
the relay state or low draw. Check plug connectivity immediately. For
FC-007/FC-008/FC-009, retries are bounded: three attempts 30 seconds apart, a
60-second wait, then three final attempts 30 seconds apart. After that,
automatic OFF commands stop; the standing fault still blocks Auto.
An OFF report with continued watts may indicate failed contacts or metering,
not necessarily a communications failure. Arrange safe independent isolation
and inspection. A recovery alert confirms OFF and low draw, not repaired
contacts, and does not clear the fault.
FC-009 means the initial Manual OFF command failed; its first retry waits
30 seconds. This can be a rejected command or a connection failure, even
with normal running watts. It does not assert welded contacts. The original
Manual promise cannot make extra attempts after the fault budget is exhausted.

If email was unavailable, the latest unsent OFF-fault notice retries about once
a minute after email becomes ready. Look for FC-203 for delivery errors. These
email retries do not command the relay or renew its six-attempt allowance.
Newer stages supersede outdated unsent notices; acknowledgement cancels pending
stage mail. Failed notification-bookkeeping writes are retried independently
of the safety-storage flag. Phase changes include the pending notice in the
same saved record. SMTP acceptance is not proof the message reached an inbox.

## Dashboard problems

### Power monitoring loop warning

**Power monitoring loop is not responding** means its last heartbeat is more
than 30 seconds old. **Power monitoring loop: ...** reports the latest exception
category. Inspect the journal and `/api/state` fields `draw_loop_at` and
`draw_loop_error`; check plug connectivity and service health.

Do not assume disabling power monitoring makes this warning harmless: the
same loop helps enforce faults and Force on. Confirm the actual fan state and
resolve the cause. A normal heartbeat is not proof of healthy power draw.

### Settings or fault-clear request is rejected

- **401:** sign in with the configured dashboard password.
- **403:** use the dashboard's own origin (scheme, host and port), not a form
  or page on another site. Check nginx forwarded-host and scheme settings
  against the [reference config](../deploy/nginx.conf.example).
- **415:** API writes require `Content-Type: application/json`.
- **400:** send a valid JSON object and correct the named validation error.
  Fault acknowledgement can use `{}`; arrays, scalars and empty bodies fail.

Do not disable origin protection to make another site's requests work. See
[API access rules](api.md). A rejected acknowledgement leaves the fault latched.

### Page loads but does not update

The browser should request `/api/state` every five seconds. If only `GET /`
appears in the service log, inspect the browser console and activity log for a
client error.

Flask is loopback-only. Access it through nginx over HTTPS or through an SSH
tunnel; port 8096 is not intended to be reachable from the LAN.

### Chart is empty or stops updating

Charts use `/api/history`; history is not embedded in `/api/state`. Check the
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

A **Partial delivery** error means the relay accepted some recipients and
rejected others. Accepted recipients may already have the message. Check
recipient addresses and relay policy before testing again; a retry may send
duplicates to those already accepted. The controller does not count this as
a fully delivered alert.

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
```

This differs from a 500 response saying nothing changed. It also differs from
**Fault not saved to disk**, which blocks a requested restart.

### `restart.sh` is refused

Read the refusal reason reported by the helper:

- **Temporarily busy:** settings are being changed, a plug action is in progress,
  or a fault is being written. Retry later; this does not imply an unsaved fault
  or a storage failure, and does not require fault acknowledgement.
- **Active fault not saved to disk:** clear the fault only if it is safe, or
  repair the storage problem and allow the fault retry to succeed. Do not bypass
  this refusal with systemd unless you understand that the replacement may not
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

`FC-0xx` identifies an alarm category, `FC-1xx` a recovery or all-clear,
`FC-2xx` an operational status or failure, and `FC-3xx` useful information.
These codes are not delivery receipts: an alarm can be logged even with email
disabled or a failed send. Check mail configuration and `FC-203` separately.
Searching `FC-301` finds cold starts retained in this file, helping separate
a power or restart event from a control problem. Use the journal for additional
evidence; absence of an entry is not proof that no restart occurred.
Recovery of a `2xx` problem uses the same code, such as
`FC-201 Poll recovered`.

The complete code table is in [configuration.md](configuration.md).
