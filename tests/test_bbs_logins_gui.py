"""Per-BBS Telnet logins in the GUI (2026-11-23).

- startup moves an old My Station login into the Telnet entries
- Station Setup has no Telnet login fields any more
- BBS entry dialog: Username/Password only saved when a username is set,
  login_id kept across edits, grey cleartext note present
- toolbar pointed at a different host never carries this entry's password
- Mail view won't connect to a Telnet BBS with no saved login
- Settings shows keyring-stored passwords and saves them back there
Offscreen, throwaway config, fake keyring backend.
"""
import os, sys, json, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

import credentials as C
class FakeKR:
    priority = 5
    def __init__(self): self.store = {}
    def get_keyring(self): return self
    def get_password(self, s, k): return self.store.get((s, k))
    def set_password(self, s, k, p): self.store[(s, k)] = p
    def delete_password(self, s, k): self.store.pop((s, k), None)
kr = FakeKR(); C._backend = kr

sandbox = tempfile.mkdtemp(prefix="qtc-logins-")
cfg_path = os.path.join(sandbox, "config.json")
CLUB = {"name": "Club", "callsign": "N0CALL-1", "transport": "telnet",
        "host": "club.example.com", "telnet_port": 8010,
        "telnet_user": "clubuser", "password": "club-pw"}
HOME = {"name": "Home", "callsign": "N0CALL-2", "transport": "telnet",
        "host": "home.example.com", "telnet_port": 8010}
json.dump({"user": {"callsign": "N0CALL", "telnet_user": "n0call",
                    "password": "station-pw"},
           "app": {"data_dir": os.path.join(sandbox, "data")},
           "mail_call": {"enabled": False},
           "bulletins": {"check_on_connect": False, "subscriptions": []},
           "bbs_list": [CLUB, HOME]}, open(cfg_path, "w"))

import main_window as M
M._CONFIG_PATH = cfg_path
M._CONFIG_BAK = cfg_path + ".bak"
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox
app = QApplication([])

# ── entry dialog ───────────────────────────────────────────────────
d = M._BBSEntryDialog(entry=dict(CLUB, login_id="abc123"))
check("dialog shows the entry's username", d.e_bbs_user.text() == "clubuser")
check("dialog shows the entry's password", d.e_bbs_pw.text() == "club-pw")
e = d.get_entry()
check("get_entry keeps login and login_id",
      e.get("telnet_user") == "clubuser" and e.get("password") == "club-pw"
      and e.get("login_id") == "abc123", e)
notes = [l.text() for l in d.findChildren(QLabel) if "unencrypted" in l.text()]
check("cleartext note in the Telnet panel", len(notes) == 1, notes)
d.e_bbs_user.setText("")
check("blank username disables the password", not d.e_bbs_pw.isEnabled())
e = d.get_entry()
check("blank username -> no login saved", "telnet_user" not in e and "password" not in e, e)
d.close()

# ── startup migration ─────────────────────────────────────────────
w = M.MainWindow()
saved = json.load(open(cfg_path))
home = [b for b in saved["bbs_list"] if b["name"] == "Home"][0]
check("startup moved the old login into the blank entry",
      home.get("telnet_user") == "n0call" and home.get("password") == "station-pw", home)
check("...and removed it from My Station",
      "telnet_user" not in saved["user"] and "password" not in saved["user"], saved["user"])
check("...and the club entry kept its own",
      [b for b in saved["bbs_list"] if b["name"] == "Club"][0]["telnet_user"] == "clubuser")

# ── toolbar guard ──────────────────────────────────────────────────
w.tb_stack.setCurrentIndex(1)
for i in range(w.bbs_combo.count()):
    if (w.bbs_combo.itemData(i) or {}).get("name") == "Club":
        w.bbs_combo.setCurrentIndex(i)
w.host_edit.setText("club.example.com"); w.port_edit.setText("8010")
a = w._get_active_bbs_entry()
check("same host:port -> entry's own login",
      C.resolve_telnet_login(w.config, a)[:2] == ("clubuser", "club-pw"))
w.host_edit.setText("other.example.com")
a = w._get_active_bbs_entry()
check("unknown host -> no login at all",
      C.resolve_telnet_login(w.config, a) == ("", "", "none"))
w.host_edit.setText("home.example.com")
a = w._get_active_bbs_entry()
check("host of another saved entry -> that entry's login",
      C.resolve_telnet_login(w.config, a)[:2] == ("n0call", "station-pw"))
w.host_edit.setText("club.example.com"); w.port_edit.setText("8011")
a = w._get_active_bbs_entry()
check("different port -> NOT the club password",
      C.resolve_telnet_login(w.config, a)[1] != "club-pw")

# ── Mail view refuses a Telnet connect with no login ──────────────
shown = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]))
w.host_edit.setText("other.example.com"); w.port_edit.setText("8010")
w.stack.setCurrentIndex(w.VIEW_MAIL)
w._on_connect()
check("Mail view: no-login connect stopped with a message",
      shown and "No Telnet login is saved" in shown[0] and "other.example.com:8010" in shown[0], shown)
check("Mail view: no session started", getattr(w, "worker", None) is None
      and w.btn_connect.isEnabled())

# ── Station Setup has no Telnet login ─────────────────────────────
dlg = M.SettingsDialog(w.config, parent=w)
labels = [l.text() for l in dlg.findChildren(QLabel)]
check("Station Setup: no Telnet username/password fields",
      not hasattr(dlg, "e_telnet_user") and not hasattr(dlg, "e_password")
      and not any(t.startswith("Telnet username") for t in labels))

# ── Settings round trip through the keyring ───────────────────────
check("keyring checkbox offered", dlg.chk_keyring.isEnabled())
check("keyring off by default", not dlg.chk_keyring.isChecked())
dlg.chk_keyring.setChecked(True)
C.store_passwords(dlg._cfg, True)          # what _on_accept does last
newcfg = dlg.get_config()
check("after opting in, config holds no passwords",
      all(b.get("password", "") == "" for b in newcfg["bbs_list"]))
check("...and the keyring holds both entries", len(kr.store) == 2, kr.store)
dlg.close()
dlg2 = M.SettingsDialog(newcfg, parent=w)
check("reopened Settings shows keyring on", dlg2.chk_keyring.isChecked())
check("reopened Settings has the keyring passwords loaded",
      sorted(b.get("password") for b in dlg2._cfg["bbs_list"]) == ["club-pw", "station-pw"])
dlg2.close()
check("connect-time login still resolves from the keyring",
      C.resolve_telnet_login(newcfg, newcfg["bbs_list"][0])[1] == "club-pw")
w.close()

print(f"\n{'PASS' if not fails else 'FAIL'}: bbs logins gui ({len(fails)} failures)")
sys.exit(1 if fails else 0)
