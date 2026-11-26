"""CAT PTT through flrig and Hamlib rigctld (cat-ptt branch, 2026-11-26).

Radios keyed over CAT (Xiegu X6100 and most rigs with a built-in USB
sound card) can't use a spare RTS line: the one CAT port belongs to the
rig program. QtC asks flrig (XML-RPC) or rigctld (TCP) to key instead.

A fake flrig and a fake rigctld run on 127.0.0.1, so this needs no radio.
Covers: key/unkey, start-in-RX, the 30 s watchdog (shortened here), a
radio that won't unkey, a rig program that isn't running, reconnect after
a restart, make_ptt() and the Settings tab round trip.
"""
import os, sys, socket, threading, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler

import ptt as P

fails = total = 0
def check(name, cond, extra=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {extra}" if extra and not cond else ""))


# ── Fake flrig ───────────────────────────────────────────────────────
class Quiet(SimpleXMLRPCRequestHandler):
    def log_message(self, *a): pass

class FakeFlrig:
    def __init__(self):
        self.ptt = 1            # pretend it was left keyed: open() must unkey
        self.calls = []
        self.stuck = False      # radio ignores PTT off
        self.srv = SimpleXMLRPCServer(("127.0.0.1", 0), Quiet,
                                      logRequests=False, allow_none=True)
        self.port = self.srv.server_address[1]
        self.srv.register_function(lambda: "", "rig.get_xcvr")
        self.srv.register_function(self._set, "rig.set_ptt")
        self.srv.register_function(lambda: self.ptt, "rig.get_ptt")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def _set(self, v):
        self.calls.append(v)
        if not (self.stuck and v == 0):
            self.ptt = v
        return 0
    def stop(self):
        self.srv.shutdown(); self.srv.server_close()

# ── Fake rigctld ─────────────────────────────────────────────────────
class FakeRigctld:
    def __init__(self, port=0):
        self.ptt = 0
        self.calls = []
        self.ls = socket.socket()
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", port)); self.ls.listen(4)
        self.port = self.ls.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()
    def _accept(self):
        while True:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()
    def _serve(self, c):
        f = c.makefile("rb")
        try:
            for line in f:
                cmd = line.decode().strip()
                self.calls.append(cmd)
                if cmd.startswith("T "):
                    self.ptt = int(cmd[2:]); c.sendall(b"RPRT 0\n")
                elif cmd == "t":
                    c.sendall(f"{self.ptt}\n".encode())
                else:
                    c.sendall(b"RPRT -1\n")
        except OSError:
            pass
    def stop(self):
        for c in self.conns:
            try: c.shutdown(socket.SHUT_RDWR); c.close()
            except OSError: pass
        try: self.ls.shutdown(socket.SHUT_RDWR)   # wakes the blocked accept()
        except OSError: pass
        self.ls.close()

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]
    s.close(); return p


# ── 1. flrig: open unkeys, tx/rx key and release ─────────────────────
fl = FakeFlrig()
log = []
c = P.FlrigPTT("127.0.0.1", fl.port)
c._log = lambda d, t: log.append(t)
c.open()
check("1 flrig open() succeeds", c.is_open, c.last_error)
check("1 open() leaves the radio in RX", fl.ptt == 0)
c.tx()
check("1 tx() keys via rig.set_ptt 1", fl.ptt == 1 and fl.calls[-1] == 1)
c.rx()
check("1 rx() unkeys", fl.ptt == 0)
check("1 log says TX and RX", any(t.startswith("TX") for t in log)
      and any(t.startswith("RX") for t in log), log)

# ── 2. Watchdog releases a key that never gets PTT OFF ───────────────
alerts = []
c.max_tx_s = 0.3
c._on_alert = alerts.append
c.tx()
time.sleep(0.6)
check("2 watchdog unkeyed after max TX time", fl.ptt == 0 and c.watchdog_fired)
check("2 watchdog raised an alert", alerts and "maximum TX" in alerts[-1], alerts)
c.watchdog_fired = False
c.tx(); time.sleep(0.1); c.rx(); time.sleep(0.4)
check("2 a normal rx() cancels the watchdog", not c.watchdog_fired)
check("2 default max TX is 30 s", P.MAX_TX_S == 30.0)

# ── 3. Radio that won't unkey → loud alert ───────────────────────────
alerts.clear()
c.max_tx_s = 0
fl.stuck = True
c.tx(); c.rx()
check("3 stuck key is reported", alerts and "may still be transmitting"
      in alerts[-1], alerts)
fl.stuck = False
c.close()
check("3 close() leaves RX", fl.ptt == 0)
fl.stop()

# ── 4. flrig not running ─────────────────────────────────────────────
c = P.FlrigPTT("127.0.0.1", free_port())
c.open()
check("4 open() fails cleanly when flrig isn't running", not c.is_open)
check("4 error says why", "refused" in c.last_error, c.last_error)
c.tx(); c.rx(); c.close()     # must not raise
check("4 tx/rx on a closed controller are no-ops", True)

# ── 5. rigctld: key, release, reconnect after restart ────────────────
rc = FakeRigctld()
port = rc.port
c = P.RigctldPTT("127.0.0.1", port)
c.open()
check("5 rigctld open() succeeds", c.is_open, c.last_error)
c.tx()
check("5 tx() sends T 1", rc.ptt == 1 and "T 1" in rc.calls)
c.rx()
check("5 rx() sends T 0 and checks with t", rc.ptt == 0 and rc.calls[-1] == "t")
rc.stop(); time.sleep(0.1)
rc = FakeRigctld(port)       # rigctld restarted on the same port
c.tx()
check("5 tx() reconnects after rigctld restart", rc.ptt == 1, rc.calls)
c.rx()
check("5 rx() after reconnect unkeys", rc.ptt == 0)
c.close(); rc.stop()

# ── 6. make_ptt() ────────────────────────────────────────────────────
check("6 none → None", P.make_ptt({"mode": "none", "port": "COM3"}) is None)
check("6 empty cfg → None", P.make_ptt({}) is None)
check("6 com → PTTController", isinstance(
    P.make_ptt({"mode": "com", "port": "COM3", "signal": "dtr"}), P.PTTController))
f = P.make_ptt({"mode": "flrig"})
check("6 flrig defaults 127.0.0.1:12345", isinstance(f, P.FlrigPTT)
      and f.where == "127.0.0.1:12345")
r = P.make_ptt({"mode": "rigctld", "rigctld_host": "192.0.2.5", "rigctld_port": 4999})
check("6 rigctld uses its own host/port", isinstance(r, P.RigctldPTT)
      and r.where == "192.0.2.5:4999")

# ── 7. Settings tab round trip ───────────────────────────────────────
from PyQt6.QtWidgets import QApplication
app = QApplication([])
import main_window as mw
cfg = {"user": {"callsign": "N0CALL"}, "bbs_list": [], "app": {},
       "bulletins": {}, "mail_call": {},
       "ptt": {"mode": "com", "port": "", "signal": "rts"}}
dlg = mw.SettingsDialog(cfg)
tab = dlg._ptt_form
check("7 COM mode hides Host/Port", not tab.isRowVisible(dlg.ptt_host))
dlg.ptt_mode.setCurrentIndex(2)        # flrig
check("7 flrig shows Host/Port, hides serial", tab.isRowVisible(dlg.ptt_host)
      and not tab.isRowVisible(dlg.ptt_signal))
check("7 flrig prefills 12345", dlg.ptt_net_port.value() == 12345)
dlg.ptt_host.setText("192.0.2.9")
dlg.ptt_mode.setCurrentIndex(3)        # rigctld
check("7 rigctld prefills 4532", dlg.ptt_net_port.value() == 4532
      and dlg.ptt_host.text() == "127.0.0.1")
dlg.ptt_mode.setCurrentIndex(2)        # back to flrig
check("7 flrig host survives a trip to rigctld", dlg.ptt_host.text() == "192.0.2.9")
out = dlg._ptt_cfg_from_dialog()
check("7 saved config", out["mode"] == "flrig" and out["flrig_host"] == "192.0.2.9"
      and out["flrig_port"] == 12345 and out["rigctld_port"] == 4532, out)
dlg2 = mw.SettingsDialog({**cfg, "ptt": out})
check("7 reopening Settings shows flrig + its host",
      dlg2.ptt_mode.currentIndex() == 2 and dlg2.ptt_host.text() == "192.0.2.9")

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
