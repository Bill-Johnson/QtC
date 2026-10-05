"""End-to-end through the real TelnetTransport reader thread against a fake
LinBPQ telnet server — proves the splitter change works in the live path,
not just in isolation, and that read_until() still matches unchanged."""
import os, socket, threading, time, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import TelnetTransport

SCRIPT = [
    b"Username: ",
    b"Password: ",
    b"Welcome to NWI LinBPQ Telnet Server\r\nEnter ? for list of commands\r\n\r\n",
    b"MYNODE:N0CALL-7} Invalid command - Enter ? for command list\r",
    b"MYNODE:N0CALL-7} Connected to BBS\r[BPQ-6.0.25.36-IHJM$]\rde N0CALL>",
]

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]

def serve():
    c, _ = srv.accept()
    for part in SCRIPT:
        c.sendall(part)
        time.sleep(0.25)          # separate reads, as on a real link
    time.sleep(0.5)
    c.close()

threading.Thread(target=serve, daemon=True).start()

lines = []
t = TelnetTransport("127.0.0.1", port)
t._log = lambda tag, msg: lines.append((tag, msg))
t.set_terminal_mode(True)
t.connect()

# read_until must still find the prompts in the shared buffer
got_user = t.read_until("username:", timeout=5)
got_pass = t.read_until("password:", timeout=5)
time.sleep(1.8)
t.disconnect()

rx = [m for tag, m in lines if tag == "RX"]
print("[RX] lines:")
for r in rx:
    print(f"   {r!r}")

want = [
    "Username:",
    "Password:",
    "Welcome to NWI LinBPQ Telnet Server",
    "Enter ? for list of commands",
    "MYNODE:N0CALL-7} Invalid command - Enter ? for command list",
    "MYNODE:N0CALL-7} Connected to BBS",
    "[BPQ-6.0.25.36-IHJM$]",
    "de N0CALL>",
]

fails = 0
ok = rx == want
fails += not ok
print(f"\n{'PASS' if ok else 'FAIL'}  [RX] stream matches expected")
if not ok:
    print(f"   want {want}")

for name, got, needle in (("read_until username", got_user, "username:"),
                          ("read_until password", got_pass, "password:")):
    good = needle in got.lower()
    fails += not good
    print(f"{'PASS' if good else 'FAIL'}  {name} still matches")

print(f"\n{3 - fails}/3 passed")
sys.exit(1 if fails else 0)
