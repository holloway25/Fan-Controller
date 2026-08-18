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
