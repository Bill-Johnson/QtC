"""Closing QtC while linked hangs up first (D9, 2026-10-01).

Calls the real MainWindow.closeEvent unbound on a stand-in. The first
close while a session exists must NOT accept: it asks the worker to
disconnect and closes when sig_disconnected arrives (or after the cap).
A second close, or a close with no session, goes straight through.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw
from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)
MW = mw.MainWindow

fails = 0
total = 0
def check(name, cond):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")

class Event:
    def __init__(self): self.state = None
    def accept(self): self.state = "accepted"
    def ignore(self): self.state = "ignored"

class Sig:
    def __init__(self): self.slots = []
    def connect(self, f): self.slots.append(f)

def stub(session=True):
    s = types.SimpleNamespace()
    s.calls = []
    s.CLOSE_DISCONNECT_CAP_MS = MW.CLOSE_DISCONNECT_CAP_MS
    s.worker = types.SimpleNamespace(
        session=object() if session else None,
        sig_disconnected=Sig(),
        do_disconnect=lambda: s.calls.append("disconnect"),
        quit=lambda: s.calls.append("quit"),
        wait=lambda ms: s.calls.append(f"wait {ms}"))
    s._set_status = lambda text, **kw: s.calls.append(f"status {text}")
    s.terminal = types.SimpleNamespace(append=lambda *a: None)
    s.debug_view = types.SimpleNamespace(append=lambda *a: None)
    s._vara_ctrl = types.SimpleNamespace(close=lambda: s.calls.append("vara close"))
    s.close = lambda: s.calls.append("close")
    return s

# ── First close while linked: hang up, don't quit yet ────────────────
s = stub(); e = Event()
MW.closeEvent(s, e)
check("first close while linked is ignored", e.state == "ignored")
check("...asks the worker to disconnect", s.calls.count("disconnect") == 1)
check("...does not tear the worker down yet", "quit" not in s.calls)
check("...says so in the status line",
      any(c.startswith("status Disconnecting") for c in s.calls))
check("...closes again when the disconnect is confirmed",
      len(s.worker.sig_disconnected.slots) == 1)

# ── Second close: quits at once ──────────────────────────────────────
e2 = Event()
MW.closeEvent(s, e2)
check("second close is accepted", e2.state == "accepted")
check("...and tears the worker down", "quit" in s.calls)

# ── Not linked: straight through, no disconnect ──────────────────────
s = stub(session=False); e = Event()
MW.closeEvent(s, e)
check("close with no session is accepted at once", e.state == "accepted")
check("...without a disconnect", "disconnect" not in s.calls)

# ── No worker at all ─────────────────────────────────────────────────
s = stub(); s.worker = None; e = Event()
MW.closeEvent(s, e)
check("close with no worker is accepted", e.state == "accepted")

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
