# Troubleshooting

## CO2 stops resolving

Most likely a Protect firmware update reshaped the payload. Open
`/api/debug/sensor` to see the live structure, then adjust `CO2_HINTS` or
`EXTRA_HINTS` in `controller.py`. The hints are substring matches against
flattened key names.

## "Cannot reach the UniFi console" right after a restart

The resolver, not the console. The service is ordered after
`network-online.target`, which only proves the link has an address - not
that DNS is answering. A machine that boots in a couple of seconds can
issue its first login before its nameserver is up, and the lookup fails
with `Temporary failure in name resolution` (EAI_AGAIN) rather than a
refusal or a timeout.

`loop()` waits for `protect.host` to resolve before its first poll, up to
30 seconds, then continues regardless. Bounded on purpose: a genuinely
wrong hostname must still reach the normal error path so the UI can report
it. The wait is on the poll thread, so the dashboard is served throughout,
and it ends early on any settings change - Manual, Force on and a
corrected hostname take effect at once rather than queueing behind it,
since modes are applied by the poll loop and not by the route that sets
them. The ceiling is below the shortest fail-safe window `stale_minutes`
can express, so ventilation is never delayed by it. A literal IP skips the
wait entirely.

If the message does appear it is logged once to the activity feed and
clears on its own - the loop backs off and retries. One that never clears
means the name really is unresolvable: check the record and the
nameserver, not the console.

## An error appeared and then cleared

Poll failures are written to the activity log as well as the banner, so a
fault that has since resolved can still be found after the fact. Look for
the message in the log with its timestamp, and for "Sensor poll recovered"
marking when it cleared.

Repeats are deduplicated: an outage lasting hours logs once, not once per
retry, so the entry you see is when it started rather than when it last
tried. This keeps a long outage from evicting real switching history from
the 300-entry log.

## HTTP 429 from UniFi

Login rate limiting. Clients are cached to avoid this, but if it appears,
the loop backs off exponentially from `poll_seconds` to a 5 minute ceiling
and recovers on its own.

## HTTP 499 on login

The account has two-factor enabled. Create a separate local user without
it; the private API cannot complete a 2FA challenge.

## Fan will not stop

Check `latch` in `/api/state`. If all three are false, the minimum run timer is
holding it - see control-logic.md. If one is true, the corresponding
reading has not yet crossed its off threshold.

## Page loads but never updates

The UI polls `/api/state` every 5 seconds. If the server log shows `GET /`
with no follow-up requests, the JavaScript died on load. Errors are posted
to `/api/clienterror` and appear in the log and activity feed. Check the
browser console too. The UI is only reachable over HTTPS: nginx redirects
plain HTTP and HSTS makes the browser refuse it anyway. Port 8096 is bound
to loopback, so it is not an alternative route - use an SSH tunnel if you
need to bypass nginx.

## Settings appear not to save

The page refreshes every 5 seconds and will overwrite fields you are
editing unless it knows they are dirty. Checkboxes and selects fire
`change`, not `input`; both are listened for. If a control is added, wire
it into the dirty list or edits will be silently reverted.

## Service will not stay running

Run it under systemd:

    sudo systemctl status office-fan-controller
    journalctl -u office-fan-controller -f

`run.sh` is the fallback where systemd is unavailable. It uses
`setsid --fork`, because backgrounding with `&` alone does not survive an
SSH session ending.

## Plug returns HTTP 401

A web password is set on the plug but the service does not have it, or has
the wrong one. Enter it under Fan plug and press Test plug.

## Plug returns an empty reply

Tasmota's HTTP command API is disabled on the device. `/` and `/?m=1` keep
working while `/cm`, `/cs` and `/in` return zero bytes, which looks like a
network fault but is not. Enable the command API in the device's web
settings.

Do not fall back to the web root's `/?m=1&o=1`: it toggles rather than
setting a state, so a dropped response leaves the fan wrong with no way to
tell.

## Fan reports on but draws no power

The relay closed and nothing is running: a tripped fan, a loose plug, or a
seized motor. The meter only reads while the relay is closed, so 0 W
with the relay off is normal and not a fault.

The plug meters with an HLW8032, which Tasmota reads through its CSE7766
driver. Athom ship it pre-calibrated and rate active power to about 0.2%,
so a wrong reading is rarely calibration.

The known failure is at the bottom of the range: the HLW8032 reports
invalid measurements below roughly 5 W. Tasmota SetOption39 sets how many
invalid readings it tolerates before declaring no load, defaulting to 128.
A small load reading 0 W is that, not a dead fan. A fan drawing tens of
watts is well clear of it, so 0 W there is genuinely nothing running.

## Home screen icon is stale or missing

iOS caches the touch icon per shortcut and will not refetch it. After
changing `static/apple-touch-icon.png`, delete the home screen shortcut and
re-add it from Safari - reloading the page will not update it.

The stored icon is already flattened onto the dashboard background and inset
by 8%. Neither is decorative: iOS composites transparency onto black, and
the squircle mask clips anything close to the corners.

Requests for `/apple-touch-icon*.png` or `/favicon.ico` arriving at the root
rather than under `/static` mean the `<link>` tags are absent from the page
head. iOS only probes the root when nothing is declared, and those probes
return 404.

## Chart is empty or stops updating

The trace is fetched from `/api/history`, not `/api/state`. The UI
refetches it only when `history_rev` or `history_generation` in
`/api/state` differs from the copy it holds, so an empty chart with live
readings means that fetch failed rather than the poll. Check the browser
network tab for `/api/history`, and confirm `history_rev` is incrementing
once per poll interval - if it is frozen, the poll thread is not
appending and the readings on screen are stale too.

A hard refresh clears it either way: with nothing held, the UI asks for
the whole trace rather than a delta.

## Finding an episode later

Every alert and every fault line in the activity log starts with a
code, so one search finds the whole episode:

    grep FC-003 events.json

An episode opens with an alarm (`0xx`) and closes with an all-clear
(`1xx`). A `FC-003` with no matching `FC-101` is a fault that was never
acknowledged; an `FC-101` carries the duration, so how long the fan was
unavailable does not have to be worked out from timestamps.

The codes never change and are never reused, so a search that works
today works on an archive from a year ago.

An alarm with no all-clear can also mean the all-clear was suppressed
on purpose: `FC-100`, `FC-101` and `FC-102` only send if the original
warning was actually delivered.

## Nothing told me the fan had stopped

Check the **Fault Handling** card first. Alerts are off until
configured, and an install that has never had them filled in sends
nothing: **Send alerts** unticked, a blank mail server, a blank from
address or no recipients each mean silence. The **Send test email**
button reports the real error and is the fastest way to find out which.

A test message proves the transport - DNS, the firewall, authentication
and delivery. It does NOT prove the alerts themselves: it sends
directly, bypassing the transitions that decide when to send. A relay
that works and an alert that never fires are different problems.

A test message also carries no fault code, which is the quickest way to
tell one apart from a real alert in a mailbox.

If alerts ARE configured and nothing arrived, the likely causes in
order: the fault was raised while a previous one still stood, so there
was no transition to alert on; the readings-stopped alert was suppressed
because `stale_minutes` is 0 or the mode was not Auto; or the send
failed, which is logged as `Alert email failed` in the activity log
rather than retried.

Without email at all, the ways to find out are:

- the dashboard, which shows the fault banner and, separately, `Fault
  not saved to disk` when a fault could not be persisted
- `curl -s localhost:8096/api/state` and look at `fault` and
  `fault_unsaved`
- `journalctl -u office-fan-controller`, which is also where a fault
  lost during a signalled stop is reported

What sends, what does not, and the three deliberate silences are in
`Docs/configuration.md`.

## "Saved and in use, but the disk did not confirm..."

The settings were applied and are live. The file on disk already holds
them, so a restart will keep them. What could not be confirmed is that
the rename survives a power cut - the write landed, the directory sync
that commits it did not.

Nothing is broken and nothing needs undoing. Save again once the disk is
healthy and the notice goes away. The same message can appear when
setting or clearing the UI password, for the same reason.

If it persists, the filesystem is the place to look:

    df -h .
    dmesg | tail -20
    touch testfile && rm testfile

A full disk, a read-only remount after an I/O error, or a failing SD
card all produce it. The fan keeps running on the settings you saved
either way - this is about what survives a power cut, not about what is
in force now.

Two related states are NOT this and are worth telling apart:

- `Could not save settings ... Nothing was changed` means the write
  never landed. Nothing was applied.
- `Fault not saved to disk` on the dashboard is about a draw fault
  rather than settings, and it blocks restarts. See below.

## Fan stopped with a draw fault

Power monitoring was enabled and the draw sat outside `watts_min` to
`watts_max` for `watts_fault_seconds` while the fan ran, so it was stopped
and the fault latched. Automatic runs stay blocked until it is cleared -
including the stale-reading failsafe.

Check the band matches the fan before assuming the fan is at fault. A
two-speed unit on high, or a replacement fan, will sit outside a band set
for something else and read as broken while working perfectly.

Force on overrides the fault, so use it to test after a repair. Clear the
fault from the Fan plug card once the draw is right.
Force on is re-asserted every few seconds by the draw thread, not only on
the poll interval, so testing a repaired fan works even while the console
is unreachable.

A fault reading "stop unconfirmed, retrying" means the plug did not
acknowledge the stop, so the fan may still be running. The stop is retried
every draw cycle until confirmed. Check the plug is reachable: losing
contact with it both raises the fault and prevents acting on it.

Changing the plug address clears its stored password by design, so the
service cannot be repointed at another host and made to authenticate
there. Re-enter it after any address change or draw readings stop.
