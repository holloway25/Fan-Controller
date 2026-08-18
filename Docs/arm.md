# Raspberry Pi and ARM

The primary ARM target is a Raspberry Pi 3B+ running a 64-bit Debian-based
image. A plain 3B is expected to work but is not verified. x86-64 Debian and
Ubuntu remain first-class targets.

## Requirements

Install the venv package supplied by the distribution:

```sh
sudo apt install -y python3-venv
```

Then use the normal installer from the repository root:

```sh
./install.sh
```

Use a 64-bit image. The 3B and 3B+ CPUs support ARMv8, and the pinned
dependencies have substantially better wheel coverage on `aarch64` than on a
32-bit ARM userland. A compiler should not be required for the pinned set.

## Raspberry Pi OS package source

Raspberry Pi OS may configure piwheels as an additional Python package index in
`/etc/pip.conf`. To verify that dependencies are available directly from
PyPI, bypass the system pip configuration:

```sh
PIP_CONFIG_FILE=/dev/null ./venv/bin/pip install \
  --index-url https://pypi.org/simple -r requirements.txt
```

This is a diagnostic check, not required for a normal installation.

## Resource expectations

The controller is I/O-bound and does not need a newer Pi. A 3B+ has enough
memory for the service and development test suite, but is substantially slower
than a modern x86-64 system for Python imports and JSON parsing.

Operational guidance:

- Keep the default 24-hour history window; startup time grows with the history
  file because it is parsed at launch.
- Avoid source-built dependencies on the Pi.
- Do not run multiple full test suites at once.
- Use `./restart.sh`; its timeout has ample headroom for the slower board.
- Use durable storage and clean power. Durable writes protect configuration,
  faults, pending Manual stops and daily summaries from interrupted renames.

The service's routine writes are modest. Power-loss corruption is a more
important concern than write endurance.

## Testing

The normal development suite is:

```sh
./venv/bin/python tests/run_all.py          # the fast tier
./venv/bin/python tests/run_all.py --all    # every module
```

The default run omits three modules that account for most of the
runtime. On this board that is the difference between roughly one minute
and roughly four. Use `--all` before a deployment; the omitted modules
include the installer tests, which are the least appropriate thing to run
on a controller in service.

The installer does not run tests. GitHub Actions provides ARM64 architecture
coverage using an Ubuntu runner, but that runner is not a Raspberry Pi and does
not validate the Pi's memory, thermals, SD-card behaviour, Raspberry Pi OS
packaging, or attached hardware.

Run tests on the Pi when a change affects Python/runtime compatibility,
installation, service startup, persistence, timing, or hardware interaction.
Routine documentation and test-only changes can be validated in CI or a
development environment.

The curated mutation harness is intentionally a CI/development task and should
not be part of routine Pi deployment:

```sh
./venv/bin/python tools/mutate.py
```

## Useful measurements

When collecting performance figures, record:

- board model and revision
- OS architecture and release
- Python version
- commit
- history point count
- whether the first/cold run was discarded

Example commands:

```sh
./venv/bin/python -X importtime -c "import flask" 2>&1 | tail -1
./venv/bin/python -m timeit -n 5 -r 3 -s "import json" \
  "json.load(open('history.json'))"
vcgencmd get_throttled
vcgencmd measure_temp
```

Do not infer input voltage from `vcgencmd measure_volts core`; it reports the
SoC core rail. The throttling flags are the useful software-visible power
indicator, and their historical bits remain set until reboot.

## Remaining hardware validation

Keep these limitations explicit when assessing a release:

- a hosted ARM64 runner does not reproduce Pi 3B+ performance
- the plain Pi 3B is not verified
- short tests do not replace a long-running hardware soak
- a real fan, Protect console, plug, DNS and mail relay remain integration
  dependencies that unit tests cannot reproduce
