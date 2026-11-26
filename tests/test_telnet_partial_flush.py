"""Telnet-only quiet-spell flush (2026-11-26).

A BBS menu app ends a CR-less prompt in "..." — not > : ? — so the
Terminal view held it until the user typed something. The Telnet reader
now prints a half-line after PARTIAL_FLUSH_S of silence. Checks, through
the real reader thread against a fake server:
  1. the "..." prompt shows within ~PARTIAL_FLUSH_S + 0.5 s, with no input
  2. a line split across two TCP sends 0.3 s apart is still ONE line
  3. read_until() still sees the prompt in the protocol buffer
  4. nothing is flushed while terminal mode is off
  5. a held half-line is printed when the BBS closes the connection
     before the timer runs out (one menu app hangs up after 1 s)
"""
import os, socket, threading, time, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import TelnetTransport

PROMPT = "Press [Enter] to restore master menu loop configuration..."


def run(script, terminal_mode=True, wait=4.0, close_after=None):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        for delay, part in script:
            time.sleep(delay)
            c.sendall(part)
        time.sleep(wait + 1 if close_after is None else close_after)
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    lines = []
    t = TelnetTransport("127.0.0.1", port)
    t._log = lambda tag, msg: lines.append((time.monotonic(), tag, msg))
    t.set_terminal_mode(terminal_mode)
    t0 = time.monotonic()
    t.connect()
    got = t.read_until("configuration...", timeout=wait)
    time.sleep(max(0, wait - (time.monotonic() - t0)))
    t.disconnect()
    srv.close()
    return t0, [(ts - t0, m) for ts, tag, m in lines if tag == "RX"], got


fails = 0
def check(name, ok, extra=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))

# 1 + 3: the "..." prompt appears on its own, no input sent
t0, rx, got = run([
    (0.1, b"[+] Session Terminated. Returning to Gateway Module...\r\n"),
    (0.0, PROMPT.encode()),
])
msgs = [m for _, m in rx]
hit = [ts for ts, m in rx if m == PROMPT]
check("'...' prompt shown without any input", bool(hit), repr(msgs))
if hit:
    limit = 0.1 + TelnetTransport.PARTIAL_FLUSH_S + 0.8
    check(f"shown within {limit:.1f} s", hit[0] <= limit, f"at {hit[0]:.2f} s")
check("read_until still matches the prompt", "configuration..." in got)

# 2: a TCP-split line still reassembles
_, rx, _ = run([
    (0.1, b"MYNODE:N0CALL-7} Invalid comm"),
    (0.3, b"and - Enter ? for command list\r\n"),
], wait=3.5)
msgs = [m for _, m in rx]
check("split line stays one line",
      msgs == ["MYNODE:N0CALL-7} Invalid command - Enter ? for command list"],
      repr(msgs))

# 4: terminal mode off → no [RX] output at all
_, rx, _ = run([(0.1, PROMPT.encode())], terminal_mode=False, wait=3.5)
check("no flush with terminal mode off", rx == [], repr(rx))

# 5: BBS hangs up 1 s after the half-line — before the 2 s timer
_, rx, _ = run([
    (0.1, b"[+] Session Terminated. Returning to Gateway Module...\r\n"),
    (0.0, b"Goodbye from the Wall..."),
], wait=2.5, close_after=1.0)
hit = [ts for ts, m in rx if m == "Goodbye from the Wall..."]
check("held half-line printed when the BBS closes", bool(hit),
      repr([m for _, m in rx]))
if hit:
    check("printed at the close, not after the timer", hit[0] < 1.9,
          f"at {hit[0]:.2f} s")

print(f"\n{7 - fails}/7 passed")
sys.exit(1 if fails else 0)
