"""BBS text must never be rendered as HTML (security review 2026-11-23, #1).

QLabel auto-detects rich text. A message subject such as
<img src="/dev/zero"> made Qt read /dev/zero as an image until memory
ran out, every time the message was selected. Check that the preview
header escapes every BBS-supplied value and that the status-bar labels
are plain text. Offscreen, throwaway config/db, nothing keys a radio.
"""
import os, sys, json, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sandbox = tempfile.mkdtemp(prefix="qtc-html-")
cfg_path = os.path.join(sandbox, "config.json")
json.dump({
    "user": {"callsign": "N0CALL", "name": "", "qth": "", "zip": "",
             "home_bbs": "", "password": "", "telnet_user": ""},
    "app": {"page_limit": 20, "data_dir": os.path.join(sandbox, "data")},
    "mail_call": {"enabled": False},
    "bulletins": {"check_on_connect": False, "subscriptions": []},
    "bbs_list": [],
}, open(cfg_path, "w"))

import main_window as M
M._CONFIG_PATH = cfg_path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
app = QApplication([])

fails = []
def check(label, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond: fails.append(label)

w = M.MainWindow()
mv = w.mail_view

EVIL = '<img src="/dev/zero">'
rd = {"from_call": "<b>SYSOP</b>", "to_call": EVIL, "subject": EVIL,
      "category": "<h1>ALL</h1>", "at_bbs": "<i>WW</i>",
      "date": "<u>2026</u>", "created_at": EVIL, "sent_at": EVIL,
      "body": EVIL}

for folder in ("inbox", "bulletins", "outbox", "sent", "drafts"):
    mv.show_preview(dict(rd), folder)
    h = mv.preview_header.text()
    check(f"{folder}: no raw <img> in preview header", "<img" not in h, h[:80])
    check(f"{folder}: subject shown escaped", "&lt;img" in h)
    for tag in ("<h1>", "<i>", "<u>"):
        check(f"{folder}: no raw {tag}", tag not in h)
    check(f"{folder}: body is plain text", mv.preview_body.toPlainText() == EVIL)

for name in ("conn_label", "status_label", "_prog_detail",
             "_vara_info_label", "_mc_status_label"):
    lbl = getattr(w, name)
    check(f"{name} is PlainText",
          lbl.textFormat() == Qt.TextFormat.PlainText, lbl.textFormat())

# An [RX] line carrying HTML lands in the status bar as literal text.
w.status_label.setText("[RX] " + EVIL)
check("status bar keeps the literal text", w.status_label.text().endswith(EVIL))

w.close()
print(f"\n{'PASS' if not fails else 'FAIL'}: html injection ({len(fails)} failures)")
sys.exit(1 if fails else 0)
