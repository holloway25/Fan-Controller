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

Three rules latch independently, each with its own enable flag and its
own on and off levels:

| Rule | Enabled by | Latches on at | Releases at |
| --- | --- | --- | --- |
| CO2 | `co2_enabled` (default true) | `on_at` | `off_at` |
| Temperature | `temp_enabled` (default false) | `temp_on` | `temp_off` |
| Humidity | `humidity_enabled` (default false) | `humidity_on` | `humidity_off` |

The fan runs while **any** enabled rule is latched. All three run the fan
when the reading is HIGH: humidity ventilates damp air out rather than
holding it in.

They live in a table in `controller.py`, not as branches in `decide()`, so
a fourth trigger is a row plus a schema entry rather than another copy of
the same block.

Independence matters. An earlier version required *both* inputs to be below
their off levels before stopping, so a rule that had never triggered could
still veto the stop. In practice a CO2-driven run was held on for hours by
a temperature condition that had never been met.

Every rule can be disabled, including the last one. Auto then has nothing
to latch on and never starts the fan; only the stale-reading fail-safe
can. That is allowed rather than refused, because Manual already exists
for "do not run automatically" - but it is never silent, and the UI warns
as soon as the last trigger is unticked.

`co2_enabled` defaults to **true** and must stay that way. `load_cfg`
fills missing keys from `DEFAULTS`, so this is what a `config.json`
written before the flag existed reads as. A false default would have
stopped every upgraded install ventilating, with nothing in the log.

A missing reading is not a release, for any of the three. The rules used
to disagree - CO2 held its latch when its reading dropped out while
temperature and humidity cleared theirs - which recorded how the code
happened to be written rather than a decision. All three now hold.

The reasoning: a reading that stops arriving says nothing about whether
the condition cleared. The room was hot, the sensor went quiet, and
nothing since suggests it cooled, so releasing would stop the fan on the
strength of not knowing.

This only bites during a partial outage. If every reading stops,
`reading_at` stops advancing and the stale-reading fail-safe ventilates
anyway. The gap was one metric dropping out while the others still
report, which keeps the fail-safe asleep - precisely when releasing the
latch was worst.

The cost is a fan that may run longer than needed on a stale latch until
a reading returns and falls below the off level. A rule that is switched
off still releases immediately, reading or no reading.

A latch is held in memory, not derived from the current thresholds, so
changing a threshold does not release one that is already set. Lowering
`on_at` to start the fan and then raising it back leaves CO2 latched: the
release condition is `co2 <= off_at`, which the reading may be nowhere
near. The fan keeps running, correctly by the rule but surprisingly if you
were only adjusting a number. To clear one deliberately, raise the off
level above the current reading for a poll, then put it back.

## Enforcement

A mode is not a one-off command; it is re-asserted. Force on is checked by
the poll thread and again by the draw thread on its shorter cadence, so a
fan switched off at the plug or by Google Home comes back promptly even
while Protect is unreachable and the poll loop is backing off. Without the
second thread, enforcement inherited the Protect backoff and could take
five minutes.

Every actuation takes a lock and re-checks the mode before touching the
plug, and a settings save commits the new mode under that same lock. A
mode change and a switch therefore cannot interleave: without this, Force
on could land after Manual's one-time stop had run, leaving the fan going
with nothing left to correct it. See `architecture.md`.

## Minimum run time

Once the fan starts it stays on for `min_run_minutes` even if the readings
clear, to prevent short-cycling. Two exceptions:

- The first reading after startup does not restart the timer, so an
  already-running fan is not granted a fresh lease on every restart.
- Leaving Force on clears the lease, so returning to Auto takes effect at
  once rather than serving out a timer started manually.

The dashboard always shows a "Min run" pill: its configured length when
idle, and a green countdown while this is the only reason the fan is
running - fan on, in Auto, with no rule latched. Without it a fan
running below the off level looks like a fault rather than the
anti-short-cycling working. The countdown is deliberately not shown in
Force on, where the mode holds the fan rather than the lease. See
`dashboard.md` for the colours.

## Responsiveness

The poll loop waits on an event rather than sleeping, and saving settings
wakes it. Mode changes therefore apply in about a second instead of
waiting up to `poll_seconds`.
