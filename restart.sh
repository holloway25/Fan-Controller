#!/usr/bin/env bash
# Ask the controller to restart itself. No privilege required.
#
# Writes restart.request, waits for the controller to consume it, and
# reports what happened. A restart is REFUSED while a fault is live but
# unsaved, because the replacement would load fault.json, find nothing,
# and let Auto start a fan that was stopped for drawing wrong.
#
# Exit 0  restarted
# Exit 1  refused, or it never came back
# Exit 2  the controller is not running, or never took the request
#
# sudo systemctl restart is the fallback, but see the warning in
# README.md: a signalled shutdown cannot refuse, so it makes the last
# attempt to save the fault and then goes ahead either way.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="$DIR/restart.request"
EVENTS="$DIR/events.json"
URL="http://127.0.0.1:8096/api/state"
TAKE_TIMEOUT=15   # seconds to wait for the trigger to be consumed
BACK_TIMEOUT=60   # seconds to wait for it to answer again

if ! curl -fsS -o /dev/null --max-time 5 "$URL"; then
  echo "The controller is not answering on 8096. Nothing to restart." >&2
  exit 2
fi

# Recorded before the trigger so a refusal from an earlier attempt is
# not mistaken for this one.
before=$(wc -c < "$EVENTS" 2>/dev/null || echo 0)

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

# Consumed. Either it restarted, or it refused - and a refusal is
# logged, so look there rather than guessing from timing.
sleep 1
if [ "$(wc -c < "$EVENTS" 2>/dev/null || echo 0)" -ne "$before" ] \
   && head -c 400 "$EVENTS" 2>/dev/null | grep -q "Restart refused"; then
  echo "REFUSED: a fault is live and not saved to disk." >&2
  echo "Clear the fault, or fix whatever is stopping fault.json being" >&2
  echo "written, then try again. Do not force it with systemctl." >&2
  exit 1
fi

waited=0
until curl -fsS -o /dev/null --max-time 5 "$URL"; do
  if [ "$waited" -ge "$BACK_TIMEOUT" ]; then
    echo "It did not answer within ${BACK_TIMEOUT}s. Check:" >&2
    echo "  journalctl -u office-fan-controller -n 50" >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

echo "Restarted."
echo "Confirm the handover landed - the activity log should show"
echo "'Restarting on request' followed by 'Resumed after restart'."
