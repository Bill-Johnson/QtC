"""KM must never reach the BBS without an explicit confirmation.

Calls the real MainWindow._on_terminal_cmd unbound on a stand-in, so the
guard under test is the shipping one. Covers the typed path, the quick
button and the test API — all three funnel through this method.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw
MW = mw.MainWindow

ANSWER = {"click": "cancel"}          # what the fake dialog "returns"

class FakeBox:
    Icon = mw.QMessageBox.Icon
    ButtonRole = mw.QMessageBox.ButtonRole
    shown = []
    def __init__(self, parent=None): self._buttons = {}
    def setIcon(self, *a): pass
    def setWindowTitle(self, t): FakeBox.shown.append(t)
    def setText(self, t): self.text = t
    def addButton(self, label, role):
        b = types.SimpleNamespace(label=label, role=role)
        self._buttons["send" if "kill my read" in label.lower() else "cancel"] = b
        return b
    def setDefaultButton(self, b): self.default = b
    def setEscapeButton(self, b): self.escape = b
    def exec(self): pass
    def clickedButton(self):
        return self._buttons.get(ANSWER["click"])   # None models Esc / window X
mw.QMessageBox = FakeBox

def stub():
    s = types.SimpleNamespace()
    s.sent = []
    s.worker = types.SimpleNamespace(
        session=object(),
        do_terminal_send=lambda c: s.sent.append(c))
    s.terminal   = types.SimpleNamespace(append=lambda t, c=None: None)
    s.debug_view = types.SimpleNamespace(append=lambda t, c=None: None)
    s._get_active_bbs_entry = lambda: {"callsign": "N0CALL-1"}
    s.DESTRUCTIVE_BBS_CMDS  = MW.DESTRUCTIVE_BBS_CMDS
    s._confirm_destructive  = types.MethodType(MW._confirm_destructive, s)
    return s

fails = 0
def check(name, cond):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")

# ── KM is blocked unless explicitly confirmed ────────────────────────
for answer, should_send in (("cancel", False), (None, False), ("send", True)):
    for cmd in ("KM", "km", "  Km  "):
        ANSWER["click"] = answer
        s = stub()
        MW._on_terminal_cmd(s, cmd)
        got = bool(s.sent)
        check(f"{cmd!r} with dialog={answer!r} -> "
              f"{'SENT' if got else 'blocked'}", got == should_send)

# Esc / window X (clickedButton None) must never send
ANSWER["click"] = "nothing-matches"
s = stub(); MW._on_terminal_cmd(s, "KM")
check("Escape / window X never sends KM", not s.sent)

# ── Everything else goes straight through, no dialog ─────────────────
FakeBox.shown = []
harmless = ["L", "LM", "LL 20", "RM", "I", "?", "B", "files", "read x.txt",
            "sp N0CALL", "R 823", "A", "bbs"]
s = stub()
for c in harmless:
    MW._on_terminal_cmd(s, c)
check("all harmless commands sent unchanged", s.sent == harmless)
check("no dialog shown for harmless commands", FakeBox.shown == [])

# ── Not connected: nothing sent, no dialog either ────────────────────
FakeBox.shown = []
s = stub(); s.worker = None
MW._on_terminal_cmd(s, "KM")
check("KM while disconnected sends nothing", not getattr(s, "sent", []))
check("...and does not even ask", FakeBox.shown == [])

print(f"\n{14 - fails}/14 passed")
sys.exit(1 if fails else 0)
