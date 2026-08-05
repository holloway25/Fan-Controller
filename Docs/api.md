# HTTP API

Served on port 8096. All responses are JSON except `/`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/state` | Readings, fan state, latches, history, events, config |
| POST | `/api/config` | Partial settings update; validates then commits |
| POST | `/api/protect/sensors` | List adopted sensors with their CO2 value |
| POST | `/api/hue/pair` | Create a Hue application key (link button first) |
| GET | `/api/hue/lights` | List lights with on/off state |
| GET | `/api/debug/sensor` | Raw Protect payload for the selected sensor |
| POST | `/api/clienterror` | Browser JS errors, logged server side |

Secrets are redacted in responses: `protect.password` and `hue.key` return
the literal `SET` when present, and a posted `SET` is ignored rather than
written back.
