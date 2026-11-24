"""config.json must survive crashes and never be silently reset
(security review 2026-11-23, #4).

- save_config writes atomically (temp file + os.replace) under a lock
- a good file is kept as config.json.bak before each save
- a damaged file is kept as config.json.corrupt-<time>, the .bak is
  restored, and _CONFIG_RECOVERY carries a message for the user
- with no good .bak: defaults, still with the damaged file kept
Throwaway temp dir; the real ~/.local/share/qtc is never touched.
"""
import os, sys, json, glob, tempfile, threading
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main_window as M

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

def use(d):
    M._CONFIG_PATH = os.path.join(d, "config.json")
    M._CONFIG_BAK = M._CONFIG_PATH + ".bak"
    M._CONFIG_RECOVERY = None

d = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d)
good = {"user": {"callsign": "N0CALL"}, "bbs_list": [{"name": "A"}]}
M.save_config(good)
check("first save writes the file", M.load_config() == good)
check("no .tmp left behind", not os.path.exists(M._CONFIG_PATH + ".tmp"))
good2 = dict(good, bbs_list=[{"name": "A"}, {"name": "B"}])
M.save_config(good2)
check(".bak holds the previous good copy", json.load(open(M._CONFIG_BAK)) == good)

# Damage the file the way a crash mid-write used to.
open(M._CONFIG_PATH, "w").write('{"user": {"callsign": "N0C')
cfg = M.load_config()
check("damaged file -> last good copy restored", cfg == good, cfg)
check("user is told", bool(M._CONFIG_RECOVERY) and "restored" in M._CONFIG_RECOVERY)
kept = glob.glob(M._CONFIG_PATH + ".corrupt-*")
check("damaged file kept", len(kept) == 1, kept)
check("restored file is on disk", json.load(open(M._CONFIG_PATH)) == good)

# No usable backup -> defaults, still kept and still told.
d2 = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d2)
open(M._CONFIG_PATH, "w").write("not json")
cfg = M.load_config()
check("no backup -> defaults", cfg.get("user", {}).get("callsign") == "NOCALL", cfg)
check("user is told about defaults", bool(M._CONFIG_RECOVERY) and "default" in M._CONFIG_RECOVERY)
check("damaged file kept (no backup case)", len(glob.glob(M._CONFIG_PATH + ".corrupt-*")) == 1)

# Empty file with a good backup (crash after truncate in an old build).
d3 = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d3)
M.save_config(good); M.save_config(good2)
open(M._CONFIG_PATH, "w").close()
check("empty file -> .bak used", M.load_config() == good)

# First run: no file at all -> defaults, no warning.
d4 = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d4)
cfg = M.load_config()
check("first run -> defaults, no warning", cfg.get("user", {}).get("callsign") == "NOCALL" and M._CONFIG_RECOVERY is None)

# Two threads saving at once never leave a broken file.
d5 = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d5)
big = {"bbs_list": [{"name": f"BBS{i}", "notes": "x" * 200} for i in range(200)]}
errs = []
def hammer(tag):
    for i in range(40):
        try: M.save_config(dict(big, tag=tag, i=i))
        except Exception as e: errs.append(e)
ts = [threading.Thread(target=hammer, args=(t,)) for t in "ab"]
[t.start() for t in ts]; [t.join() for t in ts]
check("concurrent saves raised nothing", not errs, errs[:1])
try:
    final = json.load(open(M._CONFIG_PATH)); ok = final.get("i") == 39
except ValueError:
    ok = False
check("file is valid JSON after concurrent saves", ok)

# Owner-only files (security review #3). POSIX only.
if os.name == "posix":
    import stat, importlib
    d6 = tempfile.mkdtemp(prefix="qtc-cfg-"); use(d6)
    open(M._CONFIG_PATH, "w").write(json.dumps(good)); os.chmod(M._CONFIG_PATH, 0o644)
    M.load_config()
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    check("load tightens an old 0644 config to 0600", mode(M._CONFIG_PATH) == 0o600, oct(mode(M._CONFIG_PATH)))
    M.save_config(good); M.save_config(good2)
    check("saved config is 0600", mode(M._CONFIG_PATH) == 0o600, oct(mode(M._CONFIG_PATH)))
    check("config.json.bak is 0600", mode(M._CONFIG_BAK) == 0o600, oct(mode(M._CONFIG_BAK)))
    os.environ["XDG_DATA_HOME"] = d6
    os.chmod(d6, 0o755)
    app_dir = M._get_app_dir()
    check("app dir is 0700", mode(app_dir) == 0o700, oct(mode(app_dir)))
    import database
    dbp = os.path.join(d6, "data", "messages.db")
    database.init_db(dbp)
    check("messages.db is 0600", mode(dbp) == 0o600, oct(mode(dbp)))

print(f"\n{'PASS' if not fails else 'FAIL'}: config safety ({len(fails)} failures)")
sys.exit(1 if fails else 0)
