"""Per-BBS Telnet logins and the optional keyring (2026-11-23).

- a Telnet entry's login is its own; an entry with no username has none
- the pre-0.16 My Station login is moved into every Telnet entry that has
  no login, once, and removed from My Station (keyring too); a locked
  keyring defers the move and the old login keeps working meanwhile
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

def old_cfg():
    """A 0.15.0-style config: one login in My Station."""
    return {"user": {"callsign": "N0CALL", "telnet_user": "n0call",
                     "password": "station-pw"},
            "app": {},
            "bbs_list": [
                {"name": "Home", "transport": "telnet", "host": "a.example.com",
                 "telnet_port": 8010},
                {"name": "Club", "transport": "telnet", "host": "b.example.com",
                 "telnet_port": 8010, "telnet_user": "clubuser",
                 "password": "club-pw"},
                {"name": "RF", "transport": "vara_hf"}]}

# ── before migration: the old login still works ───────────────────
c = old_cfg()
check("keyring reported available", C.keyring_status()[0])
check("unmigrated: blank entry still uses the old login",
      C.resolve_telnet_login(c, c["bbs_list"][0]) == ("n0call", "station-pw", "mystation"))

# ── migration, file mode ──────────────────────────────────────────
moved, probs = C.migrate_station_login(c)
check("migration ran", moved and probs == [], probs)
home, club = c["bbs_list"][0], c["bbs_list"][1]
check("blank entry got the old login",
      home.get("telnet_user") == "n0call" and home.get("password") == "station-pw", home)
check("entry with its own login kept it",
      club["telnet_user"] == "clubuser" and club["password"] == "club-pw")
check("non-Telnet entry untouched", "telnet_user" not in c["bbs_list"][2])
check("My Station login removed",
      not any(k in c["user"] for k in ("telnet_user", "password", "password_in_keyring")),
      c["user"])
check("callsign kept", c["user"]["callsign"] == "N0CALL")
check("migrated entry resolves as its own",
      C.resolve_telnet_login(c, home) == ("n0call", "station-pw", "bbs"))
check("second run does nothing", C.migrate_station_login(c) == (False, []))

# ── an entry with no username has no login ────────────────────────
nolog = {"transport": "telnet", "host": "x.example.com", "telnet_port": 8010}
check("no username -> no login", C.resolve_telnet_login(c, nolog) == ("", "", "none"))
check("login_source agrees", C.login_source(c, nolog) == "none")

# ── migration, keyring mode ───────────────────────────────────────
c = old_cfg(); c["app"]["use_keyring"] = True
C.store_passwords(c, True)                     # 0.16 user with keyring on
check("set-up: both passwords in the keyring", len(kr.store) == 2, kr.store)
moved, probs = C.migrate_station_login(c)
check("keyring migration ran", moved and probs == [], probs)
home = c["bbs_list"][0]
check("migrated entry's password went to the keyring",
      home.get("password") == "" and home.get("password_in_keyring") is True)
check("old My Station keyring item deleted",
      ("QtC", "telnet:mystation") not in kr.store, kr.store)
check("keyring now holds the two entries", len(kr.store) == 2, kr.store)
check("migrated entry resolves from the keyring",
      C.resolve_telnet_login(c, home)[:2] == ("n0call", "station-pw"))
kr.store.clear()

# ── keyring locked at startup: defer, change nothing ──────────────
c = old_cfg(); c["app"]["use_keyring"] = True
C.store_passwords(c, True)
snap = copy.deepcopy(c); kstore = dict(kr.store)
kr.locked = True
moved, probs = C.migrate_station_login(c)
kr.locked = False
check("locked: not moved, one note", not moved and len(probs) == 1, probs)
check("locked: config unchanged", c == snap, c)
check("locked: keyring unchanged", kr.store == kstore)
check("locked: no plain password left in memory",
      all(not h.get("password") for h in [c["user"], *c["bbs_list"]]
          if h.get("password_in_keyring")))
check("locked: old login still the fallback",
      C.login_source(c, c["bbs_list"][0]) == "mystation")
moved, probs = C.migrate_station_login(c)
check("unlocked next start: migration completes", moved and probs == [], probs)
kr.store.clear()

# ── store_passwords round trips (no My Station login any more) ────
c = old_cfg(); C.migrate_station_login(c)
probs = C.store_passwords(c, use_keyring=False)
check("file mode: no problems", probs == [])
check("file mode: keyring untouched", kr.store == {})
C.store_passwords(c, use_keyring=True)
check("keyring mode: no passwords left in config",
      all(e.get("password", "") == "" for e in c["bbs_list"]))
check("keyring mode: both entries in the keyring", len(kr.store) == 2, kr.store)
c["bbs_list"][1]["host"] = "c.example.com"
check("host edit: password still found",
      C.resolve_telnet_login(c, c["bbs_list"][1])[1] == "club-pw")

s = C.load_passwords(copy.deepcopy(c))
check("load_passwords fills the Settings copy", s["bbs_list"][1]["password"] == "club-pw")
s["bbs_list"][1]["password"] = "new-club-pw"
C.store_passwords(s, use_keyring=True)
check("changed password saved to keyring",
      C.resolve_telnet_login(s, s["bbs_list"][1])[1] == "new-club-pw")

kr.locked = True
s2 = C.load_passwords(copy.deepcopy(s))
check("locked: fields come up empty", s2["bbs_list"][0]["password"] == "")
kr.locked = False
before = dict(kr.store)
C.store_passwords(s2, use_keyring=True)
check("locked at open: keyring items survive the save", kr.store == before, kr.store)
kr.locked = True
s3 = C.load_passwords(copy.deepcopy(s2)); kr.locked = False
probs = C.store_passwords(s3, use_keyring=False)
check("locked + switch to file: items kept, problem reported",
      kr.store == before and len(probs) == 2, probs)

s4 = C.load_passwords(copy.deepcopy(s3))
C.store_passwords(s4, use_keyring=False)
check("back to file: passwords in config again",
      s4["bbs_list"][0]["password"] == "station-pw"
      and s4["bbs_list"][1]["password"] == "new-club-pw")
check("back to file: keyring emptied", kr.store == {}, kr.store)

s4["bbs_list"][1]["telnet_user"] = ""
C.store_passwords(s4, use_keyring=False)
check("cleared username -> entry password removed", "password" not in s4["bbs_list"][1])
check("...and it has no login", C.resolve_telnet_login(s4, s4["bbs_list"][1])[2] == "none")

C._backend = None
check("no package -> not available", C.keyring_status()[0] is False)
check("no package -> file login still works",
      C.resolve_telnet_login(s4, s4["bbs_list"][0])[1] == "station-pw")

print(f"\n{'PASS' if not fails else 'FAIL'}: credentials ({len(fails)} failures)")
sys.exit(1 if fails else 0)
