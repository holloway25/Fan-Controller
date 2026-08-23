# Dashboard

The dashboard reads live data from `/api/state` and history from
`/api/history`. Its controls change mode, settings, authentication and fault
state.

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

The power pill is neutral while the fan is off. While it is running and settled,
power is green inside the configured band and red outside it, even when
`watts_enabled` is off. The setting controls fault action, not visibility.

Activity entries use the server-provided `cls` value. The server decides what
an event means; CSS decides how that class appears. `FC-0xx` and `FC-2xx`
are faults, `FC-1xx` is an all-clear, and `FC-300` depends on the mode
recorded with the event.

`FC-301` is neutral. A cold start is routine on a mains- or PoE-powered board
and there is nothing to act on, so it does not take a colour that would put it
in with the entries that need attention.

## Mobile behaviour

Compact timestamps use a two-digit year and omit seconds. Tap or focus a
timestamp to show the complete local time and offset; activate it again to
collapse it. The underlying `<time>` element retains a machine-readable value.

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

## Countdowns

One browser timer updates:

- **Refresh in**, derived from `poll_seconds`
- **Min run**, showing the remaining lease

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
