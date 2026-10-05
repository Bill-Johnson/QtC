"""VARA reads stop when VARA says DISCONNECTED (2026-10-04).

On air, Windows exe, VARA FM: the link came up, the BBS sent its banner,
VARA delivered only '[BPQ-6.0.25.36-IHJ', then the link failed and VARA
said DISCONNECTED at 16:04:57. VaraTransport._link_lost() always returned
None, so the login read ignored it: it sent its 'A' nudge, ran out its
60 s silence timer, and popped "Login failed" over the next session Bill
had already started. AGW already raised; VARA now does the same.

A fake VARA listens on a command and a data port on 127.0.0.1 and plays
the real sequence through the shipping VaraTransport and BBSSession.
"""
import os, sys, socket, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import VaraTransport
import bbs_session as bs

fails = 0
def check(name, cond, extra=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {extra}" if extra and not cond else ""))


class FakeVara:
    """Answers CONNECT with CONNECTED, then does what the test tells it."""
    def __init__(self):
        self.socks, self.ports = [], []
        self.cmd = self.data = None
        self.ready = threading.Event()
        for _ in range(2):
            l = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", 0))
            l.listen(1)
            self.socks.append(l)
            self.ports.append(l.getsockname()[1])
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.cmd, _ = self.socks[0].accept()
        self.data, _ = self.socks[1].accept()
        buf = b""
        while b"CONNECT " not in buf:
            chunk = self.cmd.recv(1024)
            if not chunk:
                return
            buf += chunk
        self.say("CONNECTED N0CALL N0CALL-1 NARROW")
        self.ready.set()

    def say(self, line):
        self.cmd.sendall((line + "\r").encode())

    def send_data(self, text):
        self.data.sendall(text.encode())

    def close(self):
        for s in self.socks + [self.cmd, self.data]:
            try:
                s.close()
            except (OSError, AttributeError):
                pass


def make(vara):
    t = VaraTransport("127.0.0.1", vara.ports[0], vara.ports[1],
                      "N0CALL", "N0CALL-1", timeout=10,
                      bandwidth="NARROW", vara_type="fm")
    log = []
    t._log = lambda d, txt: log.append(f"[{d}] {txt}")
    s = bs.BBSSession(t, "N0CALL")
    s._log = lambda d, txt: log.append(f"[{d}] {txt}")
    return t, s, log


# ── 1. Link dies during the login wait: the 2026-10-04 sequence ─────────
vara = FakeVara()
t, s, log = make(vara)
result = {}
def run_login():
    t0 = time.time()
    try:
        result["ok"] = s.connect_and_login()
    except ConnectionError as e:
        result["err"] = str(e)
    result["secs"] = time.time() - t0
th = threading.Thread(target=run_login, daemon=True)
th.start()
vara.ready.wait(10)
time.sleep(0.5)
vara.send_data("[BPQ-6.0.25.36-IHJ")          # partial banner, then nothing
time.sleep(1.0)
t_drop = time.time()
vara.say("DISCONNECTED")
th.join(15)
gone = time.time() - t_drop

check("login read stops after DISCONNECTED (no 60 s wait)",
      not th.is_alive() and gone < 5, f"took {gone:.1f}s")
check("it raises ConnectionError rather than returning 'Login failed'",
      "err" in result and "ok" not in result, str(result))
check("the error says VARA ended the link",
      "VARA reports the link to N0CALL-1 is gone" in result.get("err", ""),
      result.get("err", ""))
check("no 'A' nudge typed into a dead link",
      not any("sending 'A'" in l for l in log))
check("the reason is logged as it happens",
      any("[SYS] VARA reports the link" in l for l in log))
vara.close()


# ── 2. Normal login still works (no false trips while linked) ───────────
vara = FakeVara()
t, s, log = make(vara)
result = {}
th = threading.Thread(target=run_login, daemon=True)
th.start()
vara.ready.wait(10)
time.sleep(0.5)
vara.send_data("[BPQ-6.0.25.36-IHJ")
time.sleep(0.7)
vara.send_data("M$]\rde N0CALL>")             # rest of the banner
th.join(15)
check("a banner split across VARA frames still logs in",
      result.get("ok") is True and t.connected, str(result))
check("_link_lost() is None while linked", t._link_lost() is None)


# ── 3. Bytes that arrived before the drop are still delivered ───────────
vara.send_data("Message: 860 Bid:  860_N0CALL Size: 50\rde N0CALL>")
time.sleep(0.5)
vara.say("DISCONNECTED")
deadline = time.time() + 5
while t.connected and time.time() < deadline:
    time.sleep(0.05)
got = t.read_until(">", timeout=5)
check("a read still matches what was buffered before DISCONNECTED",
      "Message: 860" in got and got.endswith(">"), repr(got))
try:
    t.read_until(">", timeout=5)
    raised = False
except ConnectionError:
    raised = True
check("the next read raises at once instead of waiting", raised)
vara.close()


# ── 4. A fresh connect clears the old reason ────────────────────────────
vara = FakeVara()
t, s, log = make(vara)
t._lost_reason = "stale"
th = threading.Thread(target=lambda: t.connect(), daemon=True)
th.start()
vara.ready.wait(10)
th.join(10)
check("connect() starts with no lost reason", t._lost_reason is None)
vara.close()

print(f"\nFAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
