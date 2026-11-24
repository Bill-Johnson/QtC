"""The Telnet password goes only to a password prompt
(security review 2026-11-23, #2).

Telnet is cleartext. A mistyped host/port, or something that isn't a BPQ
node, used to receive the password ~10 s after connect anyway. Uses a real
TelnetTransport against a local socket server, so nothing leaves the box.
"""
import os, sys, socket, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bbs_session as bs
from transport import TelnetTransport

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

SECRET = "s3cret-PW"

def server(script, got):
    """Accept one client, send each (wait_for, reply) after the client
    sends something containing wait_for (None = immediately)."""
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    def run():
        c, _ = srv.accept(); c.settimeout(0.3)
        buf = b""; end = time.time() + 25
        steps = list(script)
        while time.time() < end:
            if steps and (steps[0][0] is None or steps[0][0].encode() in buf):
                c.sendall(steps.pop(0)[1].encode()); continue
            try:
                d = c.recv(4096)
                if not d: break
                buf += d; got.append(d)
            except socket.timeout:
                pass
        c.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    return port

def login(port):
    t = TelnetTransport("127.0.0.1", port)
    s = bs.BBSSession(t, "N0CALL")
    s.telnet_user, s.password = "N0CALL", SECRET
    s.log_cb = lambda *_: None
    try:
        return s.connect_and_login()
    finally:
        try: t.disconnect()
        except Exception: pass

# 1. Something that asks for a username, then never for a password.
got = []
port = server([(None, "Welcome to some service\r\nlogin: "),
               ("N0CALL", "Hello N0CALL. Type HELP\r\n")], got)
t0 = time.time()
ok = login(port)
wire = b"".join(got).decode(errors="replace")
check("no password prompt -> login fails", ok is False, ok)
check("password never sent without its prompt", SECRET not in wire, wire)
check("gave up in reasonable time", time.time() - t0 < 30, round(time.time() - t0, 1))

# 2. A normal BPQ-style login still sends it.
got = []
port = server([(None, "user:"), ("N0CALL", "password:"),
               (SECRET, "Connected to node. Enter ? for commands\r\n"),
               ("bbs", "[BPQ-6.0.25.36-IHJM$]\r\nde N0CALL>\r\n")], got)
login(port)
wire = b"".join(got).decode(errors="replace")
check("password sent after a real prompt", SECRET in wire, wire)

# 3. A password typed by hand in the Terminal is masked in the log (#12).
class Peek:
    host, port = "fake", 0
    def __init__(self, buf): self.buf, self.sent = buf, []
    def peek_input(self): return self.buf
    def send(self, t): self.sent.append(t)
logs = []
s = bs.BBSSession(Peek("Welcome\r\nuser:N0CALL\r\nPassword: "), "N0CALL")
s.log_cb = None
s._log = lambda d, t: logs.append(f"[{d}] {t}")
s._send("typed-by-hand")
check("hand-typed password masked after a prompt", logs == ["[TX] ********"], logs)
check("it still went to the BBS", s.transport.sent == ["typed-by-hand"])
logs.clear(); s.transport.buf = "de N0CALL>"
s._send("LM")
check("ordinary command not masked", logs == ["[TX] LM"], logs)

# No login saved for this BBS (2026-11-23): Terminal view sends nothing,
# Mail view refuses before connecting.
got = []
port = server([(None, "Welcome\r\nuser: ")], got)
t = TelnetTransport("127.0.0.1", port)
s = bs.BBSSession(t, "N0CALL", password="", telnet_user="")
logs = []; s._log = lambda d, txt: logs.append(txt)
s.connect_only(); time.sleep(1.0); t.disconnect()
check("no login, Terminal: nothing sent, not even the callsign",
      b"".join(got) == b"", got)
check("no login, Terminal: says to type it",
      any("No login saved" in l for l in logs), logs)
s = bs.BBSSession(TelnetTransport("127.0.0.1", 9), "N0CALL", telnet_user="")
s._log = lambda *_: None
check("no login, Mail view: refused before connecting", s.connect_and_login() is False)
check("callsign is still the default when no login is given",
      bs.BBSSession(TelnetTransport("127.0.0.1", 9), "n0call").telnet_user == "n0call")

print(f"\n{'PASS' if not fails else 'FAIL'}: telnet password gate ({len(fails)} failures)")
sys.exit(1 if fails else 0)
