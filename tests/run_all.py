#!/usr/bin/env python3
# QtC — offline test harness runner  (built 2026-09-20)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# Development / QA only. Excluded from the release tarball and QtC.spec.
#
# Runs every test_*.py beside this file. No radio, no modem, no BBS — these
# drive the shipping classes directly, so they are free to run and safe to
# run at any time. Use them as the first step of /qtc-api_test and as a
# pre-release gate before packing.
#
#   python3 tests/run_all.py            run everything
#   python3 tests/run_all.py -v         show each harness's own output
#   python3 tests/run_all.py rx km      run only harnesses matching a word
#
# Exit status is 0 only when every harness passed, so it can gate a build.

import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    picks = [a for a in argv[1:] if not a.startswith("-")]

    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if picks:
        files = [f for f in files
                 if any(p.lower() in os.path.basename(f).lower() for p in picks)]
    if not files:
        print("No harnesses matched." if picks else "No harnesses found.")
        return 2

    width = max(len(os.path.basename(f)) for f in files)
    results, started = [], time.time()

    for f in files:
        name = os.path.basename(f)
        print(f"{name:<{width}}  ", end="", flush=True)
        t0 = time.time()
        proc = subprocess.run([sys.executable, f],
                              capture_output=True, text=True)
        dt = time.time() - t0
        ok = proc.returncode == 0

        # Each harness prints its own "n/m passed" as its last line.
        tail = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        summary = tail[-1] if tail else "(no output)"
        print(f"{'PASS' if ok else 'FAIL'}  {dt:5.1f}s  {summary}")
        results.append((name, ok, proc))

        if verbose or not ok:
            for line in proc.stdout.splitlines():
                print(f"       {line}")
            if proc.stderr.strip():
                for line in proc.stderr.strip().splitlines():
                    print(f"       ! {line}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} harnesses passed "
          f"in {time.time() - started:.1f}s")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
