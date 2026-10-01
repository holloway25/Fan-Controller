# Development checks

Tests are development and CI tools, not routine work for a live production fan.
Run the affected checks for a change; documentation-only edits need link,
formatting and privacy checks, not a controller or browser suite.

## Controller suite

From the repository root:

```sh
./venv/bin/python tests/run_all.py          # fast tier
./venv/bin/python tests/run_all.py --all    # every module
./venv/bin/python tests/run_all.py --slow   # only the slow three
```

Modules use temporary state and do not contact Protect or the plug. The slow
tier contains `test_js.py`, `test_install_sh.py` and `test_restart_sh.py`.
Installer tests create real virtual environments and install packages on the
machine running them; they are not appropriate routine production checks.

For focused work, run an affected module directly, for example:

```sh
./venv/bin/python tests/test_fault_acknowledgement.py
```

## Dashboard and mutation checks

Dashboard changes need `tests/test_js.py`: source assertions alone cannot prove
interactive behaviour. Its browser tests need the pinned jsdom dependency
installed with `npm ci`. Layout still needs visual testing because jsdom has
no layout engine.

Curated mutation checks deliberately alter copies of the code and verify that
the named regression tests detect those changes:

```sh
./venv/bin/python tools/mutate.py
```

These are separate from the normal suite. Run relevant checks during development;
CI runs the complete curated set on its selected x86 Python job.

## Privacy and CI

Before committing:

```sh
git diff --check
./venv/bin/python tools/scan_secrets.py --worktree
./venv/bin/python tools/scan_secrets.py --staged
```

Stage changes before the staged scan. Check that the diff contains only the
intended files. Never commit live configuration, credentials, local units,
private addresses or the deployment handoff.

GitHub CI runs the full suite on x86 and ARM64, including browser, installer,
restart and ShellCheck coverage. Privacy checks run separately. A hosted ARM64
runner does not reproduce Pi performance or attached hardware; see
[ARM limitations](arm.md#remaining-hardware-validation).
