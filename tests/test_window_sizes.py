"""Remembered window sizes (Bill, 2026-11-22).

A resize holds until the user changes it again: main window size, the
Mail view dividers, the From/Date/Size column widths and each dialog's
size, all kept in window_state.json beside config.json. Checks:

  * nothing saved -> the built-in default sizes
  * main window, dividers and columns come back after close + reopen
  * a dialog reopens at the size it was closed at (any close: OK/Cancel)
  * the BBS entry dialog keeps only its width (height follows its rows)
  * a size bigger than the screen is shrunk to fit
  * a broken or odd window_state.json is ignored, never a crash
  * config.json is never written by any of this
  * a dialog that was never shown saves nothing
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory

_tmp = tempfile.mkdtemp(prefix="qtc-winsize-")
# Qt's default offscreen screen is 800 x 800, smaller than QtC's minimum
# window; give it a real 1920 x 1080 one so sizes aren't clamped.
_scr = os.path.join(_tmp, "screen.json")
json.dump({"screens": [{"name": "s1", "x": 0, "y": 0, "width": 1920,
                        "height": 1080, "logicalDpi": 96,
                        "logicalBaseDpi": 96, "dpr": 1}]}, open(_scr, "w"))
# Qt splits platform options on ':', so a Windows drive letter ("C:")
# would cut the path short. Drop the drive; the temp dir is on the same
# drive as the working folder in practice.
_scr_opt = _scr.replace("\\", "/")
if os.name == "nt" and len(_scr_opt) > 1 and _scr_opt[1] == ":":
    _scr_opt = _scr_opt[2:]
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={_scr_opt}"
cfg_path = os.path.join(_tmp, "config.json")
json.dump({
    "user": {"callsign": "N0CALL", "name": "", "qth": "", "zip": "",
             "home_bbs": ""},
    "vara": {}, "ptt": {"mode": "none"}, "app": {"page_limit": 20},
    "mail_call": {"enabled": False},
    "bulletins": {"check_on_connect": False, "subscriptions": []},
    "bbs_list": [],
}, open(cfg_path, "w"))

import main_window as M
M._CONFIG_PATH = cfg_path                     # never touch the real one
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtCore import QTimer
app = QApplication([])
STATE = os.path.join(_tmp, "window_state.json")
cfg_before = open(cfg_path).read()

fails = total = 0


def check(name, cond, detail=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"   {detail}" if detail and not cond else ""))


def shut(w):
    w.close()
    app.processEvents()


# 1. No file yet: defaults
check("state path sits beside config.json", M._ui_state_path() == STATE)
w = M.MainWindow()
check("no file: default main window size 1120 x 720",
      (w.width(), w.height()) == (1120, 720), f"{w.width()}x{w.height()}")
check("no file: default From column 115",
      w.mail_view.msg_table.columnWidth(1) == 115)
w.show(); app.processEvents()

# 2. Resize everything, close, reopen
w.resize(1300, 800)
mv = w.mail_view
mv.msg_table.setColumnWidth(1, 170)
mv.msg_table.setColumnWidth(3, 95)
mv.msg_table.setColumnWidth(4, 60)
mv._split_v.setSizes([200, 400])
app.processEvents()
want_v = mv._split_v.sizes()
want_h = mv._split_h.sizes()
shut(w)
check("closing writes window_state.json", os.path.exists(STATE))
st = json.load(open(STATE))
check("saved columns", st.get("mail_columns") == [170, 95, 60],
      st.get("mail_columns"))

w = M.MainWindow()
check("reopen: main window size held",
      (w.width(), w.height()) == (1300, 800), f"{w.width()}x{w.height()}")
w.show(); app.processEvents()
mv = w.mail_view
check("reopen: From / Date / Size widths held",
      [mv.msg_table.columnWidth(c) for c in (1, 3, 4)] == [170, 95, 60])
check("reopen: message list / preview divider held",
      mv._split_v.sizes() == want_v, f"{mv._split_v.sizes()} vs {want_v}")
check("reopen: folder divider held",
      mv._split_h.sizes() == want_h, f"{mv._split_h.sizes()} vs {want_h}")
from PyQt6.QtWidgets import QHeaderView
hh = mv.msg_table.horizontalHeader()
check("From / Date / Size can be dragged",
      all(hh.sectionResizeMode(c) == QHeaderView.ResizeMode.Interactive
          for c in (1, 3, 4)))
check("status column stays fixed",
      hh.sectionResizeMode(0) == QHeaderView.ResizeMode.Fixed)
shut(w)


# 3. Dialogs: size held across close + reopen (exec, closed by a timer)
def run(dlg, size=None, how="reject"):
    def act():
        if size:
            dlg.resize(*size)
        getattr(dlg, how)()
    QTimer.singleShot(50, act)
    dlg.exec()


host = M.MainWindow()
d = M.SettingsDialog(host.config, parent=host)
check("Settings default size 900 x 880", (d.width(), d.height()) == (900, 880))
run(d, (860, 760))
d = M.SettingsDialog(host.config, parent=host)
seen = []
QTimer.singleShot(30, lambda: (seen.append((d.width(), d.height())), d.reject()))
d.exec()
check("Settings reopens at 860 x 760", seen == [(860, 760)], seen)

d = M.ComposeDialog(parent=host)
run(d, (640, 520))     # nothing typed, so Cancel closes without asking
d = M.ComposeDialog(parent=host)
seen = []
QTimer.singleShot(30, lambda: (seen.append((d.width(), d.height())), d.reject()))
d.exec()
check("Compose reopens at 640 x 520", seen == [(640, 520)], seen)

# BBS entry: width only
d = M._BBSEntryDialog(parent=host)
run(d, (760, 900))
d = M._BBSEntryDialog(parent=host)
seen = []
QTimer.singleShot(30, lambda: (seen.append((d.width(), d.height())), d.reject()))
d.exec()
check("BBS entry keeps its width", seen and seen[0][0] == 760, seen)
check("...but not the tall height (fits its rows)",
      seen and seen[0][1] < 900, seen)
k = M._BBSEntryDialog(parent=host)
pick = [kk for _, kk in k.TRANSPORTS]
k.transport_combo.setCurrentIndex(pick.index("telnet"))
check("BBS entry: switching transport keeps the width",
      k.width() >= 420)

# 4. A dialog built but never shown saves nothing
before = json.load(open(STATE))
d = M.SettingsDialog(host.config, parent=host)
d.done(0)
check("never-shown dialog saves nothing", json.load(open(STATE)) == before)

# 5. Bigger than the screen -> shrunk to fit
scr = app.primaryScreen().availableGeometry()
st = json.load(open(STATE))
st["dialogs"]["SettingsDialog"] = [scr.width() + 900, scr.height() + 900]
json.dump(st, open(STATE, "w"))
d = M.SettingsDialog(host.config, parent=host)
seen = []
QTimer.singleShot(30, lambda: (seen.append((d.width(), d.height())), d.reject()))
d.exec()
check("oversized saved dialog fits the screen",
      seen and seen[0][0] <= scr.width() and seen[0][1] <= scr.height(),
      f"{seen} screen {scr.width()}x{scr.height()}")

# 6. Broken / odd file: ignored
for label, text in [("garbage", "{not json"),
                    ("a list", "[1, 2]"),
                    ("odd values", json.dumps({
                        "main_geometry": 42, "mail_split_h": "x",
                        "mail_columns": [True, -5, 3],
                        "dialogs": {"SettingsDialog": ["a", "b"]}}))]:
    open(STATE, "w").write(text)
    try:
        w = M.MainWindow()
        ok = (w.width(), w.height()) == (1120, 720) and \
            w.mail_view.msg_table.columnWidth(1) == 115
        d = M.SettingsDialog(w.config, parent=w)
        QTimer.singleShot(30, d.reject)
        d.exec()
        ok = ok and (d.width(), d.height()) == (900, 880)
    except Exception as e:
        ok = False
        label += f" ({e!r})"
    check(f"{label} window_state.json: defaults, no crash", ok)

# 7. config.json untouched throughout
check("config.json never written", open(cfg_path).read() == cfg_before)
check("no .tmp left behind", not os.path.exists(STATE + ".tmp"))

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
