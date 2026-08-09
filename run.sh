#!/usr/bin/env bash
# Start the controller unless it is already running. Safe to call repeatedly.
# Works from any directory, and prefers a venv over vendored libs.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
# Already running? "controller.py" alone is too generic to match on - another
# service of that name would suppress our start - and the cmdline cannot be
# anchored, since the unit invokes the script relatively and the no-venv
# fallback runs it as plain "python3". Confirm the working directory instead.
for _pid in $(pgrep -f "[c]ontroller\.py" 2>/dev/null); do
  [ "$(readlink -f /proc/$_pid/cwd 2>/dev/null)" = "$DIR" ] && exit 0
done
if [ -x "$DIR/venv/bin/python" ]; then
  PY="$DIR/venv/bin/python"
else
  PY="python3"
  export PYTHONPATH="$DIR/libs"
fi
setsid --fork "$PY" controller.py >> controller.log 2>&1 < /dev/null
