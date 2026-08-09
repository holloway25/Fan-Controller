"""Air-quality-driven fan control via a Tasmota smart plug.

Reads CO2, temperature and humidity from a UniFi Protect UP-AirQuality
sensor and switches the plug with hysteresis. Settings are editable in
the web UI.

Protect 7.1's documented API omits air-quality metrics, so this reads the
private /proxy/protect/api/bootstrap endpoint. That is undocumented and may
break on firmware updates; use the Debug view to inspect the live payload.
"""
import atexit, copy, hashlib, hmac, json, math, os, signal, socket, sys
import threading, time
import requests, urllib3
from flask import Flask, Response, jsonify, request, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.json")

DEFAULTS = {
    "protect": {"host": "192.168.1.1", "username": "", "password": "",
                "verify_tls": False,
                "sensor_id": ""},
    "plug": {"host": "192.168.1.10", "user": "admin", "password": "",
             "watts_enabled": False, "watts_min": 15.0,
             "watts_max": 25.0, "watts_fault_seconds": 60},
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
    "mode": ("enum", ("auto", "manual", "on")),
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
PLUG_SCHEMA = {
    "host": ("str", 1, 253), "user": ("str", 0, 64),
    "password": ("str", 0, 128),
    "watts_enabled": ("bool",),
    "watts_min": ("num", 1.0, 5000.0),
    "watts_max": ("num", 1.0, 5000.0),
    "watts_fault_seconds": ("int", 5, 3600),
}
SCHEMAS = {"protect": PROTECT_SCHEMA, "plug": PLUG_SCHEMA, "ui": UI_SCHEMA,
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
            # Unknown top-level entries are discarded rather than carried
            # forward, matching the per-section rejection of unknown keys.
            if k in cfg and isinstance(v, dict):
                cfg[k].update(v)
    ctl = cfg.get("control")
    ctl = ctl if isinstance(ctl, dict) else {}
    if ctl.get("mode") == "off":
        # Force off was removed; manual now switches off once,
        # then leaves the fan alone.
        ctl["mode"] = "manual"
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
    # Created 0600 up front: the temp file holds the UniFi and plug
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
# When the draw first left its band. None while healthy, so a single good
# reading resets the clock rather than accumulating toward a fault.

FAULT_PATH = os.path.join(DIR, "fault.json")


def load_fault():
    """A fault outlives a restart: only acknowledgement clears it.

    Held in memory alone, restarting the service - or the host losing
    power - would silently re-arm a fan already judged faulty.
    """
    try:
        d = json.load(open(FAULT_PATH))
        return d if isinstance(d, dict) and d.get("msg") else None
    except (ValueError, OSError):
        return None


def save_fault(f):
    """Written atomically: a torn file must not read as no fault."""
    try:
        if f is None:
            if os.path.exists(FAULT_PATH):
                os.remove(FAULT_PATH)
            return
        tmp = FAULT_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(f, fh)
        os.replace(tmp, FAULT_PATH)
    except OSError:
        pass
_oob_since = None


EVENTS_PATH = os.path.join(DIR, "events.json")
EVENTS_MAX = 300      # kept on disk
EVENTS_SHOWN = 30     # returned to the UI each poll


def load_events():
    try:
        e = json.load(open(EVENTS_PATH))
    except (ValueError, OSError):
        return []
    if not isinstance(e, list):
        return []
    return [x for x in e
            if isinstance(x, dict) and "at" in x and "msg" in x][:EVENTS_MAX]


def save_events(events):
    """Written on each entry; entries are infrequent and small."""
    try:
        tmp = EVENTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f, separators=(",", ":"))
        os.replace(tmp, EVENTS_PATH)
    except OSError:
        pass

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


class PlugError(Exception):
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
        except requests.RequestException as e:
            # Ordered after SSLError, which is a subclass of this. Without
            # it a DNS or connection failure during login escaped as a raw
            # requests exception, while the identical failure in _get()
            # reported cleanly. Most visible on restart: logging in is the
            # first thing the poll loop does, and the resolver may not be
            # answering yet. Wording matches _get() deliberately.
            raise ProtectError("Cannot reach the UniFi console: %s" % e)
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


class Tasmota:
    """Athom/Tasmota plug over the HTTP command API.

    Credentials go in the query string because /cm accepts no other form;
    the URL is never logged. Single relay, so unlike the previous Hue plug
    client there is no multi-output split state to reconcile.
    """

    def __init__(self, host, user="", password=""):
        self.host = host
        self.user = user or "admin"
        self.password = password or ""

    def _cmd(self, cmnd):
        params = {"cmnd": cmnd}
        if self.password:
            params["user"] = self.user
            params["password"] = self.password
        try:
            r = requests.get("http://%s/cm" % self.host, params=params,
                             timeout=12)
        except requests.RequestException as e:
            # Only the exception type: requests puts the full URL in the
            # message, and the credentials are query parameters.
            raise PlugError("Cannot reach the plug at %s (%s)"
                            % (self.host, type(e).__name__))
        if r.status_code >= 400:
            raise PlugError("Plug returned HTTP %d" % r.status_code)
        if not r.content:
            raise PlugError("Plug returned an empty reply. Check that the "
                            "HTTP command API is enabled on the device.")
        try:
            data = r.json()
        except ValueError:
            raise PlugError("Plug returned a non-JSON reply")
        if "WARNING" in data:
            raise PlugError("Plug rejected the credentials: %s"
                            % data["WARNING"])
        return data

    @staticmethod
    def _power(data):
        for k in ("POWER", "POWER1"):
            if k in data:
                return str(data[k]).upper() == "ON"
        return None

    def state(self):
        """True or False. Raises if the plug reports no power field, so an
        unknown state is never mistaken for off."""
        v = self._power(self._cmd("Power"))
        if v is None:
            raise PlugError("Plug reported no power state")
        return v

    def set(self, on):
        """Switch, and confirm from the same reply so a rejected command
        cannot be recorded as a success."""
        got = self._power(self._cmd("Power %s" % ("ON" if on else "OFF")))
        if got is None:
            raise PlugError("Plug reported no power state after switching")
        if got != bool(on):
            raise PlugError("Plug reported %s after being told %s"
                            % ("ON" if got else "OFF",
                               "ON" if on else "OFF"))
        return got

    def power_w(self):
        """Present draw in watts, or None. Confirms the fan is actually
        running rather than trusting that the relay closed."""
        d = self._cmd("Status 8")
        try:
            return float(d["StatusSNS"]["ENERGY"]["Power"])
        except (KeyError, TypeError, ValueError):
            return None


STATE = {
    "co2": None, "co2_path": None, "fan": None, "reading_at": None,
    "changed_at": None, "error": None, "sensor_name": None,
    "temperature": None, "humidity": None, "pm25": None,
    "watts": None, "watts_at": None,
    "latch": {"co2": False, "temp": False},
    "manual_off_pending": False,
    "history": [], "events": [], "history_rev": 0, "fault": None,
}
# RLock, not Lock: nested acquisition deadlocked the poll thread.
_state_lock = threading.RLock()
START_TIME = time.time()
_wake = threading.Event()  # set to make the poll loop act at once
# Force on is asserted from both the poll thread and the draw thread, so
# the read-decide-switch has to be atomic or the two can each see the fan
# off, both switch it, and both log the same event.
_act_lock = threading.Lock()
# Guards the fault as one transaction across memory and disk. Held with
# _state_lock inside it, and never together with _act_lock, so the two
# stay independent and there is no ordering between them to get wrong.
_fault_lock = threading.Lock()
CFG = load_cfg()
STATE["history"] = load_hist()
# Reloaded, not reset: a fault must survive a restart or the acknowledgement
# it is waiting for can be skipped by a power cycle.
STATE["fault"] = load_fault()
STATE["history_rev"] = 1
STATE["events"] = load_events()


def _redact(text):
    """Strip stored secrets from anything reaching the public API.

    /api/state is deliberately readable without a password and error text
    ends up there. Reporting only exception types at each raise site is the
    primary defence; this is the backstop for paths not anticipated.
    """
    out = str(text)
    for section, key in (("plug", "password"), ("protect", "password"),
                         ("ui", "password_hash")):
        secret = (CFG.get(section) or {}).get(key)
        if secret and len(str(secret)) >= 4:
            out = out.replace(str(secret), "***")
    return out



def _log(msg):
    with _state_lock:
        STATE["events"].insert(0, {"at": time.time(), "msg": msg})
        del STATE["events"][EVENTS_MAX:]
        snapshot = list(STATE["events"])
    # Written outside the lock; entries survive a restart.
    save_events(snapshot)


_CLIENTS = {"key": None, "protect": None, "plug": None}
_clients_lock = threading.Lock()


def _clients():
    """Reuse one authenticated session per config.

    A new Protect object per poll meant a fresh /api/auth/login every 30s,
    which UniFi rate-limits with HTTP 429.
    """
    p, g = CFG["protect"], CFG["plug"]
    key = (p["host"], p["username"], p["password"],
           bool(p.get("verify_tls")), g["host"], g["user"], g["password"])
    with _clients_lock:
        if _CLIENTS["key"] != key:
            _CLIENTS.update(key=key,
                            protect=Protect(p["host"], p["username"],
                                            p["password"],
                                            p.get("verify_tls")),
                            plug=Tasmota(g["host"], g["user"],
                                         g["password"]))
        return _CLIENTS["protect"], _CLIENTS["plug"]


def decide(co2, temp, fan_on, c, ran_for, latch, fault=None):
    """Per-rule latching hysteresis. Mutates `latch` in place.

    Each input latches on above its own on-level and releases below its
    own off-level. The fan runs while any rule is latched. This matters:
    previously a rule that had never triggered could still veto the stop,
    so a CO2-driven run was held on by temperature indefinitely.
    """
    if c["mode"] == "on":
        return True, "forced on"
    # Force on is checked first on purpose: it overrides a fault so a
    # repaired fan can be tested. Past here, a fault blocks automatic
    # starts, including the stale-reading failsafe.
    if fault:
        return False, "draw fault"
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


def _fan_status(plug):
    """What the plug says it is doing, or None if unreadable.

    Single relay, so there is no multi-output split to reconcile as there
    was with the previous multi-output client.
    """
    try:
        return plug.state()
    except PlugError:
        return None


def _manual_off(plug):
    """The one-time switch-off on entering manual mode.

    Runs before the sensor read, so a Protect outage cannot block a manual
    command, and the flag is cleared only once the plug has confirmed the
    switch - otherwise a failed call silently dropped the request forever.
    """
    with _act_lock:
        if CFG["control"].get("mode") != "manual":
            # Mirror of the guard in _force_on. The pending flag is left
            # set on purpose: if manual is entered again the one-time
            # stop is still owed.
            return
        _manual_off_locked(plug)


def _manual_off_locked(plug):
    """The body of _manual_off, with _act_lock already held."""
    with _state_lock:
        pending = STATE.get("manual_off_pending")
    if not pending:
        return
    try:
        already_off = plug.state() is False
    except PlugError:
        already_off = False          # unknown: switch anyway
    if not already_off:
        plug.set(False)              # raises: flag stays set, retried
    with _state_lock:
        STATE["manual_off_pending"] = False
        STATE["fan"] = False
        STATE["changed_at"] = time.time()
    if not already_off:
        _log("Manual mode - switched off, now hands off")

def _force_on(plug):
    """Hold the fan on without consulting Protect.

    Force on is reached for precisely when the sensor cannot be trusted -
    a console that is down, a payload that stopped resolving - so it must
    not sit behind the reading, or the override would be unavailable
    exactly when it is wanted. Before the read for the same reason
    _manual_off is, and it does not return early: readings are still
    recorded while forced on, so the 24h trace has no gap.

    Re-asserted rather than latched, so a fan switched off at the plug or
    by Google Home comes back on while the mode stands. Called from the
    draw thread as well as tick(): the poll loop backs off to 300s while
    Protect is unreachable, and the override must not wait that long to
    undo an external switch-off. Idempotent, and serialised on _act_lock
    so the two callers cannot both switch and both log.
    """
    with _act_lock:
        if CFG["control"].get("mode") != "on":
            # Re-checked under the lock, not just by the caller. Manual's
            # stop is one-shot: if it has already run and cleared its
            # flag, switching on here would leave the fan going with
            # nothing left to correct it.
            return
        actual = _fan_status(plug)   # None when the plug cannot be read
        if actual is True:
            with _state_lock:
                STATE["fan"] = True  # no change, so no lease and no event
            return
        with _state_lock:
            was_on = STATE["fan"] is True
        if actual is False and was_on:
            # Confirmed off while we believed it on, so something else
            # switched it. Recorded before we undo it, as the automatic
            # path does - forcing it back must not swallow the evidence.
            # Only on a confirmed False: an unreadable plug is unknown,
            # not off, and reporting it as a switch invents an event.
            _log("Fan switched to off outside this service")
        plug.set(True)               # raises: retried on the next cycle
        now = time.time()
        with _state_lock:
            STATE["fan"] = True
            STATE["changed_at"] = now
        _log("Fan on \u2014 forced on")


def tick():
    p, h = _clients()
    c = CFG["control"]
    # Before the sensor read: a manual stop must not depend on Protect,
    # and neither may the override that exists for when Protect is the
    # thing that has failed. Both actuate here, then fall through so the
    # reading is still recorded.
    if c["mode"] == "manual":
        _manual_off(h)
    elif c["mode"] == "on":
        _force_on(h)
    sid = CFG["protect"]["sensor_id"]
    if not sid:
        raise ProtectError("No air-quality sensor selected yet")
    vals, path = p.readings(sid)
    co2 = vals['co2']
    now = time.time()
    actual = _fan_status(h)
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
        STATE["history_rev"] += 1
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
    if c["mode"] == "manual":
        return
    want, why = decide(co2, vals.get('temperature'), fan_on, c, ran_for,
                       STATE["latch"], STATE["fault"])
    if want is None or want == fan_on:
        return
    h.set(want)
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
    # A fault means the fan is not doing anything useful, so ventilating
    # with it is a fiction. Blocking here as well as in decide() matters:
    # this path switches the plug directly and never consults decide().
    with _state_lock:
        if STATE["fault"]:
            return
    with _state_lock:
        last = STATE["reading_at"]
    # "Never read anything" is measured from startup, not treated as fresh:
    # otherwise a service that never got a first reading stayed passive.
    since = last if last is not None else START_TIME
    if time.time() - since < mins * 60:
        return
    # Ask the plug rather than trusting cached state: it may have rebooted
    # off, or been switched off while Protect was unreachable.
    _, h = _clients()
    try:
        if h.state() is True:
            return
    except PlugError:
        pass                         # unreadable: assert ON anyway
    h.set(True)
    with _state_lock:
        STATE["fan"] = True
        STATE["changed_at"] = time.time()
    _log("FAIL-SAFE: no reading for %d min, ventilating" % mins)



WATTS_SECONDS = 5
WATTS_STALE = 15   # a reading older than this is reported as unknown
SETTLE_SECONDS = 20  # spin-up grace before draw is judged



def _store_fault(value):
    """Set the fault in memory and on disk as one transaction.

    Three paths write it - raising, confirming the stop, acknowledging -
    and each used to update STATE and then write the file as a separate
    step. An acknowledgement landing in that gap was then overwritten by
    a stale snapshot from another thread, leaving memory clear but disk
    faulted. Nothing showed it until a restart reloaded the file and
    blocked automatic running again, the stale-reading failsafe with it.

    Returns the fault that was there before.
    """
    with _fault_lock:
        with _state_lock:
            had = STATE["fault"]
            STATE["fault"] = value
        save_fault(value)
    return had


def _confirm_fault_stopped():
    """Record that the plug confirmed the stop. True if this call did it.

    Re-reads the fault inside the lock rather than trusting a snapshot
    taken earlier: an acknowledgement may have landed in between, and
    writing the older value back would resurrect a cleared fault.
    """
    with _fault_lock:
        with _state_lock:
            cur = STATE["fault"]
            if not cur or cur.get("stopped"):
                return False
            cur["stopped"] = True
            snap = dict(cur)
        save_fault(snap)
    return True


def _enforce_stop(plug):
    """Keep a faulted fan off until the fault is acknowledged.

    Runs every cycle while a fault stands, not once when it is raised:
    the stale-reading failsafe or an external switch can turn the fan
    back on, and a stop command that failed must be retried rather than
    assumed. The fault records whether the plug actually confirmed it.
    """
    if plug is None:
        return
    with _act_lock:
        if CFG["control"].get("mode") == "on":
            # Force on overrides a fault by design, and the mode can move
            # between _draw_check reading it and this point. Without the
            # re-check the stop could land just after _force_on switched
            # the fan on, flapping it off until the next draw cycle put
            # it back.
            return
        try:
            if plug.state() is not False:
                plug.set(False)
        except PlugError:
            return                  # unconfirmed: retried next cycle
        with _state_lock:
            STATE["fan"] = False
    # Outside _act_lock: the fault write takes its own lock, and a disk
    # write must not extend a hold a settings save may be waiting on.
    if _confirm_fault_stopped():
        _log("Draw fault - fan stop confirmed")

def _draw_check(plug):
    """Fault the fan if it runs outside its expected draw for long enough.

    Force on takes precedence: it is how a repaired fan gets tested, so
    the detector stands down there rather than fighting it.
    """
    global _oob_since
    if CFG["control"]["mode"] == "on":
        _oob_since = None
        return
    with _state_lock:
        fault = STATE["fault"]
    if fault:
        _enforce_stop(plug)
        return
    pg = CFG["plug"]
    if not pg.get("watts_enabled"):
        _oob_since = None
        return
    now = time.time()
    with _state_lock:
        fan, w = STATE["fan"], STATE["watts"]
        w_at, changed = STATE["watts_at"], STATE["changed_at"]
    # A motor spinning up draws nothing yet, and a fan meant to be off is
    # not a fault. changed_at is deliberately left unset when startup
    # finds the fan already running, so fall back to START_TIME: treating
    # None as never-settled disabled draw protection for as long as that
    # run lasted, which on a high-CO2 day is hours.
    since = changed if changed is not None else START_TIME
    if fan is not True or now - since <= SETTLE_SECONDS:
        _oob_since = None
        return
    # Freshness is judged here, not taken from the cached value: when the
    # plug stops answering, STATE keeps the last good reading and would
    # otherwise look healthy for as long as the outage lasted. No reading
    # is out of band, since an unanswering plug is what this catches.
    if w_at is None or (now - w_at) > WATTS_STALE:
        w = None
    lo, hi = float(pg["watts_min"]), float(pg["watts_max"])
    if w is not None and lo <= w <= hi:
        _oob_since = None
        return
    if _oob_since is None:
        _oob_since = now
        return
    if now - _oob_since < float(pg["watts_fault_seconds"]):
        return
    reading = "no reading" if w is None else ("%g W" % w)
    msg = ("Draw fault - %s outside %g-%g W for %ds"
           % (reading, lo, hi, int(now - _oob_since)))
    f = {"at": now, "msg": msg, "stopped": False}
    _store_fault(f)
    _oob_since = None
    _log(msg)
    _enforce_stop(plug)


def watts_loop():
    """Poll the plug's meter on its own short cadence.

    Draw is read here rather than in tick() because the Protect bootstrap
    is the expensive call: tying watts to poll_seconds left the reading up
    to 30s stale while the UI refreshed every 5s. Errors are swallowed so
    a plug hiccup cannot overwrite the control loop's error.
    """
    while True:
        plug = None
        try:
            _, plug = _clients()
            w = plug.power_w()
            with _state_lock:
                STATE["watts"] = w
                STATE["watts_at"] = time.time()
        except Exception:
            # Left for the staleness check rather than cleared here: one
            # missed request should not blank a valid reading, but a
            # stale one must never be presented as current.
            pass
        # Outside the read, so losing the plug still reaches the detector.
        # Inside, a failed power_w() jumped straight to except and the
        # out-of-band timer never started.
        try:
            if plug is not None and CFG["control"]["mode"] == "on":
                # Enforced on this thread too. The poll loop backs off to
                # 300s while Protect is unreachable, so tick() alone could
                # leave a fan switched off elsewhere stopped for minutes
                # while the mode says otherwise.
                _force_on(plug)
        except Exception:
            pass
        # Called even in Force on, where it stands down and clears the
        # out-of-band timer. Skipping it there would leave a stale start
        # time to resume from and could fault the fan early on the way out.
        try:
            _draw_check(plug)
        except Exception:
            pass
        time.sleep(WATTS_SECONDS)

def _wait_for_dns(timeout=30, interval=3):
    """Wait for the Protect host to resolve, up to `timeout` seconds.

    The unit is ordered after network-online.target, but that only proves
    the link has an address - not that the resolver is answering. A host
    that boots quickly can issue its first Protect login before DNS is
    available and lose the race, failing with EAI_AGAIN ("Temporary
    failure in name resolution") and backing off for a minute before
    recovering on its own.

    Waiting here rather than in the unit keeps the hostname in config.json
    alone, instead of duplicating it into /etc/hosts or an ExecStartPre
    where an edit made in the UI would never reach it.

    Nothing else may be held up by this. Mode changes are applied by the
    poll loop, not by the route that sets them, so the wait ends early on
    `_wake`: Manual and Force on reach tick() in about a second instead of
    queueing behind a resolver that is not answering. The host is re-read
    each attempt for the same reason - correcting a wrong hostname in the
    UI must not leave the wait chasing the old one.

    The ceiling sits below 60s, the shortest fail-safe window
    `stale_minutes` can express, so ventilating on stale readings is never
    delayed by waiting for DNS.

    Bounded, not infinite: a genuinely wrong hostname must still reach the
    normal error path so the UI can report it. True if it resolved.
    """
    deadline = time.time() + timeout
    waited = False
    while True:
        host = (CFG.get("protect") or {}).get("host")
        if not host:
            return False       # unconfigured; tick() reports that properly
        try:
            # A literal address needs no resolver. AI_NUMERICHOST fails
            # fast on anything else, so this never queries DNS.
            socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)
            return True
        except socket.gaierror:
            pass
        try:
            socket.getaddrinfo(host, None)
            if waited:
                _log("Resolved %s, starting normally" % host)
            return True
        except socket.gaierror:
            pass
        if time.time() >= deadline:
            _log("Could not resolve %s after %ds, starting anyway"
                 % (host, timeout))
            return False
        if not waited:
            _log("Waiting for DNS to resolve %s" % host)
            waited = True
        if _wake.wait(interval):
            # Settings changed. Cleared here rather than left for loop(),
            # which would otherwise consume it after its first tick() and
            # poll a second time immediately. That tick applies the change.
            _wake.clear()
            return False


def _fail(msg, last):
    """Record a poll failure in STATE and, once, in the activity log.

    STATE["error"] is the live value the dashboard shows. The log entry is
    what survives the tab being closed: this branch previously wrote only
    to STATE, so a restart-time failure left no trace in journalctl or
    events.json and could be caught only by watching the UI at the time.

    Logged only when the message changes. The loop backs off to at most
    300s, so a sustained outage would otherwise append an identical entry
    every few minutes and push real switching history out of the
    300-entry window.
    """
    with _state_lock:
        STATE["error"] = msg
    if msg != last:            # outside the lock: _log takes it itself
        _log(msg)
    return msg


def _failsafe_cap(delay):
    """Never sleep past the moment the stale-reading fail-safe falls due.

    The backoff exists so a rate limit is not made worse, but it also
    decides when _failsafe() is next evaluated. With a 30s poll the first
    failure sleeps 60s, so a 1 minute stale window was checked at 90s
    rather than 60s and ventilation started late.

    Shortens only, never extends, and only while the deadline is still
    ahead. Once it has passed _failsafe() has already run, and the normal
    backoff must resume: capping on a deadline in the past would leave a
    zero delay and spin the loop against a console that is very likely
    rate limiting already.
    """
    c = CFG["control"]
    if c.get("mode") != "auto":
        return delay                 # _failsafe only acts in auto
    try:
        mins = int(c.get("stale_minutes") or 0)
    except (TypeError, ValueError):
        return delay
    if mins <= 0:
        return delay                 # fail-safe disabled
    with _state_lock:
        last = STATE["reading_at"]
    # Measured from startup when nothing has been read, matching _failsafe.
    due = (last if last is not None else START_TIME) + mins * 60
    remaining = due - time.time()
    if remaining <= 0:
        return delay
    return min(delay, remaining)


def loop():
    # Ordered after network-online.target, which proves an address but not
    # a working resolver. See _wait_for_dns.
    _wait_for_dns()
    fails = 0
    last_logged = None
    while True:
        try:
            # Inside the try: a bad value here used to kill the thread.
            base = max(5, min(3600, int(float(CFG["control"]["poll_seconds"]))))
        except (TypeError, ValueError, KeyError):
            base = 30
        try:
            tick()
            if last_logged is not None:
                _log("Sensor poll recovered")
            fails = 0
            last_logged = None
        except (ProtectError, PlugError) as e:
            # Logged as well as shown: making login() raise ProtectError
            # moved the restart-time DNS failure into this branch, so
            # logging only the generic one below would hide it again.
            fails += 1
            last_logged = _fail(_redact(e), last_logged)
        except Exception as e:  # keep the loop alive on anything unexpected
            fails += 1
            last_logged = _fail(
                _redact("%s: %s" % (type(e).__name__, e)), last_logged)
        if fails:
            # After both branches: a login timeout or malformed payload is
            # exactly when ventilation matters, and neither raises
            # ProtectError or PlugError.
            try:
                _failsafe()
            except Exception as e:
                _log(_redact("Fail-safe failed: %s: %s"
                 % (type(e).__name__, e)))
        # Back off on repeated failures so a rate limit is not made worse.
        delay = base if not fails else min(300, base * 2 ** min(fails, 5))
        if fails:
            # The backoff must not outrun the fail-safe's own deadline.
            delay = _failsafe_cap(delay)
        # Wake early when settings change so mode switches are instant.
        _wake.wait(delay)
        _wake.clear()


app = Flask(__name__)

# One reverse proxy fronts this service, so the peer address is always
# the proxy and every request logged as the same client. Trust a single
# hop of X-Forwarded-For / -Proto / -Host to recover the real client.
# One hop only: trusting more lets a client forge the chain.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _safe_cfg():
    c = copy.deepcopy(CFG)
    c["ui"]["password_hash"] = "SET" if CFG["ui"]["password_hash"] else ""
    c["protect"]["password"] = "SET" if CFG["protect"]["password"] else ""
    c["plug"]["password"] = "SET" if CFG["plug"]["password"] else ""
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




# Readable without a password; everything else needs one once set.
# /api/debug/sensor is deliberately not public: it returns the raw
# Protect payload, which is diagnostic data, not dashboard data.
PUBLIC_GET = {"/", "/api/state", "/api/history"}
PUBLIC_POST = {"/api/clienterror"}


@app.before_request
def _require_auth():
    """Basic auth on anything that changes state or exposes internals.

    Inactive until ui.password_hash is set, so an upgrade cannot lock
    anyone out. Viewing stays open so a wall display needs no password.
    """
    stored = CFG.get("ui", {}).get("password_hash") or ""
    if not stored:
        return None
    path, method = request.path, request.method
    # Icons must load without credentials, same as / and /api/state, so a
    # wall display is not left with a broken tab icon once auth is on.
    if method in ("GET", "HEAD") and path.startswith("/static/"):
        return None
    if method in ("GET", "HEAD") and path in PUBLIC_GET:
        return None
    if method == "POST" and path in PUBLIC_POST:
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


@app.route("/signin")
def signin():
    """Protected page whose only job is to make the browser prompt.

    A 401 from fetch() does not reliably raise the Basic auth dialog,
    so a navigation is needed to obtain and cache credentials."""
    return Response("<p style='font:16px sans-serif;padding:2em'>"
                    "Signed in. <a href='/'>Back to the controller</a>"
                    "</p>", 200, {"Content-Type": "text/html"})


@app.route("/api/state")
def api_state():
    with _state_lock:
        # Exclude history from the copy rather than dropping it afterwards:
        # deep-copying ~2500 points twelve times a minute is pure waste. It
        # is ~99% of the payload and changes once per poll, not once per UI
        # refresh, so it moves to /api/history and history_rev says when to
        # refetch.
        s = copy.deepcopy({k: v for k, v in STATE.items()
                           if k != "history"})
    # Trim: 300 events every 5s is needless payload; the rest stay on disk.
    # An unreachable plug must not leave a positive draw on screen: a
    # stale reading is reported as unknown, not as current.
    if not s["watts_at"] or time.time() - s["watts_at"] > WATTS_STALE:
        s["watts"] = None
    s["events"] = s["events"][:EVENTS_SHOWN]
    s["config"] = _safe_cfg()
    return jsonify(s)



@app.route("/api/fault/clear", methods=["POST"])
def api_fault_clear():
    """Acknowledge a draw fault and allow automatic runs again.

    Deliberately manual. Retrying on a timer would switch a seized motor
    on every few minutes forever, which is the thing the fault exists to
    stop.
    """
    global _oob_since
    # One transaction: clearing memory and deleting the file separately
    # let a concurrent confirmation write the old fault back afterwards.
    had = _store_fault(None)
    _oob_since = None
    if had:
        _log("Draw fault cleared")
    _wake.set()
    return jsonify(ok=True, cleared=bool(had))
@app.route("/api/history")
def api_history():
    """The 24h trace, split out of /api/state.

    Returned with its own revision so a client that fetches while the poll
    thread appends stores the revision it actually received, rather than
    the one it saw a moment earlier and then missing the next change.
    """
    with _state_lock:
        return jsonify({"history": list(STATE["history"]),
                        "history_rev": STATE["history_rev"]})


MODE_LABELS = {"auto": "Auto", "manual": "Manual", "on": "Force on"}
SECRET_FIELDS = {("protect", "password"), ("plug", "password"),
                 ("ui", "password_hash")}


def _describe_changes(cand):
    """Human summary of what a save actually alters, or None if nothing.

    A generic "Settings saved" on every click buried the fan events that
    matter, and fired even when a save changed nothing at all.
    """
    # Only known sections: an unknown scalar at the top level would
    # otherwise raise AttributeError and fail every save with a 500.
    changed = [(sec, k) for sec in SCHEMAS
               for k, v in cand.get(sec, {}).items()
               if CFG.get(sec, {}).get(k) != v]
    if not changed:
        return None
    if ("control", "mode") in changed and len(changed) == 1:
        mode = cand["control"]["mode"]
        return "Mode: %s" % MODE_LABELS.get(mode, mode)
    names = sorted({"%s (hidden)" % k if (sec, k) in SECRET_FIELDS else k
                    for sec, k in changed})
    return "Changed: %s" % ", ".join(names)

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
    bg = body.get("plug") or {}
    if (cand["plug"]["host"] != CFG["plug"]["host"]
            and bg.get("password") in (None, "", "SET")):
        cand["plug"]["password"] = ""
        notes.append("Plug address changed, so its password was cleared. "
                     "Enter it again.")
    return " ".join(notes) or None


@app.route("/api/config", methods=["POST"])
def api_config():
    """Validate against a copy so a rejected save cannot alter live settings."""
    body = _json_body()
    if body is None:
        return jsonify(error="Send JSON with Content-Type "
                             "application/json."), 415
    cand = copy.deepcopy(CFG)
    for section in ("protect", "plug", "control"):
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
    pg = cand["plug"]
    try:
        w_lo, w_hi = float(pg["watts_min"]), float(pg["watts_max"])
    except (TypeError, ValueError):
        return jsonify(error="Draw limits must be numbers."), 400
    if w_lo >= w_hi:
        return jsonify(error="Expected draw low must be below high."), 400
    summary = _describe_changes(cand)
    # Committed under _act_lock so the mode cannot change midway through
    # an actuation. Without it, a Force on that had already passed its
    # mode check could land after manual's one-time stop had run and
    # cleared its flag, leaving the fan on with nothing to correct it.
    # Waits for an in-flight switch, so a save can block for as long as
    # the plug takes to answer. Order is _act_lock then _state_lock
    # everywhere, which is why this cannot deadlock.
    with _act_lock:
        was = CFG["control"].get("mode")
        for section in cand:
            CFG[section] = cand[section]
        now_mode = CFG["control"].get("mode")
        if now_mode == "manual" and was != "manual":
            # Entering manual switches the fan off once, then hands to the
            # plug's own controls or Google Home without interference.
            with _state_lock:
                STATE["manual_off_pending"] = True
        if was == "on" and now_mode != "on":
            # Leaving a forced state: do not let the minimum-run timer
            # from the forced switch hold the fan on once auto resumes.
            with _state_lock:
                STATE["changed_at"] = None
    save_cfg(CFG)
    if summary:
        _log(summary)
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


@app.route("/api/plug/test", methods=["POST"])
def api_plug_test():
    """Confirm the plug answers, and report draw as well as relay state."""
    _, g = _clients()
    try:
        state = g.state()
        watts = g.power_w()
        return jsonify(ok=True, on=state, watts=watts)
    except PlugError as e:
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
    threading.Thread(target=watts_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=8096, threaded=True)
