"""Per-BBS Telnet logins and the optional keyring (2026-11-23).

- a Telnet entry with its own username uses its own login; blank = My
  Station's, as before
- passwords stay in config.json unless use_keyring is on
- switching moves them both ways and never loses one, even when the
  keyring can't be read
A fake keyring backend; the real one is never touched.
"""
import os, sys, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import credentials as C

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

class FakeKR:
    priority = 5
    def __init__(self): self.store, self.locked = {}, False
    def get_keyring(self): return self
    def get_password(self, svc, key):
        if self.locked: raise RuntimeError("locked")
        return self.store.get((svc, key))
    def set_password(self, svc, key, pw):
        if self.locked: raise RuntimeError("locked")
        self.store[(svc, key)] = pw
    def delete_password(self, svc, key):
        if self.locked: raise RuntimeError("locked")
        self.store.pop((svc, key), None)

kr = FakeKR(); C._backend = kr

def cfg():
    return {"user": {"telnet_user": "n0call", "password": "station-pw"},
            "app": {},
            "bbs_list": [
                {"name": "Home", "transport": "telnet", "host": "a.example.com",
                 "telnet_port": 8010},
                {"name": "Club", "transport": "telnet", "host": "b.example.com",
                 "telnet_port": 8010, "telnet_user": "clubuser",
                 "password": "club-pw"},
                {"name": "RF", "transport": "vara_hf"}]}

c = cfg()
check("keyring reported available", C.keyring_status()[0])
check("blank entry -> My Station login",
      C.resolve_telnet_login(c, c["bbs_list"][0]) == ("n0call", "station-pw", "mystation"))
check("entry with own user -> its own login",
      C.resolve_telnet_login(c, c["bbs_list"][1]) == ("clubuser", "club-pw", "bbs"))
check("non-Telnet entry -> My Station (unchanged behavior)",
      C.resolve_telnet_login(c, c["bbs_list"][2])[2] == "mystation")

# File mode (default): passwords stay in the file.
probs = C.store_passwords(c, use_keyring=False)
check("file mode: no problems", probs == [])
check("file mode: passwords kept in config", c["user"]["password"] == "station-pw"
      and c["bbs_list"][1]["password"] == "club-pw")
check("file mode: keyring untouched", kr.store == {})

# Opt in: passwords move into the keyring, out of the file.
C.store_passwords(c, use_keyring=True)
check("keyring mode: no passwords left in config",
      c["user"]["password"] == "" and c["bbs_list"][1]["password"] == "")
check("keyring mode: both in the keyring", len(kr.store) == 2, kr.store)
check("keyring mode: login still resolves",
      C.resolve_telnet_login(c, c["bbs_list"][1])[1] == "club-pw"
      and C.resolve_telnet_login(c, c["bbs_list"][0])[1] == "station-pw")
check("entry got a stable login_id", bool(c["bbs_list"][1].get("login_id")))

# Editing the entry's host keeps the same keyring item.
c["bbs_list"][1]["host"] = "c.example.com"
check("host edit: password still found",
      C.resolve_telnet_login(c, c["bbs_list"][1])[1] == "club-pw")

# Settings round trip: load into a copy, change one, save.
s = C.load_passwords(copy.deepcopy(c))
check("load_passwords fills the Settings copy", s["bbs_list"][1]["password"] == "club-pw")
s["bbs_list"][1]["password"] = "new-club-pw"
C.store_passwords(s, use_keyring=True)
check("changed password saved to keyring",
      C.resolve_telnet_login(s, s["bbs_list"][1])[1] == "new-club-pw")

# Locked keyring when Settings opens: saving must not delete anything.
kr.locked = True
s2 = C.load_passwords(copy.deepcopy(s))
check("locked: fields come up empty", s2["user"]["password"] == "")
kr.locked = False
before = dict(kr.store)
C.store_passwords(s2, use_keyring=True)
check("locked at open: keyring items survive the save", kr.store == before, kr.store)
kr.locked = True
s3 = C.load_passwords(copy.deepcopy(s2)); kr.locked = False
probs = C.store_passwords(s3, use_keyring=False)
check("locked + switch to file: items kept, problem reported",
      kr.store == before and len(probs) == 2, probs)

# Opt back out (keyring readable): passwords come back to the file.
s4 = C.load_passwords(copy.deepcopy(s3))
C.store_passwords(s4, use_keyring=False)
check("back to file: passwords in config again",
      s4["user"]["password"] == "station-pw" and s4["bbs_list"][1]["password"] == "new-club-pw")
check("back to file: keyring emptied", kr.store == {}, kr.store)

# Clearing an entry's username drops its own password.
s4["bbs_list"][1]["telnet_user"] = ""
C.store_passwords(s4, use_keyring=False)
check("cleared username -> entry password removed", "password" not in s4["bbs_list"][1])
check("...and it falls back to My Station",
      C.resolve_telnet_login(s4, s4["bbs_list"][1])[2] == "mystation")

# No keyring package at all.
C._backend = None
check("no package -> not available", C.keyring_status()[0] is False)
check("no package -> file login still works",
      C.resolve_telnet_login(s4, s4["bbs_list"][0])[1] == "station-pw")

print(f"\n{'PASS' if not fails else 'FAIL'}: credentials ({len(fails)} failures)")
sys.exit(1 if fails else 0)
