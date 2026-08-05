# Office Fan Controller

Reads CO2, temperature, humidity and PM2.5 from a UniFi Protect
UP-AirQuality sensor and switches a fan through a Tasmota smart plug,
with hysteresis on CO2 and optionally temperature. The plug's energy
meter confirms the fan is actually drawing current. Web UI is served over
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

Above the on level the fan starts. It keeps running until CO2 falls to the
off level, so the gap between the two prevents short-cycling. A freshly
started fan is also held on for the minimum run time. Force on overrides
the thresholds entirely; Manual switches the fan off once and then leaves
it to Google Home or the plug itself.

The API refuses any save where the on level is not above the off level,
since that leaves the fan running forever.

## When it breaks

Protect 7.1's documented API omits air-quality metrics entirely, so this
reads the private /proxy/protect/api/bootstrap endpoint. That is
undocumented and can change on any firmware update.

CO2 is found by searching the payload for a numeric field whose key looks
like CO2, rather than a fixed path, so a rename survives. If it stops
resolving, open /api/debug/sensor to see the live payload and adjust
CO2_HINTS in co2fan.py.

Once Ubiquiti publishes these fields properly, this can be replaced with a
few lines against the documented API.

## Documentation

- [Docs/architecture.md](Docs/architecture.md) - design, and why the
  private Protect API is used
- [Docs/control-logic.md](Docs/control-logic.md) - modes, latching,
  minimum run time
- [Docs/configuration.md](Docs/configuration.md) - every setting
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

    echo "systemctl reload nginx" | sudo tee \
      /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
    sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
## Authentication

Off by default. Setting a password protects everything that changes state
while leaving the dashboard readable, so a wall display needs no login:

    curl -X POST https://<your-host>/api/ui/password \
      -H 'Content-Type: application/json' -d '{"password":"..."}'

Post an empty password to disable it again. See Docs/configuration.md.
