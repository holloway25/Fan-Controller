# Dashboard

What the screen shows and why it is coloured the way it is.

The live display is populated from `/api/state`. THE CONTROLS ARE NOT
READ-ONLY: mode, settings, fault acknowledgement and the test send are
POST requests that change what the controller does. This page used to
say that nothing here changed behaviour, which described the display
and misdescribed the page.

## Layout

Two rows. Fan state first - mode, running state, draw, and the
minimum-run lease - then the readings: temperature, CO2 and humidity.
Everything is a pill of the same size, on one 10px grid.

CO2 sits with the other readings rather than as a headline number. The
charts below carry the same values over 24 hours, each with its current
value in the left gutter.

Below the charts are the settings cards, each collapsible and each
remembering whether it was left open. The last of them is **Fault
Handling**: who gets told when the fan stops, and the **Send test
email** button. Alerts from this card carry a fault code; the test
message deliberately does not, which is the quickest way to tell the
two apart in a mailbox. It is named for the situation rather than the
transport, because what it configures is what happens when something
goes wrong - email is only how it reaches you. Its settings live under
the `email` key in `config.json`.

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
live. PM2.5 is recorded in the history and returned by the API, but not
drawn and not given a pill: it drives nothing, so showing it read as a
trigger it is not.

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

**Colour follows the fault code series, not the wording.** `0xx` and
`2xx` are red, `1xx` green. A new code therefore inherits the right
colour without the page being edited.

`3xx` DEPENDS ON THE MODE RECORDED IN THE LINE, which is the one place
the series alone is not enough. `FC-300` says something other than this
service moved the plug:

- `(Manual)` renders as an ordinary switch. Manual exists so a person
  can work the fan by hand, so this is the mode doing its job.
- `(Auto)` and `(Force on)` stay highlighted. There the controller
  believed it owned the plug and something contradicted it - a person,
  or a second controller running against the same hardware.

The mode is written into the entry rather than read from the current
settings, because the entry outlives the mode it happened in.

This matters because the rules used to match the words. They were
anchored to `Draw fault` and `Fan `, and both stopped working the moment
log lines gained an `FC-` prefix - faults quietly rendered as ordinary
text. Nothing caught it: no test reads those strings and the only place
it shows is a screen.

For lines with no code the text rules still apply, and the code prefix
is stripped first so a coded and an uncoded line take the same branch.
The fault match stays anchored to the start or end of the message rather
than searching for the word. `Changed: watts_fault_seconds` names a
setting, not a fault, and a substring match turned it red.

### The version

Bottom left, quiet on purpose: it answers "which code is this box
running" and nothing else. A short commit hash, with a trailing `+` when
the working tree has uncommitted changes, or `unknown` where git is not
available and no `VERSION` file exists.

Worth having because boxes drift apart deliberately - a controller can
sit on a known-good commit for days while a development copy tracks
main, and the screen is where anyone would look first to tell them
apart.

### On a phone

The timestamp column shows `17/08/26 11:27` - two-digit year, no
seconds. The column is fixed width and never wraps, so every character
it spends is taken from the message beside it.

TAP OR FOCUS A TIMESTAMP to expand it to the full local value with the
offset; tap again to collapse it. A tooltip alone needed a mouse. The
element is a `<time>` with a machine-readable `datetime`, so a screen
reader announces the whole moment without expanding anything, and
`events.json` still keeps the epoch.

Log lines read state first, then reason: `Fan off - manual`,
`FC-001 Fan on - no reading for 30 min`. The left of the dash is drawn
from a handful of strings, so the eye finds state changes without
reading the sentence.

Inputs are 16px on narrow screens. Below that, mobile Safari zooms the
whole page when a field takes focus.

The webfont is fetched without blocking render. This is a LAN device: if
the internet is down the dashboard must still appear, in system fonts
rather than not at all.

There is a manifest, so adding the page to a home screen opens it
standalone rather than in a tab.
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

History comes from `/api/history`, fetched only when the revision or
the process generation in `/api/state` differs from the copy held, and
then usually as a delta rather than the whole trace. See `api.md`.
