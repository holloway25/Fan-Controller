#!/usr/bin/env python3
"""Loopback-only Tasmota HTTP emulator. Never connects to a real device."""
import argparse
import ipaddress
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DEFAULTS = dict(relay=False, on_watts=23.0, off_watts=0.0,
                fan_profile=True, startup_watts=45.0, startup_seconds=3.0,
                warm_watts=22.0, warm_after_minutes=30.0,
                failure="none", target="all", delay_seconds=0.0)
PAGE = b'''<!doctype html><meta name="viewport" content="width=device-width">
<title>Test plug</title><h1>Emulated plug (no hardware)</h1>
<p>Simulated fan: brief startup surge, normal running draw, then a gradual reduction as it warms. Settings reset on emulator restart.</p>
<form id="form">
<label>Relay <select id="relay"><option value="false">OFF</option><option value="true">ON</option></select></label><br>
<label>Fan profile <select id="fan_profile"><option value="true">Enabled</option><option value="false">Disabled (fixed ON watts)</option></select></label><br>
<label>Startup watts <input id="startup_watts" type="number" min="0" max="100000" step="any" value="45"></label><br>
<label>Startup seconds <input id="startup_seconds" type="number" min="0" max="60" step="any" value="3"></label><br>
<label>Running / fixed ON watts <input id="on_watts" type="number" min="0" max="100000" step="any" value="23"></label><br>
<label>Warm running watts <input id="warm_watts" type="number" min="0" max="100000" step="any" value="22"></label><br>
<label>Minutes to warm after startup <input id="warm_after_minutes" type="number" min="0" max="1440" step="any" value="30"></label><br>
<label>OFF watts <input id="off_watts" type="number" min="0" max="100000" step="any" value="0"></label><br>
<label>Failure <select id="failure"><option>none</option><option>http_error</option><option>malformed</option><option>reject_switch</option></select></label><br>
<label>Target <select id="target"><option>all</option><option>relay</option><option>power</option><option>switch</option></select></label><br>
<label>Reply delay, seconds <input id="delay_seconds" type="number" min="0" max="20" step="any" value="0"></label><br>
<button>Apply</button></form><pre id="status"></pre>
<script>
const ids=['relay','fan_profile','startup_watts','startup_seconds','on_watts','warm_watts','warm_after_minutes','off_watts','failure','target','delay_seconds'];
async function load(){const r=await fetch('/api/emulator');if(!r.ok)throw Error('Read failed');const s=await r.json();for(const id of ids)document.getElementById(id).value=String(s[id]);document.getElementById('status').textContent=JSON.stringify(s,null,2);}
document.getElementById('form').onsubmit=async e=>{e.preventDefault();try{const b={};for(const id of ids){let v=document.getElementById(id).value;b[id]=['relay','fan_profile'].includes(id)?v==='true':['failure','target'].includes(id)?v:Number(v);}const r=await fetch('/api/emulator',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw Error('Settings rejected');await load();}catch(err){document.getElementById('status').textContent=err.message;}};
load().catch(e=>document.getElementById('status').textContent=e.message);
setInterval(async()=>{try{const r=await fetch('/api/emulator');if(!r.ok)throw Error('Read failed');document.getElementById('status').textContent=JSON.stringify(await r.json(),null,2);}catch(e){document.getElementById('status').textContent=e.message;}},2000);
</script>'''


class PlugState:
    def __init__(self, clock=None):
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        self.counts = dict(relay=0, power=0, switch=0)
        self.clock = clock or time.monotonic
        self.started_at = None

    def _set_relay_locked(self, on):
        if on != self.values["relay"]:
            self.started_at = self.clock() if on else None
            self.values["relay"] = on

    def _load_locked(self):
        values = self.values
        if not values["relay"]:
            return values["off_watts"], "off", 0.0
        elapsed = max(0.0, self.clock() - self.started_at)
        if not values["fan_profile"]:
            return values["on_watts"], "fixed", elapsed
        if elapsed < values["startup_seconds"]:
            return values["startup_watts"], "startup", elapsed
        running = elapsed - values["startup_seconds"]
        duration = values["warm_after_minutes"] * 60
        fraction = min(1.0, running / duration) if duration else 1.0
        watts = values["on_watts"] + fraction * (values["warm_watts"] - values["on_watts"])
        return round(watts, 2), "warmed" if fraction == 1 else "running", elapsed

    def snapshot(self):
        with self.lock:
            watts, phase, elapsed = self._load_locked()
            return dict(self.values, requests=dict(self.counts), current_watts=watts,
                        phase=phase, run_seconds=round(elapsed, 2))

    def update(self, body):
        if not isinstance(body, dict) or set(body) - set(DEFAULTS):
            raise ValueError("Unknown setting or non-object body")
        # Validate the whole patch before mutating any setting.
        for key, value in body.items():
            if key in ("relay", "fan_profile"):
                valid = type(value) is bool
            elif key == "failure":
                valid = value in ("none", "http_error", "malformed", "reject_switch")
            elif key == "target":
                valid = value in ("all", "relay", "power", "switch")
            else:
                try:
                    valid = (type(value) in (int, float) and math.isfinite(value)
                             and 0 <= value <= {"delay_seconds":20, "startup_seconds":60,
                                                 "warm_after_minutes":1440}.get(key, 100000))
                except OverflowError:
                    valid = False
            if not valid:
                raise ValueError("Invalid " + key)
        with self.lock:
            if "relay" in body:
                self._set_relay_locked(body["relay"])
            self.values.update({key:value for key,value in body.items() if key != "relay"})


class EmulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError("Emulator must bind to loopback")
        self.state = PlugState()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Request URLs can contain credentials. Never log them.

    def local_request(self, write=False):
        host = self.headers.get("Host", "")
        try:
            name = urllib.parse.urlsplit("http://" + host).hostname
            local = name == "localhost" or ipaddress.ip_address(name).is_loopback
        except (ValueError, TypeError):
            local = False
        if not local:
            self.reply(403, dict(error="Loopback Host required"))
            return False
        source = self.headers.get("Origin", self.headers.get("Referer"))
        try:
            parsed = urllib.parse.urlsplit(source) if source is not None else None
            origin_ok = parsed is None or (parsed.scheme == "http" and parsed.netloc == host)
        except ValueError:
            origin_ok = False
        if write and (self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")
                      or not origin_ok):
            self.reply(403, dict(error="Same-origin request required"))
            return False
        return True

    def reply(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.local_request():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            return self.reply(200, PAGE, "text/html; charset=utf-8")
        if parsed.path == "/api/emulator":
            return self.reply(200, self.server.state.snapshot())
        if parsed.path != "/cm":
            return self.reply(404, dict(error="Not found"))
        command = urllib.parse.parse_qs(parsed.query).get("cmnd", [""])[0].strip().upper()
        operation = {"POWER": "relay", "POWER ON": "switch", "POWER OFF": "switch",
                     "STATUS 8": "power"}.get(command)
        if operation is None:
            return self.reply(400, dict(error="Unsupported emulated command"))
        if operation == "switch" and not self.local_request(write=True):
            return
        state = self.server.state
        with state.lock:
            state.counts[operation] += 1
            settings = dict(state.values)
        selected = settings["target"] in ("all", operation)
        if selected:
            time.sleep(settings["delay_seconds"])
            if settings["failure"] == "http_error":
                return self.reply(503, dict(error="Simulated plug failure"))
            if settings["failure"] == "malformed":
                return self.reply(200, b"not JSON")
        with state.lock:
            if operation == "switch" and not (selected and settings["failure"] == "reject_switch"):
                state._set_relay_locked(command == "POWER ON")
            relay = state.values["relay"]
            watts, _, _ = state._load_locked()
        if operation == "power":
            return self.reply(200, {"StatusSNS": {"ENERGY": {"Power": watts}}})
        return self.reply(200, {"POWER": "ON" if relay else "OFF"})

    def do_POST(self):
        if self.path != "/api/emulator":
            return self.reply(404, dict(error="Not found"))
        if not self.local_request(write=True):
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.reply(415, dict(error="JSON required"))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid body length")
            body = json.loads(self.rfile.read(length))
            self.server.state.update(body)
        except (ValueError, TypeError, OverflowError):
            return self.reply(400, dict(error="Invalid emulator settings"))
        return self.reply(200, self.server.state.snapshot())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "show", "set"))
    parser.add_argument("--port", type=int, default=8981)
    parser.add_argument("--json", help="JSON settings patch for set")
    args = parser.parse_args()
    if args.action == "serve":
        with EmulatorServer(("127.0.0.1", args.port)) as server:
            print("Emulated plug on 127.0.0.1:%d (no hardware)" % server.server_port, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    else:
        if args.action == "set" and args.json is None:
            parser.error("set requires --json")
        data = args.json.encode() if args.action == "set" else None
        request = urllib.request.Request("http://127.0.0.1:%d/api/emulator" % args.port,
                                         data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            print(response.read().decode())


if __name__ == "__main__":
    main()
