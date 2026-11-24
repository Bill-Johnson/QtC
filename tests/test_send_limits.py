"""Outgoing limits (security review 2026-11-23, #7).

- a station that keeps asking a registration question gets at most
  MAX_REGISTRATION_ROUNDS answers, not an endless stream of TX
- compose and terminal fields have length caps
Offline: fake transport, offscreen Qt, throwaway config.
"""
import os, sys, json, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bbs_session as bs

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

class Nagger:
    """Answers every line with the same registration question."""
    host, port = "fake", 0
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(text)
    def read_until(self, prompt, timeout=None): return "Please enter your Name\r\n>"
    def read_until_any(self, prompts, timeout=None): return (self.read_until(None), None)

t = Nagger()
s = bs.BBSSession(t, "N0CALL", user_info={"name": "Bill"})
s.log_cb = lambda *_: None
handled, _ = s._handle_registration("Please enter your Name\r\n>")
check("registration answered", handled)
check("answers capped", len(t.sent) == s.MAX_REGISTRATION_ROUNDS, len(t.sent))

# Normal four-question LinBPQ registration still completes.
class Polite(Nagger):
    Q = ["Please enter your QTH\r\n>", "Please enter your Zip\r\n>",
         "Please enter your Home BBS\r\n>", "de N0CALL-1>"]
    def read_until(self, prompt, timeout=None):
        return self.Q[min(len(self.sent) - 1, 3)]
t = Polite()
s = bs.BBSSession(t, "N0CALL", user_info={"name": "Bill", "qth": "Town",
                                           "zip": "12345", "home_bbs": "N0CALL-1"})
s.log_cb = lambda *_: None
s._handle_registration("Please enter your Name\r\n>")
check("normal registration sends 4 answers", len(t.sent) == 4, t.sent)

# Field caps
sandbox = tempfile.mkdtemp(prefix="qtc-lim-")
cfg_path = os.path.join(sandbox, "config.json")
json.dump({"user": {"callsign": "N0CALL"}, "bbs_list": [],
           "app": {"data_dir": os.path.join(sandbox, "data")},
           "mail_call": {"enabled": False},
           "bulletins": {"check_on_connect": False, "subscriptions": []}},
          open(cfg_path, "w"))
import main_window as M
M._CONFIG_PATH = cfg_path
from PyQt6.QtWidgets import QApplication, QLineEdit
app = QApplication([])
w = M.MainWindow()
check("terminal input capped at 512", w.terminal.input_line.maxLength() == 512)
d = M.ComposeDialog(w)
check("compose To capped at 80", d.to_edit.maxLength() == 80)
check("compose @BBS capped at 80", d.at_bbs_edit.maxLength() == 80)
check("compose Subject capped at 128", d.subject_edit.maxLength() == 128)
d.close()
w.close()

print(f"\n{'PASS' if not fails else 'FAIL'}: send limits ({len(fails)} failures)")
sys.exit(1 if fails else 0)
