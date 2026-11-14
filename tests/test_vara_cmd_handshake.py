"""VARA-protocol command handshake against a strict, Mercury-style modem
(2026-11-14).

Mercury ends a command line at CR only, so QtC's old CR+LF put the LF at
the front of the next command and every command after the first got
WRONG. This fake modem parses the same way and answers like Mercury.

Checks:
  * every command reaches the modem clean and gets OK (CR-only endings)
  * VaraControl.send also uses CR only
  * MYCALL WRONG / CONNECT WRONG raise a clear ConnectionError
  * a BW WRONG is only a warning and the connect goes on
  * a connect timeout sends ABORT (the modem stops calling)
"""
import os, sys, socket, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import VaraTransport, VaraControl

fails = 0
total = 0
def check(name, cond):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")


class StrictModem:
    """Two listening ports. The cmd port splits lines at CR only, exactly
    like Mercury's process_control_bytes(), and records each line raw."""
    def __init__(self, wrong=(), answer_connect=True):
        self.wrong = set(wrong)          # command words to answer WRONG
        self.answer_connect = answer_connect
        self.lines = []
        self.ls = []
        for _ in range(2):
            l = socket.socket()
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", 0))
            l.listen(4)
            self.ls.append(l)
        self.cmd_port = self.ls[0].getsockname()[1]
        self.data_port = self.ls[1].getsockname()[1]
        threading.Thread(target=self._cmd, daemon=True).start()
        threading.Thread(target=self._data, daemon=True).start()

    def _data(self):
        while True:
            try:
                c, _ = self.ls[1].accept()
            except OSError:
                return
            self._keep = c

    def _cmd(self):
        while True:
            try:
                c, _ = self.ls[0].accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        c.sendall(b"REGISTERED OLD\r")        # Mercury greets a new client
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
                txt = line.decode()
                word = txt.split(" ")[0]
                known = word in ("BW500", "BW2300", "MYCALL", "CONNECT",
                                 "ABORT", "DISCONNECT", "NARROW", "WIDE")
                if not known or word in self.wrong:
                    c.sendall(b"WRONG\r")
                    continue
                c.sendall(b"OK\r")
                if word == "MYCALL":
                    c.sendall(b"REGISTERED " + line[7:] + b"\r")
                if word == "CONNECT":
                    c.sendall(b"PTT ON\r")
                    if self.answer_connect:
                        parts = txt.split()
                        c.sendall(f"PTT OFF\rCONNECTED {parts[1]} {parts[2]} 500\r"
                                  .encode())
                if word == "ABORT":
                    c.sendall(b"PTT OFF\rDISCONNECTED\r")

    def close(self):
        for l in self.ls:
            l.close()


def make(m, timeout=5):
    t = VaraTransport("127.0.0.1", m.cmd_port, m.data_port,
                      "N0CALL", "N0BBS-1", timeout=timeout, bandwidth="500")
    t.log_lines = []
    t._log = lambda d, s: t.log_lines.append((d, s))
    return t


# 1. Clean handshake, every command OK
m = StrictModem()
t = make(m)
try:
    t.connect()
    ok = True
except ConnectionError as e:
    ok = False
    print("   ", e)
check("connect succeeds against a CR-only modem", ok and t.connected)
check("no command line carries a stray LF",
      all(b"\n" not in ln for ln in m.lines))
check("commands arrive in order BW500, MYCALL, CONNECT",
      [ln.split(b" ")[0] for ln in m.lines[:3]]
      == [b"BW500", b"MYCALL", b"CONNECT"])
rx = [s for d, s in t.log_lines if d == "RX-CMD"]
check("no WRONG from the modem", "WRONG" not in rx)
t.abort()
m.close()

# 2. MYCALL rejected -> clear error, no CONNECT sent
m = StrictModem(wrong={"MYCALL"})
t = make(m)
try:
    t.connect()
    err = ""
except ConnectionError as e:
    err = str(e)
check("MYCALL WRONG raises a callsign error", "MYCALL" in err)
check("no CONNECT after MYCALL WRONG",
      not any(ln.startswith(b"CONNECT") for ln in m.lines))
m.close()

# 3. CONNECT rejected -> clear error
m = StrictModem(wrong={"CONNECT"})
t = make(m)
try:
    t.connect()
    err = ""
except ConnectionError as e:
    err = str(e)
check("CONNECT WRONG raises a refused error", "refused CONNECT" in err)
m.close()

# 4. BW rejected -> warning only, connect goes on
m = StrictModem(wrong={"BW500"})
t = make(m)
try:
    t.connect()
    ok = True
except ConnectionError:
    ok = False
sys_lines = [s for d, s in t.log_lines if d == "SYS"]
check("BW WRONG does not stop the connect", ok and t.connected)
check("BW WRONG is reported", any("did not accept" in s for s in sys_lines))
t.abort()
m.close()

# 5. Nobody answers -> timeout sends ABORT
m = StrictModem(answer_connect=False)
t = make(m, timeout=2)
t0 = time.time()
try:
    t.connect()
    err = ""
except ConnectionError as e:
    err = str(e)
check("connect times out", "Timed out" in err)
time.sleep(0.2)
check("timeout sends ABORT to the modem",
      any(ln == b"ABORT" for ln in m.lines))
check("timeout returns promptly (ABORT wait is short)", time.time() - t0 < 6)
m.close()

# 6. VaraControl.send uses CR only too
m = StrictModem()
vc = VaraControl("127.0.0.1", m.cmd_port, m.data_port)
vc.open()
vc.set_bandwidth("500")
vc.set_bandwidth("500")
time.sleep(0.3)
check("VaraControl repeat BW500 both arrive clean",
      [ln for ln in m.lines if ln.startswith(b"BW")] == [b"BW500", b"BW500"])
vc.close()
m.close()

# 7. lend / reclaim: no idle-side open while a session owns the port
#    (2026-11-14: a late _vara_recover took Mercury from a live connect)
m = StrictModem()
vc = VaraControl("127.0.0.1", m.cmd_port, m.data_port)
vc.open()
check("idle link opens", vc.is_open)
vc.lend()
check("lend closes the idle link", not vc.is_open)
check("open() refused while lent", vc.open() is False and not vc.is_open)
check("set_bandwidth cannot reopen while lent",
      vc.set_bandwidth("500") is False and not vc.is_open)
stale = vc.gen
vc.lend()                                   # a newer Connect
check("stale reclaim(gen) does nothing",
      vc.reclaim(stale) is False and not vc.is_open and vc.lent)
check("current reclaim(gen) reopens", vc.reclaim(vc.gen) and vc.is_open)
vc.close()
m.close()

# 8. MainWindow._vara_recover ignores a stale generation
import types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main_window as mw
calls = []
fake = types.SimpleNamespace(
    _vara_ctrl=types.SimpleNamespace(gen=5, reclaim=lambda g=None: calls.append(g)),
    btn_connect=types.SimpleNamespace(setEnabled=lambda v: calls.append(("btn", v))),
    terminal=types.SimpleNamespace(append=lambda *a: None),
    _logged_in=True)
mw.MainWindow._vara_recover(fake, 4)
check("_vara_recover with stale gen leaves port and button alone", calls == [])
mw.MainWindow._vara_recover(fake, 5)
check("_vara_recover with current gen reclaims and enables Connect",
      calls == [5, ("btn", True)])

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
