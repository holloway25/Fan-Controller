"""Run every test module. Exit status is non-zero if anything failed.

    ./venv/bin/python tests/run_all.py

Each module runs as its own process: they import controller.py under a
throwaway directory, so sharing one interpreter would share a single
already-imported module.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    failed = []
    for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
        name = os.path.basename(path)
        print("== %s" % name)
        sys.stdout.flush()
        if subprocess.call([sys.executable, path]) != 0:
            failed.append(name)
        print("")
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("everything passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
