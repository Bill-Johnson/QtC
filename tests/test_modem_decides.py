"""Let the modem decide (2026-11-15).

Data2G's author saw QtC time out calls his modem was still retrying: QtC
ABORTed at a fixed 90 s while Data2G was keying up and listening. For the
experimental modems (profile modem_decides=True) the modem's own
DISCONNECTED ends a call or a session; QtC's clocks only restart while the
modem reports activity (PTT, BUSY, MODE...) on its command port, with an
outer cap for a modem that never goes quiet. VARA keeps its fixed clocks.

Checks:
  * profiles: VARA modem_decides False, Data2G / Mercury / VardeFM True
  * connect: a retrying modem that gives up itself -> "station not
    responding" (the modem's verdict), not QtC's "Timed out"
  * connect: the same chatter does NOT keep VARA waiting past its timeout
  * connect: a silent modem still times out (and gets ABORT)
  * connect: connect_cap stops a modem that never stops reporting
  * reads: modem activity keeps a read waiting for late data; VARA
    ignores it; MODEM_ACTIVITY_CAP bounds it
  * login prompt wait: modem activity counts as "not quiet"
"""
import os, sys, socket, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import VaraTransport, VARA_MODEMS
from bbs_session import BBSSession

fails = 0
total = 0
def check(name, cond, detail=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not cond else ""))


class ChattyModem:
    """Answers OK to setup commands. On CONNECT it reports PTT/BUSY every
    `every` s (None = says nothing), and after `give_up` s sends
    DISCONNECTED (None = never)."""
    def __init__(self, every=0.3, give_up=None):
        self.every, self.give_up = every, give_up
        self.lines = []
        self.stop = threading.Event()
        self.ls = []
        for _ in range(2):
            l = socket.socket()
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", 0))
            l.listen(4)
            self.ls.append(l)
        self.cmd_port = self.ls[0].getsockname()[1]
        self.data_port = self.ls[1].getsockname()[1]
        threading.Thread(target=self._accept, args=(1, None), daemon=True).start()
        threading.Thread(target=self._accept, args=(0, self._serve),
                         daemon=True).start()

    def _accept(self, i, serve):
        while True:
            try:
                c, _ = self.ls[i].accept()
            except OSError:
                return
            if serve:
                threading.Thread(target=serve, args=(c,), daemon=True).start()
            else:
                self._keep = c

    def _call(self, c):
        t0 = time.time()
        n = 0
        while not self.stop.is_set():
            if self.give_up is not None and time.time() - t0 >= self.give_up:
                c.sendall(b"PTT OFF\rDISCONNECTED\r")
                return
            if self.every is not None:
                c.sendall([b"PTT ON\r", b"PTT OFF\r", b"BUSY ON\r",
                           b"BUSY OFF\r"][n % 4])
                n += 1
                time.sleep(self.every)
            else:
                time.sleep(0.05)

    def _serve(self, c):
        buf = b""
        while True:
            try:
                b = c.recv(1024)
            except OSError:
                return
            if not b:
                return
            buf += b
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                self.lines.append(line)
                c.sendall(b"OK\r")
                if line.startswith(b"CONNECT"):
                    threading.Thread(target=self._call, args=(c,),
                                     daemon=True).start()
                if line == b"ABORT":
                    self.stop.set()
                    c.sendall(b"DISCONNECTED\r")

    def close(self):
        self.stop.set()
        for l in self.ls:
            l.close()


def make(m, key, timeout, cap=None):
    prof = dict(VARA_MODEMS[key], connect_timeout=timeout)
    if cap is not None:
        prof["connect_cap"] = cap
    t = VaraTransport("127.0.0.1", m.cmd_port, m.data_port, "N0CALL",
                      "N0BBS-1", timeout=timeout, bandwidth="500",
                      profile=prof)
    t._log = lambda d, s: None
    return t


def try_connect(t):
    t0 = time.time()
    try:
        t.connect()
        return "", time.time() - t0
    except ConnectionError as e:
        return str(e), time.time() - t0


# 1. Profiles
for k in ("vara_hf", "vara_fm"):
    check(f"{k}: modem_decides off (VARA keeps fixed clocks)",
          VARA_MODEMS[k]["modem_decides"] is False)
for k in ("mercury", "data2g", "vardefm"):
    p = VARA_MODEMS[k]
    check(f"{k}: modem_decides on, with a connect cap",
          p["modem_decides"] is True and p["connect_cap"] >= 600)
check("Data2G connect silence window outlasts its 55 s of calling",
      VARA_MODEMS["data2g"]["connect_timeout"] >= 120)

# 2. Data2G retrying past QtC's window, then giving up by itself
m = ChattyModem(every=0.3, give_up=3.5)
err, dt = try_connect(make(m, "data2g", timeout=1.5))
check("Data2G: modem's own DISCONNECTED ends the call, not QtC's clock",
      "station not responding" in err and "Timed out" not in err, err)
check("Data2G: QtC waited for the modem (> 3 s) instead of 1.5 s",
      dt >= 3.0, f"{dt:.1f}s")
check("Data2G: no ABORT sent - the modem decided",
      not any(ln == b"ABORT" for ln in m.lines))
m.close()

# 3. VARA: the same chatter does not stretch its fixed timeout
m = ChattyModem(every=0.3, give_up=None)
t = VaraTransport("127.0.0.1", m.cmd_port, m.data_port, "N0CALL", "N0BBS-1",
                  timeout=1.5, bandwidth="500", profile=VARA_MODEMS["vara_hf"])
t.timeout = 1.5
t._log = lambda d, s: None
err, dt = try_connect(t)
check("VARA: still times out on its fixed clock", "Timed out" in err, err)
check("VARA: activity did not extend the wait", dt < 4.0, f"{dt:.1f}s")
m.close()

# 4. Silent modem -> safety net trips and ABORTs
m = ChattyModem(every=None, give_up=None)
err, dt = try_connect(make(m, "data2g", timeout=1.5))
check("silent Data2G: safety net times out", "Timed out" in err
      and "no word from Data2G" in err, err)
time.sleep(0.2)
check("silent Data2G: ABORT sent", any(ln == b"ABORT" for ln in m.lines))
m.close()

# 5. Never-quiet modem -> connect_cap
m = ChattyModem(every=0.3, give_up=None)
err, dt = try_connect(make(m, "data2g", timeout=1.5, cap=3))
check("connect_cap stops a modem that never stops calling",
      "still calling" in err and 2.5 <= dt < 7, f"{err} / {dt:.1f}s")
m.close()


# 6. Data-port reads
def read_case(modem_decides, act_for, data_at, timeout=0.6, cap=None):
    """Bump modem activity every 0.2 s for `act_for` s; drop data in at
    `data_at` s (None = never). Returns (text, seconds)."""
    t = VaraTransport("127.0.0.1", 1, 2, "N0CALL", "N0BBS",
                      profile=dict(VARA_MODEMS["data2g"],
                                   modem_decides=modem_decides))
    t.connected = True
    if cap is not None:
        t.MODEM_ACTIVITY_CAP = cap
    t0 = time.time()
    def bump():
        while time.time() - t0 < act_for:
            t._last_modem_activity = time.time()
            time.sleep(0.2)
    def feed():
        time.sleep(data_at)
        with t._data_lock:
            t._data_buf += b"de N0BBS>"
    threading.Thread(target=bump, daemon=True).start()
    if data_at is not None:
        threading.Thread(target=feed, daemon=True).start()
    out = t.read_until(">", timeout=timeout)
    return out, time.time() - t0

out, dt = read_case(True, act_for=3, data_at=2.0)
check("Data2G read: modem activity keeps waiting for a late reply",
      out.endswith(">"), f"{out!r} after {dt:.1f}s")
out, dt = read_case(False, act_for=3, data_at=2.0)
check("VARA read: activity ignored, silence timeout as before",
      out == "" and dt < 1.5, f"{out!r} after {dt:.1f}s")
out, dt = read_case(True, act_for=10, data_at=None, cap=1.5)
check("Data2G read: MODEM_ACTIVITY_CAP bounds endless chatter",
      out == "" and 1.3 <= dt < 3.0, f"{dt:.1f}s")
out, dt = read_case(True, act_for=1.0, data_at=None)
check("Data2G read: gives up soon after the modem goes quiet",
      out == "" and dt < 2.5, f"{dt:.1f}s")


# 7. Login prompt wait (_await_bbs_prompt)
class StubTransport:
    MODEM_ACTIVITY_CAP = 300
    def __init__(self, act_for, prompt_at):
        self.t0 = time.time()
        self.act_for, self.prompt_at = act_for, prompt_at
        self.given = False
    def read_until(self, expected, timeout=2):
        time.sleep(0.2)
        if (self.prompt_at is not None and not self.given
                and time.time() - self.t0 >= self.prompt_at):
            self.given = True
            return "[BPQ-6.0]\rde N0BBS>"
        return ""
    def last_link_activity(self):
        if time.time() - self.t0 < self.act_for:
            return time.time()
        return 0.0
    def send(self, text):
        self.sent = text

s = BBSSession(StubTransport(act_for=3, prompt_at=2.5), mycall="N0CALL")
out = s._await_bbs_prompt(quiet=1.0, nudge=0.5)
check("login: modem activity keeps the prompt wait open",
      "de N0BBS>" in out, repr(out))
st = StubTransport(act_for=0, prompt_at=2.5)
s = BBSSession(st, mycall="N0CALL")
t0 = time.time()
out = s._await_bbs_prompt(quiet=1.0, nudge=0.5)
check("login: with no activity the quiet window still ends it",
      out == "" and time.time() - t0 < 2.0, repr(out))

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
