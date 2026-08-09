# Dashboard

What the screen shows and why it is coloured the way it is. Nothing here
changes behaviour; it is all read from `/api/state`.

## Layout

Two rows. Fan state first - mode, running state, draw, and the
minimum-run lease - then the readings: temperature, CO2 and humidity.
Everything is a pill of the same size, on one 10px grid.

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

Green needs three things at once: Auto actually in control, the input
enabled, and a live reading. So a reading greys out in Manual and Force
on, where nothing is acting on it, and greys out when its sensor stops
reporting - green must never claim a sensor is fine while nothing is
arriving from it.

Humidity colours the same way once its trigger is enabled, and stays
neutral while it is off. A glance at the pills answers which inputs are
live. PM2.5 is recorded and charted but not shown: it drives nothing, so
a pill for it read as a trigger it is not.

The wattage pill is the one exception to green meaning monitored. It
colours by band whenever the fan is running and settled, whether or not
`watts_enabled` is ticked, because an out-of-band draw is worth seeing
even when nothing will act on it. Neutral there means the fan is off, not
that the draw is unwatched.

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
Poll failures appear in the log as well as the banner, coloured as faults.
The banner shows only the current error, so it disappears the moment the
problem clears; the log is what remains afterwards. A "Sensor poll
recovered" line marks the end of one.

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

Temperature and humidity do the same when their triggers are enabled, so
a disabled trigger cannot imply the fan responds to it. Thresholds join the row
scale, so a level the room never reaches is still drawn.

One time axis under both charts: they plot the same window.

History comes from `/api/history`, fetched only when `history_rev`
changes, not on every poll. See `api.md`.
