"""A new message arriving must not end up read by itself.

Bill, 2026-10-29: Mail-Call pulled a new message in overnight. In the
morning it was highlighted on the top line and already marked read,
though he never clicked it. He had left the highlight on the previous
newest message.

Cause: the post-download Inbox refresh rebuilt the table and dropped the
selection, while the table kept keyboard focus. The next key that reached
QtC (arrow, Space, a key tapped to wake the screen) put the current row
on row 0 — the new message — and selecting it marks it read.

Builds the real MainWindow offscreen against a throwaway config and
database, and runs the same handler Mail-Call's download path calls.
"""
import os, sys, json, tempfile, types
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sandbox = tempfile.mkdtemp(prefix="qtc-sel-")
cfg_path = os.path.join(sandbox, "config.json")
json.dump({
    "user": {"callsign": "N0CALL", "home_bbs": "N0CALL.ST.USA.NOAM"},
    "app": {"data_dir": os.path.join(sandbox, "data")},
    "mail_call": {"enabled": False},
    "bulletins": {"check_on_connect": False, "subscriptions": []},
    "bbs_list": [{"name": "Test", "callsign": "N0CALL-1",
                  "transport": "telnet", "host": "127.0.0.1",
                  "telnet_port": 8010}],
}, open(cfg_path, "w"))

import main_window as M
M._CONFIG_PATH = cfg_path                     # never touch the real one
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtCore import QEvent, Qt
app = QApplication([])

fails = total = 0


def check(name, cond, detail=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"   {detail}" if detail and not cond else ""))


w = M.MainWindow()
w.show()
app.processEvents()
t = w.mail_view.msg_table
n = 0


def arrive(read=False):
    """One message lands in the inbox, the way the worker saves it."""
    global n
    n += 1
    w.db.save_to_inbox(types.SimpleNamespace(
        msg_number=n, date="27-Oct", msg_type="P", status="N",
        to_call="N0CALL", from_call="W9XYZ", subject=f"msg {n}",
        body="73", size=2, downloaded=True), "N0CALL-1")
    if read:
        for r in w.db.get_inbox():
            if r["msg_number"] == n:
                w.db.mark_read(r["id"])
    return n


def is_read(num):
    return next(r["read"] for r in w.db.get_inbox() if r["msg_number"] == num)


def subject_at(row):
    it = t.item(row, 2)
    return it.text() if it else None


def press(key):
    app.sendEvent(t, QKeyEvent(QEvent.Type.KeyPress, key,
                               Qt.KeyboardModifier.NoModifier))
    app.processEvents()


# Two old, read messages; the user selects the newest (top) one.
arrive(read=True); arrive(read=True)
w._refresh_folder("inbox"); app.processEvents()
t.setFocus()
t.setCurrentCell(0, 1); app.processEvents()
check("setup: user is on the newest message", subject_at(0) == "msg 2")

# ── Overnight Mail-Call download ─────────────────────────────────────
new = arrive()
w._on_download_done(1); app.processEvents()
check("new message is on top", subject_at(0) == f"msg {new}")
check("highlight stays on the message the user picked (now row 1)",
      t.currentRow() == 1 and subject_at(1) == "msg 2",
      f"row {t.currentRow()}")
check("its preview is still showing",
      "msg 2" in w.mail_view.preview_header.text(),
      w.mail_view.preview_header.text())
check("new message is still unread after the refresh", not is_read(new))

# ── The morning: a key reaches the table (wake the screen, Space) ────
press(Qt.Key.Key_Space)
check("a stray Space does not read the new message", not is_read(new))
press(Qt.Key.Key_Shift)
check("a stray Shift does not read the new message", not is_read(new))

# Arrow UP from msg 2 is the user choosing the new message — that reads it.
# (The synthetic Space above can move the cursor down a row onto an older,
# already-read message, so put it back on msg 2 first.)
t.setCurrentCell(1, 1); app.processEvents()
press(Qt.Key.Key_Up)
check("Arrow Up onto the new message reads it (user's choice)",
      is_read(new) and t.currentRow() == 0)

# ── Nothing selected: the refresh doesn't invent a selection ─────────
t.setCurrentCell(-1, -1); app.processEvents()
new2 = arrive()
w._on_download_done(1); app.processEvents()
check("with nothing selected, the refresh selects nothing",
      t.currentRow() == -1, f"row {t.currentRow()}")
check("...and the new message stays unread", not is_read(new2))

# ── Switching folder doesn't carry a selection across by id ─────────
t.setCurrentCell(0, 1); app.processEvents()     # read new2
w._refresh_folder("sent"); app.processEvents()
check("switching to another folder starts with nothing selected",
      t.currentRow() == -1, f"row {t.currentRow()}")

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
