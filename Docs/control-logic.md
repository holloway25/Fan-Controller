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

The fan runs while any enabled rule is latched. Each enabled rule needs an on
threshold higher than its off threshold. CO2 relationships are validated even
when disabled; temperature and humidity relationships are checked when enabled.
See [configuration](configuration.md#control) for ranges and startup validation.

A missing metric does not release its latch because absence does not prove the
condition cleared. Disabling a rule releases it immediately. A poll must contain
every enabled trigger's measurement. If any is missing, the entire poll is
rejected: existing readings and latches are retained, but the safety timestamp
is not refreshed and no recovery is announced. The stale-reading fail-safe may
then ventilate according to `stale_minutes`, in Auto only. Disabled triggers do
not count; with no triggers enabled, any recognised measurement keeps the
sensor live. Setting `stale_minutes` to 0 disables ventilation and its alerts,
not the incomplete-reading error.

## What the stop line names

`Fan off` names every rule that latched during the run, not only whichever was
last to release. A run started by CO2, temperature and humidity, where the
latter two fall back inside their bands while CO2 keeps the fan going, still
reports all three.

Rules that were never latched are not named. A CO2-only run reports
`Fan off - 672 ppm`, not the temperature and humidity readings that had no
bearing on it.

The record follows confirmed relay actions, not attempted commands. A failed
switch-off retains it for retry; an unreadable plug is unknown, not off.
Failure of the original Manual OFF command latches FC-009 and hands the
outstanding promise to bounded fault-stop enforcement. The original failed
command is separate from the six retries; repeated Manual polls or switching
back to Manual cannot bypass their timing or replenish the budget. Confirmed
OFF and fresh low draw completes the promise, but not the standing fault.
A confirmed Manual stop or a plug read as off ends the run too. For a draw
fault, the plug must confirm off and the updated fault record must reach disk
before the run record is cleared. Force on starts a new, non-rule-driven run
if the plug is first observed off.

`./restart.sh` carries the record. A cold start does not; `FC-301` records that
loss of transient state. Fault-free Auto can give an already-ON relay a separate
bounded cold-start hold; it does not reconstruct the old run or its triggers.
See [restart model](architecture.md#restart-model).

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

The fault survives restart and blocks Auto until acknowledged. If an ordinary
draw-fault stop is not confirmed, it is retried. OFF-but-drawing faults use
the [bounded FC-007/FC-008 sequence](configuration.md#plug), also used for
FC-009 failed Manual stops, with a warning,
hard-fault updates and a confirmed-stop recovery alert. Exhausting the retry
budget stops automatic OFF commands, not read-only monitoring or the fault.
Force on overrides the fault so repaired hardware
can be tested.

## Responsiveness

The poll loop waits on an event rather than an unconditional sleep. Saving
settings wakes it, so mode and configuration changes do not wait for the full
`poll_seconds` interval.
