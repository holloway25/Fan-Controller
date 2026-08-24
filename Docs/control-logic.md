# Control logic

## Modes

| Mode | Behaviour |
|---|---|
| Auto | Enabled thresholds control the fan |
| Manual | Stop once, then leave the plug under external control |
| Force on | Keep the fan on regardless of readings |

Manual and Force on are applied before reading Protect, so they remain usable
when the sensor is unavailable. Readings are still collected in all modes.

There is no force-off mode. Older `off` values migrate to `manual`.

## Trigger latches

The three rules latch independently:

| Rule | Enabled by | Latches on | Releases |
|---|---|---|---|
| CO2 | `co2_enabled` | `on_at` | `off_at` |
| Temperature | `temp_enabled` | `temp_on` | `temp_off` |
| Humidity | `humidity_enabled` | `humidity_on` | `humidity_off` |

The fan runs while any enabled rule is latched. Every on threshold must be
higher than its corresponding off threshold.

A missing metric does not release its latch because absence does not prove the
condition cleared. Disabling a rule releases it immediately. If all metrics
stop, the stale-reading fail-safe may ventilate according to
`stale_minutes`.

## What the stop line names

`Fan off` names every rule that latched during the run, not only whichever was
last to release. A run started by CO2, temperature and humidity, where the
latter two fall back inside their bands while CO2 keeps the fan going, still
reports all three.

Rules that were never latched are not named. A CO2-only run reports
`Fan off - 672 ppm`, not the temperature and humidity readings that had no
bearing on it.

The record is per-run and is tied to what the relay confirmed, not to what was
decided. It is committed only once a switch has been accepted, so a switch-on
that failed records nothing and a switch-off that failed keeps the run intact
for the retry that eventually stops the fan.

It is cleared wherever a stop is confirmed, and there are five such places:

| Path | Confirmed by |
|---|---|
| Automatic stop | the plug accepting the switch-off |
| Plug read as off | the plug reporting off, with nothing commanded |
| Manual | the plug taking the one-time manual stop |
| Draw fault | the fault record reaching disk |
| Force on | the plug reporting off before it is forced back on |

The distinction throughout is CONFIRMED, not intended. A stop that was
attempted and failed leaves the record intact, because the fan may still be
running and the retry needs it. A plug that cannot be read is unknown rather
than off, and clears nothing.

Three of these end a run with no rule releasing, so the control decision never
sees them. Manual returns before the decision is made at all; a plug read as
off leaves automatic control looking at a fan already in the state it wants, so
it issues nothing; and Force on ends the physical run even though the fan is
about to start again, because what follows is a new run and is not driven by
the rules.

The plug-read case also covers the first poll after a restart, where the
handover has restored a run record but not the cached fan state. That is why it
is expressed as a confirmed OFF rather than as a change of state - at that
point there is nothing to have changed from.

A requested restart carries it; a cold start does not, and `FC-301` announces
that so a shorter line afterwards is not read as the whole story.

## Latch storage

Latches are stored in memory rather than recalculated when thresholds change.
To clear a latch deliberately, allow a valid reading to cross its off threshold
or disable the rule.

Disabling all rules is permitted. Auto then starts the fan only through the
stale-reading fail-safe, if configured.

## Mode enforcement

Force on is reasserted by both the poll thread and the faster draw thread. This
keeps it effective while Protect is unavailable or backing off.

Every plug action is serialised and rechecks the current mode and configuration
generation immediately before acting. A settings update cannot interleave with
an old command and leave the plug in a state that contradicts the new mode.

## Minimum run time

An automatically started fan remains on for `min_run_minutes` to avoid
short-cycling. The lease does not restart merely because the service starts and
finds the relay already on. Leaving Force on clears the lease so Auto can take
effect immediately.

The dashboard shows the configured minimum while idle and a countdown when the
lease is the only reason an Auto-mode fan remains on.

## Stale-reading fail-safe

When `stale_minutes` is greater than zero and Auto has received no reading for
that duration, the controller attempts to turn the fan on. A latched draw fault
blocks this action because a fan believed unsafe must not be started blindly.

The fail-safe is disabled in Manual and unnecessary in Force on.

## Draw faults

With `watts_enabled` on, the running fan must continuously report a fresh
reading inside `watts_min` to `watts_max`. A reading inside the band resets
the fault timer. Remaining outside it for `watts_fault_seconds` stops the fan
and stores a fault.

The fault survives restart and blocks Auto until acknowledged. If the stop is
not confirmed, it is retried. Force on overrides the fault so repaired hardware
can be tested.

## Responsiveness

The poll loop waits on an event rather than an unconditional sleep. Saving
settings wakes it, so mode and configuration changes do not wait for the full
`poll_seconds` interval.
