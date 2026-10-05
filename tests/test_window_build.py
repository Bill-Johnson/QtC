"""Build the real MainWindow, offscreen, and check the toolbar/dialog
surface v0.15.0 changed.

Everything runs against a throwaway config and database in a temp dir —
Mail-Call is off in it, so no scheduler fires, nothing keys a radio, and
~/.local/share/qtc is never touched. This is the harness that catches an
attribute removed in one place and still referenced in another.
"""
import os, sys, json, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sandbox = tempfile.mkdtemp(prefix="qtc-smoke-")
cfg_path = os.path.join(sandbox, "config.json")
cfg = {
    "user": {"callsign": "N0CALL", "name": "Bill", "qth": "Anytown, ST",
             "zip": "12345", "home_bbs": "N0CALL.#REGION.ST.USA.NOAM",
             "password": "", "telnet_user": "TESTUSER"},
    "app": {"page_limit": 20, "data_dir": os.path.join(sandbox, "data")},
    "mail_call": {"enabled": False},
    "bulletins": {"check_on_connect": False, "subscriptions": []},
    "bbs_list": [{"name": "Test", "callsign": "N0CALL-1",
                  "transport": "telnet", "host": "127.0.0.1",
                  "telnet_port": 8010}],
}
json.dump(cfg, open(cfg_path, "w"))

import main_window as M
M._CONFIG_PATH = cfg_path                     # never touch the real one
from PyQt6.QtWidgets import QApplication
app = QApplication([])

fails = []
def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond: fails.append(label)

w = M.MainWindow()
check("MainWindow builds", True)
check("no btn_terminal left", not hasattr(w, "btn_terminal"))
check("no btn_debug left", not hasattr(w, "btn_debug"))
check("view_combo exists with 3 entries", w.view_combo.count() == 3,
      [w.view_combo.itemText(i) for i in range(3)])
check("Abort button is named Abort", w.btn_abort.text() == "⏹ Abort",
      repr(w.btn_abort.text()))
check("Abort starts disabled", not w.btn_abort.isEnabled())

# (Qt)SoundModem became a working transport 2026-10-03 — the toolbar Mode
# dropdown must offer it and turn it into a soundmodem entry.
modes = [w.mode_combo.itemText(i) for i in range(w.mode_combo.count())]
check("toolbar Mode offers (Qt)SoundModem", "(Qt)SoundModem" in modes, modes)
w.tb_stack.setCurrentIndex(0)        # RF panel, whatever entry loaded
w.mode_combo.setCurrentText("(Qt)SoundModem")
check("toolbar (Qt)SoundModem -> soundmodem transport",
      w._get_active_bbs_entry().get("transport") == "soundmodem")
check("toolbar hides BW for (Qt)SoundModem", w.bw_combo.isHidden())

# J5, 2026-09-28: on GNOME/Wayland Enter PRESSES a focused button, so a
# quick-button click followed by Enter at a page prompt re-sent LL 20
# instead of a bare CR. Offscreen Qt can't reproduce the key press, but it
# can see the cause: no Terminal button may be able to take focus.
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import Qt as _Qt
focusable = [b.text() for b in w.terminal.findChildren(QPushButton)
             if b.focusPolicy() != _Qt.FocusPolicy.NoFocus]
check("no Terminal button can take keyboard focus", not focusable, focusable)

# File downloads cannot be aborted (Bill, 2026-09-28) — LinBPQ commits the
# whole file to the link, so the button stays dead through every YAPP
# progress update and says why, then goes back to its normal tooltip.
w.btn_abort.setToolTip(w._ABORT_TIP_YAPP)     # what the Get File path sets
for done in (46, 2622, 5209):
    w._on_yapp_progress(done, 5209, "SunTzuChptr1.txt")
check("Abort stays disabled through a YAPP transfer",
      not w.btn_abort.isEnabled())
check("its tooltip says file downloads cannot be aborted",
      "cannot be aborted" in w.btn_abort.toolTip(), w.btn_abort.toolTip())
w._on_yapp_done("/tmp/x.txt", "x.txt")
check("tooltip goes back to the mail/bulletin wording after YAPP",
      w.btn_abort.toolTip() == w._ABORT_TIP)

# The dropdown and the function keys must agree in both directions.
w.view_combo.setCurrentIndex(w.VIEW_TERMINAL)
check("dropdown switches the stack",
      w.stack.currentIndex() == w.VIEW_TERMINAL, w.stack.currentIndex())
w._switch_view(w.VIEW_MAIL)
check("F2 path moves the dropdown back",
      w.view_combo.currentIndex() == w.VIEW_MAIL, w.view_combo.currentIndex())
w._switch_view(w.VIEW_DEBUG)
check("F4 path moves the dropdown",
      w.view_combo.currentIndex() == w.VIEW_DEBUG, w.view_combo.currentIndex())

# Outbox round-trip: queue a message the way Compose does, and read the row.
w._queue_outgoing({"to_call": "N0CALL", "subject": "Net tonight",
                   "body": "73 de N0CALL\n", "msg_type": "P", "at_bbs": ""})
pending = w.db.get_pending_outbox()
check("queued message lands in the outbox", len(pending) == 1, len(pending))
w._refresh_folder("outbox")
hdrs = [w.mail_view.msg_table.horizontalHeaderItem(c).text()
        for c in range(w.mail_view.msg_table.columnCount())]
check("outbox shows Size, not Status", hdrs[-1] == "Size" and "Status" not in hdrs, hdrs)
check("outbox size is the body's bytes",
      w.mail_view.msg_table.item(0, 4).text() == str(len("73 de N0CALL\n".encode())),
      w.mail_view.msg_table.item(0, 4).text())

# Settings dialog still builds with three controls removed.
dlg = M.SettingsDialog(w.config, parent=w)
check("Settings dialog builds", True)
check("no auto-download checkbox", not hasattr(dlg, "chk_auto_dl"))
check("no max-size field", not hasattr(dlg, "e_max_size"))
check("§97.221 line is in the RF responsibility text",
      "97.221" in dlg.MAILCALL_RESPONSIBILITY_HTML_RF)
check("responsibility version NOT bumped",
      dlg.MAILCALL_RESPONSIBILITY_VERSION == 1,
      dlg.MAILCALL_RESPONSIBILITY_VERSION)

# Saving settings must not crash and must not resurrect the dead keys.
dlg._on_accept()
check("settings save works", True)
check("auto_check_mail not written back",
      "auto_check_mail" not in w.config.get("app", {}), w.config.get("app"))
check("max_message_size_kb not written back",
      "max_message_size_kb" not in w.config.get("app", {}), w.config.get("app"))

# The is-logged-in flag that replaced the hidden Refresh button. The old
# read went through getattr(p, "btn_refresh", None), so losing the button
# would have made the Bulletins tab believe it was never connected —
# silently, with no exception. This checks the gate both ways.
check("the hidden Refresh button is gone", not hasattr(w, "btn_refresh"))
check("_logged_in starts False", w._logged_in is False)
w._logged_in = True
check("bulletins gate opens when logged in",
      dlg._is_connected_for_bulletins() is True)
w._logged_in = False
check("bulletins gate closes when not logged in",
      dlg._is_connected_for_bulletins() is False)

# Page-limit spinner must not offer a value LinBPQ refuses (1-9).
dlg.spin_page_limit.setValue(20)
check("page limit accepts 20", dlg.spin_page_limit.value() == 20)
dlg.spin_page_limit.setValue(5)
check("page limit snaps 5 up to the BBS minimum",
      dlg.spin_page_limit.value() == M.BBSSession.PAGE_MIN,
      dlg.spin_page_limit.value())
dlg.spin_page_limit.setValue(0)
check("page limit still allows 0 (paging off)",
      dlg.spin_page_limit.value() == 0, dlg.spin_page_limit.value())
dlg.spin_page_limit.setValue(20)

# Address book with the Send Mode column gone.
ab = M.AddressBookDialog(w.cdb, parent=w)
abh = [ab.table.horizontalHeaderItem(c).text()
       for c in range(ab.table.columnCount())]
check("address book has no Send Mode column",
      "Send Mode" not in abh and ab.table.columnCount() == 5, abh)

shutil.rmtree(sandbox, ignore_errors=True)
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
