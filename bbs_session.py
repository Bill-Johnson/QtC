# QtC v0.9.11-beta — bbs_session.py  (built 2026-03-25)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
bbs_session.py — BBS session handler
Tuned for KC9MTP-7 LinBPQ node output format.
"""

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class BBSMessage:
    msg_number: int
    msg_type:   str      # P=private, B=bulletin, T=traffic
    status:     str      # N=new, Y=read
    to_call:    str
    at_bbs:     str      # home BBS e.g. KC9MTP-1 (may be blank)
    from_call:  str
    date:       str
    size:       int
    subject:    str
    body:       str = ""
    downloaded: bool = False

    @property
    def is_personal(self):
        return self.msg_type.upper() == "P"

    @property
    def is_new(self):
        return self.status.upper() == "N"


@dataclass
class BBSMailSummary:
    total_messages: int = 0
    new_personal:   List[BBSMessage] = field(default_factory=list)
    new_bulletins:  List[BBSMessage] = field(default_factory=list)
    all_messages:   List[BBSMessage] = field(default_factory=list)

    @property
    def has_new_mail(self):
        return len(self.new_personal) > 0

    @property
    def estimated_download_size(self):
        return sum(m.size for m in self.new_personal)


# ─────────────────────────────────────────────
# Message list parser
# ─────────────────────────────────────────────
#
# Actual output from KC9MTP-7:
#
# 233    10-Mar PN      78 KC9MTP @KC9MTP-1 KJ5MIW test 2
# 232    10-Mar PN      62 SYSOP          SYSTEM New User KJ5MIW
# 227    07-Mar PY     172 SYSOP          SYSTEM Housekeeping Results
#
# Columns:
#   msg#   date    type+status   size   TO [@HOMEBBS]   FROM   subject
#
# Notes:
#   - Type and status are concatenated with no space: PN, PY, BN, BY
#   - @HOMEBBS is optional — only present when TO has a registered home BBS
#   - SYSOP messages have no @BBS and FROM is SYSTEM
#   - Size is right-aligned in a ~6 char field

MSG_LINE_RE = re.compile(
    r"^\s*"
    r"(\d+)"                        # group 1: message number
    r"\s+"
    r"(\d{1,2}-\w{3})"              # group 2: date e.g. 10-Mar
    r"\s+"
    r"([PBT\$])"                    # group 3: type P/B/T/$
    r"([NYH\$\s])"                  # group 4: status N/Y/H/$ 
    r"\s+"
    r"(\d+)"                        # group 5: size in bytes
    r"\s+"
    r"([\w\-]+)"                    # group 6: TO callsign
    r"(?:\s+@([\w\-]+))?"           # group 7: optional @HOMEBBS
    r"\s+"
    r"([\w\-]+)"                    # group 8: FROM callsign
    r"\s+"
    r"(.+)$",                       # group 9: subject
    re.IGNORECASE
)


def parse_message_list(raw_text: str) -> List[BBSMessage]:
    """Parse LM output into BBSMessage objects."""
    messages = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = MSG_LINE_RE.match(line)
        if m:
            messages.append(BBSMessage(
                msg_number = int(m.group(1)),
                date       = m.group(2).strip(),
                msg_type   = m.group(3).strip().upper(),
                status     = m.group(4).strip().upper() or "N",
                size       = int(m.group(5)),
                to_call    = m.group(6).strip().upper(),
                at_bbs     = m.group(7).strip().upper() if m.group(7) else "",
                from_call  = m.group(8).strip().upper(),
                subject    = m.group(9).strip(),
            ))
    return messages


# ─────────────────────────────────────────────
# BBS Session
# ─────────────────────────────────────────────

class BBSSession:
    """
    Manages a full BBS session for LinBPQ/BPQ32.
    Tuned for the KC9MTP-7 node prompt and login sequence.

    Login sequence observed:
        user: <callsign>
        password: <blank>
        Welcome to KC9MTP's Telnet Server
        Enter ? for list of commands
        VALPO:KC9MTP-7} BBS CONNECT BYE INFO ...
        bbs
        VALPO:KC9MTP-7} Connected to BBS
        [BPQ-6.0.24.52-IHJM$]
        Hello Bill. Latest Message is 235, Last listed is 229
        de KC9MTP>
    """

    # Prompts — matched case-insensitively
    PROMPT_LOGIN    = "user:"
    PROMPT_PASSWORD = "password:"
    PROMPT_NODE     = "}"          # node prompt ends with }
    PROMPT_BBS      = ">"          # universal — works for all node types
    CMD_TIMEOUT     = 20

    def __init__(self, transport, mycall: str, password: str = "",
                 telnet_user: str = "", user_info: dict = None):
        self.transport   = transport
        self.mycall      = mycall.upper()
        self.telnet_user = telnet_user if telnet_user else mycall
        self.password    = password
        self.user_info         = user_info or {}  # name, qth, zip, home_bbs
        self.new_user          = False            # set True if BBS prompts registration
        self._rf_connected_cb  = None             # called when RF link is up
        self.session_log: List[str] = []

    # ── logging ───────────────────────────────────────────────────

    def _log(self, direction: str, text: str):
        entry = f"[{direction}] {text.strip()}"
        self.session_log.append(entry)
        print(entry)

    def _send(self, text: str):
        # Redact password from log — never show it in output
        display = "********" if text == self.password and self.password else \
                  (text if text else "<blank>")
        self._log("TX", display)
        self.transport.send(text)

    def _expect(self, prompt: str, timeout: int = None) -> str:
        t = timeout or self.CMD_TIMEOUT
        data = self.transport.read_until(prompt, timeout=t)
        self._log("RX", data)
        return data

    def _expect_silent(self, prompt: str, timeout: int = None) -> str:
        """Like _expect but does not log — caller handles logging."""
        t = timeout or self.CMD_TIMEOUT
        return self.transport.read_until(prompt, timeout=t)

    def _command(self, cmd: str, settle: float = 1.0) -> str:
        """Send a command and collect the full response."""
        self._send(cmd)
        time.sleep(settle)
        response = self.transport.read_all_pending(settle_time=settle)
        self._log("RX", response)
        return response

    # ── public API ────────────────────────────────────────────────

    def connect_and_login(self) -> bool:
        """
        Connect and log in to the BBS.

        Dispatches to the correct login sequence based on transport type:
          - TelnetTransport  → full LinBPQ telnet login (user/password/bbs)
          - VaraTransport    → VARA RF login (connect already done by
                               transport.connect(); just wait for BBS prompt)
        """
        from transport import VaraTransport
        if isinstance(self.transport, VaraTransport):
            return self._vara_login()
        return self._telnet_login()

    def _telnet_login(self) -> bool:
        """Full LinBPQ telnet login sequence."""
        self.transport.connect()
        self._log("SYS", f"TCP connected to "
                         f"{self.transport.host}:{self.transport.port}")

        # Step 1: username
        banner = self._expect(self.PROMPT_LOGIN, timeout=10)
        if self.PROMPT_LOGIN.lower() not in banner.lower():
            self._log("SYS", "WARNING: Did not see 'user:' prompt")
        self._send(self.telnet_user)

        # Step 2: password
        pwd_prompt = self._expect(self.PROMPT_PASSWORD, timeout=10)
        if self.PROMPT_PASSWORD.lower() not in pwd_prompt.lower():
            self._log("SYS", "WARNING: Did not see 'password:' prompt")
        self._send(self.password)

        # Step 3: read the welcome banner
        welcome = self._expect("commands", timeout=15)
        self._log("SYS", "Got welcome banner — sending bbs command...")

        # Step 4: send bbs directly
        self._send("bbs")

        # Step 5: wait for BBS ready prompt
        # Some nodes send status lines containing '>' before the actual BBS
        # prompt arrives (e.g. circuit status lines like "Circuit<-->").
        # We keep reading until we see a line ending in '>' that looks like
        # a real BBS prompt, with a generous timeout for multi-hop nodes.
        bbs_response = self._expect(self.PROMPT_BBS, timeout=15)

        # Drain any additional lines that arrive after the first '>'
        # (node status, BBS banner, etc.) — keep reading until quiet
        import time as _time
        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            extra = self.transport.read_all_pending(settle_time=0.5)
            if extra.strip():
                bbs_response += extra
                deadline = _time.time() + 2.0  # reset timer on new data
            else:
                break

        self._log("SYS", f"BBS response: {bbs_response!r}")

        if ">" not in bbs_response:
            self._log("SYS", f"WARNING: No BBS prompt received: {bbs_response!r}")
            return False

        # Detect exact BBS prompt style — same as VARA login
        m = re.search(r"(de\s+\S+>)", bbs_response, re.IGNORECASE)
        if m:
            self.PROMPT_BBS = m.group(1)
        elif "\r>" in bbs_response or "\n>" in bbs_response:
            self.PROMPT_BBS = "\r>"
        self._log("SYS", f"BBS prompt detected as: {self.PROMPT_BBS!r}")
        self._log("SYS", "BBS login successful")

        # Handle new user registration if needed
        if self._handle_registration(bbs_response):
            bbs_response = self._expect(self.PROMPT_BBS, timeout=30)
            self._log("SYS", f"Post-registration BBS response: {bbs_response!r}")

        return True

    def _vara_login(self) -> bool:
        """
        VARA RF login sequence.

        Calls transport.connect() which opens both TCP sockets, sends
        MYCALL + CONNECT to the VARA command port, and waits for the
        CONNECTED response (RF link established).

        Once connected, LinBPQ sends the BBS banner automatically —
        no username or password prompt, no 'bbs' command needed.
        We just wait for the '>' prompt on the data port.
        """
        self.transport.connect()
        self._log("SYS", "VARA RF link established — waiting for BBS prompt…")
        if self._rf_connected_cb:
            self._rf_connected_cb()

        # The BBS sends its banner automatically; wait for the generic '>' first
        bbs_response = self._expect(self.PROMPT_BBS, timeout=30)
        self._log("SYS", f"BBS response: {bbs_response!r}")

        if ">" not in bbs_response:
            self._log("SYS",
                f"WARNING: No BBS prompt received: {bbs_response!r}")
            return False

        # Check for new user registration prompts in the banner.
        # LinBPQ asks for Name (and optionally QTH, Zip, Home BBS) before
        # showing the normal "de CALLSIGN>" prompt. We auto-fill from
        # user_info settings to save RF time.
        if self._handle_registration(bbs_response):
            bbs_response = self._expect(self.PROMPT_BBS, timeout=30)
            self._log("SYS", f"Post-registration BBS response: {bbs_response!r}")

        # Detect exact BBS prompt style
        m = re.search(r"(de\s+\S+>)", bbs_response, re.IGNORECASE)
        if m:
            self.PROMPT_BBS = m.group(1)   # e.g. "de KC9MTP>"
        elif "\r>" in bbs_response or "\n>" in bbs_response:
            self.PROMPT_BBS = "\r>"
        self._log("SYS", f"BBS prompt detected as: {self.PROMPT_BBS!r}")
        self._log("SYS", "BBS login successful via VARA")
        return True

    # Registration prompt keywords — checked case-insensitively
    REGISTRATION_PROMPTS = [
        ("name",     ["enter your name", "your name"]),
        ("qth",      ["enter your qth",  "your qth", "enter qth"]),
        ("zip",      ["enter your zip",  "zip code", "postcode", "enter zip"]),
        ("home_bbs", ["enter your home", "home bbs", "homebbs",
                      "enter home", "enter your home bbs"]),
    ]

    def _registration_field(self, text: str):
        """Return the field name if text contains a registration prompt, else None."""
        t = text.lower()
        for field, keywords in self.REGISTRATION_PROMPTS:
            if any(kw in t for kw in keywords):
                return field
        return None

    def _handle_registration(self, banner: str) -> bool:
        """
        Detect and handle LinBPQ new user registration prompts.

        LinBPQ asks for Name, QTH, Zip, and/or Home BBS sequentially on
        first connect with an unknown callsign. Uses a while-loop to keep
        responding to prompts until the normal BBS prompt is received.
        Auto-fills values from user_info (My Station settings).

        Returns True if any registration prompts were handled.
        """
        handled = False
        current = banner

        while True:
            field = self._registration_field(current)
            if not field:
                break  # no more registration prompts — we're done

            value = self.user_info.get(field, "").strip()
            if value:
                self._log("SYS",
                    f"New user registration: auto-sending {field} = {value!r}")
            else:
                self._log("SYS",
                    f"New user registration: {field} blank in My Station "
                    f"— sending empty response")

            self._send(value)
            self.new_user = True
            handled = True

            # Wait for next prompt or final BBS prompt
            current = self._expect(self.PROMPT_BBS, timeout=15)

        return handled

    def check_mail(self, new_only: bool = True) -> BBSMailSummary:
        """
        List messages addressed to this callsign and return a BBSMailSummary.

        Always sends 'LM' — returns ONLY messages for the logged-in callsign.
        Never sends 'L N' — that returns everything new on the BBS including
        bulletins and other users' mail, which wastes RF time.

        new_only=True  — after LM, download only PN (new/unread personal)
        new_only=False — after LM, download all personal (PN + PY)
        """
        summary = BBSMailSummary()

        # Always LM — never L N
        self._send("lm")
        raw = self._expect(self.PROMPT_BBS, timeout=120)
        messages = parse_message_list(raw)

        summary.all_messages   = messages
        summary.total_messages = len(messages)

        if new_only:
            # New only — download PN (new/unread personal) only
            summary.new_personal  = [
                m for m in messages if m.is_personal and m.is_new
            ]
        else:
            # All — download all personal including already-read PY
            summary.new_personal  = [
                m for m in messages if m.is_personal
            ]

        # Note bulletins but never auto-download them
        summary.new_bulletins = [
            m for m in messages if not m.is_personal and m.is_new
        ]

        self._log("SYS",
            f"Mail check ({'new PN only' if new_only else 'all personal PN+PY'}): "
            f"{summary.total_messages} total, "
            f"{len(summary.new_personal)} personal to download, "
            f"{len(summary.new_bulletins)} new bulletins (not downloaded)")
        return summary

    def download_message(self, msg_number: int,
                          size_hint: int = 0) -> str:
        """
        Read and return a single message body.

        size_hint  — known size in bytes from LM listing.  Used to
                     calculate a sensible timeout so large messages
                     don't get cut off at slow VARA speeds.

        Timeout logic:
          At 88 bps VARA HF, each RF frame carries ~43 bytes and takes
          ~5 seconds including PTT turnaround.  We calculate how many
          frames the message needs and allow 6s per frame, with a floor
          of 120s and a ceiling of 600s (10 min — enough for ~4 KB
          at 88 bps).
        """
        BYTES_PER_FRAME = 43        # measured at 88 bps BW500
        SECS_PER_FRAME  = 6.0       # generous — includes PTT turnaround
        TIMEOUT_FLOOR   = 120       # minimum regardless of size
        TIMEOUT_CEIL    = 600       # 10 minutes absolute max

        if size_hint > 0:
            frames  = max(1, -(-size_hint // BYTES_PER_FRAME))  # ceiling div
            timeout = int(frames * SECS_PER_FRAME)
            timeout = max(TIMEOUT_FLOOR, min(timeout, TIMEOUT_CEIL))
        else:
            timeout = TIMEOUT_FLOOR

        self._log("SYS",
            f"Downloading msg #{msg_number} "
            f"({size_hint} bytes expected, timeout={timeout}s)")

        self._send(f"r {msg_number}")

        # We read in two stages to handle message bodies that contain '>'
        # characters (forwarding headers, quoted text, etc.) which would
        # trigger a false match on PROMPT_BBS before the message is complete.
        # Stage 1: wait for [End of Message — guaranteed end of body
        # Stage 2: wait for BBS prompt — confirms BBS is ready for next command
        END_MARKER = "[End of Message"
        raw = self._expect(END_MARKER, timeout=timeout)
        # Now drain to the BBS prompt to clear it from the buffer
        tail = self._expect(self.PROMPT_BBS, timeout=30)
        raw = raw + tail

        # Body is everything before [End of Message
        idx = raw.find(END_MARKER)
        raw_body = raw[:idx].strip() if idx >= 0 else raw.strip()

        # Strip any stale data before the BBS header, then strip the header
        # itself. The LinBPQ header is a single run-on line:
        #   From: X To: X Type/Status: X Date/Time: X X Bid: X Title: X
        # Everything after the Title value is the actual message body.
        # We use a regex to consume the entire header block in one shot.
        header_re = re.compile(
            r'.*?'                        # any stale data before header
            r'From:\s*\S+'               # From: CALLSIGN
            r'\s+To:\s*\S+'              # To: CALLSIGN
            r'\s+Type/Status:\s*\S+'     # Type/Status: PN
            r'\s+Date/Time:\s*\S+'       # Date/Time: 14-Mar
            r'\s+\S+'                    # 02:11Z (time)
            r'\s+Bid:\s*\S+'             # Bid: 259_KC9MTP
            r'\s+Title:\s*[^\n\r]+',      # Title: <entire title to end of line>
            re.IGNORECASE | re.DOTALL
        )
        m = header_re.match(raw_body)
        if m:
            body = raw_body[m.end():].strip()
        else:
            # No header found — return raw (shouldn't happen normally)
            self._log("SYS", "Warning: no BBS header found in message body")
            body = raw_body

        self._log("SYS",
            f"Downloaded msg #{msg_number} "
            f"({len(body)} chars received, {size_hint} expected)")
        return body

    def download_messages(self, messages: List[BBSMessage]) \
            -> List[BBSMessage]:
        """Download a list of messages. Returns list with .body filled."""
        total = len(messages)
        # Pause data monitor for entire download sequence —
        # prevents frame-split data from being double-displayed
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(False)
        # Single flush before we start — clears any stale bytes
        self.transport.flush_input()
        try:
            for i, msg in enumerate(messages, 1):
                self._log("SYS",
                    f"Downloading message {i} of {total} "
                    f"(#{msg.msg_number}, ~{msg.size} bytes)")
                msg.body = self.download_message(msg.msg_number,
                                                 size_hint=msg.size)
                msg.downloaded = True
                # Brief pause between messages — let BBS settle
                time.sleep(0.3)
        finally:
            # Always re-enable terminal mode
            if hasattr(self.transport, "set_terminal_mode"):
                self.transport.set_terminal_mode(True)
        return messages

    def send_message(self, to_call: str, subject: str, body: str,
                     msg_type: str = "P",
                     at_bbs: str = "") -> bool:
        """
        Send a message via SP (personal) or SB (bulletin).
        Returns True if the BBS confirmed with a message number.

        LinBPQ send sequence:
          1. sp <call>  or  sb <topic>
          2. BBS responds: "Subject:"  (or "Title:")
          3. We send the subject
          4. BBS responds: "Enter message..." or just a blank prompt
          5. We send the body lines then /EX on its own line
          6. BBS confirms: "Message NNN entered"  then returns to ">"
        """
        to_call = to_call.upper()

        if msg_type.upper() == "P":
            cmd = f"sp {to_call}"
            if at_bbs:
                cmd += f" @ {at_bbs.upper()}"
        else:
            cmd = f"sb {to_call}"

        # 1. Send the SP/SB command.
        # The data monitor will display the BBS prompts as they arrive.
        # We use _expect to consume them before sending our responses,
        # but we do NOT re-log them (monitor already showed them).
        self._send(cmd)

        # Wait for Subject/Title prompt — monitor displays it, we just consume
        subj_response = self._expect(":", timeout=30)

        if not any(p in subj_response.lower() for p in ["subject", "title", ":"]):
            self._log("SYS", f"Send FAILED: no Subject prompt — got {subj_response!r}")
            return False

        # 2. Send subject — blank subject cancels the message on LinBPQ,
        # so substitute .... (traditional BBS convention) if empty.
        if not subject or not subject.strip():
            subject = "...."
        self._send(subject)
        self._expect("Enter Message", timeout=30)

        # 3. Send body lines then /EX — log TX lines for terminal display
        for line in (body.splitlines() or [""]):
            self._log("TX", line)
            self.transport.send(line)
            time.sleep(0.1)
        self._log("TX", "/EX")
        self.transport.send("/EX")

        # 4. Pause monitor now — we need clean _expect for the confirmation
        # At this point all prompts have been shown, so no display loss.
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(False)

        # Use silent expect — we log the confirmation ourselves below
        confirmation = self._expect_silent(self.PROMPT_BBS, timeout=60)

        # Log confirmation while monitor is still paused
        import re as _re
        msg_match = _re.search(r"(Message:.*)", confirmation, _re.I | _re.DOTALL)
        if msg_match:
            self._log("RX", msg_match.group(1).strip())
        else:
            self._log("RX", confirmation.strip())

        # Re-enable monitor — duplicate frame suppression handled in transport
        # via the recent-lines dedup window in _data_monitor
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(True)

        # Success if BBS assigned a message number
        confirmed = bool(_re.search(r"message[\s:]+\d+", confirmation, _re.I))
        if not confirmed:
            # Fallback: any of these words also indicate success
            confirmed = any(w in confirmation.lower()
                            for w in ["entered", "saved", "msg#", "ok"])
        self._log("SYS",
            f"Send {'OK' if confirmed else 'FAILED'}: "
            f"{confirmation.strip()[:80]!r}")
        return confirmed

    def list_categories(self) -> list:
        """
        Send LC to get available bulletin categories on this BBS.
        Returns list of category name strings e.g. ['ALL', 'EWN', 'BDN']
        """
        self._send("lc")
        raw = self._expect(self.PROMPT_BBS, timeout=30)
        self._log("SYS", f"LC response: {raw.strip()!r}")
        # LinBPQ LC output: "ALL    3  BDN    2  EWN    2"
        # Parse: words that are all uppercase letters are category names
        import re
        cats = re.findall(r'\b([A-Z][A-Z0-9]+)\b', raw)
        # Filter out common non-category words
        skip = {"BBS", "DE", "OK", "TNX", "NIL", "NO", "YES"}
        return [c for c in cats if c not in skip]

    def check_bulletins(self, subscriptions: list) -> dict:
        """
        For each subscribed category, run L> CATEGORY and return
        a dict of {category: [BBSMessage, ...]} for new (BN) bulletins.
        subscriptions = list of category strings e.g. ['SITREP', 'EWN']
        """
        results = {}
        for cat in subscriptions:
            cat = cat.upper().strip()
            if not cat:
                continue
            self._send(f"l> {cat}")
            raw = self._expect(self.PROMPT_BBS, timeout=30)
            messages = parse_message_list(raw)
            # Only new bulletins (BN) — filter out any PN that appear in L> results
            new_bulls = [m for m in messages
                         if m.is_new and not m.is_personal]
            if new_bulls:
                results[cat] = new_bulls
                self._log("SYS",
                    f"Bulletins: {len(new_bulls)} new in {cat}")
            else:
                self._log("SYS", f"Bulletins: none new in {cat}")
        return results

    def logout(self, skip_bye: bool = False):
        """Send BYE and disconnect cleanly.
        skip_bye=True — omit sending 'b', used when caller already sent it
        (e.g. terminal B command) to avoid double-send echo.
        """
        try:
            if not skip_bye:
                self._send("b")
            time.sleep(0.5)
        except Exception:
            pass
        self.transport.disconnect()
        self._log("SYS", "Disconnected")
