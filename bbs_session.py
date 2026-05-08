# QtC v0.12.0-beta — bbs_session.py  (built 2026-05-07)
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
import os
from dataclasses import dataclass, field
from typing import List, Optional


# ─────────────────────────────────────────────
# YAPP file transfer protocol
# ─────────────────────────────────────────────
#
# BBS commands (LinBPQ):
#   files            — list available files
#   yapp <filename>  — download a file via YAPP
#   read <filename>  — display file as plain text (no protocol)
#
# Note: LinBPQ YAPP does not support filenames that contain spaces.
#
# YAPP wire format:
#   Header : SOH + length(1) + filename\x00 + filesize_decimal\x00
#   Data   : STX + length(1) + data bytes   (length 0 = 256 bytes)
#   End    : EOT
#   ACK/NAK: single byte from receiver after each frame
#
# Telnet caveat: data bytes of 0xFF are mangled by Telnet IAC stripping.
#   VaraHF/FM transports are unaffected.  In practice BBS files are text
#   and rarely contain 0xFF, but it is a known limitation.

_YAPP_SOH = 0x01   # Start of header
_YAPP_STX = 0x02   # Start of data block
_YAPP_ETX = 0x03   # End of text — LinBPQ uses this as end-of-transfer (same as EOT)
_YAPP_EOT = 0x04   # End of transmission (standard YAPP)
_YAPP_ENQ = 0x05   # Enquiry — BBS sends this to signal "I am ready to send"
_YAPP_ACK = 0x06   # Acknowledge
_YAPP_NAK = 0x15   # Negative acknowledge
_YAPP_CAN = 0x18   # Cancel


class YappReceiver:
    """
    Receives a file via YAPP after the BBS has been sent 'yapp <filename>'.

    Full YAPP handshake (per WA7MBL spec and yapp.c by Jonathan Naylor G4KLX):

      BBS  → Client  [ENQ][subcode]          "I am ready to send"
      Client → BBS   [ACK][0x01]  (RR)       "Receiver Ready"
      BBS  → Client  [SOH][len][name\\0][size\\0]  YAPP header
      Client → BBS   [ACK][0x02]  (RF)       "Ready for File data"
      BBS  → Client  [STX][len][data]        data block(s), repeat
      Client → BBS   [ACK][0x01]  (RR)       ack each data block
      BBS  → Client  [EOT]                   end of file
      Client → BBS   [ACK][0x01]  (RR)       final ack

    All ACKs are 2 bytes.  Single-byte 0x06 is NOT sufficient — LinBPQ
    waits for the 2-byte RR/RF response before proceeding.
    """

    TIMEOUT = 60   # seconds to wait for any single frame

    def __init__(self, transport, progress_cb=None, log_cb=None):
        self.transport   = transport
        self.progress_cb = progress_cb   # callable(bytes_done: int, total: int)
        self.log_cb      = log_cb        # callable(msg: str)

    def _log(self, msg: str):
        if self.log_cb:
            self.log_cb(msg)

    # ── Receiver-side packets per WA7MBL YAPP RFC v1.1 (1986) ──────────
    # See ../memory/reference_yapp_rfc.md for the full state tables.

    def _send_rr(self):
        """RR (Rcv_Rdy): [ACK][0x01] — generic positive ack."""
        self.transport.send_raw(bytes([_YAPP_ACK, 0x01]))

    def _send_rf(self):
        """RF (Rcv_File): [ACK][0x02] — accept the file offered by HD."""
        self.transport.send_raw(bytes([_YAPP_ACK, 0x02]))

    def _send_af(self):
        """AF (Ack_EOF): [ACK][0x03] — required ack for EF (Send_EOF)."""
        self.transport.send_raw(bytes([_YAPP_ACK, 0x03]))

    def _send_at(self):
        """AT (Ack_EOT): [ACK][0x04] — required ack for ET (Send_EOT)."""
        self.transport.send_raw(bytes([_YAPP_ACK, 0x04]))

    def _send_nr(self, reason: bytes = b""):
        """NR (Not_Rdy): [NAK][len][optional reason ASCII] per RFC v1.1."""
        self.transport.send_raw(bytes([_YAPP_NAK, len(reason)]) + reason)

    # Legacy alias — kept so the in-block error path at the data-block
    # short-read site keeps working unchanged. Sends a zero-reason NR.
    def _send_nak(self):
        self._send_nr()

    def _hex(self, data: bytes, label: str = ""):
        """Log up to 32 bytes as hex for debugging."""
        if data:
            h = " ".join(f"{b:02x}" for b in data[:32])
            suffix = "…" if len(data) > 32 else ""
            self._log(f"YAPP hex {label}: [{h}{suffix}]  ({len(data)} bytes)")
        else:
            self._log(f"YAPP hex {label}: [empty]")

    def receive(self) -> tuple:
        """
        Execute the full YAPP receive handshake.
        Returns (filename: str, data: bytes).
        Raises IOError on protocol error or timeout.
        """
        # ── Step 1: Wait for ENQ from BBS ────────────────────────────
        # LinBPQ sends [ENQ=05][subcode=01] to signal it is ready to send.
        # The subcode 0x01 coincidentally equals SOH but is NOT the header
        # frame — it is LinBPQ's YAPP version/type byte.
        # Any text before ENQ (BBS echo, status lines) is skipped.
        self._log("YAPP: waiting for ENQ signal from BBS...")
        deadline = time.time() + self.TIMEOUT
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise IOError(
                    "YAPP: timed out waiting for ENQ — "
                    "BBS may not support YAPP or filename was not found")
            b = self.transport.read_raw_bytes(1, timeout=min(2.0, remaining))
            if not b:
                continue
            if b[0] == _YAPP_ENQ:
                sub_b = self.transport.read_raw_bytes(1, timeout=5.0)
                subcode = sub_b[0] if sub_b else 0
                self._log(f"YAPP: ENQ received (subcode=0x{subcode:02x})")
                self._hex(bytes([_YAPP_ENQ, subcode]), "ENQ+subcode")
                break

        # ── Step 2: Send RR — Receiver Ready ─────────────────────────
        self._send_rr()
        self._log("YAPP: sent RR — scanning for SOH header frame...")

        # ── Step 3: Scan for SOH, then read header ────────────────────
        # After RR the BBS sends [SOH=01][len][filename\0][filesize\0].
        # We always scan for a fresh SOH regardless of the ENQ subcode.
        deadline = time.time() + self.TIMEOUT
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise IOError("YAPP: timed out waiting for SOH header after RR")
            b = self.transport.read_raw_bytes(1, timeout=min(2.0, remaining))
            if not b:
                continue
            if b[0] == _YAPP_SOH:
                self._log("YAPP: SOH found — reading header")
                break

        len_b = self.transport.read_raw_bytes(1, timeout=10)
        self._hex(len_b, "header-len-byte")
        if not len_b:
            raise IOError("YAPP: timeout reading header length byte")
        hlen = len_b[0] or 256

        hdata = self.transport.read_raw_bytes(hlen, timeout=15)
        parts = hdata.split(b'\x00')
        filename = parts[0].decode('ascii', errors='replace').strip()
        try:
            filesize = int(parts[1].decode('ascii').strip()) \
                       if len(parts) > 1 and parts[1] else 0
        except ValueError:
            filesize = 0
        self._log(f"YAPP: header — file='{filename}'  size={filesize} bytes")

        # ── Step 4: Send RF — Ready for File data ─────────────────────
        self._send_rf()
        self._log("YAPP: sent RF — receiving data blocks...")

        # ── Step 5: Receive data blocks ───────────────────────────────
        received = bytearray()
        block_num = 0
        etx_cycles = 0   # safety: bail out if ETX path loops more than once

        while True:
            ft_b = self.transport.read_raw_bytes(1, timeout=self.TIMEOUT)
            if not ft_b:
                raise IOError("YAPP: timeout waiting for data block or EOT")
            ft = ft_b[0]

            if ft in (_YAPP_EOT, _YAPP_ETX):
                if ft == _YAPP_ETX:
                    # Close handshake per WA7MBL YAPP RFC v1.1 (1986):
                    #   sender sends EF (ETX + sub=0x01)   = end-of-file
                    #   receiver MUST reply AF (ACK + 0x03) = Ack_EOF
                    #   sender then sends either:
                    #     ET (EOT + sub) → end-of-transmission, we reply AT
                    #     HD (SOH + hdr) → next file in batch, we reply RF
                    #
                    # LinBPQ32 over VARA quirk (captured 2026-05-07):
                    # even on a single-file download it tends to send a
                    # sentinel HD with the same filename and size=0 in
                    # place of ET. We treat that as "no more real files"
                    # and reply NR (proper [NAK][len][reason]), which
                    # closes the session even if LinBPQ logs it as
                    # "File Rejected" on its side. The download_file()
                    # finally block strips that artifact from the
                    # user-visible terminal output.
                    etx_cycles += 1
                    if etx_cycles > 2:
                        self._log("YAPP: EF path looped >2 times — aborting")
                        self._send_nr()
                        break

                    sub_b = self.transport.read_raw_bytes(1, timeout=5.0)
                    sub = sub_b[0] if sub_b else 0
                    self._log(f"YAPP: EF (ETX, sub=0x{sub:02x}) — sending AF")
                    self._send_af()

                    nxt_b = self.transport.read_raw_bytes(1, timeout=20.0)
                    if not nxt_b:
                        self._log("YAPP: no follow-up after AF — exiting")
                    else:
                        nxt = nxt_b[0]
                        self._log(f"YAPP: post-AF byte = 0x{nxt:02x}")

                        if nxt == _YAPP_EOT:
                            sub2_b = self.transport.read_raw_bytes(1, timeout=5.0)
                            sub2 = sub2_b[0] if sub2_b else 0
                            self._log(
                                f"YAPP: ET (EOT, sub=0x{sub2:02x}) — "
                                "sending AT (clean RFC close)")
                            self._send_at()
                        elif nxt == _YAPP_SOH:
                            hlen_b = self.transport.read_raw_bytes(1, timeout=10)
                            hlen = (hlen_b[0] if hlen_b else 0) or 256
                            hdata = self.transport.read_raw_bytes(hlen, timeout=15)
                            self._hex(bytes([_YAPP_SOH, hlen]) + hdata,
                                      "post-AF HD")
                            parts = hdata.split(b'\x00')
                            bf_name = parts[0].decode('ascii', errors='replace').strip()
                            try:
                                bf_size = int(parts[1].decode('ascii').strip()) \
                                    if len(parts) > 1 and parts[1] else 0
                            except ValueError:
                                bf_size = 0
                            self._log(
                                f"YAPP: HD file='{bf_name}' size={bf_size}")
                            if bf_size == 0:
                                self._send_nr()
                                self._log(
                                    "YAPP: sent NR for size=0 sentinel HD "
                                    "(LinBPQ end-of-batch quirk)")
                            else:
                                self._send_rf()
                                self._log(
                                    "YAPP: sent RF for batch HD — "
                                    "looping for next file")
                                continue
                        else:
                            self._log(f"YAPP: unexpected post-AF byte 0x{nxt:02x}")
                else:
                    # Standard ET (EOT) from non-LinBPQ senders that follow
                    # the RFC strictly without the size=0 HD quirk.
                    self._send_at()
                self._log(
                    f"YAPP: transfer complete "
                    f"({'EF/HD' if ft == _YAPP_ETX else 'ET'}) — "
                    f"{len(received)} bytes received")
                break

            elif ft == _YAPP_STX:
                blen_b = self.transport.read_raw_bytes(1, timeout=10)
                if not blen_b:
                    raise IOError("YAPP: timeout reading block length")
                blen = blen_b[0] or 256

                block = self.transport.read_raw_bytes(blen, timeout=20)
                if len(block) < blen:
                    self._send_nak()
                    raise IOError(
                        f"YAPP: short block (expected {blen}, got {len(block)})")

                received.extend(block)
                block_num += 1
                self._send_rr()

                if self.progress_cb:
                    self.progress_cb(len(received), filesize)
                total_str = str(filesize) if filesize else "?"
                self._log(
                    f"YAPP: block {block_num} — "
                    f"{len(received)}/{total_str} bytes")

            elif ft == _YAPP_CAN:
                raise IOError("YAPP: transfer cancelled by remote station")

            elif ft == _YAPP_ENQ:
                # Re-ENQ mid-transfer — consume the companion byte and re-send RR
                self.transport.read_raw_bytes(1, timeout=2.0)
                self._send_rr()

            else:
                self._log(f"YAPP: unexpected frame type 0x{ft:02x} — ignored")

        return filename, bytes(received)





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
        self._send(cmd)

        # LinBPQ sequence after SP CALL (always):
        #   (optional) "Address @HOMEBBS added from HomeBBS"
        #   "Enter Title (only):"       ← always present
        #   [we send title]
        #   "Enter Message Text ..."    ← always present
        #   [we send body + /EX]
        #
        # NOTE: LinBPQ sometimes splits "Enter Title (only):" across two TCP
        # packets — "Enter\r\n" arrives first, "Title (only):" arrives next.
        # Waiting for "itle" (substring of "Title") catches the second packet
        # and ensures we have the complete prompt before sending the title.
        title_response = self._expect("itle", timeout=30)

        if "itle" not in title_response.lower():
            # Timed out — send bare Enter to cancel at title prompt and recover
            self._log("SYS",
                f"Send FAILED: no title prompt received — got {title_response!r}")
            self.transport.send("")
            time.sleep(0.5)
            return False

        # 2. Send title/subject
        if not subject or not subject.strip():
            subject = "...."
        self._send(subject)

        # 3. Wait for body prompt
        self._expect("essage", timeout=30)

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
            # Accept BN (status N) and B$ (forwarded/status $) — skip PN and everything else
            new_bulls = [m for m in messages
                         if m.msg_type == "B"
                         and m.status in ("N", "$")]
            if new_bulls:
                results[cat] = new_bulls
                self._log("SYS",
                    f"Bulletins: {len(new_bulls)} new in {cat}")
            else:
                self._log("SYS", f"Bulletins: none new in {cat}")
        return results

    def list_last(self, n: int) -> list:
        """
        Send 'LL N' and return the parsed list of BBSMessage objects.
        LL N returns the N most recent messages on the BBS (all types).
        Used on first connect to discover the current high-water message number
        and to find any personal mail addressed to mycall.
        """
        self._send(f"ll {n}")
        raw = self._expect(self.PROMPT_BBS, timeout=60)
        messages = parse_message_list(raw)
        self._log("SYS", f"LL {n}: {len(messages)} messages parsed")
        return messages

    def list_since(self, watermark: int) -> list:
        """
        Send 'L watermark-' and return parsed BBSMessage objects.
        Returns all messages with number > watermark.
        Used on subsequent connects to fetch only messages newer than
        the last known high-water mark.
        """
        self._send(f"l {watermark}-")
        raw = self._expect(self.PROMPT_BBS, timeout=60)
        messages = parse_message_list(raw)
        self._log("SYS", f"L {watermark}-: {len(messages)} messages parsed")
        return messages

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

    def list_files(self) -> str:
        """
        Send 'files' to list files available on the BBS.
        Returns the raw text listing.
        Note: also works as a manual terminal command — this method is
        provided for any future automated file-browsing feature.
        """
        self._send("files")
        return self._expect(self.PROMPT_BBS, timeout=30)

    def download_file(self, filename: str, save_dir: str,
                      progress_cb=None) -> tuple:
        """
        Download a file from the BBS using YAPP protocol.

        Sends 'yapp <filename>', waits for the YAPP transfer, saves the
        received file to save_dir.  Returns (save_path: str, bytes_received: int).

        Filenames with spaces are not supported by LinBPQ YAPP — the caller
        should validate this before calling.

        Pauses the terminal monitor for the duration of the transfer and
        re-enables it on completion or error.
        """
        os.makedirs(save_dir, exist_ok=True)

        # Flush stale data BEFORE stopping the monitor, then stop the monitor.
        # Order matters: flush_input() must run while the monitor is still live
        # so it drains the socket cleanly; stopping the monitor afterwards
        # guarantees the YAPP frame bytes won't be consumed by the monitor thread
        # after we send the command.
        self.transport.flush_input()
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(False)

        try:
            self._send(f"yapp {filename}")

            receiver = YappReceiver(
                self.transport,
                progress_cb=progress_cb,
                log_cb=lambda msg: self._log("SYS", msg),
            )
            rcvd_name, data = receiver.receive()

            # Prefer the filename the BBS sent in the YAPP header;
            # fall back to the requested name if the header was empty.
            save_name = rcvd_name.strip() if rcvd_name.strip() \
                        else os.path.basename(filename)

            # Sanitize — keep alphanumeric, dots, dashes, underscores only
            safe = set("abcdefghijklmnopqrstuvwxyz"
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                       "0123456789._-")
            save_name = "".join(c for c in save_name if c in safe) \
                        or "yapp_download"

            save_path = os.path.join(save_dir, save_name)
            # Avoid silently overwriting an existing file
            if os.path.exists(save_path):
                base, ext = os.path.splitext(save_name)
                i = 1
                while os.path.exists(save_path):
                    save_path = os.path.join(save_dir, f"{base}_{i}{ext}")
                    i += 1

            with open(save_path, "wb") as f:
                f.write(data)

            self._log("SYS", f"File saved: {save_path}")
            return save_path, len(data)

        finally:
            # After YAPP, LinBPQ sends "de KC9MTP>" before returning to the
            # BBS prompt. Read until the prompt, then re-emit whatever we
            # captured as an [RX] line so the terminal view shows the prompt
            # the same way it does for any other command — without this the
            # user sees the download succeed but no prompt afterwards and
            # can't tell whether the BBS is hung or idle.
            try:
                tail = self.transport.read_until(">", timeout=20)
            except Exception:
                tail = ""
            if tail:
                # The terminal monitor is paused during YAPP, so anything
                # in this post-YAPP read is by definition cleanup noise
                # from LinBPQ — "File Rejected" log text from our NR for
                # the size=0 sentinel, stray control/punctuation bytes
                # (e.g. ',\x02' observed after multi-block transfers),
                # etc. Real BBS output arrives only AFTER terminal mode
                # is re-enabled. Find the BBS prompt and emit only from
                # there onwards so the user sees a clean prompt.
                m = re.search(r'(de\s+\S+>)', tail)
                if m:
                    self._log("RX", m.group(1))
                elif tail.strip():
                    # Fallback: no prompt found. Strip control bytes and
                    # emit whatever's left so we don't lose unexpected text.
                    cleaned = re.sub(
                        r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', tail)
                    if cleaned.strip():
                        self._log("RX", cleaned)
            self.transport.flush_input()
            if hasattr(self.transport, "set_terminal_mode"):
                self.transport.set_terminal_mode(True)
