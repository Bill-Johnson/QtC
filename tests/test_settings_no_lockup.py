"""Settings must never lock up on a bad Home BBS.

Beta report, 2026-10-26: a Home BBS typed as a full mail address
(CALL@CALL.ST.USA.NOAM) left Mail-Call enabled with no matching BBS List
entry. The Mail-Call tab greyed out its Enable box while it was still
ticked, and OK refused to save until Mail-Call was disabled. Nothing in
the dialog could get out of that; the tester had to delete config.json.

Builds the real SettingsDialog offscreen and drives OK the way a user
would. Fictional callsigns only.
"""
import os, sys, copy
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
app = QApplication([])
import main_window as mw

SD = mw.SettingsDialog
fails = total = 0


def check(name, cond, detail=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"   {detail}" if detail and not cond else ""))


# Record message boxes instead of blocking on them
shown = []
QMessageBox.information = staticmethod(
    lambda parent, title, text, *a, **k: shown.append(("info", title, text)))
QMessageBox.warning = staticmethod(
    lambda parent, title, text, *a, **k: shown.append(("warn", title, text)))

OLD_BBS = {"callsign": "W9OLD-1", "transport": "telnet",
           "host": "old.example", "telnet_port": 8010}
MY_BBS  = {"callsign": "N0CALL-7", "transport": "telnet",
           "host": "mine.example", "telnet_port": 8010}
NEW_BBS = {"callsign": "K0NEW", "transport": "telnet",
           "host": "new.example", "telnet_port": 8010}


def base_cfg():
    """A working setup: Home BBS W9OLD, Mail-Call on, visited."""
    return {
        "user": {"callsign": "N0CALL", "name": "Pat",
                 "home_bbs": "W9OLD.#REGION.ST.USA.NOAM"},
        "bbs_list": [copy.deepcopy(OLD_BBS), copy.deepcopy(MY_BBS),
                     copy.deepcopy(NEW_BBS)],
        "visited_bbs": {"N0CALL@W9OLD-1": True, "N0CALL@N0CALL-7": True},
        "mail_call": {"enabled": True, "time_zone": "local",
                      "schedule_mode": "twice", "every_n_hours": 4,
                      "custom_times": [],
                      "bbs_key": SD._mc_entry_key(OLD_BBS),
                      "responsibility_accepted_rf_version": 0},
        "app": {}, "ptt": {}, "bulletins": {},
    }


def press_ok(dlg, save_anyway=True):
    shown.clear()
    dlg._confirm_odd_home_bbs = lambda problem: save_anyway
    dlg._on_accept()
    return dlg.result() == QDialog.DialogCode.Accepted


# ── 1. The reported lock-up: bad Home BBS saved with Mail-Call on ────
cfg = base_cfg()
cfg["user"]["home_bbs"] = "N0CALL@N0CALL.ST.USA.NOAM"
dlg = SD(cfg)
check("1 Enable box is not left ticked when there is no matching BBS",
      not dlg.chk_mc_enabled.isChecked())
check("1 the reason is shown on the Mail-Call tab",
      dlg.lbl_mc_hint.isVisibleTo(dlg._tab_mailcall)
      and "No BBS List entry" in dlg.lbl_mc_hint.text())
ok = press_ok(dlg, save_anyway=True)
check("1 OK saves (no 'disable Mail-Call' dead end)", ok,
      f"messages: {shown}")
check("1 Mail-Call is saved OFF", dlg.get_config()["mail_call"]["enabled"] is False)
check("1 the user is told Mail-Call was turned off",
      any(t == "Mail-Call !!! turned off" for _, t, _ in shown), f"{shown}")
check("1 the other tabs' edits are kept",
      dlg.get_config()["user"]["name"] == "Pat")

# ── 2. Same config, user chooses Go Back and Fix ─────────────────────
dlg = SD(cfg)
ok = press_ok(dlg, save_anyway=False)
check("2 Go Back keeps the dialog open", not ok)
check("2 Go Back lands on My Station", dlg._tabs.currentWidget() is dlg._tab_user)

# ── 3. Fixing Home BBS in the same dialog makes Mail-Call usable again
dlg.e_home_bbs.setText("N0CALL.#REGION.ST.USA.NOAM")
dlg._tabs.setCurrentWidget(dlg._tab_mailcall)
check("3 Enable box is clickable again after the fix",
      dlg.chk_mc_enabled.isEnabled())
check("3 Connection list now offers the user's own BBS",
      (dlg._mc_selected_entry() or {}).get("callsign") == "N0CALL-7")
check("3 the warning hint is gone",
      not dlg.lbl_mc_hint.isVisibleTo(dlg._tab_mailcall))
dlg.chk_mc_enabled.setChecked(True)      # visited, Telnet: no dialogs
ok = press_ok(dlg)
mc = dlg.get_config()["mail_call"]
check("3 OK saves with Mail-Call on, pointed at the user's BBS",
      ok and mc["enabled"] and mc["bbs_key"]["callsign"] == "N0CALL-7",
      f"{mc} {shown}")

# ── 4. A good setup is left alone ────────────────────────────────────
dlg = SD(base_cfg())
ok = press_ok(dlg)
check("4 working Mail-Call setup saves unchanged and quietly",
      ok and dlg.get_config()["mail_call"]["enabled"] and not shown,
      f"{shown}")

# ── 5. Home BBS moved to a listed but never-visited BBS ──────────────
dlg = SD(base_cfg())
dlg.e_home_bbs.setText("K0NEW.ST.USA.NOAM")
ok = press_ok(dlg)
check("5 saves, but Mail-Call is turned off until the new BBS is visited",
      ok and dlg.get_config()["mail_call"]["enabled"] is False
      and any("Connect to it" in txt for _, _, txt in shown), f"{shown}")

# ── 6. A bad custom schedule can't block OK while Mail-Call is off ───
cfg = base_cfg()
cfg["user"]["home_bbs"] = "NOMATCH.ST.USA.NOAM"   # tab greyed out
cfg["mail_call"].update(schedule_mode="custom",
                        custom_times=["08:00", "08:30"])
dlg = SD(cfg)
check("6 OK saves when Mail-Call is off, even with too-close times",
      press_ok(dlg), f"{shown}")
cfg["user"]["home_bbs"] = "W9OLD.ST.USA.NOAM"     # usable, Mail-Call on
dlg = SD(cfg)
check("6 ...but the 2-hour rule still applies while Mail-Call is on",
      not press_ok(dlg)
      and any(t == "Mail-Call !!! schedule" for _, t, _ in shown))

# ── 7. Home BBS format check ─────────────────────────────────────────
GOOD = ["", "N0CALL", "N0CALL-1", "N0CALL.ST.USA.NOAM",
        "N0CALL.#REGION.ST.USA.NOAM", "n0call.#region.st.usa.noam",
        "  N0CALL.#REGION.ST.USA.NOAM  "]
for a in GOOD:
    check(f"7 accepts {a!r}", SD._home_bbs_problem(a) == "",
          SD._home_bbs_problem(a))
BAD = [("N0CALL@N0CALL.ST.USA.NOAM", "N0CALL.ST.USA.NOAM"),  # suggests fix
       ("N0CALL .ST.USA", "spaces"),
       ("N0CALL,ST.USA", ","),
       ("N0CALL..USA.NOAM", "empty section"),
       ("N0CALL.ST.USA.", "empty section"),
       ("N0CALL.#", "#"),
       ("N0CALL.US#A", "US#A"),
       ("#REGION.ST.USA", "callsign"),
       ("BBS.ST.USA", "callsign")]
for a, hint in BAD:
    p = SD._home_bbs_problem(a)
    check(f"7 flags {a!r}", p and hint in p, f"got {p!r}")

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
