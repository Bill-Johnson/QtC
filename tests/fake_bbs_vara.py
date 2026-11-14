#!/usr/bin/env python3
"""Fake LinBPQ mailbox behind a VARA-protocol modem (dev tool, not a harness).

Runs on the ANSWERING station. Connects to the local modem's VARA ports,
sets MYCALL + LISTEN ON, and when a caller connects talks like a small
LinBPQ BBS, close enough that QtC's Mail view runs its whole round trip:
login, OP, LM, auto-download of new personal mail, LC / L> bulletins,
SP/SB sends, K, B.

    python3 fake_bbs_vara.py N0BBS-9                  # modem on 8300/8301
    python3 fake_bbs_vara.py N0BBS-9 --port 8400      # modem on 8400/8401

Formats follow real LinBPQ 6.0.25 output (listing columns, the R header,
"Body:" and "R:" lines, [End of Message], "Page Length is n", the send
prompts and "Message: n Bid: ... Size: ..."). Callsigns in the seeded
mail are fake. Personal mail is addressed to whoever connects, so LM
finds it. State (read flags, sent and killed messages) lasts until the
script stops; restart it for a fresh mailbox.

Not modelled: OP paging (<A>bort, <CR> Continue..>), new-user
registration, forwarding, YAPP.
"""
import argparse, socket, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("mycall")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8300, help="modem command port")
ap.add_argument("--bw", default="BW500")
ap.add_argument("--demo", action="store_true",
                help="tidy mailbox for screenshots: 5 new personal + 2 bulletins")
ap.add_argument("--modem", default="",
                help="name the modem in the demo mail text (e.g. Data2G)")
ap.add_argument("--start", type=int, default=0,
                help="renumber the mailbox from this number, so a QtC that "
                     "already holds the default numbers from this BBS sees "
                     "new mail (it skips a BBS + number it already has)")
a = ap.parse_args()
CALL = a.mycall.upper()
BASE = CALL.split("-")[0]
VERSION = "6.0.25.16"
ME = "{ME}"            # replaced by the caller's base call at connect time
MODEM = a.modem or "an open-source HF modem"   # for the --demo text


def ts():
    return time.strftime("%H:%M:%S")


def ddmon(days_ago=0):
    return time.strftime("%d-%b", time.gmtime(time.time() - days_ago * 86400))


LONG_BODY = "\r".join(
    f"Line {i:02d} of a longer test message, sent to check how a slow "
    f"link carries a few KB." for i in range(1, 31))

# num: dict(t=type, s=status, to, at=@BBS or "", frm, subj, body, age days)
MSGS = {
    301: dict(t="B", s="N", to="SITREP", at="USA", frm="N0GHI",
              subj="US SITREP test 1", body="Fake national SITREP bulletin.\r"
              "Nothing to report from the test district.", age=3),
    302: dict(t="P", s="N", to="SYSOP", at="", frm="SYSTEM",
              subj="Housekeeping Results", body="Housekeeping ran. 0 killed.",
              age=2),
    303: dict(t="P", s="Y", to=ME, at=BASE, frm="N0ABC",
              subj="Old message already read", body="You read this one before.",
              age=2),
    304: dict(t="B", s="N", to="WX", at="ALLUS", frm="N0GHI",
              subj="Test weather bulletin", body="Clear and cold. Winds light.",
              age=1),
    305: dict(t="P", s="N", to=ME, at=BASE, frm="N0ABC",
              subj="Test message one", body="This is fake message 305.\r"
              "It has two lines.", age=0),
    306: dict(t="P", s="N", to=ME, at=BASE, frm="N0DEF",
              subj="Longer test message", body=LONG_BODY, age=0),
}

# --demo: a tidy mailbox for screenshots (Bill, 2026-11-14). Five new
# personal messages and two SITREP bulletins about Mercury and Data2G,
# plus the SYSOP note QtC should skip. Fake callsigns throughout.
DEMO_MSGS = {
    401: dict(t="P", s="N", to="SYSOP", at="", frm="SYSTEM",
              subj="Housekeeping Results", body="Housekeeping ran. 0 killed.",
              age=1),
    402: dict(t="B", s="N", to="SITREP", at="USA", frm="N0GHI",
              subj="HF digital SITREP - open modems on the air",
              body="SITREP: several open-source HF data modems are being\r"
                   "tested on the bands this month.\r\r"
                   f"Stations report successful BBS sessions with QtC over\r"
                   f"{MODEM} at BW500. Reports welcome on the usual net.\r\r"
                   "73, N0GHI", age=0),
    403: dict(t="P", s="N", to=ME, at=BASE, frm="N0ABC",
              subj=f"{a.modem or 'HF modem'} test via QtC",
              body=f"Hi,\r\rThis message came to you over {MODEM},\r"
                   "picked up automatically by QtC's Mail view.\r\r"
                   "Good copy here. 73,\rN0ABC", age=0),
    404: dict(t="P", s="N", to=ME, at=BASE, frm="N0DEF",
              subj="Weekly digital net reminder",
              body="Reminder: the weekly digital net meets Thursday at\r"
                   "0100Z. Check in by BBS message if you can't make it on\r"
                   "the air.\r\r73, N0DEF", age=0),
    405: dict(t="P", s="N", to=ME, at=BASE, frm="N0JKL",
              subj="Longer test - antenna notes",
              body="\r".join(f"Note {i:02d}: a few lines of antenna and "
                             f"station notes to fill out a longer message."
                             for i in range(1, 21)) + "\r\r73, N0JKL",
              age=0),
    406: dict(t="P", s="N", to=ME, at=BASE, frm="N0MNO",
              subj="Data2G BBS session - fast!",
              body="Ran a full BBS mail check over Data2G at BW500 this\r"
                   "afternoon. Three messages and a bulletin came down in\r"
                   "about two minutes, and the disconnect took seconds.\r\r"
                   "Remember to turn on Data2G's Rig switch or it won't key.\r\r"
                   "73, N0MNO", age=0),
    407: dict(t="P", s="N", to=ME, at=BASE, frm="N0PQR",
              subj="Mercury and Data2G side by side",
              body="Tried both modems on the same path today.\r\r"
                   "Data2G: big bursts, short keydowns, quick to connect.\r"
                   "Mercury: slower here, but it starts on a very robust\r"
                   "mode and climbs as the link improves.\r\r"
                   "Both carried QtC's whole Mail-view session without a\r"
                   "hitch. Same modem needed at both ends, of course.\r\r"
                   "73, N0PQR", age=0),
    408: dict(t="B", s="N", to="SITREP", at="USA", frm="N0GHI",
              subj="Data2G field report",
              body="Field report: Data2G BBS sessions on 40 m at BW500.\r\r"
                   "Personal mail and bulletins downloaded cleanly with QtC.\r"
                   "Typical transmissions were two seconds or less.\r\r"
                   "73, N0GHI", age=0),
}
if a.demo:
    MSGS = DEMO_MSGS
if a.start:
    MSGS = {a.start + i: m for i, (_, m) in enumerate(sorted(MSGS.items()))}
NEXT = [max(MSGS) + 1]


def stored_size(m):
    # BPQ's listed size is what it stores (header lines, CRLFs, R: line),
    # so it runs larger than the body. Close enough for a fake.
    return len(m["body"]) + 120


class Bbs:
    def __init__(self):
        self.cmd = socket.create_connection((a.host, a.port))
        self.data = socket.create_connection((a.host, a.port + 1))
        self.peer = None          # full call of the connected station
        self.me = None            # its base call (what LM matches)
        self.send_state = None    # None, or dict while taking a message

    # ── I/O ───────────────────────────────────────────────────────────
    def send_cmd(self, c):
        print(f"{ts()} CMD > {c}", flush=True)
        self.cmd.sendall((c + "\r").encode())

    def say(self, text):
        for ln in text.split("\r"):
            print(f"{ts()} TX  > {ln}", flush=True)
        self.data.sendall((text + "\r").encode())

    def prompt(self):
        self.say(f"de {BASE}>")

    # ── mailbox helpers ──────────────────────────────────────────────
    def to_of(self, m):
        return self.me if m["to"] == ME else m["to"]

    def line(self, n, m):
        to = self.to_of(m)
        at = f"@{m['at']}" if m["at"] else ""
        return (f"{n:<6} {ddmon(m['age'])} {m['t']}{m['s']} "
                f"{stored_size(m):>6} {to:<6} {at:<7} {m['frm']:<6} {m['subj']}")

    def listing(self, pick):
        rows = [self.line(n, m) for n, m in sorted(MSGS.items(), reverse=True)
                if pick(m)]
        return "\r".join(rows) if rows else "No messages found"

    def read(self, n):
        m = MSGS.get(n)
        if not m:
            return f"Message {n} not found"
        to = self.to_of(m)
        frm = f"{m['frm']}@{BASE}.FAKE.USA.NOAM"
        when = time.strftime("%y%m%d/%H%MZ", time.gmtime())
        txt = (f"From: {frm}\rTo: {to}\rType/Status: {m['t']}{m['s']}\r"
               f"Date/Time: {ddmon(m['age'])} {time.strftime('%H:%MZ', time.gmtime())}\r"
               f"Bid: {n}_{BASE}\rTitle: {m['subj']}\r\r"
               f"Body: {len(m['body'])}\r\r"
               f"R:{when} {n}@{BASE}.FAKE.USA.NOAM LinBPQ{VERSION[:6]}\r\r"
               f"{m['body']}\r\r"
               f"[End of Message #{n} from {frm}]")
        if m["t"] == "P" and to == self.me:
            m["s"] = "Y"
        # Real LinBPQ message text is CR+LF (a stored RF read shows 0D 0A);
        # with CR alone QtC's "Body: n" strip didn't match and the body
        # showed ^M. Listings and prompts stay CR, as BPQ sends them.
        return txt.replace("\r", "\r\n")

    # ── commands ─────────────────────────────────────────────────────
    def handle(self, raw):
        line = raw.strip()
        up = line.upper()

        if self.send_state is not None:          # taking a message
            st = self.send_state
            if st["title"] is None:
                st["title"] = line or "(no title)"
                self.say("Enter Message Text (end with /ex or ctrl/z)")
                return
            if up == "/EX" or "\x1a" in raw:
                n = NEXT[0]; NEXT[0] += 1
                body = "\r".join(st["lines"])
                MSGS[n] = dict(t=st["t"], s="N", to=st["to"], at=st["at"],
                               frm=self.me, subj=st["title"], body=body, age=0)
                self.send_state = None
                self.say(f"Message: {n} Bid: {n}_{BASE} Size: {len(body)}")
                self.prompt()
            else:
                st["lines"].append(line)
            return

        word = up.split(" ")[0] if up else ""
        if word in ("B", "BYE"):
            # LinBPQ hangs up on B without a farewell line.
            self.send_cmd("DISCONNECT")
            return
        if word == "OP":
            arg = up[2:].strip()
            if arg.isdigit():
                n = int(arg)
                if 0 < n < 10:
                    self.say(f"Page Length {n} is too short")
                else:
                    self.say(f"Page Length is {n}")
            else:
                self.say("Invalid Command")
        elif up in ("L", "LN"):
            self.say(self.listing(lambda m: up == "L" or m["s"] == "N"))
        elif up == "LM":
            self.say(self.listing(lambda m: self.to_of(m) == self.me))
        elif up.startswith("L>"):
            cat = up[2:].strip()
            self.say(self.listing(lambda m: m["t"] == "B" and m["to"] == cat))
        elif up == "LC":
            cats = {}
            for m in MSGS.values():
                if m["t"] == "B":
                    cats[m["to"]] = cats.get(m["to"], 0) + 1
            self.say("  ".join(f"{c:<6} {k}" for c, k in sorted(cats.items()))
                     or "No bulletins")
        elif word == "R" and up[1:].strip().isdigit():
            self.say(self.read(int(up[1:].strip())))
        elif word == "K" and up[1:].strip().isdigit():
            n = int(up[1:].strip())
            if MSGS.pop(n, None):
                self.say(f"Message #{n} Killed")
            else:
                self.say(f"Message {n} not found")
        elif word in ("S", "SP", "SB") and len(up.split()) >= 2:
            dest = line.split()[1].upper()
            to, _, at = dest.partition("@")
            self.send_state = dict(t="B" if word == "SB" else "P",
                                   to=to, at=at, title=None, lines=[])
            self.say("Enter Title (only):")
            return
        elif up in ("", "A"):
            pass                                  # bare CR / stray A: just prompt
        else:
            self.say("Invalid Command")
        self.prompt()

    # ── loops ─────────────────────────────────────────────────────────
    def cmd_loop(self):
        buf = b""
        while True:
            b = self.cmd.recv(1024)
            if not b:
                print(f"{ts()} modem closed the command port", flush=True)
                return
            buf += b
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                line = line.decode(errors="replace").strip()
                if not line or line.startswith(("BUFFER", "IAMALIVE")):
                    continue
                print(f"{ts()} CMD < {line}", flush=True)
                up = line.upper()
                if up.startswith("CONNECTED"):
                    parts = line.split()
                    self.peer = parts[1] if len(parts) > 1 else "N0CALL"
                    self.me = self.peer.split("-")[0].upper()
                    self.send_state = None
                    time.sleep(0.5)
                    newest = max(MSGS) if MSGS else 0
                    self.say(f"[BPQ-{VERSION}-IHJM$]\r"
                             f"Hello. Latest Message is {newest}, "
                             f"Last listed is 0")
                    self.prompt()
                elif up.startswith("DISCONNECTED"):
                    if self.peer:
                        print(f"{ts()} --- session with {self.peer} ended ---",
                              flush=True)
                    self.peer = None

    def data_loop(self):
        buf = b""
        while True:
            b = self.data.recv(4096)
            if not b:
                return
            buf += b.replace(b"\r\n", b"\r").replace(b"\n", b"\r")
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                txt = line.decode(errors="replace")
                print(f"{ts()} RX  < {txt}", flush=True)
                if self.peer:
                    self.handle(txt)

    def run(self):
        threading.Thread(target=self.data_loop, daemon=True).start()
        self.send_cmd(a.bw)
        time.sleep(0.3)
        self.send_cmd(f"MYCALL {CALL}")
        time.sleep(0.3)
        self.send_cmd("LISTEN ON")
        print(f"{ts()} fake BBS {CALL} listening via modem port {a.port}",
              flush=True)
        self.cmd_loop()


if __name__ == "__main__":
    try:
        Bbs().run()
    except KeyboardInterrupt:
        pass
