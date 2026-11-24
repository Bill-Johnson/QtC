"""Receive limits (security review 2026-11-23, #6).

1. A Telnet host streaming text with no prompt: the receive buffer stays
   bounded and read_until gives up instead of waiting forever (the
   silence timeout never fires while bytes keep coming).
2. YAPP stops a sender that goes past its own header's size, and anything
   past YappReceiver.MAX_FILE_BYTES.
Local socket / scripted bytes only.
"""
import os, sys, socket, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transport as tr
import bbs_session as bs

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

# ── 1. endless stream, no prompt ───────────────────────────────────
tr.MAX_RX_BUFFER = 256 * 1024          # keep the test quick; same code path
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]
stop = threading.Event()
def flood():
    c, _ = srv.accept()
    line = b"spam spam spam spam spam spam spam spam spam spam spam\r\n"
    try:
        while not stop.is_set():
            c.sendall(line * 50); time.sleep(0.002)
    except OSError:
        pass
    c.close(); srv.close()
threading.Thread(target=flood, daemon=True).start()

t = tr.TelnetTransport("127.0.0.1", port)
t.connect()
t0 = time.time()
got = t.read_until("de N0CALL>", timeout=15)
took = time.time() - t0
check("read_until returns while the flood continues", took < 14, round(took, 1))
check("it returned the capped amount, not a match", "de N0CALL>" not in got and len(got) >= tr.MAX_RX_BUFFER - 4096, len(got))
time.sleep(1.0)
with t._lock:
    size = len(t._buf)
check("buffer stays bounded while nobody reads", size <= tr.MAX_RX_BUFFER, size)
stop.set()
try: t.disconnect()
except Exception: pass

# ── 2. YAPP size limits ─────────────────────────────────────────────
class Wire:
    def __init__(self, data): self.data, self.pos, self.out = data, 0, bytearray()
    def read_raw_bytes(self, n, timeout=10.0):
        c = self.data[self.pos:self.pos + n]; self.pos += len(c); return c
    def send_raw(self, d): self.out += d
    def abort_requested(self): return False
    def clear_abort(self): pass

def wire(size_field, nblocks):
    hdr = b"big.bin\x00" + str(size_field).encode() + b"\x00"
    blk = bytes([bs._YAPP_STX, 0]) + b"x" * 256           # len 0 = 256 bytes
    return (bytes([bs._YAPP_ENQ, 1, bs._YAPP_SOH, len(hdr)]) + hdr
            + blk * nblocks + bytes([bs._YAPP_ETX, 1]))

def run(w, max_bytes=None):
    r = bs.YappReceiver(w, log_cb=lambda *a: None)
    r.DRAIN_TOTAL_TIMEOUT, r.DRAIN_QUIET_SECS = 0.3, 0.05
    if max_bytes: r.MAX_FILE_BYTES = max_bytes
    try:
        return r.receive(), None
    except IOError as e:
        return None, str(e)

res, err = run(Wire(wire(512, 2)))
check("honest sender (512 bytes in 2 blocks) still completes", res and len(res[1]) == 512, err)
res, err = run(Wire(wire(300, 6)))
check("sender past its header size is stopped", res is None and "larger" in (err or ""), err)
w = Wire(wire(0, 20))
res, err = run(w, max_bytes=2048)
check("unknown-size sender stopped at MAX_FILE_BYTES", res is None and "larger" in (err or ""), err)
check("CN went out when stopping", bytes([bs._YAPP_CAN]) in bytes(w.out))

# ── 3. YAPP save name (security review #8) ─────────────────────────
sn = bs.safe_download_name
check("normal name kept", sn("notes.txt") == "notes.txt")
check("dot-file loses its dot", sn(".bash_aliases") == "bash_aliases", sn(".bash_aliases"))
check("path parts stripped", "/" not in sn("../../.profile") and not sn("../../.profile").startswith("."), sn("../../.profile"))
check("Windows device name made safe", sn("CON") != "CON" and sn("com1.txt").upper() != "COM1.TXT", (sn("CON"), sn("com1.txt")))
check("empty -> fallback", sn("") == "yapp_download" and sn("...") == "yapp_download")
check("non-ASCII dropped", sn("fi\u2215le.txt") == "file.txt", sn("fi\u2215le.txt"))
src = open(bs.__file__, encoding="utf-8").read()
check("download_file saves under the requested name",
      "save_name = safe_download_name(asked or rcvd_name)" in src)

# ── 4. Post-login drain stops under a flood ─────────────────────────
src = open(bs.__file__, encoding="utf-8").read()
check("post-login drain is capped", "if len(bbs_response) > 64 * 1024:" in src)

print(f"\n{'PASS' if not fails else 'FAIL'}: rx limits ({len(fails)} failures)")
sys.exit(1 if fails else 0)
