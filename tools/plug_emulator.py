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
                failure="none", target="all", delay_seconds=0.0)
PAGE = b'''<!doctype html><meta name="viewport" content="width=device-width">
<title>Test plug</title><h1>Emulated plug (no hardware)</h1>
<p>Settings reset to OFF and 23 W on emulator restart.</p>
<form id="form">
<label>Relay <select id="relay"><option value="false">OFF</option><option value="true">ON</option></select></label><br>
<label>ON watts <input id="on_watts" type="number" min="0" max="100000" step="any" value="23"></label><br>
<label>OFF watts <input id="off_watts" type="number" min="0" max="100000" step="any" value="0"></label><br>
<label>Failure <select id="failure"><option>none</option><option>http_error</option><option>malformed</option><option>reject_switch</option></select></label><br>
<label>Target <select id="target"><option>all</option><option>relay</option><option>power</option><option>switch</option></select></label><br>
<label>Reply delay, seconds <input id="delay_seconds" type="number" min="0" max="20" step="any" value="0"></label><br>
<button>Apply</button></form><pre id="status"></pre>
<script>
const ids=['relay','on_watts','off_watts','failure','target','delay_seconds'];
async function load(){const r=await fetch('/api/emulator');if(!r.ok)throw Error('Read failed');const s=await r.json();for(const id of ids)document.getElementById(id).value=String(s[id]);document.getElementById('status').textContent=JSON.stringify(s,null,2);}
document.getElementById('form').onsubmit=async e=>{e.preventDefault();try{const b={};for(const id of ids){let v=document.getElementById(id).value;b[id]=id==='relay'?v==='true':['on_watts','off_watts','delay_seconds'].includes(id)?Number(v):v;}const r=await fetch('/api/emulator',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw Error('Settings rejected');await load();}catch(err){document.getElementById('status').textContent=err.message;}};
load().catch(e=>document.getElementById('status').textContent=e.message);
</script>'''


class PlugState:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        self.counts = dict(relay=0, power=0, switch=0)

    def snapshot(self):
        with self.lock:
            return dict(self.values, requests=dict(self.counts))

    def update(self, body):
        if not isinstance(body, dict) or set(body) - set(DEFAULTS):
            raise ValueError("Unknown setting or non-object body")
        # Validate the whole patch before mutating any setting.
        for key, value in body.items():
            if key == "relay":
                valid = type(value) is bool
            elif key == "failure":
                valid = value in ("none", "http_error", "malformed", "reject_switch")
            elif key == "target":
                valid = value in ("all", "relay", "power", "switch")
            else:
                try:
                    valid = (type(value) in (int, float) and math.isfinite(value)
                             and 0 <= value <= (20 if key == "delay_seconds" else 100000))
                except OverflowError:
                    valid = False
            if not valid:
                raise ValueError("Invalid " + key)
        with self.lock:
            self.values.update(body)


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
                state.values["relay"] = command == "POWER ON"
            relay = state.values["relay"]
            watts = state.values["on_watts" if relay else "off_watts"]
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
