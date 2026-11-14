#!/usr/bin/env python3
"""Hold a steady TUNE tone on a VARA-protocol modem while you set levels.

    python3 modem_tune.py              # modem command port 8300, -15 dBFS
    python3 modem_tune.py --port 8400 --level -20

Keys the transmitter. Type a new level (e.g. -12) + Enter to change it,
or just Enter / q to stop. Re-sends TUNE every 45 s so the modem's 60 s
safety timer doesn't cut the tone; always sends TUNE OFF on the way out.
Mercury and VARA HF use this syntax. The modem takes one client, so this
bumps any other host (fake BBS, QtC) off its command port while it runs.
"""
import argparse, select, socket, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8300)
ap.add_argument("--level", type=int, default=-15, help="dBFS, -60..0")
a = ap.parse_args()

s = socket.create_connection((a.host, a.port))
s.settimeout(0.2)


def send(cmd):
    s.sendall((cmd + "\r").encode())
    time.sleep(0.3)
    try:
        reply = s.recv(1024).decode(errors="replace").replace("\r", " ").strip()
    except socket.timeout:
        reply = ""
    print(f"  {cmd} -> {reply}", flush=True)


level = max(-60, min(0, a.level))
send(f"TUNE {level}")
print("Tone ON. New level (-60..0) + Enter to change; Enter or q to stop.",
      flush=True)
last = time.time()
try:
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 1.0)
        if r:
            line = sys.stdin.readline().strip().lower()
            if line in ("", "q"):
                break
            try:
                level = max(-60, min(0, int(line)))
            except ValueError:
                print("  a number like -12, or Enter to stop", flush=True)
                continue
            send(f"TUNE {level}")
            last = time.time()
        elif time.time() - last > 45:
            send(f"TUNE {level}")          # restart the 60 s safety timer
            last = time.time()
except KeyboardInterrupt:
    pass
finally:
    send("TUNE OFF")
    s.close()
    print("Tone OFF.", flush=True)
