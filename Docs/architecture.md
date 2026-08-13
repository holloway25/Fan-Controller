# Architecture

A single Flask process run under systemd, with three background threads.
The poll thread reads the sensor and decides; the draw thread reads the
plug's energy meter on a shorter cadence, enforces a latched fault, and
re-asserts Force on. Both switch the plug, which is why the locking below
matters. The third, the restart watcher, never touches the plug - it
only looks for the restart trigger. The Flask routes serve the UI and
settings.

```
UniFi Protect  --HTTPS-->  Fan Controller  --HTTP-->   Tasmota plug
(UP-AirQuality)            (poll, decide)            (relay + power)
```

## Why the private Protect API

Protect 7.1 exposes the sensor through the documented endpoint
`/proxy/protect/integration/v1/sensors`, but that response contains no air
quality data at all - `stats` carries only humidity, light and temperature,
all null. Verified directly against the console.

The readings live on `/proxy/protect/api/bootstrap`, the private endpoint the
Protect web UI itself uses. It needs a UniFi OS session cookie from
`/api/auth/login`, so a local user account is required. An API key returns
HTTP 500 there - it is not a valid credential for that path.

This is undocumented and may change on any firmware update. See
troubleshooting.md.

## Metric discovery

CO2, temperature, humidity and PM2.5 are located by searching the
`airQuality` subtree for a numeric field whose key matches a hint, rather
than by a fixed path, because Ubiquiti has not published the schema. A
rename therefore survives. The path actually used is shown in the UI and
returned as `co2_path`.

The search is scoped to `airQuality` so sibling config blocks such as
`humiditySettings` cannot be mistaken for live readings.

## Session reuse

Protect and plug clients are cached and rebuilt only when the relevant
settings change. An earlier version constructed a new client per poll,
which meant a fresh `/api/auth/login` every 30 seconds and eventually
HTTP 429 rate limiting from UniFi. The 401 retry path handles genuine
session expiry.

## Behind a reverse proxy

The service serves plain HTTP and binds `127.0.0.1` only. nginx runs on the
same host, terminates TLS and proxies to it over loopback, so the whole
deployment is self contained on one machine.

The bind address is the security boundary. ProxyFix trusts a hop count, not
a peer, so on a socket bound to 0.0.0.0 any client could forge
X-Forwarded-For and be believed. Binding loopback removes that: nginx is
the only process that can connect, and it overwrites the header with
$remote_addr rather than appending, so no client-supplied value survives.

ProxyFix is therefore correct at exactly one hop. If another proxy is ever
placed in front, the count must change to match or the logged address
silently reverts to the nearest proxy.

Icons live under /static and are served by Flask directly. nginx also
compresses application/json. That mattered most for `/api/history` when
it returned the whole trace; it now answers deltas, so the large payload
is only the first fetch after a restart.

## Restarting

A restart replaces the process with `execv`: same PID, unit still
active, systemd not involved, and no privilege needed. README.md covers
how to ask for one; this is why it is shaped that way.

A restart is REFUSED while a fault is live but unsaved. The fault exists
only in memory, so a replacement would load `fault.json`, find nothing,
and let Auto start a fan that was stopped for drawing wrong.
`_fault_lock` is held from that check through the exec, because
`_store_fault` holds it across both the memory update and the write - so
without it a restart could land mid-transaction, with the fault live and
the flag not yet set. Holding a lock across `execv` costs nothing: the
process image goes and the lock with it.

The signalled path cannot refuse - `systemctl restart` sends SIGTERM -
so it instead makes one last attempt to write the fault and reports to
the journal if it still cannot.

The in-memory timing state is HANDED OVER. Latches, the minimum-run
lease, the draw-fault timer, the stale-reading clock and the original
start time are passed to the replacement in the environment, which
`execv` preserves and nothing else does. Without it a restart releases a
latched fan and short-cycles the motor the lease protects. A crash or a
`systemctl restart` finds no handover and correctly starts clean, rather
than resuming a lease that expired hours ago. The activity log records
the pair: `Restarting on request`, then `Resumed after restart`.

One trap worth knowing. werkzeug marks its listening socket inheritable
so its reloader can pass the fd to a child, so the first version of this
`execv`d with port 8096 still held and the replacement died with
"Address already in use". Every descriptor above 2 is now marked
close-on-exec immediately before the exec. 0, 1 and 2 are left alone:
they are the journal, and the replacement's own startup logging is the
first thing anyone reads when a restart goes wrong.

## Threads and locks

Three threads can drive the plug: the poll thread, through `tick()`, the
Manual stop and the stale-reading fail-safe; the draw thread, through the
fault detector and the Force on re-assertion; and a settings save that
changes the mode. That is five paths that touch the relay, and every one
of them follows the same rule:

> take `_act_lock`, re-check the mode **and the config generation**,
> then touch the plug.

The re-check is not defensive padding. Checking the mode before taking the
lock and acting after it is a race: Manual's stop runs exactly once and
then clears its flag, so a Force on that had already passed its check
could land afterwards and leave the fan running in the one mode whose
purpose is to stop touching it.

Re-checking the mode alone was not enough, and the gap took a long time
to see. **The settings are re-read under the lock; the CLIENT was not.**
A cycle builds its Protect and plug clients from whatever config is
current when it starts, then makes a request that can take seconds. If
the plug's address changes during that request, re-reading the settings
gives the new rules — applied through a client still pointed at the
**old physical device**. The new rules would command someone else's
plug.

So `CFG` carries a generation, bumped whenever it is replaced.
`_client_snapshot()` returns the clients together with the generation
they were built from, taken under `_cfg_lock` so the pair is coherent,
and every path that can command or read a plug carries that generation
and re-checks it while holding `_act_lock`. That is the right lock
because a settings save swaps `CFG` and bumps the generation while
holding it, so nothing can slip between the check and the switch —
checking outside `_act_lock` narrows the window rather than closing it.

An integer rather than comparing configs: cheap, and it changes even
when a save rewrites identical values, which is the conservative
direction. A discarded cycle costs one poll interval.

The refusals are deliberately not uniform. `_manual_off` does **not**
discharge its one-time promise — the flag stays set and the next cycle
owes the same stop through the right plug. `_force_on` simply skips; it
re-asserts every cycle anyway. `_enforce_stop` retries next cycle, on
the reasoning that one more cycle of a faulted fan already being
watched beats stopping a device that is not ours.

The check also has to come **before the cycle's side effects**, not just
before the switch. `tick()` originally checked only at the actuation
block, so a superseded cycle still refreshed `reading_at` from the old
sensor — resetting a safety timer, and delaying the stale-reading
fail-safe for a newly configured sensor that may be unreachable — read
the old plug's relay and could adopt it as an external switch, and could
send the readings-returned all-clear because the old sensor answered.
The check, the plug read and the whole recording now share one
`_act_lock` hold.

Settings saves commit the mode under the same lock, so an in-flight switch
completes before a new mode takes effect. A save can therefore block for
as long as the plug takes to answer - bounded by its 12s timeout, and only
when the plug is already misbehaving. A slow save is visible and
recoverable; a fan stuck on in Manual is neither.

The ordinary control paths were converted later than the enforcement
ones, and the fail-safe was the costly omission: it re-reads the plug
before switching, so its window spans that call's 12s timeout. A Manual
selected inside it could run its one-time stop and clear the flag before
the fail-safe's switch-on landed, leaving the fan running in Manual with
nothing left to correct it - the same latching failure the enforcement
paths were fixed for, on a path that looked too simple to need it.

The fault is a separate transaction. `_fault_lock` covers the in-memory
value and `fault.json` together, because updating one and then the other
let an acknowledgement land in the gap and be overwritten by a stale
snapshot - clearing the fault on screen while leaving it on disk, to
reappear at the next restart.

```
_cfg_lock   -> _act_lock        settings saves
_cfg_lock   -> _clients_lock    client snapshots
_act_lock   -> _state_lock      five actuation paths
_act_lock   -> _fault_lock      draw decisions and acknowledgements
_fault_lock -> _state_lock      eight paths, including the restart check
```

**`_act_lock -> _fault_lock` is new, and the line above used to say the
two were never held together.** The draw decision needs it: checking the
generation, reading the band, running the out-of-band timer and
committing the fault have to be one transaction, or a settings save
landing in the middle produces a persistent fault quoting limits that no
longer exist — `Draw fault - 99 W outside 10-20 W` after the range was
widened to 90–110 W. The fan is never commanded through the old plug,
but that fault blocks Auto, emails that the fan was stopped, and is
enforced against the new plug on the next cycle.

The rule is about **direction**: no code path acquires `_act_lock`
while already holding `_fault_lock`, and every combined acquisition goes
`_act_lock -> _fault_lock`.

This paragraph used to say "no function that takes `_fault_lock` ever
takes `_act_lock`", which `_draw_check` falsifies — it holds `_act_lock`
and reaches `_fault_lock` through `_store_fault`. That was the wrong way
to state it: what prevents a deadlock is that the pair is never taken in
the reverse order, not that they are never combined.

`_enforce_stop`, the log, the email and the fault-stop confirmation all
run after the hold is released, because `_enforce_stop` takes `_act_lock`
itself and the rest must not extend a hold a settings save is waiting
on.

The cost is that a settings save can now wait on a fault write as well
as on a plug call. Bounded, visible, and preferable to a fault raised
against limits that have been replaced.

`_client_snapshot()` takes `_cfg_lock` and then `_clients_lock` - it
does **not** take `_act_lock`, and an earlier version of this table said
it did.

Nothing takes `_cfg_lock` while holding `_act_lock`. The generation is
therefore read as a plain integer under `_act_lock` rather than by
re-entering `_cfg_lock`, which would invert that order.

The fault paths are: raising one, acknowledging one, confirming the
stop, the retry after a failed write, the last-chance write on a
signalled stop, a requested restart - which takes the lock not to write
but to stop a restart landing midway through someone else's fault
transaction - and the two that stamp an alert as attempted and as
delivered.

The count grew as those paths were added, and this line has been wrong
about it once already: it said six after the email annotations brought
it to eight. **If it looks wrong, count them** rather than trusting the
number here:

```
grep -nE '^[[:space:]]*(with _fault_lock:|if not _fault_lock\.acquire\()' controller.py
```

That returns exactly the eight. Two looser recipes do not, and both were
suggested here before this one. A plain grep for the name matches every
mention including this paragraph's — dozens, and the figure moves every
time someone edits a comment; a draft of this very sentence quoted a
count that was stale by the time it was saved. And
`with _fault_lock\|_fault_lock.acquire` returns one too many, because
`_persist_fault_locked` mentions it in a docstring to say its caller
already holds it. Anchoring to the start of the line is what excludes
prose.

`_state_lock` is an RLock because nested acquisition previously deadlocked
the poll thread. Nothing takes `_act_lock` or `_fault_lock` while holding
`_state_lock`, and keeping it that way is what makes the ordering above
deadlock-free.

Ordinary event and history writes stay outside both, so they cannot
extend a hold something else is waiting on. Two deliberate exceptions:
fault persistence happens INSIDE `_fault_lock`, because the memory copy
and `fault.json` are one transaction and splitting them let an
acknowledgement land in the gap; and a requested restart holds
`_fault_lock` from its final check through the `execv`, which costs
nothing because the process image goes and the lock with it.

## State

- `config.json` - settings, written atomically, mode 0600. Not in git.
- `history.json` - readings from the last 24 hours, pruned by timestamp so
  the window does not change with the poll interval. Written at most every
  5 minutes rather than every poll, and a flush is attempted on SIGTERM
  so a restart usually loses nothing. That write can still fail, and
  says so in the journal rather than leaving an unexplained gap. Not in git.
- `events.json` - the activity log, written on each entry and reloaded
  at startup so switching history survives a restart. 300 kept, the
  most recent 30 returned to the UI each poll. Not in git.
- `fault.json` - a latched draw fault. Raised, confirmed and
  acknowledged as one transaction with the in-memory copy, never as two
  steps. Persisted rather than held in memory because a restart would
  otherwise skip the acknowledgement it is waiting for and re-arm a fan
  already judged faulty. Not in git.
- `manual-off.pending` - a one-time Manual stop that has been promised
  and not yet made. Written before the mode is committed and removed
  once the plug confirms the stop, so a crash in between does not leave
  the fan running in the mode whose purpose is to stop it. Not in git.

`config.json` and `fault.json` are written durably: the file is fsynced
and then its directory, so the rename survives a power cut rather than
merely a crash. `history.json` and `events.json` are not - they are
written far more often, and what is lost is a few minutes of the graph
or a few lines of the activity log. Neither controls the fan.
- In-memory only - latch state, the minimum-run lease, the draw-fault
  timer and the stale-reading clock. Deliberately not persisted: after a
  crash or a power cut there is no reason to believe a lease from before
  it. A requested restart is the exception and hands them over; see
  Restarting above.

Fan state is re-read from the plug every poll rather than trusted
from memory, so switching it from Google Home or the plug itself is
noticed and does not desynchronise the controller.

## Why a Tasmota plug

The fan previously ran from a Hue smart plug via the bridge. The Tasmota
plug is a direct HTTP call with explicit `Power ON` / `Power OFF` rather
than a toggle, and every switch is confirmed from the device's own reply,
so a rejected command cannot be recorded as a success.

It also meters. Draw from the HLW8032 is polled on its own 5 second
cadence rather than with the sensor, since the Protect bootstrap is the
expensive call and tying watts to `poll_seconds` left it up to 30 seconds
stale while the UI refreshed every 5. This
separates "the relay closed" from "the fan is actually running" - a
seized motor or unplugged fan becomes visible rather than silently doing
nothing while CO2 climbs.

Credentials go in the query string because Tasmota's `/cm` accepts no
other form. The URL is never logged, and errors report only the exception
type, since `requests` includes the full URL in its exception text.
