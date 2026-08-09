# Office Fan Controller

Reads CO2, temperature, humidity and PM2.5 from a UniFi Protect
UP-AirQuality sensor and switches a fan through a Tasmota smart plug,
with hysteresis on CO2, temperature or humidity - each an independent
trigger you can enable on its own. The plug's energy meter confirms the
fan is actually drawing current. Web UI is served over
HTTPS by nginx on the same host; Flask itself listens on 127.0.0.1 only.

    sudo systemctl status office-fan-controller
    sudo systemctl restart office-fan-controller
    journalctl -u office-fan-controller -f

## Setup

1. In UniFi, create a **local** user with no two-factor. Cloud SSO logins
   will not work against the private Protect API.
2. Open the dashboard and fill in the sensor card, then
   **Find sensors**. Each sensor is listed with its CO2 reading, or
   "no CO2 field" if Protect is not publishing one.
3. Open **Fan plug**, enter the plug's address and web password,
   then **Test plug**. It reports relay state and current draw.
   The plug's HTTP command API must be enabled.
4. Set thresholds, then select **Auto** to hand control to them.

## How it switches

Each enabled trigger latches on above its own on level and releases
below its own off level, and the fan runs while any of them is latched.
The gap between a rule's two levels prevents short-cycling. A freshly
started fan is also held on for the minimum run time. Force on overrides
the thresholds entirely; Manual switches the fan off once and then leaves
it to Google Home or the plug itself.

Manual and Force on are applied before the sensor is read, so both work
when the console is down, the payload has stopped resolving, or no sensor
has been chosen yet - Force on is the override reached for precisely when
the reading cannot be trusted.

The API refuses any save where the on level is not above the off level,
since that leaves the fan running forever, and refuses the whole request
rather than the offending field.

## When it breaks

Protect 7.1's documented API omits air-quality metrics entirely, so this
reads the private /proxy/protect/api/bootstrap endpoint. That is
undocumented and can change on any firmware update.

CO2 is found by searching the payload for a numeric field whose key looks
like CO2, rather than a fixed path, so a rename survives. If it stops
resolving, open /api/debug/sensor to see the live payload and adjust
CO2_HINTS in controller.py.

Once Ubiquiti publishes these fields properly, this can be replaced with a
few lines against the documented API.

## Documentation

- [Docs/architecture.md](Docs/architecture.md) - design, and why the
  private Protect API is used
- [Docs/control-logic.md](Docs/control-logic.md) - modes, latching,
  minimum run time
- [Docs/configuration.md](Docs/configuration.md) - every setting
- [Docs/dashboard.md](Docs/dashboard.md) - what the screen shows, and what
  each colour means
- [Docs/api.md](Docs/api.md) - HTTP endpoints
- [Docs/troubleshooting.md](Docs/troubleshooting.md) - known failure modes
- [deploy/nginx.conf.example](deploy/nginx.conf.example) - reference nginx
  site config, since Flask binds loopback and needs a proxy

## Install

    git clone https://github.com/holloway25/Fan-Controller.git
    cd Fan-Controller
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    cp config.example.json config.json

Run it under systemd, editing the paths and user in the unit first:

    sudo cp office-fan-controller.service /etc/systemd/system/
    sudo systemctl enable --now office-fan-controller

`./run.sh` starts it without systemd, using `setsid --fork` so it survives
an SSH session ending.

Flask binds `127.0.0.1` only, so nothing reaches it except a reverse proxy
on the same host. Before that proxy exists, reach the UI for first-time
setup with an SSH tunnel:

    ssh -L 8096:127.0.0.1:8096 <user>@<server>

then browse to http://127.0.0.1:8096 on your own machine.


## Upgrading

Pulling changes is not always enough. The installed unit at
`/etc/systemd/system/office-fan-controller.service` is a **copy**, not a
symlink to the one in the repo, so a change to `ExecStart` reaches the
repo without reaching the running service. Nothing breaks immediately -
the process already has its code loaded - and the failure appears at the
next restart or reboot, which then will not start.

After any pull that touches the unit file, reinstall it:

    sudo cp office-fan-controller.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl restart office-fan-controller

The 2026-08 release renamed `co2fan.py` to `controller.py`, so upgrading
across it requires exactly that. Verify with:

    systemctl is-active office-fan-controller
    journalctl -u office-fan-controller -n5

The journal reports `Serving Flask app 'controller'`. The name comes from
the module, so it is the quickest confirmation the new file is running.

Do not run `./run.sh` until the unit is reinstalled. It guards against a
double start by looking for the running process by name, and a stale unit
means it will not recognise what is running and will start a second
instance - two processes driving the same plug and both writing
`config.json`.

## Restarting

Ask the controller to restart itself. No privilege needed:

    ./restart.sh

It writes `restart.request`, waits for the controller to take it, and
reports what happened. Doing it by hand is the same thing without the
reporting:

    touch restart.request

The watcher checks every two seconds, then replaces its own process with
`execv`. The PID does not change and systemd is not involved. The
activity log shows `Restarting on request` followed by `Resumed after
restart`; if the second line is missing, the exec failed and systemd
caught it.

A restart carries the state that exists only in memory - the per-rule
latches, the minimum-run lease, the draw-fault timer and the
stale-reading clock - to the replacement process, so a restart does not
release a latched fan or short-cycle the motor. That handover rides in
the environment, which survives `execv` and nothing else, so a crash or
a `systemctl restart` correctly starts clean instead of resuming a lease
that expired hours ago.

**A restart is refused while a fault is live but unsaved.** The
dashboard shows `Fault not saved to disk` in that state. The fault
exists only in memory, so a replacement process would load `fault.json`,
find nothing, and let Auto start a fan that was stopped for drawing
wrong. `restart.sh` exits 1 and says so. Clear the fault, or fix
whatever is stopping `fault.json` being written, and try again.

`sudo systemctl restart office-fan-controller` still works and is the
fallback if the watcher is not running. It cannot refuse: a signalled
shutdown makes one last attempt to write the fault and then goes ahead
either way. If that attempt fails the journal says so in capitals, and
the fan will not be held off on the next start - check the draw before
running it. Prefer `./restart.sh` when the dashboard shows an unsaved
fault.

Otherwise restarting is safe: `history.json` is flushed on `SIGTERM`,
and `config.json`, `events.json` and `fault.json` are untouched. A
saved draw fault survives deliberately.

## Tests

    ./venv/bin/python tests/run_all.py

No dependencies beyond the venv. Each module imports `controller.py`
against a throwaway directory, so `config.json`, `history.json`,
`events.json` and `fault.json` are never read or written, and no plug or
Protect console is contacted. Safe to run against a live install.

- `test_control.py` - `decide()`, the per-rule latches, the min-run lease
- `test_data.py` - `_coerce` validation, `prune_hist`, `find_metric`
- `test_concurrency.py` - the actuation paths under `_act_lock`
- `test_failures.py` - persistence errors and malformed request bodies
- `test_restart.py` - the restart trigger, the handover, and the
  last-chance fault write on shutdown

`test_restart.py` includes one test that needs Linux `/proc/self/fd` and
will not run on Windows. It covers descriptor inheritance across the
`execv`, which is what stopped the replacement binding port 8096 the
first time - so run the suite on the deployment host before trusting a
restart change.

The concurrency tests gate the interleaving with events rather than
sleeping. Timing alone does not reproduce these races: `_manual_off` does
its own plug read, which reorders the stop to land last and hides the bug.
## Serving it over HTTPS

Flask binds `127.0.0.1`, so a reverse proxy on the same host is required
for network access.
[deploy/nginx.conf.example](deploy/nginx.conf.example) is a working site
config - copy it to `/etc/nginx/sites-available/`, replace `example.com`,
and symlink it into `sites-enabled`.

It expects a certificate at the usual certbot path. Any issuance method
works; DNS-01 suits a host with no inbound port 80, e.g. with the
Cloudflare plugin:

    sudo apt install nginx certbot python3-certbot-dns-cloudflare
    sudo certbot certonly --dns-cloudflare \
      --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
      -d example.com

Scope the API token narrowly - DNS edit rights on the one zone are enough,
and a zone-wide token can mint a certificate for any name you host. Add a
deploy hook so nginx reloads on renewal, or it keeps serving the old
certificate from memory until it expires:

    printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee \
      /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
    sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

## Authentication

Off by default. Setting a password protects everything that changes state
while leaving the dashboard readable, so a wall display needs no login:

    curl -X POST https://<your-host>/api/ui/password \
      -H 'Content-Type: application/json' -d '{"password":"..."}'

Post an empty password to disable it again. See Docs/configuration.md.
