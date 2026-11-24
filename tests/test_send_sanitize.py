"""An outgoing message must not be able to end itself early
(security review 2026-11-23, #5).

A body line starting with /EX closes the message on BPQ, and every line
after it runs as a BBS command (KM included, bypassing the Terminal KM
guard). Ctrl-Z does the same on FBB-style BBSes. Subject/To/@BBS are
single lines: a CR or LF in them would start a new command.
Offline: a fake transport records what would go on the air.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bbs_session as bs

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {extra!r}" if extra else ""))
    if not cond: fails.append(name)

# ── helpers ──────────────────────────────────────────────────────────
nb = bs.neutralize_body
check("/EX line indented", nb(["/EX"]) == [" /EX"], nb(["/EX"]))
check("lower-case /ex indented", nb(["/ex"]) == [" /ex"])
check("/EXIT-style line indented", nb(["/example"]) == [" /example"])
check("already-indented /EX keeps one space", nb(["   /EX"]) == [" /EX"])
check("Ctrl-Z stripped", nb(["abc\x1adef"]) == ["abcdef"])
check("NUL and ESC stripped", nb(["a\x00b\x1b[31mc"]) == ["ab[31mc"])
check("tab kept", nb(["a\tb"]) == ["a\tb"])
normal = ["Hello", "", "  indented", "> quoted /EX", "ends with /EX", "73"]
check("ordinary lines untouched", nb(normal) == normal, nb(normal))

cf = bs.clean_field
check("CR/LF in subject become spaces", cf("hi\rKM\nB") == "hi KM B")
check("control chars dropped from field", cf("a\x1ab") == "ab")
check("None field is empty", cf(None) == "")

# ── end to end through send_message ─────────────────────────────────
class FakeTransport:
    host, port = "fake", 0
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(text)
    def read_until(self, prompt, timeout=None):
        p = prompt.lower()
        if p == "itle":    return "Enter Title (only):"
        if p == "essage":  return "Enter Message Text (end with /ex or ctrl/z)"
        return "Message: 123 Bid:  123_N0CALL Size: 10\r\nde N0CALL>"

t = FakeTransport()
s = bs.BBSSession(t, "N0CALL")
s.log_cb = None
body = "How to finish:\n/EX\nKM\nthen\x1a BYE"
try:
    s.send_message("N0CALL\rKM", "sub\nKM", body, "P")
except Exception as e:      # confirmation parsing may differ; what was sent matters
    print("  (send_message raised after sending:", e, ")")
sent = t.sent
print("  sent:", sent)
check("exactly one bare /EX, and it is last", sent.count("/EX") == 1 and sent[-1] == "/EX", sent)
check("KM stays inside the body (before the only /EX)",
      sent.index("KM") < sent.index("/EX"), sent)
check("SP command is one line", all("\r" not in x and "\n" not in x for x in sent))
check("body /EX went out indented", " /EX" in sent)
check("no Ctrl-Z on the air", not any("\x1a" in x for x in sent))

print(f"\n{'PASS' if not fails else 'FAIL'}: send sanitize ({len(fails)} failures)")
sys.exit(1 if fails else 0)
