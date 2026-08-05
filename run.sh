#!/usr/bin/env bash
# Start co2fan unless it is already running. Safe to call repeatedly (cron).
cd "$HOME/co2fan" || exit 1
pgrep -f "[c]o2fan.py" >/dev/null 2>&1 && exit 0
setsid --fork env PYTHONPATH="$HOME/co2fan/libs" python3 co2fan.py \
  >> co2fan.log 2>&1 < /dev/null
