"""View-change + connect-decision logging, called unbound on a stand-in."""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw

MW = mw.MainWindow


def stub(start_view=0):
    s = types.SimpleNamespace()
    s.VIEW_MAIL, s.VIEW_TERMINAL, s.VIEW_DEBUG = 0, 1, 2
    s.VIEW_NAMES = MW.VIEW_NAMES
    s._idx = start_view
    s.logged = []
    s.stack = types.SimpleNamespace(
        currentIndex=lambda: s._idx,
        setCurrentIndex=lambda v: setattr(s, "_idx", v))
    s.debug_view = types.SimpleNamespace(
        append=lambda text, color=None: s.logged.append(text.strip()))
    # The two view toggle buttons became one dropdown in v0.15.0.
    s.view_combo = types.SimpleNamespace(
        currentIndex=lambda: s._idx,
        setCurrentIndex=lambda v: None,
        blockSignals=lambda v: None)
    s._set_transport_terminal_mode = lambda e: None
    s.terminal = types.SimpleNamespace(
        input_line=types.SimpleNamespace(setFocus=lambda: None))
    s.mail_view = types.SimpleNamespace(focus_default=lambda: None)
    s._pending_summary = None
    s._pending_outbox  = False
    return s

fails = 0

def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        want {want}\n        got  {got}")

# Mail → Terminal → Debug → Mail
s = stub(0)
for v in (1, 2, 0):
    MW._switch_view(s, v)
check("each switch logged once, named",
      s.logged,
      ["[SYS] View: Terminal", "[SYS] View: Debug", "[SYS] View: Mail"])

# Re-selecting the view you are already on logs nothing
s = stub(1)
MW._switch_view(s, 1)
MW._switch_view(s, 1)
check("no line when the view does not change", s.logged, [])

# The view really does change
s = stub(0)
MW._switch_view(s, 2)
check("stack index followed", s._idx, 2)

# ── Connect-decision line ────────────────────────────────────────────
def connect_line(view, login_only):
    s = stub(view)
    s._login_only_connect = login_only
    s.VIEW_NAMES = MW.VIEW_NAMES
    s.debug_view.append(
        f"[SYS] Connect from {s.VIEW_NAMES.get(s.stack.currentIndex(), '?')} "
        f"view — {'dumb terminal, QtC drives nothing' if login_only else 'QtC drives this session'}",
        None)
    return s.logged[-1]

check("Mail-view connect line", connect_line(0, False),
      "[SYS] Connect from Mail view — QtC drives this session")
check("Terminal-view connect line", connect_line(1, True),
      "[SYS] Connect from Terminal view — dumb terminal, QtC drives nothing")
check("Debug-view connect line", connect_line(2, True),
      "[SYS] Connect from Debug view — dumb terminal, QtC drives nothing")

print(f"\n{6 - fails}/6 passed")
sys.exit(1 if fails else 0)
