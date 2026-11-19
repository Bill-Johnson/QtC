#!/usr/bin/env python3
"""AGW reads must work with the shared (borrowed) VaraTransport readers.

a737367 ("let the modem decide") made read_until / read_until_any /
read_raw_bytes call self._silence_over(); AGWTransport borrows those
readers but did not have the helper, so every packet read that did not
match at once raised AttributeError. No radio, no modem: the buffer is
filled by hand."""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import transport as t

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

def make():
    a = t.AGWTransport("127.0.0.1", 8000, "N0CALL", "N0CALL-1")
    a.connected = True
    a._log = lambda *x: None
    return a

# 1. read_until that has to wait, then matches
a = make()
threading.Timer(0.3, lambda: setattr(a, "_data_buf", b"de N0CALL>")).start()
try:
    r = a.read_until(">", timeout=5)
    check("read_until waits and matches", r.endswith(">"))
except Exception as e:
    check(f"read_until waits and matches ({type(e).__name__}: {e})", False)

# 2. read_until that times out on silence
a = make()
t0 = time.time()
try:
    r = a.read_until(">", timeout=0.4)
    check("read_until times out on silence", r == "" and time.time() - t0 < 3)
except Exception as e:
    check(f"read_until times out on silence ({type(e).__name__}: {e})", False)

# 3. read_until_any
a = make()
threading.Timer(0.2, lambda: setattr(a, "_data_buf", b"text\r<A>bort, <CR> Continue..>")).start()
try:
    txt, m = a.read_until_any(["<A>bort", ">"], timeout=5)
    check("read_until_any matches the earliest", m == "<A>bort")
except Exception as e:
    check(f"read_until_any matches ({type(e).__name__}: {e})", False)

# 4. read_raw_bytes
a = make()
threading.Timer(0.2, lambda: setattr(a, "_data_buf", b"\x05\x01")).start()
try:
    b = a.read_raw_bytes(2, timeout=5)
    check("read_raw_bytes gets its bytes", b == b"\x05\x01")
except Exception as e:
    check(f"read_raw_bytes ({type(e).__name__}: {e})", False)

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
