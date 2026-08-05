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

Start with `run.sh`, which uses `setsid --fork`. Backgrounding with `&`
alone does not survive an SSH session ending.
