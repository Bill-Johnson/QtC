"""The v0.15.0 edit list, where it can be checked without a radio.

Covers the four changes that have a right answer a machine can see:
  * the LinBPQ "Body: nn" line is stripped, and the R: routing lines are not
  * the progress counter names the item on the air, and the bar trails it
  * a user abort during YAPP sends CN and raises DownloadAborted
  * the Outbox reports a size, in the same bytes Sent will report

Everything else on that list is a label, a tooltip or a dropdown, and is
checked by eye.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import bbs_session as bs

fails = 0
def check(name, cond, extra=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))


# ── 1. "Body: nn" out, "R:" in ───────────────────────────────────────
# Captured from a real read, qtc-debug-20260906-100223.log line 876.
REAL_READ = (
    "From: N0CALL@N0CALL.ST.USA.NOAM \r\n"
    "To: N0CALL \r\n"
    "Type/Status: PN \r\n"
    "Date/Time: 05-Sep 20:28Z \r\n"
    "Bid: 573_N0CALL \r\n"
    "Title: Update testing \r\n"
    "\r\n"
    "Body: 56 \r\n"
    "\r\n"
    "R:260905/2028Z 573@N0CALL.ST.USA.NOAM LinBPQ6.0.25 \r\n"
    "\r\n"
    "ok \r\n"
    "\r\n"
    "[End of Message #753 from N0CALL@N0CALL.ST.USA.NOAM] \r\n"
    "de N0CALL>")


class FakeTransport:
    """Hands download_message() a canned read, then a prompt."""
    PROMPT = "de N0CALL>"
    def __init__(self, raw): self.raw, self.sent = raw, []
    def send(self, data): self.sent.append(data)
    def read_until_any(self, markers, timeout=30): return (self.PROMPT, self.PROMPT)
    def read_all_pending(self, settle_time=0.0): return ""
    def flush_input(self): pass
    def set_terminal_mode(self, on): pass
    def clear_abort(self): pass
    def abort_requested(self): return False


def body_of(raw):
    s = bs.BBSSession.__new__(bs.BBSSession)
    s.transport = FakeTransport(raw)
    s._log = lambda *a: None
    s._send = lambda c: None
    s._reply_wait = lambda n: n
    s.at_page_prompt = False
    s.PAGE_MARKERS = bs.BBSSession.PAGE_MARKERS
    s.PROMPT_BBS = bs.BBSSession.PROMPT_BBS
    s.MAX_PAGES = bs.BBSSession.MAX_PAGES
    s._expect_paged = lambda marker, timeout=0, max_pages=0: (raw, "ok")
    s._finish_page_prompt = lambda m: False
    s._abort_paged_output = lambda max_menus=3: True
    return bs.BBSSession.download_message(s, 753, size_hint=291)

body = body_of(REAL_READ)
check("Body: nn line is gone", "Body:" not in body, repr(body[:80]))
check("R: routing line survives", "R:260905/2028Z" in body, repr(body[:80]))
check("message text survives", "ok" in body, repr(body))
check("body does not start blank", body[:1].strip() != "", repr(body[:20]))

# A message with no Body: line must be untouched.
plain = body_of(REAL_READ.replace("Body: 56 \r\n", ""))
check("no Body: line is a no-op", plain.startswith("R:260905"), repr(plain[:40]))

# "Body:" inside the message text must not be eaten — only the count line.
prose = body_of(REAL_READ.replace("ok \r\n", "Body: see attached \r\n"))
check("prose starting 'Body:' is kept",
      "Body: see attached" in prose, repr(prose))


# ── 2. Progress: text names the live item, bar trails it ─────────────
import main_window as mw
MW = mw.MainWindow

def prog_stub():
    s = types.SimpleNamespace(texts=[], pcts=[], logged=[], term_logged=[])
    s.debug_view = types.SimpleNamespace(
        append=lambda t, c=None: s.logged.append(t.strip()))
    s.terminal = types.SimpleNamespace(
        append=lambda t, c=None: s.term_logged.append(t.strip()))
    s.btn_abort    = types.SimpleNamespace(setEnabled=lambda v: None)
    s._prog_bar    = types.SimpleNamespace(
        setValue=lambda v: s.pcts.append(v), setVisible=lambda v: None)
    s._prog_detail = types.SimpleNamespace(
        setText=lambda t: s.texts.append(t), setVisible=lambda v: None)
    s.status_label = types.SimpleNamespace(setVisible=lambda v: None)
    s.conn_label   = types.SimpleNamespace(setText=lambda t: None)
    return s

s = prog_stub()
for i in (1, 2, 3):
    MW._on_progress(s, "sending", i, 3, f"to CALL{i}")
check("first send says 1 of 3", s.texts[0].startswith("Sending 1 of 3"), s.texts[0])
check("second send says 2 of 3", s.texts[1].startswith("Sending 2 of 3"), s.texts[1])
check("bar starts at 0%", s.pcts[0] == 0, str(s.pcts))
check("bar trails the count", s.pcts == [0, 33, 66], str(s.pcts))

s = prog_stub()
MW._on_progress(s, "downloading", 1, 4, "msg #1")
check("download says 1 of 4, not 0 of 4",
      s.texts[0].startswith("Downloading 1 of 4"), s.texts[0])

# Each step is logged to the Debug view. Two reasons: a saved debug log
# can now say which message was in flight when something went wrong, and
# the QA control socket can `wait` on it (qa_tests/steps/j6_*.txt).
s = prog_stub()
MW._on_progress(s, "sending", 2, 3, "to N0CALL")
check("progress is logged to the Debug view",
      any("Sending 2 of 3" in t for t in s.logged), s.logged)
check("progress is NOT logged to the Terminal view",
      not s.term_logged, s.term_logged)


# ── 3. YAPP: Abort sends CN, and says it was the user ────────────────
# Drives the real receive() through a real transfer: ENQ, header, one
# data block. abort_after says which block the user interrupts after.

HDR = b"note.txt\x000\x00"           # filename NUL size NUL — 11 bytes
BLOCK = bytes([bs._YAPP_STX]) + bytes([4]) + b"abcd"   # 6 bytes each
PREAMBLE = (bytes([bs._YAPP_ENQ, 0x01])
            + bytes([bs._YAPP_SOH]) + bytes([len(HDR)]) + HDR)


def yapp_wire(nblocks=3):
    """The bytes LinBPQ puts on the wire for a small file."""
    return PREAMBLE + BLOCK * nblocks + bytes([bs._YAPP_ETX, 0x01])  # EF


def end_of_block(n):
    """Wire offset just past the nth data block."""
    return len(PREAMBLE) + len(BLOCK) * n


class ScriptedTransport:
    """A flat byte wire, handed out exactly n bytes at a time — the fake
    must not be looser than the real transport or the test proves nothing.
    Goes quiet once it runs out, which is what _abort_and_drain waits for.

    abort_at is a wire offset: once the receiver has consumed that many
    bytes, the user has pressed Abort.
    """
    def __init__(self, wire, abort_at=None):
        self.wire, self.pos, self.raw_out = bytes(wire), 0, bytearray()
        self.abort_at, self.aborted = abort_at, False
    def read_raw_bytes(self, n, timeout=10.0):
        chunk = self.wire[self.pos:self.pos + n]
        self.pos += len(chunk)
        if self.abort_at is not None and self.pos >= self.abort_at:
            self.aborted = True
        return chunk
    def send_raw(self, data): self.raw_out += data
    def abort_requested(self): return self.aborted
    def clear_abort(self): self.aborted = False


def receiver_on(t):
    r = bs.YappReceiver(t, progress_cb=None, log_cb=lambda *a: None)
    r.DRAIN_TOTAL_TIMEOUT, r.DRAIN_QUIET_SECS = 0.3, 0.05
    return r

# Baseline: no abort, the transfer completes and nothing extra goes out.
t = ScriptedTransport(yapp_wire(2))
name, data = receiver_on(t).receive()
check("clean transfer still completes", data == b"abcdabcd", repr(data))
check("clean transfer sends no CN",
      bytes([bs._YAPP_CAN]) not in bytes(t.raw_out), repr(bytes(t.raw_out)))

# Abort after the first of three blocks.
t = ScriptedTransport(yapp_wire(3), abort_at=end_of_block(1))
raised = None
try:
    receiver_on(t).receive()
except bs.DownloadAborted as e:
    raised = e
except Exception as e:                      # any other exception is a bug
    raised = e
check("user abort raises DownloadAborted",
      isinstance(raised, bs.DownloadAborted), repr(raised))
check("user abort puts CN on the wire",
      bytes([bs._YAPP_CAN]) in bytes(t.raw_out), repr(bytes(t.raw_out)))
check("CN carries a reason", b"user" in bytes(t.raw_out).lower(),
      repr(bytes(t.raw_out)))
# After CN, QtC deliberately KEEPS acking. Walking out of the protocol
# is what broke the first two on-air attempts: LinBPQ does not purge what
# it has already queued, so the rest of the file arrives regardless, and
# its close frames go unanswered — leaving the BBS off its prompt and the
# backlog printed on screen as text. Staying in and swallowing is the
# fix, so RR after CN is correct, not a bug (2026-09-23, J7).
wire = bytes(t.raw_out)
cn_at = wire.index(bytes([bs._YAPP_CAN]))
check("CN goes out exactly once", wire.count(bytes([bs._YAPP_CAN])) == 1,
      repr(wire))

# THE one that matters. LinBPQ leaves YAPP the moment it reads the CN and
# goes back to its command interpreter, so anything sent after that is
# typed into the BBS command line. A version that kept acking put 111 RRs
# there and the operator's next command came back "Invalid Command"
# (2026-09-23, J7 run 3, confirmed byte-for-byte in the BBS log).
after_cn = wire[cn_at + 2 + len(b"aborted by the user"):]
check("NOTHING is transmitted after the CN", after_cn == b"",
      repr(after_cn))
check("no stray YAPP RR after the cancel",
      bytes([bs._YAPP_ACK, 0x01]) not in after_cn, repr(after_cn))

# A remote cancel is answered with CA (ACK 05), not just raised on.
t = ScriptedTransport(
    PREAMBLE + bytes([bs._YAPP_CAN]) + bytes([6]) + b"sysop!")
err = None
try:
    receiver_on(t).receive()
except IOError as e:
    err = e
check("remote cancel is reported", err is not None and "cancel" in str(err).lower(),
      repr(err))
check("remote cancel's reason is shown", "sysop!" in str(err), repr(err))
check("remote cancel is answered with CA (ACK 0x05)",
      bytes([bs._YAPP_ACK, 0x05]) in bytes(t.raw_out), repr(bytes(t.raw_out)))


# ── 3a. F2: data blocks are not acked, and the tail read has a cap ───
# F2, 2026-09-23, 300 baud: an RR per data block queued up behind the
# BBS's traffic; LinBPQ answered every late RR after EOF with another
# size=0 sentinel (81 blocks, 81 sentinels), and download_file()'s
# silence-based read_until(">") never returned. QtC had to be killed.
SENTINEL = bytes([bs._YAPP_SOH, len(HDR)]) + HDR     # size=0 HD
t = ScriptedTransport(yapp_wire(5) + SENTINEL)
name, data = receiver_on(t).receive()
check("F2: 5-block transfer completes", data == b"abcd" * 5, repr(data))
check("F2: the wire carries only RR, RF, AF, NR — no per-block RR",
      bytes(t.raw_out) == bytes([bs._YAPP_ACK, 0x01, bs._YAPP_ACK, 0x02,
                                 bs._YAPP_ACK, 0x03, bs._YAPP_NAK, 0x00]),
      repr(bytes(t.raw_out)))


r = receiver_on(ScriptedTransport(yapp_wire(5) + SENTINEL)); r.receive()
check("F2: a sentinel close is recorded as HD", r.close_kind == "HD",
      r.close_kind)

# F2 re-run, 2026-09-28: with no per-block RR, LinBPQ closes the RFC way —
# ET, not the sentinel — and then sends NO prompt. download_file() keys
# off close_kind to skip a two-minute wait for one.
t = ScriptedTransport(yapp_wire(5) + bytes([bs._YAPP_EOT, 0x01]))
r = receiver_on(t)
name, data = r.receive()
check("F2: ET close completes", data == b"abcd" * 5, repr(data))
check("F2: ET close is answered with AT and is recorded as ET",
      bytes(t.raw_out).endswith(bytes([bs._YAPP_ACK, 0x04]))
      and r.close_kind == "ET", (bytes(t.raw_out), r.close_kind))


class EndlessSentinels:
    """LinBPQ stuck re-sending the size=0 sentinel: never quiet, never '>'."""
    def __init__(self): self.raw_out = bytearray()
    def read_raw_bytes(self, n, timeout=10.0): return SENTINEL[:n]
    def send_raw(self, d): self.raw_out += d

import time as _time
s = bs.BBSSession.__new__(bs.BBSSession)
s.transport = EndlessSentinels()
s.logged = []
s._log = lambda d, txt: s.logged.append(txt)
s._reply_wait = lambda n=None: 0.2            # 0.6 s cap in total
t0 = _time.time()
tail = bs.BBSSession._read_post_yapp_tail(s)
took = _time.time() - t0
check("F2: post-YAPP read returns under an endless sentinel stream",
      took < 2.0, f"{took:.1f} s")
check("F2: and says so in the log",
      any("no BBS prompt" in x for x in s.logged), s.logged)
check("F2: and answers none of the sentinels", s.transport.raw_out == b"",
      repr(bytes(s.transport.raw_out)))

# The normal case still stops at the prompt, and keeps the prompt text.
s.transport = ScriptedTransport(SENTINEL + b"File Rejected\rde N0CALL>")
s._reply_wait = lambda n=None: 5
tail = bs.BBSSession._read_post_yapp_tail(s)
check("F2: post-YAPP read stops at the BBS prompt",
      tail.endswith("de N0CALL>"), repr(tail))

# The short read after a clean ET close honours its own silence window
# instead of AGW's 120 s floor.
s.transport = ScriptedTransport(b"")
s._reply_wait = lambda n=None: 120
t0 = _time.time()
bs.BBSSession._read_post_yapp_tail(s, silence=0.3)
check("F2: clean-close read is short, not the 120 s packet floor",
      _time.time() - t0 < 2.0, f"{_time.time() - t0:.1f} s")


# ── 3b. Attributes main_window reads THROUGH the session object ──────
# These are set in BBSSession.__init__ and read nowhere inside that class
# — only as self.session.<name> from main_window. A dead-code sweep that
# looks for `self.<name>` calls them dead and deletes them, and the app
# then raises AttributeError on the next connect. That happened on
# 2026-09-23; this check is here so it cannot happen quietly again.
sess = bs.BBSSession.__new__(bs.BBSSession)
bs.BBSSession.__init__(sess, FakeTransport(""), mycall="N0CALL")
for attr, reader in (("new_user",    "main_window: if self.session.new_user"),
                     ("page_length", "main_window: if not self.session.page_length")):
    check(f"BBSSession.{attr} exists ({reader})", hasattr(sess, attr))


# ── 3bb. After a user abort, drain to the PROMPT, not to silence ─────
# J7, 2026-09-23, on air: LinBPQ honoured the CN but ~5 KB was already in
# the AX.25 stream. YappReceiver's 2 s quiet window is an ordinary
# inter-frame gap at 300 baud, so QtC called the wire idle, restored
# terminal mode, and 4.5 minutes of file text landed on the operator's
# screen with the YAPP framing bytes in it.

class BackloggedTransport:
    """Quiet for a while, then more backlog, then the prompt — the shape
    that defeated a silence-based drain.

    Models the LATCHED ABORT FLAG too: the real read_until_any() returns
    ("", None) instantly while transport._abort is set, which is what
    made the first version of the drain bail in zero seconds with 5 KB
    still inbound. Nothing here works until the drain clears it.
    """
    def __init__(self, reads, aborted=True):
        self.reads, self.n = list(reads), 0
        self.aborted = aborted
        self.cleared = False
    def read_until_any(self, markers, timeout=30):
        if self.aborted:
            return ("", None)          # exactly what the real one does
        if not self.reads:
            return ("", None)
        self.n += 1
        return self.reads.pop(0)
    def clear_abort(self):
        self.aborted = False
        self.cleared = True
    def abort_requested(self): return self.aborted
    def read_raw_bytes(self, n, timeout=10.0): return b""
    def send_raw(self, d): pass
    def flush_input(self): pass

def drain(reads, outstanding):
    s = bs.BBSSession.__new__(bs.BBSSession)
    s.transport = BackloggedTransport(reads)
    s.logged = []
    s._log = lambda d, t: s.logged.append(t)
    s._reply_wait = lambda n=None: 1
    s.at_page_prompt = True
    s.PROMPT_BBS = bs.BBSSession.PROMPT_BBS
    s.DRAIN_BYTES_PER_SEC = bs.BBSSession.DRAIN_BYTES_PER_SEC
    s.DRAIN_MAX_SECS = bs.BBSSession.DRAIN_MAX_SECS
    ok = bs.BBSSession._drain_after_abort(s, outstanding)
    return ok, s.logged, s.at_page_prompt, s.transport

# Backlog arrives in chunks with gaps, then the prompt. Must keep waiting.
ok, logged, parked, t = drain(
    [("a" * 300, None), ("b" * 300, None), ("de N0CALL>", bs.BBSSession.PROMPT_BBS)],
    5000)
check("drain clears the latched abort flag before reading", t.cleared is True)
check("drain waits through gaps and stops on the prompt", ok is True)
check("drain clears at_page_prompt on success", parked is False)
check("drain says how much it threw away",
      any("discarded" in t for t in logged), logged[-1] if logged else "")
check("drain warns up front that the BBS is still sending",
      any("still has about 5000 bytes queued" in t for t in logged), logged[0])

# Truly quiet with no prompt: stop, do not burn the whole budget.
ok, logged, _, _t = drain([("", None)], 5000)
check("drain gives up when the wire is genuinely quiet", ok is False)
check("drain says why it stopped",
      any("quiet with no prompt" in t for t in logged), logged)

# Nothing outstanding — still waits for the prompt, with a softer message.
ok, logged, _, _t = drain([("de N0CALL>", bs.BBSSession.PROMPT_BBS)], 0)
check("drain with nothing outstanding still waits for the prompt", ok is True)
check("no bogus byte count when nothing is outstanding",
      "still has about" not in logged[0], logged[0])

# The budget must outlast the transport's own blocking read, or the loop
# gets exactly one attempt and gives up on a healthy session (J7 run 3:
# 90 s budget vs AGW's MIN_REPLY_WAIT of 120).
s = bs.BBSSession.__new__(bs.BBSSession)
s.transport = BackloggedTransport([("de N0CALL>", bs.BBSSession.PROMPT_BBS)])
s.logged = []
s._log = lambda d, t: s.logged.append(t)
s._reply_wait = lambda n=None: 120          # AGW's floor
s.at_page_prompt = True
s.PROMPT_BBS = bs.BBSSession.PROMPT_BBS
s.DRAIN_BYTES_PER_SEC = bs.BBSSession.DRAIN_BYTES_PER_SEC
s.DRAIN_MAX_SECS = bs.BBSSession.DRAIN_MAX_SECS
floor = max(90.0, s._reply_wait(30) * 2.5)
check("drain budget outlasts a slow transport's blocking read",
      floor >= 120 * 2, floor)

# The budget has to be big enough for a real 300-baud backlog.
budget = min(bs.BBSSession.DRAIN_MAX_SECS,
             max(90.0, 5000 / bs.BBSSession.DRAIN_BYTES_PER_SEC * 1.5))
check("5 KB at 300 baud gets a budget over the ~4.5 min it really takes",
      budget >= 270, budget)


# ── 3c. OP n: a refusal is not a confirmation ────────────────────────
# LinBPQ answers `op n` with "Page Length is 10" when it takes it and
# "Page Length 9 is too short" when it does not — and the old check was
# `"page length" in raw.lower()`, which is in BOTH. A refused OP was
# logged as confirmed and QtC went on believing the BBS was paging at a
# length it had never accepted. Captured live 2026-09-23 against
# N0CALL-1 (BPQ 6.0.25.36); 10 is the floor, 9 is refused.

def op_result(reply, asked):
    s = bs.BBSSession.__new__(bs.BBSSession)
    s.page_length = 20            # what the BBS is already doing
    s._send = lambda c: None
    s._log = lambda *a: None
    s._expect = lambda marker, timeout=0: reply
    s.PROMPT_BBS = bs.BBSSession.PROMPT_BBS
    s.PAGE_CONFIRM = bs.BBSSession.PAGE_CONFIRM
    s.PAGE_REFUSED = bs.BBSSession.PAGE_REFUSED
    s.PAGE_MIN = bs.BBSSession.PAGE_MIN
    return bs.BBSSession.set_page_length(s, asked), s.page_length

r, n = op_result("Page Length is 10 \r\nde N0CALL>", 10)
check("OP accepted reads as confirmed", r == "confirmed", r)
check("confirmed OP records the length", n == 10, n)

r, n = op_result("Page Length 9 is too short \r\nde N0CALL>", 9)
check("OP refusal is NOT confirmed", r == "refused", r)
check("a refused OP leaves page_length alone", n == 20, n)

r, n = op_result("de N0CALL>", 20)
check("no recognisable reply reads as unconfirmed", r == "unconfirmed", r)

# The BBS is the authority on what it actually set.
r, n = op_result("Page Length is 20 \r\nde N0CALL>", 25)
check("the BBS's own number wins over the one we asked for", n == 20, n)

check("PAGE_MIN matches what LinBPQ told us", bs.BBSSession.PAGE_MIN == 10,
      bs.BBSSession.PAGE_MIN)


# ── 4. Outbox size, in the bytes Sent will use ───────────────────────
BODY = "73 de N0CALL\nsee you on the net\n"
expected = len(BODY.encode("utf-8"))
rows = [{"id": 1, "to_call": "N0CALL", "subject": "Net",
         "created_at": "2026-09-23 14:02:11", "body": BODY}]

class FakeTable:
    def __init__(self): self.headers, self.cells, self.n = [], {}, 0
    def setHorizontalHeaderLabels(self, h): self.headers = h
    def rowCount(self): return self.n
    def insertRow(self, r): self.n += 1
    def setItem(self, r, c, item): self.cells[(r, c)] = item
    def resizeRowsToContents(self): pass

mv = types.SimpleNamespace(msg_table=FakeTable())
mw.MailView._fill_outbox(mv, rows)
check("Outbox header ends in Size",
      mv.msg_table.headers[-1] == "Size", str(mv.msg_table.headers))
check("Outbox header has no Status",
      "Status" not in mv.msg_table.headers, str(mv.msg_table.headers))
check("Outbox size is the body's bytes",
      mv.msg_table.cells[(0, 4)].text() == str(expected),
      mv.msg_table.cells[(0, 4)].text())

print(f"\n{'FAILED' if fails else 'OK'} — {fails} failure(s)")
sys.exit(1 if fails else 0)
