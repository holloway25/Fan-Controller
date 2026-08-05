"""CO2-driven Hue fan control.

Reads CO2 from a UniFi Protect UP-AirQuality sensor and switches Hue
lights/plugs with hysteresis. Settings are editable in the web UI.

Protect 7.1's documented API omits air-quality metrics, so this reads the
private /proxy/protect/api/bootstrap endpoint. That is undocumented and may
break on firmware updates; use the Debug view to inspect the live payload.
"""
import atexit, copy, hashlib, hmac, json, math, os, signal, sys
import threading, time
import requests, urllib3
from flask import Flask, Response, jsonify, request, render_template

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.json")

DEFAULTS = {
    "protect": {"host": "192.168.1.1", "username": "", "password": "",
                "verify_tls": False,
                "sensor_id": ""},
    "hue": {"bridge": "192.168.1.2", "key": "", "lights": []},
    "ui": {"password_hash": ""},
    "control": {"mode": "auto", "on_at": 1000, "off_at": 800,
                "poll_seconds": 30, "min_run_minutes": 5,
                "temp_enabled": False, "temp_on": 26.0,
                "temp_off": 24.5,
                "stale_minutes": 0},
}
CO2_HINTS = ("co2", "carbondioxide")
EXTRA_HINTS = {"temperature": ("temperature",),
               "pm25": ("pm2p5", "pm25"),
               "humidity": ("humidity",)}
VALUE_KEYS = ("value", "val", "current", "ppm", "reading")


CONTROL_SCHEMA = {
    "mode": ("enum", ("auto", "manual", "on", "off")),
    "on_at": ("num", 300.0, 10000.0),
    "off_at": ("num", 300.0, 10000.0),
    "poll_seconds": ("int", 5, 3600),
    "min_run_minutes": ("int", 0, 240),
    "temp_enabled": ("bool",),
    "temp_on": ("num", -20.0, 60.0),
    "temp_off": ("num", -20.0, 60.0),
    "stale_minutes": ("int", 0, 1440),
}
PROTECT_SCHEMA = {
    "host": ("str", 1, 253), "username": ("str", 0, 128),
    "password": ("str", 0, 256), "sensor_id": ("str", 0, 64),
    "verify_tls": ("bool",),
}
UI_SCHEMA = {"password_hash": ("str", 0, 200)}
HUE_SCHEMA = {
    "bridge": ("str", 1, 253), "key": ("str", 0, 128),
    "lights": ("list_str", 0, 32),
}
SCHEMAS = {"protect": PROTECT_SCHEMA, "hue": HUE_SCHEMA, "ui": UI_SCHEMA,
           "control": CONTROL_SCHEMA}


def _coerce(name, spec, v):
    """Convert and range-check one value, or raise ValueError.

    Values are stored coerced, never as supplied: accepting "1000" for a
    threshold made decide() raise TypeError every poll, disabling
    automation while the UI still looked healthy.
    """
    kind = spec[0]
    if kind == "enum":
        if v not in spec[1]:
            raise ValueError("%s must be one of: %s"
                             % (name, ", ".join(spec[1])))
        return v
    if kind == "bool":
        # bool("false") is True, so require a real JSON boolean.
        if not isinstance(v, bool):
            raise ValueError("%s must be true or false" % name)
        return v
    if kind in ("num", "int"):
        if isinstance(v, bool) or isinstance(v, (list, dict, type(None))):
            raise ValueError("%s must be a number" % name)
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % name)
        if not math.isfinite(f):
            raise ValueError("%s must be a finite number" % name)
        if not (spec[1] <= f <= spec[2]):
            raise ValueError("%s must be between %g and %g"
                             % (name, spec[1], spec[2]))
        if kind == "int":
            if f != int(f):
                raise ValueError("%s must be a whole number" % name)
            return int(f)
        return f
    if kind == "str":
        if not isinstance(v, str) or not (spec[1] <= len(v) <= spec[2]):
            raise ValueError("%s is not valid text" % name)
        return v
    if kind == "list_str":
        if not isinstance(v, list) or len(v) > spec[2] or \
                not all(isinstance(x, str) and x for x in v):
            raise ValueError("%s must be a list of ids" % name)
        return v
    raise ValueError("%s cannot be set" % name)


def normalise(section, data):
    """Coerce a section, rejecting unknown keys."""
    schema = SCHEMAS[section]
    out = {}
    for k, v in data.items():
        if k not in schema:
            raise ValueError("unknown setting %s.%s" % (section, k))
        out[k] = _coerce("%s.%s" % (section, k), schema[k], v)
    return out


def load_cfg():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CFG_PATH):
        try:
            disk = json.load(open(CFG_PATH))
        except (ValueError, OSError):
            disk = {}
        # Valid JSON of the wrong shape (a list, a string-valued section)
        # used to raise AttributeError before normalisation could run.
        if not isinstance(disk, dict):
            disk = {}
        for k, v in disk.items():
            if isinstance(v, dict) and k in cfg:
                cfg[k].update(v)
            else:
                cfg[k] = v
    ctl = cfg.get("control")
    ctl = ctl if isinstance(ctl, dict) else {}
    if "enabled" in ctl:
        # Older configs used a separate enable flag; "not enabled" is now
        # simply manual mode. Fold it in and drop the duplicate control.
        if not ctl.pop("enabled") and ctl.get("mode") == "auto":
            ctl["mode"] = "manual"
    # Disk config is untrusted too: coerce it, and fall back to the default
    # for any field that will not validate rather than starting poisoned.
    for section, schema in SCHEMAS.items():
        clean = dict(DEFAULTS[section])
        got = cfg.get(section)
        for k, v in (got if isinstance(got, dict) else {}).items():
            if k not in schema:
                continue
            try:
                clean[k] = _coerce("%s.%s" % (section, k), schema[k], v)
            except ValueError:
                pass
        cfg[section] = clean
    return cfg



def save_cfg(cfg):
    tmp = CFG_PATH + ".tmp"
    # Created 0600 up front: the temp file holds the UniFi password and Hue
    # key, and chmod after the rename left a window at the process umask.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
    except BaseException:
        os.unlink(tmp)
        raise
    os.replace(tmp, CFG_PATH)
    os.chmod(CFG_PATH, 0o600)


HIST_PATH = os.path.join(DIR, "history.json")
HIST_HOURS = 24           # retention window, independent of poll interval
HIST_CAP = 20000          # memory bound if poll_seconds is very small
HIST_FLUSH_SECONDS = 300  # batch disk writes rather than one per poll
_last_flush = 0.0


def prune_hist(hist, now=None):
    """Keep HIST_HOURS of samples by timestamp, not by count.

    A fixed sample cap only equals 24h at one specific poll interval, so
    changing poll_seconds silently changed the retention window. HIST_CAP
    remains as a memory bound, not as the retention policy.
    """
    cutoff = (now or time.time()) - HIST_HOURS * 3600
    kept = [p for p in hist
            if isinstance(p, dict) and (p.get("t") or 0) >= cutoff]
    return kept[-HIST_CAP:]


def load_hist():
    try:
        h = json.load(open(HIST_PATH))
    except (ValueError, OSError):
        return []
    return prune_hist(h) if isinstance(h, list) else []


def save_hist(hist):
    """Persist readings so a restart does not blank the graph."""
    try:
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(tmp, HIST_PATH)
    except OSError:
        pass



def maybe_save_hist(hist, force=False):
    """Batch writes: one full rewrite per poll was 2,880 a day at 30s."""
    global _last_flush
    now = time.time()
    if not force and now - _last_flush < HIST_FLUSH_SECONDS:
        return False
    save_hist(hist)
    _last_flush = now
    return True


def flush_hist(signum=None, frame=None):
    """Persist on shutdown so batching never costs a restart's samples."""
    with _state_lock:
        snapshot = list(STATE["history"])
    maybe_save_hist(snapshot, force=True)
    if signum is not None:
        sys.exit(0)

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

    def __init__(self, host, username, password, verify=False):
        self.host, self.username, self.password = host, username, password
        # verify=True requires a hostname the cert covers, not an IP.
        self.verify = bool(verify)
        self._s = None
        self._csrf = None
        self._lock = threading.Lock()

    @property
    def base(self):
        return "https://%s" % self.host

    def login(self):
        s = requests.Session()
        s.verify = self.verify
        try:
            r = s.post(self.base + "/api/auth/login",
                       json={"username": self.username,
                             "password": self.password,
                             "rememberMe": True}, timeout=15)
        except requests.exceptions.SSLError as e:
            raise ProtectError(
                "TLS verification failed for %s. The certificate must cover "
                "this exact name and be current - an IP address will not "
                "validate. Untick Verify certificate to use an IP. (%s)"
                % (self.host, str(e)[:120]))
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
            except requests.exceptions.SSLError as e:
                self._s = None
                raise ProtectError(
                    "TLS verification failed for %s. Check the certificate "
                    "covers this name and has not expired, or turn off "
                    "Verify certificate. (%s)" % (self.host, e))
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

    @staticmethod
    def _check_body(r, light_id):
        """Hue v1 reports rejection inside a 200 body, so status is not
        enough - trusting it marked failed switches as successful."""
        try:
            data = r.json()
        except ValueError:
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "error" in item:
                    d = (item["error"] or {}).get("description", "rejected")
                    raise HueError("Bridge rejected the command for %s: %s"
                                   % (light_id, d))
        elif isinstance(data, dict) and data.get("errors"):
            d = data["errors"][0]
            raise HueError("Bridge rejected the command for %s: %s"
                           % (light_id, d.get("description", "rejected")))

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
        self._check_body(r, light_id)


STATE = {
    "co2": None, "co2_path": None, "fan": None, "reading_at": None,
    "changed_at": None, "error": None, "sensor_name": None,
    "temperature": None, "humidity": None, "pm25": None,
    "latch": {"co2": False, "temp": False},
    "history": [], "events": [],
}
# RLock, not Lock: nested acquisition deadlocked the poll thread.
_state_lock = threading.RLock()
START_TIME = time.time()
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
    key = (p["host"], p["username"], p["password"],
           bool(p.get("verify_tls")), h["bridge"], h["key"])
    with _clients_lock:
        if _CLIENTS["key"] != key:
            _CLIENTS.update(key=key,
                            protect=Protect(p["host"], p["username"],
                                            p["password"],
                                            p.get("verify_tls")),
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


def _fan_status(h, ids):
    """(state, split) for the selected outputs.

    state is True/False only when every output agrees, else None. split is
    True when they read successfully but disagree - that must force a
    re-send, since a desired state matching the believed state would
    otherwise skip switching and never repair the odd one out.
    """
    if not ids:
        return None, False
    try:
        seen = {l["id"]: l["on"] for l in h.lights()}
    except HueError:
        return None, False
    vals = [seen.get(i) for i in ids]
    if any(v is None for v in vals):
        return None, False
    if all(vals):
        return True, False
    if not any(vals):
        return False, False
    return None, True


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
    actual, split = _fan_status(h, ids)
    ext_msg = None
    with _state_lock:
        STATE.update(co2=co2, co2_path=path, reading_at=now, error=None,
                     temperature=vals.get('temperature'),
                     humidity=vals.get('humidity'),
                     pm25=vals.get('pm25'))
        STATE["history"].append({"t": now, "v": co2,
                                 "tc": vals.get('temperature'),
                                 "rh": vals.get('humidity'),
                                 "pm": vals.get('pm25')})
        STATE["history"][:] = prune_hist(STATE["history"], now)
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
                ext_msg = ("Fan switched to %s outside this service"
                           % ("on" if actual else "off"))
        fan_on = STATE["fan"]
        changed = STATE["changed_at"]
    maybe_save_hist(hsnap)
    if ext_msg:
        _log(ext_msg)
    ran_for = (now - changed) if changed else None
    want, why = decide(co2, vals.get('temperature'), fan_on, c, ran_for,
                       STATE["latch"])
    if split:
        # Outputs disagree: drive them all to the intended state even when
        # that matches what we already believed.
        want = want if want is not None else fan_on
        why = why or "resynchronising outputs"
    elif want is None or want == fan_on:
        return
    if want is None:
        return
    if not ids:
        raise HueError("No Hue lights selected yet")
    for lid in ids:
        h.set(lid, want)
    with _state_lock:
        STATE["fan"] = want
        STATE["changed_at"] = now
    _log("Fan %s \u2014 %s" % ("on" if want else "off", why))



def _failsafe():
    """Ventilate when readings have been stale too long.

    Repeated sensor failures otherwise leave the fan off indefinitely with
    no valid reading. Opt-in: stale_minutes of 0 disables it entirely.
    """
    mins = int(CFG["control"].get("stale_minutes") or 0)
    if not mins or CFG["control"]["mode"] != "auto":
        return
    with _state_lock:
        last = STATE["reading_at"]
        already = STATE["fan"]
    # "Never read anything" is measured from startup, not treated as fresh:
    # otherwise a service that never got a first reading stayed passive.
    since = last if last is not None else START_TIME
    if time.time() - since < mins * 60:
        return
    if already:
        return
    ids = CFG["hue"]["lights"]
    if not ids:
        return
    _, h = _clients()
    for lid in ids:
        h.set(lid, True)
    with _state_lock:
        STATE["fan"] = True
        STATE["changed_at"] = time.time()
    _log("FAIL-SAFE: no reading for %d min, ventilating" % mins)


def loop():
    fails = 0
    while True:
        try:
            # Inside the try: a bad value here used to kill the thread.
            base = max(5, min(3600, int(float(CFG["control"]["poll_seconds"]))))
        except (TypeError, ValueError, KeyError):
            base = 30
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
        if fails:
            # After both branches: a login timeout or malformed payload is
            # exactly when ventilation matters, and neither raises
            # ProtectError or HueError.
            try:
                _failsafe()
            except Exception as e:
                _log("Fail-safe failed: %s: %s" % (type(e).__name__, e))
        # Back off on repeated failures so a rate limit is not made worse.
        delay = base if not fails else min(300, base * 2 ** min(fails, 5))
        # Wake early when settings change so mode switches are instant.
        _wake.wait(delay)
        _wake.clear()


app = Flask(__name__)


def _safe_cfg():
    c = copy.deepcopy(CFG)
    c["ui"]["password_hash"] = "SET" if CFG["ui"]["password_hash"] else ""
    c["protect"]["password"] = "SET" if CFG["protect"]["password"] else ""
    c["hue"]["key"] = "SET" if CFG["hue"]["key"] else ""
    return c


@app.after_request
def _no_cache(resp):
    """The UI is small and always dynamic; never let a browser hold a stale
    copy, which otherwise hides fixes behind a hard refresh."""
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def set_ui_password(plain):
    """Store a salted PBKDF2 hash. The plaintext is never written."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, 200000)
    return "pbkdf2$200000$%s$%s" % (salt.hex(), dk.hex())


def check_ui_password(plain, stored):
    try:
        _, rounds, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(),
                                 bytes.fromhex(salt), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


@app.before_request
def _require_auth():
    """Basic auth, active only once a password is set.

    Auth is off by default so an unattended upgrade cannot lock anyone out;
    setting ui_password turns it on for every route at once, including the
    API, rather than leaving state-changing endpoints exposed.
    """
    stored = CFG.get("ui", {}).get("password_hash") or ""
    if not stored:
        return None
    a = request.authorization
    if a and a.password and check_ui_password(a.password, stored):
        return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Fan Controller"'})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/clienterror", methods=["POST"])
def api_clienterror():
    """Surface browser-side JS failures in the server log, since a script
    that dies on load leaves no other trace."""
    b = _json_body() or {}
    msg = str(b.get("msg", ""))[:300]
    where = "%s:%s" % (str(b.get("src", ""))[:120], b.get("line", "?"))
    app.logger.error("UI error: %s @ %s", msg, where)
    _log("UI error: %s" % msg[:120])
    return jsonify(ok=True)


def _json_body():
    """Require a real JSON content type on state-changing requests.

    get_json(force=True) parsed any content type, so a cross-origin form
    post - which needs no preflight - could drive these endpoints.
    """
    if not request.is_json:
        return None
    return request.get_json(silent=True) or {}


@app.route("/api/ui/password", methods=["POST"])
def api_ui_password():
    body = _json_body()
    if body is None:
        return jsonify(error="Send JSON with Content-Type "
                             "application/json."), 415
    pw = body.get("password")
    if pw is None or (pw != "" and len(pw) < 8):
        return jsonify(error="Password must be at least 8 characters, or "
                             "empty to disable authentication."), 400
    CFG["ui"]["password_hash"] = set_ui_password(pw) if pw else ""
    save_cfg(CFG)
    _log("UI password %s" % ("set" if pw else "cleared"))
    return jsonify(ok=True, enabled=bool(pw))


@app.route("/api/state")
def api_state():
    with _state_lock:
        s = copy.deepcopy(STATE)
    s["config"] = _safe_cfg()
    return jsonify(s)


def _guard_credentials(cand, body):
    """Clear a stored secret when the host it belongs to changes.

    Otherwise anyone reaching the API could repoint the service at a server
    they control and have it authenticate there with the real credentials,
    turning a config write into credential theft. Supplying a new secret in
    the same request is a deliberate reconfiguration and is honoured.
    """
    notes = []
    bp = body.get("protect") or {}
    if (cand["protect"]["host"] != CFG["protect"]["host"]
            and bp.get("password") in (None, "", "SET")):
        cand["protect"]["password"] = ""
        notes.append("Console address changed, so the UniFi password was "
                     "cleared. Enter it again.")
    bh = body.get("hue") or {}
    if (cand["hue"]["bridge"] != CFG["hue"]["bridge"]
            and bh.get("key") in (None, "", "SET")):
        cand["hue"]["key"] = ""
        notes.append("Bridge address changed, so the Hue key was cleared. "
                     "Pair again.")
    return " ".join(notes) or None


@app.route("/api/config", methods=["POST"])
def api_config():
    """Validate against a copy so a rejected save cannot alter live settings."""
    body = _json_body()
    if body is None:
        return jsonify(error="Send JSON with Content-Type "
                             "application/json."), 415
    cand = copy.deepcopy(CFG)
    for section in ("protect", "hue", "control"):
        raw = {k: v for k, v in (body.get(section) or {}).items()
               if v != "SET"}
        try:
            cand[section].update(normalise(section, raw))
        except ValueError as e:
            return jsonify(error=str(e)), 400
    notice = _guard_credentials(cand, body)
    cand["protect"]["verify_tls"] = bool(
        cand["protect"].get("verify_tls"))
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
    return jsonify(ok=True, config=_safe_cfg(), notice=notice)


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
    atexit.register(flush_hist)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, flush_hist)
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8096, threaded=True)
