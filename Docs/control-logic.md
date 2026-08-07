# Control logic

## Modes

| Mode | Behaviour |
|---|---|
| Auto | Thresholds below decide |
| Manual | Switches the fan off once, then leaves it alone |
| Force on | Holds the fan on regardless of readings |

Manual hands control back to Google Home or the plug itself. Entering it switches
the fan off once so it is not left running, and after that nothing is
touched - external switching is recorded but never undone.

Manual and Force on are both applied before the sensor is read, so neither
depends on Protect. Force on is the override reached for when the reading
cannot be trusted, so it has to work while the console is unreachable, the
payload has stopped resolving, or no sensor has been selected yet. Neither
mode returns early: readings are still recorded, so the 24h trace has no
gap across a forced run.

There is no force-off mode: holding the fan off actively fought manual
control. Configs still holding the old `off` value migrate to `manual`.

## Per-rule latching

CO2 and temperature latch independently:

- CO2 latches on at `on_at`, releases at `off_at`
- Temperature latches on at `temp_on`, releases at `temp_off`

The fan runs while **any** rule is latched.

Independence matters. An earlier version required *both* inputs to be below
their off levels before stopping, so a rule that had never triggered could
still veto the stop. In practice a CO2-driven run was held on for hours by
a temperature condition that had never been met.

## Minimum run time

Once the fan starts it stays on for `min_run_minutes` even if the readings
clear, to prevent short-cycling. Two exceptions:

- The first reading after startup does not restart the timer, so an
  already-running fan is not granted a fresh lease on every restart.
- Leaving Force on clears the lease, so returning to Auto takes effect at
  once rather than serving out a timer started manually.

The dashboard always shows a "Min run" pill: its configured length when
idle, and a green countdown while this is the only reason the fan is
running - fan on, in Auto, with neither rule latched. Without it a fan
running below the off level looks like a fault rather than the
anti-short-cycling working. The countdown is deliberately not shown in
Force on, where the mode holds the fan rather than the lease. See
`dashboard.md` for the colours.

## Responsiveness

The poll loop waits on an event rather than sleeping, and saving settings
wakes it. Mode changes therefore apply in about a second instead of
waiting up to `poll_seconds`.
