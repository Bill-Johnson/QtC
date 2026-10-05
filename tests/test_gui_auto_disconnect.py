"""Offline check of MainWindow._auto_disconnect_if_done.

One rule since 2026-09-20: QtC hangs up the sessions QtC drove — a Mail-view
connect on ANY transport, plus every Mail-Call. A Terminal / Debug connect is
the user's to end. Decided at Connect (_login_only_connect), never by
whichever view is open when the work finishes. Called unbound on a stand-in
so no QApplication or real window is needed.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory

import main_window as mw
from transport import TelnetTransport


def a_telnet():
    return TelnetTransport('127.0.0.1', 8772)


class FakeTimer:
    fired = []
    @staticmethod
    def singleShot(ms, fn):
        FakeTimer.fired.append(fn)

mw.QTimer = FakeTimer


class FakeAGW:            # anything that is not a TelnetTransport
    pass


def a_packet():
    return FakeAGW()


def make(mc_owned, login_only, view, transport, has_session=True):
    s = types.SimpleNamespace()
    s._mc_session_owned    = mc_owned
    s._login_only_connect  = login_only
    s.VIEW_MAIL, s.VIEW_TERMINAL, s.VIEW_DEBUG = 0, 1, 2
    s.stack   = types.SimpleNamespace(currentIndex=lambda: view)
    s.status  = []
    s._set_status = lambda text, connected=False: s.status.append(text)
    s._on_disconnect = lambda: None
    s.worker  = (types.SimpleNamespace(
                    session=types.SimpleNamespace(transport=transport()))
                 if has_session else None)
    return s


CASES = [
    # name, mc_owned, login_only, view, transport, expect hang-up
    ("Mail-Call packet, Terminal view open",
     True,  False, 1, a_packet,          True),
    ("Mail-Call telnet",
     True,  False, 0, a_telnet,        True),
    ("Mail-view telnet, Mail view open",
     False, False, 0, a_telnet,        True),
    ("Mail-view telnet, WATCHED from Terminal view  (the fix)",
     False, False, 1, a_telnet,        True),
    ("Mail-view telnet, watched from Debug view",
     False, False, 2, a_telnet,        True),
    ("Terminal-view telnet connect — user's to end",
     False, True,  1, a_telnet,        False),
    ("Terminal-view telnet connect, switched to Mail view",
     False, True,  0, a_telnet,        False),
    ("Mail-view packet — QtC drove it, so QtC hangs up  (changed 2026-09-20)",
     False, False, 0, a_packet,          True),
    ("Mail-view packet, watched from Terminal view",
     False, False, 1, a_packet,          True),
    ("Mail-view VARA, watched from Debug view",
     False, False, 2, a_packet,          True),
    ("Terminal-view packet connect",
     False, True,  1, a_packet,          False),
]

fails = 0
for name, mc, lo, view, tr, expect in CASES:
    FakeTimer.fired = []
    s = make(mc, lo, view, tr)
    mw.MainWindow._auto_disconnect_if_done(s)
    got = bool(FakeTimer.fired)
    ok = got == expect
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: "
          f"{'hangs up' if got else 'stays up'}"
          f"{'' if ok else '  (expected ' + ('hang-up' if expect else 'stay-up') + ')'}"
          f"{'  ' + s.status[-1] if s.status else ''}")

# No session at all — must not touch the timer or crash
FakeTimer.fired = []
s = make(True, False, 0, a_telnet, has_session=False)
mw.MainWindow._auto_disconnect_if_done(s)
ok = not FakeTimer.fired
fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  No session yet: stays quiet")

print(f"\n{len(CASES) + 1 - fails}/{len(CASES) + 1} passed")
sys.exit(1 if fails else 0)
