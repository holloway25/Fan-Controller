# Running on ARM

The Raspberry Pi 3B+ is a first-class target, not a best-effort one.
This file records what has actually been measured on it, how to repeat
those measurements, and what is still unknown.

First measured 2026-08-13 on a Pi 3 Model B Plus Rev 1.3, 64-bit
Raspberry Pi OS (Debian 13 trixie), kernel 6.18, aarch64, Python
3.13.5, 905MB usable, on an idle board.

## Summary

| | x86 VM | Pi 3B+ | ratio |
|---|---|---|---|
| `import flask` | 74.9 ms | 805 ms | 10.8x |
| parse a ~189KB `history.json` | 1.86 ms | 31 ms | 16.8x |
| service RSS, 4 threads | ~45 MB | ~40 MB | - |
| full test suite | - | 155/155 pass, 246MB peak | - |

Nothing in the code is architecture specific. No source change has ever
been needed to run on ARM.

## Use the 64-bit image

Both the 3B and 3B+ are ARMv8, so a 32-bit image boots — but do not use
one. Wheel coverage on PyPI is much better for `aarch64`, and this is
not hypothetical: MarkupSafe publishes `aarch64` wheels for every
supported Python and no `armv7l` wheels at all. A 32-bit image builds it
from source and needs a compiler.

On 64-bit, all twelve pinned dependencies install as wheels with
**nothing built from source**. Ten are architecture-neutral; MarkupSafe
and charset-normalizer arrive as `aarch64` manylinux builds. This was
verified against PyPI directly, not just as a claim.

## Prerequisites

Only one package beyond a default image:

```
sudo apt install -y python3-venv
```

Debian splits the bundled pip wheels out of `ensurepip`, so
`python3 -m venv` fails without it. Do **not** install `build-essential`
or `python3-dev` — if something needs a compiler, that is a finding
worth investigating, and having a toolchain present hides it.

### A pip trap specific to Raspberry Pi OS

The image ships `/etc/pip.conf` with:

```
[global]
extra-index-url=https://www.piwheels.org/simple
```

`--index-url https://pypi.org/simple` **does not override this** — the
extra index survives and piwheels still serves most packages. To
genuinely test PyPI coverage, bypass the config file:

```
PIP_CONFIG_FILE=/dev/null ./venv/bin/pip install \
    --index-url https://pypi.org/simple -r requirements.txt
```

Any claim about where a dependency came from on a Pi is worthless
without this.

## Timings, and how to repeat them

```
./venv/bin/python -X importtime -c "import flask" 2>&1 | tail -1
./venv/bin/python -m timeit -n 5 -r 3 -s "import json" \
    "json.load(open('history.json'))"
```

Run three times on an idle board; discard the first, which is
cold-cache. Record the board, image, Python version and the **point
count** of the history file — a parse time without the point count means
nothing.

Startup is dominated by imports, not history. Even a genuinely cold
start reads and parses history in about 41 ms against 805 ms of
imports.

### The restart ceiling is not in question

`restart.sh` allows 60 s for a new process to answer. On x86 that takes
0.26 s; at these multipliers the 3B+ is roughly 3–4.4 s, leaving 14–21x
headroom. The multiplier would have to approach 200 before this
mattered.

### Parse cost is CPU, not the SD card

The parse is a larger share of startup on ARM (~3.9%) than on x86
(~2.5%). **This is not slow storage**, which is the obvious wrong
conclusion. Splitting the file read from the parse, with pages evicted
between runs:

```
warm   read  0.6 ms | parse 30.6 ms
cold   read 10.7 ms | parse 30.5 ms
```

Parse is flat whether the file comes off the card or out of cache. The
card costs ~10 ms for 189KB (~18 MB/s) and all of it lands in the read.
`HIST_CAP` does not need revisiting on storage grounds.

To evict a single file's pages without root:

```python
fd = os.open(path, os.O_RDONLY)
os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
os.close(fd)
```

This is more targeted than `drop_caches` and needs no privileges. If
you repeat this on another board, **split the read from the parse
before blaming storage.**

## Memory

About 40 MB resident with four threads, against ~45 MB on the VM.

Treat the ARM figure as a **floor**: it was taken with no `history.json`
to load, no accumulated events, and no sensor connection, so none of
those structures existed. In service, expect it nearer the VM number.
Either way it is not a constraint on a 1GB board.

## The test suite on ARM

All 11 modules, 155 tests, pass. No failures and no skips.

| module | tests | time |
|---|---|---|
| `test_data` | 12 | <1s |
| `test_dashboard` | 6 | <1s |
| `test_control` | 20 | <1s |
| `test_plug_parse` | 22 | <1s |
| `test_history` | 14 | 3s |
| `test_failures` | 12 | 3s |
| `test_actuation` | 15 | 4s |
| `test_concurrency` | 5 | 4s |
| `test_durability` | 16 | 5s |
| `test_restart_sh` | 5 | 16s |
| `test_restart` | 28 | 9s |
| **total** | **155** | peak 246 MB, peak load 0.26 |

**One module runs at a time — `run_all.py` already guarantees that.**
It starts each module as its own process with `subprocess.call`, which
blocks until that module exits, so the suite never runs modules
concurrently. It is the standard way to run the full suite and it is
safe here: a complete sequential pass peaks at 246 MB and never took
load above 0.26 on a 3B+.

**What is not safe is two suite runs at once.** That is what once drove
the x86 VM into swap exhaustion — a second full run launched while the
first was still going, on a single core with no headroom to absorb it.
Before relaunching anything that looks stuck, check it is actually
gone:

```
pgrep -af run_all
```

While iterating on one area, running just that module is quicker than
the full suite — but that is convenience, not a safety rule.

`test_restart_sh` is the slow one — 16 s for 5 tests, nearly twice
`test_restart` with 28 — because it spawns real bash and waits on real
sleeps. It has not hung.

## What is still unknown

- **No long-running soak on ARM.** Everything above is minutes, not
  days. Nothing is known about drift or growth over weeks.
- **The plain 3B is untested.** Same design, slightly slower; expected
  to work, not verified.
- **SD card wear** was assessed on x86 and found to be three orders of
  magnitude away from mattering. That analysis has not been redone for
  a Pi's write patterns, though nothing about the write path changes.
- **The service has not yet run in production on ARM.** These are
  measurements on a test board, not operational experience.
