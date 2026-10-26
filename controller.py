"""Air-quality-driven fan control via a Tasmota smart plug.

Reads CO2, temperature and humidity from a UniFi Protect UP-AirQuality
sensor and switches the plug with hysteresis. Settings are editable in
the web UI.

Protect 7.1's documented API omits air-quality metrics, so this reads the
private /proxy/protect/api/bootstrap endpoint. That is undocumented and may
break on firmware updates; use the Debug view to inspect the live payload.
"""
import atexit, copy, hashlib, hmac, json, math, os, re, signal, smtplib
import socket, ssl, sys, tempfile, ipaddress
import threading, time
import logging
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from logging.handlers import RotatingFileHandler
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util.connection import allowed_gai_family
from datetime import date
from contextlib import nullcontext, contextmanager
from urllib.parse import urlsplit
import requests, urllib3
from flask import Flask, Response, jsonify, request, render_template
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from waitress.wasyncore import ExitNow

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("FAN_CONTROLLER_DATA_DIR", DIR))
PERSIST_STOP_STATE = os.environ.get("FAN_CONTROLLER_PERSIST_STATE") == "1"
STOP_STATE_PATH = os.path.join(DATA_DIR, "resume.json")
_stopping = threading.Event()
_restart_dispatch = None  # Installed by the main-thread web loop before workers start.
CFG_PATH = os.path.join(DATA_DIR, "config.json")
# Local testing interlock, sampled at startup. No request (including a
# dashboard plug test or actuation) may reach hardware while this is set.
PLUG_ACCESS_DISABLED = os.path.lexists(os.path.join(DATA_DIR, "plug-access.disabled"))

DEFAULTS = {
    "protect": {"host": "192.0.2.1", "username": "", "password": "",
                "verify_tls": False,
                "sensor_id": ""},
    "plug": {"host": "192.0.2.10", "user": "admin", "password": "",
             "watts_enabled": False, "watts_min": 15.0,
             "watts_max": 25.0, "watts_fault_seconds": 60,
             "watts_off_max": 1.0},
    "ui": {"password_hash": ""},
    # Off until it is configured deliberately: an install that upgrades
    # into this must not start trying to send. Every other key can be
    # blank and enabled stays False, so nothing is sent by accident.
    "email": {"enabled": False, "host": "", "port": 25,
              "username": "", "password": "", "sender": "",
              "recipients": ""},
    "control": {"mode": "auto", "on_at": 1000, "off_at": 800,
                "poll_seconds": 30, "min_run_minutes": 5,
                "cold_start_minutes": 15,
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
CO2_HINTS = ("co2", "carbondioxide", "co2ppm", "carbondioxideppm")
EXTRA_HINTS = {"temperature": ("temperature", "temperaturec"),
               "pm25": ("pm2p5", "pm25", "pm25ugm3"),
               "humidity": ("humidity", "relativehumidity")}
VALUE_KEYS = ("value", "val", "current", "ppm", "reading")


CONTROL_SCHEMA = {
    "mode": ("enum", ("auto", "manual", "on")),
    "on_at": ("num", 300.0, 10000.0),
    "off_at": ("num", 300.0, 10000.0),
    "poll_seconds": ("int", 5, 3600),
    "min_run_minutes": ("int", 0, 240),
    "cold_start_minutes": ("int", 0, 240),
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
EMAIL_SCHEMA = {
    "enabled": ("bool",),
    "host": ("str", 0, 253),
    "port": ("int", 1, 65535),
    "username": ("str", 0, 128),
    "password": ("str", 0, 256),
    "sender": ("str", 0, 320),
    # Comma or space separated. Kept as one string rather than a list so
    # it round-trips through a single text input, and is split at send
    # time - the split is not policy, just formatting.
    "recipients": ("str", 0, 1024),
}
PLUG_SCHEMA = {
    "host": ("str", 1, 253), "user": ("str", 0, 64),
    "password": ("str", 0, 128),
    "watts_enabled": ("bool",),
    "watts_min": ("num", 1.0, 5000.0),
    "watts_max": ("num", 1.0, 5000.0),
    "watts_fault_seconds": ("int", 5, 3600),
    "watts_off_max": ("num", 0.0, 5000.0),
}
SCHEMAS = {"protect": PROTECT_SCHEMA, "plug": PLUG_SCHEMA, "ui": UI_SCHEMA,
           "control": CONTROL_SCHEMA, "email": EMAIL_SCHEMA}


class ConfigurationError(RuntimeError):
    """The stored configuration cannot be trusted for safe operation."""


def validate_config_relationships(cfg):
    """Validate relationships that per-field schemas cannot express.

    Used for both startup and dashboard writes so a hand-edited file cannot
    bypass the safety checks applied to the same values in the UI.
    """
    c = cfg["control"]
    if c["on_at"] <= c["off_at"]:
        raise ValueError("Turn-on level must be higher than turn-off level, "
                         "or the fan will never stop.")
    if c.get("temp_enabled") and c["temp_on"] <= c["temp_off"]:
        raise ValueError("Cool-to temperature must be below the turn-on "
                         "temperature.")
    if c.get("humidity_enabled") \
            and c["humidity_on"] <= c["humidity_off"]:
        raise ValueError("The humidity turn-off level must be below the "
                         "turn-on level.")
    pg = cfg["plug"]
    if pg["watts_min"] >= pg["watts_max"]:
        raise ValueError("Expected draw low must be below high.")


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
        except (TypeError, ValueError, OverflowError):
            raise ValueError("%s must be a finite number" % name)
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
        if name in ("email.sender", "email.recipients") and any(ch in v for ch in "\r\n"):
            raise ValueError("%s must not contain line breaks" % name)
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
    if not os.path.exists(CFG_PATH):
        return cfg
    try:
        with open(CFG_PATH) as f:
            disk = json.load(f)
    except (ValueError, OSError) as e:
        raise ConfigurationError(
            "config.json cannot be read safely (%s); refusing to start"
            % type(e).__name__)
    if not isinstance(disk, dict):
        raise ConfigurationError(
            "config.json must contain a JSON object; refusing to start")
    for section, values in disk.items():
        if section not in cfg:
            raise ConfigurationError(
                "unknown configuration section %s; refusing to start"
                % section)
        if not isinstance(values, dict):
            raise ConfigurationError(
                "%s must contain a JSON object; refusing to start"
                % section)
        cfg[section].update(values)
    ctl = cfg.get("control")
    ctl = ctl if isinstance(ctl, dict) else {}
    if ctl.get("mode") == "off":
        # Force off was removed; manual now switches off once,
        # then leaves the fan alone.
        ctl["mode"] = "manual"
    if "enabled" in ctl:
        # Older configs used a separate enable flag; "not enabled" is now
        # simply manual mode. Fold it in and drop the duplicate control.
        if not isinstance(ctl["enabled"], bool):
            raise ConfigurationError("control.enabled must be a Boolean; refusing to start")
        if not ctl.pop("enabled") and ctl.get("mode") == "auto":
            ctl["mode"] = "manual"
    # Missing fields receive upgrade defaults. Present but invalid fields do
    # not: silently replacing a damaged control.mode with the Auto default can
    # start a controller the operator had deliberately left in Manual.
    for section, schema in SCHEMAS.items():
        clean = dict(DEFAULTS[section])
        got = cfg.get(section)
        for k, v in (got if isinstance(got, dict) else {}).items():
            if k not in schema:
                raise ConfigurationError(
                    "unknown setting %s.%s; refusing to start" % (section, k))
            try:
                clean[k] = _coerce("%s.%s" % (section, k), schema[k], v)
            except ValueError as e:
                raise ConfigurationError("%s; refusing to start" % e)
        cfg[section] = clean
    try:
        validate_config_relationships(cfg)
    except ValueError as e:
        raise ConfigurationError("%s refusing to start" % e)
    return cfg



def _fsync_dir(path):
    """Make a rename in this directory survive power loss.

    os.replace is atomic - no reader ever sees half a file - but atomic
    is not durable. The rename can sit in the page cache for the
    filesystem's commit interval, so a fault written a second before the
    power goes can simply not be there afterwards. Syncing the directory
    is what commits the name.

    Raises OSError if it cannot. An earlier version swallowed it, on the
    theory that some filesystem might refuse to open a directory for
    reading - but the supported targets are Debian and Ubuntu on x86-64
    and ARM64, where every filesystem in use allows it. So the only
    thing the swallow actually caught was a real durability failure, and
    it caught it by reporting success: write_json_durable returned
    normally, _store_fault cleared fault_unsaved, and a restart went
    ahead on a fault that might not be on disk. An EIO here is precisely
    when the fault should stay flagged.

    O_DIRECTORY so a path that is not a directory fails loudly instead
    of syncing whatever else the descriptor turned out to be.
    """
    fd = os.open(path or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class InstalledNotDurable(OSError):
    """The new file is in place; the rename may not survive a power cut.

    A subclass of OSError so existing handlers still catch it, but a
    distinct type because the two failures need OPPOSITE handling. A
    write that never landed means nothing changed. A write that landed
    without a durable rename means everything changed - the new file is
    what a restart will load - so a caller that reports "nothing was
    changed" would be lying, and the setting would appear later anyway.
    """


def write_json_durable(path, value, mode=0o600, **dump_args):
    """Write JSON so it survives a power cut, not merely a crash.

    Used for config.json, fault.json, manual-off.pending and daily.json. These
    are small, written relatively rarely. Losing the first three has
    consequences a user cannot see: a fault that vanishes lets Auto start a fan already
    judged faulty, a lost config silently reverts thresholds, and a lost
    promise leaves a fan running in Manual.

    NOT used for history.json or events.json. They are written far more
    often - history every HIST_FLUSH_SECONDS - and an fsync of 190KB on
    a Pi's SD card every five minutes buys nothing worth having: the
    cost of losing the last few minutes of a graph is a gap in a graph.

    Raises OSError if it cannot write. Callers decide what that means.
    """
    # Create beside the destination so os.replace remains atomic. mkstemp
    # creates a new 0600 inode, unaffected by a stale temp file's mode.
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.",
                               dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fd = None
            json.dump(value, fh, **dump_args)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        _fsync_dir(os.path.dirname(path))
    except OSError as e:
        # Past this point the new file is already visible under its real
        # name. Only its durability is in doubt, and saying so is not the
        # same as saying the write failed.
        raise InstalledNotDurable("%s (the new file is in place)" % e)


def save_cfg(cfg):
    """Write config.json atomically. CALLERS MUST HOLD _cfg_lock.

    The unique temp file prevents an earlier file's permissions from
    becoming config.json's permissions. The lock still serialises the
    configuration transaction: which snapshot reaches disk, becomes active
    in memory and supplies the clients must remain one coherent order.
    """
    # The temp file is created 0600 and set to the requested mode before any
    # credential is written. os.replace keeps that mode, so there is no
    # post-rename chmod that can fail after the new configuration is live.
    write_json_durable(CFG_PATH, cfg, mode=0o600, indent=2)


HIST_PATH = os.path.join(DATA_DIR, "history.json")
HIST_HOURS = 24           # retention window, independent of poll interval
HIST_CAP = 20000          # memory bound if poll_seconds is very small
HIST_FLUSH_SECONDS = 300  # batch disk writes rather than one per poll
_last_flush = None
# When the draw first left its band. None while healthy, so a single good
# reading resets the clock rather than accumulating toward a fault.

FAULT_PATH = os.path.join(DATA_DIR, "fault.json")
RESTART_PATH = os.path.join(DATA_DIR, "restart.request")
def _restart_identity(st):
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)

try:
    _startup_restart_identity = _restart_identity(os.stat(RESTART_PATH))
except OSError:
    _startup_restart_identity = None
# A one-time stop that has been promised and not yet made. On disk
# because a crash or a power cut between committing Manual and making
# the stop would otherwise drop the promise, leaving the fan running in
# the one mode whose whole purpose is to stop touching it.
MANUAL_PENDING_PATH = os.path.join(DATA_DIR, "manual-off.pending")
# Captured at import, and again by the process that replaces this one,
# so a trigger written before startup can be told from one written
# after it.
_PROC_START = time.time()
# A token for "which process is answering", carried as a STRING so it
# survives a JSON round trip through the browser and back as a query
# parameter without float formatting getting a vote.
PROC_GENERATION = "%.6f" % _PROC_START
_PROC_MONO = time.monotonic()
RESTART_POLL_SECONDS = 2


def load_fault():
    """A fault outlives a restart: only acknowledgement clears it.

    Held in memory alone, restarting the service - or the host losing
    power - would silently re-arm a fan already judged faulty.
    """
    if not os.path.lexists(FAULT_PATH):
        return None
    try:
        with open(FAULT_PATH) as f:
            d = json.load(f)
    except (ValueError, OSError) as e:
        why = type(e).__name__
    else:
        if _valid_fault_record(d):
            return d
        why = "invalid structure"
    # Never interpret an unreadable latch as an acknowledgement. This fault
    # is deliberately clearable through the ordinary acknowledgement path;
    # that removes the damaged file only after the operator has seen it.
    return {"at": time.time(),
            "msg": ("FC-206 Startup safety fault - persisted fault state "
                    "could not be read (%s)" % why),
            "code": "FC-206", "stopped": False,
            "attempted_at": None, "delivered_at": None}


def _valid_fault_timestamp(value):
    """Accept only timestamps our local date and alert formatters can use."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        stamp = float(value)
        if not math.isfinite(stamp):
            return False
        # date enforces a portable year range (1..9999), and strftime is
        # the same platform formatting operation used by alert messages.
        date.fromtimestamp(stamp)
        time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(stamp))
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _valid_fault_record(value):
    """Whether a persisted fault is safe for every downstream operation."""
    if not isinstance(value, dict):
        return False
    if set(value) - {"at", "msg", "code", "stopped",
                     "attempted_at", "delivered_at", "relay_retry", "relay_notice",
                     "manual_stop_id"}:
        return False
    if "manual_stop_id" in value and (
            not isinstance(value["manual_stop_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["manual_stop_id"])):
        return False
    if not isinstance(value.get("msg"), str) or not value["msg"].strip():
        return False
    if not _valid_fault_timestamp(value.get("at")):
        return False
    if "stopped" in value and not isinstance(value["stopped"], bool):
        return False
    if "code" in value and (not isinstance(value["code"], str)
                            or not value["code"].strip()):
        return False
    for key in ("attempted_at", "delivered_at"):
        stamp = value.get(key)
        if stamp is not None and not _valid_fault_timestamp(stamp):
            return False
    retry = value.get("relay_retry")
    if "relay_retry" in value:
        if (value.get("code") not in ("FC-007", "FC-008", "FC-009")
                or not isinstance(retry, dict)
                or set(retry) != {"attempts", "phase"}
                or type(retry["attempts"]) is not int):
            return False
        n, phase = retry["attempts"], retry["phase"]
        if not (0 <= n <= 6 and (
                (phase == "first" and n < 3)
                or (phase == "wait" and n == 3)
                or (phase == "final" and 3 < n < 6)
                or (phase == "exhausted" and n == 6)
                or (phase == "recovered" and n >= 0))):
            return False
        if phase in ("wait", "final", "exhausted") and value["code"] != "FC-008":
            return False
    if "relay_notice" in value:
        notice = value["relay_notice"]
        if (retry is None or not isinstance(notice, dict)
                or set(notice) != {"stage", "delivered"}
                or notice["stage"] not in ("warning", "hard", "exhausted", "recovered")
                or type(notice["delivered"]) is not bool):
            return False
    return True


def promise_manual_off():
    """Record that a one-time stop is owed, durably, BEFORE Manual is
    committed.

    Written first on purpose. If this fails the mode change is refused,
    which is the safe direction: no promise recorded and no Manual
    stored beats Manual stored with the promise lost. If it succeeds and
    the process then dies before the stop, the marker is still there and
    startup restores it.

    A marker left behind by a mode that moved on is harmless: startup
    only honours it while the stored mode is still Manual, and selecting
    Manual again writes it afresh at a point where the stop is genuinely
    owed.
    """
    write_json_durable(MANUAL_PENDING_PATH,
                       {"at": time.time(), "id": os.urandom(16).hex()}, mode=0o600)


def _manual_stop_id():
    """Identify the exact durable promise, including legacy marker files."""
    try:
        with open(MANUAL_PENDING_PATH, "rb") as marker:
            return hashlib.sha256(marker.read()).hexdigest()
    except OSError:
        return None


def clear_manual_off_promise():
    """Forget the owed stop, once the plug has confirmed it.

    Failure is swallowed here, unlike the write. A marker that will not
    delete costs one extra stop after a restart in Manual, which is the
    action the user asked for in the first place. Refusing to record a
    completed stop would be worse.
    """
    try:
        if os.path.exists(MANUAL_PENDING_PATH):
            os.remove(MANUAL_PENDING_PATH)
            _fsync_dir(os.path.dirname(MANUAL_PENDING_PATH))
    except OSError:
        pass


def manual_off_promised():
    """Whether a stop was owed when we last stopped running."""
    return os.path.exists(MANUAL_PENDING_PATH)


def save_fault(f):
    """Write the fault atomically. Raises OSError if it cannot.

    Deliberately does NOT swallow errors. A fault that stays in memory
    but never reaches disk is lost at the next restart, and automatic
    running then resumes on a fan already judged faulty. _store_fault
    catches this, keeps the fault live, says so, and retries.
    """
    if f is None:
        try:
            os.remove(FAULT_PATH)
        except FileNotFoundError:
            # Already gone. Fall through to the sync anyway - see below.
            pass
        # ALWAYS, even when there was nothing to remove. The unlink needs
        # committing or a fault acknowledged just before a power cut comes
        # back on the next start with nothing to explain why. Guarding
        # this on the file existing meant a second acknowledgement, after
        # a first that unlinked and then failed to sync, found no file,
        # skipped the sync, and reported a durable delete that had never
        # been committed. Unconditional makes the retry free.
        try:
            _fsync_dir(os.path.dirname(FAULT_PATH))
        except OSError as e:
            # The file is gone but the removal may not survive. Same
            # shape as a write that landed without a durable rename,
            # and it gets the same distinct type so the caller can
            # tell it apart from "the delete failed".
            raise InstalledNotDurable(
                "%s (the fault file is already removed)" % e)
        return
    write_json_durable(FAULT_PATH, f, mode=0o600)


_oob_since = None
_off_draw_since = None  # monotonic: sustained draw while relay reports OFF
_relay_retry_clock = None  # (fault identity, consumed attempts, monotonic deadline)
_oob_monotonic = None  # (wall start, monotonic start); wall start is for diagnostics
_draw_grace = None  # (physical wall timestamp, monotonic grace deadline)
# Which config the out-of-band timer belongs to. A timer accumulated
# against one plug and one draw band must not carry into another: the
# elapsed seconds would be counted against limits they were never
# measured under.
_oob_generation = None


_CODE_SERIES = re.compile(r"^FC-(\d)\d\d\b")
_CODE_PREFIX = re.compile(r"^FC-\d+\s+")


# The keys a history point can carry, and what the charts call them.
HIST_METRICS = ("v", "tc", "rh")


def history_ranges(hist):
    """Observed low and high per metric, over the trace as retained.

    THE OBSERVED VALUES, NOT THE DRAWN SCALE, and that is the whole
    reason this is worth computing rather than reading off the chart.
    The plot stretches its scale to keep the fan-on and fan-off guides
    on screen, so a humidity row whose guide sits at 70 would report a
    70% high on a day the room never passed 55 - which is what the page
    did before the two were separated.

    Moved out of the browser on 2026-08-17. The distinction above was
    guarded by a comment and by a test that read the page source for a
    variable name; here it is a function that a mutation can break.

    Missing and non-finite values are skipped rather than defaulting to
    zero: a sensor that reports no humidity has no humidity range, and
    a NaN low would be both unserialisable and permanently unbeatable
    by any later comparison. A metric with no readings is ABSENT from
    the result, so the page has nothing to draw rather than a zero to
    draw wrongly.
    """
    out = {}
    for key in HIST_METRICS:
        vals = [p[key] for p in (hist or [])
                if isinstance(p, dict) and _finite(p.get(key))]
        if vals:
            out[key] = {"lo": min(vals), "hi": max(vals)}
    return out


def event_class(msg):
    """Which colour an activity line takes, decided HERE not in the page.

    This lived in the dashboard's JavaScript, where it could only be
    asserted by searching the page source for regexes - and on
    2026-08-16 it broke silently: the rules matched "^Draw fault" and
    "^Fan ", both of which stopped working the moment log lines gained
    an FC- prefix. Faults rendered as ordinary text, every test stayed
    green, and it surfaced only because someone looked at a screen.

    Moved because THIS IS A DECISION, and decisions belong where they
    can be tested. What is left in the page is one assignment from a
    field, which cannot silently mean something else. The classes are
    still CSS names - the page owns what a colour looks like, this owns
    what a line means.

    Returns "" for lines that take the default colour.
    """
    raw = msg if isinstance(msg, str) else ""
    series = _CODE_SERIES.match(raw)
    # The prefix comes off before the text rules run, so a coded line
    # and an uncoded one take the same branch.
    m = _CODE_PREFIX.sub("", raw)
    if raw.startswith((FC_FAULT_PERSISTED + " ", FC_HARD_CLEARED + " ",
                       FC_RELAY_RECOVERED + " ")):
        return "notice"  # Persistence, acknowledgement or a still-latched fault.
    if (raw == FC_POLL_ERROR + " Draw loop recovered"
            or (raw in (code + " Poll recovered" for code in
                        (FC_POLL_ERROR, FC_SENSOR_DOWN, FC_PLUG_DOWN)))):
        return "ok"
    # Startup DNS uses the same code for waiting, failure and recovery.
    if (raw.startswith(FC_STARTUP + " Resolved ")
            and m.endswith(", starting normally")):
        return "ok"
    if series:
        ser = series.group(1)
        # 0xx alarm and 2xx failure are both things going wrong.
        if ser in ("0", "2"):
            return "fault"
        # 1xx is an all-clear: a return to normal.
        if ser == "1":
            return "ok"
        if ser == "3":
            # A cold start is an observation about THIS process, not
            # about the plug, and on a PoE board it is routine. Neutral
            # deliberately: the colours are how the log says which lines
            # want attention, and a line that appears every time the
            # network switch reboots must not join that column. The
            # information is in the text for whoever is already reading.
            if raw.startswith((FC_COLD_START, FC_COLD_HOLD)):
                return ""
            # The rest of 3xx is the plug moving without this service
            # commanding it. In Manual that is the mode doing its job;
            # in any other mode the controller has been contradicted and
            # it should stand out.
            return "ok" if m.endswith("(Manual)") else "m-manual"
    # Anchored, not a bare substring: "Changed: watts_fault_seconds"
    # contains the word but is a settings edit, not a fault.
    if re.match(r"Draw fault", m, re.I) or re.search(r"draw fault$", m, re.I):
        return "fault"
    # A forced switch-on is Force on demanding the fan rather than the
    # rules acting, so it takes the mode colour.
    if re.search(r"forced on$", m) or m.startswith("Mode: Force on"):
        return "m-on"
    if m.startswith("Fan "):
        return "ok"
    # Mode entries take the same colour as the mode button, so the log
    # and the selector agree. startsWith, not equality: a folded line
    # reads "Mode: Manual - fan off".
    if m.startswith("Mode: "):
        if m.startswith("Mode: Manual"):
            return "m-manual"
        if m.startswith("Mode: Auto"):
            return "m-auto"
        return "m-on"
    return ""


EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
# 1000 rather than 300 since 2026-08-16, when the alert transitions
# started logging with their codes. An episode now costs several lines
# instead of one, and the point of the codes is that a search finds a
# whole episode months later - which it cannot if the depth has already
# evicted half of it. About 21KB on disk at this size; the file is
# rewritten whole on every append, and events are rare enough that this
# is not a concern on an SD card.
EVENTS_MAX = 1000     # kept on disk
EVENTS_SHOWN = 30     # returned to the UI each poll


def load_events():
    try:
        e = json.load(open(EVENTS_PATH))
    except (ValueError, OSError):
        return []
    if not isinstance(e, list):
        return []
    # Disk/restored data is not trusted: one Infinity anywhere in an event
    # would make /api/state invalid JSON in browsers. Message types must also
    # be safe for classification, and only the supported fields are retained.
    return [{"at": x["at"], "msg": x["msg"]} for x in e
            if isinstance(x, dict) and _valid_fault_timestamp(x.get("at"))
            and isinstance(x.get("msg"), str)][:EVENTS_MAX]


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
    # Restored records may contain arbitrary extra fields, including values
    # that cannot be represented in browser JSON. Keep only our schema.
    fields = ("t", "v", "tc", "rh", "pm")
    kept = [{k: p[k] for k in fields if k in p}
            for p in hist if _usable_point(p, cutoff)]
    kept.sort(key=lambda p: p["t"])
    return kept[-HIST_CAP:]


def _usable_point(point, cutoff):
    """True for a history sample that can be compared and plotted.

    A string, list or None where a timestamp belongs used to raise
    TypeError from the comparison. This runs during module import, so
    one malformed entry in history.json took the whole service down -
    and with Restart=on-failure, took it down repeatedly. A graph point
    is not worth a crash loop: drop it and keep serving.

    bool is excluded explicitly because it is a subclass of int, and
    True would otherwise pass as the timestamp 1. NaN is excluded
    because every comparison against it is False, which would silently
    drop the point anyway, and infinity because it would never expire.
    """
    if not isinstance(point, dict):
        return False
    ts = point.get("t")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return (_finite(ts) and ts >= cutoff
            and all(point.get(key) is None or _finite(point[key])
                    for key in ("v", "tc", "rh", "pm")))


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



_hist_write_lock = threading.Lock()

DAILY_PATH = os.path.join(DATA_DIR, "daily.json")
# Two years. One line per day, three metrics - kilobytes, against the
# 188KB the 24h trace already costs. The point is to answer "was it
# this humid last week" without extending the trace, which is the
# expensive way to buy the same answer.
DAILY_KEEP_DAYS = 730
_daily_lock = threading.Lock()
_daily_write_lock = threading.Lock()
_daily_dirty = False


def _finite(v):
    """A number that can be compared and can be serialised.

    NaN IS A FLOAT AND IT POISONS BOTH. json.dumps writes a bare NaN,
    which is not JSON and which browser JSON.parse rejects outright - so
    one bad reading takes the whole dashboard down, not just its own
    row. And every comparison against NaN is false, so once stored as an
    extreme the range stops updating forever. Reported by review of
    099b249.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except (OverflowError, ValueError):
        return False


def _clean_row(x):
    """A row from disk, normalised, or None if it cannot be trusted.

    ANYTHING ON DISK IS INPUT. This accepted any dict with a "d" in it,
    so a hand-edited or truncated file could carry a range with no
    "lo", or a string where a count belongs - and the fold then raised
    INSIDE tick(). See the call site for why that was worse than it
    sounds. Reported by review of 479e6b6.
    """
    if not isinstance(x, dict):
        return None
    day = x.get("d")
    # A REAL DATE, not ten characters. "not-a-date" is ten characters,
    # and a row keyed on it breaks the oldest-first ordering the
    # endpoint promises and makes ?days=N return the wrong logical
    # days. Reported by review of 099b249.
    try:
        day = date.fromisoformat(day).isoformat()
    except (TypeError, ValueError):
        return None
    out = {"d": day}
    n = x.get("n")
    out["n"] = n if (isinstance(n, int) and not isinstance(n, bool)
                     and n >= 0) else 0
    for key in ("v", "tc", "rh"):
        r = x.get(key)
        if not isinstance(r, dict):
            continue
        lo, hi = r.get("lo"), r.get("hi")
        if not _finite(lo) or not _finite(hi):
            continue
        out[key] = {"lo": min(lo, hi), "hi": max(lo, hi)}
    return out


def load_daily():
    try:
        d = json.load(open(DAILY_PATH))
    except (ValueError, OSError):
        return []
    if not isinstance(d, list):
        return []
    rows = [r for r in (_clean_row(x) for x in d) if r]
    # SORTED AND DEDUPLICATED. The endpoint promises oldest-first;
    # a hand-edited file can break this without being malformed. Later
    # duplicates win, being the ones written most recently.
    byday = {}
    for r in rows:
        byday[r["d"]] = r
    rows = [byday[k] for k in sorted(byday)]
    return rows[-DAILY_KEEP_DAYS:]


def _daily_note(now, co2, tc, rh):
    """Fold one reading into today's row. Cheap and called every poll.

    KEPT SEPARATELY FROM history rather than derived from it: history
    holds 24 hours, so anything computed from it can only ever describe
    the last day - which is the limitation this exists to lift. The
    rollover is by LOCAL DATE, because "was yesterday hot" is a question
    about the day someone lived through, not about UTC.
    """
    global _daily_dirty
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with _daily_lock:
        rows = STATE.setdefault("daily", [])
        row = next((r for r in rows if r.get("d") == day), None)
        if row is None:
            # A gap of days leaves gaps in the list rather than empty
            # rows: a day the controller was off has no readings, and
            # inventing zeroes for it would be a lie a chart would draw.
            row = {"d": day, "n": 0}
            rows.append(row)
            rows.sort(key=lambda r: r["d"])
            del rows[:-DAILY_KEEP_DAYS]
        row["n"] = row.get("n", 0) + 1
        for key, val in (("v", co2), ("tc", tc), ("rh", rh)):
            if not _finite(val):
                continue
            cur = row.get(key)
            if not isinstance(cur, dict):
                row[key] = {"lo": val, "hi": val}
            else:
                if val < cur["lo"]:
                    cur["lo"] = val
                if val > cur["hi"]:
                    cur["hi"] = val
        _daily_dirty = True


def reset_daily():
    """Throw the summary away and remember that the file needs writing.

    A HELPER BECAUSE THE FLAG IS A MODULE GLOBAL. The recovery branch
    in tick() assigned _daily_dirty without declaring it global, so
    Python made a local and the reset was never marked for writing -
    the isolation worked, the persistence did not. Owning both changes
    in one place under one lock means the next caller cannot repeat it.
    Reported by review of 099b249.
    """
    global _daily_dirty
    with _daily_lock:
        STATE["daily"] = []
        _daily_dirty = True


def save_daily(force=False, report_error=True):
    # Serialize snapshot capture AND replacement, while allowing new samples
    # to accumulate under the independent data lock during disk I/O.
    with _daily_write_lock:
        return _save_daily_locked(force, report_error)


def _save_daily_locked(force=False, report_error=True):
    """Write only when something changed. Shares the history cadence.

    report_error IS FALSE FROM A SIGNAL HANDLER. _log takes
    _events_lock, which is not reentrant, so a signal arriving while a
    thread holds it would deadlock the shutdown path. The history
    writer already avoids _log for this reason and prints to stderr.
    Reported by review of 479e6b6.
    """
    global _daily_dirty
    with _daily_lock:
        if not _daily_dirty and not force:
            return True
        # DEEP. The rows and their ranges are mutated by the polling
        # thread, so a shallow copy shares them and the writer could
        # serialise a row mid-update.
        rows = copy.deepcopy(STATE.get("daily") or [])
        _daily_dirty = False
    try:
        write_json_durable(DAILY_PATH, rows)
        return True
    except OSError as e:
        with _daily_lock:
            _daily_dirty = True     # retried on the next flush
        if report_error:
            _log("%s Daily summary could not be written: %s"
                 % (FC_DISK_FAILED, e))
        return False


def maybe_save_hist(hist, force=False):
    """Batch writes: one full rewrite per poll was 2,880 a day at 30s.

    SERIALISED. The poll thread and the restart or shutdown path can
    both arrive here, and save_hist writes through one fixed temporary
    path - two writers interleaving on history.json.tmp can lose the
    graph, or make the restart path report that history could not be
    saved when it could. _last_flush was also read and written without
    synchronisation, so both could pass the interval check.

    Innermost lock: nothing is taken while holding it, so it cannot
    participate in an ordering problem with _act_lock or _state_lock.
    """
    global _last_flush
    with _hist_write_lock:
        now = time.monotonic()
        if not force and _last_flush is not None and now - _last_flush < HIST_FLUSH_SECONDS:
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

    Essential persistence needs only the fault lock, with a bounded wait.
    If actuation is busy, defer Manual-stop cleanup, never the safety write.
    The first version skipped the
    lock entirely, to avoid a signal handler blocking on something
    another thread held - and reintroduced the exact stale-snapshot race
    the lock exists to prevent: read the fault, have an acknowledgement
    clear it, then write the old snapshot back, and the fault returns
    from the dead at the next start with nothing to say why. Correctness
    first; the timeout is what keeps it from waiting until systemd
    escalates to SIGKILL. If a fault transaction outlasts the bound,
    report that refusal rather than waiting through the stop deadline.

    Giving up without writing is the right failure. Whoever holds the
    lock is in the middle of a fault transaction of their own, so the
    write is very likely to land anyway - and writing without the lock
    is what caused the bug.

    Reports to stderr rather than the activity log. _log takes
    _events_lock, another plain Lock, and this already holds one. stderr
    is the journal, which is where anyone looks after a stop that went
    wrong.
    """
    act_locked = _act_lock.acquire(blocking=False)
    if not _fault_lock.acquire(timeout=2):
        if act_locked:
            _act_lock.release()
        print("Could not take the fault lock on the way out; an unsaved "
              "fault may be lost. Check the draw before running the fan.",
              file=sys.stderr, flush=True)
        return False
    try:
        return _persist_fault_locked(cleanup=act_locked)
    finally:
        _fault_lock.release()
        if act_locked:
            _act_lock.release()


def _persist_fault_locked(cleanup=True):
    """Fault lock required; cleanup additionally requires the actuation lock."""
    with _state_lock:
        # Deep-copied: _confirm_fault_stopped mutates the dict in place,
        # so a reference could be changed underneath the write.
        fault = copy.deepcopy(STATE["fault"])
        owed = STATE["fault_unsaved"]
    if not owed:
        if fault and _fault_annotation_dirty == fault["at"]:
            return _save_annotation(fault)
        return False
    if fault is None:
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
    if cleanup:
        _finish_saved_relay_recovery_locked(fault)
    print("Unsaved fault written to disk on shutdown; it will survive "
          "the restart.", file=sys.stderr, flush=True)
    return True


def flush_hist(signum=None, frame=None):
    """Persist on shutdown so batching never costs a restart's samples."""
    if signum is not None:
        _stopping.set()
        if PERSIST_STOP_STATE:
            _save_stop_state()
        # Before the history write, which can be 20,000 points against a
        # stop deadline. The fault matters more than the graph.
        _persist_fault_on_exit()
    with _state_lock:
        snapshot = list(STATE["history"])
    ok = maybe_save_hist(snapshot, force=True)
    if not save_daily(force=True, report_error=signum is None) \
            and signum is not None:
        print("Daily summary could not be written on shutdown.",
              file=sys.stderr, flush=True)
    if signum is not None:
        if not ok:
            # Said out loud rather than swallowed: the graph will have a
            # gap and nothing else would ever mention it. stderr because
            # this is a signal handler and _log takes a lock.
            print("History could not be written on the way out; the trace "
                  "will be missing its most recent points.",
                  file=sys.stderr, flush=True)
        sys.exit(0)
    return ok

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if _finite(v) else None
    if isinstance(v, dict):
        for k in VALUE_KEYS:
            if k in v:
                return _num(v[k])
    return None


def find_metric(obj, hints):
    """Depth-first search for a numeric reading with an exact known key.

    Paths remain flexible because Ubiquiti has not published the airQuality
    schema, but keys are exact after punctuation/case normalization. Substring
    matches used to let metadata such as co2Threshold beat the live reading.
    Returns (value, dotted_path) or None.
    """
    hits = []
    known = set(hints)

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                flat = re.sub(r"[^a-z0-9]", "", str(k).lower())
                p = path + [k]
                if flat in known and _num(v) is not None:
                    hits.append((_num(v), ".".join(p)))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [str(i)])

    walk(obj, [])
    return hits[0] if hits else None


# Which kinds of request failure get a short name of their own.
# Ordered most specific first: ConnectTimeout and ReadTimeout both
# subclass Timeout and both say something more useful than it does.
_REQUEST_CAUSES = (
    ("ConnectTimeout", "timed out connecting"),
    ("ReadTimeout", "no reply before the timeout"),
    ("Timeout", "timed out"),
    ("TooManyRedirects", "too many redirects"),
    ("ConnectionError", "no connection"),
)


def _dns_failure(e):
    """Whether a request failed because the name would not resolve.

    Worth separating from every other connection failure because the
    remedy is a different one and troubleshooting.md sends people to
    the resolver for it - `getent hosts`, `resolvectl status` - which
    is wasted advice for a console that resolved fine and refused the
    connection.

    Walks the cause chain by TYPE, never by text, because the text is
    exactly what is being thrown away here and matching on it would
    reintroduce the coupling. socket.gaierror is stdlib and stable.
    urllib3's NameResolutionError is neither - it does not exist in
    1.x - so it is recognised by name without being imported, and a
    version that lacks it simply falls through to "no connection".

    The walk is bounded. requests wraps urllib3, urllib3 wraps the
    socket error, and a chain that loops would otherwise hang the poll
    thread inside its own error handler.
    """
    seen = 0
    while isinstance(e, BaseException) and seen < 10:
        if isinstance(e, socket.gaierror):
            return True
        if type(e).__name__ == "NameResolutionError":
            return True
        nxt = getattr(e, "reason", None)
        if not isinstance(nxt, BaseException):
            nxt = e.__cause__ or e.__context__
        if not isinstance(nxt, BaseException):
            args = getattr(e, "args", None) or ()
            nxt = args[0] if args and isinstance(args[0],
                                                 BaseException) else None
        e = nxt
        seen += 1
    return False


def _why(e):
    """One short phrase for a failed HTTP request.

    requests renders a connection failure as its whole retry stack -
    the pool, the port, the full URL, the wrapped urllib3 error and the
    errno underneath it - and that reached the activity log verbatim.
    The 2026-08-21 UDM OS update is the example worth keeping: the
    console rebooted, the name stopped resolving, and every poll wrote
    the same four-line paragraph to say so. On a phone it filled the
    log.

    The sentence this joins already names the subsystem and, where it
    matters, the host. All that is wanted here is WHICH KIND of failure
    it was. An unrecognised class falls back to its own name, which is
    short, stable, and cannot contain a URL - the same choice
    Tasmota._cmd already made for the same reason.
    """
    if _dns_failure(e):
        return "host name did not resolve"
    for name, phrase in _REQUEST_CAUSES:
        cls = getattr(requests.exceptions, name, None)
        if cls is not None and isinstance(e, cls):
            return phrase
    return type(e).__name__


class ProtectError(Exception):
    pass


class PlugError(Exception):
    def __init__(self, message, reason=None, elapsed=None):
        super().__init__(message)
        self.reason, self.elapsed = reason, elapsed


def _plug_issue(error):
    """Only locally classified metadata; never device text or request URLs."""
    if isinstance(error, PlugError) and error.reason:
        timing = (", %.2fs" % error.elapsed
                  if error.elapsed is not None else "")
        return "PlugError (%s%s)" % (error.reason, timing)
    return type(error).__name__


PROTECT_REQUEST_SECONDS = 30


class Protect:
    """Local-account session against UniFi OS, reading Protect's bootstrap."""

    def __init__(self, host, username, password, verify=False):
        self.host, self.username, self.password = host, username, password
        # verify=True requires a hostname the cert covers, not an IP.
        self.verify = bool(verify)
        self._s = None
        self._csrf = None
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._pending_request = None

    def _request(self, operation):
        """Bound caller latency including session contention, DNS and retries.

        requests' connect/read timeouts do not bound libc DNS. One daemon
        worker per client owns the session; an unfinished abandoned request
        rejects further work instead of accumulating workers. Healthy overlaps
        wait outside the session/worker locks within their own caller deadline.
        The worker touches only this client's session/result, never fan state.
        """
        deadline = time.monotonic() + PROTECT_REQUEST_SECONDS
        outcome = None
        while not _stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if outcome is None:
                with self._worker_lock:
                    if time.monotonic() >= deadline or _stopping.is_set():
                        break
                    if self._pending_request is not None and not self._pending_request['done'].is_set():
                        if time.monotonic() >= self._pending_request['deadline']:
                            raise ProtectError('Previous Protect request is still in progress')
                        previous = self._pending_request
                    else:
                        outcome = dict(done=threading.Event(), value=None, error=None, deadline=deadline)
                        self._pending_request = outcome
                        def fetch():
                            try:
                                outcome['value'] = operation()
                            except Exception as error:
                                outcome['error'] = error
                            finally:
                                outcome['done'].set()
                        try:
                            threading.Thread(target=fetch, daemon=True, name='protect-request').start()
                        except RuntimeError as error:
                            outcome['done'].set()
                            raise ProtectError('Could not start Protect request worker') from error
                if outcome is None:
                    previous['done'].wait(min(0.1, remaining))
                    continue
            if outcome['done'].wait(min(0.1, remaining)):
                if time.monotonic() >= deadline:
                    break
                if outcome['error'] is not None:
                    raise outcome['error']
                return outcome['value']
        raise ProtectError('Protect request did not complete within its deadline (including DNS)')

    @property
    def base(self):
        return "https://%s" % self.host

    def login(self):
        return self._request(self._login_direct)

    def _login_direct(self):
        s = requests.Session()
        s.verify = self.verify
        try:
            r = s.post(self.base + "/api/auth/login",
                       json=dict(username=self.username,
                                 password=self.password,
                                 rememberMe=True), timeout=15)
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
            raise ProtectError("Cannot reach the UniFi console (%s)"
                               % _why(e))
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
        if r.status_code >= 400:
            # Was raise_for_status(). Its HTTPError is not a
            # ProtectError, so the poll loop caught it in the catch-all
            # and filed a Protect outage as FC-200 unclassified - and
            # requests puts the full login URL in the message, which
            # then reached the activity log. Every other failure in
            # this method already reports as FC-201; this was the one
            # gap.
            raise ProtectError("UniFi login returned HTTP %d"
                               % r.status_code)
        self._csrf = r.headers.get("X-CSRF-Token")
        self._s = s

    def _get(self, path, retry=True):
        return self._request(lambda: self._get_direct(path, retry))

    def _get_direct(self, path, retry=True):
        with self._lock:
            if self._s is None:
                self._login_direct()
            h = {"Accept": "application/json"}
            if self._csrf:
                h["X-CSRF-Token"] = self._csrf
            try:
                r = self._s.get(self.base + path, headers=h, timeout=20)
            except requests.exceptions.SSLError as e:
                self._s = None
                # Truncated like login()'s. An OpenSSL failure prints a
                # verify chain, and the untruncated version here was the
                # second-longest line the log could produce.
                raise ProtectError(
                    "TLS verification failed for %s. Check the certificate "
                    "covers this name and has not expired, or turn off "
                    "Verify certificate. (%s)"
                    % (self.host, str(e)[:120]))
            except requests.RequestException as e:
                self._s = None
                raise ProtectError("Cannot reach the UniFi console (%s)"
                                   % _why(e))
        if r.status_code in (401, 403) and retry:
            self._s = None
            return self._get_direct(path, retry=False)
        if r.status_code >= 400:
            raise ProtectError("Protect returned HTTP %d for %s"
                               % (r.status_code, path))
        try:
            data = r.json()
        except ValueError:
            # A console part-way through an OS update answers 200 with
            # an HTML holding page. r.json() then raised JSONDecodeError
            # out of the poll loop's catch-all, so the log said "FC-200
            # JSONDecodeError: Expecting value: line 1 column 1" -
            # pointing at this controller, in the series that means
            # unclassified, for something Protect did and recovered from
            # on its own. Named here so it lands as FC-201 beside the
            # unreachable case it sits between.
            raise ProtectError("Protect replied with something that is not "
                               "JSON. The console may be restarting.")
        if not isinstance(data, dict):
            raise ProtectError("Protect returned a JSON value where an "
                               "object was expected")
        return data

    def sensors(self):
        data = self._get("/proxy/protect/api/bootstrap")
        sensors = data.get("sensors")
        if sensors is None:
            return []
        if not isinstance(sensors, list) or not all(
                isinstance(sensor, dict) for sensor in sensors):
            raise ProtectError("Protect returned an invalid sensors list")
        return sensors

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
        if s.get("isConnected") is False:
            # Protect can retain measurements in a disconnected device's
            # payload. They must not refresh the stale-reading safety timer.
            raise ProtectError("Selected sensor is disconnected")
        air = s.get("airQuality")
        if air is not None and not isinstance(air, dict):
            raise ProtectError("Protect returned invalid airQuality data")
        src = air or s
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
        s = self.sensor(sid)
        if s.get("isConnected") is False:
            raise ProtectError("Selected sensor is disconnected")
        hit = find_metric(s, CO2_HINTS)
        if hit is None:
            raise ProtectError("No CO2 field in this sensor's payload. Open "
                               "Debug to see what Protect is returning.")
        return hit


# One installed controller drives one plug. Share across client replacements
# and dashboard tests, not per client instance. Lock order is actuation then
# plug I/O; the HTTP path never takes the actuation/config/state locks.
class _PlugRequestGate:
    """Concurrent HTTP requests, with an exclusive bounded restart barrier."""
    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0
        self._exclusive = False
        self._waiting = 0

    @contextmanager
    def request(self):
        with self._condition:
            while self._exclusive or self._waiting:
                self._condition.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def acquire(self, blocking=True, timeout=-1):
        deadline = None if timeout < 0 else time.monotonic()+timeout
        with self._condition:
            self._waiting += 1
            try:
                while self._exclusive or self._active:
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline-time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._exclusive = True
                return True
            finally:
                self._waiting -= 1
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if not self._exclusive:
                raise RuntimeError('Plug restart barrier is not held')
            self._exclusive = False
            self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


_plug_request_lock = _PlugRequestGate()

# The plug can retain a closed TCP tuple longer than Linux does. Bind first,
# reject recently used source ports, then connect using that SAME socket.
# Shared across client replacements; never tune system-wide TCP settings.
PLUG_PORT_HOLD_SECONDS = 180
_plug_ports = {}
_plug_ports_lock = threading.Lock()


def _bind_plug_socket(sock):
    sock.bind(('', 0))
    key = (int(sock.family), sock.getsockname()[1])
    now = time.monotonic()
    with _plug_ports_lock:
        for old in list(_plug_ports):
            if now-_plug_ports[old] >= PLUG_PORT_HOLD_SECONDS:
                del _plug_ports[old]
        if key in _plug_ports:
            return None
        _plug_ports[key] = now
    return key


def _close_failed_plug_socket(sock, key):
    try:
        sock.close()
    finally:
        if key is not None:
            with _plug_ports_lock:
                _plug_ports[key] = time.monotonic()


PLUG_DNS_SECONDS = 3
_plug_dns_lock = threading.Lock()
_plug_dns_pending = {}
_plug_request_context = threading.local()


class _PlugRequestDeadline:
    """Interrupt socket I/O, never run a relay command in a worker thread."""
    def __init__(self, seconds):
        self.until = time.monotonic() + seconds
        self.lock = threading.Lock()
        self.sockets = []
        self.expired = False
        self.finished = False
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True

    def remaining(self):
        remaining = self.until - time.monotonic()
        if self.expired or remaining <= 0:
            raise socket.timeout('Plug request deadline expired')
        return remaining

    def register(self, sock, key):
        with self.lock:
            self.remaining()
            self.sockets.append((sock, key))

    def expire(self):
        with self.lock:
            if self.finished:
                return
            self.expired = True
            for sock, key in self.sockets:
                # close alone does not interrupt a makefile() body/header read.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                _close_failed_plug_socket(sock, key)

    def finish(self):
        with self.lock:
            self.timer.cancel()
            self.remaining()
            self.finished = True

    def cancel(self):
        with self.lock:
            self.finished = True
            self.timer.cancel()
            # Connection.close() can run at headers while HTTPResponse's
            # makefile still owns the socket. The session/body has unwound
            # before this final teardown, so restart the hold HERE as well.
            for sock, key in self.sockets:
                _close_failed_plug_socket(sock, key)


def _plug_addresses(host, port, family):
    """Bound DNS only: abandoned workers can never connect or send commands."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        af = socket.AF_INET if literal.version == 4 else socket.AF_INET6
        if family not in (socket.AF_UNSPEC, af):
            raise socket.gaierror('Plug address family is unavailable')
        if literal.version == 4:
            address = (str(literal), port)
        else:
            scope = literal.scope_id
            scope = (int(scope) if str(scope).isdigit() else
                     socket.if_nametoindex(scope)) if scope else 0
            address = (str(literal).split('%')[0], port, 0, scope)
        return [(af, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)]

    def resolve(outcome, resolver):
        try:
            outcome['addresses'] = resolver(host, port, family, socket.SOCK_STREAM)
        except Exception as exc:
            outcome['error'] = exc
        finally:
            outcome['done'].set()

    key = (host, port, family)
    with _plug_dns_lock:
        for old in list(_plug_dns_pending):
            if _plug_dns_pending[old]['done'].is_set():
                del _plug_dns_pending[old]
        outcome = _plug_dns_pending.get(key)
        if outcome is None:
            # Configuration changes must not accumulate unlimited stuck libc
            # lookups. Healthy concurrent callers for one host share its worker.
            if len(_plug_dns_pending) >= 4:
                raise socket.timeout('Plug DNS workers are still busy')
            outcome = dict(done=threading.Event(), addresses=[], error=None,
                           deadline=time.monotonic() + PLUG_DNS_SECONDS)
            _plug_dns_pending[key] = outcome
            try:
                threading.Thread(target=resolve, args=(outcome, socket.getaddrinfo),
                                 daemon=True, name='plug-dns').start()
            except RuntimeError:
                del _plug_dns_pending[key]
                raise OSError('Could not start plug DNS lookup') from None
    while not _stopping.is_set():
        remaining = outcome['deadline'] - time.monotonic()
        request = getattr(_plug_request_context, 'deadline', None)
        if request is not None:
            remaining = min(remaining, request.remaining())
        if remaining <= 0:
            break
        if outcome['done'].wait(min(0.1, remaining)):
            if time.monotonic() >= outcome['deadline'] or _stopping.is_set():
                break
            if outcome['error'] is not None:
                raise outcome['error']
            return outcome['addresses']
    raise socket.timeout('Plug DNS lookup timed out')


class _PlugHTTPConnection(HTTPConnection):
    def _new_conn(self):
        try:
            addresses = _plug_addresses(self._dns_host, self.port, allowed_gai_family())
            error = OSError('No usable plug address or source port')
            for family, kind, proto, _, address in addresses:
                for _ in range(128):
                    deadline = getattr(_plug_request_context, 'deadline', None)
                    if deadline is not None:
                        deadline.remaining()
                    sock = socket.socket(family, kind, proto)
                    key = None
                    try:
                        for option in self.socket_options or ():
                            sock.setsockopt(*option)
                        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                            sock.settimeout(self.timeout)
                        key = _bind_plug_socket(sock)
                        if key is None:
                            sock.close()
                            continue
                        self._plug_port_key = key
                        if deadline is not None:
                            deadline.register(sock, key)
                            sock.settimeout(min(self.timeout, deadline.remaining()))
                        sock.connect(address)
                        if deadline is not None:
                            deadline.remaining()
                        return sock
                    except OSError as exc:
                        _close_failed_plug_socket(sock, key)
                        error = exc
                        break
                    except BaseException:
                        _close_failed_plug_socket(sock, key)
                        raise
            raise error
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        except socket.timeout as exc:
            raise ConnectTimeoutError(self, 'Plug connection timed out') from exc
        except OSError as exc:
            raise NewConnectionError(self, 'Could not establish plug connection') from exc

    def close(self):
        # Start the hold at close, not request start (a read can take 12s).
        if self.sock is not None and hasattr(self, '_plug_port_key'):
            with _plug_ports_lock:
                _plug_ports[self._plug_port_key] = time.monotonic()
        super().close()


class _PlugHTTPPool(HTTPConnectionPool):
    ConnectionCls = _PlugHTTPConnection


class _PlugHTTPAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme['http'] = _PlugHTTPPool


def _plug_http_get(url, **kwargs):
    # A local plug must be reached directly, never through an environment
    # proxy. No automatic retries of commands that may already have landed.
    deadline = _PlugRequestDeadline(kwargs['timeout'])
    previous = getattr(_plug_request_context, 'deadline', None)
    _plug_request_context.deadline = deadline
    try:
        try:
            deadline.timer.start()
        except RuntimeError as exc:
            raise requests.ConnectionError('Could not start plug deadline timer') from exc
        with requests.Session() as session:
            session.trust_env = False
            session.mount('http://', _PlugHTTPAdapter(max_retries=0))
            response = session.get(url, allow_redirects=False, **kwargs)
            deadline.finish()
            return response
    except (requests.RequestException, OSError):
        if deadline.expired or time.monotonic() >= deadline.until:
            raise requests.Timeout('Plug request deadline expired') from None
        raise
    finally:
        deadline.cancel()
        _plug_request_context.deadline = previous


def _plug_port_handover():
    with _plug_ports_lock:
        now = time.monotonic()
        return [[family, port, PLUG_PORT_HOLD_SECONDS-(now-at)]
                for (family, port), at in _plug_ports.items()
                if 0 <= now-at < PLUG_PORT_HOLD_SECONDS]


def _restore_plug_ports(rows):
    if not isinstance(rows, list) or len(rows) > 2048:
        return
    with _plug_ports_lock:
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                continue
            family, port, remaining = row
            if (type(family) is int and family in (socket.AF_INET, socket.AF_INET6)
                    and type(port) is int and 1 <= port <= 65535
                    and _finite(remaining) and 0 < remaining <= PLUG_PORT_HOLD_SECONDS):
                _plug_ports[(family, port)] = time.monotonic()-(PLUG_PORT_HOLD_SECONDS-remaining)


def _open_plug_diagnostics():
    """Local, time-limited opt-in; never part of saved device settings."""
    try:
        with open(os.path.join(DATA_DIR, 'plug-diagnostics.enabled')) as stream:
            until = json.load(stream)['until']
        if not _finite(until) or until <= time.time():
            return None, 0
        folder = os.path.join(DATA_DIR, 'diagnostics')
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        path = os.path.join(folder, 'plug-requests.jsonl')
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        logger = logging.Logger('fan-plug-diagnostics')
        logger.addHandler(RotatingFileHandler(path, maxBytes=1048576,
                                             backupCount=3))
        return logger, min(until, time.time()+7*24*3600)
    except Exception:
        # Diagnostics must never block startup or change fan protection.
        return None, 0


_plug_diagnostics, _plug_diagnostics_until = _open_plug_diagnostics()


def _plug_diag(phase, request_id, operation, **fields):
    if _plug_diagnostics is None or time.time() >= _plug_diagnostics_until:
        return
    try:
        _plug_diagnostics.info(json.dumps(dict(at=time.time(), phase=phase,
            request_id=request_id, operation=operation, **fields)))
    except Exception:
        pass


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
        queued = time.monotonic()
        request_id = time.monotonic_ns()
        # Fixed labels only: never commands, URLs, credentials or replies.
        operation = {'Status 8': 'power', 'Power': 'relay',
                     'Power ON': 'switch_on', 'Power OFF': 'switch_off'}.get(cmnd, 'other')
        _plug_diag('queued', request_id, operation)
        with _plug_request_lock.request():
            started = time.monotonic()
            _plug_diag('start', request_id, operation, wait_s=round(started-queued, 6))
            try:
                result = self._cmd_locked(cmnd)
            except Exception as exc:
                _plug_diag('error', request_id, operation,
                           elapsed_s=round(time.monotonic()-started, 6),
                           error_type=type(exc).__name__,
                           reason=exc.reason if isinstance(exc, PlugError) else 'unexpected')
                raise
            _plug_diag('ok', request_id, operation,
                       elapsed_s=round(time.monotonic()-started, 6))
            return result

    def _cmd_locked(self, cmnd):
        """Execute one HTTP request; other registered requests may overlap.

        Transport timeout timing excludes waiting for the restart barrier.
        No caller callbacks or controller locks are acquired on this path.
        """
        if PLUG_ACCESS_DISABLED:
            raise PlugError("Plug access is disabled for this installation",
                            reason="local interlock")
        params = {"cmnd": cmnd}
        if self.password:
            params["user"] = self.user
            params["password"] = self.password
        started = time.monotonic()
        try:
            r = _plug_http_get("http://%s/cm" % self.host, params=params,
                             timeout=12)
        except requests.RequestException as e:
            # Only the exception type: requests puts the full URL in the
            # message, and the credentials are query parameters.
            raise PlugError("Cannot reach the plug at %s (%s)"
                            % (self.host, type(e).__name__),
                            reason=type(e).__name__,
                            elapsed=time.monotonic()-started)
        elapsed = time.monotonic()-started
        if r.status_code >= 400:
            raise PlugError("Plug returned HTTP %d" % r.status_code,
                            reason="HTTP %d" % r.status_code, elapsed=elapsed)
        if not r.content:
            raise PlugError("Plug returned an empty reply. Check that the "
                            "HTTP command API is enabled on the device.",
                            reason="empty reply", elapsed=elapsed)
        try:
            data = r.json()
        except ValueError:
            raise PlugError("Plug returned a non-JSON reply",
                            reason="non-JSON reply", elapsed=elapsed)
        if not isinstance(data, dict):
            raise PlugError("Plug returned a non-object JSON reply",
                            reason="non-object JSON reply", elapsed=elapsed)
        if "WARNING" in data:
            raise PlugError("Plug rejected the credentials",
                            reason="credentials rejected", elapsed=elapsed)
        return data

    @staticmethod
    def _power(data):
        """True, False, or None when the reply cannot be read.

        None is the whole contract. state() turns it into a raise, and
        every caller of state() treats a raise as "switch anyway" - see
        the comment in _manual_off_locked. A value that is neither a
        bool nor a recognisable ON/OFF string is therefore reported as
        UNKNOWN rather than guessed at, because guessing "off" is what
        let a live relay be recorded as stopped: _manual_off_locked
        skipped the switch and then discharged the durable promise, and
        the draw enforcement recorded a confirmed stop and quit
        retrying. Neither raised, because nothing had gone wrong as far
        as either could tell.

        Only bools and the strings ON and OFF are believed. A JSON true
        means on. An int 1 does NOT, because Tasmota does not send one
        and inventing that mapping would be a guess in the one place
        guessing is unsafe. If a firmware ever does send "1", this
        returns None and the plug reads as unreadable - which is loud,
        and the right moment to add it deliberately with a capture to
        justify it.

        A key that is present but unusable falls through to the next key
        rather than short-circuiting, so a reply carrying a junk POWER
        and a good POWER1 is still readable.
        """
        for k in ("POWER", "POWER1"):
            if k not in data:
                continue
            v = data[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                v = v.strip().upper()
                if v == "ON":
                    return True
                if v == "OFF":
                    return False
        return None

    def state(self):
        """True or False. Raises if the plug reports no power field, so an
        unknown state is never mistaken for off."""
        v = self._power(self._cmd("Power"))
        if v is None:
            raise PlugError("Plug reported no power state", reason="missing relay state")
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
            raw = d["StatusSNS"]["ENERGY"]["Power"]
            if isinstance(raw, bool):
                return None
            watts = float(raw)
            return watts if math.isfinite(watts) else None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


STATE = {
    "plug_polling_enabled": not PLUG_ACCESS_DISABLED,
    "co2": None, "co2_path": None, "fan": None, "reading_at": None,
    "relay_revision": 0,  # Process-local sequence for confirmed observations/actions.
    "changed_at": None, "error": None, "sensor_name": None,
    # Demand may renew a lease without physically starting the motor.
    "min_run_at": None,
    # Sensor acquisition health is separate from plug/control failures that
    # may occur after a valid reading has already been recorded.
    "sensor_error": None,
    "temperature": None, "humidity": None, "pm25": None,
    "watts": None, "watts_at": None,
    "latch": {"co2": False, "temp": False, "humidity": False},
    # Which rules have latched at any point during the CURRENT run.
    # Separate from "latch" rather than folded into it because that dict
    # is the live state - the handover iterates its keys and the stop
    # confirmation reads them - and a rule that released an hour ago
    # must not read as still holding the fan on. Accumulated while the
    # fan runs, cleared when it stops.
    "ran_with": {},
    # One bounded hold for a relay found ON after a fault-free Auto cold
    # start. Monotonic time avoids wall-clock corrections extending it.
    "cold_start_pending": False, "cold_start_hold_until": None,
    "manual_off_pending": False,
    "history": [], "events": [], "history_rev": 0, "fault": None,
    # True when the fault in memory could not be written to disk, so
    # a restart would lose it. Retried every draw cycle.
    "fault_unsaved": False,
    # Draw-loop status is independent of the Protect poll error. A broken
    # draw check must be visible even while the dashboard still responds.
    "draw_loop_at": None, "draw_loop_error": None,
}
# RLock, not Lock: nested acquisition deadlocked the poll thread.
_state_lock = threading.RLock()
START_TIME = time.time()
_elapsed_clocks = {"startup": (START_TIME, time.monotonic())}


def _stamp_elapsed_locked(key, wall=None, mono=None):
    """Record both clocks; readings may supply their earlier acquisition pair."""
    wall = time.time() if wall is None else wall
    STATE[key] = wall
    _elapsed_clocks[key] = (wall, time.monotonic() if mono is None else mono)


def _elapsed_age_locked(key, startup=False):
    """Elapsed age; legacy wall-only state is converted once, never per poll."""
    wall = STATE.get(key)
    if wall is None:
        if not startup:
            return None
        key, wall = "startup", START_TIME
    pair = _elapsed_clocks.get(key)
    if pair is None or pair[0] != wall:
        pair = (wall, time.monotonic() - max(0, time.time() - wall))
        _elapsed_clocks[key] = pair
    return max(0, time.monotonic() - pair[1])
_wake = threading.Event()  # set to make the poll loop act at once
# Force on is asserted from both the poll thread and the draw thread, so
# the read-decide-switch has to be atomic or the two can each see the fan
# off, both switch it, and both log the same event.
_act_lock = threading.Lock()
# Guards the fault as one transaction across memory and disk.
#
# THE FULL ORDER, when more than one is held:
#
#     _cfg_lock -> _act_lock -> _fault_lock -> _state_lock
#
# NOTHING MAY TAKE _act_lock WHILE HOLDING _fault_lock. This comment
# used to say the two were never held together at all, which stopped
# being true when the draw decision and the fault acknowledgement each
# had to be one transaction: a settings save landing mid-decision
# produced a fault quoting limits that had already been replaced, and an
# acknowledgement landing mid-enforcement let the fan be stopped after
# the dashboard said the fault was clear.
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
# unrelated change, a UI password among them. Order is _cfg_lock then
# _act_lock; nothing takes _cfg_lock while holding _act_lock.
_cfg_lock = threading.RLock()

# Bumped every time CFG is replaced. A cycle captures it alongside its
# clients and re-checks before actuating: the settings are already
# re-read under _act_lock, but the CLIENT was not, so a plug address
# changing during a Protect read left the new rules commanding the OLD
# physical plug. Re-reading CFG cannot fix that - the client is built
# from the config that was current when the cycle started.
#
# An integer rather than comparing configs: cheap, and it changes even
# when a save rewrites the same values, which is the conservative
# direction. A discarded cycle costs one poll interval.
_cfg_generation = 0


def _stale_client(gen):
    """True if CFG has been replaced since this cycle built its clients.

    ONLY MEANINGFUL UNDER _act_lock. The config commit swaps CFG and
    bumps the generation while holding that lock, so a check inside it
    cannot be overtaken between the check and the plug.set() after it.
    Checked outside, it is a race with a shorter window rather than a
    guard.

    gen is None for callers with no window to guard - a client built
    inside the same lock hold, or a caller that has no generation to
    offer. Those keep the old behaviour rather than being refused.
    """
    return _stopping.is_set() or (gen is not None and gen != _cfg_generation)


def _client_snapshot():
    """Clients, and the config generation they were built from.

    Taken under _cfg_lock so the pair is coherent. Lock order is
    _cfg_lock then _clients_lock; nothing takes _clients_lock first.
    """
    with _cfg_lock:
        gen = _cfg_generation
        p, g = _clients()
    return p, g, gen


CFG = load_cfg()
STATE["history"] = load_hist()
STATE["daily"] = load_daily()
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
                         ("email", "password"), ("ui", "password_hash")):
        secret = (CFG.get(section) or {}).get(key)
        if secret and len(str(secret)) >= 4:
            out = out.replace(str(secret), "***")
    return out



# THE SHAPE OF A LINE: state first, then why, separated by an em dash.
#
#     Fan on - 1000 ppm
#     Fan off - manual
#     FC-001 Fan on - no reading for 30 min
#
# The left of the dash is what changed and is the same handful of
# strings every time, so the log scans as a column on a narrow screen
# instead of a wall of sentences. The right is the reason and varies.
# Anything that is not a fan-state change says what it is first for the
# same reason: "Restart refused - ...", "Fault cleared - ...".
#
# Rewritten 2026-08-16: the same event class had three different verbs
# - "Fan on", "Fan switched to off", "Manual mode - switched off" - and
# the long ones wrapped on a phone.

# How long a line stays open to being folded into. Generous on
# purpose: a mode change is followed by its actuation on the NEXT poll,
# which is poll_seconds away and configurable, and a plug that needed a
# retry pushes it further. The exact-match and newest-event conditions
# are what make it safe, not the window.
ABSORB_SECONDS = 300


def _log(msg, absorb=None, merged=None):
    """Append an event and persist it. Safe from any thread.

    ABSORB FOLDS THE LINE ABOVE. A mode change and the actuation it
    causes are logged by different code paths a poll apart, so the
    journal read as pairs saying the same thing twice:

        Mode: Force on
        Fan on - forced on

    If the NEWEST event is exactly `absorb` and recent, it is replaced
    by `merged` rather than a second line being added. Conditions kept
    deliberately tight: exact string, newest position only, one
    replacement. Anything logged in between means the two are no longer
    adjacent and both stand - which is the honest outcome, because
    something happened between them.

    Without `absorb` this is the plain append it always was.
    """
    with _events_lock:
        with _state_lock:
            folded = False
            if absorb and STATE["events"]:
                top = STATE["events"][0]
                if (top.get("msg") == absorb
                        and time.time() - top.get("at", 0) <= ABSORB_SECONDS):
                    # The timestamp stays the ORIGINAL one. The mode
                    # changed when the person clicked, not when the plug
                    # got round to it, and the second half is what was
                    # already implied.
                    top["msg"] = merged or msg
                    folded = True
            if not folded:
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
# There is no clear_on_missing any more. The three rules used to
# disagree about a dropped reading - CO2 held its latch, temperature and
# humidity released theirs - which recorded how the code happened to be
# written rather than a decision. All three now hold. See decide().
RULES = (
    {"key": "co2", "reading": "co2", "flag": "co2_enabled",
     "on": "on_at", "off": "off_at", "default": True, "label": "CO2",
     "fmt": lambda v: "%d ppm" % round(v)},
    {"key": "temp", "reading": "temperature", "flag": "temp_enabled",
     "on": "temp_on", "off": "temp_off", "default": False,
     "label": "temperature",
     "fmt": lambda v: "%.1f C" % v},
    {"key": "humidity", "reading": "humidity",
     "flag": "humidity_enabled", "on": "humidity_on",
     "off": "humidity_off", "default": False, "label": "humidity",
     "fmt": lambda v: "%.0f%% RH" % v},
)


def active_rules(c):
    """The rules Auto is currently allowed to act on."""
    return [r for r in RULES if bool(c.get(r["flag"], r["default"]))]


def decide(co2, temp, fan_on, c, ran_for, latch, fault=None,
           humidity=None, ran_with=None):
    """Per-rule latching hysteresis. Mutates `latch` in place.

    Each input latches on above its own on-level and releases below its
    own off-level. The fan runs while any rule is latched. This matters:
    previously a rule that had never triggered could still veto the stop,
    so a CO2-driven run was held on by temperature indefinitely.

    With every rule disabled there is nothing to latch, so Auto never
    starts the fan. That is allowed - the UI warns rather than refusing -
    and the stale-reading fail-safe still runs.

    `ran_with`, when given, is mutated in place to record every rule
    that has latched during the current run, so the stop line can name
    all of them and not merely whichever was last to let go. It is
    cleared when the fan stops. Optional so that callers testing a
    single decision need not carry run-length state.
    """
    readings = {"co2": co2, "temp": temp, "humidity": humidity}
    if c["mode"] == "on":
        return True, "forced on"
    # Force on is checked first on purpose: it overrides a fault so a
    # repaired fan can be tested. Past here, a fault blocks automatic
    # starts, including the stale-reading failsafe.
    if fault:
        if ran_with is not None:
            ran_with.clear()
        return False, "draw fault"
    if c["mode"] == "manual":
        for r in RULES:
            latch[r["key"]] = False
        # The run is over and no rule ended it. Leaving the accumulator
        # populated would let a later Auto stop name rules from a run
        # that finished before the mode changed.
        if ran_with is not None:
            ran_with.clear()
        return None, None
    # Snapshot before the loop mutates it. The stop line names the
    # rules that were HOLDING the fan on, and by the time the decision
    # is reached every one of them has already released.
    was_latched = {r["key"]: bool(latch.get(r["key"])) for r in RULES}
    for r in RULES:
        key, val = r["key"], readings.get(r["key"])
        if not bool(c.get(r["flag"], r["default"])):
            latch[key] = False
        elif val is None:
            # Deliberately nothing: the latch is left as it was.
            #
            # A missing reading is not evidence the condition cleared.
            # The room was hot, the sensor stopped answering, and
            # nothing has happened since to suggest it cooled - so
            # releasing here would stop the fan on the strength of not
            # knowing. This errs toward ventilating, like the rest of
            # decide().
            #
            # It only matters during a PARTIAL outage. If every reading
            # stops, reading_at stops advancing and the stale-reading
            # fail-safe ventilates regardless. The uncovered case was
            # one metric dropping out while others still report, which
            # keeps the fail-safe asleep - exactly when releasing the
            # latch was worst.
            #
            # The cost is a fan that may run longer than needed on a
            # stale latch until a reading returns and falls below the
            # off level. That is the cheaper mistake.
            pass
        elif val >= c[r["on"]]:
            latch[key] = True
        elif val <= c[r["off"]]:
            latch[key] = False
    want = any(latch.get(r["key"]) for r in RULES)
    # BEFORE the early return below, which is the path taken for most of
    # a run: a rule that latches and releases again while the fan is
    # already on would otherwise never be recorded.
    if ran_with is not None and want:
        for r in RULES:
            if latch.get(r["key"]):
                ran_with[r["key"]] = True
    if want == bool(fan_on):
        return (True, None) if want else (False, None)
    if want:
        # A latch can now outlive its reading, so a rule may be holding
        # the fan on with nothing to print. Saying so beats the empty
        # string this used to produce: the log read "Fan on - " and
        # gave no reason at all, which is at its worst exactly when the
        # reason is unusual.
        why = [r["fmt"](readings[r["key"]])
               if readings.get(r["key"]) is not None
               else "%s latch held, no reading" % r["label"]
               for r in RULES if latch.get(r["key"])]
        return True, " and ".join(why)
    if fan_on and ran_for is not None and \
            ran_for < c["min_run_minutes"] * 60:
        return None, None
    # NAME WHAT WAS HOLDING IT ON, not every metric that happens to be
    # enabled. A CO2-driven run ended with "Fan off - 672 ppm, 21.9 C,
    # 54% RH": three numbers, two with no bearing on the stop, and the
    # reader still left to work out which rule released. "Fan on"
    # already names only its trigger, so this makes the pair read the
    # same way round.
    #
    # A rule that was latched has a reading here by construction - a
    # missing one leaves the latch alone, which would have kept the fan
    # on - except where the rule was disabled mid-run, so the None
    # guard stays.
    held = dict(ran_with) if ran_with else {}
    for r in RULES:
        if was_latched.get(r["key"]):
            held[r["key"]] = True
    # The run ends here, so the accumulator starts again empty. Cleared
    # after it is read, not before, and not at the caller: a stop that
    # forgot to clear would carry rules into the next run and name them
    # there.
    if ran_with is not None:
        ran_with.clear()
    parts = [r["fmt"](readings[r["key"]]) for r in RULES
             if held.get(r["key"])
             and readings.get(r["key"]) is not None]
    if not parts:
        # Nothing was latched, so no rule released and there is nothing
        # to name. The fan was on for some other reason and Auto is
        # taking it back. Report the enabled readings rather than
        # nothing, which is the empty-string the "Fan on" branch above
        # already had to be fixed for.
        parts = [r["fmt"](readings[r["key"]]) for r in active_rules(c)
                 if readings.get(r["key"]) is not None]
    return False, ", ".join(parts) if parts else "no reading"


_relay_cache = None
_normal_relay_at = None


def _relay_fallback_needed(plug, gen):
    """Only supplement normal polling when state is unknown/unverified
    or its relay observations have stopped. Caller does not hold locks.
    """
    with _act_lock:
        if _stale_client(gen):
            return False
        cached = _relay_cache
        with _state_lock:
            unknown = STATE["fan"] is None
            sensor_failed = bool(STATE["sensor_error"])
        if (unknown or sensor_failed or cached is None or cached[0] is not plug
                or cached[1] != gen or cached[4] is not None):
            return True
        normal = _normal_relay_at
        if normal is None or normal[0] is not plug or normal[1] != gen:
            return True
        # Give the normal sensor cycle one meter interval of scheduling
        # grace. A sensor outage never disables protection indefinitely.
        age = time.time()-normal[2]
        return age < 0 or age > max(RELAY_SECONDS, CFG["control"]["poll_seconds"])+WATTS_SECONDS


def _remember_relay(plug, actual, error=None):
    """Cache a reply/failed attempt under the actuation lock."""
    global _relay_cache
    _relay_cache = (plug, _cfg_generation, time.time(), actual, error)


def _read_relay(plug, gen=None, poll=False):
    """Share one background relay attempt per 30s across both loops.

    Caller holds the actuation lock. Explicit actuation and fault
    confirmation still request fresh reads.
    Failed attempts are cached too, so a second loop cannot immediately
    retry a timeout. Confirmed switch replies update the same cache.
    """
    if _stale_client(gen):
        return None
    cached = _relay_cache
    if (poll and cached is not None and cached[0] is plug
            and cached[1] == _cfg_generation
            and 0 <= time.time()-cached[2] < RELAY_SECONDS):
        if cached[4] is not None:
            raise cached[4]
        return cached[3]
    try:
        actual = plug.state()
    except PlugError as error:
        _remember_relay(plug, None, error)
        raise
    _remember_relay(plug, actual)
    return actual


def _fan_status(plug, poll=False):
    """What the plug says it is doing, or None if unreadable.

    Single relay, so there is no multi-output split to reconcile as there
    was with the previous multi-output client.
    """
    if PLUG_ACCESS_DISABLED:
        return None
    try:
        return _read_relay(plug, poll=poll)
    except PlugError:
        return None


def _end_run_locked():
    """Clear the live run record. Requires _state_lock.

    FOR THE CONFIRMED STOPS THAT decide() CANNOT SEE - four of them.
    Four consecutive reviews found the same defect in a different one,
    each time because the record was cleared where a stop was
    convenient to notice rather than everywhere one is confirmed:

      1. _adopt_relay_locked(), when either loop reads the plug as off -
         someone switched it by hand, from the vendor app, or from a
         second controller. No
         command is issued, so nothing else on that path can end the
         run. This also covers the first poll after a restart, where
         the handover has restored a record but not the cached fan
         state, so there is nothing to have "changed" from.
      2. _manual_off_locked(), after the plug takes the manual stop.
         tick() returns before the control decision in manual mode, so
         the decision path cannot see this one at all.
      3. _confirm_fault_stopped(), in the branch where the fault write
         landed. A draw fault ends the run.
      4. _force_on(), when the plug is confirmed off before being
         forced back on. The physical run ended even though the fan is
         about to start again, and the new run is not the old one.

    THE COMMANDED AUTOMATIC STOP IS NOT ONE OF THESE, and deliberately
    so. decide() clears a COPY, and tick() commits that copy only once
    the relay has accepted the switch-off - so a stop the plug refused
    leaves the live record intact for the retry to name. Routing it
    through here instead would clear on intent rather than on
    confirmation, which is the defect this whole sequence began with.
    So the clearing is not literally in one place; it is in one place
    per transaction model, and this is the non-transactional half.

    ONLY ON A CONFIRMED OFF. An unreadable plug is unknown, not off,
    and clearing on unknown would lose a run that is still going.

    A stop that was attempted and FAILED must not call this: the fan
    may still be running, the stop will be retried, and the retry needs
    the record to name.
    """
    STATE["ran_with"].clear()
    STATE["min_run_at"] = None


def _adopt_relay_locked(actual, now):
    """Record relay state with caller holding act -> fault -> state locks.

    Shared by sensor polling and independent draw monitoring. A first
    observation does not invent a new minimum-run lease or spin-up grace.
    Unknown is never interpreted as off or as the end of a run.
    """
    prev = STATE["fan"]
    if actual is not None:
        STATE["relay_revision"] += 1
    if actual is True and STATE["fault"] and STATE["fault"].get("stopped"):
        STATE["fault"].update(stopped=False)
        STATE["fault_unsaved"] = True
    startup_msg = None
    if STATE["cold_start_pending"] and actual is not None:
        STATE["cold_start_pending"] = False
        if (prev is None and actual is True and CFG["control"]["mode"] == "auto"
                and STATE["fault"] is None and not PLUG_ACCESS_DISABLED
                and CFG["control"]["cold_start_minutes"] > 0):
            minutes = CFG["control"]["cold_start_minutes"]
            STATE["cold_start_hold_until"] = time.monotonic() + minutes * 60
            startup_msg = "%s Latched on \u2014 Fan on (%d min)" % (FC_COLD_HOLD, minutes)
    if actual is False:
        STATE["cold_start_hold_until"] = None
        _end_run_locked()
    if actual is not None and actual != prev:
        STATE["fan"] = actual
        if prev is not None:
            _stamp_elapsed_locked("changed_at", now)
            STATE["min_run_at"] = None
            return "%s Fan %s — changed outside (%s)" % (
                FC_EXTERNAL_CHANGE, "on" if actual else "off", _mode_now())
    return startup_msg


def _refresh_relay(plug, gen=None, poll=False):
    """Observe this fan independently of Protect, without switching it."""
    with _act_lock:
        if _stale_client(gen):
            return
        actual = _read_relay(plug, gen, poll=poll)
        with _fault_lock, _state_lock:
            message = _adopt_relay_locked(actual, time.time())
    if message:
        _log(message)


def _manual_off(plug, gen=None):
    """The one-time switch-off on entering manual mode.

    Runs before the sensor read, so a Protect outage cannot block a manual
    command, and the flag is cleared only once the plug has confirmed the
    switch - otherwise a failed call silently dropped the request forever.
    """
    with _act_lock:
        if _stale_client(gen):
            # The plug address changed after this cycle built its client.
            # The promise is NOT discharged: the flag stays set and the
            # next cycle owes the same stop through the right plug.
            return
        if CFG["control"].get("mode") != "manual":
            # Mirror of the guard in _force_on. The pending flag is left
            # set on purpose: if manual is entered again the one-time
            # stop is still owed.
            return
        _manual_off_locked(plug)


def _manual_off_locked(plug):
    """The body of _manual_off, with _act_lock already held."""
    global _relay_retry_clock
    with _state_lock:
        pending = STATE.get("manual_off_pending")
        fault = STATE["fault"]
    if not pending:
        return
    if fault:
        # Fault enforcement owns any outstanding stop. Never let the
        # one-time Manual promise bypass its bounded budget or spacing.
        return
    try:
        actual = plug.state()
        _record_relay_observation(actual)
        already_off = actual is False
    except PlugError:
        actual = None
        already_off = False          # unknown: switch anyway
    if not already_off:
        try:
            plug.set(False)
        except PlugError:
            # A healthy running wattage cannot diagnose a rejected OFF.
            # Latch the failed stop independently of power-monitor settings;
            # unknown connectivity is NOT proof of mechanical contact failure.
            now = time.time()
            msg = "%s Manual OFF unconfirmed — relay %s" % (
                FC_MANUAL_STOP, "ON" if actual is True else "unknown")
            _store_fault(dict(at=now, msg=msg, code=FC_MANUAL_STOP,
                stopped=False, attempted_at=None, delivered_at=None,
                relay_retry={"attempts": 0, "phase": "first"}))
            _relay_retry_clock = (now, 0, time.monotonic()+30)
            _log(msg)
            raise  # Keep the original stop promise and diagnostic error.
    _remember_relay(plug, False)
    with _state_lock:
        STATE["manual_off_pending"] = False
        STATE["fan"] = False
        _stamp_elapsed_locked("changed_at")
        # tick() returns before the control decision in manual mode,
        # so this is the only place that can end this run.
        _end_run_locked()
    # Only now: the plug has confirmed, so the promise is discharged.
    clear_manual_off_promise()
    if not already_off:
        _log("Fan off \u2014 manual",
             absorb="Mode: %s" % MODE_LABELS.get("manual", "manual"),
             merged="Mode: Manual \u2014 fan off")

def _force_on(plug, gen=None, poll=False):
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
        if _stale_client(gen):
            # Re-asserting through a plug that is no longer configured
            # would switch on someone else's device. Skipped, not
            # deferred: this is re-asserted every cycle anyway.
            return
        if CFG["control"].get("mode") != "on":
            # Re-checked under the lock, not just by the caller. Manual's
            # stop is one-shot: if it has already run and cleared its
            # flag, switching on here would leave the fan going with
            # nothing left to correct it.
            return
        read_error = None
        try:
            actual = _read_relay(plug, gen, poll=poll)
        except PlugError as error:
            actual, read_error = None, error
        if actual is True:
            _record_relay_observation(True)  # No lease or event for an unchanged relay.
            return
        with _state_lock:
            was_on = STATE["fan"] is True
        _record_relay_observation(actual)
        with _state_lock:
            if actual is False:
                STATE["fan"] = False
                # The physical run ended, even though the fan is about
                # to be forced back on. What follows is a NEW run and
                # is not driven by the rules at all, so carrying the
                # old record into it would let a later Auto stop name
                # rules from before the override.
                _end_run_locked()
        if actual is False and was_on:
            # Confirmed off while we believed it on, so something else
            # switched it. Recorded before we undo it, as the automatic
            # path does - forcing it back must not swallow the evidence.
            # Only on a confirmed False: an unreadable plug is unknown,
            # not off, and reporting it as a switch invents an event.
            # THE MODE GOES IN THE LINE. A reader six months later
            # cannot know what mode the box was in at the time, and it
            # changes what the entry MEANS: in Manual someone switching
            # the fan by hand is the mode working as intended, while in
            # Auto the same line is the controller being contradicted -
            # possibly by a second controller. Same code either way, so
            # the occurrences stay countable.
            _log("%s Fan off \u2014 changed outside (%s)"
                 % (FC_EXTERNAL_CHANGE, _mode_now()))
        plug.set(True)               # raises: retried on the next cycle
        _remember_relay(plug, True)
        _record_relay_observation(True)
        now = time.time()
        with _state_lock:
            STATE["fan"] = True
            if actual is not None or not was_on:
                _stamp_elapsed_locked("changed_at", now)
                STATE["min_run_at"] = None
        # Folds into "Mode: Force on" when that is still the newest
        # line: same click, same second, and the mode alone already
        # says the fan is being held on.
        if read_error is not None:
            _log("%s Force on reasserted — relay read: %s"
                 % (FC_POLL_ERROR, _plug_issue(read_error)))
        else:
            _log("Fan on \u2014 forced on",
                 absorb="Mode: %s" % MODE_LABELS.get("on", "on"),
                 merged="Mode: Force on \u2014 fan on")


def tick():
    global _normal_relay_at
    p, h, gen = _client_snapshot()
    c = CFG["control"]
    # Before the sensor read: a manual stop must not depend on Protect,
    # and neither may the override that exists for when Protect is the
    # thing that has failed. Both actuate here, then fall through so the
    # reading is still recorded.
    if not PLUG_ACCESS_DISABLED and c["mode"] == "manual":
        _manual_off(h, gen)
    elif not PLUG_ACCESS_DISABLED and c["mode"] == "on":
        _force_on(h, gen)
    sid = CFG["protect"]["sensor_id"]
    if not sid:
        raise ProtectError("No air-quality sensor selected yet")
    vals, path = p.readings(sid)
    acquired_wall, acquired_mono = time.time(), time.monotonic()
    co2 = vals.get('co2')
    # Judged against the rules that are ENABLED, not against CO2. With
    # CO2 disabled and humidity driving the fan, a payload carrying no
    # CO2 field is perfectly usable. Every enabled rule must have a
    # reading: another metric must not hide a partial sensor outage.
    # Reject incomplete polls without refreshing the fail-safe timer,
    # releasing latches, or announcing recovery.
    live = active_rules(CFG["control"])
    if live:
        usable = all(vals.get(r["reading"]) is not None for r in live)
        why = "enabled trigger(s): %s" % ", ".join(
            r["key"] for r in live if vals.get(r["reading"]) is None)
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
    now = acquired_wall
    # BEFORE anything derived from this cycle's clients is consumed. The
    # generation used to be checked only at the actuation block below,
    # which left everything between here and there acting on a
    # superseded cycle:
    #
    #   - reading_at was refreshed from the OLD sensor, resetting the
    #     stale timer and delaying the fail-safe for a newly configured
    #     one that may be unreachable. That is a safety timer being
    #     reset by a reading that no longer applies.
    #   - _fan_status read the OLD plug's relay, and a difference from
    #     cached state was adopted as an external switch: wrong fan
    #     state, a fresh minimum-run lease, and a log line describing a
    #     device that is not ours.
    #   - the readings-returned all-clear could be sent on the strength
    #     of the old sensor answering.
    #
    # Under _act_lock because that is what the config commit holds while
    # it swaps CFG and bumps the generation; checking outside it would
    # narrow the window rather than close it.
    ext_msg = None
    recovered = False
    # ONE HOLD, covering the check, the plug read and everything derived
    # from this cycle. Checking and then releasing would leave exactly
    # the window this guards: a save landing between them and the
    # recording below still writing a superseded reading.
    #
    # Relay adoption changes fault safety state: act -> fault -> state.
    with _act_lock:
        if _stale_client(gen):
            _wake.set()
            return
        actual = _fan_status(h)
        if actual is not None:
            _normal_relay_at = (h, gen, time.time())
        _daily_pending = None
        with _fault_lock, _state_lock:
            STATE.update(co2=co2, co2_path=path, reading_at=now,
                         error=None, sensor_error=None,
                         temperature=vals.get('temperature'),
                         humidity=vals.get('humidity'),
                         pm25=vals.get('pm25'))
            _stamp_elapsed_locked("reading_at", acquired_wall, acquired_mono)
            if STATE["history"] and now < STATE["history"][-1]["t"]:
                # Inserting before the tail is not an append delta. Rotate the
                # history epoch so all existing clients request a full trace.
                STATE["history_epoch"] = os.urandom(8).hex()
            STATE["history"].append({"t": now, "v": co2,
                                     "tc": vals.get('temperature'),
                                     "rh": vals.get('humidity'),
                                     "pm": vals.get('pm25')})
            STATE["history"][:] = prune_hist(STATE["history"], now)
            _daily_pending = (co2, vals.get('temperature'),
                              vals.get('humidity'))
            STATE["history_rev"] += 1
            hsnap = list(STATE["history"])
            ext_msg = _adopt_relay_locked(actual, now)
    # Recovery is an edge too, and only announced if the stale alert
    # actually went out - so a blip that never alerted stays silent both
    # ways. Dispatched OUTSIDE the locks, and only once this cycle has
    # been accepted: a discarded cycle must not announce that readings
    # have returned on the strength of the old sensor answering.
    global _stale_delivered, _stale_inflight, _stale_episode
    with _state_lock:
        recovered = _stale_delivered
        _stale_episode = None
        _stale_delivered = False
        _stale_inflight = False
    if recovered:
        # LOGGED AS WELL AS MAILED. The recovery was mailed and never
        # written down, so events.json showed a fail-safe starting and
        # nothing ending it - and a search for the all-clear found
        # nothing at all.
        _log("%s Readings returned \u2014 normal control resumed"
             % FC_SENSOR_RETURNED)
        _notify("%s Sensor readings have returned" % FC_SENSOR_RETURNED,
                _alert_body(
                    FC_SENSOR_RETURNED, "Sensor Readings Restored",
                    time.time(),
                    "Readings are arriving again and normal control has "
                    "resumed.\nThis is the all-clear for the earlier "
                    "sensor alert.",
                    [("Action taken", "Normal control resumed"),
                     ("Manual action", "No"),
                     ("Controller mode", _mode_now())]))
    # Outside _state_lock: the daily fold takes its own lock and must
    # not extend a hold a reader is waiting on.
    if _daily_pending:
        # NOTHING IN THE SUMMARY MAY ABORT A POLL. This runs after
        # reading_at has been refreshed, so an exception here marks the
        # poll failed, skips the control decision, AND leaves the
        # stale-reading fail-safe looking at a fresh timestamp - the fan
        # would sit off with nothing left to notice. A low-and-high
        # record for a chart is not worth that.
        try:
            _daily_note(now, *_daily_pending)
        except Exception as e:      # pragma: no cover - must never escape
            reset_daily()
            _log("%s Daily summary reset after an error: %s: %s"
                 % (FC_DISK_FAILED, type(e).__name__, e))
    # Persisted on the SAME cadence as history, not every poll: the
    # summary changes on every reading, and writing it each time would
    # be a file rewrite every 30 seconds on an SD card to save data
    # that is regenerated from the next reading anyway. maybe_save_hist
    # returns true only when it actually wrote.
    if maybe_save_hist(hsnap):
        save_daily()
    if ext_msg:
        _log(ext_msg)
    if PLUG_ACCESS_DISABLED or c["mode"] == "manual":
        return
    activity = None
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
        # The settings are current; the PLUG may not be. If anything was
        # saved while the Protect read was in flight, h may address a
        # plug that is no longer ours. Discard the cycle rather than
        # command it, and wake the loop so the replacement runs at once.
        if _stale_client(gen):
            _wake.set()
            return
        # A COPY, COMMITTED ONLY ONCE THE RELAY HAS AGREED. decide()
        # mutates what it is given, and it is called BEFORE h.set().
        # Handing it the live record meant a switch-on that then failed
        # left rules recorded for a fan that never started, and a
        # failed switch-off cleared the record of a run still going -
        # so the eventual stop line named the wrong run, or no run.
        # Nothing here changes the relay; it changes whether the
        # activity log tells the truth about it.
        # The independent relay loop may have observed an off/on cycle
        # while history was being saved. Decide from the CURRENT run's
        # state and lease, not values captured before that observation.
        with _state_lock:
            if STATE["fault"] is not None:
                return  # Fault-stop enforcement alone owns OFF commands and retry budgets.
            fan_on = STATE["fan"]
            changed = STATE["changed_at"]
            lease_at = STATE["min_run_at"] if STATE["min_run_at"] is not None else changed
            lease_key = "min_run_at" if STATE["min_run_at"] is not None else "changed_at"
            ran_for = _elapsed_age_locked(lease_key) if lease_at is not None else None
            ran_snapshot = dict(STATE["ran_with"])
            before_latch = dict(STATE["latch"])
            want, why = decide(co2, vals.get('temperature'), fan_on, cur,
                               ran_for, STATE["latch"], STATE["fault"],
                               humidity=vals.get('humidity'),
                               ran_with=ran_snapshot)
            deadline = STATE["cold_start_hold_until"]
            if deadline is not None:
                if STATE["fault"] or fan_on is not True:
                    STATE["cold_start_hold_until"] = None
                elif any(STATE["latch"].values()):
                    # Fresh demand takes over with the configured normal
                    # minimum run; this is not a restored pre-cut timer.
                    STATE["cold_start_hold_until"] = None
                    _stamp_elapsed_locked("min_run_at")
                elif time.monotonic() < deadline:
                    if want is False:
                        want, why = True, "cold-start hold"
                else:
                    readings = {"co2": co2, "temp": vals.get('temperature'),
                                "humidity": vals.get('humidity')}
                    if want is False and any(readings[r["key"]] is None
                                              for r in active_rules(cur)):
                        # Missing measurements are not proof demand cleared.
                        want, why = None, None
                    else:
                        STATE["cold_start_hold_until"] = None
        if want is None or want == fan_on:
            # No command needed, so the fan is already in the state the
            # record describes. This is the path most cycles take, and
            # the one that accumulates a rule latching mid-run.
            with _state_lock:
                STATE["ran_with"] = ran_snapshot
                if fan_on is True and want is True:
                    readings = {"co2": co2, "temp": vals.get('temperature'),
                                "humidity": vals.get('humidity')}
                    joined = [r["fmt"](readings[r["key"]])
                              for r in RULES
                              if STATE["latch"].get(r["key"])
                              and not before_latch.get(r["key"])
                              and readings.get(r["key"]) is not None]
                    if joined:
                        activity = "Fan on \u2014 " + " and ".join(joined)
        else:
            h.set(want)
            _remember_relay(h, want)
            with _state_lock:
                STATE["fan"] = want
                _stamp_elapsed_locked("changed_at")
                STATE["min_run_at"] = None
                # Only after confirmed actuation: failures keep the run.
                STATE["ran_with"] = ran_snapshot
            activity = "Fan %s \u2014 %s" % ("on" if want else "off", why)
    # Events describe transitions, not every poll; logging stays outside
    # the actuation/state locks and never refreshes the minimum-run lease.
    if activity:
        _log(activity)



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
    # "Never read anything" is measured from startup, not treated as fresh:
    # otherwise a service that never got a first reading stayed passive.
    with _state_lock:
        age = _elapsed_age_locked("reading_at", startup=True)
    if age < mins * 60:
        return
    # Ask the plug rather than trusting cached state: it may have rebooted
    # off, or been switched off while Protect was unreachable.
    _, h, gen = _client_snapshot()
    switch_error = None
    # Re-checked under _act_lock: the plug read below can take up to its
    # 12s timeout, and a mode change landing in that window could leave
    # this switching the fan on just after Manual ran its one-time stop -
    # which is never repeated, so the fan would stay on in Manual.
    # stale_minutes and the deadline are re-read here too, not just the
    # mode: setting it to 0 disables the fail-safe, and a save doing that
    # while this call sits in the plug read must not still ventilate.
    with _act_lock:
        # Same reasoning as tick(): a save during the plug read can leave
        # h pointing at a plug that is no longer configured.
        if _stale_client(gen):
            return
        cur = CFG["control"]
        mins = int(cur.get("stale_minutes") or 0)
        if not mins or cur["mode"] != "auto":
            return
        with _state_lock:
            if STATE["fault"]:
                return
            age = _elapsed_age_locked("reading_at", startup=True)
        if age < mins * 60:
            return
        already_on = False
        actual = None
        with _state_lock:
            was_on = STATE["fan"] is True
        try:
            actual = h.state()
            _record_relay_observation(actual)
            already_on = actual is True
        except PlugError:
            pass                 # unreadable: assert ON anyway
        if not already_on:
            try:
                h.set(True)
            except PlugError as exc:
                switch_error = exc
        if switch_error is None:
            _remember_relay(h, True)
        # Recorded either way. When the plug was ALREADY on this used to
        # return without touching STATE, so the relay could be on while
        # /api/state and the dashboard both said off - and the draw
        # check, which only runs when STATE says the fan is on, stayed
        # asleep over a running fan.
        with _state_lock:
            if switch_error is None and (actual is False or not was_on):
                STATE["fan"] = True
                _stamp_elapsed_locked("changed_at")
                STATE["min_run_at"] = None
    if not already_on and switch_error is None:
        _log("%s Fan on \u2014 no reading for %s"
             % (FC_SENSOR_COMMS, _plural(mins, "min")))
    # OUTSIDE the already_on check, deliberately. This alert is about the
    # SENSOR having died, not about the plug being switched. Sitting it
    # under the switch meant readings stopping while the fan happened to
    # be running sent nothing at all - and that is the COMMON case, since
    # high CO2 starts the fan and the fan is then on when the sensor goes
    # quiet. Found in review, not by the tests, which is why there is now
    # a test for exactly this.
    _notify_stale(mins, already_on, fan_confirmed=switch_error is None)
    if switch_error is not None:
        # Preserve the existing failure log/backoff, but only AFTER queuing
        # the outage alert. Never claim ventilation or renew start timing.
        raise switch_error



# The fail-safe re-asserts every cycle while readings stay stale, so it
# has no natural edge. These give it one. _delivered is the one that
# gates the all-clear, and it is only ever set from the sending thread
# once the relay has accepted the message.
_stale_delivered = False
_stale_inflight = False
_stale_episode = None

SMTP_TIMEOUT = 20
EMAIL_TEST_SECONDS = 30
_email_test_lock = threading.Lock()
_email_test_pending = None
# Long enough that a slow relay still delivers, short enough that a
# wedged one cannot pile threads up: at one alert per fault transition
# there is no realistic way to accumulate them.


def _split_recipients(raw):
    """Split the stored recipient string. Pure; takes no config."""
    return [a for a in (raw or "").replace(",", " ").split() if a]


def _recipients():
    """The recipient list as currently configured.

    For callers that want the live value - the readiness check and the
    test-send endpoint. _send_mail deliberately does NOT use this: it
    splits its own snapshot, so one send cannot straddle a save.
    """
    return _split_recipients(CFG["email"].get("recipients"))


def _email_ready():
    """True when there is enough configuration to attempt a send."""
    e = CFG["email"]
    return bool(e.get("enabled") and e.get("host") and e.get("sender")
                and _recipients())


def _send_mail(subject, body, email_cfg=None, deadline=None):
    """Send one message. Returns None on success or a reason string.

    Runs on a throwaway thread, never on the control path. AUTH is chosen
    HERE and deliberately, not left to smtplib.

    smtplib.login() looks like it picks the strongest mechanism the
    server offers. It does not. It iterates CRAM-MD5, PLAIN, LOGIN,
    catching SMTPAuthenticationError and CONTINUING - so a CRAM-MD5
    attempt that fails for any reason, a mistyped password included,
    silently retries as PLAIN. With no STARTTLS that puts the password
    on the wire base64-encoded, and the first hop here is the IoT VLAN.
    An earlier version of this comment asserted login() could not do
    that. It was wrong; a review caught it.

    So: use STARTTLS when the relay offers it, and otherwise require
    CRAM-MD5 and REFUSE rather than degrade. A refused send is a logged
    failure. A downgraded one is a leaked credential that looks like
    success.
    """
    # ONE snapshot, taken under the lock that guards writes. Reading
    # CFG["email"] here and calling _recipients() - which reads CFG
    # again - meant a save landing between them could send to the new
    # recipients through the old relay, or authenticate with a password
    # that had just been cleared by a host change.
    with _cfg_lock:
        e = copy.deepcopy(CFG["email"] if email_cfg is None else email_cfg)
    to = _split_recipients(e.get("recipients"))
    if not to:
        return "no recipients configured"
    def expired():
        return deadline is not None and (_stopping.is_set()
                                        or time.monotonic() >= deadline)
    def budget(s=None):
        if expired():
            raise TimeoutError('Email test deadline reached')
        sock = getattr(s, 'sock', None)
        if deadline is not None and sock is not None:
            sock.settimeout(max(0.001, min(SMTP_TIMEOUT, deadline-time.monotonic())))
    try:
        # Validate raw recipients before splitting, which would hide newlines.
        if any(ch in value for value in (e["sender"], e.get("recipients", ""), subject)
               for ch in "\r\n"):
            raise ValueError("Header contains a line break")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = e["sender"], ", ".join(to), subject
        msg.set_content(body, charset="utf-8")
        payload = msg.as_bytes(policy=SMTP_POLICY)
    except Exception as exc:
        return "Invalid email message headers or content (%s)" % type(exc).__name__
    try:
        budget()
        s = smtplib.SMTP(e["host"], int(e["port"] or 25),
                         timeout=SMTP_TIMEOUT if deadline is None else
                         max(0.001, min(SMTP_TIMEOUT, deadline-time.monotonic())))
    except Exception as exc:
        return "connect failed (%s)" % type(exc).__name__
    try:
        budget(s)  # A DNS result arriving after the test expired cannot send.
        s.ehlo()
        budget(s)
        secure = False
        if s.has_extn("starttls"):
            # Not exercised against this relay, which offers none. Here
            # so that repointing at one that does is an upgrade rather
            # than a silent continuation in clear.
            #
            # THE CONTEXT IS NOT OPTIONAL. starttls() with context=None
            # builds ssl._create_stdlib_context(), which has
            # check_hostname False and verify_mode CERT_NONE - encrypted
            # against a passive listener and useless against an active
            # one. Anything that could redirect port 25 could present
            # its own certificate and collect the password, which is the
            # exact outcome the CRAM-MD5 branch below exists to prevent.
            s.starttls(context=ssl.create_default_context())
            budget(s)
            s.ehlo()
            budget(s)
            secure = True
        if e.get("username"):
            if secure:
                s.login(e["username"], e["password"])
            else:
                mechs = (s.esmtp_features.get("auth") or "").upper().split()
                if "CRAM-MD5" not in mechs:
                    return ("refusing to authenticate: the relay offers "
                            "neither STARTTLS nor CRAM-MD5, so the "
                            "password would cross the network in clear.")
                # Called directly rather than through login(), which
                # falls back to PLAIN when this fails.
                s.user = e["username"]
                s.password = e["password"]
                s.auth("CRAM-MD5", s.auth_cram_md5)
        budget(s)
        refused = s.sendmail(e["sender"], to, payload)
        if refused:
            # smtplib returns normally when SOME recipients accepted the
            # message. Do not mark the whole alert delivered, or expose
            # recipient addresses and arbitrary relay replies in errors.
            return ("Partial delivery: relay accepted %d of %d recipients; "
                    "%d rejected. Accepted recipients may already have "
                    "received the message."
                    % (len(to) - len(refused), len(to), len(refused)))
        return None
    except Exception as exc:
        # Relay text may echo credentials from this send's old snapshot.
        # Never expose it, even after current settings have changed.
        return "SMTP send failed (%s)" % type(exc).__name__
    finally:
        try:
            if expired():
                s.close()
            else:
                budget(s)
                s.quit()
        except Exception:
            try:
                s.close()
            except Exception:
                pass


def _notify(subject, body, on_complete=None):
    """Queue a notification. Returns immediately; never raises.

    on_complete(ok) IS ALWAYS CALLED EXACTLY ONCE, whatever happens:
    with True from the sending thread if the relay accepted the message
    for every configured recipient, and with False if any were rejected,
    if alerts are switched off,
    or if the thread could not even be started. Callers rely on that to
    clear an in-flight flag, and a path that returns without calling it
    leaves that flag set for the life of the process - which silences
    the alert it was guarding.

    An earlier version checked readiness in a wrapper and again in here.
    Alerts being switched off between the two checks meant no worker ran
    and nothing called back. The check belongs in ONE place, and that
    place also owns the callback.

    Bookkeeping belongs in there, not at the call site. _notify returns
    as soon as the thread starts, so anything written after the call
    runs while the send is still in flight: a flag cleared there is
    cleared too early and lets a second send queue behind the first, and
    a flag set there records a delivery that may never happen.

    Called from the draw thread and the poll thread, sometimes with a
    fault just written. Nothing here may block or throw: the fan being
    stopped matters more than the mail going out, and an exception
    escaping into _store_fault's caller would leave the fault path
    half-run.
    """
    def complete(ok):
        """Report completion once, and never let it escape.

        A callback that raised used to be caught by the queueing handler
        below, which then reported completion AGAIN - so the contract
        two paragraphs up was false in exactly the case it mattered.
        Bookkeeping failures are logged and dropped here instead.
        """
        if not on_complete:
            return
        try:
            on_complete(ok)
        except Exception as exc:
            try:
                _log("%s Alert bookkeeping failed: %s"
                     % (FC_ALERT_FAILED, type(exc).__name__))
            except Exception:
                pass

    try:
        if not _email_ready():
            complete(False)
            return

        def run():
            # _send_mail reports failures by returning a reason rather
            # than raising, so this guard is redundant today. It is here
            # because a daemon thread that DOES raise prints a traceback
            # to stderr and dies silently otherwise, and the next edit
            # to _send_mail should not be able to cause that.
            try:
                why = _send_mail(subject, body)
            except Exception as exc:
                why = "SMTP send failed (%s)" % type(exc).__name__
            if why:
                # Redacted: a relay's rejection can quote what it was
                # given, and the activity log is served without a
                # password.
                _log(_redact("%s Alert email failed (%s): %s"
                             % (FC_ALERT_FAILED, subject, why)))
            complete(why is None)
        threading.Thread(target=run, daemon=True,
                         name="notify").start()
    except Exception as exc:            # pragma: no cover - belt and braces
        # Thread creation can fail. Reporting completion here as well
        # means an in-flight flag is released even then. complete()
        # swallows, so this cannot become a second call for a callback
        # that already raised above.
        complete(False)
        try:
            _log("%s Alert email could not be queued: %s"
                 % (FC_ALERT_FAILED, type(exc).__name__))
        except Exception:
            pass


def _notify_stale(mins, already_on, fan_confirmed=True):
    """Say once that readings have stopped. Not about the plug.

    Fires whether or not the fail-safe had to switch anything: the news
    is that the sensor is silent and automatic control is running blind.

    TWO FLAGS, NOT ONE. _stale_inflight stops the fail-safe queueing a
    fresh send every poll; _stale_delivered records that one actually
    arrived, and ONLY that gates the all-clear. Recording delivery
    optimistically meant a failed warning still produced a recovery
    email later - an all-clear for something nobody was told about.
    A send that fails is retried on the next fail-safe cycle, which is
    the behaviour you want during an outage.

    Completion updates only its own outage episode under the state lock.
    An accepted reading closes that episode, even if delivery is still
    pending. Late completion cannot mark a new outage delivered or clear
    its in-flight send. Failed sends retry within the same episode.
    """
    global _stale_inflight, _stale_episode
    with _state_lock:
        if _stale_delivered or _stale_inflight:
            return
        if _stale_episode is None:
            _stale_episode = object()
        episode = _stale_episode
        _stale_inflight = True

    def completed(ok):
        global _stale_delivered, _stale_inflight
        with _state_lock:
            if _stale_episode is not episode:
                return
            if ok:
                _stale_delivered = True
            _stale_inflight = False
    _notify("%s Sensor readings stopped - %s"
            % (FC_SENSOR_COMMS, "fan on as a precaution" if fan_confirmed else "fan ON unconfirmed"),
            _alert_body(
                FC_SENSOR_COMMS, "Sensor Communications Fault",
                time.time(),
                "No valid reading for %s, so automatic control "
                "is running blind.\n%s\nNormal control resumes on its "
                "own once readings come back, and there will be an "
                "all-clear when it does."
                % (_plural(mins, "minute"),
                   "The fail-safe ON command failed. Ventilation is NOT confirmed; "
                   "the fan may still be off. The controller will retry while "
                   "the outage remains eligible for fail-safe ventilation."
                   if not fan_confirmed else
                   "The fan was already running and will stay on."
                   if already_on else
                   "The fan has been switched on as a precaution and "
                   "will stay on until readings return."),
                [("Action taken", "Fail-safe ventilation" if fan_confirmed else
                  "Fail-safe ON attempted, NOT confirmed"),
                 ("Manual action", "No" if fan_confirmed else "Yes - check plug and fan"),
                 ("Controller mode", _mode_now())],
                ["Sensor offline or unpowered",
                 "UniFi Protect unavailable",
                 "Network connectivity",
                 "API authentication failure or schema change"]),
            on_complete=completed)


_unsaved_notified = False
# WHICH EPISODE was delivered, not merely that one was. None means no
# FC-005 has been delivered for the fault currently standing.
#
# A bare boolean let a late callback from fault A authorise an FC-102
# for fault B: A's send completes after A is acknowledged, the flag
# stays true, B's send then FAILS, and B's falling edge reports "saved
# after all" for a warning nobody received. Reported by review against
# 3b30c6c, reproduced. The fault's "at" is the identity - it never
# changes for the life of a fault, and _mark_fault_delivered already
# uses it the same way.
_unsaved_delivered = None


def _fault_stop_status(fault, mode, fan):
    """Describe the observed stop, never infer success from a standing fault."""
    if mode == "on":
        return ("Force on overrides fault-stop enforcement; the fan may run "
                "while this fault remains active.", "Force on override active")
    if PLUG_ACCESS_DISABLED:
        return ("Plug access is disabled; no stop command can be sent.",
                "Stop not enforced: plug access disabled")
    if fault.get("stopped") is True and fan is False:
        return ("The relay was confirmed off at the last check.",
                "Fan stop confirmed at last check")
    return ("A fan stop is requested but is not currently confirmed. "
            "The fan may still be running.", "Fan stop requested, not confirmed")


def _unsaved_edge():
    """Notify once when fault_unsaved rises. CALL OUTSIDE ALL LOCKS.

    Three sites raise this flag and four clear it, so instead of an
    edge check at each, this reads the live value and keeps its own
    memory of what it last announced. Clearing resets it, so a second
    write failure later does send again - which is right: each one is a
    fresh window where a restart would lose the fault.

    The fault is held in memory, but a requested stop need not have
    succeeded. What is at risk is that fault surviving a restart.
    """
    global _unsaved_notified, _unsaved_delivered
    with _state_lock:
        now = bool(STATE["fault_unsaved"])
        # Copied, not referenced: an acknowledgement landing between
        # here and the send would otherwise leave the message naming a
        # fault that no longer exists.
        standing = dict(STATE["fault"]) if STATE["fault"] else {}
        fan = STATE["fan"]
        # READ UNDER THE LOCK THAT WRITES IT, so the flag has exactly
        # one owner. The callback's assignment was moved inside this
        # lock; leaving the read outside it kept a narrower version of
        # the same race, where a delivery landing mid-decision changes
        # whether an all-clear goes out.
        delivered = _unsaved_delivered
    mode = CFG["control"]["mode"]
    stop_text, stop_action = _fault_stop_status(standing, mode, fan)
    if not now:
        # FALLING EDGE. Two ways to get here and they are not the same
        # news: the retry finally wrote the file, or the whole fault was
        # acknowledged and the flag went with it. Only the first is an
        # all-clear for FC-005 - the second is FC-101's story, and
        # sending both would report one event twice.
        if (delivered is not None and standing
                and standing.get("at") == delivered):
            _notify("%s Fan fault has been saved to disk"
                    % FC_FAULT_PERSISTED,
                    _alert_body(
                        FC_FAULT_PERSISTED, "Fault Persisted After All",
                        time.time(),
                        "The fault could not be written earlier, but the "
                        "retry has now succeeded.\nIt will survive a "
                        "restart. The fault itself still stands.\n" + stop_text,
                        [("Standing fault", standing.get("code")
                          or "unknown"),
                         ("Action taken", "Fault written to disk"),
                         # NOT "No". The persistence needs nothing from
                         # anyone - that is what this message is about -
                         # but the fault still stands. A reader skimming
                         # the fields would
                         # otherwise conclude nothing was required of
                         # them, and the How to clear line below
                         # contradicts it outright.
                         ("Manual action",
                          "Yes - the fault still stands"),
                         ("How to clear", "Acknowledge in the dashboard"),
                         ("Controller mode", _mode_now())]))
        _unsaved_notified = False
        with _state_lock:
            _unsaved_delivered = None
        return
    if _unsaved_notified:
        return
    _unsaved_notified = True

    episode = standing.get("at")

    def _unsaved_sent(ok):
        # Checked against the fault standing NOW, not the one captured
        # when the send began. A callback that outlives its own episode
        # must not vouch for the next one.
        # THE ASSIGNMENT IS INSIDE THE LOCK, not merely the check.
        # Validating under the lock and then writing outside it left a
        # window where two callbacks could interleave and an older one
        # overwrite the current episode - suppressing a legitimate
        # FC-102. Found by review 2026-08-16.
        global _unsaved_delivered
        if not ok:
            return
        with _state_lock:
            cur = STATE["fault"]
            if (STATE["fault_unsaved"] and cur
                    and cur.get("at") == episode):
                _unsaved_delivered = episode
    _notify("%s Fan fault could not be saved to disk" % FC_FAULT_UNSAVED,
            _alert_body(
                FC_FAULT_UNSAVED, "Fault Not Persisted",
                time.time(),
                "A fault is active but could not be written to fault.json.\n"
                + stop_text + "\nAutomatic starts are blocked while the "
                "fault stands; Force on overrides stop enforcement.\n"
                "The risk is a restart: the fault would be "
                "forgotten and normal control would resume as though "
                "nothing were wrong.\nThe service retries the write on "
                "its own.",
                [("Standing fault", standing.get("code") or "unknown"),
                 ("Action taken", stop_action + "; fault held in memory only"),
                 ("Manual action", "Yes"),
                 ("How to clear", "Fix the disk; the write retries "
                                  "itself"),
                 ("Controller mode", _mode_now())],
                ["Disk full",
                 "Filesystem mounted read-only",
                 "Permissions changed on the install directory"]),
            on_complete=_unsaved_sent)


def _mark_fault_attempted(expected_at):
    """Stamp attempted_at on the fault, if it is still that fault.

    The restart notice used to snapshot the fault, then call
    _store_fault to record the attempt. An acknowledgement landing
    between those two steps was undone - _store_fault wrote the stale
    copy back over a fault the operator had just cleared.

    Same shape as _mark_fault_delivered and for the same reason: hold
    _fault_lock across the check AND the write, and match on "at".
    Returns False if the fault has gone or been replaced, in which case
    the caller must not send.
    """
    with _fault_lock:
        with _state_lock:
            cur = dict(STATE["fault"]) if STATE["fault"] else None
        if not cur or cur.get("at") != expected_at:
            return False
        cur["attempted_at"] = time.time()
        with _state_lock:
            if not STATE["fault"] or STATE["fault"].get("at") != expected_at:
                return False
            STATE["fault"] = cur
        _save_annotation(cur)
    _unsaved_edge()
    return True


_fault_annotation_dirty = None


def _save_annotation(fault):
    """Persist a fault whose SAFETY content has not changed.

    CALLERS MUST HOLD _fault_lock. Only the alert bookkeeping -
    attempted_at, delivered_at, relay_notice delivery status - differs from disk,
    so a write failure here must NOT raise fault_unsaved: that flag
    means "this fault would be lost on restart", it produces an alarming
    alert of its own, and it blocks requested restarts. Losing a
    delivery annotation can cause a duplicate notice after restart. Failed
    annotation writes are retried independently of the safety flag.

    A fault that was ALREADY unsaved stays unsaved: the previous value
    is restored rather than cleared, so this cannot mask a real problem.
    """
    global _fault_annotation_dirty
    with _state_lock:
        was_unsaved = STATE["fault_unsaved"]
    try:
        save_fault(fault)
        with _state_lock:
            STATE["fault_unsaved"] = False
        _fault_annotation_dirty = None
        return True
    except OSError:
        _fault_annotation_dirty = fault["at"]
        with _state_lock:
            STATE["fault_unsaved"] = was_unsaved
        return False


def _retry_fault_annotation():
    """Repair notification writes using the latest record, not a stale copy."""
    global _fault_annotation_dirty
    with _fault_lock:
        with _state_lock:
            current = copy.deepcopy(STATE["fault"])
        if _fault_annotation_dirty is None:
            return False
        if not current or current["at"] != _fault_annotation_dirty:
            _fault_annotation_dirty = None
            return False
        saved = _save_annotation(current)
    _unsaved_edge()
    return saved


def _mark_fault_delivered(expected_at):
    """Record that the alert for THIS fault reached the relay.

    Runs on a sending thread, long after the fault was raised. Two
    things can have happened in between, and both were possible before
    this checked:

      - the operator ACKNOWLEDGED the fault. Snapshotting it, releasing
        the lock and storing the copy afterwards wrote the cleared fault
        back to memory and disk. The fan then stayed blocked on a fault
        the dashboard said was gone.
      - a DIFFERENT fault replaced it. Without an identity check the
        callback stamped the new one as delivered, so its own restart
        notice was suppressed for an hour.

    "at" is the identity: it is set once when the fault is raised and
    never rewritten.

    HOLDS _fault_lock ACROSS THE CHECK AND THE WRITE, and therefore
    cannot call _store_fault, which takes the same non-reentrant lock.
    The body below is _store_fault's inner half, deliberately duplicated
    rather than refactored: this path must not become a second way to
    SET a fault, only to annotate the one already there.
    """
    with _fault_lock:
        with _state_lock:
            cur = dict(STATE["fault"]) if STATE["fault"] else None
        if not cur or cur.get("at") != expected_at:
            return                  # acknowledged, or replaced
        cur["delivered_at"] = time.time()
        with _state_lock:
            if not STATE["fault"] or STATE["fault"].get("at") != expected_at:
                return              # changed while we were copying
            STATE["fault"] = cur
        _save_annotation(cur)
    _unsaved_edge()


RESTART_NOTICE_SUPPRESS = 3600


def _notify_restart_fault():
    """Announce a fault that survived a restart. Never raises.

    RESTARTING DOES NOT CLEAR A DRAW FAULT - the dashboard
    acknowledgement does - so a fault still standing after a restart
    means either someone tried the wrong remedy, or the service is
    looping under one. Both are worth saying.

    Rate limited on delivered_at, which is stamped only once the relay
    has accepted a message about this fault; attempted_at records the
    try. Suppressing on the attempt meant a FAILED send bought an hour
    of silence, at the moment mail was already known to be broken. The
    unit
    is Restart=on-failure with RestartSec=10, and systemd's start limit
    (5 in 10s) does NOT catch a loop spaced that widely - so without
    this a crash-looping service would mail about six times a minute,
    which is how an alert address gets muted before the next real fault.

    CONSEQUENCE, DELIBERATE: a genuine restart within the suppression
    window sends nothing. Whoever restarted has almost certainly just
    read the original alert.
    """
    try:
        with _state_lock:
            f = dict(STATE["fault"]) if STATE["fault"] else None
            fan = STATE["fan"]
        if not f:
            return
        # Checked BEFORE anything is stamped. _mark_fault_attempted
        # WRITES fault.json, so leaving this until _notify meant every
        # restart with a standing fault wrote and fsynced the file even
        # with alerts switched off - recording an attempt that never
        # happened, and on a Pi in a crash loop, writing to the SD card
        # every ten seconds for nothing. _notify checks again, because
        # the configuration can change between here and there.
        if not _email_ready():
            return
        # Suppress on DELIVERY, not on the attempt. Rate limiting on
        # the attempt meant a send that failed still bought an hour of
        # silence, so a fault that outlived a restart could go
        # unreported precisely because the mail was broken.
        if time.time() - (f.get("delivered_at") or 0) < RESTART_NOTICE_SUPPRESS:
            return
        expected_at = f.get("at")
        # Guarded: an acknowledgement landing between the read above and
        # this write would otherwise be undone by storing the stale copy.
        # If it has gone, there is nothing to notify about either.
        if not _mark_fault_attempted(expected_at):
            return
        mode = CFG["control"]["mode"]
        stop_text, stop_action = _fault_stop_status(f, mode, fan)

        def completed(ok):
            if ok:
                _mark_fault_delivered(expected_at)
        _log("%s Fault still standing after restart \u2014 %s"
             % (FC_RESTART_FAULT, f.get("code") or "unknown"))
        _notify("%s Fan fault still standing after a restart"
                % FC_RESTART_FAULT,
                _alert_body(
                    FC_RESTART_FAULT, "Fault Survived a Restart",
                    time.time(),
                    "%s\nThe service has restarted and the fault is "
                    "still there.\nRESTARTING DOES NOT CLEAR A DRAW "
                    "FAULT. It is written to disk and reloaded on "
                    "purpose; automatic starts stay blocked until "
                    "acknowledged. Force on overrides stop enforcement.\n"
                    "%s\nIf this arrives repeatedly the "
                    "service is restarting in a loop, which is a "
                    "separate problem from the fault itself."
                    % (_without_code(f.get("msg")), stop_text),
                    [("Standing fault", f.get("code") or "unknown"),
                     ("Action taken", stop_action),
                     ("Manual action", "Yes"),
                     ("How to clear", "Acknowledge in the dashboard"),
                     ("Controller mode", _mode_now())]),
                on_complete=completed)
    except Exception as exc:        # pragma: no cover - must never escape
        try:
            _log("%s Restart fault notice failed: %s"
                 % (FC_ALERT_FAILED, exc))
        except Exception:
            pass


# Fault codes. STABLE IDENTIFIERS, and the point of them is that they
# outlive wording: a subject line gets rewritten, FC-003 does not. They
# appear in the subject, in the body, and in the activity log, so one
# grep finds an episode across events.json and the mail archive.
#
# NEVER RENUMBER AND NEVER REUSE. A retired code stays retired; a
# recycled number silently merges two different faults in anyone's
# archive. 0xx are alarms, 1xx are all-clears, so a mail rule can split
# them without parsing subjects.
FC_SENSOR_COMMS = "FC-001"
FC_PLUG_COMMS = "FC-002"
FC_UNDER_POWER = "FC-003"
FC_OVER_POWER = "FC-004"
FC_FAULT_UNSAVED = "FC-005"
FC_RESTART_FAULT = "FC-006"
FC_OFF_DRAW = "FC-007"  # Relay reports OFF but the load still draws power.
FC_RELAY_RELEASE = "FC-008"  # Bounded OFF retries failed; Auto remains blocked.
FC_RELAY_RECOVERED = "FC-103"  # OFF and low draw confirmed; acknowledgement still required.
FC_MANUAL_STOP = "FC-009"  # Manual OFF failed; relay ON or unknown, not a welded-contact diagnosis.
_RELAY_FAULT_CODES = (FC_OFF_DRAW, FC_RELAY_RELEASE, FC_MANUAL_STOP)
FC_SENSOR_RETURNED = "FC-100"
FC_HARD_CLEARED = "FC-101"
FC_FAULT_PERSISTED = "FC-102"

# 3xx: WORTH KNOWING, NOTHING FAILED. Not an alarm and not an
# all-clear, so it sends no mail and belongs to neither series - the
# code exists because the question "how often did this happen" is worth
# being able to ask months later.
#
# FC-300 is the plug changing state without this service commanding it.
# Usually that is a person at the switch. It is also exactly what a
# SECOND CONTROLLER looks like, and this project has run two at once
# for a week at a time - so the occurrences are worth being able to
# count. The other fan-state lines stay uncoded on purpose: manual off
# and the owed stop are the system doing what it was told, and coding
# normal operation would make a code stop meaning "something
# happened".
FC_EXTERNAL_CHANGE = "FC-300"

# FC-301 is THIS PROCESS STARTING WITHOUT A HANDOVER: a systemd start, a
# crash, or the board losing power. Only the execv path wrote anything,
# so a cold start left no mark at all - and a cold start deliberately
# begins with no latch and no minimum-run lease. The activity log then
# showed a fan stopping at a reading well above off_at, seconds into a
# lease, with nothing above it to explain why. It read as the rules
# breaking. It cost a false defect report on 2026-08-18.
#
# 3xx and not 2xx: NOTHING FAILED. The controller is powered over
# Ethernet, so this is routine - a switch reboot, a switch firmware
# update, a PoE renegotiation or a power-budget event each take the
# board down and bring it back. The line exists to be read afterwards,
# not to be acted on, so it is not emailed and it is not coloured as a
# problem. See event_class.
FC_COLD_START = "FC-301"
FC_COLD_HOLD = "FC-302"  # Already-ON relay given a bounded Auto startup hold.

# 2xx: SOMETHING FAILED, NOBODY WAS EMAILED. Operational errors that
# reach the activity log and stop there.
#
# The distinction from 0xx is the whole point of a separate series: an
# FC-0xx means a message was sent and someone may already be reading
# it, while an FC-2xx means the log is the only place this exists. That
# is the thing worth knowing immediately at 2am.
#
# Grouped by SUBSYSTEM rather than one code per message. Fifteen log
# lines share these seven codes, because "which subsystem is unhappy"
# is the question people actually ask, and a code per message would be
# a lookup table nobody remembers.
FC_POLL_ERROR = "FC-200"        # unclassified: the loop caught something
FC_SENSOR_DOWN = "FC-201"       # Protect unreachable, and its recovery
FC_PLUG_DOWN = "FC-202"         # the plug will not read or will not obey
FC_ALERT_FAILED = "FC-203"      # an alert could not be sent
FC_DISK_FAILED = "FC-204"       # a write did not land
FC_RESTART_PROBLEM = "FC-205"   # a restart was refused or came back degraded
FC_STARTUP = "FC-206"           # startup conditions, DNS
# The one 2xx that means PROTECTION DID NOT HAPPEN. Separate from the
# rest because the others describe something not working; this one
# describes the fan not being ventilated when it should have been.
FC_FAILSAFE_FAILED = "FC-207"
# THE PAGE DREW NOTHING. Not a script error - the script ran and
# produced an empty chart or an empty log while the server had data to
# show. It exists because a broken page LOOKS LIKE A WORKING PAGE: the
# log-colouring regression on 2026-08-16 survived a full day, every
# test green, and was found only because somebody looked at a screen.
#
# 2xx, because nothing is emailed and the log is the only record. It
# says the DISPLAY failed, never the controller: the fan is unaffected
# and the reader should not go looking for a fault that is not there.
FC_RENDER_EMPTY = "FC-208"


def _duration(seconds):
    """A span someone reads at a glance, not a float."""
    s = int(max(0, seconds))
    if s < 90:
        return "%d seconds" % s
    if s < 5400:
        return "%d minutes" % round(s / 60.0)
    return "%.1f hours" % (s / 3600.0)


def _stamp(when):
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(when))


def _mode_now():
    """The mode as a person would say it. Never raises."""
    try:
        m = CFG["control"]["mode"]
    except Exception:
        return "unknown"
    return {"auto": "Auto", "manual": "Manual", "on": "Force on"}.get(m, m)


def _alert_body(code, title, when, summary, fields, causes=None):
    """One plain-text shape for every alert.

    Fields are a list of pairs rather than a dict so the order is the
    author's, not the interpreter's. Plain text on purpose: greppable in
    the archive and readable in any client, including the phone this
    will actually be read on.

    NO DASHBOARD URL. The site address is not repo content - the CI
    privacy check exists for exactly this - so the body says where to go
    without saying where that is.
    """
    out = ["%s  %s" % (code, title),
           time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(when)),
           "",
           summary,
           ""]
    for k, v in fields:
        out.append("%-19s%s" % (k + ":", v))
    if causes:
        out.append("")
        out.append("%-19s%s" % ("Likely causes:", causes[0]))
        for c in causes[1:]:
            out.append("%-19s%s" % ("", c))
    return "\n".join(out) + "\n"


def _plural(n, word):
    """"1 minute", not "1 minutes". Read by a person, not parsed."""
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _without_code(msg):
    """A fault message with its code prefix removed.

    The code leads every stored message and is also carried as its own
    field, so quoting the message verbatim printed it twice. Stripped
    rather than stopping the message carrying it: the ACTIVITY LOG line
    needs the code at the front, which is what the search relies on.
    """
    if not msg:
        return "A fault is standing."
    parts = msg.split(" ", 1)
    if len(parts) == 2 and parts[0].startswith("FC-"):
        return parts[1]
    return msg


def _draw_fault_code(watts, lo, hi):
    """Which code a draw fault carries.

    Mirrors _draw_fault_cause and must keep mirroring it: no reading is
    a plug problem, anything else is judged against the live band. Under
    covers "nothing is drawing at all", because a fan that never started
    and a fan running weakly are the same class of problem to whoever
    has to go and look at it.
    """
    if watts is None:
        return FC_PLUG_COMMS
    if watts > hi:
        return FC_OVER_POWER
    return FC_UNDER_POWER


def _draw_fault_cause(watts, lo, hi):
    """Explain a draw fault in terms someone can act on.

    The band is read LIVE from CFG by the caller and passed in, never
    persisted onto the fault and never parsed back out of its message.
    It is user-editable in the dashboard, so a stored copy would drift -
    the same mistake this project has made about live settings before.
    """
    if watts is None:
        return ("The plug stopped answering, so no reading was available.\n"
                "This is a plug or network problem rather than a fan "
                "problem - the fan may well still be running.")
    if watts < 1.0:
        return ("The plug is switched on but nothing is drawing power, so "
                "the fan did not start.\nLikely a failed motor, something "
                "unplugged, or the plug's relay not actually closing.")
    if watts < lo:
        return ("The fan is drawing power but less than expected, so it is "
                "running weakly or stalling.\nWorth checking for an "
                "obstruction before assuming the motor has failed.")
    return ("The fan is drawing more power than expected, which suggests a "
            "straining motor or an obstruction.\nCHECK THE BAND FIRST: if "
            "the fan was recently changed, the draw range may simply still "
            "be set for the old one, and nothing is wrong with the fan.")


WATTS_SECONDS = 5
RELAY_SECONDS = 30
WATTS_STALE = 15   # a reading older than this is reported as unknown
SETTLE_SECONDS = 20  # spin-up grace before draw is judged
OFF_SETTLE_SECONDS = 5  # allow the meter to settle after a reported stop



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
            if value is not None:
                STATE["cold_start_pending"] = False
                STATE["cold_start_hold_until"] = None
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
                warn = ("%s WARNING: fault could not be saved (%s). "
                        "Active now but would be lost on restart."
                        % (FC_FAULT_UNSAVED, e))
    # Outside _fault_lock, like the stop-confirmed log: an event append
    # must not extend a hold another writer may be waiting on. The edge
    # check goes here too, and covers CLEARING as well as raising.
    _unsaved_edge()
    if warn:
        _log(warn)
    return had


def _clear_fault():
    """Acknowledge a fault. Takes _act_lock before _fault_lock.

    The lock is not for the fault - _fault_lock covers that - it is to
    serialise against ENFORCEMENT. _enforce_stop re-checks the fault
    while holding _act_lock, so acquiring it here means an acknowledgement
    and a stop cannot interleave: either the stop runs and then the
    fault clears, or the fault clears and the stop finds nothing to
    enforce. Previously the stop could land afterwards and switch the
    fan off with the fault already gone from the screen.

    Order is _act_lock -> _fault_lock, as the draw decision established.
    Nothing that holds _fault_lock takes _act_lock, so this cannot
    invert.
    """
    with _act_lock:
        result = _clear_fault_locked()
    # Outside both locks, deliberately: see _clear_fault_locked. A
    # no-op unless the flag actually moved.
    _unsaved_edge()
    return result


def _clear_fault_locked():
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
        except InstalledNotDurable:
            # The file is gone, so the fault is no longer on disk - but
            # it is still live in memory and the removal is not proven.
            # Flagging it unsaved is what keeps a restart from exec'ing
            # away a fault that fault.json can no longer vouch for. The
            # retry loop rewrites it, and the acknowledgement has to be
            # made again once the disk is healthy.
            with _state_lock:
                STATE["fault_unsaved"] = True
            # The edge is reported by the CALLER, once both locks are
            # released. _unsaved_edge can start a mail thread and can
            # log, and doing that here held _fault_lock - and, since the
            # acknowledgement began taking _act_lock, the actuation lock
            # as well - across a disk failure, which is exactly when it
            # would be slowest.
            return had, False
        except OSError:
            return had, False
        with _state_lock:
            STATE["fault"] = None
            STATE["fault_unsaved"] = False
    return had, True


def _confirm_fault_stopped(expected_at, relay_revision):
    """Record that the plug confirmed the stop. True if this call did it.

    Requires the episode and observation revision captured after the stop.
    A newer observation or replacement fault discards the old result.
    Re-reads the fault inside the lock rather than trusting a snapshot
    taken earlier: an acknowledgement may have landed in between, and
    writing the older value back would resurrect a cleared fault.

    The disk write comes BEFORE the memory update. The other order set
    stopped=True in memory, and if the write then failed the next call
    saw stopped already true and returned early - so it was never
    retried and disk and memory disagreed for good.
    """
    report_unsaved = False
    with _fault_lock:
        with _state_lock:
            cur = STATE["fault"]
            if (
                    not cur or cur.get("at") != expected_at
                    or STATE["relay_revision"] != relay_revision
                    or STATE["fan"] is not False):
                return False  # This stop result was superseded; do not write or log it.
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
            # Reported AFTER the lock, not here. _unsaved_edge can start
            # a mail thread and can log, and doing that under
            # _fault_lock blocked acknowledgement, the retry loop and a
            # requested restart - during a disk failure, which is the
            # only way to reach this line and the moment it is slowest.
            report_unsaved = True
        else:
            with _state_lock:
                if STATE["fault"] is not None:
                    STATE["fault"]["stopped"] = True
                    STATE["fault_unsaved"] = False
                # Confirmed, so the run is over. Reached only when
                # the fault write landed: an unconfirmed stop is
                # retried, and until it lands the fan may still be
                # running on the rules recorded here.
                _end_run_locked()
    if report_unsaved:
        _unsaved_edge()
        return False
    return True


def _record_relay_observation(actual):
    """Keep confirmed observations even if the next command fails.

    A newly observed ON invalidates an earlier fault-stop confirmation;
    mark that revision for the existing durable fault retry path.
    """
    if actual is not True and actual is not False:
        return
    with _fault_lock:
        with _state_lock:
            STATE["relay_revision"] += 1
            STATE["fan"] = actual
            fault = STATE["fault"]
            if actual is True and fault and fault.get("stopped"):
                fault["stopped"] = False
                STATE["fault_unsaved"] = True


def _invalidate_fault_stop():
    with _fault_lock:
        with _state_lock:
            fault = STATE["fault"]
            if fault and fault.get("stopped"):
                fault.update(stopped=False)
                STATE["fault_unsaved"] = True


def _relay_retry_update(**changes):
    """Revise the standing relay fault under act -> fault -> state lock order.

    A consumed attempt is persisted BEFORE touching hardware. If storage
    fails, keep the budget in memory but issue no further command until
    the existing persistence retry has saved it. Never replenish a budget.
    Caller holds _act_lock.
    """
    with _fault_lock:
        with _state_lock:
            f = copy.deepcopy(STATE["fault"])
        if not f or f.get("code") not in _RELAY_FAULT_CODES:
            return None, False
        previous_retry = f.get("relay_retry")
        f.update(changes)
        if changes.get("stopped") and _relay_recovered(f):
            marker_id = _manual_stop_id()
            if marker_id is not None:
                f["manual_stop_id"] = marker_id
        retry = changes.get("relay_retry")
        if retry and retry != previous_retry:
            stage = ({"wait": "hard", "exhausted": "exhausted",
                      "recovered": "recovered"}.get(retry["phase"])
                     or ("warning" if retry["attempts"] == 1 else None))
            if stage:
                f["relay_notice"] = {"stage": stage, "delivered": False}
        try:
            save_fault(f)
            saved = True
        except OSError:
            saved = False
        with _state_lock:
            STATE["fault"] = f
            STATE["fault_unsaved"] = not saved
            if saved and f.get("stopped"):
                _end_run_locked()
                STATE["manual_off_pending"] = False
    if saved and f.get("stopped"):
        clear_manual_off_promise()
    return f, saved


def _relay_retry_deadline(f):
    """Cold starts keep the persisted budget, with a conservative new wait."""
    global _relay_retry_clock
    retry = f["relay_retry"]
    key = (f["at"], retry["attempts"])
    if _relay_retry_clock is None or _relay_retry_clock[:2] != key:
        immediate = retry["attempts"] == 0 and f["code"] != FC_MANUAL_STOP
        delay = 0 if immediate else (60 if retry["phase"] == "wait" else 30)
        _relay_retry_clock = (*key, time.monotonic() + delay)
    return _relay_retry_clock[2]


_RELAY_ACTIVITY_MESSAGES = {
    "warning": "OFF retry started",
    "hard": "First 3 OFF attempts failed — wait 60s for next 3",
    "exhausted": "All 6 OFF retries failed — hard fault",
    "recovered": "Relay reports OFF — hard fault remains",
}


def _short_relay_activity(msg):
    """Compact older verbose entries for display, without rewriting history."""
    for prefix, stage in (
        ("FC-007 OFF retry started. Three attempts are allowed,", "warning"),
        ("FC-008 The first three OFF attempts did not confirm power removal.", "hard"),
        ("FC-008 All six OFF retries are exhausted.", "exhausted"),
        ("FC-103 The relay reports OFF and fresh measured draw is within the OFF limit.", "recovered"),
    ):
        if msg.startswith(prefix):
            return msg[:6] + " " + _RELAY_ACTIVITY_MESSAGES[stage]
    return msg


RELAY_NOTICE_RETRY_SECONDS = 60
_relay_notice_inflight = set()
_relay_notice_due = {}


def _relay_retry_notice(f, stage):
    """Record the latest stage durably; failed mail never consumes OFF retries."""
    with _fault_lock:
        with _state_lock:
            current = copy.deepcopy(STATE["fault"])
            if not current or current.get("at") != f["at"]:
                return
            if current.get("relay_notice", {}).get("stage") != stage:
                return  # A later phase already superseded this announcement.
            _relay_notice_due.clear()
    _unsaved_edge()
    code = FC_RELAY_RECOVERED if stage == "recovered" else f["code"]
    _log("%s %s" % (code, _RELAY_ACTIVITY_MESSAGES[stage]))
    _retry_relay_notice()


def _relay_notice_completed(token, ok):
    """Late callbacks cannot acknowledge a newer stage or another fault."""
    with _fault_lock:
        with _state_lock:
            _relay_notice_inflight.discard(token)
            f = copy.deepcopy(STATE["fault"])
            notice = (f or {}).get("relay_notice")
            if not notice or (f["at"], notice["stage"]) != token:
                _relay_notice_due.pop(token, None)
                return
            if not ok:
                _relay_notice_due[token] = time.monotonic() + RELAY_NOTICE_RETRY_SECONDS
                return
            notice["delivered"] = True
            f["delivered_at"] = time.time()
            STATE["fault"] = f
            _relay_notice_due.pop(token, None)
        _save_annotation(f)
    _unsaved_edge()


def _retry_relay_notice():
    """Retry only current undelivered mail, outside all control locks."""
    _retry_fault_annotation()
    if not _email_ready():
        return
    with _fault_lock, _state_lock:
        f = copy.deepcopy(STATE["fault"])
        notice = (f or {}).get("relay_notice")
        if not notice or notice["delivered"]:
            return
        stage = notice["stage"]
        if stage == "recovered" and not f.get("stopped"):
            return  # A subsequent ON/draw observation superseded the recovery.
        token = (f["at"], stage)
        if token in _relay_notice_inflight or time.monotonic() < _relay_notice_due.get(token, 0):
            return
        _relay_notice_inflight.add(token)
    code = FC_RELAY_RECOVERED if stage == "recovered" else f["code"]
    descriptions = {
        "warning": "OFF retry started. Three attempts are allowed, 30 seconds apart. "
                   "The bounded schedule continues independently of email delivery.",
        "hard": "The first three OFF attempts did not confirm power removal. "
                "A hard fault is latched. The bounded schedule allows a 60-second "
                "wait followed by three final attempts, 30 seconds apart; email retries do not restart it.",
        "exhausted": "All six OFF retries are exhausted. Automatic OFF retries have stopped.",
        "recovered": "The relay reports OFF and fresh measured draw is within the OFF limit. "
                     "Stop confirmed. The fault remains latched until acknowledged.",
    }
    text = descriptions[stage]
    if stage == "warning" and f["code"] == FC_MANUAL_STOP:
        text = "A Manual OFF command could not be confirmed. " + text
    title = "Stop confirmed" if stage == "recovered" else (
        "OFF retry warning" if stage == "warning" else "Unable to stop - hard fault")
    _notify("%s %s" % (code, title),
            _alert_body(code, title, time.time(), text + "\nAuto remains blocked. "
                "Force on overrides the stop without clearing the fault. "
                "An OFF reply alone does not prove contacts opened. Continued draw may "
                "indicate failed contacts or incorrect metering; communication failures "
                "cannot establish a mechanical fault. Arrange safe independent isolation "
                "and inspection if power removal is unconfirmed.",
                [("OFF retries consumed", str(f["relay_retry"]["attempts"])),
                 ("Power removal", "Confirmed" if f.get("stopped") else "NOT confirmed"),
                 ("Controller mode", _mode_now())]),
            on_complete=lambda ok: _relay_notice_completed(token, ok))


def _relay_release_step(plug, gen=None):
    """Two bounded groups of three OFF retries; observation continues afterwards."""
    global _relay_retry_clock
    notices = []
    attempt_message = None
    result = False
    with _act_lock:
        if plug is None or _stale_client(gen) or CFG["control"].get("mode") == "on":
            return False
        with _state_lock:
            f = copy.deepcopy(STATE["fault"])
            unsaved = STATE["fault_unsaved"]
            watts, age = STATE["watts"], _elapsed_age_locked("watts_at")
            fan = STATE["fan"]
        if not f or f.get("code") not in _RELAY_FAULT_CODES or unsaved:
            return False
        if "relay_retry" not in f:
            f, saved = _relay_retry_update(relay_retry={"attempts": 0, "phase": "first"})
            if not saved:
                return False
        retry = f["relay_retry"]
        # Only fresh low watts warrant an extra read-only relay check. High
        # draw does not create an extra five-second command/polling loop.
        confirmed = False
        low = (watts is not None and _finite(watts) and watts <= CFG["plug"]["watts_off_max"]
               and age is not None and age <= WATTS_STALE)
        if low and fan is False and retry["phase"] == "recovered" and f.get("stopped"):
            return True  # Normal meter/relay loops monitor a confirmed recovery.
        if low:
            try:
                actual = plug.state()
                _record_relay_observation(actual)
                if actual is False:
                    watts = plug.power_w()
                    acquired_wall, acquired_mono = time.time(), time.monotonic()
                    with _state_lock:
                        STATE["watts"] = watts
                        _stamp_elapsed_locked("watts_at", acquired_wall, acquired_mono)
                    confirmed = (watts is not None and _finite(watts)
                                 and watts <= CFG["plug"]["watts_off_max"])
            except PlugError:
                pass
        if confirmed:
            if retry["phase"] != "recovered" or not f.get("stopped"):
                f, saved = _relay_retry_update(stopped=True, relay_retry={
                    "attempts": retry["attempts"], "phase": "recovered"},
                    msg="%s Fan off" % f["code"])
                if saved and retry["phase"] != "recovered":
                    notices.append((f, "recovered"))
            result = bool(f.get("stopped"))
        elif retry["phase"] in ("exhausted", "recovered"):
            # Exhaustion is terminal. A relapse does not grant six new tries.
            if f.get("stopped"):
                _relay_retry_update(stopped=False)
        elif time.monotonic() >= _relay_retry_deadline(f):
            n = retry["attempts"] + 1
            phase = "exhausted" if n == 6 else ("wait" if n == 3 else (
                "final" if n > 3 else "first"))
            code = FC_RELAY_RELEASE if n >= 3 else f["code"]
            msg = "%s %s" % (code, "Unable to stop" if n >= 3 else
                "Manual OFF unconfirmed" if code == FC_MANUAL_STOP else "OFF but still drawing power")
            f, saved = _relay_retry_update(code=code, msg=msg, stopped=False,
                relay_retry={"attempts": n, "phase": phase})
            _relay_retry_clock = (f["at"], n, time.monotonic() + (60 if n == 3 else 30))
            if not saved:
                return False
            attempt_message = "%s OFF retry %d of 6" % (code, n)
            if n == 1:
                notices.append((f, "warning"))
            try:
                actual = plug.state()
                _record_relay_observation(actual)
            except PlugError:
                pass  # An unknown state must not prevent the bounded OFF attempt.
            try:
                plug.set(False)
                _remember_relay(plug, False)
                _record_relay_observation(False)
                w = plug.power_w()
                acquired_wall, acquired_mono = time.time(), time.monotonic()
                with _state_lock:
                    STATE["watts"] = w
                    _stamp_elapsed_locked("watts_at", acquired_wall, acquired_mono)
                confirmed = w is not None and _finite(w) and w <= CFG["plug"]["watts_off_max"]
            except PlugError:
                confirmed = False
            # Spacing begins after the request finishes, including timeouts.
            _relay_retry_clock = (f["at"], n, time.monotonic() + (60 if n == 3 else 30))
            if confirmed:
                f, saved = _relay_retry_update(stopped=True,
                    relay_retry={"attempts": n, "phase": "recovered"},
                    msg="%s Fan off" % f["code"])
                if saved:
                    notices.append((f, "recovered"))
                result = saved
            elif n in (3, 6):
                notices.append((f, "hard" if n == 3 else "exhausted"))
    _unsaved_edge()
    # Start precedes attempt 1 in event order. The dashboard is newest-first,
    # so the numbered attempt appears ABOVE its start announcement.
    for snapshot, stage in notices:
        if stage == "warning":
            _relay_retry_notice(snapshot, stage)
    if attempt_message:
        _log(attempt_message)
    for snapshot, stage in notices:
        if stage != "warning":
            _relay_retry_notice(snapshot, stage)
    return result


def _enforce_stop(plug, gen=None):
    """Keep a faulted fan off until the fault is acknowledged.

    Runs every cycle while a fault stands, not once when it is raised:
    the stale-reading failsafe or an external switch can turn the fan
    back on, and a stop command that failed must be retried rather than
    assumed. The fault records whether the plug actually confirmed it.

    RETURNS TRUE ONLY IF THE FAN IS NOW OFF AND THE PLUG SAID SO. Every
    other path returns False: no plug, a superseded generation, Force on
    overriding, an acknowledgement landing first, or the command
    failing. The alert wording depends on this, so "did not confirm" and
    "did not try" both have to read as unconfirmed.
    """
    if plug is None:
        return False
    with _state_lock:
        relay_fault = (STATE["fault"] or {}).get("code") in _RELAY_FAULT_CODES
    if relay_fault:
        return _relay_release_step(plug, gen)
    with _act_lock:
        if _stale_client(gen):
            # Retried next cycle against the right plug. Stopping a
            # device that is not ours is worse than one more cycle of a
            # fan that is already faulted and already being watched.
            return False
        if CFG["control"].get("mode") == "on":
            # Force on overrides a fault by design, and the mode can move
            # between _draw_check reading it and this point. Without the
            # re-check the stop could land just after _force_on switched
            # the fan on, flapping it off until the next draw cycle put
            # it back.
            return False
        with _state_lock:
            if not STATE["fault"]:
                # THE FAULT ITSELF is re-checked for the same reason as
                # the mode. _draw_check sees a fault, releases the lock
                # and calls this - and an acknowledgement can land in
                # between. Without this the fan was switched OFF after
                # the dashboard had reported the fault cleared, with
                # nothing left to explain why. _clear_fault takes
                # _act_lock, so a clear cannot now complete between this
                # check and the command below.
                return False
            # A relay fault created since the outer snapshot is handled on
            # the next cycle by the bounded scheduler, never by this loop.
            if STATE["fault"].get("code") in _RELAY_FAULT_CODES:
                return False
        try:
            actual = plug.state()
            _record_relay_observation(actual)
            if actual is not False:
                plug.set(False)
            _remember_relay(plug, False)
            _record_relay_observation(False)
            # A software OFF reply is not contact confirmation. Read AFTER
            # the command, never trust cached pre-stop watts. Unknown draw
            # cannot certify a stop when monitoring is enabled.
            if CFG["plug"].get("watts_enabled"):
                w = plug.power_w()
                acquired_wall, acquired_mono = time.time(), time.monotonic()
                with _state_lock:
                    STATE["watts"] = w
                    _stamp_elapsed_locked("watts_at", acquired_wall, acquired_mono)
                if w is None or not _finite(w) or w > CFG["plug"]["watts_off_max"]:
                    _invalidate_fault_stop()
                    return False
        except PlugError:
            _invalidate_fault_stop()
            return False            # unconfirmed: retried next cycle
        with _state_lock:
            STATE["fan"] = False
            stop_token = (STATE["fault"].get("at"), STATE["relay_revision"])
    # Outside _act_lock: the fault write takes its own lock, and a disk
    # write must not extend a hold a settings save may be waiting on.
    newly_confirmed = _confirm_fault_stopped(*stop_token)
    with _fault_lock, _state_lock:
        f = STATE["fault"] or {}
        valid = (f.get("at") == stop_token[0]
                 and STATE["relay_revision"] == stop_token[1]
                 and STATE["fan"] is False and f.get("stopped") is True)
        code = f.get("code")
    if newly_confirmed and valid:
        # Carries the code of the fault it belongs to, so the raise and
        # the confirmation come back from the same search. Falls back to
        # the bare line for a fault written by an older version.
        _log("%sFan off \u2014 stop confirmed"
             % ("%s " % code if code else ""))
    return bool(valid)

def _off_draw_check(plug, gen=None):
    """Latch sustained fresh draw despite a confirmed software OFF state."""
    global _off_draw_since
    with _act_lock:
        if _stale_client(gen):
            return False
        pg = CFG["plug"]
        with _state_lock:
            fault, fan, w = STATE["fault"], STATE["fan"], STATE["watts"]
            age = _elapsed_age_locked("watts_at")
            switch_age = _elapsed_age_locked("changed_at", startup=True)
        if (fault or not pg.get("watts_enabled") or CFG["control"]["mode"] == "on"
                or fan is not False or switch_age < OFF_SETTLE_SECONDS
                or age is None or age > WATTS_STALE or w is None
                or not _finite(w) or w <= pg["watts_off_max"]):
            _off_draw_since = None
            return False
        mono = time.monotonic()
        if _off_draw_since is None:
            _off_draw_since = mono
            return False
        elapsed = mono - _off_draw_since
        if elapsed < pg["watts_fault_seconds"]:
            return False
        # A cached OFF must not fault a legitimate external ON transition.
        actual = _fan_status(plug) if plug is not None else None
        if actual is not False:
            if actual is True:
                with _fault_lock, _state_lock:
                    message = _adopt_relay_locked(True, time.time())
            else:
                message = None
            _off_draw_since = None
            if message:
                _log(message)
            return False
        now = time.time()
        msg = "%s OFF but drawing %g W for %ds" % (FC_OFF_DRAW, w, int(elapsed))
        f = dict(at=now, msg=msg, stopped=False, code=FC_OFF_DRAW,
                 attempted_at=now, delivered_at=None,
                 relay_retry={"attempts": 0, "phase": "first"})
        _store_fault(f)
        _off_draw_since = None
    _log(msg)
    try:
        _enforce_stop(plug, gen)
    except Exception:
        pass
    return True


def _draw_check(plug, gen=None):
    """Fault the fan if it runs outside its expected draw for long enough.

    Force on takes precedence: it is how a repaired fan gets tested, so
    the detector stands down there rather than fighting it.

    THE GENERATION IS CHECKED HERE, not merely handed to _enforce_stop.
    This function accepted gen and passed it on, which stopped it
    COMMANDING a superseded plug but not JUDGING one: it still read the
    cached watts, ran the timer, and could raise and persist a draw
    fault describing a device that is no longer configured. A false
    fault blocks automatic running, emails that the fan was stopped, and
    is then enforced against the NEW plug on the next cycle. Not
    commanding the wrong plug is not the same as not faulting because of
    it.
    """
    if _off_draw_check(plug, gen):
        return
    global _oob_since, _oob_generation, _oob_monotonic, _draw_grace
    external_stop, external_msg = False, None
    with _act_lock:
        if _stale_client(gen):
            return
        # In the same hold as the check: a save landing between them
        # would leave the timer in the state this is guarding against.
        if gen is not None and _oob_generation != gen:
            _oob_since = None
            _oob_generation = gen

        # EVERYTHING that decides whether to fault stays in this hold,
        # not just the check that opens it. Checking the generation and
        # then releasing left the band, the timer and _store_fault
        # outside: a save widening the range to 90-110W while a 99W
        # reading was being judged still produced a persistent
        # "Draw fault - 99 W outside 10-20 W". The old plug was never
        # commanded, but the false fault blocks Auto, emails that the fan
        # was stopped, and is enforced against the NEW plug next cycle.
        #
        # THIS ESTABLISHES _act_lock -> _fault_lock, through
        # _store_fault. The rule is about DIRECTION: no code path
        # acquires _act_lock while already holding _fault_lock, and
        # every combined acquisition goes _act_lock -> _fault_lock.
        #
        # An earlier version of this comment said "no function that
        # takes _fault_lock ever takes _act_lock", which THIS FUNCTION
        # falsifies - it holds _act_lock and reaches _fault_lock through
        # _store_fault. Audited by direction, which is the property that
        # prevents a deadlock.
        #
        # A settings save can now wait on a fault write as well as on a
        # plug call - bounded, visible, and preferable to a fault raised
        # against limits that no longer exist.
        if CFG["control"]["mode"] == "on":
            _oob_since = None
            return
        with _state_lock:
            fault = STATE["fault"]
        if fault:
            standing = True
        else:
            standing = False
            pg = CFG["plug"]
            if not pg.get("watts_enabled"):
                _oob_since = None
                return
            now = time.time()
            with _state_lock:
                fan, w = STATE["fan"], STATE["watts"]
                changed = STATE["changed_at"]
                watt_age = _elapsed_age_locked("watts_at")
            # A motor spinning up draws nothing yet, and a fan meant to
            # be off is not a fault. changed_at is deliberately left
            # unset when startup finds the fan already running, so fall
            # back to START_TIME: treating None as never-settled
            # disabled draw protection for as long as that run lasted,
            # which on a high-CO2 day is hours.
            since = changed if changed is not None else START_TIME
            mono = time.monotonic()
            if _draw_grace is None or _draw_grace[0] != since:
                with _state_lock:
                    age = _elapsed_age_locked("changed_at", startup=True)
                remaining = SETTLE_SECONDS - age
                _draw_grace = (since, mono + remaining)
            if fan is not True or mono <= _draw_grace[1]:
                _oob_since = None
                return
            # Freshness is judged here, not taken from the cached value:
            # when the plug stops answering, STATE keeps the last good
            # reading and would otherwise look healthy for as long as
            # the outage lasted. No reading is out of band, since an
            # unanswering plug is what this catches.
            if watt_age is None or watt_age > WATTS_STALE:
                w = None
            lo, hi = float(pg["watts_min"]), float(pg["watts_max"])
            if w is not None and lo <= w <= hi:
                _oob_since = None
                return
            if _oob_since is None:
                _oob_since = now
                _oob_monotonic = (now, mono)
                return
            if _oob_monotonic is None or _oob_monotonic[0] != _oob_since:
                # Legacy handovers carried only a wall timestamp. Migrate once;
                # subsequent elapsed checks must never follow wall-clock changes.
                elapsed = max(0, now - _oob_since)
                _oob_monotonic = (_oob_since, mono - elapsed)
            elapsed = mono - _oob_monotonic[1]
            if elapsed < float(pg["watts_fault_seconds"]):
                return
            # Cached relay state can outlive an external stop when the
            # fault delay is shorter than the independent relay cadence.
            # Confirm under the same actuation hold as the fault decision.
            # Unknown is NOT off: failed confirmation cannot disable the
            # existing fail-closed draw/comms protection.
            actual = _fan_status(plug) if plug is not None else None
            if actual is False:
                with _fault_lock, _state_lock:
                    external_msg = _adopt_relay_locked(False, time.time())
                _oob_since = None
                external_stop = True
            else:
                reading = "no reading" if w is None else ("%g W" % w)
                code = _draw_fault_code(w, lo, hi)
                # Same token in activity log, dashboard, email and disk.
                msg = ("%s Draw fault - %s outside %g-%g W for %ds"
                       % (code, reading, lo, hi, int(elapsed)))
                f = {"at": now, "msg": msg, "stopped": False, "code": code,
                     "attempted_at": now, "delivered_at": None}
                prev = _store_fault(f)
                _oob_since = None

    # Out of the hold. _enforce_stop takes _act_lock itself, and the
    # log, the email and the fault-stop confirmation are all things that
    # must not extend a hold a settings save is waiting on.
    if external_stop:
        if external_msg:
            _log(external_msg)
        return
    if standing:
        _enforce_stop(plug, gen)
        return
    _log(msg)
    # Only on the transition into a fault. _store_fault returns what was
    # there before, so a fault raised while one already stood - which
    # cannot currently happen, but is one refactor away - stays silent.
    # Dispatched HERE rather than inside _store_fault: that holds
    # _fault_lock and _state_lock, and nothing that can block belongs
    # under them.
    #
    # prev is None IS the duplicate guard - a fault raised while one
    # already stands cannot send. attempted_at and delivered_at are not
    # needed for that; they exist for the RESTART notice, which rate
    # limits on delivery. Do not add a "have I sent" flag to this path
    # expecting it to do work: it would read as protection and provide
    # none.
    #
    # lo/hi are the LIVE band, read from CFG a few lines above and passed
    # in, never stored on the fault and never parsed back out of msg.
    # The dashboard edits that band, so a copy would drift.
    raised_at = f["at"]

    def _fault_sent(ok):
        if ok:
            _mark_fault_delivered(raised_at)

    if prev is None:
        # THE STOP RUNS FIRST so the message can say what actually
        # happened. It used to be queued first and enforced after, so
        # every alert asserted "Plug switched off" even when the relay
        # then refused - reported by review against 3b30c6c, reproduced
        # with the fan still on and stopped=false.
        #
        # WRAPPED, because the old order had one virtue worth keeping: a
        # plug that explodes must not be able to suppress the alert. An
        # unconfirmed stop is still a fault worth mailing about.
        try:
            confirmed = bool(_enforce_stop(plug, gen))
        except Exception:           # pragma: no cover - belt and braces
            confirmed = False
        code = f.get("code") or _draw_fault_code(w, lo, hi)
        titles = {FC_PLUG_COMMS: "Plug Communications Fault",
                  FC_UNDER_POWER: "Hard Fault - Under Power",
                  FC_OVER_POWER: "Hard Fault - Over Power"}
        causes = {
            FC_PLUG_COMMS: ["Plug offline or rebooting",
                            "Wi-Fi or network connectivity",
                            "Plug power lost"],
            # FC-003 covers nothing drawing at all AND drawing but
            # not enough. _draw_fault_cause already tells them apart in
            # prose; the causes have to agree with the sentence above
            # them, or the message contradicts itself the way FC-002
            # once did.
            FC_UNDER_POWER: (["Fan failed to start",
                              "Fan unplugged or disconnected",
                              "Plug relay not actually closing",
                              "Failed motor"]
                             if (w is not None and w < 1.0) else
                             ["Obstruction slowing the fan",
                              "Seized or dragging bearing",
                              "Failed capacitor",
                              "Wiring fault",
                              "Draw band set for a different fan"]),
            FC_OVER_POWER: ["Mechanical obstruction",
                            "Bearing failure or excessive load",
                            "Motor fault",
                            "Draw band still set for a previous fan"],
        }
        _notify("%s %s - %s"
                % (code,
                   # "Fan stopped" is a claim. Unconfirmed, the honest
                   # subject is that the fault is latched: the fan is
                   # blocked from automatic running either way, which is
                   # the part that is certainly true.
                   "Fan stopped" if confirmed else "Fan fault latched",
                   "plug not answering" if w is None
                   else "under power %g W" % w if code == FC_UNDER_POWER
                   else "over power %g W" % w),
                _alert_body(
                    code, titles[code], raised_at,
                    _draw_fault_cause(w, lo, hi),
                    [("Expected range", "%g - %g W" % (lo, hi)),
                     ("Actual reading",
                      "no reading" if w is None else "%g W" % w),
                     # From the RESULT, not from the code. A silent
                     # plug usually means an unconfirmed stop, but not
                     # always - it can answer a switch while its power
                     # reading is stale - and a plug that reads fine can
                     # still refuse the command.
                     ("Action taken",
                      "Plug switched off, fault latched" if confirmed else
                      "Stop commanded, NOT confirmed - fault latched"),
                     ("Manual action", "Yes"),
                     ("How to clear", "Acknowledge in the dashboard"),
                     ("Controller mode", _mode_now())],
                    causes[code]),
                on_complete=_fault_sent)


def _relay_recovered(fault):
    return (fault.get("code") in _RELAY_FAULT_CODES
            and fault.get("stopped")
            and fault.get("relay_retry", {}).get("phase") == "recovered")


def _finish_saved_relay_recovery_locked(fault):
    """Complete a durable recovery while holding act -> fault locks.

    Manual marker creation and its mode commit use the same actuation
    transaction, so this cannot delete a marker created midway through it.
    The standing fault is deliberately retained for acknowledgement.
    """
    recovered = _relay_recovered(fault)
    if "manual_stop_id" in fault and fault["manual_stop_id"] != _manual_stop_id():
        return  # This durable recovery must not consume a newer promise.
    if recovered:
        with _state_lock:
            _end_run_locked()
            STATE["manual_off_pending"] = False
        clear_manual_off_promise()


def _retry_fault_persist():
    """Re-attempt a fault write that failed. True if it saved this time.

    The read happens INSIDE _fault_lock. Taking the snapshot first and
    the lock afterwards let an acknowledgement land in between, so the
    retry wrote the cleared fault back to disk and a restart resurrected
    it: memory None, disk faulted.
    """
    # Completing a saved relay recovery also discharges Manual's durable
    # stop promise. Serialize with mode changes and relay observations so
    # neither a newer ON nor a newly requested stop can be cleared here.
    with _act_lock:
        with _fault_lock:
            with _state_lock:
                owed = STATE["fault_unsaved"]
                pending = copy.deepcopy(STATE["fault"])
            if pending is None:
                return False
            if not owed:
                # Essential shutdown persistence may already have landed
                # while actuation was busy. Finish matching cleanup without
                # claiming the safety record is still unsaved or rewriting it.
                if (_relay_recovered(pending) and pending.get("manual_stop_id")
                        and pending["manual_stop_id"] == _manual_stop_id()):
                    _finish_saved_relay_recovery_locked(pending)
                    return True
                return False
            try:
                save_fault(pending)
            except OSError:
                return False
            with _state_lock:
                STATE["fault_unsaved"] = False
            _finish_saved_relay_recovery_locked(pending)
    _log("%s Fault saved \u2014 will survive a restart"
         % FC_FAULT_PERSISTED)
    # Resets the alert edge. Without this the flag stayed raised after a
    # successful retry, and a LATER write failure sent nothing - the one
    # thing this alert exists to catch. Found by test_alert_delivery.py
    # rather than by reading the code.
    _unsaved_edge()
    return True


def watts_loop():
    """Poll the plug's meter on its own short cadence.

    Draw is read here rather than in tick() because the Protect bootstrap
    is the expensive call: tying watts to poll_seconds left the reading up
    to 30s stale while the UI refreshed every 5s. Errors are swallowed so
    a plug hiccup cannot overwrite the control loop's error.
    Relay checks only supplement the normal loop when state is unknown or
    normal observations stop. Meter success is not required for that fallback,
    so a sensor outage cannot hide an already-running fan.
    """
    last_issue = None
    relay_issue = None
    while not _stopping.is_set():
        try:
            _retry_relay_notice()
        except Exception as e:
            _log("%s OFF-fault notification retry failed: %s" % (FC_ALERT_FAILED, type(e).__name__))
        if PLUG_ACCESS_DISABLED:
            # Keep the service heartbeat, not fictional meter/relay readings.
            with _state_lock:
                _stamp_elapsed_locked("draw_loop_at")
                STATE["draw_loop_error"] = None
            time.sleep(WATTS_SECONDS)
            continue
        issue = None
        try:
            _retry_fault_persist()
        except Exception as e:
            issue = "fault retry: %s" % type(e).__name__
        plug = None
        gen = None
        relay_fallback = False
        relay_phase = "relay read"
        try:
            _, plug, gen = _client_snapshot()
            if CFG['control']['mode'] == 'manual':
                # Independent of Protect: its DNS/session may still be stuck
                # while the control thread waits for a bounded sensor fetch.
                relay_phase = "Manual OFF"
                _manual_off(plug, gen)
            relay_phase = "relay read"
            relay_fallback = (CFG['control']['mode'] != 'on'
                              and _relay_fallback_needed(plug, gen))
            relay_issue = None
            if relay_fallback:
                _refresh_relay(plug, gen, poll=True)
        except Exception as e:
            relay_issue = "%s: %s" % (relay_phase, _plug_issue(e))
        # A power-only success cannot announce recovery of a relay failure.
        issue = issue or relay_issue
        try:
            if plug is None:
                raise PlugError("No plug client available")
            w = plug.power_w()
            acquired_wall, acquired_mono = time.time(), time.monotonic()
            # A reading from a plug that is no longer configured is not
            # this fan's draw. Discarding it here covers the stored value
            # AND the draw check below, which would otherwise raise a
            # fault about the wrong device.
            #
            # This skips the sleep at the end of the loop, deliberately:
            # the next iteration takes a fresh snapshot and will match,
            # so it costs one early retry rather than spinning, and the
            # new plug gets read at once instead of 5s late.
            with _act_lock:
                if _stale_client(gen):
                    continue
                with _state_lock:
                    STATE["watts"] = w
                    _stamp_elapsed_locked("watts_at", acquired_wall, acquired_mono)
        except Exception as e:
            # Left for the staleness check rather than cleared here: one
            # missed request should not blank a valid reading, but a
            # stale one must never be presented as current.
            issue = issue or "power read: %s" % _plug_issue(e)
        # Outside the read, so losing the plug still reaches the detector.
        # Inside, a failed power_w() jumped straight to except and the
        # out-of-band timer never started.
        try:
            if (plug is not None
                    and CFG["control"]["mode"] == "on"):
                # Enforced on this thread too. The poll loop backs off to
                # 300s while Protect is unreachable, so tick() alone could
                # leave a fan switched off elsewhere stopped for minutes
                # while the mode says otherwise.
                _force_on(plug, gen)
        except Exception as e:
            issue = issue or "Force on: %s" % _plug_issue(e)
        # Called even in Force on, where it stands down and clears the
        # out-of-band timer. Skipping it there would leave a stale start
        # time to resume from and could fault the fan early on the way out.
        try:
            _draw_check(plug, gen)
        except Exception as e:
            issue = issue or "draw check: %s" % _plug_issue(e)
        with _state_lock:
            _stamp_elapsed_locked("draw_loop_at")
            STATE["draw_loop_error"] = issue
        if issue != last_issue:
            if issue:
                _log("%s Draw loop error - %s" % (FC_POLL_ERROR, issue))
            elif last_issue:
                _log("%s Draw loop recovered" % FC_POLL_ERROR)
            last_issue = issue
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

    Manual and Force on bypass this wait. An Auto lookup runs in one
    daemon worker, without controller locks or state/log callbacks: libc's
    resolver cannot be interrupted, but it must not hold up the poll loop.
    Settings changes, shutdown and the monotonic deadline abandon its
    result promptly. A stuck lookup never creates overlapping retry workers.

    The ceiling sits below 60s, the shortest fail-safe window
    `stale_minutes` can express, so ventilating on stale readings is never
    delayed by waiting for DNS.

    Bounded, not infinite: a genuinely wrong hostname must still reach the
    normal error path so the UI can report it. True if it resolved.
    """
    started = time.monotonic()
    deadline = started + timeout
    waited = False
    pending = None
    next_attempt = time.monotonic()

    def resolve(target, outcome, resolver):
        try:
            resolver(target, None)
            outcome['ok'] = True
        except Exception:
            # Normal polling owns detailed error reporting after startup.
            pass
        finally:
            outcome['done'].set()

    while not _stopping.is_set():
        with _cfg_lock:
            host = (CFG.get("protect") or {}).get("host")
            mode = (CFG.get("control") or {}).get("mode")
        if mode != 'auto':
            return False       # Actuation must not wait for Protect DNS.
        if _wake.is_set():
            _wake.clear()
            return False
        if not host:
            return False       # unconfigured; tick() reports that properly
        try:
            # Pure parsing: even a resolver call with AI_NUMERICHOST need
            # not be trusted to return promptly in every implementation.
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if time.monotonic() >= deadline:
            _log("%s Could not resolve %s after %ds, starting anyway"
                 % (FC_STARTUP, host, timeout))
            return False
        if pending is not None:
            if pending['host'] != host:
                return False   # Never consume a result for an old host.
            if pending['done'].is_set():
                if pending['ok']:
                    if waited:
                        _log("%s Resolved %s, starting normally" % (FC_STARTUP, host))
                    return True
                pending = None
                next_attempt = time.monotonic() + interval
        if pending is None and time.monotonic() >= next_attempt:
            pending = dict(host=host, ok=False, done=threading.Event())
            threading.Thread(target=resolve,
                             args=(host, pending, socket.getaddrinfo),
                             daemon=True, name='startup-dns').start()
        # Normal DNS success should not add two activity rows per restart.
        # Report a real delay, and pair its recovery with that notice only.
        if not waited and time.monotonic() - started >= 5:
            _log("%s Waiting for DNS to resolve %s" % (FC_STARTUP, host))
            waited = True
        if _wake.wait(min(0.1, max(0, deadline - time.monotonic()))):
            # Settings changed. Cleared here rather than left for loop(),
            # which would otherwise consume it after its first tick() and
            # poll a second time immediately. That tick applies the change.
            _wake.clear()
            return False
    return False


def _fail(msg, last, code=None):
    """Record a poll failure in STATE and, once, in the activity log.

    The CODE comes from the caller because only the caller knows which
    subsystem raised: a ProtectError and a PlugError arrive here as
    plain text otherwise. STATE["error"] stays uncoded - the dashboard
    shows it as a sentence, and a code in a status line is noise.

    STATE["error"] is the live value the dashboard shows. The log entry is
    what survives the tab being closed: this branch previously wrote only
    to STATE, so a restart-time failure left no trace in journalctl or
    events.json and could be caught only by watching the UI at the time.

    Logged only when the message changes. The loop backs off to at most
    300s, so a sustained outage would otherwise append an identical entry
    every few minutes and push real switching history out of the
    1000-entry window.
    """
    with _state_lock:
        STATE["error"] = msg
        if code == FC_SENSOR_DOWN:
            STATE["sensor_error"] = msg
    if msg != last:            # outside the lock: _log takes it itself
        _log("%s %s" % (code, msg) if code else msg)
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
    # Measured from startup when nothing has been read, matching _failsafe.
    with _state_lock:
        remaining = mins * 60 - _elapsed_age_locked("reading_at", startup=True)
    if remaining <= 0:
        return delay
    return min(delay, remaining)


def loop():
    # Ordered after network-online.target, which proves an address but not
    # a working resolver. See _wait_for_dns.
    _wait_for_dns()
    fails = 0
    last_logged = None
    last_code = None
    while not _stopping.is_set():
        try:
            # Inside the try: a bad value here used to kill the thread.
            base = max(5, min(3600, int(float(CFG["control"]["poll_seconds"]))))
        except (TypeError, ValueError, KeyError):
            base = 30
        try:
            tick()
            if last_logged is not None:
                # Recovery carries the code of the failure it ends, so
                # both halves come back from one search. 2xx has no
                # separate all-clear series: 1xx exists so a MAIL RULE
                # can split alarms from clears, and nothing here is
                # mailed.
                _log("%s Poll recovered" % (last_code or FC_POLL_ERROR))
            fails = 0
            last_logged = None
            last_code = None
        except (ProtectError, PlugError) as e:
            # Logged as well as shown: making login() raise ProtectError
            # moved the restart-time DNS failure into this branch, so
            # logging only the generic one below would hide it again.
            fails += 1
            last_code = (FC_PLUG_DOWN if isinstance(e, PlugError)
                         else FC_SENSOR_DOWN)
            last_logged = _fail(_redact(e), last_logged, last_code)
        except Exception as e:  # keep the loop alive on anything unexpected
            fails += 1
            last_code = FC_POLL_ERROR
            last_logged = _fail(
                _redact("%s: %s" % (type(e).__name__, e)), last_logged,
                last_code)
        if fails:
            # After both branches: a login timeout or malformed payload is
            # exactly when ventilation matters, and neither raises
            # ProtectError or PlugError.
            try:
                _failsafe()
            except Exception as e:
                _log(_redact("%s Fail-safe failed: %s: %s"
                 % (FC_FAILSAFE_FAILED, type(e).__name__, e)))
        # Back off on repeated failures so a rate limit is not made worse.
        delay = base if not fails else min(300, base * 2 ** min(fails, 5))
        if fails:
            # The backoff must not outrun the fail-safe's own deadline.
            delay = _failsafe_cap(delay)
        # Wake early when settings change so mode switches are instant.
        _wake.wait(delay)
        _wake.clear()


app = Flask(__name__)
# Every request body. Nothing here legitimately posts more than a few
# hundred bytes - the largest is a settings save - and /api/clienterror
# stays open even once auth is on, so without a cap one LAN device can
# push arbitrary volume into the journal and events.json.
app.config["MAX_CONTENT_LENGTH"] = 8192
# When each recent client error was last written to the activity log, so
# a script erroring in a render loop cannot evict the 1000 entries of real
# fan history behind it. A single most-recent slot was not enough:
# alternating messages - A, B, A, B - each differ from the one before, so
# every one got through and the suppression did nothing.
_ui_error_times = {}
_ui_error_lock = threading.Lock()
UI_ERROR_QUIET_SECONDS = 60
UI_ERROR_TRACKED = 64

# One reverse proxy fronts this service, so the peer address is always
# the proxy and every request logged as the same client. Trust a single
# hop of X-Forwarded-For / -Proto / -Host to recover the real client.
# One hop only: trusting more lets a client forge the chain.
if os.environ.get("FAN_CONTROLLER_TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def _proxy_ip(value):
    """Exact IP literals only; normalize IPv4 peers on dual-stack sockets."""
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Expected an unscoped IP address')
    address = ipaddress.ip_address(value)
    return getattr(address, 'ipv4_mapped', None) or address


def _parse_proxy_ips(value):
    if value == '':
        return frozenset()
    try:
        return frozenset(_proxy_ip(part.strip()) for part in value.split(','))
    except ValueError as exc:
        raise ValueError('FAN_CONTROLLER_ALLOWED_PROXY_IPS must contain comma-separated IP addresses') from exc


class _ProxySourceAllowlist:
    """Gate the socket peer BEFORE ProxyFix can replace REMOTE_ADDR."""
    def __init__(self, application, allowed):
        self.application, self.allowed = application, allowed

    def __call__(self, environ, start_response):
        try:
            peer = _proxy_ip(environ.get('REMOTE_ADDR'))
        except ValueError:
            peer = None
        local_health = (peer is not None and peer.is_loopback
                        and environ.get('PATH_INFO') == '/healthz'
                        and environ.get('REQUEST_METHOD') in ('GET', 'HEAD'))
        if peer not in self.allowed and not local_health:
            return Response('{"error":"Request source is not an allowed proxy"}',
                            status=403, mimetype='application/json',
                            headers={'Cache-Control': 'no-store'})(environ, start_response)
        return self.application(environ, start_response)


_allowed_proxy_ips = _parse_proxy_ips(os.environ.get('FAN_CONTROLLER_ALLOWED_PROXY_IPS', ''))
if _allowed_proxy_ips:
    # Outermost middleware: do not move this inside ProxyFix or use
    # request.remote_addr / forwarded headers as the access-control identity.
    app.wsgi_app = _ProxySourceAllowlist(app.wsgi_app, _allowed_proxy_ips)


@app.get('/healthz')
def healthz():
    """Process HTTP health only; contains no controller state or credentials."""
    return jsonify(ok=True, process_started_at=_PROC_START)


def _safe_cfg():
    with _cfg_lock:
        c = copy.deepcopy(CFG)
    c["ui"]["password_hash"] = "SET" if c["ui"]["password_hash"] else ""
    c["protect"]["password"] = "SET" if c["protect"]["password"] else ""
    c["plug"]["password"] = "SET" if c["plug"]["password"] else ""
    c["email"]["password"] = "SET" if c["email"]["password"] else ""
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
PUBLIC_GET = {"/", "/api/state", "/api/history", "/api/daily",
              "/healthz",
              "/favicon.ico", "/apple-touch-icon.png",
              "/apple-touch-icon-precomposed.png"}
PUBLIC_POST = {"/api/clienterror"}


def _origin_key(value):
    """Compare HTTP origins, including their effective port."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


@app.before_request
def _require_same_origin():
    """Protect API writes even when optional dashboard auth is disabled.

    Browser origin/fetch headers cannot be forged by page JavaScript.
    Headerless scripts remain supported; JSON validation blocks HTML forms.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE") \
            or not request.path.startswith("/api/") \
            or request.path in PUBLIC_POST:
        return None
    expected = _origin_key(request.host_url)
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    source = origin if origin is not None else referer
    supplied = _origin_key(source) if source is not None else None
    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if (source is not None and (supplied is None or supplied != expected)) \
            or fetch_site in ("cross-site", "same-site"):
        return jsonify(error="API changes require a same-origin request."), 403
    return None


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


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon-32.png")


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return app.send_static_file("apple-touch-icon.png")


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
    # A render report is a different animal from a script error: the
    # script did not fail, it produced nothing. Coded so one search
    # finds every occurrence, and so a reader knows immediately that
    # the fan is fine and the SCREEN is not.
    # Normalised here, once: anything that is not exactly "render" is a
    # script error, so an unknown kind from a browser cannot promote
    # itself into the fault series.
    kind = "render" if b.get("kind") == "render" else "error"
    prefix = "%s " % FC_RENDER_EMPTY if kind == "render" else ""
    # The journal gets every report: it rotates, and the repeat count is
    # diagnostic. The activity log does not, because it is shared with
    # the fan events that matter. Rate limiting the journal is nginx's
    # job, not this endpoint's.
    app.logger.error("UI error: %s @ %s", msg, where)
    # KEYED ON KIND AS WELL AS MESSAGE. The same text means different
    # things from the two paths - one says the script died, the other
    # says it ran and drew nothing - and they carry different codes. On
    # the message alone, whichever arrived second was silently
    # discarded, so an FC-208 could be swallowed by an ordinary script
    # error that happened to say the same thing. Reported by review of
    # 3ea51b3.
    if _ui_error_is_new((kind, msg)):
        _log("%sUI error: %s" % (prefix, msg[:120]))
    return jsonify(ok=True)


def _ui_error_is_new(msg):
    """True if this report has not been logged in the quiet period.

    The key is (kind, message), not the message: the same text from the
    two paths means different things and carries different codes, so
    keying on text alone let one swallow the other.

    Per key, not just the most recent one, or two errors taking turns
    would each look new every time. Bounded, or remembering them
    becomes its own way to fill memory: past the limit the least
    recently seen is dropped, which at worst lets one extra entry
    through for a report nobody has made in a while.
    """
    now = time.time()
    with _ui_error_lock:
        if now - _ui_error_times.get(msg, 0) < UI_ERROR_QUIET_SECONDS:
            return False
        _ui_error_times[msg] = now
        while len(_ui_error_times) > UI_ERROR_TRACKED:
            del _ui_error_times[min(_ui_error_times,
                                    key=_ui_error_times.get)]
        return True


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
    except RequestEntityTooLarge:
        # Let Flask answer 413. Catching it here turned "your body is
        # too big" into "your JSON is malformed", which sends whoever
        # is debugging it looking at the wrong thing entirely.
        raise
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
        durable = True
        try:
            save_cfg(cand)
        except InstalledNotDurable:
            # config.json already holds the new hash. Leaving CFG on the
            # old one would divide disk from memory, and for this route
            # that means a restart silently turning authentication off
            # after the caller was told nothing had changed.
            durable = False
        except OSError as e:
            return jsonify(error="Could not save the password (%s). "
                                 "Nothing was changed." % e), 500
        CFG["ui"] = cand["ui"]
    _log("UI password %s" % ("set" if pw else "cleared"))
    notice = None if durable else (
        "The password change is active, but the disk did not confirm it "
        "will survive a power cut. Check the filesystem and set it again.")
    return jsonify(ok=True, enabled=bool(pw), notice=notice)


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
                           if k not in ("history", "daily")})
        watt_age = _elapsed_age_locked("watts_at")
        s["sensor_age"] = _elapsed_age_locked("reading_at")
        draw_age = _elapsed_age_locked("draw_loop_at")
        s["draw_loop_age"] = draw_age if draw_age is not None else max(0,time.monotonic()-_PROC_MONO)
        s["switch_age"] = _elapsed_age_locked("changed_at")
        lease_key = "min_run_at" if STATE["min_run_at"] is not None else "changed_at"
        age = _elapsed_age_locked(lease_key)
        s["min_run_remaining"] = max(0, CFG["control"]["min_run_minutes"]*60-age) if age is not None else 0
        retry = (s["fault"] or {}).get("relay_retry")
        if retry:
            clock = _relay_retry_clock
            matching = clock is not None and clock[:2] == (s["fault"]["at"], retry["attempts"])
            s["fault_retry"] = dict(retry, remaining=max(0, clock[2]-time.monotonic())
                if matching else (60 if retry["phase"] == "wait" else 30))
    deadline = s.pop("cold_start_hold_until", None)
    s.pop("cold_start_pending", None)
    s["cold_start_remaining"] = max(0, deadline-time.monotonic()) if deadline else 0
    # Trim: 300 events every 5s is needless payload; the rest stay on disk.
    # An unreachable plug must not leave a positive draw on screen: a
    # stale reading is reported as unknown, not as current.
    if watt_age is None or watt_age > WATTS_STALE:
        s["watts"] = None
    # Served with the class, not stored with it: the colour is a
    # presentation decision that can change, and rewriting events.json
    # to recolour a log would be absurd.
    s["events"] = [dict(e, msg=_short_relay_activity(e["msg"]), cls=event_class(e.get("msg")))
                   for e in s["events"][:EVENTS_SHOWN]]
    s["config"] = _safe_cfg()
    # Changes on every start, including an execv. It is the only thing a
    # caller can use to prove a restart actually happened: the port
    # answering proves nothing, because a failed exec leaves the original
    # process serving on it.
    s["process_started_at"] = _PROC_START
    # The same token /api/history uses. It is here so a client can tell
    # it is talking to a different process WITHOUT making a history
    # request first: revisions restart with the process, so a dashboard
    # holding revision 100 from a process that is gone would otherwise
    # see a matching revision, never fetch, and never learn.
    epoch = s.pop("history_epoch", "")
    s["history_generation"] = PROC_GENERATION + (":" + epoch if epoch else "")
    return jsonify(s)



def _notify_fault_cleared(had):
    """Send a closure only for a fault whose warning was delivered."""
    if not had.get("delivered_at"):
        return
    raised = had.get("at") or time.time()
    titles = {FC_PLUG_COMMS: "Plug Communications Fault",
              FC_UNDER_POWER: "Hard Fault - Under Power",
              FC_OVER_POWER: "Hard Fault - Over Power",
              FC_OFF_DRAW: "Relay OFF - Power Still Flowing",
              FC_RELAY_RELEASE: "Unable to Stop - Hard Fault"}
    titles[FC_MANUAL_STOP] = "Manual OFF Unconfirmed"
    _notify("%s Fault cleared - fan available again" % FC_HARD_CLEARED,
            _alert_body(
                FC_HARD_CLEARED, "Hard Fault Cleared", time.time(),
                "Acknowledged in the dashboard. The fan is available "
                "again and normal control has resumed.",
                [("Cleared fault", "%s %s"
                  % (had.get("code") or "unknown",
                     titles.get(had.get("code"), ""))),
                 ("Raised at", _stamp(raised)),
                 ("Fault duration", _duration(time.time() - raised)),
                 ("Action taken", "Fault cleared, automatic control resumed"),
                 ("Manual action", "No"),
                 ("Controller mode", _mode_now())]))


@app.route("/api/fault/clear", methods=["POST"])
def api_fault_clear():
    """Acknowledge a draw fault and allow automatic runs again.

    Deliberately manual. Retrying on a timer would switch a seized motor
    on every few minutes forever, which is the thing the fault exists to
    stop.
    """
    global _oob_since, _off_draw_since
    err = _body_error(_json_body())
    if err:
        return err
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
    _off_draw_since = None
    if had:
        code = had.get("code") or "a draw fault"
        _log("%s Fault cleared \u2014 %s" % (FC_HARD_CLEARED, code))
        # Clearing is already committed. Optional notification formatting
        # or queueing must never turn that success into a failed response.
        try:
            _notify_fault_cleared(had)
        except Exception as exc:
            try:
                _log("%s Fault-clear notification could not be prepared: %s"
                     % (FC_ALERT_FAILED, type(exc).__name__))
            except Exception:
                pass
    _wake.set()
    return jsonify(ok=True, cleared=bool(had))
@app.route("/api/daily")
def api_daily():
    """Per-day low and high, going back as far as the file does.

    Separate from /api/history on purpose: history is 2800 points and
    is fetched on a revision check, this is a few hundred bytes a year
    and changes once a day. Nothing on the dashboard polls it yet - it
    exists so the question "was it this humid last week" has an answer
    at all, which the 24h trace cannot give.
    """
    days = request.args.get("days", type=int)
    with _daily_lock:
        # Deep, for the same reason the writer is: a poll can mutate a
        # range while this is being serialised.
        rows = copy.deepcopy(STATE.get("daily") or [])
    if days and days > 0:
        rows = rows[-days:]
    return jsonify(days=rows)


@app.route("/api/history")
def api_history():
    """The 24h trace, split out of /api/state.

    Returned with its own revision so a client that fetches while the poll
    thread appends stores the revision it actually received, rather than
    the one it saw a moment earlier and then missing the next change.

    Answers a DELTA when the caller says which revision it already holds
    AND which process issued it. history_rev increases by exactly one per
    appended point, so a caller is missing rev - since points, and they
    are the last that many: pruning only ever removes from the front,
    never the tail. That one property is what makes this safe without
    tracking anything extra.

    The generation is not decoration. history_rev restarts at 1 with the
    process, so a dashboard left in a background tab can hold revision
    100 from a process that no longer exists, and if the replacement
    climbs back to 100 before the tab wakes, nothing anywhere would
    notice: the revision matches, so no refetch happens, and the next
    delta is appended to a trace from the previous process. Comparing
    generations makes that impossible, and it is checked HERE rather
    than in the browser so a client cannot get it wrong.

    Removals are not sent as a list. first_t is the oldest timestamp
    still held, and the caller drops anything older - which describes a
    prune of any size, including one that happened while the caller was
    away, in a single number.

    Falls back to a full copy whenever that reasoning does not hold: an
    unparseable since, a caller ahead of us - which is what a restart
    looks like, because the counter starts again - or more points
    missing than are still held, because pruning has overtaken them.
    The caller is told which it got rather than having to guess.
    """
    with _state_lock:
        hist = list(STATE["history"])
        rev = STATE["history_rev"]
        epoch = STATE.get("history_epoch", "")
        generation = PROC_GENERATION + (":" + epoch if epoch else "")

    first_t = hist[0].get("t") if hist else None
    since = request.args.get("since", type=int)
    # A revision we never issued is not a revision. Negative values are
    # the obvious case, and treating one as real turns rev - since into
    # a larger number than the trace has ever held.
    known = since is not None and since >= 0
    same_process = request.args.get("gen") == generation
    behind = rev - since if (known and same_process) else None
    if behind is not None and 0 <= behind <= len(hist):
        # RANGES GO ON BOTH ARMS. A delta carries only the new points,
        # and the page holds a delta response for most of its life - so
        # ranges sent only with a full copy would freeze at whatever
        # they were when the page loaded, which is the sort of wrong
        # that looks right for hours.
        return jsonify({"history_rev": rev, "full": False,
                        "history_generation": generation,
                        "add": hist[len(hist) - behind:],
                        "ranges": history_ranges(hist),
                        "first_t": first_t})
    return jsonify({"history": hist, "history_rev": rev, "full": True,
                    "history_generation": generation,
                    "ranges": history_ranges(hist),
                    "first_t": first_t})


MODE_LABELS = {"auto": "Auto", "manual": "Manual", "on": "Force on"}
SECRET_FIELDS = {("protect", "password"), ("plug", "password"),
                 ("email", "password"), ("ui", "password_hash")}


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
    be = body.get("email") or {}
    if (cand["email"]["host"] != CFG["email"]["host"]
            and be.get("password") in (None, "", "SET")):
        cand["email"]["password"] = ""
        notes.append("Mail server changed, so the SMTP password was "
                     "cleared. Enter it again.")
    return " ".join(notes) or None


@app.route("/api/config", methods=["POST"])
def api_config():
    """Serialise the whole save: snapshot, validate, commit, persist."""
    with _cfg_lock:
        body = _json_body()
        bad = _body_error(body)
        if bad:
            return bad
        if set(body) - {"protect", "plug", "control", "email"}:
            return jsonify(error="Unknown configuration section"), 400
        control = body.get("control")
        if isinstance(control, dict) and control.get("mode") == "on":
            # Hold both locks from the live-fault check through persistence
            # and commit: a newly raised fault cannot enter that gap.
            with _act_lock, _fault_lock:
                with _state_lock:
                    fault = copy.deepcopy(STATE["fault"])
                if fault:
                    token = _fault_override_token(fault)
                    if request.headers.get("X-Fault-Override") != token:
                        return jsonify(
                            error="A safety fault is active. Confirm Force on to override it.",
                            fault_override_required=True,
                            fault_override_token=token), 409
                return _api_config_locked(act_locked=True)
        return _api_config_locked()


def _fault_override_token(fault):
    """Identify this fault episode and process, not mutable delivery state.

    The token is explicit override acknowledgement, not authentication.
    Normal auth and origin checks still apply before this endpoint runs.
    """
    identity = [_PROC_START, fault.get("at"), fault.get("code"), fault.get("msg")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _api_config_locked(act_locked=False):
    """The body of api_config, with _cfg_lock already held.

    Validates against a copy so a rejected save cannot alter live
    settings. That copy is also why the lock is needed: it spans every
    section, so committing it writes back whatever the sections this
    request did not touch looked like when the copy was taken.
    """
    if _stopping.is_set():
        return jsonify(error="Controller is stopping; retry after startup."), 503
    # Include marker creation and disk persistence, not just the memory
    # commit: recovery cleanup must never unlink a newly created promise
    # before its corresponding Manual mode has been committed.
    if not act_locked:
        with _act_lock:
            return _api_config_locked(act_locked=True)
    body = _json_body()
    bad = _body_error(body)
    if bad:
        return bad
    cand = copy.deepcopy(CFG)
    for section in ("protect", "plug", "control", "email"):
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
    try:
        validate_config_relationships(cand)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    summary = _describe_changes(cand)
    # Before save_cfg, not after. The promise has to reach disk BEFORE
    # the mode that creates it, or a crash in between leaves Manual
    # stored with nothing owed and a fan still running. Written even if
    # the plug is unreachable: the promise is what is being recorded,
    # not the stop.
    entering_manual = (cand.get("control", {}).get("mode") == "manual"
                       and CFG["control"].get("mode") != "manual")
    if entering_manual:
        # Finish an older unsaved recovery BEFORE creating a new promise.
        # Locking alone is not enough if this save wins the actuation lock
        # before the background persistence retry does.
        with _fault_lock:
            with _state_lock:
                old = copy.deepcopy(STATE["fault"])
                owed = STATE["fault_unsaved"]
            if (owed and old and old.get("code") in _RELAY_FAULT_CODES
                    and old.get("stopped")
                    and old.get("relay_retry", {}).get("phase") == "recovered"):
                try:
                    save_fault(old)
                except OSError:
                    return jsonify(error="Could not save the previous stop recovery. "
                                         "Manual was not selected; retry once storage recovers."), 503
                with _state_lock:
                    STATE["fault_unsaved"] = False
                _finish_saved_relay_recovery_locked(old)
        try:
            promise_manual_off()
        except OSError as e:
            # InstalledNotDurable is NOT excused here, unlike a config
            # save. This marker exists for exactly one failure - a power
            # cut between committing Manual and making the stop - so a
            # marker that "probably" survives a power cut is not a
            # property worth having. If the mode write then IS durable,
            # the pair is worse than either alone: Manual stored with
            # nothing owed, and a fan left running in the mode whose
            # purpose is to stop it. A marker left behind by a refused
            # change is harmless; startup only honours one while the
            # stored mode is Manual.
            return jsonify(error="Could not durably record the manual "
                                 "stop (%s). Nothing was changed." % e), 500
    # Persist BEFORE committing. The reverse order left a failed write
    # returning 500 while the controller ran on settings that never
    # reached disk - live behaviour and stored behaviour disagreeing
    # until the next restart silently reverted it. cand is a complete
    # config, so writing it is equivalent to writing the merged CFG.
    durable = True
    try:
        save_cfg(cand)
    except InstalledNotDurable:
        # config.json now HOLDS the new settings, so a restart would
        # apply them. Refusing here and leaving CFG on the old values
        # would divide disk from memory and let a change the caller was
        # told had been rejected take effect later, unannounced.
        durable = False
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
    with nullcontext() if act_locked else _act_lock:
        was = CFG["control"].get("mode")
        recovery_changed = any(cand[key] != CFG[key]
                               for key in ("plug", "protect", "control"))
        draw_changed = (cand["plug"] != CFG["plug"] or
                        (was == "on") !=
                        (cand["control"].get("mode") == "on"))
        global _cfg_generation, _oob_generation, _oob_since, _off_draw_since
        old_generation = _cfg_generation
        for section in cand:
            CFG[section] = cand[section]
        _cfg_generation += 1
        if draw_changed:
            _oob_since = None
            _off_draw_since = None
            _oob_generation = _cfg_generation
        elif _oob_generation == old_generation:
            # Still reject every stale client, but do not give an unhealthy
            # fan a fresh grace period for an email, UI or no-op save.
            # Carry only a timer known to belong to the previous settings.
            _oob_generation = _cfg_generation
        now_mode = CFG["control"].get("mode")
        if recovery_changed:
            with _state_lock:
                STATE["cold_start_pending"] = False
                STATE["cold_start_hold_until"] = None
        if now_mode == "manual" and was != "manual":
            # Entering manual switches the fan off once, then hands to the
            # plug's own controls or Google Home without interference.
            with _state_lock:
                STATE["manual_off_pending"] = True
        if was == "on" and now_mode != "on":
            # Leaving a forced state: do not let the minimum-run timer
            # from the forced switch hold the fan on once auto resumes.
            with _state_lock:
                # Cancel only the lease, retaining physical switch timing.
                expired = time.time() - CFG["control"]["min_run_minutes"]*60 - 1
                _stamp_elapsed_locked("min_run_at", expired)
                _elapsed_clocks["min_run_at"] = (expired, time.monotonic() - CFG["control"]["min_run_minutes"]*60 - 1)
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
    if not durable:
        warn = ("Saved and in use, but the disk did not confirm the write "
                "will survive a power cut. Check free space and the "
                "filesystem, then save again.")
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
                        "isConnected": s.get("isConnected"),
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


@app.route("/api/email/test", methods=["POST"])
def api_email_test():
    """Wait within a deadline for one globally bounded test-send worker.

    Deliberately NOT routed through _notify: that returns immediately and
    swallows failures, which is right for the fault path and useless for
    a button whose whole purpose is to show the error. An untested mail
    path is the kind that fails exactly when it is needed.
    """
    global _email_test_pending
    with _cfg_lock:
        e = copy.deepcopy(CFG["email"])
    recipients = _split_recipients(e.get("recipients"))
    if not e.get("enabled"):
        return jsonify(error="Email alerts are switched off."), 400
    missing = [k for k in ("host", "sender") if not e.get(k)]
    if not recipients:
        missing.append("recipients")
    if missing:
        return jsonify(
            error="Not configured yet: %s." % ", ".join(missing)), 400
    with _email_test_lock:
        if _email_test_pending is not None and not _email_test_pending['done'].is_set():
            return jsonify(error="A test email is already in progress. Wait for it to finish."), 409
        outcome = dict(done=threading.Event(), why=None,
                       deadline=time.monotonic()+EMAIL_TEST_SECONDS)
        _email_test_pending = outcome
        def send():
            try:
                outcome['why'] = _send_mail(
                    "Fan controller test message",
                    "This is a test from the fan controller.\n\nIf you are reading "
                    "it, alerts will reach you: a stopped fan, a fault that could not "
                    "be saved, readings drying up, and the all-clear when they "
                    "return.", email_cfg=e, deadline=outcome['deadline'])
            except Exception as exc:
                outcome['why'] = 'SMTP test failed (%s)' % type(exc).__name__
            finally:
                outcome['done'].set()
        try:
            threading.Thread(target=send, daemon=True, name='email-test').start()
        except RuntimeError:
            _email_test_pending = None
            return jsonify(error="Could not start the email test. Try again."), 503
    while not _stopping.is_set():
        remaining = outcome['deadline']-time.monotonic()
        if remaining <= 0:
            break
        if outcome['done'].wait(min(0.1, remaining)):
            if time.monotonic() >= outcome['deadline'] or _stopping.is_set():
                break
            if outcome['why']:
                return jsonify(error="Send failed - %s" % outcome['why']), 400
            return jsonify(ok=True, sent_to=recipients)
    if _stopping.is_set():
        return jsonify(error="Controller is stopping; retry after startup."), 503
    return jsonify(error="Email test timed out; delivery is unconfirmed. "
                   "A new test is blocked until the previous attempt finishes."), 504


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
    return float(v) if _finite(v) else None


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

    manual_off_pending carries a one-time stop that has been promised
    and not yet made. Manual switches the fan off once and then hands
    over; a restart in the gap between selecting Manual and the stop
    landing would otherwise drop the promise, leaving the fan running in
    the one mode whose purpose is to stop touching it. Startup also
    reconciles the durable Manual-stop marker, including after a handover,
    so carried state cannot cancel a committed promise.
    """
    with _state_lock:
        return {
            "v": 1,
            "off_draw_since": _off_draw_since,
            "relay_retry_clock": _relay_retry_clock,
            "plug_ports": _plug_port_handover(),
            "latch": dict(STATE["latch"]),
            "ran_with": dict(STATE["ran_with"]),
            "changed_at": STATE["changed_at"],
            "min_run_at": STATE["min_run_at"],
            "cold_start_pending": STATE["cold_start_pending"],
            "cold_start_hold_until": STATE["cold_start_hold_until"],
            "reading_at": STATE["reading_at"],
            "manual_off_pending": bool(STATE["manual_off_pending"]),
            "oob_since": _oob_since,
            "oob_monotonic": _oob_monotonic,
            "draw_grace": _draw_grace,
            "start_time": START_TIME,
            "elapsed_clocks": dict(_elapsed_clocks),
        }


def _apply_resume(allow_old_monotonic=False):
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
    global _oob_since, _oob_generation, START_TIME, _oob_monotonic, _draw_grace, _off_draw_since
    global _relay_retry_clock
    raw = os.environ.pop(RESUME_ENV, None)
    if not raw:
        return False
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        _log("%s Restart - handover data unreadable, starting clean"
             % FC_RESTART_PROBLEM)
        return False
    if not isinstance(d, dict) or d.get("v") != 1:
        _log("%s Restart - handover data not a version this build "
             "reads, starting clean" % FC_RESTART_PROBLEM)
        return False
    _restore_plug_ports(d.get('plug_ports'))
    latch = d.get("latch")
    with _state_lock:
        if isinstance(latch, dict):
            for k in STATE["latch"]:
                # A real bool, not anything truthy. bool("false") is
                # True, and a latch is what keeps the fan running.
                raw = latch.get(k)
                STATE["latch"][k] = raw if isinstance(raw, bool) else False
        ran = d.get("ran_with")
        if isinstance(ran, dict):
            # Same rule as the latches: a real bool only. This one only
            # shapes a log line rather than the fan, but a restart that
            # invented a rule would misattribute the stop that follows.
            STATE["ran_with"] = {
                k: True for k in STATE["latch"]
                if isinstance(ran.get(k), bool) and ran[k]}
        STATE["changed_at"] = _n(d.get("changed_at"))
        STATE["min_run_at"] = _n(d.get("min_run_at"))
        deadline = _n(d.get("cold_start_hold_until"))
        STATE["cold_start_hold_until"] = (deadline if deadline is not None
            and 0 < deadline <= time.monotonic()+CFG["control"]["cold_start_minutes"]*60
            else None)
        STATE["cold_start_pending"] = d.get("cold_start_pending") is True
        if (STATE["fault"] or CFG["control"]["mode"] != "auto"
                or CFG["control"]["cold_start_minutes"] <= 0 or PLUG_ACCESS_DISABLED):
            STATE["cold_start_pending"] = False
            STATE["cold_start_hold_until"] = None
        STATE["reading_at"] = _n(d.get("reading_at"))
        # A real bool, like the latches: anything truthy would promise a
        # stop that was never owed.
        owed = d.get("manual_off_pending")
        STATE["manual_off_pending"] = owed if isinstance(owed, bool) else False
        held = [k for k, v in sorted(STATE["latch"].items()) if v]
        lease = STATE["min_run_at"] is not None or STATE["changed_at"] is not None
    _oob_since = _n(d.get("oob_since"))
    mono = time.monotonic()
    off_since = _n(d.get("off_draw_since"))
    _off_draw_since = (off_since if off_since is not None and off_since <= mono
                      and (allow_old_monotonic or off_since >= 0) else None)
    retry_clock = d.get("relay_retry_clock")
    with _state_lock:
        fault = STATE["fault"] or {}
        retry = fault.get("relay_retry") or {}
    if (isinstance(retry_clock, list) and len(retry_clock) == 3
            and _valid_fault_timestamp(retry_clock[0])
            and type(retry_clock[1]) is int
            and retry_clock[:2] == [fault.get("at"), retry.get("attempts")]
            and _finite(retry_clock[2]) and 0 <= retry_clock[2] <= mono+60):
        _relay_retry_clock = tuple(retry_clock)
    def clock_pair(value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        a, b = _n(value[0]), _n(value[1])
        return (a, b) if a is not None and b is not None else None
    _oob_monotonic = clock_pair(d.get("oob_monotonic"))
    if (_oob_monotonic is not None and
            (_oob_monotonic[0] != _oob_since or _oob_monotonic[1] > mono)):
        _oob_monotonic = None
    _draw_grace = clock_pair(d.get("draw_grace"))
    if _draw_grace is not None and _draw_grace[1] > mono + SETTLE_SECONDS:
        _draw_grace = None
    # Stamped with the CURRENT generation, not left at None. The
    # replacement loaded the same config.json the outgoing process was
    # running, so the restored timer belongs to this generation - but
    # _oob_generation starts as None, which differs from 0, so the first
    # _draw_check treated the carried timer as belonging to another
    # config and reset it. A draw fault that was 30 seconds from
    # tripping got a fresh grace period on every ./restart.sh, which is
    # precisely the guarantee a requested restart is supposed to make.
    _oob_generation = _cfg_generation if _oob_since is not None else None
    st = _n(d.get("start_time"))
    if st is not None:
        START_TIME = st
    carried = d.get("elapsed_clocks")
    if isinstance(carried, dict):
        for key in ("startup", "changed_at", "min_run_at", "reading_at", "watts_at"):
            pair = clock_pair(carried.get(key))
            wall = START_TIME if key == "startup" else STATE.get(key)
            if pair is not None and pair[0] == wall and pair[1] <= mono:
                _elapsed_clocks[key] = pair
    # Logged because the handover is otherwise invisible: the values it
    # carries are the ones that decide whether the fan keeps running,
    # and without a line here the only way to tell a handover from a
    # clean start is that the fan did not behave oddly.
    _log("Resumed after restart - %s, %s"
         % ("latched: " + ", ".join(held) if held else "no latch held",
            "minimum-run lease carried" if lease else "no lease"))
    return True


def _state_fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def _checkpoint_fault_fingerprint(fault):
    # Email callbacks may finish after the snapshot. They cannot change its
    # safety identity or make an otherwise valid lease checkpoint stale.
    safety = None if fault is None else {k:v for k,v in fault.items()
        if k not in ('attempted_at','delivered_at','relay_notice')}
    return _state_fingerprint(safety)


def _boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id') as stream:
            return stream.read().strip()
    except OSError:
        return None


def _save_stop_state():
    """Opt-in orderly-stop checkpoint, never a periodic snapshot or relay action.

    Freeze configuration, actuation, requests and fault transactions in the
    same order as exec handover. Missing/busy/failed checkpoints mean cold
    start, never permission to lose a separately persisted safety fault.
    """
    acquired = []
    deadline = time.monotonic() + 25
    try:
        for lock in (_cfg_lock, _act_lock, _plug_request_lock, _fault_lock):
            if not lock.acquire(timeout=max(0, deadline-time.monotonic())):
                raise OSError('shutdown checkpoint lock timeout')
            acquired.append(lock)
        with _state_lock:
            owed = STATE['fault_unsaved']
        if owed and not _persist_fault_locked():
            raise OSError('safety fault is not durable')
        if _fault_annotation_dirty is not None:
            with _state_lock:
                fault = copy.deepcopy(STATE['fault'])
            if fault and not _save_annotation(fault):
                raise OSError('fault notification state is not durable')
        with _state_lock:
            snapshot = {'version': 1, 'wall': time.time(), 'mono': time.monotonic(),
                        'boot': _boot_id(), 'config': _state_fingerprint(CFG),
                        'fault': _checkpoint_fault_fingerprint(STATE['fault']),
                        'payload': _resume_payload()}
        write_json_durable(STOP_STATE_PATH, snapshot, mode=0o600)
        print('Orderly-stop state saved.', file=sys.stderr, flush=True)
        return True
    except (OSError, ValueError, TypeError) as exc:
        print('Orderly-stop state not saved: %s; next start will be cold.'
              % type(exc).__name__, file=sys.stderr, flush=True)
        return False
    finally:
        for lock in reversed(acquired):
            lock.release()


def _load_stop_state():
    """Consume one clean-stop checkpoint before threads start.

    On the same kernel boot, elapsed time is monotonic. Across host reboots
    use bounded nonnegative wall downtime; ambiguous or >24h gaps start cold.
    Absolute monotonic stamps are rebased, never compared across hosts.
    """
    if not PERSIST_STOP_STATE or not os.path.lexists(STOP_STATE_PATH):
        return False
    def invalid_constant(_):
        raise ValueError('non-finite checkpoint JSON')
    try:
        with open(STOP_STATE_PATH) as stream:
            raw = stream.read(65537)
        # Consume durably BEFORE applying: a subsequent crash cannot replay it.
        os.unlink(STOP_STATE_PATH)
        _fsync_dir(DATA_DIR)
        if len(raw) > 65536:
            raise ValueError('checkpoint too large')
        record = json.loads(raw, parse_constant=invalid_constant)
        if (not isinstance(record, dict) or type(record.get('version')) is not int
                or record['version'] != 1
                or record.get('config') != _state_fingerprint(CFG)
                or record.get('fault') != _checkpoint_fault_fingerprint(STATE['fault'])
                or not isinstance(record.get('payload'), dict)
                or not _valid_fault_timestamp(record.get('wall'))
                or not _finite(record.get('mono')) or record['mono'] < 0
                or (record.get('boot') is not None and not isinstance(record['boot'], str))):
            raise ValueError('checkpoint does not match current configuration/fault')
        mono = time.monotonic()
        same_boot = _boot_id() is not None and record.get('boot') == _boot_id()
        downtime = (mono-record['mono'] if same_boot else time.time()-record['wall'])
        if not _finite(downtime) or not 0 <= downtime <= 86400:
            raise ValueError('ambiguous checkpoint downtime')
        payload = copy.deepcopy(record['payload'])
        if (payload.get('v') != 1
                or set(payload) != set(_resume_payload())
                or not isinstance(payload.get('latch'), dict)
                or set(payload['latch']) != set(STATE['latch'])
                or any(type(v) is not bool for v in payload['latch'].values())
                or not isinstance(payload.get('ran_with'), dict)
                or set(payload['ran_with']) - set(STATE['latch'])
                or any(type(v) is not bool for v in payload['ran_with'].values())
                or type(payload.get('manual_off_pending')) is not bool
                or type(payload.get('cold_start_pending')) is not bool):
            raise ValueError('invalid checkpoint control state')
        for key in ('changed_at','min_run_at','reading_at','oob_since','start_time'):
            if payload[key] is not None and _n(payload[key]) is None:
                raise ValueError('invalid checkpoint timestamp')
        shift = mono-record['mono']-downtime
        def shifted(value):
            if value is None:
                return None
            if not _finite(value):
                raise ValueError('invalid elapsed clock')
            return value+shift
        payload['off_draw_since'] = shifted(payload.get('off_draw_since'))
        payload['cold_start_hold_until'] = shifted(payload.get('cold_start_hold_until'))
        for key in ('oob_monotonic', 'draw_grace'):
            pair = payload.get(key)
            if pair is not None:
                if (not isinstance(pair, list) or len(pair) != 2
                        or not all(_finite(v) for v in pair)):
                    raise ValueError('invalid clock pair')
                payload[key] = [pair[0], shifted(pair[1])]
        clocks = payload.get('elapsed_clocks', {})
        if not isinstance(clocks, dict):
            raise ValueError('invalid elapsed clocks')
        for key, pair in clocks.items():
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(_finite(v) for v in pair)):
                raise ValueError('invalid acquisition clock')
            clocks[key] = [pair[0], shifted(pair[1])]
        retry = payload.get('relay_retry_clock')
        if retry is not None:
            if not isinstance(retry, list) or len(retry) != 3:
                raise ValueError('invalid retry clock')
            retry[2] = max(mono, shifted(retry[2]))
        # Hold source ports conservatively for their remaining wait after downtime.
        ports = payload.get('plug_ports', [])
        if not isinstance(ports, list):
            raise ValueError('invalid port history')
        for row in ports:
            if not isinstance(row, list) or len(row) != 3 or not _finite(row[2]):
                raise ValueError('invalid port record')
            row[2] = max(0, row[2]-downtime)
        os.environ[RESUME_ENV] = json.dumps(payload, allow_nan=False)
        return _apply_resume(allow_old_monotonic=True)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        os.environ.pop(RESUME_ENV, None)
        _log('%s Stop checkpoint unusable; starting cold' % FC_RESTART_PROBLEM)
        return False


def _announce_owed_manual_stop():
    """Record a durable Manual-mode stop restored during startup."""
    _log("Fan off \u2014 manual stop owed from before restart")


def _restore_manual_stop_promise():
    """Reconcile the durable stop marker even after a successful handover.

    Disk records a committed promise; a carried False cannot cancel it.
    A marker left behind after leaving Manual is not a current stop request.
    Called before threads start, when no configuration update can race it.
    """
    fault = STATE["fault"]
    if fault and _relay_recovered(fault) and fault.get("manual_stop_id"):
        with _act_lock, _fault_lock:
            _finish_saved_relay_recovery_locked(fault)
    if CFG["control"].get("mode") == "manual" and manual_off_promised():
        with _state_lock:
            owed = STATE["manual_off_pending"]
            STATE["manual_off_pending"] = True
        if not owed:
            _announce_owed_manual_stop()


def _announce_start(resumed):
    """Log a cold start before threads run; an execv handover logs separately.

    A cold start drops memory-only latches and the minimum-run lease, but
    a persisted fault remains held. Return the line, or None on handover.
    """
    if resumed:
        return None
    with _state_lock:
        f = STATE["fault"]
        standing = (f.get("code") or "unknown") if f else None
        STATE["cold_start_pending"] = (f is None
            and CFG["control"]["mode"] == "auto" and not PLUG_ACCESS_DISABLED
            and CFG["control"]["cold_start_minutes"] > 0)
        STATE["cold_start_hold_until"] = None
    msg = "%s Started cold" % FC_COLD_START
    # _log attempts the events.json write before it returns, so the
    # line is offered to disk ahead of the first reading rather than
    # riding on the periodic history flush that the 2026-08-18 readings
    # were lost with. ATTEMPTED, NOT GUARANTEED: save_events swallows
    # OSError and does not sync the directory, deliberately, because a
    # lost event is recoverable and does not control the fan. Do not
    # read this as a durability promise.
    _log(msg)
    if standing:
        _log("%s Standing fault still held" % standing)
    return msg


def _consume_restart_request():
    """Take the restart trigger if there is a fresh one.

    Deleting BEFORE the exec, and only going ahead when the delete
    succeeded, is what stops a boot loop. A trigger that cannot be
    removed would otherwise be seen again by the process that replaces
    this one, and again by the next, forever. A failed unlink refuses
    the request and says so rather than retrying.

    Only the exact file present at startup is treated as leftover. A new
    or rewritten trigger is accepted even when a clock correction gives
    it a timestamp earlier than process startup.
    """
    try:
        st = os.stat(RESTART_PATH)
    except OSError:
        return False
    fresh = _restart_identity(st) != _startup_restart_identity
    try:
        os.unlink(RESTART_PATH)
    except OSError as e:
        _log("%s Restart request ignored - could not remove "
             "restart.request (%s)" % (FC_RESTART_PROBLEM, e))
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
        _log("%s Restart - could not enumerate descriptors (%s)"
             % (FC_RESTART_PROBLEM, e))
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
    THE DAILY SUMMARY GOES WITH IT - flush_hist writes both, for the
    same reason and on the same schedule, and a lost day of extremes is
    the one thing here that cannot be reconstructed afterwards.
    config, fault and events are each written atomically as they
    change, so they need nothing here.

    The relay is not touched, and the fan keeps doing whatever it was
    doing. The fault survives in fault.json. The in-memory timing state
    - latches, the minimum-run lease, the draw-fault and stale-reading
    clocks - is handed to the replacement through the environment, so a
    requested restart does not release a latched fan or short-cycle the
    motor. See _resume_payload. A crash or a systemd restart carries
    none of it and correctly starts clean.
    """
    # _fault_lock is held from the check right through the exec.
    #
    # Reading the flag under _state_lock alone was not enough.
    # _store_fault holds _fault_lock across BOTH the memory update and
    # the write, so a restart could land in the middle of raising a
    # fault: live in memory, flag still False because the write had not
    # failed yet, and the exec then discards the only copy. The
    # replacement loads nothing and Auto is free to start a fan that was
    # stopped for drawing wrong.
    #
    # Holding a lock across execv costs nothing, because the process
    # image goes and the lock with it. For the same reason the rule in
    # _store_fault - never extend a _fault_lock hold with an event write
    # - does not apply here: there is no later contender to starve. If
    # the exec fails, the finally releases it on the way out.
    #
    # Bounded, like _persist_fault_on_exit. If a fault transaction is
    # already in flight, refusing and being asked again is better than
    # queueing behind a disk that may be failing.
    # Freeze settings and plug actions before taking the fault lock.
    # This matches configuration/actuation lock ordering and closes the
    # gap between handover capture and exec where Manual could be saved
    # with a stop promise that was absent from the captured handover.
    if _stopping.is_set():
        return False
    acquired = []
    unsaved = True
    try:
        for lock, reason in (
                (_cfg_lock, "settings are being changed"),
                (_act_lock, "a plug action is in progress"),
                (_plug_request_lock, "a plug request is in progress"),
                (_fault_lock, "a fault is being written right now")):
            if not lock.acquire(timeout=2):
                _log("%s Restart refused - %s; try again in a moment"
                     % (FC_RESTART_PROBLEM, reason))
                return False
            acquired.append(lock)
        if _stopping.is_set():
            return False
        with _state_lock:
            unsaved = STATE["fault_unsaved"]
        if not unsaved:
            return _restart_from_here()
    finally:
        for lock in reversed(acquired):
            lock.release()

    # Only reachable when the restart was refused. A failed exec raises
    # instead, and restart_loop logs it.
    _log("%s Restart refused - the active fault is not saved to disk "
         "and would be lost" % FC_RESTART_PROBLEM)
    return False


def _restart_from_here():
    """Hold config, actuation, plug-request and fault locks through exec."""
    _log("Restarting on request")
    if not flush_hist():
        # Worth saying, not worth refusing over: the graph loses at
        # most HIST_FLUSH_SECONDS of points and nothing about the
        # fan's safety rests on them.
        _log("%s Restart - history could not be written, up to %ds of "
             "readings will be lost" % (FC_DISK_FAILED, HIST_FLUSH_SECONDS))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # A signal can arrive during either blocking flush. Never let this worker
    # replace the main thread after it has begun shutdown/checkpointing.
    if _stopping.is_set():
        return False
    return _commit_restart()


def _commit_restart():
    """Only the signal-handling main thread may make the final exec decision.

    The restart worker retains the handover locks until completion. If shutdown
    starts while this callback is queued, release those locks promptly so its
    checkpoint can finish. The queued callback also checks shutdown itself.
    """
    if threading.current_thread() is threading.main_thread():
        return _exec_restart()
    if _restart_dispatch is None:
        return False
    done, result = threading.Event(), {}
    def commit():
        try:
            result['value'] = _exec_restart()
        except BaseException as exc:
            result['error'] = exc
        finally:
            done.set()
    try:
        _restart_dispatch(commit)
    except OSError:
        if _stopping.is_set():
            return False
        raise
    while not done.wait(0.05):
        if _stopping.is_set():
            return False
    if _stopping.is_set():
        return False
    if 'error' in result:
        raise result['error']
    return result.get('value', False)


def _exec_restart():
    """Main-thread commit: signals either cancel it or follow exec to the new process."""
    if _stopping.is_set():
        return False
    # Handed over through the environment, which execv preserves and
    # nothing else does. Set last, so a refusal above never leaves it
    # behind for a later exec to pick up.
    os.environ[RESUME_ENV] = json.dumps(_resume_payload())
    # Must come immediately before the exec: it is what stops the
    # replacement inheriting the bound listening socket.
    _mark_fds_close_on_exec()
    os.chdir(DIR)
    if _stopping.is_set():
        os.environ.pop(RESUME_ENV, None)
        return False
    os.execv(sys.executable, [sys.executable] + sys.argv)


def restart_loop():
    """Watch for the restart trigger.

    A file rather than an endpoint on purpose. Basic auth is inactive
    until a UI password is set, so an HTTP route would let anything on
    the VLAN bounce the controller. A single restart is now cheap - the
    timing state is handed over, so it no longer releases latches or the
    lease - but it still costs a short outage, and nothing rate limits
    it. Writing this file needs shell access as the service user, which
    is already total control.
    """
    while not _stopping.is_set():
        try:
            if _consume_restart_request():
                _restart_now()
        except Exception as e:
            _log("%s Restart watch error - %s" % (FC_RESTART_PROBLEM, e))
        time.sleep(RESTART_POLL_SECONDS)


class _HTTPServerStopped(ExitNow):
    """Use Waitress's explicit exit path after closing all socket channels."""


def _http_restart_dispatch(channels):
    """Every listener has a trigger; the multi-listener wrapper does not."""
    for channel in list(channels.values()):
        dispatch = getattr(getattr(channel, 'trigger', None), 'pull_trigger', None)
        if callable(dispatch):
            return dispatch
    raise RuntimeError('Dashboard listener has no lifecycle trigger')


def _close_http_channels(channels):
    # Close the actual listeners/triggers/connections in the shared map.
    # MultiSocketServer.close() also performs an unbounded worker shutdown;
    # worker cleanup belongs exclusively to our bounded path below.
    for channel in list(channels.values()):
        channel.close()


def _close_http_server(server, channels, dispatcher):
    """Close listeners/channels and bound Waitress worker cleanup.

    Waitress workers are daemon threads. Its own shutdown uses wall time;
    wait on a separate daemon so a clock correction or busy request cannot
    extend the controller's stop indefinitely. State has already been saved
    by the signal handler before this cleanup starts.
    """
    _close_http_channels(channels)
    finished = threading.Event()
    def close_workers():
        try:
            dispatcher.shutdown(timeout=2)
        finally:
            finished.set()
    try:
        threading.Thread(target=close_workers, daemon=True,
                         name='http-shutdown').start()
        if not finished.wait(2.5):
            print('Web worker cleanup deadline reached; exiting.',
                  file=sys.stderr, flush=True)
    except RuntimeError:
        dispatcher.shutdown(timeout=0)


def _make_http_server(host=None, port=8096):
    from waitress.server import create_server
    from waitress.task import ThreadedTaskDispatcher
    channels = {}
    dispatcher = ThreadedTaskDispatcher()
    server = None
    try:
        # Keep one process. Bind successfully before starting any controller
        # loops or web workers, so a port collision cannot run a second fan.
        server = create_server(app, map=channels, _dispatcher=dispatcher,
                               host=host or os.environ.get('FAN_CONTROLLER_BIND', '127.0.0.1'),
                               port=port, threads=4,
                               max_request_body_size=app.config['MAX_CONTENT_LENGTH'],
                               clear_untrusted_proxy_headers=False)
        # Waitress must leave the original socket peer intact. Our outer
        # allowlist runs before ProxyFix and owns all forwarded-header trust.
        dispatcher.set_thread_count(4)
        return server, channels, dispatcher
    except BaseException:
        _close_http_server(server, channels, dispatcher)
        raise


def _run_controller():
    server, channels, dispatcher = _make_http_server()
    try:
        _start_controller(server, channels)
    except _HTTPServerStopped:
        pass
    finally:
        _stopping.set()
        _close_http_server(server, channels, dispatcher)


def _start_controller(server, channels):
    global _restart_dispatch
    # Before any thread starts, so the first decide() sees the lease and
    # the latches the previous process was holding.
    resumed = _apply_resume() or _load_stop_state()
    # Immediately after, and before anything else can log: this line is
    # what explains every line that appears below it.
    _announce_start(resumed)
    if PLUG_ACCESS_DISABLED:
        _log("Plug access disabled by local interlock - no plug requests")
    # A durable promise must also be honoured after handover. The carried
    # state supplements disk; it must not cancel an outstanding stop.
    _restore_manual_stop_promise()
    # A fault that outlived the restart. Said out loud because
    # restarting is not how one gets cleared, and whoever just restarted
    # may well have been trying exactly that. Before the threads, so it
    # cannot race the first draw cycle.
    _notify_restart_fault()
    atexit.register(flush_hist)
    atexit.register(_persist_fault_on_exit)
    def stop(signum, frame):
        if _stopping.is_set():
            return
        _stopping.set()
        _wake.set()
        # Response flushing catches Exception, including ExitNow. Empty the
        # async map first so even a swallowed exit cannot leave idle sockets
        # keeping the event loop alive after controller loops have stopped.
        _close_http_channels(channels)  # Stop all listeners before checkpointing.
        try:
            flush_hist(signum, frame)
        except SystemExit:
            # server.run catches SystemExit itself and performs an unbounded
            # wall-clock-based cleanup. Use our bounded cleanup instead.
            raise _HTTPServerStopped()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, stop)
    _restart_dispatch = _http_restart_dispatch(channels)
    threading.Thread(target=loop, daemon=True, name='fan-control').start()
    threading.Thread(target=watts_loop, daemon=True, name='fan-power').start()
    threading.Thread(target=restart_loop, daemon=True, name='fan-restart').start()
    print('Serving dashboard with Waitress (one process, four web threads).',
          file=sys.stderr, flush=True)
    server.run()
    if not _stopping.is_set():
        raise RuntimeError('Dashboard server stopped unexpectedly')


if __name__ == "__main__":
    _run_controller()
