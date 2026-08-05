# Office Fan Controller

Reads CO2 from a UniFi Protect UP-AirQuality sensor and switches a
Tasmota smart plug with hysteresis. Web UI on port 8096.

    ~/co2fan/run.sh          # start (idempotent, safe from cron)
    pkill -f '[c]o2fan.py'   # stop
    tail -f ~/co2fan/co2fan.log

## Setup

1. In UniFi, create a **local** user with no two-factor. Cloud SSO logins
   will not work against the private Protect API.
2. Open http://<server>:8096 and fill in the sensor card, then
   **Find sensors**. Each sensor is listed with its CO2 reading, or
   "no CO2 field" if Protect is not publishing one.
3. Open **Fan plug**, enter the plug's address and web password,
   then **Test plug**. It reports relay state and current draw.
   The plug's HTTP command API must be enabled.
4. Set thresholds and tick **Run the control loop**.

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

## Install

    pip install --target ./libs flask requests
    cp config.example.json config.json
    ./run.sh

Then open port 8096 and complete setup in the UI.
