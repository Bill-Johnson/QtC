# QtC v0.13.2-beta — transport.py  (built 2026-05-24)
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
import time
import threading

# PTT is optional — only imported when a PTTController is attached
try:
    from ptt import PTTController
except ImportError:
    PTTController = None

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
        log entries. Lines are flushed on real terminators only —
        \\r, \\n, >, :, ? — never on socket timeout, so a BBS line split
        across two recv() calls is reassembled, not printed in pieces.
        """
        line_buf = ""
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
            text = chunk.decode("utf-8", errors="replace")
            for ch in text:
                if ch in ("\r", "\n"):
                    if line_buf.strip() and self._log:
                        self._log("RX", line_buf.strip())
                    line_buf = ""
                elif ch in (">", ":", "?"):
                    line_buf += ch
                    if line_buf.strip() and self._log:
                        self._log("RX", line_buf.strip())
                    line_buf = ""
                else:
                    line_buf += ch

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

    def read_until(self, expected: str, timeout: int = 15) -> str:
        """Read from _buf until expected string appears.

        Consumes from _buf only — never calls recv() (the reader does).
        """
        if isinstance(expected, str):
            expected_b = expected.encode("utf-8")
        else:
            expected_b = expected

        deadline = time.time() + timeout
        while True:
            with self._lock:
                idx = self._buf.lower().find(expected_b.lower())
                if idx >= 0:
                    end = idx + len(expected_b)
                    result = self._buf[:end]
                    self._buf = self._buf[end:]
                    return result.decode("utf-8", errors="replace")
                if time.time() >= deadline:
                    result = self._buf
                    self._buf = b""
                    return result.decode("utf-8", errors="replace")
            time.sleep(0.05)

    def read_eager(self) -> str:
        """Return and drain whatever is immediately in the buffer."""
        with self._lock:
            result = self._buf
            self._buf = b""
            return result.decode("utf-8", errors="replace")

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
        while True:
            with self._lock:
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
                    self.connected = False
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
        as [RX] log entries for the terminal view. Lines are flushed only
        on real terminators (\\r, \\n, >, :, ?) — no timeout-based partial
        flush, so a line split across two RF frames is reassembled instead
        of being printed in two pieces.

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

            text = chunk.decode("utf-8", errors="replace")
            for ch in text:
                if ch in ("\r", "\n"):
                    _emit_line(line_buf.strip())
                    line_buf = ""
                elif ch in (">", ":", "?"):
                    # Prompt terminators — flush immediately so the user
                    # sees the BBS waiting-for-input cue without delay.
                    line_buf += ch
                    _emit_line(line_buf.strip())
                    line_buf = ""
                else:
                    line_buf += ch

    # ── Public interface (mirrors TelnetTransport) ────────────────

    def connect(self):
        """
        Open command + data sockets, handshake with VARA, initiate RF connect.
        Raises ConnectionError if anything fails.
        """
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

    def _buf_find(self, needle: bytes):
        """Case-insensitive search in _data_buf under the lock."""
        with self._data_lock:
            i = self._data_buf.lower().find(needle.lower())
            if i < 0:
                return -1
            return i

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
        while True:
            with self._data_lock:
                idx = self._data_buf.lower().find(expected_b.lower())
                if idx >= 0:
                    end = idx + len(expected_b)
                    result = self._data_buf[:end]
                    self._data_buf = self._data_buf[end:]
                    return result.decode("utf-8", errors="replace")
                if time.time() >= deadline:
                    result = self._data_buf
                    self._data_buf = b""
                    return result.decode("utf-8", errors="replace")
            # No match yet — give the reader a moment to add more bytes
            time.sleep(0.05)

    def read_eager(self) -> str:
        """Return and drain whatever is immediately in the buffer."""
        with self._data_lock:
            result = self._data_buf
            self._data_buf = b""
            return result.decode("utf-8", errors="replace")

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
        while True:
            with self._data_lock:
                if len(self._data_buf) >= n:
                    result = self._data_buf[:n]
                    self._data_buf = self._data_buf[n:]
                    return result
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

    def _emit(self, direction: str, text: str):
        if self._log:
            self._log(direction, text)

    def open(self) -> bool:
        """
        Open both the command (8300) and data (8301) sockets.
        Returns True if at least the command port connected.
        Never raises.  Safe to call multiple times.
        """
        with self._lock:
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
                self._reader_stop.wait(0.5)
                continue
            if not chunk:
                # Socket closed by VARA — wait briefly and let open() reopen
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

    def set_mycall(self, callsign: str) -> bool:
        """Send MYCALL <callsign>."""
        return self.send(f"MYCALL {callsign.upper()}")

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
