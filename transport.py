# QtC v0.15.0-beta — transport.py  (built 2026-10-04)
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
import socket
import struct
import time
import threading

# PTT is optional — only imported when a PTTController is attached
try:
    from ptt import PTTController
except ImportError:
    PTTController = None

# Codecs offered for decoding BBS content. utf-8 is the default; cp437 and
# cp850 are the DOS "OEM" code pages used by bulletins with 8-bit ASCII
# box-drawing / block graphics. All are single-byte (cp437/cp850) or
# self-synchronizing (utf-8), so per-chunk decoding in the reader is safe.
SUPPORTED_TEXT_CODECS = ("utf-8", "cp437", "cp850")

def _validate_codec(codec: str) -> str:
    """Return a known-good codec name, defaulting to utf-8 for anything
    unrecognized so a typo in config.json can never break the data path."""
    name = (codec or "").strip().lower().replace("_", "-")
    return name if name in SUPPORTED_TEXT_CODECS else "utf-8"


# Characters a BBS prompt can end on with no CR/LF behind it: "Username:",
# "de N0CALL>", "<CR> = Continue..>". They only END A LINE when they are the
# last thing that arrived. Flushing on every one of them chopped ordinary
# sentences into pieces — LinBPQ's node line "MYNODE:N0CALL-7} Invalid
# command - Enter ? for command list" came out as three [RX] lines, and a
# subject line like "Re: tonight's net" split at the colon (Bill,
# 2026-09-20).
_PROMPT_ENDS = (">", ":", "?")


def _stream_rx_lines(text: str, line_buf: str, emit) -> str:
    """Feed newly arrived text through the [RX] line splitter and return
    the new partial-line buffer.

    A line flushes on CR/LF, or on a prompt character that is the last
    non-blank thing in `text` — so a prompt with nothing after it still
    shows the instant it lands, while a line split across two reads (or
    two AX.25 frames) is reassembled instead of being printed in pieces.
    A prompt arriving with trailing blanks ("Username: ") still counts.

    Display only. The receive buffer the protocol code matches against is
    filled separately by the caller and is not touched here, so changing
    what the user sees can never change what QtC reads.
    """
    for i, ch in enumerate(text):
        if ch in ("\r", "\n"):
            emit(line_buf.strip())
            line_buf = ""
        elif ch in _PROMPT_ENDS and not text[i + 1:].strip():
            emit((line_buf + ch).strip())
            line_buf = ""
        else:
            line_buf += ch
    return line_buf


class TelnetTransport:
    """
    Raw TCP transport replacing telnetlib (removed in Python 3.13).
    Handles basic Telnet IAC negotiation so LinBPQ doesn't get confused.
    """

    IAC  = bytes([255])
    DONT = bytes([254])
    DO   = bytes([253])
    WONT = bytes([252])
    WILL = bytes([251])

    def __init__(self, host, port, timeout=30):
        self.host    = host
        self.port    = port
        self.timeout = timeout
        self.sock    = None
        self.connected = False
        self._buf  = b""
        self._lock = threading.Lock()      # protects self._buf
        # _terminal_mode controls whether the reader emits [RX] lines.
        # Reader thread itself is always running while the socket is open
        # (single-reader pattern — see _reader() docstring).
        self._terminal_mode   = False
        self._stop_reader     = threading.Event()
        self._reader_thread   = None
        self._log = None   # set by SessionWorker to emit [RX] log lines
        # Codec used to decode BBS content (terminal view + message bodies).
        # Default UTF-8; can be switched to cp437/cp850 so bulletins built
        # with DOS 8-bit ASCII box-drawing graphics render correctly.
        self._text_codec = "utf-8"
        # User-abort flag. Set from the GUI thread to break a blocking
        # read_until() immediately (e.g. bailing on a slow download) instead
        # of waiting out the multi-minute message timeout.
        self._abort = threading.Event()

    def set_text_codec(self, codec: str):
        """Select the codec for decoding BBS content. Falls back to utf-8
        if the name is unknown so a bad config value can never crash I/O."""
        self._text_codec = _validate_codec(codec)

    def request_abort(self):
        """Break any in-progress read_until() right away (thread-safe)."""
        self._abort.set()

    def clear_abort(self):
        self._abort.clear()

    def abort_requested(self) -> bool:
        return self._abort.is_set()

    def set_terminal_mode(self, enabled: bool):
        """Toggle [RX] line streaming. Reader thread runs either way."""
        self._terminal_mode = enabled

    def _reader(self):
        """
        Single-reader thread — the ONLY caller of recv() on self.sock.

        Buffers every incoming byte into self._buf under self._lock so
        read_until() / read_raw_bytes() can consume it without ever
        touching the socket directly. Eliminates the race where a
        background streamer and a foreground _expect() both call recv()
        on the same socket and end up with fragmented or duplicated lines.

        When _terminal_mode is True, also emits complete lines as [RX]
        log entries, via _stream_rx_lines: CR/LF, or a prompt character
        that is the last thing in the chunk. Never on socket timeout, so
        a BBS line split across two recv() calls is reassembled, not
        printed in pieces.
        """
        line_buf = ""

        def _emit_line(s: str):
            if s and self._log:
                self._log("RX", s)

        while not self._stop_reader.is_set():
            if not self.connected or self.sock is None:
                time.sleep(0.1)
                continue
            chunk = self._recv_chunk(timeout=0.5)
            if not chunk:
                continue
            with self._lock:
                self._buf += chunk
            if not self._terminal_mode:
                line_buf = ""
                continue
            text = chunk.decode(self._text_codec, errors="replace")
            line_buf = _stream_rx_lines(text, line_buf, _emit_line)

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect((self.host, self.port))
            self.connected = True
        except (ConnectionRefusedError, OSError) as e:
            raise ConnectionError(
                f"Could not connect to {self.host}:{self.port} — {e}")
        # Start the single reader — owns recv() for the rest of the session
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="telnet-reader")
        self._reader_thread.start()

    def disconnect(self):
        self._stop_reader.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.connected = False

    def send(self, text):
        if not self.connected:
            raise ConnectionError("Not connected")
        if isinstance(text, str):
            text = (text + "\r\n").encode("utf-8", errors="replace")
        self.sock.sendall(text)

    def _recv_chunk(self, timeout=None):
        """Read a chunk from the socket, stripping Telnet IAC sequences."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            raw = self.sock.recv(4096)
        except socket.timeout:
            return b""
        except OSError:
            self.connected = False
            return b""

        if not raw:
            self.connected = False
            return b""

        # Strip and respond to Telnet IAC negotiation
        # LinBPQ may send DO/WILL options on connect — we refuse them all
        cleaned = b""
        i = 0
        while i < len(raw):
            if raw[i:i+1] == self.IAC and i + 2 < len(raw):
                cmd  = raw[i+1:i+2]
                opt  = raw[i+2:i+3]
                # Reply: DONT to DO requests, WONT to WILL requests
                if cmd == self.DO:
                    self.sock.sendall(self.IAC + self.WONT + opt)
                elif cmd == self.WILL:
                    self.sock.sendall(self.IAC + self.DONT + opt)
                i += 3
            else:
                cleaned += raw[i:i+1]
                i += 1
        return cleaned

    def flush_input(self):
        """Discard any unread bytes in the buffer.

        Under single-reader, the reader thread is the only one that calls
        recv() — flush_input just empties the shared buffer. Any bytes
        still en route from the wire will be picked up by the reader
        and end up in the next read_until() result.
        """
        with self._lock:
            self._buf = b""

    def peek_input(self) -> str:
        """Everything buffered and not yet read, without consuming it."""
        with self._lock:
            return self._buf.decode(self._text_codec, errors="replace")

    def read_until(self, expected: str, timeout: int = 15) -> str:
        """Read from _buf until expected string appears.

        Consumes from _buf only — never calls recv() (the reader does).
        """
        if isinstance(expected, str):
            expected_b = expected.encode("utf-8")
        else:
            expected_b = expected

        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._lock:
                # User abort — return whatever's buffered now. The caller
                # checks abort_requested() and stops; the flag stays set
                # until the abort path resyncs.
                if self._abort.is_set():
                    result = self._buf
                    self._buf = b""
                    return result.decode(self._text_codec, errors="replace")
                if len(self._buf) != seen:
                    # Silence-based timeout — see read_until_any()
                    seen = len(self._buf)
                    deadline = time.time() + timeout
                idx = self._buf.lower().find(expected_b.lower())
                if idx >= 0:
                    end = idx + len(expected_b)
                    result = self._buf[:end]
                    self._buf = self._buf[end:]
                    return result.decode(self._text_codec, errors="replace")
                if time.time() >= deadline:
                    result = self._buf
                    self._buf = b""
                    return result.decode(self._text_codec, errors="replace")
            time.sleep(0.05)

    def read_until_any(self, expected, timeout: int = 15):
        """Read until ANY string in `expected` appears.

        Returns (text, matched) where `matched` is the entry from
        `expected` that fired, or None on timeout/abort. Same
        consume-from-buffer contract as read_until() — never calls
        recv(); the reader thread owns the socket.

        Used for BBS paged output: with `OP n` set, LinBPQ interrupts a
        listing with "<A>bort, <R Msg(s)>, <CR> = Continue..>" instead of
        returning to the command prompt, so a caller has to watch for both
        terminators at once. Order matters — the first entry that matches
        at the EARLIEST buffer position wins, so pass the page prompt
        before the BBS prompt (the page prompt also ends in '>').
        """
        if isinstance(expected, str):
            expected = [expected]
        needles = [(e, e.encode("utf-8") if isinstance(e, str) else e)
                   for e in expected]

        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._lock:
                if self._abort.is_set():
                    result = self._buf
                    self._buf = b""
                    return (result.decode(self._text_codec,
                                          errors="replace"), None)
                if len(self._buf) != seen:
                    # Time out on SILENCE, not total time: the deadline
                    # moves on while bytes keep arriving. Half-duplex RF
                    # can take minutes over one reply — never jump ahead.
                    seen = len(self._buf)
                    deadline = time.time() + timeout
                low = self._buf.lower()
                best_idx, best = -1, None
                for entry, needle in needles:
                    i = low.find(needle.lower())
                    if i >= 0 and (best_idx < 0 or i < best_idx):
                        best_idx, best = i, (entry, needle)
                if best is not None:
                    end = best_idx + len(best[1])
                    result = self._buf[:end]
                    self._buf = self._buf[end:]
                    return (result.decode(self._text_codec,
                                          errors="replace"), best[0])
                if time.time() >= deadline:
                    result = self._buf
                    self._buf = b""
                    return (result.decode(self._text_codec,
                                          errors="replace"), None)
            time.sleep(0.05)

    def read_eager(self) -> str:
        """Return and drain whatever is immediately in the buffer."""
        with self._lock:
            result = self._buf
            self._buf = b""
            return result.decode(self._text_codec, errors="replace")

    def read_all_pending(self, settle_time: float = 0.5) -> str:
        """Read all pending data, waiting briefly for the reader to drain new bytes."""
        time.sleep(settle_time)
        result = ""
        chunk = self.read_eager()
        while chunk:
            result += chunk
            time.sleep(0.2)
            chunk = self.read_eager()
        return result

    def read_raw_bytes(self, n: int, timeout: float = 10.0) -> bytes:
        """
        Read exactly n raw bytes from _buf (populated by the reader).
        Used by YappReceiver for binary frame reading.
        Returns fewer than n bytes only if timeout expires.
        """
        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._lock:
                if len(self._buf) != seen:
                    # Silence-based timeout — see read_until_any()
                    seen = len(self._buf)
                    deadline = time.time() + timeout
                if len(self._buf) >= n:
                    result = self._buf[:n]
                    self._buf = self._buf[n:]
                    return result
                if time.time() >= deadline:
                    result = self._buf
                    self._buf = b""
                    return result
            time.sleep(0.02)

    def send_raw(self, data: bytes):
        """Send raw bytes without appending \\r\\n. Used for YAPP ACK/NAK bytes."""
        if not self.connected:
            raise ConnectionError("Not connected")
        self.sock.sendall(data)


class VaraTransport:
    """
    VARA HF/FM transport.

    VARA exposes two TCP ports:
      cmd_port  (default 8300) — ASCII command/response channel
      data_port (default 8301) — raw data stream (what goes over RF)

    Connect sequence:
      1. Open both sockets
      2. Send MYCALL <mycall> on cmd port
      3. Send CONNECT <mycall> <target_call> on cmd port
      4. Poll cmd port for CONNECTED response
      5. Once CONNECTED, data port is live — BBSSession uses it like Telnet

    Disconnect sequence:
      1. Send DISCONNECT on cmd port (waits for TX buffer empty)
         or ABORT for immediate dirty disconnect
      2. Wait for DISCONNECTED on cmd port
      3. Close both sockets

    The cmd port is monitored in a background thread so we don't miss
    DISCONNECTED or BUSY notifications while BBSSession is reading data.
    """

    # VARA command responses we care about
    RESP_CONNECTED    = "CONNECTED"
    RESP_DISCONNECTED = "DISCONNECTED"
    RESP_BUSY_ON      = "BUSY ON"
    RESP_BUSY_OFF     = "BUSY OFF"
    RESP_BUFFER       = "BUFFER"
    RESP_OK           = "OK"
    RESP_WRONG        = "WRONG"

    def __init__(self, vara_host, cmd_port, data_port, mycall, target_call,
                 timeout=60, bandwidth="2300", vara_type="hf"):
        self.vara_host   = vara_host
        self.cmd_port    = cmd_port
        self.data_port   = data_port
        self.mycall      = mycall.upper()
        self.target_call = target_call.upper()
        self.timeout     = timeout
        # HF: "500" / "2300" (sent on the wire as BW500 / BW2300).
        # FM: "NARROW" / "WIDE" (sent as bare keyword — no BW prefix).
        self.bandwidth   = str(bandwidth)
        self.vara_type   = (vara_type or "hf").lower()

        self._cmd_sock  = None
        self._data_sock = None

        self.connected  = False          # True once VARA says CONNECTED
        self._lost_reason = None         # why VARA ended the link, if it did
        self._busy      = False          # Channel busy flag
        self._buffer    = 0             # Bytes in VARA TX queue
        self._last_cmd_resp = ""        # Last raw response from cmd port

        self._cmd_buf   = b""           # Unprocessed bytes from cmd port
        self._data_buf  = b""           # Unprocessed bytes from data port

        self._cmd_lock  = threading.Lock()
        self._data_lock = threading.Lock()  # protects _data_buf
        self._stop_evt  = threading.Event()
        self._cmd_thread = None
        self._data_thread = None
        # _terminal_mode controls whether the data reader emits [RX] lines
        # for the terminal view. The reader thread runs the whole time the
        # data socket is open, regardless of this flag — single-reader
        # pattern, the only caller of recv() on the data socket.
        self._terminal_mode = False

        # Callback for log messages — set by _make_session same as Telnet
        self._log = None

        # Optional PTTController — set by _make_session before connect()
        self.ptt: "PTTController | None" = None

        # Optional callback fired when VARA sends DISCONNECTED unexpectedly
        # (BBS or remote timeout). Set by SessionWorker to trigger GUI update.
        self._on_disconnected_cb = None

        # Codec for decoding BBS content on the data port (terminal view +
        # message bodies). The cmd port is always ASCII/utf-8 (VARA's own
        # protocol) and is never affected by this. See _validate_codec.
        self._text_codec = "utf-8"
        # User-abort flag — see TelnetTransport. Set from the GUI thread to
        # break a blocking data-port read_until() immediately.
        self._abort = threading.Event()

    def set_text_codec(self, codec: str):
        """Select the codec for decoding BBS content on the data port.
        Falls back to utf-8 for any unknown name."""
        self._text_codec = _validate_codec(codec)

    def request_abort(self):
        """Break any in-progress read_until() right away (thread-safe)."""
        self._abort.set()

    def clear_abort(self):
        self._abort.clear()

    def abort_requested(self) -> bool:
        return self._abort.is_set()

    # ── Internal helpers ──────────────────────────────────────────

    def _emit(self, direction, text):
        if self._log:
            self._log(direction, text)

    def _send_cmd(self, cmd: str):
        """Send a command to VARA command port."""
        raw = (cmd + "\r\n").encode("utf-8")
        self._emit("TX-CMD", cmd)
        self._cmd_sock.sendall(raw)

    def _read_cmd_line(self, timeout: float = 2.0) -> str:
        """
        Read one CR/LF-terminated line from the command socket.
        Returns empty string on timeout or if socket is gone.
        """
        if self._cmd_sock is None:
            return ""

        deadline = time.time() + timeout
        while True:
            # Check buffer for a complete line
            for sep in (b"\r\n", b"\n", b"\r"):
                if sep in self._cmd_buf:
                    idx  = self._cmd_buf.index(sep)
                    line = self._cmd_buf[:idx].decode("utf-8", errors="replace").strip()
                    self._cmd_buf = self._cmd_buf[idx + len(sep):]
                    if line:
                        return line

            remaining = deadline - time.time()
            if remaining <= 0:
                return ""

            if self._cmd_sock is None:
                return ""

            self._cmd_sock.settimeout(min(remaining, 0.5))
            try:
                chunk = self._cmd_sock.recv(1024)
                if chunk:
                    self._cmd_buf += chunk
                else:
                    # Remote end closed the connection cleanly
                    return ""
            except socket.timeout:
                pass
            except OSError:
                return ""

    def _cmd_monitor(self):
        """
        Background thread — reads VARA command port continuously.
        Updates connected/busy/buffer state and logs responses.
        Called after initial connect handshake is complete.
        Exits cleanly if the socket is closed (remote or local disconnect).
        """
        while not self._stop_evt.is_set():
            if self._cmd_sock is None:
                break
            line = self._read_cmd_line(timeout=1.0)
            if not line:
                continue
            self._emit("RX-CMD", line)
            with self._cmd_lock:
                self._last_cmd_resp = line
                upper = line.upper()
                if upper.startswith(self.RESP_CONNECTED):
                    self.connected = True
                elif upper.startswith(self.RESP_DISCONNECTED):
                    # The monitor only runs while linked — disconnect()
                    # stops it before sending DISCONNECT — so this one is
                    # never ours. Set the reason before connected drops: a
                    # blocked read raises with it the moment it looks.
                    self._lost_reason = (
                        f"VARA reports the link to {self.target_call} is gone")
                    self.connected = False
                    self._emit("SYS", self._lost_reason)
                    # Remote-initiated disconnect (BBS timeout, far-end bye, etc.)
                    # Stop the monitor thread, release PTT, and clean up sockets
                    # so VARA can reset its TCP listener before the next connect.
                    self._stop_evt.set()
                    if self.ptt:
                        self.ptt.rx()
                        try:
                            self.ptt.close()
                        except Exception:
                            pass
                    if self._on_disconnected_cb:
                        self._on_disconnected_cb()
                    self._cleanup()
                    return   # exit the monitor thread cleanly
                elif upper.startswith(self.RESP_BUSY_ON):
                    self._busy = True
                elif upper.startswith(self.RESP_BUSY_OFF):
                    self._busy = False
                elif upper.startswith(self.RESP_BUFFER):
                    try:
                        self._buffer = int(line.split()[1])
                    except (IndexError, ValueError):
                        pass
                elif upper == "PTT ON":
                    if self.ptt:
                        self.ptt.tx()
                elif upper == "PTT OFF":
                    if self.ptt:
                        self.ptt.rx()

    def _data_reader(self):
        """
        Single-reader thread — the ONLY caller of recv() on _data_sock.

        Runs the whole time the socket is open, regardless of terminal_mode.
        Every byte recv()'d goes into self._data_buf under self._data_lock,
        which is what read_until() / read_raw_bytes() / read_eager() /
        flush_input() consume from. Having only one recv() caller is what
        prevents the ghost/fragment/duplicate issues — there is no race
        between background streaming and a foreground _expect().

        When _terminal_mode is True, the reader ALSO emits complete lines
        as [RX] log entries for the terminal view, via _stream_rx_lines:
        CR/LF, or a prompt character with nothing after it. No
        timeout-based partial flush, so a line split across two RF frames
        is reassembled instead of being printed in two pieces.

        Defense in depth (unchanged from prior design):
          * `recent[]` — suppresses identical lines repeated within a short
            window. VARA occasionally double-delivers a line on retransmit.
          * Half-line duplicate check — when a single emit comes through as
            "Enter Title:Enter Title:" the second half is stripped.
        """
        line_buf = ""       # accumulates chars until a terminator
        recent = []         # last N lines logged — suppress duplicates in window

        def _emit_line(s: str):
            """Apply dedup heuristics and emit one [RX] line."""
            if not s or not self._log:
                return
            n = len(s)
            half = n // 2
            if half > 4 and n % 2 == 0 and s[:half] == s[half:]:
                s = s[:half]
            if s not in recent:
                self._log("RX", s)
            recent.append(s)
            if len(recent) > 6:
                recent.pop(0)

        while not self._stop_evt.is_set():
            if self._data_sock is None:
                time.sleep(0.1)
                continue
            try:
                self._data_sock.settimeout(0.5)
                chunk = self._data_sock.recv(4096)
            except socket.timeout:
                chunk = b""
            except OSError:
                break

            if not chunk:
                continue

            # Always buffer the bytes — read_until/read_raw_bytes consume from here
            with self._data_lock:
                self._data_buf += chunk

            # Stream to [RX] only when the user is watching (Terminal/Debug view)
            if not self._terminal_mode:
                # Reset recent[] when we're not streaming so a later switch
                # to terminal mode starts with a clean dedup window.
                if recent:
                    recent.clear()
                line_buf = ""
                continue

            text = chunk.decode(self._text_codec, errors="replace")
            line_buf = _stream_rx_lines(text, line_buf, _emit_line)

    # ── Public interface (mirrors TelnetTransport) ────────────────

    def connect(self):
        """
        Open command + data sockets, handshake with VARA, initiate RF connect.
        Raises ConnectionError if anything fails.
        """
        self._lost_reason = None
        # 1. Open command socket — retry generously since VARA needs time
        #    to reset its TCP listener after a previous disconnect/failed connect.
        #    A failed RF connect can leave VARA resetting for up to ~15 seconds.
        _RETRY_ATTEMPTS = 12
        _RETRY_DELAY    = 3.0   # seconds between attempts (36s total window)
        last_err = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._cmd_sock.settimeout(10)
                self._cmd_sock.connect((self.vara_host, self.cmd_port))
                last_err = None
                break   # connected OK
            except OSError as e:
                last_err = e
                self._cmd_sock.close()
                self._cmd_sock = None
                if attempt < _RETRY_ATTEMPTS:
                    self._emit("SYS",
                        f"VARA not ready (attempt {attempt}/{_RETRY_ATTEMPTS}) — "
                        f"retrying in {int(_RETRY_DELAY)}s…")
                    time.sleep(_RETRY_DELAY)

        if last_err is not None:
            raise ConnectionError(
                f"Cannot reach VARA command port {self.vara_host}:{self.cmd_port} — "
                f"{last_err}\n"
                f"Is VARA HF/FM running?")

        # 2. Open data socket
        try:
            self._data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._data_sock.settimeout(self.timeout)
            self._data_sock.connect((self.vara_host, self.data_port))
        except OSError as e:
            self._cmd_sock.close()
            raise ConnectionError(
                f"Cannot reach VARA data port {self.vara_host}:{self.data_port} — {e}")

        # 3. Drain any startup banner VARA may send
        time.sleep(0.3)
        self._cmd_sock.settimeout(0.5)
        try:
            banner = self._cmd_sock.recv(1024)
            if banner:
                self._cmd_buf += banner
        except socket.timeout:
            pass

        # 4. Set bandwidth, then our callsign.
        # HF takes "BW500" / "BW2300"; FM takes a bare "NARROW" / "WIDE".
        if self.vara_type == "fm":
            self._send_cmd(self.bandwidth.upper())
        else:
            self._send_cmd(f"BW{self.bandwidth}")
        time.sleep(0.1)
        self._send_cmd(f"MYCALL {self.mycall}")
        time.sleep(0.2)

        # 5. Initiate RF connection
        self._send_cmd(f"CONNECT {self.mycall} {self.target_call}")

        # 6. Wait for CONNECTED (or failure) on cmd port
        self._emit("SYS", f"Waiting for VARA connection to {self.target_call}…")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self._read_cmd_line(timeout=2.0)
            if not line:
                continue
            self._emit("RX-CMD", line)
            upper = line.upper()
            if upper.startswith(self.RESP_CONNECTED):
                self.connected = True
                self._emit("SYS", f"VARA connected to {self.target_call}")
                break
            elif upper.startswith(self.RESP_DISCONNECTED):
                # Ensure PTT is released and sockets are closed cleanly so
                # VARA can reset its TCP listener before the next attempt.
                if self.ptt:
                    self.ptt.rx()
                self._cleanup()
                raise ConnectionError(
                    f"VARA could not connect to {self.target_call} — station not responding")
            elif upper.startswith(self.RESP_BUSY_ON):
                self._emit("SYS", "Channel busy — waiting…")
                # keep waiting, VARA will retry
            elif upper == "PTT ON":
                if self.ptt:
                    self.ptt.tx()
            elif upper == "PTT OFF":
                if self.ptt:
                    self.ptt.rx()
        if not self.connected:
            if self.ptt:
                self.ptt.rx()
            self._cleanup()
            raise ConnectionError(
                f"Timed out waiting for VARA connection to {self.target_call}")

        # 7. Start background cmd monitor thread
        self._stop_evt.clear()
        self._cmd_thread = threading.Thread(
            target=self._cmd_monitor, daemon=True, name="vara-cmd-monitor")
        self._cmd_thread.start()
        # Start the single data-port reader. This owns recv() on _data_sock
        # for the entire session — read_until / read_raw_bytes / etc.
        # consume from _data_buf, never from the socket directly.
        self._data_thread = threading.Thread(
            target=self._data_reader, daemon=True, name="vara-data-reader")
        self._data_thread.start()

    def disconnect(self):
        """
        Clean disconnect — sends DISCONNECT, keeps processing PTT ON/OFF
        commands while VARA drains its TX buffer and sends the CW ID,
        then closes sockets once DISCONNECTED is received.
        """
        if not (self._cmd_sock and self.connected):
            self._cleanup()
            return

        # Signal the background monitor to stop — we take over cmd reading
        # here so we can handle PTT during the drain + CW ID
        self._stop_evt.set()

        try:
            self._send_cmd("DISCONNECT")

            # Wait up to 60s for DISCONNECTED — PTT ON/OFF keep firing
            # during buffer drain and CW ID; process them so the radio
            # keys/unkeys correctly throughout
            deadline = time.time() + 60
            while time.time() < deadline:
                line = self._read_cmd_line(timeout=1.0)
                if not line:
                    continue
                self._emit("RX-CMD", line)
                upper = line.upper()
                if upper == "PTT ON":
                    if self.ptt:
                        self.ptt.tx()
                elif upper == "PTT OFF":
                    if self.ptt:
                        self.ptt.rx()
                elif upper.startswith(self.RESP_DISCONNECTED):
                    break

        except OSError:
            pass

        # Final safety — ensure PTT is released
        if self.ptt:
            self.ptt.rx()

        self.connected = False
        self._cleanup()

    def _cleanup(self):
        """Close sockets — called after disconnect or on error."""
        for sock in (self._data_sock, self._cmd_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._cmd_sock  = None
        self._data_sock = None

    def abort(self):
        """Immediate dirty disconnect — use if clean disconnect hangs."""
        self._stop_evt.set()
        if self.ptt:
            self.ptt.rx()
        if self._cmd_sock:
            try:
                self._send_cmd("ABORT")
            except OSError:
                pass
        self.connected = False
        self._cleanup()

    def set_terminal_mode(self, enabled: bool):
        """
        Toggle whether the data reader emits [RX] log lines for terminal view.

        Under the single-reader architecture the reader thread is always
        running and always populates _data_buf — this flag only controls
        whether it also streams complete lines to the log. read_until()
        and friends work the same in either mode.

        flush_input() is NOT called here. Callers (mail check, YAPP, etc.)
        decide when to flush. See bbs_session.py docstrings for the
        per-operation ordering rules.
        """
        self._terminal_mode = enabled

    def send(self, text):
        """Send text over RF via VARA data port."""
        if not self.connected:
            raise ConnectionError("VARA not connected")
        if isinstance(text, str):
            text = (text + "\r\n").encode("utf-8", errors="replace")
        self._data_sock.sendall(text)

    def flush_input(self):
        """Discard any unread bytes in the data buffer.

        Under single-reader, the background reader is the only thread that
        recv()'s from the socket. flush_input simply empties the shared
        buffer — bytes already on the wire will be picked up by the reader
        and end up in the next caller's read_until() result.
        """
        with self._data_lock:
            if self._data_buf:
                self._log("SYS",
                    f"flush_input: discarding {len(self._data_buf)} stale bytes")
            self._data_buf = b""

    def _link_lost(self):
        """Why a blocked read should give up now because the link is gone,
        or None while linked. Same rule as AGWTransport._link_lost.

        Until 2026-10-04 VARA reads ignored DISCONNECTED and ran out their
        silence timer instead. On air that day a VARA FM link failed during
        the login wait: VARA said DISCONNECTED at 16:04:57, but the login
        read kept going for a full minute, sent its 'A' nudge, then popped
        "Login failed" over the next session Bill had already started."""
        if self.connected:
            return None
        return self._lost_reason or f"Not linked to {self.target_call}"

    def peek_input(self) -> str:
        """Everything buffered and not yet read, without consuming it."""
        with self._data_lock:
            return self._data_buf.decode(self._text_codec, errors="replace")

    def read_until(self, expected: str, timeout: int = 30) -> str:
        """
        Read from VARA data port until expected string appears.
        Consumes from _data_buf — never calls recv() (the reader thread does).
        Same interface as TelnetTransport.read_until().
        """
        if isinstance(expected, str):
            expected_b = expected.encode("utf-8")
        else:
            expected_b = expected

        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._data_lock:
                # User abort — return whatever's buffered now. The caller
                # checks abort_requested() and stops; the flag stays set
                # until the abort path resyncs.
                if self._abort.is_set():
                    result = self._data_buf
                    self._data_buf = b""
                    return result.decode(self._text_codec, errors="replace")
                if len(self._data_buf) != seen:
                    # Silence-based timeout — see read_until_any()
                    seen = len(self._data_buf)
                    deadline = time.time() + timeout
                idx = self._data_buf.lower().find(expected_b.lower())
                if idx >= 0:
                    end = idx + len(expected_b)
                    result = self._data_buf[:end]
                    self._data_buf = self._data_buf[end:]
                    return result.decode(self._text_codec, errors="replace")
                lost = self._link_lost()
                if lost:
                    raise ConnectionError(lost)
                if time.time() >= deadline:
                    result = self._data_buf
                    self._data_buf = b""
                    return result.decode(self._text_codec, errors="replace")
            # No match yet — give the reader a moment to add more bytes
            time.sleep(0.05)

    def read_until_any(self, expected, timeout: int = 30):
        """Read until ANY string in `expected` appears.

        Returns (text, matched) where `matched` is the entry from
        `expected` that fired, or None on timeout/abort. Same
        consume-from-buffer contract as read_until() — never calls
        recv(); the reader thread owns the socket.

        Used for BBS paged output: with `OP n` set, LinBPQ interrupts a
        listing with "<A>bort, <R Msg(s)>, <CR> = Continue..>" instead of
        returning to the command prompt, so a caller has to watch for both
        terminators at once. Order matters — the first entry that matches
        at the EARLIEST buffer position wins, so pass the page prompt
        before the BBS prompt (the page prompt also ends in '>').
        """
        if isinstance(expected, str):
            expected = [expected]
        needles = [(e, e.encode("utf-8") if isinstance(e, str) else e)
                   for e in expected]

        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._data_lock:
                if self._abort.is_set():
                    result = self._data_buf
                    self._data_buf = b""
                    return (result.decode(self._text_codec,
                                          errors="replace"), None)
                if len(self._data_buf) != seen:
                    # Time out on SILENCE, not total time: the deadline
                    # moves on while bytes keep arriving. Half-duplex RF
                    # can take minutes over one reply — never jump ahead.
                    seen = len(self._data_buf)
                    deadline = time.time() + timeout
                low = self._data_buf.lower()
                best_idx, best = -1, None
                for entry, needle in needles:
                    i = low.find(needle.lower())
                    if i >= 0 and (best_idx < 0 or i < best_idx):
                        best_idx, best = i, (entry, needle)
                if best is not None:
                    end = best_idx + len(best[1])
                    result = self._data_buf[:end]
                    self._data_buf = self._data_buf[end:]
                    return (result.decode(self._text_codec,
                                          errors="replace"), best[0])
                lost = self._link_lost()
                if lost:
                    raise ConnectionError(lost)
                if time.time() >= deadline:
                    result = self._data_buf
                    self._data_buf = b""
                    return (result.decode(self._text_codec,
                                          errors="replace"), None)
            time.sleep(0.05)

    def read_eager(self) -> str:
        """Return and drain whatever is immediately in the buffer."""
        with self._data_lock:
            result = self._data_buf
            self._data_buf = b""
            return result.decode(self._text_codec, errors="replace")

    def read_all_pending(self, settle_time: float = 0.5) -> str:
        """Read all pending data, waiting briefly for the reader to drain new frames."""
        time.sleep(settle_time)
        result = ""
        chunk = self.read_eager()
        while chunk:
            result += chunk
            time.sleep(0.2)
            chunk = self.read_eager()
        return result

    def read_raw_bytes(self, n: int, timeout: float = 10.0) -> bytes:
        """
        Read exactly n raw bytes from _data_buf (populated by the reader).
        Used by YappReceiver for binary frame reading.
        Returns fewer than n bytes only if timeout expires.
        """
        deadline = time.time() + timeout
        seen = -1
        while True:
            with self._data_lock:
                if len(self._data_buf) != seen:
                    # Silence-based timeout — see read_until_any()
                    seen = len(self._data_buf)
                    deadline = time.time() + timeout
                if len(self._data_buf) >= n:
                    result = self._data_buf[:n]
                    self._data_buf = self._data_buf[n:]
                    return result
                lost = self._link_lost()
                if lost:
                    raise ConnectionError(lost)
                if time.time() >= deadline:
                    result = self._data_buf
                    self._data_buf = b""
                    return result
            time.sleep(0.02)

    def send_raw(self, data: bytes):
        """Send raw bytes without appending \\r\\n. Used for YAPP ACK/NAK bytes."""
        if not self.connected:
            raise ConnectionError("VARA not connected")
        self._data_sock.sendall(data)


class AGWTransport:
    """
    AX.25 packet transport through an AGWPE-compatible modem — Direwolf,
    or (Qt)SoundModem.

    Unlike VARA there is ONE TCP socket (default port 8000), and
    everything on it is a frame: a 36-byte little-endian header followed
    by data_len bytes of data.

      offset  0  port       int32    radio port, 0 = first channel
              4  kind       1 byte   'C' connect, 'D' data, 'd' disconnect…
              6  pid        1 byte   0xF0 for BBS text
              8  call_from  10 bytes NUL padded
             18  call_to    10 bytes NUL padded
             28  data_len   uint32
             32  user       uint32   unused

    The modem runs the AX.25 link itself — QtC never builds an AX.25
    frame. BBS text arrives in 'D' frames and goes into _data_buf, the
    same shared buffer VaraTransport uses, so BBSSession can't tell the
    two apart.

    Connect sequence:
      1. Open the socket, start the single reader thread
      2. 'R' version and 'G' port list — logged; 'G' also checks that the
         radio port exists before anything is transmitted
      3. 'C' mycall -> BBS
      4. Wait for 'C' "*** CONNECTED With Station <BBS>", or a 'd' frame
         if the station never answers ("RETRYOUT" from Direwolf) or
         refuses ("From Station" — also (Qt)SoundModem's retry-out text)

    Disconnect sequence:
      1. BBSSession has already sent `b`
      2. Poll 'Y' (frames not yet acknowledged) until it reaches 0 — a
         'd' throws away anything still queued, `b` included
      3. Give the BBS HANGUP_WAIT_SECS to hang up by itself — LinBPQ
         sends its own DISC right after `b`
      4. Still linked? Send 'd' and wait for the modem's 'd' reply
         (DISC/UA on air)
      5. Only then close the socket. Closing it with the link still up
         frees the link inside Direwolf WITHOUT a DISC on the air, which
         leaves the BBS holding a dead session.

    The modems silently drop 'D' / 'd' / 'Y' frames whose callsigns don't
    match the link exactly (case and SSID), so the remote call is taken
    from the modem's own 'C' reply rather than from what the user typed.
    """

    _HDR        = struct.Struct("<IBxBx10s10sII")
    HEADER_LEN  = _HDR.size          # 36
    PID_TEXT    = 0xF0               # no layer 3 — plain BBS text
    # A header claiming more than this is a desync (wrong port, not an
    # AGW server), not a real frame. Direwolf itself caps data near 2 KB.
    MAX_RX_DATA = 65536

    # Floor, in seconds of SILENCE, for any wait on a BBS reply (applied
    # by BBSSession._reply_wait). Half-duplex 300 baud with RF retries can
    # go quiet a long while mid-reply, and a genuinely dead link is
    # reported by the modem itself (retries run out → 'd'), so QtC must
    # never give up early and send its next command over the BBS.
    MIN_REPLY_WAIT = 120

    # After `b` is acknowledged, how long disconnect() waits for the BBS to
    # hang up by itself before sending our own DISC. LinBPQ hangs up about
    # a second after `b`; sending 'd' straight away crossed its DISC on the
    # air and cost two wasted frames (our DISC, its DM) — 2026-09-10.
    HANGUP_WAIT_SECS = 10

    def __init__(self, host, port, mycall, target_call, radio_port=0,
                 timeout=120, modem_name="Direwolf", max_frame_data=256):
        self.host        = host
        self.port        = int(port)
        self.mycall      = self._norm_call(mycall)
        self.target_call = self._norm_call(target_call)
        self.radio_port  = int(radio_port)
        # QtC's own backstop. The modem normally reports a failed connect
        # first (Direwolf: RETRY x FRACK), so this only fires if it never
        # answers at all.
        self.timeout     = timeout
        self.modem_name  = modem_name
        # Largest payload QtC puts in one 'D' frame. Direwolf re-splits to
        # its own PACLEN; the cap keeps a long send under Direwolf's ~2 KB
        # receive buffer, which drops the client when exceeded.
        self.max_frame_data = max_frame_data
        # Callsign the modem uses for the far end — replaced by the
        # call_from of its 'C' reply once connected.
        self.remote_call = self.target_call

        self.sock        = None
        self.connected   = False
        self.agw_version = None
        self.radio_ports = []            # descriptions from the 'G' reply

        self._data_buf  = b""            # BBS bytes for read_until & co.
        self._data_lock = threading.Lock()
        self._send_lock = threading.Lock()   # sendall() from several threads
        self._stop_evt  = threading.Event()
        self._reader_thread = None
        self._line_buf  = ""             # reader-only: partial [RX] line
        self._terminal_mode = False

        # Reader -> connect()/disconnect() hand-off, all under _cond.
        self._cond       = threading.Condition()
        self._replies    = {}            # 'R' / 'G' / 'Y' -> latest data
        self._link_event = None          # ('C' | 'd', text) of the last link frame
        self._sock_gone  = False         # modem closed the socket, or we did
        self._closing    = False         # our own disconnect is in progress
        self._lost_reason = None         # why the far end or the modem ended it

        self._log = None
        # Kept for interface parity with VaraTransport. The modem keys the
        # radio itself (or VOX does) — nothing here drives PTT.
        self.ptt  = None
        # Fired when the BBS or the modem ends the link unexpectedly.
        self._on_disconnected_cb = None
        self._text_codec = "utf-8"
        self._abort = threading.Event()

    # Buffer consumers are identical to VaraTransport's — same attribute
    # names (_data_buf, _data_lock, _abort, _text_codec, _terminal_mode,
    # _log) — so borrow them instead of keeping a third copy in step.
    # The readers ask self._link_lost(), which this class overrides.
    set_text_codec    = VaraTransport.set_text_codec
    request_abort     = VaraTransport.request_abort
    clear_abort       = VaraTransport.clear_abort
    abort_requested   = VaraTransport.abort_requested
    set_terminal_mode = VaraTransport.set_terminal_mode
    flush_input       = VaraTransport.flush_input
    read_until        = VaraTransport.read_until
    read_until_any    = VaraTransport.read_until_any
    read_eager        = VaraTransport.read_eager
    read_all_pending  = VaraTransport.read_all_pending
    read_raw_bytes    = VaraTransport.read_raw_bytes
    peek_input        = VaraTransport.peek_input

    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _norm_call(call: str) -> str:
        """Uppercase, and drop a -0 SSID — the modems report SSID 0 as the
        bare call, and every later frame has to match their spelling."""
        c = (call or "").strip().upper()
        if c.endswith("-0"):
            c = c[:-2]
        return c

    @staticmethod
    def _frame_text(data: bytes) -> str:
        """Status texts end in CR plus a NUL counted in data_len."""
        return data.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()

    def _emit(self, direction, text):
        if self._log:
            self._log(direction, text)

    def _send_frame(self, kind: str, data: bytes = b"",
                    call_from: str = "", call_to: str = "", pid: int = 0):
        # 9 characters max so byte 10 of each call field stays NUL.
        header = self._HDR.pack(
            self.radio_port, ord(kind), pid,
            call_from.encode("ascii", errors="replace")[:9],
            call_to.encode("ascii", errors="replace")[:9],
            len(data), 0)
        with self._send_lock:
            if self.sock is None:
                raise ConnectionError(f"{self.modem_name} not connected")
            try:
                self.sock.sendall(header + data)
            except OSError as e:
                raise ConnectionError(
                    f"Lost the connection to {self.modem_name} — {e}") from e

    def _wait_reply(self, kind: str, timeout: float, while_linked=False):
        """Wait for the modem's answer to an 'R' / 'G' / 'Y' request.
        Returns its data, or None on timeout. while_linked=True also stops
        waiting the moment the link goes down."""
        with self._cond:
            self._cond.wait_for(
                lambda: (kind in self._replies or self._sock_gone
                         or (while_linked and not self.connected)),
                timeout)
            return self._replies.pop(kind, None)

    def _wait_link_event(self, timeout: float):
        """Wait for the next 'C' / 'd' frame. Returns it, or None."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._link_event is not None or self._sock_gone,
                timeout)
            return self._link_event

    def _reader(self):
        """
        Single-reader thread — the ONLY caller of recv() on self.sock.

        Reassembles the byte stream into AGW frames (a header can arrive
        split across recv() calls) and dispatches each one. 'D' data goes
        into _data_buf exactly as VaraTransport's data reader does, and is
        streamed as [RX] lines when _terminal_mode is on.
        """
        rx = b""
        while not self._stop_evt.is_set():
            sock = self.sock
            if sock is None:
                break
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self._on_socket_closed()
                return
            rx += chunk
            while len(rx) >= self.HEADER_LEN:
                (_port, kind, _pid, call_from, call_to,
                 data_len, _user) = self._HDR.unpack_from(rx)
                if data_len > self.MAX_RX_DATA:
                    self._emit("SYS",
                        f"{self.modem_name} sent a frame QtC can't read "
                        f"(length {data_len}) — is {self.host}:{self.port} "
                        f"really its AGW port?")
                    self._on_socket_closed()
                    return
                end = self.HEADER_LEN + data_len
                if len(rx) < end:
                    break
                data = rx[self.HEADER_LEN:end]
                rx = rx[end:]
                self._handle_frame(
                    chr(kind),
                    call_from.split(b"\0", 1)[0].decode("ascii", "replace"),
                    call_to.split(b"\0", 1)[0].decode("ascii", "replace"),
                    data)

    def _handle_frame(self, kind, call_from, call_to, data):
        if kind == "D":
            with self._data_lock:
                self._data_buf += data
            if self._terminal_mode:
                self._stream_lines(data)
            else:
                self._line_buf = ""
        elif kind == "C":
            text = self._frame_text(data)
            self._emit("RX-CMD", text)
            with self._cond:
                if call_from:
                    self.remote_call = call_from
                self.connected   = True
                self._link_event = ("C", text)
                self._cond.notify_all()
        elif kind == "d":
            text = self._frame_text(data)
            self._emit("RX-CMD", text)
            with self._cond:
                unsolicited = self.connected and not self._closing
                if unsolicited:
                    # Set before connected drops — a blocked read raises
                    # with this text the moment it sees the link gone.
                    if "RETRYOUT" in text.upper():
                        self._lost_reason = (
                            f"Link to {self.remote_call} lost — "
                            f"{self.modem_name} ran out of retries")
                    else:
                        self._lost_reason = f"{self.remote_call} disconnected"
                self.connected   = False
                self._link_event = ("d", text)
                self._cond.notify_all()
            if unsolicited:
                self._emit("SYS", self._lost_reason)
                self._drop_link()
        elif kind in ("R", "G", "Y"):
            with self._cond:
                self._replies[kind] = data
                self._cond.notify_all()
        # Anything else ('X' register replies, monitor frames) is not ours.

    def _stream_lines(self, data: bytes):
        """Emit complete [RX] lines through the shared splitter, so a line
        split across AX.25 frames is reassembled, not printed in pieces.
        _line_buf carries the partial line between frames."""
        def _emit_line(s: str):
            if s:
                self._emit("RX", s)

        text = data.decode(self._text_codec, errors="replace")
        self._line_buf = _stream_rx_lines(text, self._line_buf, _emit_line)

    def _on_socket_closed(self):
        """The modem closed the socket (or quit), or we closed it."""
        with self._cond:
            unsolicited = (self.connected and not self._closing
                           and not self._stop_evt.is_set())
            if unsolicited:
                self._lost_reason = (
                    f"{self.modem_name} closed the connection — the link to "
                    f"{self.remote_call} is gone")
            self.connected  = False
            self._sock_gone = True
            self._cond.notify_all()
        if unsolicited:
            self._emit("SYS", self._lost_reason)
            self._drop_link()

    def _drop_link(self):
        """Tear down after the far end or the modem ended the link.
        Runs on the reader thread, like VaraTransport's cmd monitor."""
        self._stop_evt.set()
        if self._on_disconnected_cb:
            self._on_disconnected_cb()
        self._cleanup()

    def _link_lost(self):
        """Why a blocked BBS read should stop now, or None while linked.

        Once the modem reports the link gone, nothing more can arrive. The
        one reader thread files every 'D' frame into _data_buf before it
        handles the 'd' that follows, so a read still matches anything
        that got through first. Waiting out MIN_REPLY_WAIT after that cost
        2+ minutes before the session noticed, and a read that timed out
        handed back half a message as if it were whole."""
        if self.connected:
            return None
        return self._lost_reason or f"Not linked to {self.remote_call}"

    def _handshake(self):
        self._send_frame("R")
        ver = self._wait_reply("R", 3.0)
        if ver is None or len(ver) < 8:
            raise ConnectionError(
                f"{self.host}:{self.port} did not answer like an AGW port.\n"
                f"Check that {self.modem_name} is running and that its AGW "
                f"port matches this BBS entry.")
        major, minor = struct.unpack_from("<II", ver)
        self.agw_version = f"{major}.{minor}"
        self._emit("SYS", f"{self.modem_name} AGW version {self.agw_version}")

        self._send_frame("G")
        info = self._wait_reply("G", 3.0)
        if info is None:
            self._emit("SYS", f"{self.modem_name} did not list its channels")
            return
        # "1;Port1 first soundcard mono;" — a count, then one description
        # per port. Port1 in the text is radio port 0 on the wire.
        parts = self._frame_text(info).split(";")
        self.radio_ports = [p.strip() for p in parts[1:] if p.strip()]
        for i, desc in enumerate(self.radio_ports):
            self._emit("SYS", f"  channel {i}: {desc}")
        try:
            count = int(parts[0])
        except ValueError:
            return
        if self.radio_port >= count:
            raise ConnectionError(
                f"{self.modem_name} has no channel {self.radio_port} — it "
                f"reports {count} channel(s), numbered from 0. Set Channel in "
                f"the BBS entry to match CHANNEL in the modem's config.")

    def _open_link(self):
        with self._cond:
            self._link_event = None
        self._emit("TX-CMD",
            f"CONNECT {self.mycall} {self.target_call} "
            f"(channel {self.radio_port})")
        self._send_frame("C", call_from=self.mycall, call_to=self.target_call)
        self._emit("SYS",
            f"Waiting for {self.modem_name} connection to {self.target_call}…")

        event = self._wait_link_event(self.timeout)
        if event and event[0] == "C":
            self._emit("SYS",
                f"{self.modem_name} connected to {self.remote_call}")
            return
        if event and event[0] == "d":
            if "RETRYOUT" in event[1].upper():
                why = "no answer"
            else:
                why = "no answer, or the station refused the connection"
            raise ConnectionError(
                f"{self.modem_name} could not connect to {self.target_call} "
                f"— {why}")
        if self._sock_gone:
            raise ConnectionError(
                f"{self.modem_name} closed the connection while connecting "
                f"to {self.target_call}")
        # Our own backstop fired — cancel the attempt so the modem stops
        # calling on the air.
        self._closing = True
        try:
            self._send_frame("d", call_from=self.mycall,
                             call_to=self.target_call)
            self._wait_link_event(10)
        except ConnectionError:
            pass
        raise ConnectionError(
            f"Timed out waiting for {self.modem_name} connection to "
            f"{self.target_call}")

    def _wait_for_tx_drain(self, limit_secs: float = 60):
        """Poll 'Y' until nothing is queued or unacknowledged on the link,
        so the `b` sent just before disconnect() actually reaches the BBS."""
        self.wait_until_sent(stall_secs=limit_secs,
                             why="before disconnecting")

    def wait_until_sent(self, stall_secs: float = 180, why: str = "") -> bool:
        """Block until everything handed to the modem is on the air and
        acknowledged — 'Y' reports 0 frames outstanding.

        Until then the BBS has not even received the end of what we sent,
        so it cannot have answered: a reply wait started earlier is timing
        our own transmission. At 300 baud with MAXFRAME 1 each frame and
        its RR take ~5 s, so a 2 KB message body stays on the air longer
        than the 120 s silence floor, and the wait for "Message nnn saved"
        would run out while Direwolf is still sending the body.

        Gives up only when the count stops going down for `stall_secs` —
        never on total time; frame size, PACLEN and MAXFRAME don't matter.
        Returns True once nothing is outstanding (or the modem doesn't
        answer 'Y'), False if the link dropped or stalled.
        """
        reported, last_change, last_note = None, time.time(), 0.0
        while self.connected:
            with self._cond:
                self._replies.pop("Y", None)
            self._send_frame("Y", call_from=self.mycall,
                             call_to=self.remote_call)
            reply = self._wait_reply("Y", 2.0, while_linked=True)
            if reply is None or len(reply) < 4:
                # Link already gone, or the modem doesn't answer 'Y' for it.
                return self.connected
            outstanding = struct.unpack_from("<I", reply)[0]
            if outstanding == 0:
                return True
            now = time.time()
            if outstanding != reported:
                last_change = now
                reported = outstanding
                # First count, then at most every 30 s — a long body would
                # otherwise log one line per acknowledged frame.
                if now - last_note >= 30:
                    self._emit("SYS",
                        f"Waiting for {outstanding} frame(s) to be "
                        f"acknowledged {why}…".replace("  ", " "))
                    last_note = now
            elif now - last_change >= stall_secs:
                self._emit("SYS",
                    f"{outstanding} frame(s) still unacknowledged — no "
                    f"progress in {int(stall_secs)} s")
                return False
            time.sleep(1.0)
        return False

    def _cleanup(self):
        """Close the socket — called after disconnect or on error."""
        self._stop_evt.set()
        with self._send_lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._cond:
            self.connected  = False
            self._sock_gone = True
            self._cond.notify_all()

    # ── Public interface (mirrors VaraTransport) ──────────────────

    def connect(self):
        """
        Open the AGW socket, check the modem, and bring up the AX.25 link.
        Raises ConnectionError if anything fails.
        """
        if not self.target_call:
            raise ConnectionError("No BBS callsign set.")
        for call in (self.mycall, self.target_call):
            if len(call) > 9:
                raise ConnectionError(
                    f"Callsign {call} is too long for AX.25 packet "
                    f"(9 characters at most, e.g. N0CALL-15).")
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=5)
        except OSError as e:
            self.sock = None
            raise ConnectionError(
                f"Cannot reach {self.modem_name} AGW port "
                f"{self.host}:{self.port} — {e}\n"
                f"Is {self.modem_name} running?")
        # Short timeout so the reader notices _stop_evt promptly. Sends
        # are a few hundred bytes to a local socket, well inside it.
        self.sock.settimeout(0.5)
        self._stop_evt.clear()
        with self._cond:
            self._replies.clear()
            self._link_event = None
            self._sock_gone  = False
            self._closing    = False
            self._lost_reason = None
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="agw-reader")
        self._reader_thread.start()

        try:
            self._handshake()
            self._open_link()
        except Exception:
            self._cleanup()
            raise

    def disconnect(self):
        """
        Clean disconnect — let the modem deliver what's queued (the `b`),
        then ask it for a DISC and wait for confirmation before closing.
        """
        if not (self.sock and self.connected):
            self._cleanup()
            return
        self._closing = True
        try:
            self._wait_for_tx_drain()
            if self.connected and self.HANGUP_WAIT_SECS:
                # Let the BBS end it — see HANGUP_WAIT_SECS
                with self._cond:
                    self._cond.wait_for(
                        lambda: not self.connected or self._sock_gone,
                        self.HANGUP_WAIT_SECS)
                if not self.connected:
                    self._emit("SYS", f"{self.remote_call} hung up")
            if self.connected:
                with self._cond:
                    self._link_event = None
                self._emit("TX-CMD", f"DISCONNECT {self.remote_call}")
                self._send_frame("d", call_from=self.mycall,
                                 call_to=self.remote_call)
                if self._wait_link_event(60) is None:
                    self._emit("SYS",
                        f"No disconnect confirmation from {self.modem_name} "
                        f"— closing anyway")
        except ConnectionError:
            pass
        self._cleanup()

    def abort(self):
        """Immediate disconnect — no drain. Still sends 'd' so the modem
        puts a DISC on the air instead of silently dropping the link."""
        self._closing = True
        if self.sock and self.connected:
            try:
                self._send_frame("d", call_from=self.mycall,
                                 call_to=self.remote_call)
                time.sleep(0.5)   # let the modem act before the socket closes
            except ConnectionError:
                pass
        self._cleanup()

    def send(self, text):
        """Send text to the BBS. Lines end in CR only — the packet
        convention; LinBPQ treats CR as end of line."""
        if isinstance(text, str):
            text = (text + "\r").encode("utf-8", errors="replace")
        self.send_raw(text)

    def send_raw(self, data: bytes):
        """Send raw bytes without a line ending. Used for YAPP ACK/NAK bytes."""
        if not self.connected:
            raise ConnectionError(f"{self.modem_name} not connected")
        for i in range(0, len(data), self.max_frame_data):
            self._send_frame("D", data[i:i + self.max_frame_data],
                             call_from=self.mycall, call_to=self.remote_call,
                             pid=self.PID_TEXT)


class VaraControl:
    """
    Persistent connection to VARA's command and data ports.

    Holds both port 8300 (cmd) and port 8301 (data) open continuously
    while the app is running — exactly like LinBPQ and VARA Terminal do.
    This keeps VARA's TCP indicator green and keeps VARA in a ready state.

    When a BBS session starts, both sockets are closed so VaraTransport
    can take them over.  They are re-opened when the session ends.
    """

    def __init__(self, vara_host: str = "127.0.0.1",
                 cmd_port: int = 8300, data_port: int = 8301):
        self.vara_host = vara_host
        self.cmd_port  = cmd_port
        self.data_port = data_port
        self._sock      = None   # cmd port 8300
        self._data_sock = None   # data port 8301
        self._lock     = threading.Lock()
        self._log      = None
        # Pre-session DCD/channel-busy tracking. VARA emits "BUSY ON" /
        # "BUSY OFF" lines on the cmd port whenever channel activity is
        # detected. A small reader thread keeps `_busy` current so
        # Mail-Call can do a polite pre-flight check before transmitting.
        # Default False = clear; VARA only emits on state change so a
        # genuinely-quiet channel never sets this True.
        self._busy = False
        self._busy_last_update = 0.0
        self._reader_thread = None
        self._reader_stop = threading.Event()
        # Bumped by close(). open(gen=...) from a background re-link does
        # nothing if a close() happened since the caller read `gen` — so a
        # re-link in flight can never take the port back from a session
        # that is just starting.
        self.gen = 0

    def _emit(self, direction: str, text: str):
        if self._log:
            self._log(direction, text)

    def open(self, gen: int = None) -> bool:
        """
        Open both the command (8300) and data (8301) sockets.
        Returns True if at least the command port connected.
        Never raises.  Safe to call multiple times.
        gen: skip the open if close() was called since this value of
        self.gen was read.
        """
        with self._lock:
            if gen is not None and gen != self.gen:
                return False
            # Command port
            if not self._sock:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2.0)
                    s.connect((self.vara_host, self.cmd_port))
                    self._sock = s
                except OSError:
                    self._sock = None

            # Data port — hold open so VARA sees a connected client
            if not self._data_sock:
                try:
                    d = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    d.settimeout(2.0)
                    d.connect((self.vara_host, self.data_port))
                    self._data_sock = d
                except OSError:
                    self._data_sock = None

        # Spin up the BUSY-state reader if the cmd port opened and the
        # thread isn't already running. Daemon thread; dies with the app.
        if self._sock and (self._reader_thread is None
                           or not self._reader_thread.is_alive()):
            self._reader_stop.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="VaraIdleReader",
                daemon=True,
            )
            self._reader_thread.start()

        return self._sock is not None

    def close(self):
        """Close both sockets and stop the busy-reader thread."""
        self._reader_stop.set()
        with self._lock:
            self.gen += 1
            for sock in (self._sock, self._data_sock):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._sock      = None
            self._data_sock = None
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

    def is_busy(self) -> bool:
        """Return VARA's current BUSY state (DCD-equivalent).

        False = clear OR unknown (no traffic since startup). VARA emits
        BUSY ON / BUSY OFF only on state changes; a genuinely quiet
        channel never flips this to True. Used by Mail-Call's pre-flight
        check to avoid transmitting over an active QSO.
        """
        return self._busy

    def _reader_loop(self):
        """
        Read lines from the cmd port and track BUSY state.

        Stays silent (does not call self._emit) — the verbose [RX-CMD]
        logging belongs to VaraTransport's monitor thread during an
        active session. While idle, we just track _busy invisibly.
        """
        buf = b""
        while not self._reader_stop.is_set():
            sock = self._sock
            if sock is None:
                self._reader_stop.wait(1.0)
                continue
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self._drop_dead(sock)
                buf = b""
                self._reader_stop.wait(0.5)
                continue
            if not chunk:
                # Socket closed by VARA (VARA quit). Forget both sockets so
                # is_open goes False and the GUI's re-link timer can reopen
                # them once VARA is back — until 2026-10-01 a dead socket
                # stayed "open" for good.
                self._drop_dead(sock)
                buf = b""
                self._reader_stop.wait(1.0)
                continue
            buf += chunk
            while True:
                # Find the earliest line terminator (\r or \n)
                idx = -1
                for ch in (b"\r", b"\n"):
                    p = buf.find(ch)
                    if p >= 0 and (idx < 0 or p < idx):
                        idx = p
                if idx < 0:
                    break
                line = buf[:idx].decode("utf-8", errors="replace").strip()
                buf = buf[idx + 1:]
                if not line:
                    continue
                upper = line.upper()
                if upper.startswith("BUSY ON"):
                    self._busy = True
                    self._busy_last_update = time.time()
                elif upper.startswith("BUSY OFF"):
                    self._busy = False
                    self._busy_last_update = time.time()

    def _drop_dead(self, sock):
        """Forget both sockets after the cmd socket `sock` died, unless
        open()/close() already replaced it in the meantime."""
        with self._lock:
            if self._sock is not sock:
                return
            for s in (self._sock, self._data_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
            self._sock      = None
            self._data_sock = None
            self._busy      = False

    def send(self, cmd: str) -> bool:
        """
        Send a single command to VARA.  Tries to reconnect once if the
        socket has gone stale.  Returns True if the send succeeded.
        """
        for attempt in range(2):
            with self._lock:
                if self._sock is None:
                    break
                try:
                    self._sock.sendall((cmd + "\r\n").encode("utf-8"))
                    self._emit("TX-CMD", cmd)
                    return True
                except OSError:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            if attempt == 0:
                self.open()
        return False

    def set_bandwidth(self, bw: str) -> bool:
        """Push a bandwidth selection to VARA.

        HF takes BW500 / BW2300 (with the BW prefix); FM takes a bare
        NARROW / WIDE keyword. Caller passes the raw user-facing value
        — "500", "2300", "NARROW", or "WIDE" — and this picks the wire
        form by looking at the value itself."""
        if not bw:
            return False
        up = bw.upper().strip()
        if up in ("NARROW", "WIDE"):
            return self.send(up)
        return self.send(f"BW{up}")

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
