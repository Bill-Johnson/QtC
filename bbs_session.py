# QtC v0.15.0-beta — bbs_session.py  (built 2026-10-01)
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
Tuned for N0CALL-7 LinBPQ node output format.
"""

import re
import time
import os
import textwrap
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
      BBS  → Client  [STX][len][data]        data block(s), repeat — NO reply
      BBS  → Client  [ETX][0x01]  (EF)       end of file
      Client → BBS   [ACK][0x03]  (AF)       ack end of file

    All ACKs are 2 bytes.  Single-byte 0x06 is NOT sufficient — LinBPQ
    waits for the 2-byte RR/RF response before proceeding.

    Data blocks are NOT acknowledged. The RFC's receiver sends nothing in
    state RD until EF, and LinBPQ does not wait for one — it streams. QtC
    used to RR every block, and at 300 baud that was the F2 hang
    (2026-09-23): the RRs queued up behind the BBS's traffic, AF sat at
    the back of that queue, and LinBPQ answered each late RR it read after
    the file ended with another size=0 sentinel HD — 81 blocks, exactly
    81 sentinels on the air. It also cost 81 needless transmissions.
    """

    TIMEOUT = 60   # seconds to wait for any single frame

    def __init__(self, transport, progress_cb=None, log_cb=None):
        self.transport   = transport
        self.progress_cb = progress_cb   # callable(bytes_done: int, total: int)
        self.log_cb      = log_cb        # callable(msg: str)
        # True once the user has aborted and CN has gone out. The loop
        # keeps running after that — see _begin_cancel.
        self._cancelling = False
        self.aborted_outstanding = 0
        # How the transfer closed: "ET" (sender sent ET, we sent AT — the
        # RFC close) or "HD" (size=0 sentinel, we sent NR). LinBPQ only
        # re-sends its prompt after the second; download_file() needs to
        # know which, or it waits a full silence window for nothing.
        self.close_kind = ""

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

    def _send_cn(self, reason: bytes = b""):
        """CN (Cancel): [CAN][len][optional reason ASCII] per RFC v1.1.
        Sent when the receiver is giving up on the transfer entirely —
        tells the sender to stop transmitting so its bytes don't leak
        into the terminal once YAPP exits."""
        self.transport.send_raw(bytes([_YAPP_CAN, len(reason)]) + reason)

    def _send_ca(self):
        """CA (Can_Ack): [ACK][0x05] — owed to a station that sends us CN.
        RFC v1.1: "Any state except CW: on CN → send CA → Done". We used to
        raise without answering, leaving the sender waiting out its own
        timer with the link still up."""
        self.transport.send_raw(bytes([_YAPP_ACK, 0x05]))

    # ── Block read with stall-watchdog (slow-HF safe) ──────────────────
    # Memory: project-yapp-short-block-todo. The original fixed 20s timeout
    # was too short at 61 bps (234 bytes ≈ 30s pure transmit, before ARQ).
    # Replaced with a watchdog that resets as long as bytes keep arriving.

    BLOCK_STALL_TIMEOUT  = 30.0   # give up if no bytes arrive for this long
    BLOCK_HARD_DEADLINE  = 300.0  # absolute ceiling per block (5 min)

    def _read_block_with_watchdog(self, n: int) -> bytes:
        """Read up to n bytes. Reset the deadline whenever bytes arrive;
        give up only on a true stall (no bytes for BLOCK_STALL_TIMEOUT)
        or on the hard ceiling. Returns whatever was actually received —
        caller checks len(result) vs n."""
        received = bytearray()
        hard_end = time.time() + self.BLOCK_HARD_DEADLINE
        last_progress = time.time()
        while len(received) < n:
            now = time.time()
            if now > hard_end:
                self._log(
                    f"YAPP: block hard deadline ({self.BLOCK_HARD_DEADLINE}s) "
                    f"hit at {len(received)}/{n}")
                break
            if now - last_progress > self.BLOCK_STALL_TIMEOUT:
                self._log(
                    f"YAPP: block stall — no bytes for "
                    f"{self.BLOCK_STALL_TIMEOUT}s at {len(received)}/{n}")
                break
            want  = n - len(received)
            chunk = self.transport.read_raw_bytes(want, timeout=2.0)
            if chunk:
                received.extend(chunk)
                last_progress = time.time()
        return bytes(received)

    # ── Abort + wire drain ─────────────────────────────────────────────
    # Memory: project-yapp-short-block-todo problem #2. Without this, after
    # an in-flight abort the BBS keeps transmitting the rest of the file
    # and those bytes leak into the terminal log when set_terminal_mode(True)
    # re-engages in download_file()'s finally block.

    DRAIN_QUIET_SECS    = 2.0   # consider the wire idle after this long
    DRAIN_TOTAL_TIMEOUT = 30.0  # cap how long we wait for it to go quiet

    def _begin_cancel(self, filesize: int, got: int):
        """User pressed Abort. Send CN and then SAY NOTHING MORE.

        This is the part that is not obvious, and it took three on-air
        runs to get right (2026-09-23, J7, 300 baud).

        LinBPQ leaves YAPP the instant it receives the CN — its log says
        "YAPP Transfer cancelled by Terminal" — and goes straight back to
        the BBS command interpreter. But it does NOT take back what it
        has already handed to the AX.25 stream, so the rest of the file
        keeps arriving for minutes afterwards.

        Two consequences, both counter-intuitive:

        1. Acknowledging those blocks does not make them drain faster.
           The sender is not waiting on us any more.
        2. Worse, every byte we send after the CN is TYPED INTO THE BBS
           COMMAND LINE. An earlier version of this kept answering RR,
           and the BBS buffered all 111 of them; the operator's next
           command arrived as
               <0x06><0x01> x111 <0x06><0x03><0x15><0x00> lm
           and came back "Invalid Command". On the air it looked like
           minutes of pointless two-byte ping-pong, because that is
           exactly what it was.

        So: send CN, stop transmitting, and let _drain_after_abort read
        and discard until the BBS prompt appears.
        """
        self._cancelling = True
        self.aborted_outstanding = max(0, filesize - got)
        self._send_cn(b"aborted by the user")
        self._log(
            f"YAPP: sent CN — aborted by the user. About "
            f"{self.aborted_outstanding} bytes are already on their way "
            f"from the BBS; swallowing them in silence. Nothing more is "
            f"transmitted — after a cancel the BBS reads anything we send "
            f"as a command.")

    def _user_aborted(self) -> bool:
        """True once the GUI's Abort button has set the transport flag.
        Same flag the mail and bulletin downloads watch, so one button
        covers every kind of transfer."""
        return bool(getattr(self.transport, "abort_requested",
                            lambda: False)())

    def _abort_and_drain(self, reason: str):
        """Send CN with a short reason, then drain the wire until quiet.
        Always called BEFORE raising IOError on a mid-transfer abort —
        leaves the link in a state where download_file()'s finally block
        finds the BBS prompt quickly instead of swallowing file bytes."""
        try:
            reason_b = reason.encode("ascii", errors="replace")[:200]
            self._send_cn(reason_b)
            self._log(f"YAPP: sent CN — {reason}")
        except Exception as e:
            self._log(f"YAPP: failed to send CN ({e}) — draining anyway")

        drained = 0
        quiet_start = None
        end = time.time() + self.DRAIN_TOTAL_TIMEOUT
        while time.time() < end:
            chunk = self.transport.read_raw_bytes(256, timeout=1.0)
            if chunk:
                drained += len(chunk)
                quiet_start = None
            else:
                if quiet_start is None:
                    quiet_start = time.time()
                elif time.time() - quiet_start >= self.DRAIN_QUIET_SECS:
                    break
        self._log(
            f"YAPP: drained {drained} bytes after abort "
            f"(quiet={quiet_start is not None})")

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
            # User pressed Abort. Checked at frame boundaries only, never
            # mid-block: the sender is waiting on our reply here, so CN
            # lands in a state it understands and stops it dead. Breaking
            # a part-read block instead would be indistinguishable from a
            # short block on a bad link. Cost is that an abort waits out
            # the block already on the air — up to ~8 s of a 256-byte
            # block at 300 baud (Bill, 2026-09-23).
            if self._user_aborted() and not self._cancelling:
                self._begin_cancel(filesize, len(received))
                raise DownloadAborted()

            ft_b = self.transport.read_raw_bytes(1, timeout=self.TIMEOUT)
            if not ft_b:
                self._abort_and_drain("inter-frame timeout — sender went silent")
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
                            self.close_kind = "ET"
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
                                self.close_kind = "HD"
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
                    self.close_kind = "ET"
                self._log(
                    f"YAPP: transfer complete "
                    f"({'EF/' + (self.close_kind or '?') if ft == _YAPP_ETX else 'ET'}) — "
                    f"{len(received)} bytes received")
                break

            elif ft == _YAPP_STX:
                blen_b = self.transport.read_raw_bytes(1, timeout=10)
                if not blen_b:
                    self._abort_and_drain("timeout reading block length")
                    raise IOError("YAPP: timeout reading block length")
                blen = blen_b[0] or 256

                block = self._read_block_with_watchdog(blen)
                if len(block) < blen:
                    self._abort_and_drain(
                        f"short block ({len(block)}/{blen}) — link too slow")
                    raise IOError(
                        f"YAPP: short block (expected {blen}, got {len(block)})")

                if self._user_aborted() and not self._cancelling:
                    self._begin_cancel(filesize, len(received))
                    raise DownloadAborted()

                # No RR here — see the class docstring (F2, 2026-09-23).
                received.extend(block)
                block_num += 1

                if self.progress_cb:
                    self.progress_cb(len(received), filesize)
                total_str = str(filesize) if filesize else "?"
                self._log(
                    f"YAPP: block {block_num} — "
                    f"{len(received)}/{total_str} bytes")

            elif ft == _YAPP_CAN:
                # [CAN][len][reason] — consume the reason so it cannot leak
                # into the terminal, answer with CA as the RFC requires,
                # then give up.
                reason = b""
                rlen_b = self.transport.read_raw_bytes(1, timeout=2.0)
                if rlen_b and rlen_b[0]:
                    reason = self.transport.read_raw_bytes(
                        rlen_b[0], timeout=5.0)
                try:
                    self._send_ca()
                    self._log("YAPP: remote sent CN — answered CA")
                except Exception as e:
                    self._log(f"YAPP: could not send CA ({e})")
                why = reason.decode("ascii", errors="replace").strip()
                raise IOError("YAPP: transfer cancelled by remote station"
                              + (f" — {why}" if why else ""))

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
    at_bbs:     str      # home BBS e.g. N0CALL-1 (may be blank)
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


# ─────────────────────────────────────────────
# Outgoing line wrapping
# ─────────────────────────────────────────────
#
# There is no formal standard here — nothing in AX.25, Part 97, BPQ or
# FBB mandates a line length. What there is, is a universal convention
# inherited from 80-column terminals and still repeated in every packet
# primer: hard CR at 70-75 characters, never past 80.
#
# It matters because:
#   - old BBS editors (MSYS and friends) only handle 80-char lines and
#     bounce anything longer back at the sender
#   - some terminals and printers silently drop everything past column 80
#   - a TNC fragments at PACLEN regardless of word boundaries, so a long
#     line arrives chopped mid-word ("...ANNOUNCE TH" / "AT WE ARE...")
#
# QtC's compose box is a QTextEdit, which soft-wraps at the WIDGET width.
# That looks wrapped on screen, but toPlainText() returns logical lines —
# so a typed paragraph went out as one 400-character line. We wrap here,
# at the single choke point every outgoing message passes through.

WRAP_COLUMNS = 72

# Leading whitespace and/or '>' quote markers, reused as the hanging
# indent so a wrapped quoted line stays quoted on every continuation.
_LINE_PREFIX_RE = re.compile(r"^(\s*(?:>+\s*)*)")


def wrap_body(body: str, width: int = WRAP_COLUMNS) -> List[str]:
    """
    Wrap an outgoing message body to `width` columns, breaking at spaces.

    Minimal-touch by design: any line already within the limit is passed
    through byte for byte. That preserves blank lines, indentation, ASCII
    art, tables, separator rules and quoted text — rewrapping those would
    do more damage than the long lines we are here to fix.

    **Never splits a word.** A single token longer than `width` (a URL, a
    hierarchical address) is left to overhang rather than be broken — a
    chopped URL is useless, an over-long line is merely untidy. Hyphen
    splitting is off too, so `N0CALL-1` and `N0CALL.#REGION.ST.USA.NOAM`
    survive intact.
    """
    out: List[str] = []
    for line in (body.splitlines() or [""]):
        if len(line) <= width:
            out.append(line)          # untouched — formatting preserved
            continue
        prefix = _LINE_PREFIX_RE.match(line).group(1)
        rest   = line[len(prefix):]
        pieces = textwrap.wrap(
            rest,
            width=max(20, width - len(prefix)),
            break_long_words=False,   # never chop a URL or long token
            break_on_hyphens=False,   # never split N0CALL-1 or an H-address
        )
        out.extend(prefix + p for p in pieces) if pieces else out.append(line)
    return out


# ─────────────────────────────────────────────
# Message list parser
# ─────────────────────────────────────────────
#
# Actual output from N0CALL-7:
#
# 233    10-Mar PN      78 N0CALL @N0CALL-1 N0CALL test 2
# 232    10-Mar PN      62 SYSOP          SYSTEM New User N0CALL
# 227    07-Mar PY     172 SYSOP          SYSTEM Housekeeping Results
#
# Columns:
#   msg#   date    type+status   size   TO [@HOMEBBS]   FROM   subject
#
# Notes:
#   - Type and status are concatenated with no space: PN, PY, BN, BY
#   - Status is any of N Y H F D K $ — F (forwarded) and D (delivered) are
#     set by the BBS's own forwarding, not by the user, so a line carrying
#     one must still parse. We no longer FILTER on status (bulletins are
#     selected by "do we already have it", not by flag), but we do have to
#     READ every line, so the class has to be complete.
#   - @HOMEBBS is optional — only present when TO has a registered home BBS.
#     LinBPQ sometimes pads it as "@ EWN" (space after the @) when the
#     TO field is short, so the space is optional too.
#   - SYSOP messages have no @BBS and FROM is SYSTEM
#   - Size is right-aligned in a ~6 char field

MSG_LINE_RE = re.compile(
    r"^\s*"
    r"(\d+)"                        # group 1: message number
    r"\s+"
    r"(\d{1,2}-\w{3})"              # group 2: date e.g. 10-Mar
    r"\s+"
    r"([PBT\$])"                    # group 3: type P/B/T/$
    r"([NYHFDK\$\s])"               # group 4: status N/Y/H/F/D/K/$
    r"\s+"
    r"(\d+)"                        # group 5: size in bytes
    r"\s+"
    r"([\w\-]+)"                    # group 6: TO callsign
    r"(?:\s+@\s*([\w\-]+))?"        # group 7: optional @HOMEBBS
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

class DownloadAborted(Exception):
    """Raised inside the download path when the user aborts a transfer.
    Caught by download_messages, which then sends 'A' to the BBS and
    resyncs to the command prompt — the session stays connected."""
    pass


class BBSSession:
    """
    Manages a full BBS session for LinBPQ/BPQ32.
    Tuned for the N0CALL-7 node prompt and login sequence.

    Login sequence observed:
        user: <callsign>
        password: <blank>
        Welcome to N0CALL's Telnet Server
        Enter ? for list of commands
        MYNODE:N0CALL-7} BBS CONNECT BYE INFO ...
        bbs
        MYNODE:N0CALL-7} Connected to BBS
        [BPQ-6.0.24.52-IHJM$]
        Hello Bill. Latest Message is 235, Last listed is 229
        de N0CALL>
    """

    # Prompts — matched case-insensitively
    # LinBPQ answers "Username:", other nodes "user:" or "login:". A bare
    # "user:" does NOT substring-match "Username:" (the next char is 'n'),
    # so matching on one spelling alone burned the full 10s timeout on
    # every Telnet connect before falling through.
    PROMPT_LOGIN     = "user:"
    PROMPT_LOGIN_ALT = ["username:", "user:", "login:", "callsign:"]
    PROMPT_PASSWORD = "password:"
    PROMPT_NODE     = "}"          # node prompt ends with }
    PROMPT_BBS      = ">"          # universal — works for all node types
    CMD_TIMEOUT     = 20
    # RF login (_rf_login): give up after this long with nothing received,
    # and nudge a BBS that goes silent after its banner with 'A'.
    LOGIN_QUIET_SECS = 60
    LOGIN_NUDGE_SECS = 20
    TELNET_NUDGE_SECS = 3

    def __init__(self, transport, mycall: str, password: str = "",
                 telnet_user: str = "", user_info: dict = None):
        self.transport   = transport
        self.mycall      = mycall.upper()
        self.telnet_user = telnet_user if telnet_user else mycall
        self.password    = password
        self.user_info         = user_info or {}  # name, qth, zip, home_bbs
        # True when the BBS is sitting at a listing's page prompt
        # ("<A>bort, <R Msg(s)>, <CR> = Continue..>") rather than at the
        # command prompt. Anything about to send a real command — `b`
        # most of all — has to know: BPQ does not accept `b` at the page
        # menu, it just re-issues the menu, and dropping the link there
        # leaves the session open on the BBS side.
        self.at_page_prompt    = False
        # Both of these are read from main_window THROUGH the session object
        # (self.session.new_user, self.session.page_length). Nothing inside
        # this class reads them, so an attribute sweep that only looks for
        # self.<name> will call them dead. They are not (2026-09-23).
        self.new_user          = False            # set True if BBS prompts registration
        self.page_length       = 0                # lines per page; 0 = paging off
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

    def _reply_wait(self, timeout: int = None) -> int:
        """Seconds of SILENCE to allow while waiting on a BBS reply.

        Transport reads already restart their clock while bytes arrive,
        so this is never a cap on how long a reply may take. Packet
        transports add a floor (MIN_REPLY_WAIT): at 300 baud, RF retries
        leave long quiet gaps mid-reply, and the modem reports a dead link
        on its own — QtC must not give up and talk over the BBS."""
        return max(timeout or self.CMD_TIMEOUT,
                   getattr(self.transport, "MIN_REPLY_WAIT", 0))

    def _expect(self, prompt: str, timeout: int = None) -> str:
        t = self._reply_wait(timeout)
        data = self.transport.read_until(prompt, timeout=t)
        # Skip [RX] log when the transport reader is already streaming
        # complete lines to the terminal view — logging the full response
        # here would duplicate everything the reader just emitted.
        if not getattr(self.transport, "_terminal_mode", False):
            self._log("RX", data)
        return data

    def _expect_silent(self, prompt: str, timeout: int = None) -> str:
        """Like _expect but does not log — caller handles logging."""
        t = self._reply_wait(timeout)
        return self.transport.read_until(prompt, timeout=t)

    # ── BBS output paging (the OP command) ────────────────────────
    #
    # `OP n` tells the BBS to send at most n lines before it stops and
    # offers a menu:
    #
    #     <A>bort, <R Msg(s)>, <CR> = Continue..>
    #
    # Bare CR carries on, "A" abandons the listing — and an `R <msg#>`
    # typed right there both ENDS the listing and STARTS the read, which
    # saves a whole round trip on a slow RF link. That is why the paged
    # readers below can hand the session back to the caller still parked
    # at this prompt instead of always aborting first.
    #
    # The prompt ends in '>' so it must always be matched BEFORE
    # PROMPT_BBS in a read_until_any() list — read_until_any takes the
    # earliest match in the buffer, which handles that for us.
    #
    # Inside a READ the pause is shorter, and it is not the same menu:
    #
    #     <A>bort, <CR> Continue..>
    #
    # No "<R Msg(s)>" and no "= " — only A or CR are answers there, so
    # `R <msg#>` must never be typed into it. Aborting it drops the BBS
    # back to wherever the `r` was typed: the listing menu again, or the
    # command prompt. Verified on air 2026-09-11 (#779).
    PAGE_MARKERS   = ["<A>bort", "= Continue"]
    MAX_PAGES      = 12     # absolute safety stop for any paged read
    MAX_MAIL_PAGES = 10     # LM — your own mailbox, worth paging through
    # LinBPQ answers `op n` with one of (captured live 2026-09-23):
    #     "Page Length is 10"          accepted
    #     "Page Length 9 is too short" refused — 10 is the floor
    # The old check was `"page length" in raw.lower()`, which is in BOTH,
    # so a refused OP was logged as confirmed and QtC then believed the
    # BBS was paging at a length it had never accepted.
    PAGE_CONFIRM   = re.compile(r'page\s+length\s+is\s+(\d+)', re.I)
    PAGE_REFUSED   = re.compile(r'page\s+length\s+\d+\s+is\s+too\s+short', re.I)
    PAGE_MIN       = 10    # LinBPQ's floor; 0 still means "no paging"

    def _finish_page_prompt(self, matched: str) -> bool:
        """Read the rest of a page prompt line once one of PAGE_MARKERS has
        matched, so none of it is left behind for the next read.

        Returns True for a read pause ("<A>bort, <CR> Continue..>"), False
        for a listing menu (which offers "<R Msg(s)>" or "<R Message>").

        The BBS is waiting on us, so nothing else is in flight. On a slow
        packet link the line arrives in pieces — "<A>bort, <R Msg(" in one
        frame and "s)>, <CR> = Continue..>" a second later — so read on to
        its closing '>' rather than only draining what has already landed.
        A leftover "= Continue..>" would be taken as a second page prompt.

        Wait for "Continue", not "= Continue": the read pause has no "=",
        and waiting for one cost two full reply timeouts at every page of
        a bulletin — 4 minutes a page at 300 baud, 46 minutes for one
        7.5 KB SITREP (2026-09-11).
        """
        t0 = time.time()
        rest = ""
        if matched == "<A>bort":
            rest = self.transport.read_until("Continue",
                                             timeout=self._reply_wait(15))
        t_cont = time.time()
        rest += self.transport.read_until(">", timeout=self._reply_wait(15))
        t_gt = time.time()
        self.transport.read_all_pending(settle_time=0.3)
        # Time the two reads separately. Both get _reply_wait(15), which on
        # packet is floored to MIN_REPLY_WAIT (120 s) — so a prompt tail
        # that never arrives costs two minutes with nothing in the log to
        # say why. A 94 s gap between the BBS sending a listing page prompt
        # and QtC's 'A' was seen on air 2026-09-20 and could not be pinned
        # down afterwards, because the log recorded only the start and the
        # end of the whole listing (Bill: "something similar to what we
        # fixed earlier").
        if t_gt - t0 >= 2.0:
            self._log("SYS",
                f"Page prompt tail took {t_gt - t0:.1f}s "
                f"(to 'Continue' {t_cont - t0:.1f}s, then to '>' "
                f"{t_gt - t_cont:.1f}s) after matching {matched!r} — "
                f"tail={rest[-60:]!r}")
        return matched == "<A>bort" and "<r " not in rest.lower()

    def _expect_paged(self, terminator: str, timeout: int = None,
                      max_pages: int = None, on_page=None):
        """
        Read until `terminator`, transparently handling BBS output paging.

        Returns (text, state):
          text  — everything received, with the page-prompt lines removed
          state — "prompt"  terminator reached; BBS is at the command prompt
                  "paged"   stopped AT a page prompt; the BBS is still
                            waiting for input and the CALLER owns the next
                            command (send `R <msg#>` to read and close the
                            listing in one go, or end_listing() to bail)
                  "timeout" nothing matched in time
                  "abort"   the user hit Abort

        on_page(page_no, text_so_far) runs at each page prompt and returns:
            ""     send CR and keep paging   (also the default when
                   on_page is None — i.e. page straight through)
            "A"    abort the listing and read on to `terminator`
            None   stop here and hand the BBS back to the caller, parked

        If paging is off on this BBS (OP 0, or the sysop disabled it) no
        page prompt ever arrives and this behaves exactly like _expect().
        """
        t   = self._reply_wait(timeout)
        cap = max_pages if max_pages is not None else self.MAX_PAGES
        collected = ""
        page = 0

        while True:
            # `t` is how long the BBS may go SILENT, not how long the whole
            # page may take. One 20-line page took 91 s at 300 baud packet;
            # a total-time 60 s gave up mid-page, QtC moved on, and the BBS
            # was left parked at a menu nobody was reading (2026-09-10).
            chunk, matched = self.transport.read_until_any(
                self.PAGE_MARKERS + [terminator], timeout=t)

            if matched is None:
                collected += chunk
                if getattr(self.transport, "abort_requested",
                           lambda: False)():
                    return collected, "abort"
                return collected, "timeout"

            if matched == terminator:
                collected += chunk
                if not getattr(self.transport, "_terminal_mode", False):
                    self._log("RX", collected)
                self.at_page_prompt = False
                return collected, "prompt"

            # ── page prompt ──────────────────────────────────────
            page += 1
            idx = chunk.lower().rfind(matched.lower())
            collected += chunk[:idx] if idx >= 0 else chunk
            # Swallow the rest of the prompt line — it can arrive in pieces.
            self._finish_page_prompt(matched)

            reply = "" if on_page is None else on_page(page, collected)
            if reply is None:
                # Log what the page actually contained before handing the
                # session over — otherwise a parked listing is invisible in
                # both the Terminal and Debug views (the monitor is paused
                # for the whole sweep, so this is the only record of it).
                if not getattr(self.transport, "_terminal_mode", False):
                    self._log("RX", collected)
                self._log("SYS",
                    f"Paused at page {page} — handing the BBS to the caller")
                self.at_page_prompt = True
                return collected, "paged"

            if reply == "" and page >= cap:
                self._log("SYS",
                    f"Paging safety cap hit at {cap} pages — aborting listing")
                reply = "A"

            self._send(reply)          # "" is a bare CR; "A" aborts
            if reply.strip().upper() == "A":
                tail, _ = self.transport.read_until_any(
                    [terminator], timeout=self._reply_wait(30))
                collected += tail
                if not getattr(self.transport, "_terminal_mode", False):
                    self._log("RX", collected)
                return collected, "prompt"

    def set_page_length(self, lines: int) -> bool:
        """
        Send `OP n` — how many lines the BBS sends before it pauses.

        Keeping this low is good RF manners: it stops a month of daily
        bulletins from tying up the frequency in one uninterruptible
        listing. LinBPQ confirms with "Page length is 20".

        lines=0 turns paging off entirely (BBS sends everything in one
        go) — how QtC behaved before the LM/LC/L> scheme.

        Returns one of three words, because the caller has to tell them
        apart:
            "confirmed"    the BBS took it. page_length now reflects it.
            "refused"      the value is out of range for this BBS (below
                           PAGE_MIN). Ours to fix, so the caller should
                           NOT remember it and should try again once the
                           setting changes.
            "unconfirmed"  nothing recognisable came back — this BBS
                           probably has no OP command. Retrying every
                           connect would only spend airtime, so the
                           caller remembers it and moves on.
        """
        n = int(lines)
        self._send(f"op {n}")
        raw = self._expect(self.PROMPT_BBS, timeout=30)

        if self.PAGE_REFUSED.search(raw):
            self._log("SYS",
                f"OP {n} REFUSED by the BBS — {n} is below its minimum "
                f"(LinBPQ wants {self.PAGE_MIN} or more). Paging is "
                f"unchanged; fix the page limit in Settings -> App. "
                f"({raw.strip()[:80]!r})")
            return "refused"

        m = self.PAGE_CONFIRM.search(raw)
        if m:
            # Trust the number the BBS echoed, not the one we asked for.
            self.page_length = int(m.group(1))
            self._log("SYS", f"OP {n} confirmed — page length is "
                             f"{self.page_length}")
            return "confirmed"

        # No idea — assume it went in, since that is what QtC has always
        # assumed here, but say so plainly in the log.
        self.page_length = n
        self._log("SYS",
            f"OP {n} — NO confirmation from the BBS ({raw.strip()[:80]!r})")
        return "unconfirmed"

    def end_listing(self, reason: str = "") -> str:
        """
        Release a listing that is parked at a page prompt.

        Two callers, two different reasons, so the reason is the caller's
        to supply — this used to log "Nothing wanted from this listing",
        which was wrong on the interactive path, where we close the
        listing precisely so a selection dialog can open on top of it.

        Skipping this is worth a round trip: if we already know a message
        we want, `R <msg#>` answers the page menu directly and ends the
        listing as a side effect.
        """
        why = f" ({reason})" if reason else ""
        self._log("SYS", f"Closing listing — sending 'A'{why}")
        self._send("A")
        raw = self._expect(self.PROMPT_BBS, timeout=30)
        self.at_page_prompt = False
        return raw

    # ── public API ────────────────────────────────────────────────

    def connect_and_login(self) -> bool:
        """
        Connect and log in to the BBS.

        Dispatches to the correct login sequence based on transport type:
          - TelnetTransport  → full LinBPQ telnet login (user/password/bbs)
          - VaraTransport /
            AGWTransport     → RF login (transport.connect() brings up the
                               link; just wait for BBS prompt)
        """
        from transport import VaraTransport, AGWTransport
        # Fresh link — nothing is parked, whatever the last session did.
        self.at_page_prompt = False
        if isinstance(self.transport, (VaraTransport, AGWTransport)):
            return self._rf_login()
        return self._telnet_login()

    def connect_only(self):
        """Bring up the link and nothing else — the Terminal / Debug view's
        dumb terminal (Bill, 2026-09-13). Node text, banner and prompt
        stream to the Terminal as they arrive. The one exception is Telnet's
        username and password, which are answered for the user; everything
        after that, the `bbs` command included, is theirs to type."""
        from transport import TelnetTransport
        self.at_page_prompt = False
        self.transport.connect()
        if self._rf_connected_cb:
            self._rf_connected_cb()
        if isinstance(self.transport, TelnetTransport):
            self._telnet_credentials()

    def _telnet_credentials(self):
        """Answer the Telnet username and password prompts, then stop. Each
        answer goes only once its prompt has actually arrived — if one never
        shows, the user takes it from there. The prompts still stream to the
        Terminal; the password is masked in the log by _send()."""
        _, matched = self.transport.read_until_any(
            self.PROMPT_LOGIN_ALT, timeout=10)
        if not (matched and self.telnet_user):
            self._log("SYS", "No username prompt seen — type the login yourself")
            return
        self._send(self.telnet_user)
        _, matched = self.transport.read_until_any(
            [self.PROMPT_PASSWORD], timeout=10)
        if not (matched and self.password):
            self._log("SYS", "No password prompt seen — type it yourself")
            return
        self._send(self.password)

    def learn_prompt(self):
        """Pick up the BBS prompt from what the Terminal has already shown.
        A Terminal / Debug connect skips the login that normally finds it,
        and QtC's own reads (Check Mail, Get File…) need it. Sends nothing;
        no-op once the prompt is known."""
        if self.PROMPT_BBS != BBSSession.PROMPT_BBS:
            return
        peek = getattr(self.transport, "peek_input", None)
        text = peek() if peek else ""
        found = re.findall(r"de\s+\S+>", text, re.IGNORECASE)
        if found:
            self.PROMPT_BBS = found[-1]
        elif re.search(r"[\r\n]>", text):
            self.PROMPT_BBS = "\r>"
        else:
            return
        self._log("SYS",
            f"BBS prompt picked up from the terminal: {self.PROMPT_BBS!r}")

    def _await_bbs_prompt(self, quiet: float, nudge: float) -> str:
        """Read until the real BBS prompt (de CALL>, or a bare '>' on its
        own line) or a registration prompt arrives, or `quiet` seconds of
        silence pass. Returns everything received.

        A BBS left parked at a page menu by an earlier session that
        dropped mid-listing picks that listing back up on the NEXT entry,
        whatever the transport: it sends the banner, then either re-shows
        the menu or says nothing while it waits for an answer. Answer 'A'
        once — straight away if the menu shows, or after `nudge` seconds
        of silence following the [BPQ- SID — so it lets go and prints its
        prompt. Seen on RF 2026-09-10; on Telnet 2026-09-29, when a D8
        listing dropped over RF was still parked 12 minutes later and the
        Telnet login gave up with "No BBS prompt received".
        """
        import time as _time
        bbs_response = ""
        last_rx      = _time.time()
        sent_abort   = False
        while _time.time() - last_rx < quiet:
            if getattr(self.transport, "abort_requested", lambda: False)():
                break
            chunk = self.transport.read_until(">", timeout=2)
            if chunk:
                bbs_response += chunk
                last_rx = _time.time()
            if (re.search(r"de\s+\S+>", bbs_response, re.IGNORECASE)
                    or re.search(r"(^|[\r\n])>\s*$", bbs_response)
                    or self._registration_field(bbs_response)):
                break
            if sent_abort:
                continue
            parked_menu = any(m.lower() in bbs_response.lower()
                              for m in self.PAGE_MARKERS)
            silent_after_banner = (
                "[BPQ-" in bbs_response.upper()
                and _time.time() - last_rx >= nudge)
            if parked_menu or silent_after_banner:
                self._log("SYS",
                    "BBS is still holding an earlier listing — sending 'A' "
                    "to release it")
                self._send("A")
                sent_abort = True
                last_rx = _time.time()
        return bbs_response

    def _telnet_login(self) -> bool:
        """Full LinBPQ telnet login sequence."""
        self.transport.connect()
        self._log("SYS", f"TCP connected to "
                         f"{self.transport.host}:{self.transport.port}")

        # Step 1: username
        banner, matched = self.transport.read_until_any(
            self.PROMPT_LOGIN_ALT, timeout=10)
        self._log("RX", banner)
        if not matched:
            self._log("SYS",
                "WARNING: Did not see a username prompt "
                f"(looked for {self.PROMPT_LOGIN_ALT})")
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
        # A BBS still holding a listing from a dropped session sends only
        # its SID here, never a '>' — see _await_bbs_prompt. A LAN answers
        # in well under a second, so a short silence is proof enough.
        bbs_response = self._await_bbs_prompt(15, self.TELNET_NUDGE_SECS)
        if not getattr(self.transport, "_terminal_mode", False):
            self._log("RX", bbs_response)

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

        # Handle new user registration if needed. _handle_registration
        # returns the final BBS response itself — no need to _expect again
        # (the inner loop already consumed the trailing prompt).
        handled, bbs_response = self._handle_registration(bbs_response)
        if handled:
            self._log("SYS", f"Post-registration BBS response: {bbs_response!r}")

        return True

    def _rf_login(self) -> bool:
        """
        RF login sequence — VARA HF/FM, or AX.25 packet through an AGW
        modem (Direwolf, (Qt)SoundModem).

        Calls transport.connect(), which brings up the RF link — VARA's
        MYCALL + CONNECT on its command port, or an AGW 'C' frame — and
        returns once the link is established.

        Once connected, LinBPQ sends the BBS banner automatically —
        no username or password prompt, no 'bbs' command needed.
        We just wait for the '>' prompt.
        """
        link = getattr(self.transport, "modem_name", "VARA")
        self.transport.connect()
        self._log("SYS", f"{link} RF link established — waiting for BBS prompt…")
        if self._rf_connected_cb:
            self._rf_connected_cb()

        # The BBS sends its banner automatically — but on some ports the
        # node talks first:
        #     Welcome to the N0CALL-7 LinBPQ Node.
        #     COMMANDS> BBS CHAT CONNECT BYE INFO NODES ROUTES PORTS USERS MHEARD
        #     MYNODE:N0CALL-7} Connected to BBS
        #     [BPQ-6.0.25.36-IHJM$]
        #     de N0CALL>
        # That "COMMANDS>" is not the BBS. Taking it as the prompt left
        # PROMPT_BBS a bare '>', put every later command one reply out of
        # step, and let the '>' in a split "<A>bort" page prompt end a
        # listing early (300 baud packet port, 2026-09-10). Keep reading
        # until the real prompt (de CALL>, or a bare '>' on its own line)
        # or a new-user registration prompt arrives.
        #
        # A BBS left parked at a page menu by an earlier session that
        # dropped mid-listing picks that session back up on reconnect: it
        # sends the banner, then either re-shows the menu or says nothing
        # at all while it waits for an answer (seen the same evening — the
        # old 60 s wait ran out and QtC carried on with a bare '>'). Answer
        # 'A' once so it lets go of the old listing and prints its prompt.
        # Silence is timed from the last byte received, not from connect,
        # so a slow node welcome with RF retries can't run the clock out.
        bbs_response = self._await_bbs_prompt(
            self._reply_wait(self.LOGIN_QUIET_SECS), self.LOGIN_NUDGE_SECS)
        self._log("SYS", f"BBS response: {bbs_response!r}")

        if ">" not in bbs_response:
            self._log("SYS",
                f"WARNING: No BBS prompt received: {bbs_response!r}")
            return False

        # Check for new user registration prompts in the banner.
        # LinBPQ asks for Name (and optionally QTH, Zip, Home BBS) before
        # showing the normal "de CALLSIGN>" prompt. We auto-fill from
        # user_info settings to save RF time. _handle_registration returns
        # the final BBS response — no need to _expect again here.
        handled, bbs_response = self._handle_registration(bbs_response)
        if handled:
            self._log("SYS", f"Post-registration BBS response: {bbs_response!r}")

        # Detect exact BBS prompt style
        m = re.search(r"(de\s+\S+>)", bbs_response, re.IGNORECASE)
        if m:
            self.PROMPT_BBS = m.group(1)   # e.g. "de N0CALL>"
        elif "\r>" in bbs_response or "\n>" in bbs_response:
            self.PROMPT_BBS = "\r>"
        self._log("SYS", f"BBS prompt detected as: {self.PROMPT_BBS!r}")
        self._log("SYS", f"BBS login successful via {link}")
        return True

    # Registration prompt keywords — checked case-insensitively
    REGISTRATION_PROMPTS = [
        ("name",     ["enter your name", "your name"]),
        ("qth",      ["enter your qth",  "your qth", "enter qth"]),
        ("zip",      ["enter your zip",  "zip code", "postcode", "enter zip"]),
        ("home_bbs", ["enter your home", "home bbs", "homebbs",
                      "enter home", "enter your home bbs"]),
    ]

    # BPQ user-database field commands. Bare value works for the name
    # prompt; the others must be sent as `<CMD> <value>` because BPQ treats
    # those field-names as dual-purpose commands (bare = query, with arg = set).
    REGISTRATION_WIRE_PREFIX = {
        "name":     "",
        "qth":      "QTH ",
        "zip":      "ZIP ",
        "home_bbs": "HOME ",
    }

    def _registration_field(self, text: str):
        """Return the field name if text contains a registration prompt, else None."""
        t = text.lower()
        for field, keywords in self.REGISTRATION_PROMPTS:
            if any(kw in t for kw in keywords):
                return field
        return None

    def _handle_registration(self, banner: str):
        """
        Detect and handle LinBPQ new user registration prompts.

        LinBPQ asks for Name, QTH, Zip, and/or Home BBS sequentially on
        first connect with an unknown callsign. Uses a while-loop to keep
        responding to prompts until the normal BBS prompt is received.
        Auto-fills values from user_info (My Station settings).

        Returns (handled, last_response). last_response is the most recent
        BBS response — either the input banner (if no registration ran) or
        the response after the final field was submitted (already includes
        the trailing BBS prompt, so callers do NOT need another _expect).

        Distinguishing real prompts from informational hints:
        A genuine registration prompt is the LAST thing in the banner —
        the BBS is waiting for input. It may terminate with ':' (e.g.
        'Enter your name:') or with '>' (e.g. 'Please enter your Name >'
        as seen on another BBS running BPQ 6.0.25.16).
        Informational hints like 'You may also enter your QTH using qth
        commands.' are shown to established users who are already at the
        main BBS prompt — banner ends with 'de XXX>'. We bail early in
        that case to avoid resubmitting QTH every login.
        """
        # Only bail when the banner ends with the main BBS prompt 'de XXX>'.
        # A bare trailing '>' is NOT enough — new-user prompts like
        # 'Please enter your Name >' also end with '>' and must be handled.
        if re.search(r'de\s+\S+>\s*$', banner, re.IGNORECASE):
            return False, banner

        handled = False
        current = banner

        while True:
            field = self._registration_field(current)
            if not field:
                break  # no more registration prompts — we're done

            value = self.user_info.get(field, "").strip()
            prefix = self.REGISTRATION_WIRE_PREFIX.get(field, "")
            wire = f"{prefix}{value}" if value else ""
            if value:
                self._log("SYS",
                    f"New user registration: auto-sending {field} = {value!r}")
            else:
                self._log("SYS",
                    f"New user registration: {field} blank in My Station "
                    f"— sending empty response")

            self._send(wire)
            self.new_user = True
            handled = True

            # Wait for next prompt or final BBS prompt
            current = self._expect(self.PROMPT_BBS, timeout=15)

        return handled, current

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

        # Always LM — never L N, and never L <watermark>-.
        # LM is bounded by YOUR mailbox; the others are bounded by total
        # BBS traffic, which on a busy node means hundreds or thousands of
        # header lines before a single byte of your mail moves.
        #
        # We page all the way through LM (up to MAX_MAIL_PAGES): missing a
        # personal message because it fell off the back of page 1 is worse
        # than the airtime, and a personal mailbox is small to begin with.
        self._send("lm")
        raw, state = self._expect_paged(self.PROMPT_BBS, timeout=120,
                                        max_pages=self.MAX_MAIL_PAGES)
        if state == "paged":
            self.end_listing()
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
            f"LM listed {summary.total_messages} message(s) — "
            f"{len(summary.new_personal)} personal "
            f"({'PN only' if new_only else 'PN+PY'}) matched. "
            f"What actually downloads is the caller's decision.")
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
        # Paged read: with OP set, a long bulletin stops every n lines and
        # waits. We always send CR here — never truncate a message the user
        # actually asked for. This also works when the BBS is currently
        # parked at a listing's page prompt: `r N` is a legal answer to
        # that menu and ends the listing as a side effect.
        raw, _state = self._expect_paged(END_MARKER, timeout=timeout,
                                         max_pages=self.MAX_PAGES * 8)
        # User bailed mid-read (e.g. poor conditions). read_until returned
        # early because the abort flag is set; stop here and let
        # download_messages send 'A' to the BBS and resync to the prompt.
        if getattr(self.transport, "abort_requested", lambda: False)():
            raise DownloadAborted()
        # Now drain to whatever prompt actually follows the body.
        #
        # It is NOT always the command prompt. When `r <msg#>` was typed
        # as the answer to a listing's page menu, the read ends that
        # listing's output but BPQ returns to the SAME page menu, not to
        # "de CALL>". Expecting the command prompt alone burned the full
        # 30s timeout and then returned anyway, so the caller believed it
        # was at the command prompt when it was still parked — and the
        # `b` that followed was rejected by the menu, leaving the BBS-side
        # session hung. Watch for both, PAGE_MARKERS first: the page menu
        # also ends in '>' and read_until_any takes the earliest match.
        tail, matched = self.transport.read_until_any(
            self.PAGE_MARKERS + [self.PROMPT_BBS],
            timeout=self._reply_wait(30))
        if not getattr(self.transport, "_terminal_mode", False):
            self._log("RX", tail)
        if matched is None:
            # Nothing recognisable before the BBS went quiet. Assume
            # parked — resyncing costs one 'A' we may not need, while
            # guessing the other way hangs the BBS until the sysop clears
            # the session.
            self._log("SYS",
                "Warning: no prompt after read — assuming page prompt")
        self.at_page_prompt = (matched is None
                               or matched in self.PAGE_MARKERS)
        if matched in self.PAGE_MARKERS:
            if self._finish_page_prompt(matched):
                # The page ran out on the body's last line, so the BBS is
                # at a READ pause, not back at the listing menu — the next
                # `r <msg#>` would be typed into a prompt that only takes
                # A or CR. The body is already complete ([End of Message
                # arrived), so 'A' loses nothing; it returns the BBS to the
                # listing menu or the command prompt, whichever the `r`
                # was typed at.
                self._log("SYS",
                    "Read ended on a page pause — sending 'A' to close it")
                self._abort_paged_output(max_menus=1)
        elif self.at_page_prompt:
            self.transport.read_all_pending(settle_time=0.3)
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
            r'\s+Bid:\s*\S+'             # Bid: 259_N0CALL
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

        # LinBPQ follows its header with a "Body: <n>" line — the byte count
        # it has stored, not anything the sender typed. It is on every
        # message and it is not part of the message (Bill, 2026-09-23).
        #
        # The R: routing lines that follow it are deliberately KEPT: plenty
        # of operators read them to see the path a message took.
        body = re.sub(r'\A[ \t]*Body:[ \t]*\d+[ \t]*\r?\n', '', body,
                      count=1).lstrip("\r\n")

        # size_hint is what the LM/L> listing advertised. It is NOT the same
        # quantity as what arrives on the wire: on a bulletin it tracks the
        # body (7206 listed vs 7212 body), but on a short personal it exceeds
        # even header+body (291 listed vs 213 received) because BPQ counts the
        # message as stored — CRLF pairs and full R: routing lines included.
        # No arithmetic reconciles the two, so log both and claim nothing.
        # Real truncation is caught by END_MARKER, not by a character count.
        self._log("SYS",
            f"Downloaded msg #{msg_number} — {len(body)} chars of body "
            f"(BBS listed {size_hint})")
        return body

    def download_messages(self, messages: List[BBSMessage]) \
            -> List[BBSMessage]:
        """Download a list of messages. Returns list with .body filled.
        Messages downloaded before a user abort keep their bodies; the rest
        are left undownloaded and the session is resynced to the BBS prompt."""
        total = len(messages)
        # Clear any stale abort flag so a previous bail can't kill this run.
        if hasattr(self.transport, "clear_abort"):
            self.transport.clear_abort()
        # Pause data monitor for entire download sequence —
        # prevents frame-split data from being double-displayed
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(False)
        # Single flush before we start — clears any stale bytes
        self.transport.flush_input()
        try:
            for i, msg in enumerate(messages, 1):
                # Honor an abort requested between messages, too.
                if getattr(self.transport, "abort_requested",
                           lambda: False)():
                    raise DownloadAborted()
                self._log("SYS",
                    f"Downloading message {i} of {total} "
                    f"(#{msg.msg_number}, ~{msg.size} bytes)")
                msg.body = self.download_message(msg.msg_number,
                                                 size_hint=msg.size)
                msg.downloaded = True
                # Brief pause between messages — let BBS settle
                time.sleep(0.3)
        except DownloadAborted:
            self._abort_to_prompt()
        finally:
            # The last read may have answered a listing's page menu, which
            # leaves BPQ back at that menu. Close it so the caller gets a
            # session at the command prompt. No-op otherwise.
            self.resync_to_prompt("downloads finished")
            # Always re-enable terminal mode
            if hasattr(self.transport, "set_terminal_mode"):
                self.transport.set_terminal_mode(True)
        return messages

    def _abort_to_prompt(self):
        """Clean user-abort path: the blocking read was already interrupted,
        so tell the BBS to stop sending ('A' — abort paged output, per the
        LinBPQ/BPQ32 command set) and resync to the command prompt. The RF
        session stays connected so the user can disconnect cleanly (CW ID)
        or carry on. Note: 'A' aborts paged output; if the node isn't paging,
        the BBS finishes the current item before the prompt returns."""
        if hasattr(self.transport, "clear_abort"):
            self.transport.clear_abort()   # so the resync read isn't aborted
        self._log("SYS", "Download aborted by user — sending 'A' to the BBS")
        try:
            self._send("A")
            self._expect(self.PROMPT_BBS, timeout=30)
            self.at_page_prompt = False
            self._log("SYS", "Back at the BBS command prompt")
        except Exception as e:
            self._log("SYS", f"Abort resync issue (still connected): {e}")

    def resync_to_prompt(self, reason: str = "") -> bool:
        """Guarantee the BBS is at its command prompt, not a page menu.

        A no-op when we are already at the command prompt, so it is cheap
        to call on any path about to send a real command — and it must be
        called before `b`, because BPQ does not accept `b` at a page menu.
        When the session IS parked, this sends 'A' (abort paged output)
        and drains to the prompt, which costs one short round trip and
        only ever happens when a listing was genuinely left open.

        Returns True if the BBS is at (or was resynced to) the prompt.
        """
        if not self.at_page_prompt:
            return True
        why = f" ({reason})" if reason else ""
        self._log("SYS",
            f"BBS parked at a page prompt — sending 'A' to resync{why}")
        try:
            if self._abort_paged_output():
                self._log("SYS", "Back at the BBS command prompt")
                return True
            self._log("SYS",
                "Warning: BBS not back at its command prompt after 'A'")
            return False
        except Exception as e:
            # Never let a resync failure block the disconnect that follows.
            self._log("SYS", f"Resync issue (still connected): {e}")
            self.at_page_prompt = False
            return False

    def _abort_paged_output(self, max_menus: int = 3) -> bool:
        """Answer page prompts with 'A' until the BBS reaches its command
        prompt, one reply at a time. Returns True once PROMPT_BBS is seen;
        at_page_prompt is left saying where the BBS actually is.

        One 'A' is not always enough. Aborting a read pause drops the BBS
        back to the listing menu the read started from, and that menu
        wants its own 'A': "Output aborted", then "<A>bort, <R Msg(s)>,
        <CR> = Continue..>" (2026-09-11). Taking any '>' in that reply for
        the command prompt sent `b` into the menu, where BPQ refuses it,
        and the link was dropped with the BBS still parked.

        max_menus=1 sends a single 'A' — used after a read that ended on a
        pause, where landing back at the listing menu is the goal.
        """
        for _ in range(max_menus):
            self._send("A")
            raw, matched = self.transport.read_until_any(
                self.PAGE_MARKERS + [self.PROMPT_BBS],
                timeout=self._reply_wait(30))
            if not getattr(self.transport, "_terminal_mode", False):
                self._log("RX", raw)
            if matched == self.PROMPT_BBS:
                self.at_page_prompt = False
                return True
            self.at_page_prompt = True
            if matched is None:
                return False       # went quiet — assume still parked
            self._finish_page_prompt(matched)
        return False

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

        # 3. Send body lines then /EX — log TX lines for terminal display.
        # wrap_body() enforces the 72-column packet convention; see its
        # docstring. It never splits a word and leaves already-short lines
        # byte-for-byte alone.
        lines = wrap_body(body)
        from transport import AGWTransport
        if isinstance(self.transport, AGWTransport):
            # Packet: hand the modem the whole body as ONE block. Direwolf
            # cuts each block it is given into PACLEN frames and never tops
            # up a short last frame from the next block, so line-by-line
            # sending put a 1-8 byte tail frame on the air after nearly
            # every 72-column line, and a 1-byte frame for every blank line
            # — each costing a whole frame + RR turnaround (~4.6 s at 300
            # baud, MAXFRAME 1). 2535 B took 71 frames and 5 m 25 s line by
            # line (2026-09-12); as one block it is ~40. LinBPQ still sees
            # the same CR-terminated lines.
            for line in lines:
                self._log("TX", line)
            self._log("TX", "/EX")
            self.transport.send("\r".join(lines + ["/EX"]))
        else:
            for line in lines:
                self._log("TX", line)
                self.transport.send(line)
                time.sleep(0.1)
            self._log("TX", "/EX")
            self.transport.send("/EX")

        # On a slow packet link most of the body is still queued in the
        # modem at this point — the BBS hasn't seen /EX, so it can't have
        # answered yet. Wait until the modem reports it all acknowledged,
        # or the reply wait below times out on our OWN transmission.
        wait_sent = getattr(self.transport, "wait_until_sent", None)
        if wait_sent:
            wait_sent(why="(message going out)")

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

        # Re-enable streaming — duplicate frame suppression handled in transport
        # via the recent-lines dedup window in _data_reader
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
        Returns list of (category, count) tuples e.g.
        [('ALL', 3), ('BDN', 2), ('EWN', 2)].
        LinBPQ LC output looks like: "ALL    3  BDN    2  EWN    2".
        """
        self._send("lc")
        raw, state = self._expect_paged(self.PROMPT_BBS, timeout=30,
                                        max_pages=6)
        if state == "paged":
            self.end_listing()
        self._log("SYS", f"LC response: {raw.strip()!r}")
        pairs = re.findall(r'\b([A-Z][A-Z0-9]+)\s+(\d+)\b', raw)
        skip = {"BBS", "DE", "OK", "TNX", "NIL", "NO", "YES"}
        return [(cat, int(n)) for cat, n in pairs if cat not in skip]

    def list_category(self, category: str, max_pages: int = 1):
        """
        Send `L> CATEGORY` and read up to `max_pages` of the listing.

        Returns (messages, parked).

        `parked` is True when the BBS stopped at a page prompt and is
        still waiting on us. A caller that wants something out of the
        listing should send `R <msg#>` immediately — the page menu
        accepts it, and it ends the listing and starts the read in one
        command instead of costing an extra 'A' round trip. A caller that
        wants nothing must call end_listing() to let the BBS go.

        With `OP 20` set, one page is the 20 newest bulletins in the
        category — about three weeks of a daily bulletin. That is the
        bound that keeps a long absence from flooding the frequency.
        """
        cat = category.upper().strip()
        self._send(f"l> {cat}")

        def on_page(page_no, _text):
            return None if page_no >= max_pages else ""

        raw, state = self._expect_paged(self.PROMPT_BBS, timeout=60,
                                        max_pages=max_pages + 1,
                                        on_page=on_page)
        messages = parse_message_list(raw)
        self._log("SYS",
            f"L> {cat}: {len(messages)} listed"
            f"{' (parked at page prompt)' if state == 'paged' else ''}")
        return messages, (state == "paged")

    def check_bulletins(self, subscriptions: list, have_cb=None,
                        max_pages: int = 1) -> dict:
        """
        Sweep each subscribed category with `L> CATEGORY` and return
        {category: [BBSMessage, ...]} of bulletins we do not already hold.

        Status flags are deliberately ignored. N / Y / $ / F / D are set
        by the BBS's own forwarding and housekeeping — they describe what
        the BBS has done with a bulletin, not what THIS user has read. The
        only question that matters is have_cb(msg): is it already in our
        database or tombstoned? If not, we want it.

        have_cb(msg) -> True if we already have it. Omitted means "we have
        nothing", which is what a bare terminal caller wants.

        This entry point always closes the listing (sends 'A' when parked)
        because its caller shows a selection dialog before downloading —
        we must not hold the BBS at a page prompt while a modal window
        waits on the user. The connect-flow sweep in main_window uses
        list_category() directly so it can send `R <msg#>` from the page
        prompt and skip that round trip.
        """
        results = {}
        for cat in subscriptions:
            cat = cat.upper().strip()
            if not cat:
                continue
            messages, parked = self.list_category(cat, max_pages=max_pages)
            if parked:
                self.end_listing("caller shows a dialog before reading")
            wanted = [m for m in messages
                      if m.msg_type == "B"
                      and not (have_cb(m) if have_cb else False)]
            if wanted:
                results[cat] = wanted
                self._log("SYS",
                    f"Bulletins: {len(wanted)} wanted in {cat}")
            else:
                self._log("SYS", f"Bulletins: nothing new in {cat}")
        return results

    # ── Retained for a future Advanced mode ───────────────────────
    # list_last() and list_since() are the old watermark scheme: a single
    # BBS-WIDE listing (`LL n`, then `L <watermark>-` on return visits).
    # Nothing calls them any more — on a busy node `L <watermark>-`
    # after a couple of weeks away means hundreds or thousands of header
    # lines before any of your own mail moves, which is exactly what the
    # LM / LC / L> scheme replaced.
    #
    # They stay because a planned Advanced / Simple selector in Settings
    # would let an experienced operator opt back into the full BBS-wide
    # sweep — useful on a quiet node, or when you deliberately want to see
    # everything. The bbs_watermarks table is still written on every
    # session so that switch would work immediately, with no catch-up
    # connect needed. See CLAUDE.md → ARCHITECTURE NOTES.

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
                # Never send 'b' into a page menu — BPQ rejects it there,
                # re-issues the menu, and the BBS-side session is left
                # open when we drop the socket a moment later. That is
                # what wedged the BBS for 20 hours on 2026-09-03.
                self.resync_to_prompt("about to disconnect")
                self._send("b")
            time.sleep(0.5)
        except Exception:
            pass
        self.transport.disconnect()
        self._log("SYS", "Disconnected")

    # Conservative effective throughput, bytes per second, used only to
    # size how long a post-abort backlog might take to clear. 300 baud
    # AX.25 with PACLEN 64 measured ~12 B/s wall-clock including ACKs and
    # retries (2026-09-23). Guessing LOW is the safe direction: it makes
    # the drain wait longer, and the drain stops the moment the prompt
    # arrives anyway.
    DRAIN_BYTES_PER_SEC = 10.0
    DRAIN_MAX_SECS      = 900.0    # 15 minutes, absolute stop

    # The post-YAPP read gives up after this many silence windows in
    # total, even if bytes keep coming.
    POST_YAPP_CAP_WINDOWS = 3

    def _read_post_yapp_tail(self, silence: float = None) -> str:
        """Read up to the BBS prompt after a completed YAPP transfer.

        Stops on '>', on a silence window, or on an ABSOLUTE cap —
        whichever comes first. read_until() alone has no cap: its clock
        restarts on every byte, so a BBS that keeps sending anything at
        all holds the worker thread forever. That is exactly what F2 did
        on 2026-09-23 — LinBPQ re-sent the size=0 sentinel HD over and
        over, no '>' ever came, and every command after it was queued
        and never ran.

        Anything that arrives here is read and dropped, never answered.
        Once LinBPQ has our NR it is back at its command line, and
        whatever we send is typed there as a command (see _begin_cancel).
        """
        if silence is None:
            silence = self._reply_wait(20)
        hard_end = time.time() + silence * self.POST_YAPP_CAP_WINDOWS
        last_rx = time.time()
        buf = bytearray()
        while b">" not in buf:
            now = time.time()
            if now >= hard_end:
                self._log("SYS",
                          f"YAPP: no BBS prompt within "
                          f"{silence * self.POST_YAPP_CAP_WINDOWS:.0f} s of "
                          f"the transfer — carrying on "
                          f"({len(buf)} stray bytes dropped)")
                break
            if now - last_rx >= silence:
                break
            chunk = self.transport.read_raw_bytes(64, timeout=1.0)
            if chunk:
                buf.extend(chunk)
                last_rx = time.time()
        codec = getattr(self.transport, "_text_codec", "utf-8")
        return bytes(buf).decode(codec, errors="replace")

    def _drain_after_abort(self, outstanding: int):
        """Swallow whatever the BBS still has queued after a cancelled
        YAPP transfer, and hand back only when it reaches its prompt.

        This exists because draining on SILENCE does not work on a slow
        link. LinBPQ answers CN by stopping the YAPP state machine, but
        everything it has already pushed into the AX.25 stream still has
        to come out — about 5 KB, or four and a half minutes at 300 baud,
        in the run that found this (J7, 2026-09-23). YappReceiver's
        two-second quiet window is an ordinary inter-frame gap at that
        speed, so QtC called the wire idle, went back to terminal mode,
        and the rest of the file arrived as text on the operator's
        screen with the YAPP framing bytes still in it.

        The prompt is the only real "I have finished" signal. Terminal
        mode stays OFF for the whole wait so none of the backlog is shown
        as if it were something the user asked for.
        """
        # The user's Abort press is still latched on the transport, and
        # read_until_any() returns instantly with nothing while it is set
        # — which made the first version of this drain give up in zero
        # seconds and log "wire went quiet after 0 bytes" while 5 KB was
        # still on its way (2026-09-23, J7 re-run). _abort_to_prompt()
        # clears it for the mail path for exactly this reason.
        if hasattr(self.transport, "clear_abort"):
            self.transport.clear_abort()

        floor  = max(90.0, self._reply_wait(30) * 2.5)
        budget = min(self.DRAIN_MAX_SECS,
                     max(floor, outstanding / self.DRAIN_BYTES_PER_SEC * 1.5))
        if outstanding:
            self._log("SYS",
                f"Transfer cancelled — the BBS still has about {outstanding} "
                f"bytes queued. Waiting for it to finish sending before the "
                f"session is usable (up to {int(budget // 60)} min "
                f"{int(budget % 60)} s). Nothing more is being requested.")
        else:
            self._log("SYS",
                "Transfer cancelled — waiting for the BBS to come back to "
                "its prompt.")

        deadline = time.time() + budget
        swallowed = 0
        while time.time() < deadline:
            chunk, matched = self.transport.read_until_any(
                [self.PROMPT_BBS], timeout=self._reply_wait(30))
            swallowed += len(chunk or "")
            if matched:
                self._log("SYS",
                    f"Back at the BBS command prompt — discarded "
                    f"{swallowed} bytes of cancelled transfer.")
                self.at_page_prompt = False
                return True
            if not chunk:
                # Genuinely quiet for a full reply-wait AND no prompt.
                # Nothing more is coming; stop waiting.
                self._log("SYS",
                    f"Wire went quiet with no prompt after {swallowed} "
                    f"bytes — assuming the BBS is done.")
                return False
            self._log("SYS",
                f"Still draining the cancelled transfer — {swallowed} bytes "
                f"discarded so far.")
        self._log("SYS",
            f"Gave up draining after {int(budget)} s and {swallowed} bytes. "
            f"The session may still have BBS output queued behind it.")
        return False

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

        # A bail-out from an earlier transfer must not kill this one before
        # it starts — same guard download_messages() uses.
        if hasattr(self.transport, "clear_abort"):
            self.transport.clear_abort()

        # Flush stale data BEFORE stopping the monitor, then stop the monitor.
        # Order matters: flush_input() must run while the monitor is still live
        # so it drains the socket cleanly; stopping the monitor afterwards
        # guarantees the YAPP frame bytes won't be consumed by the monitor thread
        # after we send the command.
        self.transport.flush_input()
        if hasattr(self.transport, "set_terminal_mode"):
            self.transport.set_terminal_mode(False)

        aborted  = False
        receiver = None
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

        except DownloadAborted:
            aborted = True
            raise

        finally:
            # After YAPP, LinBPQ sends "de N0CALL>" before returning to the
            # BBS prompt. Read until the prompt, then re-emit whatever we
            # captured as an [RX] line so the terminal view shows the prompt
            # the same way it does for any other command — without this the
            # user sees the download succeed but no prompt afterwards and
            # can't tell whether the BBS is hung or idle.
            if aborted:
                # Cancelled: the BBS may still have kilobytes queued.
                # Wait for its prompt with terminal mode still OFF, so the
                # backlog is swallowed instead of printed (J7, 2026-09-23).
                try:
                    self._drain_after_abort(
                        getattr(receiver, "aborted_outstanding", 0))
                except Exception as e:
                    self._log("SYS", f"Drain after abort failed: {e}")
                tail = ""
            elif getattr(receiver, "close_kind", "") == "ET":
                # Clean RFC close. LinBPQ goes straight back to its
                # command line and does NOT repeat "de N0CALL>" — F2 re-run,
                # 2026-09-28: its log shows nothing between `yapp` and our
                # `b`, and waiting for a prompt cost two dead minutes at
                # 300 baud. Swallow any stray bytes briefly, then say so.
                try:
                    tail = self._read_post_yapp_tail(silence=3.0)
                except Exception:
                    tail = ""
                if not re.search(r'de\s+\S+>', tail):
                    self._log("SYS", "YAPP closed cleanly — the BBS is "
                                     "ready for the next command.")
            else:
                try:
                    tail = self._read_post_yapp_tail()
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
