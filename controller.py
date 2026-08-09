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
                # Must stay True: load_cfg fills missing keys from
                # here, so this is what a config.json written before
                # the flag existed picks up. False would stop every
                # upgraded install ventilating, silently.
                "co2_enabled": True,
                "temp_enabled": False, "temp_on": 26.0,
                "temp_off": 24.5,
                # Off until thresholds are chosen deliberately.
                "humidity_enabled": False, "humidity_on": 70.0,
                "humidity_off": 60.0,
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
    "co2_enabled": ("bool",),
    "temp_enabled": ("bool",),
    "temp_on": ("num", -20.0, 60.0),
    "temp_off": ("num", -20.0, 60.0),
    "humidity_enabled": ("bool",),
    "humidity_on": ("num", 0.0, 100.0),
    "humidity_off": ("num", 0.0, 100.0),
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
    """Write config.json atomically. CALLERS MUST HOLD _cfg_lock.

    The temp path is fixed, so two concurrent writers would truncate the
    same file and one os.replace would then fail on a name the other had
    already moved. The lock is what makes that impossible; this function
    does not defend itself.
    """
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
RESTART_PATH = os.path.join(DIR, "restart.request")
# Captured at import, and again by the process that replaces this one,
# so a trigger written before startup can be told from one written
# after it.
_PROC_START = time.time()
RESTART_POLL_SECONDS = 2


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
    """Write the fault atomically. Raises OSError if it cannot.

    Deliberately does NOT swallow errors. A fault that stays in memory
    but never reaches disk is lost at the next restart, and automatic
    running then resumes on a fan already judged faulty. _store_fault
    catches this, keeps the fault live, says so, and retries.
    """
    if f is None:
        if os.path.exists(FAULT_PATH):
            os.remove(FAULT_PATH)
        return
    tmp = FAULT_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(f, fh)
    os.replace(tmp, FAULT_PATH)


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
    """Persist readings so a restart does not blank the graph.

    Returns whether the write landed. The OSError is still swallowed -
    a full disk must not take the controller down over the graph - but
    swallowing it silently left callers unable to tell, and the restart
    path guarded this with a try/except that could never fire.
    """
    try:
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(tmp, HIST_PATH)
        return True
    except OSError:
        return False



def maybe_save_hist(hist, force=False):
    """Batch writes: one full rewrite per poll was 2,880 a day at 30s."""
    global _last_flush
    now = time.time()
    if not force and now - _last_flush < HIST_FLUSH_SECONDS:
        return False
    ok = save_hist(hist)
    # Advanced either way. A failing disk should not turn every poll
    # into a full rewrite of 20,000 points.
    _last_flush = now
    return ok


def _persist_fault_on_exit():
    """Last chance to write a fault that exists only in memory.

    A signalled shutdown - systemctl restart, a reboot - cannot be
    refused the way a requested restart can. The replacement loads
    fault.json, and if the flag is still set there is nothing there to
    load: Auto would then be free to start a fan that was stopped for
    drawing wrong. The write that failed may have failed minutes ago
    and the disk may since have recovered, so try once more on the way
    down. It costs one write and turns a silent loss into either a save
    or a loud complaint.

    Deliberately does NOT take _fault_lock. This runs in a signal
    handler on the main thread, and blocking on a lock another thread
    holds would hang the shutdown until systemd escalated to SIGKILL -
    which loses the fault anyway, and the history with it. _state_lock
    is an RLock, so re-entering it here is safe. save_fault writes via
    tmp and os.replace, so a racing write cannot tear the file: one
    simply wins, and both are writing the same fault.

    Reports to stderr rather than the activity log for the same reason.
    _log takes _events_lock, which is a plain Lock, and a signal handler
    must not block on one. stderr is the journal, which is where anyone
    looks after a shutdown that went wrong.
    """
    with _state_lock:
        fault = STATE["fault"]
        owed = STATE["fault_unsaved"]
    if not owed or fault is None:
        return False
    try:
        save_fault(fault)
    except OSError as e:
        print("SHUTDOWN WITH AN UNSAVED FAULT - it is being lost (%s). "
              "The fan will NOT be held off on the next start; check the "
              "draw before running it." % e, file=sys.stderr, flush=True)
        return False
    with _state_lock:
        STATE["fault_unsaved"] = False
    print("Unsaved fault written to disk on shutdown; it will survive "
          "the restart.", file=sys.stderr, flush=True)
    return True


def flush_hist(signum=None, frame=None):
    """Persist on shutdown so batching never costs a restart's samples."""
    if signum is not None:
        # Before the history write, which can be 20,000 points against a
        # shutdown deadline. The fault matters more than the graph.
        _persist_fault_on_exit()
    with _state_lock:
        snapshot = list(STATE["history"])
    ok = maybe_save_hist(snapshot, force=True)
    if signum is not None:
        sys.exit(0)
    return ok

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
        # A missing CO2 field is not fatal here. CO2 can be disabled,
        # leaving temperature or humidity driving the fan, so refusing
        # the whole payload would stop those working too. tick() decides
        # whether the rules that ARE enabled have anything to act on.
        co2 = find_metric(src, CO2_HINTS)
        out = {"co2": co2[0] if co2 else None}
        for name, hints in EXTRA_HINTS.items():
            hit = find_metric(src, hints)
            if hit is not None:
                out[name] = hit[0]
        return out, (co2[1] if co2 else None)

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
    "latch": {"co2": False, "temp": False, "humidity": False},
    "manual_off_pending": False,
    "history": [], "events": [], "history_rev": 0, "fault": None,
    # True when the fault in memory could not be written to disk, so
    # a restart would lose it. Retried every draw cycle.
    "fault_unsaved": False,
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
# Serialises the activity log: the poll thread, the draw thread and
# Flask request threads all call _log, and save_events shares one
# events.json.tmp. Held with _state_lock inside it, never the other
# way round, so it composes with _act_lock and _cfg_lock above it.
_events_lock = threading.Lock()
# One settings write at a time, covering snapshot, validation, commit AND
# the disk write. Flask is threaded, so two saves could otherwise each
# deep-copy CFG, and the later commit would write back the earlier
# snapshot of every section it did not touch - silently reverting an
# unrelated change, a UI password among them. They also shared one
# config.json.tmp. Order is _cfg_lock then _act_lock; nothing takes
# _cfg_lock while holding _act_lock.
_cfg_lock = threading.Lock()
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
    """Append an event and persist it. Safe from any thread."""
    with _events_lock:
        with _state_lock:
            STATE["events"].insert(0, {"at": time.time(), "msg": msg})
            del STATE["events"][EVENTS_MAX:]
            snapshot = list(STATE["events"])
        # Still outside _state_lock, so disk I/O never blocks a reader,
        # but inside _events_lock so the write landing last is the
        # newest. Without it two threads shared one events.json.tmp and
        # an older snapshot could overwrite a newer one - or the replace
        # could fail, which save_events swallows.
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


# One entry per trigger. Adding a fourth means adding a row here and a
# schema entry, not another branch in decide() and another special case
# in the dashboard's "why is the fan running" logic.
#
# default is what an absent flag means, and it is load-bearing:
# co2_enabled defaults True so an existing config.json written before
# the flag existed keeps ventilating. A False default there would stop
# every upgraded install silently.
#
# clear_on_missing records existing behaviour rather than choosing it.
# CO2 keeps its latch when the reading drops out, temperature clears it.
# Unifying them would change how the fan behaves during a sensor
# outage, so it is left alone here and noted in the handoff.
RULES = (
    {"key": "co2", "reading": "co2", "flag": "co2_enabled",
     "on": "on_at", "off": "off_at",
     "default": True, "clear_on_missing": False,
     "fmt": lambda v: "%d ppm" % round(v)},
    {"key": "temp", "reading": "temperature", "flag": "temp_enabled",
     "on": "temp_on",
     "off": "temp_off", "default": False, "clear_on_missing": True,
     "fmt": lambda v: "%.1f C" % v},
    {"key": "humidity", "reading": "humidity",
     "flag": "humidity_enabled", "on": "humidity_on",
     "off": "humidity_off", "default": False, "clear_on_missing": True,
     "fmt": lambda v: "%.0f%% RH" % v},
)


def active_rules(c):
    """The rules Auto is currently allowed to act on."""
    return [r for r in RULES if bool(c.get(r["flag"], r["default"]))]


def decide(co2, temp, fan_on, c, ran_for, latch, fault=None,
           humidity=None):
    """Per-rule latching hysteresis. Mutates `latch` in place.

    Each input latches on above its own on-level and releases below its
    own off-level. The fan runs while any rule is latched. This matters:
    previously a rule that had never triggered could still veto the stop,
    so a CO2-driven run was held on by temperature indefinitely.

    With every rule disabled there is nothing to latch, so Auto never
    starts the fan. That is allowed - the UI warns rather than refusing -
    and the stale-reading fail-safe still runs.
    """
    readings = {"co2": co2, "temp": temp, "humidity": humidity}
    if c["mode"] == "on":
        return True, "forced on"
    # Force on is checked first on purpose: it overrides a fault so a
    # repaired fan can be tested. Past here, a fault blocks automatic
    # starts, including the stale-reading failsafe.
    if fault:
        return False, "draw fault"
    if c["mode"] == "manual":
        for r in RULES:
            latch[r["key"]] = False
        return None, None
    for r in RULES:
        key, val = r["key"], readings.get(r["key"])
        if not bool(c.get(r["flag"], r["default"])):
            latch[key] = False
        elif val is None:
            if r["clear_on_missing"]:
                latch[key] = False
        elif val >= c[r["on"]]:
            latch[key] = True
        elif val <= c[r["off"]]:
            latch[key] = False
    want = any(latch.get(r["key"]) for r in RULES)
    if want == bool(fan_on):
        return (True, None) if want else (False, None)
    if want:
        why = [r["fmt"](readings[r["key"]]) for r in RULES
               if latch.get(r["key"]) and readings.get(r["key"]) is not None]
        return True, " and ".join(why)
    if fan_on and ran_for is not None and \
            ran_for < c["min_run_minutes"] * 60:
        return None, None
    parts = [r["fmt"](readings[r["key"]]) for r in active_rules(c)
             if readings.get(r["key"]) is not None]
    return False, ", ".join(parts) if parts else "no reading"


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
    co2 = vals.get('co2')
    # Judged against the rules that are ENABLED, not against CO2. With
    # CO2 disabled and humidity driving the fan, a payload carrying no
    # CO2 field is perfectly usable. Nothing for any live rule means
    # this poll told us nothing we act on, so it is an error and
    # reading_at stays put - which is what arms the fail-safe.
    live = active_rules(CFG["control"])
    if live:
        usable = any(vals.get(r["reading"]) is not None for r in live)
        why = "any enabled trigger (%s)" % ", ".join(r["key"] for r in live)
    else:
        # Nothing enabled, so the fail-safe is the only automatic action
        # left - and it is armed by reading_at going stale. Accepting an
        # empty payload here would keep reading_at fresh forever and
        # quietly disable the last protection there is.
        usable = any(vals.get(k) is not None
                     for k in ("co2", "temperature", "humidity", "pm25"))
        why = "any recognised metric"
    if not usable:
        raise ProtectError("No reading for %s. Open Debug to see what "
                           "Protect is returning." % why)
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
    # Decided under the lock, not merely actuated under it. A settings
    # save replaces CFG["control"] wholesale, and decide() latches in
    # place: deciding on thresholds a save has already replaced sets a
    # latch from numbers no longer in force. That latch is sticky inside
    # the hysteresis band, so gating only the switch would delay the
    # unintended start by one poll rather than prevent it.
    with _act_lock:
        cur = CFG["control"]
        if cur["mode"] != "auto":
            return
        want, why = decide(co2, vals.get('temperature'), fan_on, cur,
                           ran_for, STATE["latch"], STATE["fault"],
                           humidity=vals.get('humidity'))
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
    # Re-checked under _act_lock: the plug read below can take up to its
    # 12s timeout, and a mode change landing in that window could leave
    # this switching the fan on just after Manual ran its one-time stop -
    # which is never repeated, so the fan would stay on in Manual.
    # stale_minutes and the deadline are re-read here too, not just the
    # mode: setting it to 0 disables the fail-safe, and a save doing that
    # while this call sits in the plug read must not still ventilate.
    with _act_lock:
        cur = CFG["control"]
        mins = int(cur.get("stale_minutes") or 0)
        if not mins or cur["mode"] != "auto":
            return
        with _state_lock:
            if STATE["fault"]:
                return
            last = STATE["reading_at"]
        since = last if last is not None else START_TIME
        if time.time() - since < mins * 60:
            return
        try:
            if h.state() is True:
                return
        except PlugError:
            pass                 # unreadable: assert ON anyway
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
    warn = None
    with _fault_lock:
        with _state_lock:
            had = STATE["fault"]
            STATE["fault"] = value
        try:
            save_fault(value)
            with _state_lock:
                STATE["fault_unsaved"] = False
        except OSError as e:
            # Never re-raised. The caller's job after this is to stop
            # the fan, and an unwritable disk must not skip that. The
            # fault stays live in memory; only its survival of a restart
            # is at risk, so flag it and let the draw loop retry.
            with _state_lock:
                STATE["fault_unsaved"] = value is not None
            if value is not None:
                warn = ("WARNING: fault could not be saved (%s). Active "
                        "now but would be lost on restart." % e)
    # Outside _fault_lock, like the stop-confirmed log: an event append
    # must not extend a hold another writer may be waiting on.
    if warn:
        _log(warn)
    return had


def _clear_fault():
    """Acknowledge a fault. Returns (had, ok).

    Deletes from disk BEFORE clearing memory. The other order cleared
    memory, reported success, and left the file behind when the delete
    failed - so the fault came back at the next restart having been
    told it was gone. If the delete fails the fault stays live and the
    caller reports the failure.
    """
    with _fault_lock:
        with _state_lock:
            had = STATE["fault"]
        if had is None:
            return None, True
        try:
            save_fault(None)
        except OSError:
            return had, False
        with _state_lock:
            STATE["fault"] = None
            STATE["fault_unsaved"] = False
    return had, True


def _confirm_fault_stopped():
    """Record that the plug confirmed the stop. True if this call did it.

    Re-reads the fault inside the lock rather than trusting a snapshot
    taken earlier: an acknowledgement may have landed in between, and
    writing the older value back would resurrect a cleared fault.

    The disk write comes BEFORE the memory update. The other order set
    stopped=True in memory, and if the write then failed the next call
    saw stopped already true and returned early - so it was never
    retried and disk and memory disagreed for good.
    """
    with _fault_lock:
        with _state_lock:
            cur = STATE["fault"]
            if not cur or cur.get("stopped"):
                return False
            snap = dict(cur)
            snap["stopped"] = True
        try:
            save_fault(snap)
        except OSError:
            # Not confirmed. Left for the next cycle to try again.
            with _state_lock:
                STATE["fault_unsaved"] = True
            return False
        with _state_lock:
            if STATE["fault"] is not None:
                STATE["fault"]["stopped"] = True
                STATE["fault_unsaved"] = False
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


def _retry_fault_persist():
    """Re-attempt a fault write that failed. True if it saved this time.

    The read happens INSIDE _fault_lock. Taking the snapshot first and
    the lock afterwards let an acknowledgement land in between, so the
    retry wrote the cleared fault back to disk and a restart resurrected
    it: memory None, disk faulted.
    """
    with _fault_lock:
        with _state_lock:
            owed = STATE["fault_unsaved"]
            pending = copy.deepcopy(STATE["fault"])
        if not owed or pending is None:
            return False
        try:
            save_fault(pending)
        except OSError:
            return False
        with _state_lock:
            STATE["fault_unsaved"] = False
    _log("Fault saved on retry; it will survive a restart.")
    return True


def watts_loop():
    """Poll the plug's meter on its own short cadence.

    Draw is read here rather than in tick() because the Protect bootstrap
    is the expensive call: tying watts to poll_seconds left the reading up
    to 30s stale while the UI refreshed every 5s. Errors are swallowed so
    a plug hiccup cannot overwrite the control loop's error.
    """
    while True:
        _retry_fault_persist()
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
    b = _json_body()
    if not isinstance(b, dict):
        # A reporting endpoint should not 500 on a malformed report.
        return jsonify(error="JSON body must be an object."), 400
    msg = str(b.get("msg", ""))[:300]
    where = "%s:%s" % (str(b.get("src", ""))[:120], b.get("line", "?"))
    app.logger.error("UI error: %s @ %s", msg, where)
    _log("UI error: %s" % msg[:120])
    return jsonify(ok=True)


# Distinct from None, which is a valid JSON body ("null"). Without the
# split, a truncated body like "{" parsed to None and was treated as an
# empty request - so a malformed save answered 200 Saved.
_NO_JSON = object()
_BAD_JSON = object()


def _json_body():
    """The parsed body, or a sentinel saying why there isn't one.

    get_json(force=True) parsed any content type, so a cross-origin form
    post - which needs no preflight - could drive these endpoints.
    """
    if not request.is_json:
        return _NO_JSON
    try:
        return request.get_json()
    except Exception:
        return _BAD_JSON


def _body_error(body):
    """Response for a body that is missing, malformed or not an object,
    or None when it is usable."""
    if body is _NO_JSON:
        return jsonify(error="Send JSON with Content-Type "
                             "application/json."), 415
    if body is _BAD_JSON:
        return jsonify(error="Body is not valid JSON."), 400
    if not isinstance(body, dict):
        return jsonify(error="JSON body must be an object."), 400
    return None


@app.route("/api/ui/password", methods=["POST"])
def api_ui_password():
    body = _json_body()
    bad = _body_error(body)
    if bad:
        return bad
    pw = body.get("password")
    if not isinstance(pw, str):
        return jsonify(error="Password must be a string."), 400
    if pw != "" and len(pw) < 8:
        return jsonify(error="Password must be at least 8 characters, or "
                             "empty to disable authentication."), 400
    # Same lock as /api/config: without it a settings save that had
    # already copied CFG would commit its stale "ui" section over this.
    with _cfg_lock:
        cand = copy.deepcopy(CFG)
        cand["ui"]["password_hash"] = set_ui_password(pw) if pw else ""
        # Persisted before it goes live, so a failed write cannot leave
        # authentication changed only in memory.
        try:
            save_cfg(cand)
        except OSError as e:
            return jsonify(error="Could not save the password (%s). "
                                 "Nothing was changed." % e), 500
        CFG["ui"] = cand["ui"]
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
    had, ok = _clear_fault()
    if not ok:
        # The fault is still live and still on disk. Saying "cleared"
        # here would leave the operator believing the fan is available
        # when the next restart brings the fault straight back.
        return jsonify(error="Could not delete the stored fault, so it "
                             "has been left in place. Check the disk and "
                             "try again."), 503
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
    """Serialise the whole save: snapshot, validate, commit, persist."""
    with _cfg_lock:
        return _api_config_locked()


def _api_config_locked():
    """The body of api_config, with _cfg_lock already held.

    Validates against a copy so a rejected save cannot alter live
    settings. That copy is also why the lock is needed: it spans every
    section, so committing it writes back whatever the sections this
    request did not touch looked like when the copy was taken.
    """
    body = _json_body()
    bad = _body_error(body)
    if bad:
        return bad
    cand = copy.deepcopy(CFG)
    for section in ("protect", "plug", "control"):
        raw_section = body.get(section)
        if raw_section is None:
            raw_section = {}
        if not isinstance(raw_section, dict):
            return jsonify(
                error="%s must be an object." % section), 400
        raw = {k: v for k, v in raw_section.items() if v != "SET"}
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
    if c.get("humidity_enabled"):
        try:
            h_on, h_off = float(c["humidity_on"]), float(c["humidity_off"])
        except (TypeError, ValueError):
            return jsonify(error="Humidity levels must be numbers."), 400
        if h_on <= h_off:
            return jsonify(error="The humidity turn-off level must be "
                                 "below the turn-on level."), 400
    pg = cand["plug"]
    try:
        w_lo, w_hi = float(pg["watts_min"]), float(pg["watts_max"])
    except (TypeError, ValueError):
        return jsonify(error="Draw limits must be numbers."), 400
    if w_lo >= w_hi:
        return jsonify(error="Expected draw low must be below high."), 400
    summary = _describe_changes(cand)
    # Persist BEFORE committing. The reverse order left a failed write
    # returning 500 while the controller ran on settings that never
    # reached disk - live behaviour and stored behaviour disagreeing
    # until the next restart silently reverted it. cand is a complete
    # config, so writing it is equivalent to writing the merged CFG.
    try:
        save_cfg(cand)
    except OSError as e:
        return jsonify(error="Could not save settings (%s). Nothing "
                             "was changed." % e), 500
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
    if summary:
        _log(summary)
    _wake.set()
    # Allowed, but never silently: with no rule enabled Auto has
    # nothing to latch on and the fan will not start by itself.
    if not active_rules(CFG["control"]) and \
            CFG["control"].get("mode") == "auto":
        warn = ("No triggers are enabled, so Auto will not start the fan. "
                "Only the stale-reading fail-safe can still run it.")
        notice = "%s %s" % (notice, warn) if notice else warn
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


RESUME_ENV = "FAN_CONTROLLER_RESUME"


def _n(v):
    """A finite number or None. Anything else in a handover is not trusted.

    json.loads accepts bare NaN and Infinity, and both pass an isinstance
    check against float. A NaN lease is the worst of the three: every
    comparison against it is False, so a minimum-run lease would neither
    hold nor expire, and it would never look wrong in the state dump.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _resume_payload():
    """Snapshot the timing state that exists only in memory.

    A restart is a clean slate everywhere else by design, but these are
    not values the controller can re-derive, and dropping them silently
    changes what the fan does. A latched rule releases, so a fan running
    on demand stops. changed_at carries the minimum-run lease, without
    which a restart short-cycles the motor that lease exists to protect.
    _oob_since carries how long the draw has been out of band, so a
    developing fault does not restart its clock. reading_at carries the
    stale-reading fail-safe, and START_TIME the grace periods that fall
    back to it when nothing has been read yet.
    """
    with _state_lock:
        return {
            "v": 1,
            "latch": dict(STATE["latch"]),
            "changed_at": STATE["changed_at"],
            "reading_at": STATE["reading_at"],
            "oob_since": _oob_since,
            "start_time": START_TIME,
        }


def _apply_resume():
    """Take over the timing state of the process we replaced.

    Carried in the environment rather than a file, deliberately. The
    environment survives execv and nothing else: a crash, a systemd
    restart or a hand start all begin clean, which is exactly right
    when the previous process did not hand over in an orderly way. A
    file would be found by all of them, and a stale one would resume
    a lease that expired hours ago.

    Anything malformed is ignored rather than raised. Starting from a
    clean slate is the behaviour we had before this existed; not
    starting at all is worse.
    """
    global _oob_since, START_TIME
    raw = os.environ.pop(RESUME_ENV, None)
    if not raw:
        return False
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        _log("Restart - handover data unreadable, starting clean")
        return False
    if not isinstance(d, dict) or d.get("v") != 1:
        _log("Restart - handover data not a version this build reads, "
             "starting clean")
        return False
    latch = d.get("latch")
    with _state_lock:
        if isinstance(latch, dict):
            for k in STATE["latch"]:
                # A real bool, not anything truthy. bool("false") is
                # True, and a latch is what keeps the fan running.
                raw = latch.get(k)
                STATE["latch"][k] = raw if isinstance(raw, bool) else False
        STATE["changed_at"] = _n(d.get("changed_at"))
        STATE["reading_at"] = _n(d.get("reading_at"))
        held = [k for k, v in sorted(STATE["latch"].items()) if v]
        lease = STATE["changed_at"] is not None
    _oob_since = _n(d.get("oob_since"))
    st = _n(d.get("start_time"))
    if st is not None:
        START_TIME = st
    # Logged because the handover is otherwise invisible: the values it
    # carries are the ones that decide whether the fan keeps running,
    # and without a line here the only way to tell a handover from a
    # clean start is that the fan did not behave oddly.
    _log("Resumed after restart - %s, %s"
         % ("latched: " + ", ".join(held) if held else "no latch held",
            "minimum-run lease carried" if lease else "no lease"))
    return True


def _consume_restart_request():
    """Take the restart trigger if there is a fresh one.

    Deleting BEFORE the exec, and only going ahead when the delete
    succeeded, is what stops a boot loop. A trigger that cannot be
    removed would otherwise be seen again by the process that replaces
    this one, and again by the next, forever. A failed unlink refuses
    the request and says so rather than retrying.

    A file older than this process has already been acted on: exec
    keeps the working directory, so a trigger written just before
    startup would otherwise fire a second, pointless restart. It is
    still removed, so it cannot accumulate.
    """
    try:
        st = os.stat(RESTART_PATH)
    except OSError:
        return False
    fresh = st.st_mtime >= _PROC_START
    try:
        os.unlink(RESTART_PATH)
    except OSError as e:
        _log("Restart request ignored - could not remove restart.request "
             "(%s)" % e)
        return False
    return fresh


def _mark_fds_close_on_exec():
    """Stop open descriptors surviving into the replacement process.

    Werkzeug marks its listening socket inheritable so the reloader can
    hand the fd to a child, and nothing clears that on the way into
    execv. The replacement therefore inherited a socket already bound
    to 8096, died with "Address already in use", and the service only
    came back because Restart=on-failure caught the non-zero exit ten
    seconds later. Observed in the journal on 08-10, not theorised:
    PEP 446 makes Python's own descriptors non-inheritable, but
    werkzeug opts this one back in.

    0, 1 and 2 are deliberately left alone. stdout and stderr are the
    journal, and closing them would cost the replacement its own
    startup logging - the first thing anyone reads when a restart goes
    wrong.

    Setting the flag is not destructive. If the exec then fails, this
    process carries on with every descriptor still open and usable.
    """
    try:
        fds = sorted(int(n) for n in os.listdir("/proc/self/fd"))
    except (OSError, ValueError) as e:
        _log("Restart - could not enumerate descriptors (%s)" % e)
        return
    for fd in fds:
        if fd <= 2:
            continue
        try:
            os.set_inheritable(fd, False)
        except OSError:
            # The listing is a snapshot taken a moment ago: a descriptor
            # closed since is not a problem, and one that refuses the
            # flag is better skipped than made fatal.
            pass


def _restart_now():
    """Replace this process with a fresh one. Same PID, same unit.

    Returns False if the restart was refused. On success it does not
    return at all - the process image is gone.

    execv rather than exiting: the unit is Restart=on-failure and
    flush_hist exits zero, so anything that merely stops would stay
    stopped. Replacing the image keeps systemd's main PID valid and
    needs no privilege, which is the point - a restart should not
    depend on sudo being reachable.

    History is flushed first because the batch writer holds up to
    HIST_FLUSH_SECONDS of samples in memory and exec discards them.
    config, fault and events are each written atomically as they
    change, so they need nothing here.

    The relay is not touched. The fan keeps doing whatever it was
    doing, and the fault survives in fault.json; the in-memory latches
    and the minimum-run lease do not, so a restart re-decides from the
    current readings.
    """
    # A fault that never reached disk exists only in this process.
    # The replacement loads fault.json, finds nothing, and Auto is then
    # free to start a fan that was stopped for drawing wrong - the
    # exact failure fault.json exists to prevent. Refuse, and let the
    # draw loop's retry clear the flag before a later request tries
    # again. Returns False rather than raising: the caller is a watch
    # loop, and a refused restart is a decision, not an error.
    with _state_lock:
        unsaved = STATE["fault_unsaved"]
    if unsaved:
        _log("Restart refused - the active fault is not saved to disk "
             "and would be lost")
        return False

    _log("Restarting on request")
    if not flush_hist():
        # Worth saying, not worth refusing over: the graph loses at
        # most HIST_FLUSH_SECONDS of points and nothing about the
        # fan's safety rests on them.
        _log("Restart - history could not be written, up to %ds of "
             "readings will be lost" % HIST_FLUSH_SECONDS)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # Handed over through the environment, which execv preserves and
    # nothing else does. Set last, so a refusal above never leaves it
    # behind for a later exec to pick up.
    os.environ[RESUME_ENV] = json.dumps(_resume_payload())
    # Must come immediately before the exec: it is what stops the
    # replacement inheriting the bound listening socket.
    _mark_fds_close_on_exec()
    os.chdir(DIR)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def restart_loop():
    """Watch for the restart trigger.

    A file rather than an endpoint on purpose. Basic auth is inactive
    until a UI password is set, so an HTTP route would let anything on
    the VLAN bounce the controller - and since a restart clears the
    latches and the lease, repeatedly is a way to stop the room being
    ventilated. Writing this file needs shell access as the service
    user, which is already total control.
    """
    while True:
        try:
            if _consume_restart_request():
                _restart_now()
        except Exception as e:
            _log("Restart watch error - %s" % e)
        time.sleep(RESTART_POLL_SECONDS)


if __name__ == "__main__":
    # Before any thread starts, so the first decide() sees the lease and
    # the latches the previous process was holding.
    _apply_resume()
    atexit.register(flush_hist)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, flush_hist)
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=watts_loop, daemon=True).start()
    threading.Thread(target=restart_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=8096, threaded=True)
