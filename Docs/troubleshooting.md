# Troubleshooting

## CO2 stops resolving

Most likely a Protect firmware update reshaped the payload. Open
`/api/debug/sensor` to see the live structure, then adjust `CO2_HINTS` or
`EXTRA_HINTS` in `co2fan.py`. The hints are substring matches against
flattened key names.

## HTTP 429 from UniFi

Login rate limiting. Clients are cached to avoid this, but if it appears,
the loop backs off exponentially from `poll_seconds` to a 5 minute ceiling
and recovers on its own.

## HTTP 499 on login

The account has two-factor enabled. Create a separate local user without
it; the private API cannot complete a 2FA challenge.

## Fan will not stop

Check `latch` in `/api/state`. If both are false, the minimum run timer is
holding it - see control-logic.md. If one is true, the corresponding
reading has not yet crossed its off threshold.

## Page loads but never updates

The UI polls `/api/state` every 5 seconds. If the server log shows `GET /`
with no follow-up requests, the JavaScript died on load. Errors are posted
to `/api/clienterror` and appear in the log and activity feed. Check the
browser console too. Confirm the URL is `http://`, not `https://` - the
service does not serve TLS and a forced upgrade fails with HTTP 400.

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
seized motor. The CSE7766 only meters while the relay is closed, so 0 W
with the relay off is normal and not a fault.

If it reads 0 W with the fan audibly running, the plug needs power
monitoring calibration - see the Athom template notes at
templates.blakadder.com.
