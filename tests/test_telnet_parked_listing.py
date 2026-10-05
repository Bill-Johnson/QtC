"""Telnet login releases a listing parked by an earlier dropped session.

Found 2026-09-29 (BBS log 23:19-23:31 UTC): D8 killed Direwolf while
`l> SITREP` sat at a page prompt. LinBPQ kept that listing parked on the
user record, so every later entry to the BBS - RF or Telnet - got the SID
and then silence, no "de N0CALL>". The RF login already answered 'A'
(7cb0991); the Telnet login waited 15 s for a '>' and gave up with
"No BBS prompt received". Both now share _await_bbs_prompt.

A fake clock stands in for the silence waits, so this runs instantly.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import bbs_session as bs

fails = 0
def check(name, cond, extra=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))


class Clock:
    now = 1_000_000.0
    def time(self): return self.now

clock = Clock()


class FakeTelnet:
    """Scripted LinBPQ telnet port. `after_bbs` is what the BBS sends once
    `bbs` is typed; `after_a` what it sends if QtC answers 'A'. Reads with
    nothing queued advance the fake clock by their timeout."""
    host, port = "192.168.1.50", 8110

    def __init__(self, after_bbs, after_a=""):
        self.after_bbs, self.after_a = after_bbs, after_a
        self.queue, self.sent = [], []

    def connect(self): self.queue.append("Username:")
    def send(self, data):
        self.sent.append(data)
        if data == "TESTUSER":
            self.queue.append("Password:")
        elif data == "pw":
            self.queue.append("Welcome to NWI LinBPQ Telnet Server\r\n"
                              "Enter ? for list of commands\r\n")
        elif data == "bbs":
            self.queue.extend(self.after_bbs)
        elif data == "A":
            self.queue.append(self.after_a)

    def _take(self, timeout):
        if self.queue:
            return self.queue.pop(0)
        clock.now += timeout
        return ""
    def read_until(self, expected, timeout=15): return self._take(timeout)
    def read_until_any(self, markers, timeout=10):
        got = self._take(timeout)
        return got, next((m for m in markers if m.lower() in got.lower()), None)
    def read_all_pending(self, settle_time=0.5):
        clock.now += settle_time
        return ""
    def abort_requested(self): return False


def login(tr):
    s = bs.BBSSession(tr, "N0CALL", password="pw", telnet_user="TESTUSER")
    s._log = lambda *a: None
    real = time.time
    time.time = clock.time
    try:
        t0 = clock.now
        ok = s._telnet_login()
        return ok, s, clock.now - t0
    finally:
        time.time = real


SID = "MYNODE:N0CALL-7} Connected to BBS\r\n[BPQ-6.0.25.36-IHJM$]\r\n"
PROMPT = "de N0CALL>"

# 1. Normal entry - prompt straight away, no 'A' ever sent.
tr = FakeTelnet([SID + PROMPT])
ok, s, _ = login(tr)
check("normal login succeeds", ok)
check("normal login never sends A", "A" not in tr.sent, tr.sent)
check("normal login finds the prompt", s.PROMPT_BBS == PROMPT, s.PROMPT_BBS)

# 2. The 2026-09-29 case - SID, then silence. 'A' after the nudge.
tr = FakeTelnet([SID], after_a=PROMPT)
ok, s, took = login(tr)
check("parked (silent) login succeeds", ok)
check("parked (silent) sends exactly one A", tr.sent.count("A") == 1, tr.sent)
check("A goes only after bbs", tr.sent.index("A") > tr.sent.index("bbs"), tr.sent)
check("parked (silent) finds the prompt", s.PROMPT_BBS == PROMPT, s.PROMPT_BBS)
check("A waits for the Telnet nudge, not the RF one",
      bs.BBSSession.TELNET_NUDGE_SECS <= took < bs.BBSSession.LOGIN_NUDGE_SECS,
      f"{took:.1f}s")

# 3. Parked, but the BBS re-shows the menu - 'A' at once.
MENU = "<A>bort, <R Msg(s)>, <CR> = Continue..>"
tr = FakeTelnet([SID + MENU], after_a=PROMPT)
ok, s, took = login(tr)
check("parked (menu shown) login succeeds", ok)
check("parked (menu shown) sends exactly one A", tr.sent.count("A") == 1, tr.sent)
check("parked (menu shown) does not wait for the nudge",
      took < bs.BBSSession.TELNET_NUDGE_SECS, f"{took:.1f}s")

# 4. 'A' changes nothing - one A only, then an honest failure.
tr = FakeTelnet([SID], after_a="")
ok, s, _ = login(tr)
check("BBS that never answers: login fails", not ok)
check("BBS that never answers: still only one A", tr.sent.count("A") == 1, tr.sent)

print(f"\n{'OK' if not fails else 'FAILURES'} — {fails} failure(s)")
sys.exit(1 if fails else 0)
