"""VaraControl re-link: VARA started after QtC, VARA quit, and the race
with Connect (2026-10-01).

A fake VARA listens on two local ports. Checks that a dead link is
noticed (is_open goes False when VARA hangs up), that a later open()
re-links, that open(gen=...) does nothing once close() has run, and that
MainWindow._vara_relink_tick only tries while idle on a VARA entry.
"""
import os, sys, socket, threading, time, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import VaraControl
import main_window as mw
MW = mw.MainWindow

fails = 0
total = 0
def check(name, cond):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")

def wait_for(pred, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()

class FakeVara:
    """Accepts one client on each port and can hang up on it."""
    def __init__(self, cmd_port=0, data_port=0):
        self.lsocks, self.clients = [], []
        self.ports = []
        for p in (cmd_port, data_port):
            l = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", p))
            l.listen(1)
            self.lsocks.append(l)
            self.ports.append(l.getsockname()[1])
            threading.Thread(target=self._accept, args=(l,), daemon=True).start()
    def _accept(self, l):
        while True:
            try:
                c, _ = l.accept()
            except OSError:
                return
            self.clients.append(c)
    def quit(self):
        for s in self.lsocks + self.clients:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

def free_ports():
    out = []
    for _ in range(2):
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        out.append(s.getsockname()[1]); s.close()
    return out

cmd_p, data_p = free_ports()
ctrl = VaraControl("127.0.0.1", cmd_p, data_p)

# ── VARA not running yet ─────────────────────────────────────────────
check("open() with VARA down returns False", ctrl.open() is False)
check("...and is_open is False", not ctrl.is_open)

# ── VARA starts after QtC: a later open() links ──────────────────────
vara = FakeVara(cmd_p, data_p)
check("open() after VARA starts links", ctrl.open() is True)
check("both ports held", wait_for(lambda: len(vara.clients) == 2))

# ── VARA quits: the dead socket is noticed ───────────────────────────
vara.quit()
check("is_open goes False when VARA hangs up", wait_for(lambda: not ctrl.is_open))
check("data socket dropped too", ctrl._data_sock is None)

# ── VARA comes back: re-link works, reader thread still serving ──────
vara = FakeVara(cmd_p, data_p)
check("re-link after VARA restarts", ctrl.open() is True)
check("reader thread alive after re-link",
      ctrl._reader_thread is not None and ctrl._reader_thread.is_alive())

# ── Connect race: a stale gen never reopens ──────────────────────────
gen = ctrl.gen
ctrl.close()                         # what _on_connect does
check("close() bumps gen", ctrl.gen == gen + 1)
check("open(gen=stale) does nothing", ctrl.open(gen=gen) is False
      and not ctrl.is_open)
check("open(gen=current) links", ctrl.open(gen=ctrl.gen) is True)
ctrl.close()
vara.quit()

# ── The GUI tick: only idle, only on a VARA entry ────────────────────
def stub(mode="vara_hf", connect_enabled=True, logged_in=False, linked=False):
    s = types.SimpleNamespace()
    s.opens = []
    s.bw_pushed = []
    s._vara_ctrl = types.SimpleNamespace(
        is_open=linked, gen=7,
        open=lambda gen=None: s.opens.append(gen))
    s._vara_relink_busy = False
    s._vara_was_linked = linked
    s._get_active_bbs_entry = lambda: {"transport": mode}
    s.bw_combo = types.SimpleNamespace(currentText=lambda: "500")
    s.btn_connect = types.SimpleNamespace(isEnabled=lambda: connect_enabled)
    s._logged_in = logged_in
    s._vara_set_bw = lambda bw: s.bw_pushed.append(bw)
    s.debug_view = types.SimpleNamespace(append=lambda *a: None)
    return s

s = stub(); MW._vara_relink_tick(s)
check("idle on VARA HF: tries to link, with the current gen",
      wait_for(lambda: s.opens == [7]))
s = stub(mode="vara_fm"); MW._vara_relink_tick(s)
check("idle on VARA FM: tries too", wait_for(lambda: s.opens == [7]))
for label, kw in [("Direwolf entry", dict(mode="direwolf")),
                  ("Telnet entry", dict(mode="telnet")),
                  ("connecting / session (Connect greyed)", dict(connect_enabled=False)),
                  ("logged in", dict(logged_in=True)),
                  ("already linked", dict(linked=True))]:
    s = stub(**kw); MW._vara_relink_tick(s); time.sleep(0.2)
    check(f"no attempt: {label}", s.opens == [])
s = stub(); s._vara_relink_busy = True; MW._vara_relink_tick(s); time.sleep(0.2)
check("no second attempt while one is in flight", s.opens == [])
s = stub(); s._vara_ctrl.is_open = True; MW._vara_relink_tick(s)
check("newly linked: pushes the bandwidth once", s.bw_pushed == ["500"])
MW._vara_relink_tick(s)
check("...and not again on the next tick", s.bw_pushed == ["500"])

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
