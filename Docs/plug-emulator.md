# Development plug emulator

`tools/plug_emulator.py` is a software-only test plug. It implements the
controller's Tasmota commands (`Power`, `Power ON/OFF`, `Status 8`), not the
Tasmota firmware or its network stack. It cannot reproduce hardware TCP faults
or prove a physical fan is safe. It never forwards requests to another device.

Start it with the existing Python environment; no extra packages are needed:

```sh
./venv/bin/python tools/plug_emulator.py serve
```

It listens only on `127.0.0.1:8981`. Configure a **development** controller's
plug address to that value, clearing real plug credentials. Keep production
settings separate. Remove a development plug-access interlock only after
verifying the saved address targets the local emulator, never a real plug.
The controller must run on the same machine as the emulator.

The emulator starts OFF and reports 0 W when OFF. Its default fan profile
starts at 27 W and tapers to 23 W over the first three seconds after switching ON,
gradually reducing to 22 W over 30 minutes. These are adjustable example
values, not measurements of a particular fan. The warm draw can also be set
to 20 W, or another value, on the control page. Disable the fan profile to
use a fixed `on_watts` value for sustained low/high-draw fault tests.
Settings are
in-memory and reset whenever it restarts. Keep development in Manual with
power monitoring off initially; change development settings deliberately when
testing Auto, Force on or draw protection. No physical load is involved.

## Adjust the simulated plug

The local control page is `http://127.0.0.1:8981/`. For a remote development
machine, forward that loopback port through SSH and open the forwarded address
locally. Keep the emulator listener on loopback. If an operator chooses LAN
access, a separate, trusted reverse proxy may expose **only** `/` and
`/api/emulator`, restricted to authorised local clients. It must reject
unexpected Host headers and cross-origin/fetch-site browser requests before
adapting headers for the loopback upstream. Do not proxy `/cm`, expose the
whole server, or publish the controls to the internet. Anyone allowed to use
these controls can change the simulated relay and load; there is no login.

Alternatively, on the development machine:

```sh
./venv/bin/python tools/plug_emulator.py show
./venv/bin/python tools/plug_emulator.py set --json '{"on_watts":100}'
./venv/bin/python tools/plug_emulator.py set --json '{"relay":false}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"http_error","target":"power"}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"outage","target":"all","outage_seconds":30}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"timeout","target":"power","timeout_seconds":15}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"none","delay_seconds":0}'
```

For a sustained fault, use `{"fan_profile":false,"on_watts":100}` rather
than changing normal running watts while the warm profile remains enabled.

Settings: `relay` and `fan_profile` (Booleans), `on_watts`, `off_watts`,
`startup_watts` and `warm_watts` (whole watts, 0–100000), `startup_seconds` (0–60),
`warm_after_minutes` (0–1440, warming duration after startup),
`delay_seconds` (0–20), `timeout_seconds` (0.1–60, default 15),
`outage_seconds` (0.1–600, default 30), `target` (`all`, `relay`, `power`, `switch`), and
`failure` (`none`, `http_error`, `malformed`, `reject_switch`, `timeout`,
`drop_connection`, `outage`). The delay and
failure apply only to the chosen operation. A rejected switch leaves the
relay unchanged. A delay beyond the controller's timeout simulates a slow
HTTP reply, **not** a TCP connection timeout. Request counts contain no
credentials. The control API accepts JSON and rejects cross-origin browser
writes. Tasmota credential parameters are ignored and never logged.
The control page's live status includes current watts, phase and run duration.
For slow replies use `delay_seconds`. `timeout` accepts the HTTP connection
but holds the request without a reply for `timeout_seconds`, then closes it;
choose a hold longer than the client's read timeout. It never executes a
timed-out switch later. `drop_connection` closes selected requests immediately.
`outage` drops selected requests for `outage_seconds`, then automatically
resumes normal replies. Selecting `outage` again or changing its duration
starts a new window; unrelated setting changes do not extend it. Controls stay
available throughout, and the simulated relay retains its last state.
Live status reports `active_failure` and `outage_remaining_seconds`.
These are HTTP/read failures, **not** a reproduction of TCP SYN loss,
source-port reuse, or the production `ConnectTimeout`. They test controller
error handling and recovery, not the suspected hardware/network cause.
Repeated ON commands or settings saves do not restart the motor profile;
only an OFF-to-ON transition does. Timing uses an elapsed clock, not wall time.
Reported power is rounded to whole watts, including during the load taper.

Run its isolated checks on development:

```sh
./venv/bin/python tests/test_plug_emulator.py
```

These checks use an ephemeral loopback server and the controller's actual
HTTP transport; they do not access configured hardware or live settings.

To simulate contacts remaining closed despite an OFF report, keep the relay
OFF and set `off_watts` to a load value such as 23. With controller power
monitoring enabled this should latch FC-007 after settling and the configured
fault delay, leave the stop unconfirmed, and start bounded OFF retries.
It sends a first-retry warning, an FC-008 hard-fault alert after three failed
attempts, and a final hard-fault update if the last three fail. See
[power monitoring](configuration.md#plug) for timing and restart behaviour.
Restore `off_watts`
to 0 before acknowledging the fault. To simulate a rejected OFF command,
start the emulated relay ON and use `reject_switch` targeting `switch`.
Enter Manual in the controller to test FC-009: the failed original OFF must
latch a fault, then use the same bounded retries without repeated Manual
polls making extra commands. Restore normal emulator responses and OFF/0 W
to check recovery before acknowledging the generated test fault.
