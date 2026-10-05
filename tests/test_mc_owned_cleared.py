"""An exhausted Mail-Call slot must give the session back to the user
(H3, 2026-10-01).

Calls the real MainWindow._mc_handle_connect_failure / _mc_end_retry
unbound on a stand-in. On a failed connect _on_error greys Connect out
for the modem recovery BEFORE the roll-over runs. The old test for
"never connected", btn_connect.isEnabled(), was therefore False, and
_mc_session_owned stayed True: the next MANUAL connect ran as Mail-Call.
"""
import os, sys, types
from datetime import datetime, timedelta
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw
MW = mw.MainWindow

fails = 0
total = 0
def check(name, cond):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")

def stub(tries, logged_in=False, connect_enabled=False):
    s = types.SimpleNamespace()
    s.log = []
    s.retried = []
    s.MC_MAX_TRIES = MW.MC_MAX_TRIES
    s.MC_RETRY_SECONDS = MW.MC_RETRY_SECONDS
    s._mc_active_entry = {"callsign": "N0CALL-1", "transport": "direwolf"}
    s._mc_tries_used = tries
    s._mc_slot_deadline = datetime.now() + timedelta(minutes=10)
    s._mc_session_owned = True
    s._logged_in = logged_in
    s.btn_connect = types.SimpleNamespace(isEnabled=lambda: connect_enabled)
    s._mc_retry_timer = types.SimpleNamespace(stop=lambda: None)
    s._mc_scheduler = types.SimpleNamespace(
        set_status_paused=lambda b: None, refresh=lambda: None)
    s._on_log = s.log.append
    s._mc_schedule_retry = lambda e: s.retried.append(e)
    s._mc_end_retry = lambda: MW._mc_end_retry(s)
    return s

# ── The bug: 5/5 failed while Connect is greyed for modem recovery ───
s = stub(tries=MW.MC_MAX_TRIES, connect_enabled=False)
MW._mc_handle_connect_failure(s, "Direwolf could not connect — no answer")
check("budget exhausted is logged",
      any("retry budget exhausted" in l for l in s.log))
check("slot state torn down", s._mc_active_entry is None)
check("Mail-Call no longer owns the session (Connect greyed)",
      s._mc_session_owned is False)

# ── Slot deadline passed: same rule ──────────────────────────────────
s = stub(tries=2)
s._mc_slot_deadline = datetime.now() - timedelta(seconds=1)
MW._mc_handle_connect_failure(s, "no answer")
check("deadline roll-over also releases ownership",
      s._mc_session_owned is False)

# ── Budget left: retry, keep ownership ───────────────────────────────
s = stub(tries=2)
MW._mc_handle_connect_failure(s, "no answer")
check("budget left: a retry is scheduled", len(s.retried) == 1)
check("...and Mail-Call still owns the slot", s._mc_session_owned is True)

# ── Success: _on_connected path keeps ownership for the session ──────
s = stub(tries=1, logged_in=True, connect_enabled=False)
MW._mc_end_retry(s)
check("connected: Mail-Call keeps the session it just opened",
      s._mc_session_owned is True)

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
