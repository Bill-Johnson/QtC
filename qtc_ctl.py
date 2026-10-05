#!/usr/bin/env python3
# QtC v0.15.0-beta — qtc_ctl.py  (built 2026-09-20)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# Development / QA only — client for the --test-api control socket.
# Not shipped in releases (see the pack excludes in CLAUDE.md).
#
# Usage:
#   ./qtc_ctl.py status
#   ./qtc_ctl.py view terminal
#   ./qtc_ctl.py wait "de N0CALL>" 120
#   ./qtc_ctl.py -f steps.txt        # one command per line, # for comments
#
# In a step file: `#` comments, `{now}`/`{date}` expand to a timestamp, and
# a leading `!` inverts the check — `!wait View: Mail 3` passes when that
# line does NOT appear, which is how the "nothing happened" tests assert.
#   ./qtc_ctl.py                     # interactive
#   ./qtc_ctl.py -w 45 status        # wait up to 45 s for QtC to come up
#
# Exit status is 0 when every command answered ok, 1 otherwise — so a
# script of steps can be run from a shell and actually checked.

import json
import socket
import sys
import time
from datetime import datetime

HOST, PORT = "127.0.0.1", 8787


_RUN_STARTED = datetime.now()


def expand(line):
    """{now} / {date} in a step file become a timestamp, so a message sent
    to yourself is uniquely identifiable when it comes back down off the
    BBS on the next connect. Fixed once per run: D16 at 1200 baud
    (2026-10-01) queued "173659" and then looked for "173812", because
    each line used to take its own clock reading."""
    if "{" not in line:
        return line
    now = _RUN_STARTED
    return (line.replace("{now}", now.strftime("%H%M%S"))
                .replace("{date}", now.strftime("%Y-%m-%d")))


def send(sock, line):
    sock.sendall((line + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("QtC closed the control socket")
        buf += chunk
    return json.loads(buf.decode())


def show(line, res, negate=False):
    """A step prefixed with `!` is expected to FAIL — that is how a test
    asserts that something did NOT happen, which is most of what the
    "nothing went on the air" checks are made of."""
    ok = bool(res.get("ok"))
    if negate:
        ok = not ok
    mark = "ok " if ok else "ERR"
    body = {k: v for k, v in res.items() if k != "ok"}
    rendered = json.dumps(body, indent=2) if len(json.dumps(body)) > 70 \
        else json.dumps(body)
    print(f"[{mark}] {'!' if negate else ''}{line}\n      {rendered}")
    return ok


def connect(port, wait):
    """Open the control socket, optionally retrying while QtC starts.

    QtC takes 10-20 s to build its main window, and longer on a cold cache.
    A fixed sleep either wastes time or — worse — declares it dead too
    early, and the natural next move is to launch a SECOND instance, which
    then dies on "Address already in use" (2026-09-20). Poll instead.
    """
    deadline = time.time() + max(wait, 0)
    while True:
        try:
            return socket.create_connection((HOST, port), timeout=10)
        except OSError as e:
            if time.time() >= deadline:
                raise e
            time.sleep(0.5)


def main(argv):
    port, wait = PORT, 0
    while len(argv) > 2 and argv[1] in ("-p", "-w", "--wait"):
        if argv[1] == "-p":
            port = int(argv[2])
        else:
            wait = float(argv[2])
        argv = [argv[0]] + argv[3:]

    try:
        sock = connect(port, wait)
    except OSError as e:
        print(f"Cannot reach the QtC test API on {HOST}:{port} — {e}")
        if wait:
            print(f"Waited {wait:g}s. Check whether QtC actually started.")
        print("Start QtC with:  python3 main_window.py --test-api")
        return 2
    sock.settimeout(None)      # `wait` can legitimately block for minutes

    all_ok = True
    with sock:
        if len(argv) > 2 and argv[1] == "-f":
            for raw in open(argv[2], encoding="utf-8"):
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                negate = line.startswith("!")
                line = expand(line.lstrip("!").strip())
                all_ok &= show(line, send(sock, line), negate)
                if not all_ok:
                    print("      — stopping, a step failed")
                    break
        elif len(argv) > 1:
            raw = " ".join(argv[1:])
            negate = raw.startswith("!")
            line = expand(raw.lstrip("!").strip())
            all_ok = show(line, send(sock, line), negate)
        else:
            print("QtC control — 'help' for commands, Ctrl-D to leave")
            while True:
                try:
                    line = input("qtc> ").strip()
                except EOFError:
                    print()
                    break
                if line:
                    negate = line.startswith("!")
                    line = expand(line.lstrip("!").strip())
                    show(line, send(sock, line), negate)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
