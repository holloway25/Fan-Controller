#!/usr/bin/env bash
# Start the controller unless it is already running. Safe to call repeatedly.
# Works from any directory, and prefers a venv over vendored libs.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
pgrep -f "[c]ontroller.py" >/dev/null 2>&1 && exit 0
if [ -x "$DIR/venv/bin/python" ]; then
  PY="$DIR/venv/bin/python"
else
  PY="python3"
  export PYTHONPATH="$DIR/libs"
fi
setsid --fork "$PY" controller.py >> controller.log 2>&1 < /dev/null
