# Control logic

## Modes

| Mode | Behaviour |
|---|---|
| Auto | Thresholds below decide |
| Manual | Never switches the fan; keeps polling and charting |
| Force on | Holds the fan on regardless of readings |
| Force off | Holds the fan off regardless of readings |

Force off is not "hands off" - it actively holds the fan off and will undo
a manual switch within one poll. Use Manual to hand control to Google Home
or the Hue app.

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
- Leaving Force on or Force off clears the lease, so returning to Auto
  takes effect at once rather than serving out a timer started manually.

## Responsiveness

The poll loop waits on an event rather than sleeping, and saving settings
wakes it. Mode changes therefore apply in about a second instead of
waiting up to `poll_seconds`.
