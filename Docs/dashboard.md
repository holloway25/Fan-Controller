# Dashboard

The dashboard reads live data from `/api/state` and history from
`/api/history`. Its controls change mode, settings and fault state. Password
changes use the [UI authentication API](configuration.md#ui-authentication),
not a dashboard control.

## Layout

The top row shows fan state, mode, power draw and the minimum-run lease. The
next row shows temperature, CO2 and humidity. Charts, collapsible settings
cards, the activity log and **Fault Handling** follow.

PM2.5 can be recorded in history but is not displayed as a control input because
it does not drive the fan.

## Colours

| Colour | Meaning |
|---|---|
| Green | Monitored and healthy |
| Amber | Currently demanding the fan |
| Red | Fault or Manual override |
| Blue | Auto mode |
| Neutral | Displayed but not acted upon |

A metric is green only when Auto is active, its trigger is enabled, and its
reading is live. A latched trigger is amber. Disabled triggers and readings in
Manual or Force on are neutral.

Retained readings are labelled stale after a sensor-read failure or when their
timestamp is older than two polling intervals (minimum 30 seconds). Plug/control
errors keep the overall error indicator red without marking fresh sensor
measurements stale. `/api/state` exposes sensor acquisition errors separately
as `sensor_error`.

The power pill is neutral while the fan is off. While it is running and settled,
power is green inside the configured band and red outside it, even when
`watts_enabled` is off. The setting controls fault action, not visibility.

Activity entries use the server-provided `cls` value. The server decides what
an event means; CSS decides how that class appears. `FC-0xx` and `FC-2xx`
are faults, `FC-1xx` is an all-clear, and `FC-300` depends on the mode
recorded with the event.

`FC-301` is neutral: it reports a cold start, not a fault. An unexpected or
repeated cold start still warrants checking the service journal and power.

## Safety controls and loop health

Selecting **Force on** first asks the server to change mode. With no fault it
succeeds without a prompt. An active safety fault requires explicit confirmation
for that particular fault; a stale dashboard cannot bypass this check. If the
fault changes before confirmation is submitted, the server rejects it; click
again to review and confirm the current fault. **Clear fault** always asks for confirmation: acknowledgement allows Auto
to run again, but does not prove the hardware is repaired.

**Test email** saves the displayed settings first. Any save warning remains
visible alongside the email result, whether the send succeeds or fails. A
successful test does not resolve a disk-durability or cleared-password warning.

A fault banner adds **stop unconfirmed, retrying** when the plug has not
confirmed the stop. A separate warning reports an unsaved fault; requested
restarts are blocked until it is persisted or safely cleared.

The status dot turns red for a polling error, a draw-loop exception or a
draw-loop heartbeat more than 30 seconds old. The page reports **Power
monitoring loop is not responding** for a stale heartbeat. This loop also
enforces modes and faults, so its health matters even when power monitoring
is disabled. See [troubleshooting](troubleshooting.md#power-monitoring-loop-warning).

## Mobile behaviour

Compact timestamps are `DD/MM HH:MM`, on every row including today's. The year
and the seconds are omitted. Tap or focus a timestamp to show the complete
local time and offset; activate it again to collapse it. The underlying
`<time>` element retains a machine-readable value.

Inputs remain at least 16 px on narrow screens to prevent mobile Safari from
zooming on focus. The web font does not block rendering, so the dashboard
remains usable without internet access. The web manifest supports standalone
home-screen use.

## Why the fan is running

In Auto, one of these indicates the reason:

- an amber metric: that trigger is latched
- a green **Min run** countdown: the anti-short-cycling lease is active
- neither: inspect the mode, fault and activity log

Latch colours are Auto-only because values retained from an earlier Auto run do
not explain a fan currently held by Force on or controlled manually.

When another trigger activates during an existing Auto run, the activity log
records it once, using the normal start wording: `Fan on — 70% RH` or
`Fan on — 27.2 C`.
The entry includes the reading and explains continued running; it is not
another switch-on command and does not restart the minimum-run countdown.
A trigger that releases and activates again gets a new entry. Unchanged
triggers are not logged every poll.

## Countdowns

One browser timer updates:

- **Refresh in**, derived from `poll_seconds`
- **Min run**, showing the remaining lease
- **Cold start**, temporarily replacing Min run while an already-ON fan gets
  its bounded Auto startup hold. Set its duration under Fan Plug; 0 disables it.

The server is polled every five seconds, so **Refresh due** may appear briefly
between a server reading and the dashboard learning about it.

## Charts

CO2, temperature and humidity charts show their 24-hour readings. Enabled
triggers add on/off guide lines and a shaded hysteresis band. Low/high labels
describe observed data rather than the chart scale or threshold guides.

The dashboard requests history only when `history_rev` or
`history_generation` changes and normally receives a delta. A process change
or an unreconcilable revision triggers a full refresh.

## Browser error reporting

Browser JavaScript errors are posted to `/api/clienterror` and appear in the
journal and activity log. `FC-208` reports a page that rendered no log or
chart data while the server supplied data. It diagnoses display failure only
and does not affect fan control.
