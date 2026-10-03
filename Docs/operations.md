# Operations and upgrades

## Check the running service

```sh
systemctl status office-fan-controller
journalctl -u office-fan-controller -n 100
curl -s http://127.0.0.1:8096/api/state
```

Check the dashboard for fresh readings, the intended mode, draw-loop health
and any fault. A relay reported on does not by itself prove the fan is turning.
See [troubleshooting](troubleshooting.md) when status and hardware disagree.

## Restart without dropping control state

From the checkout, as the service account:

```sh
./restart.sh
```

The helper asks the controller to replace itself and verifies a new process
generation is answering. It preserves latches, the current run's trigger
record, the minimum-run lease and safety timers. It refuses while a live fault
has not been saved to disk; repair storage or safely resolve the fault rather
than bypassing this protection.

A restart can also be temporarily refused while settings, a plug action or a
fault write are in progress. The helper reports the logged reason; retry later
for a busy refusal rather than clearing a fault that may not exist.

`sudo systemctl restart office-fan-controller` is a fallback or a required step
when changing systemd settings. It drops transient latches and timing state;
a saved draw fault remains persistent. Schedule it with awareness of the fan's
current demand and minimum-run lease. See [restart model](architecture.md#restart-model).

## Apply a reviewed update

Before updating production, review and test the change on development using
mocked devices or an isolated test plug. Prevent development from reaching the
production plug, for example through blocked network access, before starting
the controller or exercising controls. Manual is not isolation: selecting it
still commands a one-time stop, even with power monitoring disabled. Never
use a shared production plug as a development test device.

Check that the checkout is clean and record the old revision. Back up live
configuration and local unit files outside the repo in an owner-only directory.
Do not overwrite local changes or copy settings between installations.

```sh
git status --short
git rev-parse HEAD
git pull --ff-only
```

Compare the old and new revisions before deciding which steps are needed:

- **Docs or metadata only:** no service restart or dependency installation.
- **Application changes, dependencies and unit unchanged:** use `./restart.sh`.
- **`requirements.txt` changed:** run `./install.sh` before restarting. It
  installs dependencies and regenerates the local unit; review local unit
  customisations before doing so.
- **Systemd example changed:** merge the new settings into the concrete local
  unit, preserving its account, paths and intentional customisations. Install
  it and reload systemd, then perform a systemd restart to activate the settings:

  ```sh
  sudo cp office-fan-controller.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl restart office-fan-controller
  ```

After an application update, confirm the service is active, readings advance,
loop health is normal, and local settings remain correct. Review the journal
and HTTPS dashboard. Avoid a full test suite or package installation on a
production board unless the change actually requires it.

## Local data

### Development plug interlock

For a running development dashboard that must not contact a real plug, create
an empty `plug-access.disabled` file beside `controller.py`, then use
`./restart.sh`. This startup-only interlock disables automatic relay/meter
polling and blocks all HTTP plug requests, including dashboard tests and
switching. Sensor polling and the dashboard remain available; relay and power
values are unknown, not simulated. `/api/state` reports
`plug_polling_enabled: false`. Manual and disabled power monitoring alone do
not disable polling or isolate hardware.
Sensor readings continue in every mode while interlocked; any outstanding
Manual stop promise is retained, not executed or discharged.

This is for development only: it also prevents fault-stop commands and is
unsuitable for an installation controlling a real fan. To restore plug access,
remove the marker and restart, only after checking the configured target,
mode, faults and any outstanding Manual stop promise. The marker is ignored
by Git and must not be copied to a production installation.

Normal meter polling is every five seconds. Independent relay polling occurs
immediately at startup or after a configuration-generation change, then every
30 seconds regardless of sensor health. Successful sensor polls also read
the relay before making control decisions. These are separate from the
configurable sensor polling interval.
Before latching a new draw fault, the controller checks the relay again. A
confirmed external stop is adopted without faulting; an unreadable relay is
not assumed off and does not suppress protection.

Settings and runtime files stay outside Git. See the
[runtime state table](architecture.md#runtime-state) for their purpose and
durability. Protect credentials in backups just as carefully as live files.

Do not delete `fault.json` to suppress a warning. Inspect the cause and use
the normal acknowledgement flow once safe. Restoring a backup or rolling back
code also requires checking configuration compatibility and outstanding faults;
an older controller may not understand newer safety state.
