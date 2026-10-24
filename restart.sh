#!/usr/bin/env bash
# Ask the controller to restart itself. No privilege required.
#
# Writes restart.request, waits for the controller to consume it, and
# proves a replacement process actually started. A restart is REFUSED
# while a fault is live but unsaved, because the replacement would load
# fault.json, find nothing, and let Auto start a fan that was stopped
# for drawing wrong.
# Busy settings, plug actions or fault writes can also temporarily refuse
# a restart. Those cases need a later retry, not fault acknowledgement.
#
# Exit 0  restarted, and proved
# Exit 1  refused, or it never came back
# Exit 2  the controller is not running, or never took the request
#
# sudo systemctl restart is the fallback, but see the warning in
# README.md: a signalled stop cannot refuse, so it makes one last
# attempt to save the fault and then goes ahead either way.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="$DIR/restart.request"
EVENTS="$DIR/events.json"
URL="http://127.0.0.1:8096/healthz"
# Allows upgrading an older running process that predates /healthz. With an
# enabled proxy-only allowlist, the new /healthz path is the local exception.
LEGACY_URL="http://127.0.0.1:8096/api/state"
# Overridable so the tests can exercise the failure paths without
# waiting a minute for each one. Not intended for ordinary use.
TAKE_TIMEOUT=${RESTART_TAKE_TIMEOUT:-15}  # wait for the trigger to be taken
BACK_TIMEOUT=${RESTART_BACK_TIMEOUT:-60}  # wait for a replacement to answer

# The process generation. Changes on every start, including an execv.
# Everything else is circumstantial: the port answering proves nothing,
# because a failed exec leaves the ORIGINAL process serving on it.
generation() {
  { curl -fsS --max-time 5 "$URL" 2>/dev/null \
      || curl -fsS --max-time 5 "$LEGACY_URL" 2>/dev/null; } \
    | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("process_started_at") or "")
except Exception:
    print("")' 2>/dev/null
}

before=$(generation)
if [ -z "$before" ]; then
  echo "The controller is not answering on 8096, or is too old to" >&2
  echo "report process_started_at. Nothing to restart." >&2
  exit 2
fi

# Identify refusal records, not byte counts or a fragment of the log:
# unrelated events and bounded-log replacement can both change its size.
refusals() {
  python3 -c 'import hashlib,json,re,sys
try:
    with open(sys.argv[1]) as stream:
        events = json.load(stream)
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        msg = event.get("msg")
        if not isinstance(msg, str) or not re.match(r"^(?:FC-\d{3} )?Restart refused\b",msg):
            continue
        key = hashlib.sha256(json.dumps([event.get("at"),msg]).encode()).hexdigest()
        rows.append((key,msg))
    if sys.argv[2] == "snapshot":
        print("\n".join(key for key,msg in rows))
    else:
        previous = set(sys.argv[3].splitlines())
        for key,msg in rows:
            if key not in previous:
                print(msg)
                break
except (OSError,ValueError,TypeError):
    pass' "$EVENTS" "$1" "${2:-}"
}
# Recorded before the trigger so an old refusal is not blamed on this request.
refusals_before=$(refusals snapshot)

: > "$REQ"
echo "Requested a restart; waiting for it to be taken..."

waited=0
while [ -e "$REQ" ]; do
  if [ "$waited" -ge "$TAKE_TIMEOUT" ]; then
    rm -f "$REQ"
    echo "The trigger was never read after ${TAKE_TIMEOUT}s. Is the" >&2
    echo "restart watcher running? Expect three threads, not two." >&2
    exit 2
  fi
  sleep 1
  waited=$((waited + 1))
done

# Consumed. Now prove a new process is answering, rather than trusting
# that the trigger disappearing meant anything.
waited=0
while :; do
  now=$(generation)
  if [ -n "$now" ] && [ "$now" != "$before" ]; then
    echo "Restarted, and confirmed: a new process is serving."
    echo "The activity log should show 'Restarting on request' followed"
    echo "by 'Resumed after restart'."
    exit 0
  fi
  if [ "$waited" -ge "$BACK_TIMEOUT" ]; then
    break
  fi
  sleep 2
  waited=$((waited + 2))
done

# It never came back as a new process. Say which of the two it was,
# because they need different fixes. The refusal check is last and is
# only a hint: if the disk is unwritable the refusal may not have been
# logged either.
refusal=$(refusals reason "$refusals_before")
if [ -n "$refusal" ]; then
  printf 'REFUSED: %s\n' "$refusal" >&2
  case "$refusal" in
    *"not saved to disk"*)
      echo "Clear the fault, or fix whatever is stopping fault.json being" >&2
      echo "written, then try again. Do not force it with systemctl." >&2 ;;
    *"try again in a moment"*)
      echo "The controller is busy. Try again later; nothing was restarted." >&2 ;;
    *)
      echo "Check the refusal reason above before trying again." >&2 ;;
  esac
  exit 1
fi

if [ -n "$(generation)" ]; then
  echo "The ORIGINAL process is still serving after ${BACK_TIMEOUT}s." >&2
  echo "The exec did not happen. Either it was refused and the refusal" >&2
  echo "could not be logged, or the exec failed - check:" >&2
else
  echo "Nothing is answering on 8096 after ${BACK_TIMEOUT}s." >&2
  echo "The exec may have failed without systemd catching it. Check:" >&2
fi
echo "  journalctl -u office-fan-controller -n 50" >&2
exit 1
