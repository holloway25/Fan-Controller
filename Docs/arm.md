# Running on ARM

The Raspberry Pi 3B+ is a first-class target, not a best-effort one.
This file records what has actually been measured on it, how to repeat
those measurements, and what is still unknown.

First measured 2026-08-13 on a Pi 3 Model B Plus Rev 1.3, 64-bit
Raspberry Pi OS (Debian 13 trixie), kernel 6.18, aarch64, Python
3.13.5, 905MB usable, on an idle board.

The **suite** figures were remeasured 2026-08-14 at d50a754 on the same
board, with a small heatsink fitted. The import and parse timings have
NOT been retaken since 08-13 — they carry the older date for a reason
and should not be quoted as if they were refreshed.

## Summary

| | x86 VM | Pi 3B+ | ratio |
|---|---|---|---|
| `import flask` | 74.9 ms | 805 ms | 10.8x |
| parse a ~189KB `history.json` | 1.86 ms | 31 ms | 16.8x |
| service RSS, 4 threads | ~49 MB | ~44 MB | - |
| full test suite | - | 217/217 pass, ~67s | - |

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

Measured 2026-08-14 with the service running against a live sensor and
an accumulating trace, rather than idle:

| | RSS | threads | history held | uptime |
|---|---|---|---|---|
| Pi 3B+ | 44508 kB (~43.5 MB) | 4 | 97 points | 21 min |
| x86 VM | 50572 kB (~49.4 MB) | 4 | 2869 points | 20h 46m |

**THESE ARE NOT LIKE FOR LIKE**, and the difference is mostly the trace:
one held a full 24 hours and the other twenty minutes of one. Expect the
ARM figure to rise as its trace fills. Re-measure after 24 hours for a
comparable number.

The earlier ARM figure was 40480 kB, taken with no `history.json` to
load, no accumulated events and no sensor connection, and this file
called it a floor that should land nearer the VM number in service. That
prediction held: 40.5 MB idle became 43.5 MB in service within twenty
minutes of real operation.

The VM figure has also moved. The ~45 MB previously recorded here
predates the 2026-08-13 resize and is superseded.

Either way it is not a constraint. 44 MB against 905 MB usable leaves
the board at roughly 200 MB used in total, with 700 MB available.

## The test suite on ARM

All 13 modules, 217 tests, pass. No failures and no skips.

Remeasured 2026-08-14 at d50a754. These figures move as the suite
grows, so they carry the commit they were taken at: a timing without
one is not a measurement, it is a memory. The previous version of this
table read 182 tests across 12 modules at 729c936, and was CORRECT for
that commit — `test_structure.py` arrived in 3a65643, after it. It went
stale rather than wrong, which is the failure this section's own rule
about carrying the commit is meant to make visible.

Per-module times are from the second of two consecutive passes, each
module invoked the way `run_all.py` invokes it — its own interpreter
process. The second pass ran marginally SLOWER than the first, which is
the opposite of a cold-cache effect: the board was warmer. Board
temperature went 54.8°C at the start to 59.1°C at the end, so the 60°C
soft limit never engaged and nothing here is thermally capped.

| module | tests | time |
|---|---|---|
| `test_data` | 14 | 2.0s |
| `test_dashboard` | 6 | 0.2s |
| `test_control` | 20 | 1.6s |
| `test_structure` | 4 | 1.0s |
| `test_plug_parse` | 22 | 1.7s |
| `test_email` | 44 | 8.5s |
| `test_history` | 14 | 3.9s |
| `test_failures` | 12 | 4.1s |
| `test_actuation` | 17 | 3.8s |
| `test_concurrency` | 13 | 7.7s |
| `test_durability` | 16 | 5.8s |
| `test_restart_sh` | 5 | 16.4s |
| `test_restart` | 30 | 10.0s |
| **total** | **217** | ~67s end to end |

**One module runs at a time — `run_all.py` already guarantees that.**
It starts each module as its own process with `subprocess.call`, which
blocks until that module exits, so the suite never runs modules
concurrently. It is the standard way to run the full suite and it is
safe here: a complete sequential pass of all 217 tests takes about 67
seconds on a 3B+, with 227 MB in use at peak against 905 MB available,
and load never above 0.20.

**That peak is LOWER than the ~300 MB recorded before it, despite 35
more tests, and the reason matters.** Every earlier memory figure here
was taken while the test harness leaked a tempfile directory per
`load()`. `/tmp` is tmpfs, so those directories were MEMORY, and the old
peaks were partly measuring the leak rather than the suite. c9460f5
fixed it, and 2026-08-14 was the first ARM run to confirm the fix: 217
tests finished with `/tmp` at 1 MB and no `fanctl-test-*` directories
left behind. Before that commit that board had accumulated about 600 of
them — 162 MB — across two runs. Do not compare a post-c9460f5 peak
against a pre-c9460f5 one and conclude anything about the suite.

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

`test_restart_sh` is the slow one — 16 s for 5 tests, over half again as
long as `test_restart` with 30 — because it spawns real bash and waits
on real sleeps. It has not hung. Timing multipliers do not apply to it:
what it spends is wall clock on `sleep`, which no amount of CPU
shortens.

## Power and thermals

Measured 2026-08-14 on the same 3B+, powered over 802.3af PoE through
a splitter supplying 5V 2.4A via micro-USB.

Under three minutes of deliberate saturation — four busy loops plus a
continuous 40 MB fsynced `dd` write loop, load 5.19 — the board reported:

- **no under-voltage, at any point.** Neither the live bit (`0x1`) nor
  the sticky since-boot bit (`0x10000`) ever set. 2.4A is ample: a
  headless 3B+ draws roughly 0.4A idle and up to about 1.3A with all
  cores busy, and the official 2.5A recommendation budgets for
  downstream USB peripherals this board does not have.
- **no ARM frequency capping** (`0x2`/`0x20000`) and **no hard
  throttling** (`0x4`/`0x40000`).
- the 60°C **soft** temperature limit engaging about ten seconds in
  (`0x8`, and `0x80000` once set), temperature climbing 51.5°C to a
  75.2°C plateau — below the 80°C point where progressive throttling
  begins, and below the 85°C defined maximum.

So on this board power is not the constraint; sustained thermals are,
and only under loads this service never generates. The suite itself
peaks around 59°C and never trips the soft limit.

Three caveats, because these numbers are easy to overread:

- **The 5V input was never measured.** `vcgencmd measure_volts core`
  reads the SoC core rail, not the supply — a reading of 1.33V during
  the run is the normal turbo voltage bump on the jump from 700 MHz to
  1400 MHz. Input voltage is not readable in software; the
  under-voltage flag is a proxy for it. A meter at the board end under
  load is still the only direct measurement.
- **Do not claim the clock held at 1400 MHz.** `scaling_cur_freq`
  reported 1400000 throughout, but that is the cpufreq view and
  firmware-level soft capping need not surface there. `vcgencmd
  measure_clock arm` is the right probe and was not the one used.
- **The historical bits from `0x10000` through `0x80000` are sticky
  until reboot**, `0x10000` included. A board will keep reporting one
  long after the condition has passed, so read a baseline immediately
  after a reboot and check again after any power change.

## What is still unknown

- **No long-running soak on ARM.** Everything measured is minutes and
  hours, not days. Nothing is known about drift or growth over weeks.
- **The plain 3B is untested.** Same design, slightly slower; expected
  to work, not verified.
- **SD card wear** was assessed on x86 and found to be three orders of
  magnitude away from mattering. That analysis has not been redone for
  a Pi's write patterns, though nothing about the write path changes.
  A high-endurance card widens the margin further, but endurance says
  nothing about torn writes: the durable-write design is what covers
  power loss, not the card.
- **The service has not been verified controlling a fan on ARM.** The
  Protect path, the plug read path, the restart mechanism and stability
  have been exercised on this board; a full Auto cycle driving a real
  fan has not.
- **ARM is not in CI.** Every figure in this file came from running the
  suite on the board BY HAND, so an architecture or deployment
  regression is caught only when someone remembers to look. Worth
  knowing before reaching for GitHub's hosted `ubuntu-*-arm` runners as
  the fix: they are Ubuntu userland with Ubuntu's Python, while this
  board is Debian trixie on Python 3.13 with piwheels configured. A
  hosted runner would cover the ARCHITECTURE and none of the
  DEPLOYMENT. Note also that CI's matrix is 3.10, 3.11 and 3.14, so
  3.13 — what this board actually runs — is tested nowhere automatic.
- **No power measurement under a real long run.** The saturation test
  above is three minutes of synthetic load, not days of service.
