# Office Fan Controller

Reads CO2, temperature, humidity and PM2.5 from a UniFi Protect
UP-AirQuality sensor and switches a fan through a Tasmota smart plug.
CO2, temperature and humidity are independent triggers, each with its own
hysteresis, and each can be enabled on its own. The plug's energy meter
confirms the fan is actually drawing current. Flask listens on
`127.0.0.1` only; nginx on the same host serves the UI over HTTPS.

    systemctl status office-fan-controller
    journalctl -u office-fan-controller -f
    ./restart.sh

## Install

    git clone https://github.com/holloway25/Fan-Controller.git
    cd Fan-Controller
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    cp config.example.json config.json

Run it under systemd, editing the paths and user in the unit first:

    sudo cp office-fan-controller.service /etc/systemd/system/
    sudo systemctl enable --now office-fan-controller

`./run.sh` starts it without systemd, using `setsid --fork` so it
survives an SSH session ending.

Flask binds `127.0.0.1` only, so nothing reaches it except a reverse
proxy on the same host. Before that proxy exists, reach the UI for
first-time setup with an SSH tunnel:

    ssh -L 8096:127.0.0.1:8096 <user>@<server>

then browse to http://127.0.0.1:8096 on your own machine.

## Setup

1. In UniFi, create a **local** user with no two-factor. Cloud SSO
   logins will not work against the private Protect API.
2. Open the dashboard, fill in the sensor card, then **Find sensors**.
   Each sensor is listed with its CO2 reading, or "no CO2 field" if
   Protect is not publishing one.
3. Open **Fan plug**, enter the plug's address and web password, then
   **Test plug**. It reports relay state and current draw. The plug's
   HTTP command API must be enabled.
4. Set thresholds, then select **Auto** to hand control to them.

## How it switches

Each enabled trigger latches on above its own on level and releases
below its own off level, and the fan runs while any of them is latched.
The gap between a rule's two levels prevents short-cycling, and a
freshly started fan is held on for the minimum run time. Force on
overrides the thresholds entirely; Manual switches the fan off once and
then leaves it to Google Home or the plug itself.

Manual and Force on are applied before the sensor is read, so both work
when the console is down, the payload has stopped resolving, or no
sensor has been chosen yet - Force on is the override reached for
precisely when the reading cannot be trusted.

A missing reading does not release a latch. The room was hot, the sensor
went quiet, and nothing since suggests it cooled, so releasing would
stop the fan on the strength of not knowing. A reading that returns
below the off level releases normally, and a rule that is switched off
releases immediately either way.

The API refuses any save where a rule's on level is not above its off
level, since that leaves the fan running forever, and refuses the whole
request rather than the offending field. CO2's levels are checked on
every save; temperature and humidity only when their trigger is
enabled.

## When it breaks

Protect 7.1's documented API omits air-quality metrics entirely, so this
reads the private `/proxy/protect/api/bootstrap` endpoint. That is
undocumented and can change on any firmware update.

CO2 is found by searching the payload for a numeric field whose key
looks like CO2, rather than a fixed path, so a rename survives. If it
stops resolving, open `/api/debug/sensor` to see the live payload and
adjust `CO2_HINTS` in `controller.py`.

Once Ubiquiti publishes these fields properly, this can be replaced with
a few lines against the documented API.

## What it does not do

It does not push, and there is no mobile app or home automation
integration. Alerting is email only.

Alerts are off until configured in the **Fault Handling** card, and an
install that has not filled them in still tells you nothing: a draw
fault stops the fan and says so on the dashboard and in the activity
log, and nowhere else. What sends, and what deliberately does not, is
in `Docs/configuration.md`.

## Documentation

- [Docs/architecture.md](Docs/architecture.md) - design, and why the
  private Protect API is used
- [Docs/control-logic.md](Docs/control-logic.md) - modes, latching,
  minimum run time
- [Docs/configuration.md](Docs/configuration.md) - every setting
- [Docs/dashboard.md](Docs/dashboard.md) - what the screen shows, and
  what each colour means
- [Docs/api.md](Docs/api.md) - HTTP endpoints
- [Docs/troubleshooting.md](Docs/troubleshooting.md) - known failure
  modes
- [Docs/arm.md](Docs/arm.md) - running on a Raspberry Pi: what has been
  measured, and how to repeat it
- [deploy/nginx.conf.example](deploy/nginx.conf.example) - reference
  nginx site config, since Flask binds loopback and needs a proxy

## Hardware

Two targets, both first class:

- **x86-64 Debian or Ubuntu.** What it is developed and run on. The
  current host is an Ubuntu 26.04 VM with two cores and 1024MB.
- **A Raspberry Pi 3B+ on the 64-bit image.** 1.4GHz quad-core
  Cortex-A53, 1GB. A plain 3B is the same design a little slower and
  works fine; a 4 or a 5 is more machine than this needs and will run it
  without complaint. Prefer the 64-bit image over the 32-bit one: both
  boards are ARMv8 so either boots, but wheel coverage on PyPI is much
  better for arm64, and that is not hypothetical: MarkupSafe, already a
  dependency, publishes aarch64 wheels for every Python here and no
  `armv7l` wheels at all, so a 32-bit image builds it from source and
  needs a compiler.

Neither covers the other, and they are tight on different axes. Memory
is close now - 1024MB on the VM against 905MB usable on the Pi, after
the VM was resized on 2026-08-13 - so the Pi is the slightly smaller
box. The Pi is slower at everything - one i5-12500T core
against a 1.4GHz Cortex-A53 is not a close comparison, and the VM also
has much faster memory and much faster storage than an SD card. The Pi's
four cores do not close that gap, because this is one process whose
threads are mostly idle or blocked on I/O, not parallel work.

**So size both memory and time against the Pi.** This used to say to
size memory against the VM, which had half the RAM; the 2026-08-13
resize reversed that, and the Pi is now the smaller box on both axes.
Resident memory and anything that accumulates should be judged there.
The service has now been measured on both: about 40MB with four threads
on a 3B+, against about 45MB on the VM. The ARM figure is a floor - it
was taken with no history file and no sensor connection - so expect it
nearer the VM number in service. Startup, JSON parsing and anything CPU-bound should be
judged on the Pi. Nothing in the code is architecture specific.

Measurements below are x86 unless stated. ARM was measured for the
first time on 2026-08-13, on a 3B+ running the 64-bit Trixie image with
Python 3.13: imports roughly 11x slower than x86, JSON parsing roughly
16x, and the full test suite passing 155/155 with a 246MB peak. Every
pinned dependency installed from PyPI as a wheel with nothing built
from source, which is the arm64 claim above, tested rather than
assumed.

What those targets mean for changes:

- **Python 3.10 to 3.14.** The floor is set by the pinned
  dependencies, not the syntax: `click`, `requests` and `urllib3` all
  declare `requires-python >=3.10`, so `requirements.txt` cannot install
  below it. The source itself parses back to 3.9, which is not the same
  thing - a version it compiles on but cannot get its libraries on is
  not a version it supports. That rules out Bullseye; Bookworm is 3.11,
  Trixie 3.13, and production runs 3.14. CI covers 3.10, 3.11 and 3.14.
- **Four third-party imports** - Flask, Werkzeug, requests, urllib3.
  With their own dependencies that is twelve pins, and on 64-bit ARM
  every one installs from a wheel: ten are architecture-neutral, while
  MarkupSafe and charset-normalizer publish aarch64 builds
  (charset-normalizer also keeps a pure-Python fallback). Nothing is
  compiled during install. Keep it that way - a dependency that builds
  from source is a much bigger ask on a Pi than it looks on a laptop.
- **A restart is cheap.** Measured on x86-64: `/api/state` unavailable
  for 0.10s, and a new process answering 0.26s after the trigger. A 3B+
  is slower to start an interpreter and import Flask, so expect a few
  seconds there - still an order of magnitude inside `restart.sh`'s 60s
  timeout.
- **Disk writes are not a concern.** `history.json` is rewritten whole
  every 5 minutes: about 55MB a day, 20GB a year. That is orders of
  magnitude below what any current SD card is rated for, high-endurance
  or otherwise. The SD failure worth designing against is corruption
  from power loss mid-write. `config.json`, `fault.json` and
  `manual-off.pending` are written to a temporary, fsynced, renamed, and
  the directory fsynced after - so a cut leaves the old file or the new
  one, and the rename itself survives. `history.json` and `events.json`
  are renamed atomically but not synced: losing their most recent update
  is accepted, because neither controls the fan.

One coupling to remember: `history.json` is parsed at startup, so the
retention window sets how long starting takes. At the default 24 hours
and a 30s poll that is around 2,900 points and 190KB; a week would be
roughly 1.3MB. It is the only part of startup that grows.

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

The journal reports `Serving Flask app 'controller'`. The name comes
from the module, so it is the quickest confirmation the new file is
running.

Do not run `./run.sh` until the unit is reinstalled. It guards against a
double start by looking for the running process by name, and a stale
unit means it will not recognise what is running and will start a second
instance - two processes driving the same plug, both writing
`config.json`.

## Restarting

Ask the controller to restart itself. No privilege needed:

    ./restart.sh

It writes `restart.request`, waits for the controller to take it, and
proves a new process is answering before reporting success. Doing it by
hand is the same thing without the checking:

    touch restart.request

The watcher checks every two seconds, then replaces its own process with
`execv`. The PID does not change and systemd is not involved. The
activity log shows `Restarting on request` followed by `Resumed after
restart`; if the second line is missing, the exec failed and systemd
caught it.

A restart carries the state that exists only in memory - the per-rule
latches, the minimum-run lease, the draw-fault timer and the
stale-reading clock - across to the replacement process, so it does not
release a latched fan or short-cycle the motor. That handover rides in
the environment, which survives `execv` and nothing else, so a crash or
a `systemctl restart` correctly starts clean rather than resuming a
lease that expired hours ago.

**A restart is refused while a fault is live but unsaved.** The
dashboard shows `Fault not saved to disk` in that state. The fault
exists only in memory, so a replacement would load `fault.json`, find
nothing, and let Auto start a fan that was stopped for drawing wrong.
`restart.sh` exits 1 and says so. Clear the fault, or fix whatever is
stopping `fault.json` being written, then try again.

`sudo systemctl restart office-fan-controller` still works and is the
fallback if the watcher is not running. It cannot refuse: a signalled
stop makes one last attempt to write the fault and then goes ahead
either way. If that attempt fails the journal says so in capitals, and
the fan will not be held off on the next start - check the draw before
running it. Prefer `./restart.sh` whenever the dashboard shows an
unsaved fault.

Otherwise restarting is safe: `history.json` is flushed on `SIGTERM`,
and `config.json`, `events.json` and `fault.json` are untouched. A saved
draw fault survives deliberately.

## Tests

    ./venv/bin/python tests/run_all.py

GitHub Actions runs the same suite on Ubuntu against 3.10, 3.11 and 3.14
on every push. That matters because the tests needing Linux - the
durability suite and the shell tests - skip everywhere else, so CI is
the only place they run without someone remembering to. The workflow
fails if anything reports SKIP, since a skip on Linux means the suite is
covering less than it appears to.

No dependencies beyond the venv. Each module imports `controller.py`
against a throwaway directory, so `config.json`, `history.json`,
`events.json`, `fault.json` and `manual-off.pending` are never read or
written, and no plug or Protect console is contacted. Safe to run
against a live install.

- `test_control.py` - `decide()`, the per-rule latches, the min-run
  lease
- `test_data.py` - `_coerce` validation, `prune_hist`, `find_metric`
- `test_concurrency.py` - the actuation paths under `_act_lock`
- `test_actuation.py` - `_force_on` and `_enforce_stop`: the mode
  re-checks under the lock, the fault write kept outside it, and an
  unreadable plug never being mistaken for a fan that is off
- `test_email.py` - the alert edges, the draw-fault cause text, and the
  authentication choice. `login()` is deliberately not used, because it
  falls back from CRAM-MD5 to PLAIN in the clear; a test asserts that
- `test_failures.py` - persistence errors and malformed request bodies
- `test_history.py` - `/api/history` deltas, including the round trip
  that proves a client ends up holding exactly what the server holds
- `test_restart.py` - the restart trigger, the handover, and the
  last-chance fault write on a signalled stop
- `test_restart_sh.py` - the exit codes `restart.sh` reports, driven
  against a stub controller: a proved restart, a failed exec, a refusal,
  an ignored trigger and an unreachable service
- `test_dashboard.py` - source assertions on `templates/index.html`, for
  the client-side wiring the server's behaviour depends on
- `test_durability.py` - the durable writes and the Manual stop promise:
  a failed directory sync reported rather than hidden, a stop that
  survives a crash, and the routes that must not claim nothing changed
  when the file on disk already did

`test_dashboard.py` needs saying out loud: there is no JavaScript engine
on the deployment host, so it reads the template rather than running it.
It catches a line being **deleted**, not a line being **wrong**. It
exists because the alternative for one defect was a test named after the
dashboard that never opened the file, which is worse than no test at
all.

The concurrency tests gate the interleaving with events rather than
sleeping. Timing alone does not reproduce these races, and a test that
only widens the window can pass against code with the lock removed.

Some tests need the deployment host and report `SKIP` elsewhere rather
than failing, with the count on the summary line so a skip cannot pass
unnoticed:

- one test in `test_restart.py` needs Linux `/proc/self/fd`. It covers
  descriptor inheritance across the `execv`, which is what stopped the
  replacement binding port 8096 the first time.
- all of `test_restart_sh.py` needs a working `bash`. That is probed by
  running it rather than inferred from the platform: macOS has bash and
  should run these, while on Windows the name can resolve to a WSL
  launcher that is present but blocked.
- all of `test_durability.py` needs Linux `O_DIRECTORY`, since every
  test in it reaches a directory fsync.

`test_durability.py` is not the only module that reaches that fsync,
which is why the harness stubs it out where `O_DIRECTORY` does not
exist. `write_json_durable` calls `_fsync_dir`, and `save_cfg`,
`promise_manual_off` and `save_fault` all call `write_json_durable` - so
without the stub an ordinary config save raises `AttributeError` on
Windows and takes dozens of unrelated tests down with it. The harness
prints a `NOTE` when it does this: the run is real, but the directory
fsync is not exercised anywhere in it.

An earlier version of this section listed the three skips above and
implied everything else passed on Windows. It did not. That was wrong
until the stub and the `test_restart_sh.py` platform skip were added.

So a green run on Windows does not cover the restart path, and does not
cover durability either. Run the suite on the host, or read CI, before
trusting a change to either.

## Serving it over HTTPS

Flask binds `127.0.0.1`, so a reverse proxy on the same host is required
for network access.
[deploy/nginx.conf.example](deploy/nginx.conf.example) is a working site
config - copy it to `/etc/nginx/sites-available/`, replace
`example.com`, symlink it into `sites-enabled`, then check and apply it:

    sudo nginx -t && sudo systemctl reload nginx

It declares its own `limit_req_zone` at the top of the file, which works
because Debian and Ubuntu include `sites-enabled` from the http context.
No edit to `nginx.conf` is needed.

It expects a certificate at the usual certbot path. Any issuance method
works; DNS-01 suits a host with no inbound port 80, e.g. with the
Cloudflare plugin:

    sudo apt install nginx certbot python3-certbot-dns-cloudflare
    sudo certbot certonly --dns-cloudflare \
      --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
      -d example.com

Scope the API token narrowly - DNS edit rights on the one zone are
enough, and a zone-wide token can mint a certificate for any name you
host. Add a deploy hook so nginx reloads on renewal, or it keeps serving
the old certificate from memory until it expires:

    printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee \
      /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
    sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

## Authentication

Off by default. Setting a password protects everything that changes
state while leaving the dashboard readable, so a wall display needs no
login:

    curl -X POST https://<your-host>/api/ui/password \
      -H 'Content-Type: application/json' -d '{"password":"..."}'

Post an empty password to disable it again. See
[Docs/configuration.md](Docs/configuration.md).
