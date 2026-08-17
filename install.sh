#!/usr/bin/env bash
#
# Install or update this controller on the machine it is checked out on.
#
#     ./install.sh              # set up, then print what to run as root
#     ./install.sh --unit       # also write and enable the systemd unit
#
# WHAT THIS IS FOR. The steps have lived in the README and in one
# person's memory, which was tolerable while there was one box. There
# are two, a third is planned on a second SD card to test Ubuntu 26.04,
# and "did I remember to chmod the config" is not a question anyone
# should have to answer from memory on a machine that switches mains.
#
# IDEMPOTENT ON PURPOSE. Run it on a fresh checkout or an existing
# install; it never overwrites config.json, never downgrades a venv it
# did not make, and says what it skipped rather than doing nothing
# quietly.
#
# IT DOES NOT RESTART A RUNNING CONTROLLER. Updating dependencies under
# a live process is safe - Python has already imported what it needs -
# and choosing when the fan service restarts belongs to a person, not
# to an install script.
set -u

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

WANT_UNIT=0
for arg in "$@"; do
  case "$arg" in
    --unit) WANT_UNIT=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- root
# NOT AS ROOT. Everything below would end up owned by root - the venv,
# config.json, history.json - and the service runs as an ordinary user,
# so it would then be unable to write its own state. This is the single
# most likely way to produce an install that looks fine and cannot
# start.
if [ "$(id -u)" = "0" ]; then
  die "run this as the user the service will run as, not as root.
         Only the --unit step needs privilege, and it asks for it."
fi

WHO="$(id -un)"
WHERE="$PWD"

step "Installing as $WHO in $WHERE"

# -------------------------------------------------------------- python
step "Python"
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || die "python3 is not on PATH."

# 3.10 is the floor: click, requests and urllib3 in requirements.txt all
# declare requires-python >=3.10, so the pinned set cannot install below
# it. Checked here rather than left to pip, whose error for this is a
# wall of resolver output.
if ! "$PY3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  die "python3 is $("$PY3" -V 2>&1), and 3.10 is the floor."
fi
say "$("$PY3" -V 2>&1) at $PY3"

# ----------------------------------------------------------------- venv
step "Virtual environment"
if [ -x venv/bin/python ]; then
  say "venv/ exists, reusing it"
else
  "$PY3" -m venv venv || die "could not create venv/.
         On Debian and Ubuntu this usually means python3-venv is missing:
             sudo apt install python3-venv"
  say "created venv/"
fi

step "Dependencies"
# Quiet unless something goes wrong: a wall of resolver output hides the
# one line that matters.
if ! out="$(./venv/bin/pip install -q -r requirements.txt 2>&1)"; then
  printf '%s\n' "$out" >&2
  die "pip install failed."
fi
say "installed from requirements.txt"

# --------------------------------------------------------------- config
step "Configuration"
if [ -e config.json ]; then
  say "config.json exists, left alone"
else
  cp config.example.json config.json || die "could not write config.json"
  say "created config.json from the example"
fi

# 0600 EVERY TIME, not only on creation. It holds the Protect password,
# the plug password, the SMTP password and the UI hash; a config copied
# in by hand or restored from a backup arrives with whatever mode it
# had, and this is the one file where that matters.
chmod 600 config.json || die "could not chmod config.json"
say "config.json is 0600"

# ----------------------------------------------------------------- unit
step "systemd unit"
UNIT_SRC="office-fan-controller.service.example"
UNIT="office-fan-controller.service"

# Generated rather than copied, because the example deliberately carries
# a generic user and path - the concrete ones are site detail and stay
# out of the repository. Both the generated file and config.json are
# gitignored.
#
# NOT sed. Two ways that went wrong, both reported by review of
# 3ea51b3:
#
#   A PATH CONTAINING A SPACE produced an ExecStart systemd splits into
#   a command and an argument - "/opt/fan control/venv/bin/python"
#   became the command "/opt/fan" - so the unit was silently invalid
#   until the first start attempt.
#
#   AN AMPERSAND in a path is the whole match in a sed replacement, so
#   a directory with one in its name produced a mangled line rather
#   than an error.
#
# awk with the values in variables never interprets them, and systemd
# takes a quoted first token for exactly this case. The quotes go
# around the executable only: everything after it is an argument.
GROUP="$(id -gn)"
awk -v who="$WHO" -v grp="$GROUP" -v dir="$WHERE" '
  /^User=/            { print "User=" who; next }
  /^Group=/           { print "Group=" grp; next }
  /^WorkingDirectory=/{ print "WorkingDirectory=" dir; next }
  /^ExecStart=/       { print "ExecStart=\"" dir "/venv/bin/python\" controller.py"; next }
  { print }
' "$UNIT_SRC" > "$UNIT" || die "could not write $UNIT"
say "wrote $UNIT for $WHO in $WHERE"

# Verified when systemd is present to verify it. A unit that is wrong
# in a way only the first start reveals is the worst kind: the install
# looks finished.
if command -v systemd-analyze >/dev/null 2>&1; then
  if out=$(systemd-analyze verify "./$UNIT" 2>&1); then
    say "systemd-analyze verify passed"
  else
    printf '%s\n' "$out" >&2
    die "the generated unit does not verify."
  fi
else
  say "systemd-analyze not present, unit not verified"
fi

if [ "$WANT_UNIT" = "1" ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemctl is not on PATH."
  sudo cp "$UNIT" /etc/systemd/system/ || die "could not install the unit"
  sudo systemctl daemon-reload || die "daemon-reload failed"
  sudo systemctl enable office-fan-controller || die "enable failed"
  say "installed and enabled"
  say "NOT STARTED. Fill in config.json first, then:"
  say "    sudo systemctl start office-fan-controller"
else
  say "not installed - run with --unit, or do it yourself:"
  say "    sudo cp $UNIT /etc/systemd/system/"
  say "    sudo systemctl daemon-reload"
  say "    sudo systemctl enable office-fan-controller"
fi

# ----------------------------------------------------------------- hook
step "Commit hook"
if [ -d .git ]; then
  git config core.hooksPath .githooks && say "core.hooksPath is .githooks"
else
  say "not a git checkout, skipped"
fi

step "Done"
say "Next: fill in config.json, or start the service and use the dashboard."
say "Reach it over an SSH tunnel until a reverse proxy exists:"
say "    ssh -L 8096:127.0.0.1:8096 $WHO@<this-host>"
say ""
say "Check the install with:"
say "    ./venv/bin/python tests/run_all.py"
