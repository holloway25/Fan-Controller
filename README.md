# Office Fan Controller

Controls a ventilation fan from a UniFi Protect UP-AirQuality sensor and a
Tasmota smart plug. CO2, temperature and humidity can trigger the fan
independently, with hysteresis and a minimum run time. The plug's power meter
can also detect a fan that is not drawing the expected current.

Supported targets:

- Debian or Ubuntu on x86-64
- Raspberry Pi 3B+ using a 64-bit Debian-based image

Flask listens only on `127.0.0.1:8096`. Use nginx on the same host for HTTPS
and network access.

## Install

```sh
git clone https://github.com/holloway25/Fan-Controller.git
cd Fan-Controller
./install.sh
```

Run the installer as the ordinary user that will run the service, not as root.
It creates the virtual environment, installs pinned dependencies, creates
`config.json` with mode 0600, and generates a systemd unit for the current user
and checkout path.

The installer is safe to re-run and does not overwrite `config.json` or restart
a running controller. It prints the required privileged commands;
`./install.sh --unit` runs those commands.

To install manually:

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
cp office-fan-controller.service.example office-fan-controller.service
# Edit User, Group, WorkingDirectory and ExecStart.
sudo cp office-fan-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now office-fan-controller
```

Before nginx is configured, use an SSH tunnel:

```sh
ssh -L 8096:127.0.0.1:8096 <user>@<server>
```

Then open `http://127.0.0.1:8096` locally.

## Setup

1. Create a local UniFi user without two-factor authentication. Cloud SSO does
   not work with the private Protect endpoint.
2. Open the dashboard, enter the Protect details, and select **Find sensors**.
3. Enter the Tasmota plug address and web password, then select **Test plug**.
   The Tasmota HTTP command API must be enabled.
4. Configure thresholds and select **Auto**.
5. If alerts are required, configure and test them in **Fault Handling**.

## Control behaviour

- **Auto** applies the enabled CO2, temperature and humidity rules.
- **Manual** switches the fan off once, then leaves the plug alone.
- **Force on** keeps the fan on even if Protect is unavailable.

Each enabled rule latches above its on threshold and releases at or below its
off threshold. The fan runs while any rule is latched. A missing reading does
not release an existing latch. The minimum run time prevents short-cycling.

Power monitoring is off by default. When enabled, a continuous draw outside
the configured band stops the fan and latches a fault. Force on can be used to
test a repaired fan; clear the fault only after confirming the draw is healthy.

See [Docs/control-logic.md](Docs/control-logic.md) for the complete behaviour.

## Service operation

```sh
systemctl status office-fan-controller
journalctl -u office-fan-controller -f
./restart.sh
```

`restart.sh` asks the running controller to replace itself and confirms that a
new process is answering. It preserves transient latch and timing state. It
refuses to restart while a live fault has not been saved to disk.

Use `sudo systemctl restart office-fan-controller` only as a fallback. A
systemd restart does not preserve transient latch or lease state, although a
saved draw fault remains persistent.

`./run.sh` is available where systemd is unavailable. Do not run it alongside
the systemd service because two controllers could drive the same plug.

## Upgrading

```sh
git pull --ff-only
./install.sh
./restart.sh
```

The installed systemd unit is a copy. If
`office-fan-controller.service.example` changes, update the local
`office-fan-controller.service`, reinstall it, and reload systemd:

```sh
sudo cp office-fan-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart office-fan-controller
```

Verify the result with `systemctl is-active office-fan-controller` and the
service journal.

## HTTPS and authentication

[deploy/nginx.conf.example](deploy/nginx.conf.example) is the reference nginx
site. Replace its example hostname and certificate paths, enable the site, and
reload nginx after `nginx -t` succeeds.

UI authentication is off by default. Set a password with:

```sh
curl -X POST https://<your-host>/api/ui/password \
  -H 'Content-Type: application/json' \
  -d '{"password":"your-password"}'
```

An empty password disables authentication. Read-only dashboard endpoints stay
open so a wall display does not need credentials. State-changing and diagnostic
endpoints require authentication once a password is set.

## Private Protect API

Air-quality readings are obtained from Protect's private
`/proxy/protect/api/bootstrap` endpoint because the documented sensor API does
not provide them. This endpoint can change with Protect firmware. If metrics
stop resolving, see [Docs/troubleshooting.md](Docs/troubleshooting.md).

## Alerts and fault codes

Alerts are disabled until configured. Each operational event uses a stable
fault-code series:

| Series | Meaning |
|---|---|
| `FC-0xx` | Alarm email delivered |
| `FC-1xx` | All-clear email delivered |
| `FC-2xx` | Operational failure; no email delivered |
| `FC-3xx` | Informational event; no failure |

The complete table and delivery rules are in
[Docs/configuration.md](Docs/configuration.md).

## Tests

Tests are development and CI tools; the installer does not run them.

```sh
./venv/bin/python tests/run_all.py
```

The suite uses temporary state and does not contact Protect or the plug. Some
tests require Linux, Node/jsdom, or shellcheck and may skip when those tools are
absent. GitHub Actions runs the full suite on supported x86 Python versions and
an ARM64 runner, fails on unexpected skips, and runs the curated mutation
checks on one Python version:

```sh
./venv/bin/python tools/mutate.py
```

The mutation checks deliberately break important production behaviour and
confirm that the named test fails. Browser layout still requires visual testing
because jsdom has no layout engine.

The suite currently contains these modules. The list is intentionally limited
to filenames; the tests themselves are the source of truth for their coverage:

- `test_actuation.py`
- `test_chart_ranges.py`
- `test_concurrency.py`
- `test_control.py`
- `test_daily.py`
- `test_dashboard_source.py`
- `test_data.py`
- `test_durability.py`
- `test_email.py`
- `test_failures.py`
- `test_fault_codes.py`
- `test_history.py`
- `test_install_sh.py`
- `test_js.py`
- `test_log_classes.py`
- `test_log_formats.py`
- `test_mutate_tool.py`
- `test_plug_parse.py`
- `test_render_canary.py`
- `test_restart_sh.py`
- `test_restart.py`
- `test_secret_scanner.py`
- `test_shellcheck.py`
- `test_structure.py`

## Runtime files

The following local files are excluded from Git:

- `config.json` — settings and credentials
- `history.json` — rolling 24-hour readings
- `daily.json` — daily low/high summaries
- `events.json` — activity history
- `fault.json` — persistent draw fault
- `manual-off.pending` — a promised Manual stop not yet confirmed

Do not commit live configuration, credentials, private hostnames, addresses, or
runtime state.

## Documentation

- [Architecture](Docs/architecture.md)
- [Control logic](Docs/control-logic.md)
- [Configuration and alerts](Docs/configuration.md)
- [Dashboard](Docs/dashboard.md)
- [HTTP API](Docs/api.md)
- [Troubleshooting](Docs/troubleshooting.md)
- [Raspberry Pi and ARM](Docs/arm.md)
