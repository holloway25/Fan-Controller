"""CO2-driven Hue fan control.

Reads CO2 from a UniFi Protect UP-AirQuality sensor and switches Hue
lights/plugs with hysteresis. Settings are editable in the web UI.

Protect 7.1's documented API omits air-quality metrics, so this reads the
private /proxy/protect/api/bootstrap endpoint. That is undocumented and may
break on firmware updates; use the Debug view to inspect the live payload.
"""
import json, os, threading, time, copy
import requests, urllib3
from flask import Flask, jsonify, request, render_template

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.json")

DEFAULTS = {
    "protect": {"host": "192.168.1.1", "username": "", "password": "",
                "sensor_id": ""},
    "hue": {"bridge": "192.168.1.2", "key": "", "lights": []},
    "control": {"mode": "auto", "on_at": 1000, "off_at": 800,
                "poll_seconds": 30, "min_run_minutes": 5,
                "temp_enabled": False, "temp_on": 26.0,
                "temp_off": 24.5},
}
CO2_HINTS = ("co2", "carbondioxide")
EXTRA_HINTS = {"temperature": ("temperature",),
               "pm25": ("pm2p5", "pm25"),
               "humidity": ("humidity",)}
VALUE_KEYS = ("value", "val", "current", "ppm", "reading")


def load_cfg():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CFG_PATH):
        try:
            disk = json.load(open(CFG_PATH))
        except (ValueError, OSError):
            disk = {}
        for k, v in disk.items():
            if isinstance(v, dict) and k in cfg:
                cfg[k].update(v)
            else:
                cfg[k] = v
    ctl = cfg.get("control", {})
    if "enabled" in ctl:
        # Older configs used a separate enable flag; "not enabled" is now
        # simply manual mode. Fold it in and drop the duplicate control.
        if not ctl.pop("enabled") and ctl.get("mode") == "auto":
            ctl["mode"] = "manual"
    return cfg


def save_cfg(cfg):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_PATH)
    os.chmod(CFG_PATH, 0o600)


HIST_PATH = os.path.join(DIR, "history.json")
HIST_MAX = 2880  # 24h at 30s polling


def load_hist():
    try:
        h = json.load(open(HIST_PATH))
        return h[-HIST_MAX:] if isinstance(h, list) else []
    except (ValueError, OSError):
        return []


def save_hist(hist):
    """Persist readings so a restart does not blank the graph."""
    try:
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(tmp, HIST_PATH)
    except OSError:
        pass


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in VALUE_KEYS:
            if k in v:
                return _num(v[k])
    return None


def find_metric(obj, hints):
    """Depth-first search for a numeric reading whose key matches a hint.

    Used instead of a fixed path because Ubiquiti has not published the
    airQuality schema. Returns (value, dotted_path) or None.
    """
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                flat = k.lower().replace("_", "").replace("-", "")
                p = path + [k]
                if any(h in flat for h in hints) and _num(v) is not None:
                    hits.append((_num(v), ".".join(p)))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [str(i)])

    walk(obj, [])
    return hits[0] if hits else None


class ProtectError(Exception):
    pass


class HueError(Exception):
    pass


class Protect:
    """Local-account session against UniFi OS, reading Protect's bootstrap."""

    def __init__(self, host, username, password):
        self.host, self.username, self.password = host, username, password
        self._s = None
        self._csrf = None
        self._lock = threading.Lock()

    @property
    def base(self):
        return "https://%s" % self.host

    def login(self):
        s = requests.Session()
        s.verify = False
        r = s.post(self.base + "/api/auth/login",
                   json={"username": self.username, "password": self.password,
                         "rememberMe": True}, timeout=15)
        if r.status_code == 499:
            raise ProtectError("That account has two-factor enabled. Create a "
                               "separate local user without 2FA.")
        if r.status_code == 429:
            raise ProtectError("UniFi is rate-limiting logins (429). Backing "
                               "off; this clears on its own in a few minutes.")
        if r.status_code in (400, 401, 403):
            raise ProtectError("UniFi rejected those credentials (HTTP %d). "
                               "Use a local user, not a UI Cloud login."
                               % r.status_code)
        r.raise_for_status()
        self._csrf = r.headers.get("X-CSRF-Token")
        self._s = s

    def _get(self, path, retry=True):
        with self._lock:
            if self._s is None:
                self.login()
            h = {"Accept": "application/json"}
            if self._csrf:
                h["X-CSRF-Token"] = self._csrf
            try:
                r = self._s.get(self.base + path, headers=h, timeout=20)
            except requests.RequestException as e:
                self._s = None
                raise ProtectError("Cannot reach the UniFi console: %s" % e)
        if r.status_code in (401, 403) and retry:
            self._s = None
            return self._get(path, retry=False)
        if r.status_code >= 400:
            raise ProtectError("Protect returned HTTP %d for %s"
                               % (r.status_code, path))
        return r.json()

    def sensors(self):
        return self._get("/proxy/protect/api/bootstrap").get("sensors") or []

    def sensor(self, sid):
        for s in self.sensors():
            if s.get("id") == sid:
                return s
        raise ProtectError("Sensor %s is not adopted on this console" % sid)

    def readings(self, sid):
        """All tracked metrics, plus the CO2 payload path.

        Scoped to the airQuality subtree where present, so sibling config
        blocks like humiditySettings cannot be mistaken for readings.
        """
        s = self.sensor(sid)
        src = s.get("airQuality") or s
        co2 = find_metric(src, CO2_HINTS)
        if co2 is None:
            raise ProtectError("No CO2 field in this sensor's payload. Open "
                               "Debug to see what Protect is returning.")
        out = {"co2": co2[0]}
        for name, hints in EXTRA_HINTS.items():
            hit = find_metric(src, hints)
            if hit is not None:
                out[name] = hit[0]
        return out, co2[1]

    def co2(self, sid):
        """Current CO2 in ppm plus the payload path it came from."""
        hit = find_metric(self.sensor(sid), CO2_HINTS)
        if hit is None:
            raise ProtectError("No CO2 field in this sensor's payload. Open "
                               "Debug to see what Protect is returning.")
        return hit


class Hue:
    """Hue bridge client. Prefers CLIP v2, falls back to the v1 API."""

    def __init__(self, ip, key=""):
        self.ip, self.key = ip, key
        self._v2 = None

    def _url(self, p):
        return "https://%s%s" % (self.ip, p)

    def pair(self, devicetype="co2fan#dashboard"):
        """Create an application key. The link button must be pressed first."""
        r = requests.post(self._url("/api"), json={"devicetype": devicetype},
                          verify=False, timeout=15)
        data = r.json()
        if isinstance(data, list) and data:
            if "success" in data[0]:
                self.key = data[0]["success"]["username"]
                return self.key
            err = data[0].get("error", {})
            if err.get("type") == 101:
                raise HueError("Press the round button on the Hue bridge, then "
                               "try again within 30 seconds.")
            raise HueError(err.get("description", "Pairing failed"))
        raise HueError("Unexpected reply from the bridge")

    def lights(self):
        """[{id, name, on}] across whichever API version the bridge answers."""
        if not self.key:
            raise HueError("Not paired with the bridge yet")
        if self._v2 is not False:
            try:
                r = requests.get(self._url("/clip/v2/resource/light"),
                                 headers={"hue-application-key": self.key},
                                 verify=False, timeout=15)
                if r.status_code == 200:
                    self._v2 = True
                    return [{"id": d["id"],
                             "name": d.get("metadata", {}).get("name", d["id"]),
                             "on": d.get("on", {}).get("on")}
                            for d in r.json().get("data", [])]
            except requests.RequestException:
                pass
        self._v2 = False
        return self._lights_v1()

    def _lights_v1(self):
        r = requests.get(self._url("/api/%s/lights" % self.key),
                         verify=False, timeout=15)
        if r.status_code != 200:
            raise HueError("Bridge returned HTTP %d listing lights"
                           % r.status_code)
        data = r.json()
        if isinstance(data, list):
            raise HueError("Bridge rejected the stored key. Pair again.")
        return [{"id": k, "name": v.get("name", k),
                 "on": v.get("state", {}).get("on")}
                for k, v in data.items()]

    def set(self, light_id, on):
        if self._v2:
            r = requests.put(self._url("/clip/v2/resource/light/" + light_id),
                             headers={"hue-application-key": self.key},
                             json={"on": {"on": bool(on)}},
                             verify=False, timeout=15)
        else:
            r = requests.put(
                self._url("/api/%s/lights/%s/state" % (self.key, light_id)),
                json={"on": bool(on)}, verify=False, timeout=15)
        if r.status_code >= 400:
            raise HueError("Bridge returned HTTP %d switching light %s"
                           % (r.status_code, light_id))


STATE = {
    "co2": None, "co2_path": None, "fan": None, "reading_at": None,
    "changed_at": None, "error": None, "sensor_name": None,
    "temperature": None, "humidity": None, "pm25": None,
    "latch": {"co2": False, "temp": False},
    "history": [], "events": [],
}
_state_lock = threading.Lock()
_wake = threading.Event()  # set to make the poll loop act at once
CFG = load_cfg()
STATE["history"] = load_hist()


def _log(msg):
    with _state_lock:
        STATE["events"].insert(0, {"at": time.time(), "msg": msg})
        del STATE["events"][40:]


_CLIENTS = {"key": None, "protect": None, "hue": None}
_clients_lock = threading.Lock()


def _clients():
    """Reuse one authenticated session per config.

    Building a new Protect object per poll meant a fresh /api/auth/login
    every 30s, which UniFi rate-limits with HTTP 429. Clients are rebuilt
    only when the relevant settings actually change.
    """
    p, h = CFG["protect"], CFG["hue"]
    key = (p["host"], p["username"], p["password"], h["bridge"], h["key"])
    with _clients_lock:
        if _CLIENTS["key"] != key:
            _CLIENTS.update(key=key,
                            protect=Protect(p["host"], p["username"],
                                            p["password"]),
                            hue=Hue(h["bridge"], h["key"]))
        return _CLIENTS["protect"], _CLIENTS["hue"]


def decide(co2, temp, fan_on, c, ran_for, latch):
    """Per-rule latching hysteresis. Mutates `latch` in place.

    Each input latches on above its own on-level and releases below its
    own off-level. The fan runs while any rule is latched. This matters:
    previously a rule that had never triggered could still veto the stop,
    so a CO2-driven run was held on by temperature indefinitely.
    """
    if c["mode"] == "on":
        return True, "forced on"
    if c["mode"] == "off":
        return False, "forced off"
    if c["mode"] == "manual":
        latch["co2"] = latch["temp"] = False
        return None, None
    if co2 is not None:
        if co2 >= c["on_at"]:
            latch["co2"] = True
        elif co2 <= c["off_at"]:
            latch["co2"] = False
    use_t = bool(c.get("temp_enabled")) and temp is not None
    if not use_t:
        latch["temp"] = False
    elif temp >= c["temp_on"]:
        latch["temp"] = True
    elif temp <= c["temp_off"]:
        latch["temp"] = False
    want = latch["co2"] or latch["temp"]
    if want == bool(fan_on):
        return (True, None) if want else (False, None)
    if want:
        why = []
        if latch["co2"]:
            why.append("%d ppm" % round(co2))
        if latch["temp"]:
            why.append("%.1f C" % temp)
        return True, " and ".join(why)
    if fan_on and ran_for is not None and \
            ran_for < c["min_run_minutes"] * 60:
        return None, None
    why = "%d ppm" % round(co2) if co2 is not None else "no reading"
    if use_t:
        why += ", %.1f C" % temp
    return False, why


def _actual_fan(h, ids):
    """Ask the bridge what the fan is really doing. None if unknown."""
    if not ids:
        return None
    try:
        return {l["id"]: l["on"] for l in h.lights()}.get(ids[0])
    except HueError:
        return None


def tick():
    p, h = _clients()
    c = CFG["control"]
    sid = CFG["protect"]["sensor_id"]
    if not sid:
        raise ProtectError("No air-quality sensor selected yet")
    vals, path = p.readings(sid)
    co2 = vals['co2']
    now = time.time()
    ids = CFG["hue"]["lights"]
    actual = _actual_fan(h, ids)
    with _state_lock:
        STATE.update(co2=co2, co2_path=path, reading_at=now, error=None,
                     temperature=vals.get('temperature'),
                     humidity=vals.get('humidity'),
                     pm25=vals.get('pm25'))
        STATE["history"].append({"t": now, "v": co2,
                                 "tc": vals.get('temperature'),
                                 "rh": vals.get('humidity'),
                                 "pm": vals.get('pm25')})
        del STATE["history"][:-HIST_MAX]
        hsnap = list(STATE["history"])
        prev = STATE["fan"]
        if actual is not None and actual != prev:
            # Someone switched it outside this service. Trust the bridge,
            # and restart the minimum-run clock from now.
            STATE["fan"] = actual
            if prev is not None:
                # Only a genuine external switch restarts the minimum-run
                # clock; the first reading after startup must not grant a
                # fresh lease to a fan that was already running.
                STATE["changed_at"] = now
                _log("Fan switched to %s outside this service"
                     % ("on" if actual else "off"))
        fan_on = STATE["fan"]
        changed = STATE["changed_at"]
    save_hist(hsnap)
    ran_for = (now - changed) if changed else None
    want, why = decide(co2, vals.get('temperature'), fan_on, c, ran_for,
                       STATE["latch"])
    if want is None or want == fan_on:
        return
    if not ids:
        raise HueError("No Hue lights selected yet")
    for lid in ids:
        h.set(lid, want)
    with _state_lock:
        STATE["fan"] = want
        STATE["changed_at"] = now
    _log("Fan %s \u2014 %s" % ("on" if want else "off", why))


def loop():
    fails = 0
    while True:
        base = max(5, int(CFG["control"].get("poll_seconds", 30)))
        try:
            tick()
            fails = 0
        except (ProtectError, HueError) as e:
            fails += 1
            with _state_lock:
                STATE["error"] = str(e)
        except Exception as e:  # keep the loop alive on anything unexpected
            fails += 1
            with _state_lock:
                STATE["error"] = "%s: %s" % (type(e).__name__, e)
        # Back off on repeated failures so a rate limit is not made worse.
        delay = base if not fails else min(300, base * 2 ** min(fails, 5))
        # Wake early when settings change so mode switches are instant.
        _wake.wait(delay)
        _wake.clear()


app = Flask(__name__)


def _safe_cfg():
    c = copy.deepcopy(CFG)
    c["protect"]["password"] = "SET" if CFG["protect"]["password"] else ""
    c["hue"]["key"] = "SET" if CFG["hue"]["key"] else ""
    return c


@app.after_request
def _no_cache(resp):
    """The UI is small and always dynamic; never let a browser hold a stale
    copy, which otherwise hides fixes behind a hard refresh."""
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/clienterror", methods=["POST"])
def api_clienterror():
    """Surface browser-side JS failures in the server log, since a script
    that dies on load leaves no other trace."""
    b = request.get_json(force=True, silent=True) or {}
    msg = str(b.get("msg", ""))[:300]
    where = "%s:%s" % (str(b.get("src", ""))[:120], b.get("line", "?"))
    app.logger.error("UI error: %s @ %s", msg, where)
    _log("UI error: %s" % msg[:120])
    return jsonify(ok=True)


@app.route("/api/state")
def api_state():
    with _state_lock:
        s = copy.deepcopy(STATE)
    s["config"] = _safe_cfg()
    return jsonify(s)


@app.route("/api/config", methods=["POST"])
def api_config():
    """Validate against a copy so a rejected save cannot alter live settings."""
    body = request.get_json(force=True) or {}
    cand = copy.deepcopy(CFG)
    for section in ("protect", "hue", "control"):
        for k, v in (body.get(section) or {}).items():
            if v == "SET":
                continue
            cand[section][k] = v
    c = cand["control"]
    try:
        on_at, off_at = float(c["on_at"]), float(c["off_at"])
    except (TypeError, ValueError):
        return jsonify(error="Thresholds must be numbers."), 400
    if on_at <= off_at:
        return jsonify(error="Turn-on level must be higher than turn-off "
                             "level, or the fan will never stop."), 400
    if c.get("temp_enabled"):
        try:
            t_on, t_off = float(c["temp_on"]), float(c["temp_off"])
        except (TypeError, ValueError):
            return jsonify(error="Temperatures must be numbers."), 400
        if t_on <= t_off:
            return jsonify(error="Cool-to temperature must be below the "
                                 "turn-on temperature."), 400
    was = CFG["control"].get("mode")
    for section in cand:
        CFG[section] = cand[section]
    now_mode = CFG["control"].get("mode")
    if was in ("on", "off") and now_mode not in ("on", "off"):
        # Leaving a forced state: do not let the minimum-run timer from the
        # forced switch hold the fan on once automatic control resumes.
        with _state_lock:
            STATE["changed_at"] = None
    save_cfg(CFG)
    _log("Settings saved")
    _wake.set()
    return jsonify(ok=True, config=_safe_cfg())


@app.route("/api/protect/sensors", methods=["POST"])
def api_sensors():
    p, _ = _clients()
    try:
        out = []
        for s in p.sensors():
            hit = find_metric(s, CO2_HINTS)
            out.append({"id": s.get("id"), "name": s.get("name"),
                        "state": s.get("state"),
                        "co2": hit[0] if hit else None})
        return jsonify(sensors=out)
    except ProtectError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/hue/pair", methods=["POST"])
def api_pair():
    _, h = _clients()
    try:
        CFG["hue"]["key"] = h.pair()
        save_cfg(CFG)
        _log("Paired with Hue bridge")
        return jsonify(ok=True, lights=h.lights())
    except HueError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/hue/lights")
def api_lights():
    _, h = _clients()
    try:
        return jsonify(lights=h.lights())
    except HueError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/debug/sensor")
def api_debug():
    """Raw sensor payload - the place to look when CO2 stops resolving."""
    p, _ = _clients()
    sid = CFG["protect"]["sensor_id"]
    try:
        return jsonify(p.sensor(sid) if sid else p.sensors())
    except ProtectError as e:
        return jsonify(error=str(e)), 400


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8096, threaded=True)
