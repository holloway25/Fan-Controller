"""Run the test modules. Exit status is non-zero if anything failed.

    ./venv/bin/python tests/run_all.py            the fast tier
    ./venv/bin/python tests/run_all.py --all      every module
    ./venv/bin/python tests/run_all.py --slow      the slow tier only

Each module runs as its own process: they import controller.py under a
throwaway directory, so sharing one interpreter would share a single
already-imported module.

TWO TIERS, DIVIDED BY MEASURED COST AND NOTHING ELSE.

Three modules were 61 of the 77 seconds this suite took on an x86-64 VM,
and roughly three of the four minutes it took on the Pi. Everything else
together was about 15 seconds. The split is those three against the rest.

Measured 2026-08-18, x86-64 VM, slowest first:

    test_js.py           24.0s   Node startup and jsdom parsing the page
    test_install_sh.py   23.1s   real virtual environments, real pip
    test_restart_sh.py   14.2s   real bash, real sleeps

The temptation is to sort by relevance instead - "does a developer need
this every cycle" - and that produces a different and worse answer. On
that reasoning test_shellcheck.py and test_mutate_tool.py look like CI
work, and they cost 132ms and 55ms; moving them would save under a fifth
of a second and lose a check on every iteration. Meanwhile
test_dashboard_source.py, the module two separate reviews proposed
trimming hardest, is the CHEAPEST in the suite at 46ms. Relevance and
cost barely correlate. This list is cost.

NOTHING IS DELETED BY BEING SLOW. The slow tier is required:

  - in CI, on every job, which passes --all;
  - on a development machine before a deployment - NOT on the machine
    being deployed to. test_install_sh.py builds real virtual
    environments and runs real package installations wherever it
    executes, which is not work for a board switching mains power;
  - for test_js.py specifically, on any change to templates/index.html,
    because jsdom executing the page is what justifies deleting source
    assertions about it. A dashboard change that skips it has no
    behavioural coverage at all.

tools/mutate.py is separate from every tier and always has been. It
copies the tree once per mutation, so it is CI work on one interpreter.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Filename -> why it is slow. The reason is here so a future reader can
# tell whether it still applies: if test_js stops shelling out to Node,
# this entry is wrong and should go.
SLOW = {
    "test_js.py": "Node startup and jsdom parsing the page",
    "test_install_sh.py": "builds real virtual environments and runs pip",
    "test_restart_sh.py": "spawns real bash and waits on real sleeps",
}


def modules():
    return sorted(os.path.basename(p)
                  for p in glob.glob(os.path.join(HERE, "test_*.py")))


def main(argv):
    every = "--all" in argv
    slow_only = "--slow" in argv
    if every and slow_only:
        print("--all and --slow are contradictory; pick one")
        return 2
    unknown = [a for a in argv[1:] if a not in ("--all", "--slow")]
    if unknown:
        print("unknown option: %s" % " ".join(unknown))
        return 2

    have = modules()

    # A NAME IN SLOW THAT DOES NOT EXIST IS AN ERROR, not something to
    # skip past. A list of filenames that silently tolerates a rename is
    # how a module ends up in neither tier and stops running anywhere,
    # while every run still reports success.
    stale = sorted(set(SLOW) - set(have))
    if stale:
        print("SLOW names a module that does not exist: %s" % ", ".join(stale))
        print("Fix the list in tests/run_all.py.")
        return 2

    if every:
        chosen, tier = have, "every module"
    elif slow_only:
        chosen, tier = [m for m in have if m in SLOW], "the slow tier"
    else:
        chosen, tier = [m for m in have if m not in SLOW], "the fast tier"

    print("running %s (%d of %d)" % (tier, len(chosen), len(have)))
    if not every and not slow_only:
        for name in sorted(SLOW):
            print("  not run: %-20s %s" % (name, SLOW[name]))
        print("  run these with --all on a development machine or in CI.")
    print("")
    sys.stdout.flush()

    failed = []
    for name in chosen:
        print("== %s" % name)
        sys.stdout.flush()
        if subprocess.call([sys.executable, os.path.join(HERE, name)]) != 0:
            failed.append(name)
        print("")

    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    if every:
        print("everything passed")
    else:
        print("%s passed" % tier)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
