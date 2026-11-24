"""The --test-api socket needs the per-run token
(security review 2026-11-23, #8).

Without it any local process — or a browser fetch() to 127.0.0.1 — could
type commands or `connect` (keys the transmitter). Offscreen QtC on a spare
port with a throwaway XDG_DATA_HOME; nothing connects to a BBS.
"""
import os, sys, json, socket, tempfile, threading, time, stat
sandbox = tempfile.mkdtemp(prefix="qtc-api-")
os.environ["XDG_DATA_HOME"] = sandbox
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

import main_window as M
cfg_path = os.path.join(M._APP_DIR, "config.json")
json.dump({"user": {"callsign": "N0CALL"}, "bbs_list": [],
           "app": {"data_dir": os.path.join(sandbox, "data")},
           "mail_call": {"enabled": False},
           "bulletins": {"check_on_connect": False, "subscriptions": []}},
          open(cfg_path, "w"))
M._CONFIG_PATH = cfg_path
from PyQt6.QtWidgets import QApplication
app = QApplication([])
w = M.MainWindow()
import qtc_test_api, qtc_ctl

s0 = socket.socket(); s0.bind(("127.0.0.1", 0)); port = s0.getsockname()[1]; s0.close()
api = qtc_test_api.install(w, port)
tok = qtc_test_api.token_path(port)
check("token file written", os.path.exists(tok))
check("token file is owner-only", os.name != "posix" or stat.S_IMODE(os.stat(tok).st_mode) == 0o600)

results = {}
def client():
    # 1. A browser-style POST: nothing it carries may run.
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    c.sendall(b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\nstatus\nview debug\n")
    data = b""
    try:
        while True:
            d = c.recv(4096)
            if not d: break
            data += d
    except OSError:
        pass
    results["http"] = data.decode(errors="replace")
    # 2. Wrong token.
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    results["wrong"] = qtc_ctl.send(c, "auth nope")
    c.close()
    # 3. The real client path.
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    results["auth"] = qtc_ctl.send(c, f"auth {qtc_ctl._token(port)}")
    results["status"] = qtc_ctl.send(c, "status")
    c.close()

th = threading.Thread(target=client); th.start()
end = time.time() + 20
while th.is_alive() and time.time() < end:
    app.processEvents(); time.sleep(0.02)

check("HTTP request refused", '"auth required"' in results.get("http", ""), results.get("http"))
check("HTTP request ran nothing", '"view"' not in results.get("http", ""))
check("wrong token refused", results.get("wrong", {}).get("ok") is False, results.get("wrong"))
check("right token accepted", results.get("auth", {}).get("ok") is True, results.get("auth"))
check("commands work after auth", results.get("status", {}).get("ok") is True, str(results.get("status"))[:80])
api.stop()
check("token file removed on stop", not os.path.exists(tok))
w.close()

print(f"\n{'PASS' if not fails else 'FAIL'}: test api auth ({len(fails)} failures)")
sys.exit(1 if fails else 0)
