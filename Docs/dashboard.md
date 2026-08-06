# Dashboard

What the screen shows and why it is coloured the way it is. Nothing here
changes behaviour; it is all read from `/api/state`.

## Layout

Two rows. Fan state first - mode, running state, draw, and the
minimum-run lease - then the readings: temperature, CO2, humidity,
PM2.5. Everything is a pill of the same size, on one 10px grid.

CO2 sits with the other readings rather than as a headline number. The
charts below carry the same values over 24 hours, each with its current
value in the left gutter.

## Colour

One meaning per colour, used the same way everywhere it appears.

| Colour | Meaning |
|---|---|
| Green | Monitored and behaving |
| Amber | Currently demanding the fan |
| Red | Fault, or manual override |
| Blue | Auto mode |
| Neutral | Shown but not acted on |

So temperature is green only when its trigger is enabled, and humidity
and PM2.5 are always neutral: they are recorded and charted but nothing
switches on them. A glance at the pills answers which inputs are live.

Amber covers Force on as well as a latched rule, because Force on is
demand too - yours rather than a sensor. Red covers Manual alongside
faults: manual hands the fan to the plug and bypasses every rule, so it
is the point automation stopped being in charge.

The activity log uses the same colours, so scanning it reads like the
controls: fan switches green, faults red, mode changes in their mode
colour, settings edits neutral. A switch-on caused by Force on takes the
mode colour rather than the green of an automatic one: the same event
should not read two ways on adjacent lines.

The fault match is anchored to the start or end of the message rather
than searching for the word. `Changed: watts_fault_seconds` names a
setting, not a fault, and a substring match turned it red.

## Why the fan is running

Three states answer it, and only one can apply at a time:

- An amber reading - that rule is latched and demanding the fan.
- A green `Min run` countdown - nothing is demanding it, the
  anti-short-cycling lease is holding it on.
- Neither, with the fan running - Force on or Manual.

The lease pill is always present, showing its configured length when
idle, so it is discoverable before it is holding anything.

All three are Auto-only judgements. In Force on the latches can still
hold values from an earlier run, so colouring them there would name the
wrong reason.

## Countdowns

Two, both driven by one 1-second timer rather than the 5-second poll,
which would make them step in fives.

- `Refresh in` counts to the next sensor read, derived from
  `poll_seconds` rather than a fixed number so it stays honest if the
  interval changes. An overdue read shows `Refresh due`.
- `Min run` counts the lease down.

The refresh countdown can briefly touch `Refresh due` and jump back up.
The read happens on the server but the page only learns of it on its
next 5-second poll, so it can count against the previous reading for a
moment. Accurate rather than wrong, but visible.

## Charts

The CO2 trace shows its thresholds as dashed lines with the band between
them shaded, and the current reading in the gutter at the height of the
latest point, nudged clear of the threshold labels sharing that gutter.

Temperature does the same when its trigger is enabled, so a disabled
trigger cannot imply the fan responds to it. Thresholds join the row
scale, so a level the room never reaches is still drawn.

One time axis under both charts: they plot the same window.

History comes from `/api/history`, fetched only when `history_rev`
changes, not on every poll. See `api.md`.
