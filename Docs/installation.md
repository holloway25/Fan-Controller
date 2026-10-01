# Installation and first setup

Use Python 3.10 or newer on x86-64 Debian/Ubuntu or a 64-bit Debian-based
Raspberry Pi 3B+ image. See [ARM notes](arm.md) for board-specific limitations.
Install Git and the distribution's `python3-venv` package if missing.

## Install with systemd

Run as the ordinary account that will own the checkout and service:

```sh
git clone https://github.com/holloway25/Fan-Controller.git
cd Fan-Controller
./install.sh --unit
```

The installer creates `venv`, installs pinned dependencies, creates
`config.json` from the example if absent, and sets its permissions to 0600.
It generates a unit using the current account and checkout path. `--unit`
uses sudo to install and enable that unit, but does not start the service.
Without `--unit`, it prints the privileged commands for you to run.

Re-running the installer preserves existing configuration and does not restart
a running controller. It regenerates the local unit; preserve any deliberate
site-specific customisations before re-running it.

The example starts in Manual with power monitoring off. Either fill in
`config.json` before starting, or start with the example and use the dashboard:

```sh
sudo systemctl start office-fan-controller
```

Run an SSH tunnel on your own computer and open `http://127.0.0.1:8096`:

```sh
ssh -L 8096:127.0.0.1:8096 <user>@<server>
```

Flask is not exposed on the LAN. Manual attempts a one-time stop, so even
initial setup must not target a plug already controlled by another instance.

## Configure and verify

1. Create a local UniFi user without two-factor authentication; cloud SSO does
   not work with this private Protect endpoint.
2. Enter the Protect details and select **Find sensors**. Select the intended
   sensor and confirm its readings.
3. Enter the Tasmota plug address and web credentials, then select **Test plug**.
   Its HTTP command API must be enabled. Confirm this is the correct device.
4. Set enabled triggers, on/off thresholds, minimum run time and, if needed,
   the stale-reading fail-safe. Only then select **Auto**.
5. Before enabling power monitoring, measure the fan's normal running draw and
   configure an appropriate band and fault delay.
6. If alerts are needed, configure **Fault Handling** and send a test email.

See [configuration](configuration.md) for validation, defaults and alerts,
and [control logic](control-logic.md) before using overrides or clearing faults.
Each device keeps its own settings; the example is not a production profile.

## HTTPS and access control

Use [the nginx example](../deploy/nginx.conf.example) on the controller host.
Replace its hostname and certificate paths, enable the site, check `nginx -t`,
then reload nginx. Do not expose port 8096 externally.

UI authentication is optional and off by default. Read-only dashboard data
remains public even when a password is enabled. See
[UI authentication](configuration.md#ui-authentication) to set or clear it.
Browser API changes must also come from the same origin; this is not a
replacement for authentication or a trusted network.

## Manual installation

If not using the installer:

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
cp office-fan-controller.service.example office-fan-controller.service
# Edit User, Group, WorkingDirectory and ExecStart for this installation.
sudo cp office-fan-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable office-fan-controller
# Check config.json and the target plug before starting.
sudo systemctl start office-fan-controller
```

Use a fresh installation directory; do not overwrite an existing live config.
Quote the executable path in `ExecStart` if it contains spaces. Without systemd,
`./run.sh` is available, but never run it alongside the systemd service.
