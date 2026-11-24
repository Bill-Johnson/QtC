# QtC v0.15.0-beta — credentials.py  (built 2026-11-23)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Telnet logins: which one a BBS uses, and where its password is kept.

A login lives on its BBS entry: entry["telnet_user"] / ["password"]
(Bill, 2026-11-23). An entry with no username has no login: Terminal view
shows the BBS's prompts and you type it; Mail view and Mail-Call won't
connect.

Up to 0.15.0 there was one login for every BBS, in My Station
(config["user"]["telnet_user"] / ["password"]). migrate_station_login()
copies it once into every Telnet entry that has none and removes it from
My Station. Until that has happened (keyring locked at startup) the old
login still works as the fallback, so nothing breaks in between.

Two places a password can be stored:

  config.json  the default. The file is owner-only (0600).
  keyring      opt-in: config["app"]["use_keyring"]. Windows Credential
               Manager or the desktop keyring. config.json then holds no
               password, only "password_in_keyring": true on that holder.

Passwords move between the two only when Settings is saved
(store_passwords), so Cancel never leaves half a move behind. While
Settings is open its copy of the config holds the real passwords
(load_passwords) so the fields can show them.
"""

import uuid

SERVICE = "QtC"
MYSTATION_KEY = "mystation"

try:                                    # optional dependency
    import keyring as _keyring
except Exception:                       # not installed, or broken install
    _keyring = None

# Tests swap in a fake backend here.
_backend = _keyring


def keyring_status():
    """(available: bool, reason: str). Never raises."""
    if _backend is None:
        return False, ("the 'keyring' Python package is not installed "
                       "(pip install keyring)")
    try:
        kr = _backend.get_keyring()
        name = type(kr).__module__ + "." + type(kr).__name__
        prio = getattr(kr, "priority", 1)
        if "fail" in name.lower() or "null" in name.lower() or prio <= 0:
            return False, "no system keyring was found on this computer"
        return True, ""
    except Exception as e:
        return False, f"the system keyring could not be opened ({e})"


def _key_for(holder_key):
    return f"telnet:{holder_key}"


def _entry_key(entry):
    """Stable id for an entry's login, so editing its host or name doesn't
    orphan the keyring item."""
    if not entry.get("login_id"):
        entry["login_id"] = uuid.uuid4().hex[:12]
    return f"bbs:{entry['login_id']}"


def _has_station_login(u):
    return bool((u.get("telnet_user") or "").strip() or u.get("password")
                or u.get("password_in_keyring"))


def _holders(cfg):
    """(holder_key, dict) for every Telnet entry that has its own login,
    plus the old My Station login while it is still waiting to be
    migrated."""
    out = []
    u = cfg.setdefault("user", {})
    if _has_station_login(u):
        out.append((MYSTATION_KEY, u))
    for e in cfg.get("bbs_list", []) or []:
        if e.get("transport") == "telnet" and e.get("telnet_user"):
            out.append((_entry_key(e), e))
    return out


def _get(holder_key, holder):
    if not holder.get("password_in_keyring"):
        return holder.get("password", "") or ""
    if _backend is None:
        return ""
    try:
        return _backend.get_password(SERVICE, _key_for(holder_key)) or ""
    except Exception:
        return ""


def has_own_login(entry):
    return bool(entry and entry.get("transport") == "telnet"
                and (entry.get("telnet_user") or "").strip())


def login_source(cfg, entry):
    """"bbs", "mystation" (old login, not yet migrated) or "none".
    Reads no password, so it never touches the keyring."""
    if has_own_login(entry):
        return "bbs"
    u = cfg.get("user", {})
    if (u.get("telnet_user") or "").strip():
        return "mystation"
    return "none"


def resolve_telnet_login(cfg, entry):
    """(username, password, source) for a Telnet connect to `entry`.
    source as login_source(); "none" gives ("", "", "none"). The password
    is read from the keyring here, at connect time, when that is where it
    lives."""
    src = login_source(cfg, entry)
    if src == "bbs":
        return (entry["telnet_user"].strip(),
                _get(_entry_key(entry), entry), "bbs")
    if src == "mystation":
        u = cfg.get("user", {})
        return (u["telnet_user"].strip(), _get(MYSTATION_KEY, u), "mystation")
    return ("", "", "none")


def migrate_station_login(cfg):
    """Move the pre-0.16 My Station login into every Telnet entry that has
    no login of its own, then remove it from My Station (Bill, 2026-11-23).
    Returns (changed, problems). changed=False with no problems means
    there was nothing to do. If the old password is in a keyring that
    can't be read right now, nothing changes and the next start tries
    again; the old login keeps working meanwhile."""
    u = cfg.setdefault("user", {})
    if not _has_station_login(u):
        for k in ("telnet_user", "password", "password_in_keyring"):
            u.pop(k, None)
        return False, []
    load_passwords(cfg)
    if u.get("_keyring_unread"):
        # Undo load_passwords' marks; leave everything as it was.
        for _, h in _holders(cfg):
            if h.pop("_keyring_unread", False) or h.get("password_in_keyring"):
                h["password"] = ""
        return False, ["My Station login: keyring locked, will move it "
                       "into your Telnet BBS entries next time"]
    user = (u.get("telnet_user") or "").strip()
    pw = u.get("password", "") or ""
    if user:
        for e in cfg.get("bbs_list", []) or []:
            if e.get("transport") == "telnet" and not has_own_login(e):
                e["telnet_user"] = user
                e["password"] = pw
                e.pop("password_in_keyring", None)
    for k in ("telnet_user", "password", "password_in_keyring"):
        u.pop(k, None)
    problems = store_passwords(
        cfg, bool(cfg.get("app", {}).get("use_keyring")))
    return True, problems


def load_passwords(cfg):
    """Fill every holder's "password" from wherever it is stored. Used on
    the Settings dialog's private copy so its fields show real values.
    A keyring item that can't be read (locked, gone) is marked so that
    saving Settings leaves it alone instead of deleting it."""
    for key, h in _holders(cfg):
        if not h.get("password_in_keyring"):
            continue
        pw = None
        if _backend is not None:
            try:
                pw = _backend.get_password(SERVICE, _key_for(key))
            except Exception:
                pw = None
        if pw is None:
            h["_keyring_unread"] = True
            h["password"] = ""
        else:
            h["password"] = pw
    return cfg


def store_passwords(cfg, use_keyring):
    """Put every password where the setting says, on Settings save.
    `cfg` holds plain passwords (from load_passwords + the dialog).
    Returns a list of problems (empty = all fine); on any keyring failure
    that password stays in config.json so it is never lost."""
    problems = []
    app = cfg.setdefault("app", {})
    app["use_keyring"] = bool(use_keyring)
    wanted = set()
    for key, h in _holders(cfg):
        pw = h.get("password", "") or ""
        if h.pop("_keyring_unread", False) and not pw:
            # Couldn't read it when Settings opened and nobody typed a new
            # one: keep the keyring item exactly as it was, even when
            # switching back to the file — it can't be copied out unread.
            h["password"] = ""
            h["password_in_keyring"] = True
            wanted.add(key)
            if not use_keyring:
                problems.append(f"{key}: still in the keyring (it could "
                                f"not be read, so it was not moved)")
            continue
        if use_keyring and pw:
            try:
                _backend.set_password(SERVICE, _key_for(key), pw)
                h["password"] = ""
                h["password_in_keyring"] = True
                wanted.add(key)
                continue
            except Exception as e:
                problems.append(f"{key}: {e}")
        h.pop("password_in_keyring", None)
        h["password"] = pw
    # Entries that dropped their own login keep no password of their own.
    for e in cfg.get("bbs_list", []) or []:
        if not has_own_login(e):
            e.pop("password", None)
            e.pop("password_in_keyring", None)
    # Remove keyring items QtC no longer uses (moved back to the file,
    # entry deleted, login cleared).
    for key in set(app.get("keyring_keys", [])) - wanted:
        if _backend is not None:
            try:
                _backend.delete_password(SERVICE, _key_for(key))
            except Exception:
                pass
    app["keyring_keys"] = sorted(wanted)
    return problems
