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

The emulator starts OFF, reports 23 W when ON and 0 W when OFF. Settings are
in-memory and reset whenever it restarts. Keep development in Manual with
power monitoring off initially; change development settings deliberately when
testing Auto, Force on or draw protection. No physical load is involved.

## Adjust the simulated plug

The local control page is `http://127.0.0.1:8981/`. For a remote development
machine, forward that loopback port through SSH and open the forwarded address
locally. Do not expose the emulator through a public/LAN reverse proxy.

Alternatively, on the development machine:

```sh
./venv/bin/python tools/plug_emulator.py show
./venv/bin/python tools/plug_emulator.py set --json '{"on_watts":100}'
./venv/bin/python tools/plug_emulator.py set --json '{"relay":false}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"http_error","target":"power"}'
./venv/bin/python tools/plug_emulator.py set --json '{"failure":"none","delay_seconds":0}'
```

Settings: `relay` (Boolean), `on_watts` and `off_watts` (0–100000),
`delay_seconds` (0–20), `target` (`all`, `relay`, `power`, `switch`), and
`failure` (`none`, `http_error`, `malformed`, `reject_switch`). The delay and
failure apply only to the chosen operation. A rejected switch leaves the
relay unchanged. A delay beyond the controller's timeout simulates a slow
HTTP reply, **not** a TCP connection timeout. Request counts contain no
credentials. The control API accepts JSON and rejects cross-origin browser
writes. Tasmota credential parameters are ignored and never logged.

Run its isolated checks on development:

```sh
./venv/bin/python tests/test_plug_emulator.py
```

These checks use an ephemeral loopback server and the controller's actual
HTTP transport; they do not access configured hardware or live settings.
