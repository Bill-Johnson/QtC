# QtC v0.9.8-beta — transport.py  (built 2026-03-22)
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
        self._lock = threading.Lock()
        self._terminal_mode   = False
        self._stop_monitor    = threading.Event()
        self._monitor_thread  = None
        self._log = None   # set by SessionWorker to emit [RX] log lines

    def set_terminal_mode(self, enabled: bool):
        """Enable/disable background streaming of BBS responses to terminal."""
        self._terminal_mode = enabled
        if enabled:
            self.flush_input()
            self._stop_monitor.clear()
            if self._monitor_thread is None or not self._monitor_thread.is_alive():
                self._monitor_thread = threading.Thread(
                    target=self._terminal_monitor, daemon=True,
                    name="telnet-terminal-monitor")
                self._monitor_thread.start()
        else:
            self._stop_monitor.set()
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2.0)
                self._monitor_thread = None

    def _terminal_monitor(self):
        """Background thread — reads Telnet socket and emits [RX] log lines."""
        import time as _time
        line_buf = ""
        while not self._stop_monitor.is_set():
            # Wait for connection
            if not self.connected or self.sock is None:
                _time.sleep(0.1)
                continue
            # Drain leftover _buf first (from login _expect calls), then socket
            if self._buf:
                chunk = self._buf
                self._buf = b""
            else:
                chunk = self._recv_chunk(timeout=0.5)
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="replace")
            for ch in text:
                if ch in ("\r", "\n"):
                    if line_buf.strip():
                        if self._log:
                            self._log("RX", line_buf.strip())
                    line_buf = ""
                else:
                    line_buf += ch
        # Flush any remaining partial line
        if line_buf.strip() and self._log:
            self._log("RX", line_buf.strip())

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect((self.host, self.port))
            self.connected = True
        except (ConnectionRefusedError, OSError) as e:
            raise ConnectionError(
                f"Could not connect to {self.host}:{self.port} — {e}")

    def disconnect(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._stop_monitor.set()
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
        """Discard any unread bytes on the telnet socket."""
        if not self.sock:
            return
        old_to = self.sock.gettimeout()
        self.sock.settimeout(0.1)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
        except (socket.timeout, OSError):
            pass
        self.sock.settimeout(old_to)

    def read_until(self, expected: str, timeout: int = 15) -> str:
        """Read until expected string appears in the buffer."""
        if isinstance(expected, str):
            expected_b = expected.encode("utf-8")
        else:
            expected_b = expected

        deadline = time.time() + timeout
        while True:
            if expected_b.lower() in self._buf.lower():
                # Return everything up to and including the match
                idx = self._buf.lower().find(expected_b.lower())
                result = self._buf[:idx + len(expected_b)]
                self._buf = self._buf[idx + len(expected_b):]
                return result.decode("utf-8", errors="replace")

            remaining = deadline - time.time()
            if remaining <= 0:
                # Return whatever we have even if no match
                result = self._buf
                self._buf = b""
                return result.decode("utf-8", errors="replace")

            chunk = self._recv_chunk(timeout=min(remaining, 2.0))
            if chunk:
                self._buf += chunk

    def read_eager(self) -> str:
        """Non-blocking read of whatever is immediately available."""
        chunk = self._recv_chunk(timeout=0.1)
        if chunk:
            self._buf += chunk
        result = self._buf
        self._buf = b""
        return result.decode("utf-8", errors="replace")

    def read_all_pending(self, settle_time: float = 0.5) -> str:
        """Read all pending data, waiting briefly for more."""
        time.sleep(settle_time)
        result = ""
        chunk = self.read_eager()
        while chunk:
            result += chunk
            time.sleep(0.2)
            chunk = self.read_eager()
        return result


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
                 timeout=60, bandwidth="2300"):
        self.vara_host   = vara_host
        self.cmd_port    = cmd_port
        self.data_port   = data_port
        self.mycall      = mycall.upper()
        self.target_call = target_call.upper()
        self.timeout     = timeout
        self.bandwidth   = str(bandwidth)   # "500" or "2300"

        self._cmd_sock  = None
        self._data_sock = None

        self.connected  = False          # True once VARA says CONNECTED
        self._busy      = False          # Channel busy flag
        self._buffer    = 0             # Bytes in VARA TX queue
        self._last_cmd_resp = ""        # Last raw response from cmd port

        self._cmd_buf   = b""           # Unprocessed bytes from cmd port
        self._data_buf  = b""           # Unprocessed bytes from data port

        self._cmd_lock  = threading.Lock()
        self._stop_evt  = threading.Event()
        self._cmd_thread = None
        self._data_thread = None
        self._terminal_mode = False  # when True, data monitor streams to log
        self._monitor_idle  = threading.Event()
        self._monitor_idle.set()   # starts idle

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

    def _data_monitor(self):
        """
        Background thread — reads data port continuously in terminal mode
        and emits complete lines as [RX] log entries. Buffers partial lines
        across recv() calls so VARA frame splits never cut mid-line.
        Pauses automatically when terminal_mode is False.
        """
        line_buf = ""       # accumulates chars until \r or \n
        recent = []         # last N lines logged — suppress duplicates in window
        while not self._stop_evt.is_set():
            if not self._terminal_mode or not self._data_sock:
                if line_buf and self._log:
                    # Flush partial buffer on mode switch
                    self._log("RX", line_buf.strip())
                    line_buf = ""
                self._monitor_idle.set()   # signal that monitor is idle
                time.sleep(0.1)
                continue
            self._monitor_idle.clear()  # signal that monitor is active
            try:
                self._data_sock.settimeout(0.5)
                chunk = self._data_sock.recv(4096)
                if chunk:
                    self._data_buf += chunk
                    text = chunk.decode("utf-8", errors="replace")
                    # Feed char-by-char into line buffer
                    # Emit a complete line on \r or \n
                    for ch in text:
                        if ch in ("\r", "\n"):
                            stripped = line_buf.strip()
                            if stripped and self._log:
                                # Deduplicate: if two frames concatenated
                                # e.g. "Enter Title:Enter Title:", emit only once
                                n = len(stripped)
                                half = n // 2
                                if (half > 4 and n % 2 == 0
                                        and stripped[:half] == stripped[half:]):
                                    stripped = stripped[:half]
                                # Suppress lines seen recently
                                if stripped not in recent:
                                    self._log("RX", stripped)
                                recent.append(stripped)
                                if len(recent) > 6:
                                    recent.pop(0)
                            line_buf = ""
                        else:
                            line_buf += ch
                    # If buffer ends with BBS prompt, flush immediately
                    if line_buf.endswith(">") and self._log:
                        stripped = line_buf.strip()
                        if stripped not in recent:
                            self._log("RX", stripped)
                        recent.append(stripped)
                        if len(recent) > 6:
                            recent.pop(0)
                        line_buf = ""
            except socket.timeout:
                # On timeout, flush any partial line — RF turnarounds can
                # split a single BBS line across two recv() calls. Emitting
                # on timeout prevents the partial from concatenating with
                # the next frame and defeating the duplicate check.
                if line_buf.strip() and self._log:
                    stripped = line_buf.strip()
                    if stripped not in recent:
                        self._log("RX", stripped)
                    recent.append(stripped)
                    if len(recent) > 6:
                        recent.pop(0)
                    line_buf = ""
            except OSError:
                break

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

        # 4. Set bandwidth, then our callsign
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
        # Start background data monitor for terminal mode streaming
        self._data_thread = threading.Thread(
            target=self._data_monitor, daemon=True, name="vara-data-monitor")
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
        Enable/disable background data streaming for terminal view.
        When True: data port is read continuously and emitted as [RX] log lines.
        When False: data port is only read via explicit _expect() calls (mail mode).
        """
        self._terminal_mode = enabled
        if not enabled:
            # Wait up to 1s for monitor to finish its current recv() and go idle
            self._monitor_idle.wait(timeout=1.0)
            self.flush_input()   # discard anything buffered during transition
        else:
            self.flush_input()

    def send(self, text):
        """Send text over RF via VARA data port."""
        if not self.connected:
            raise ConnectionError("VARA not connected")
        if isinstance(text, str):
            text = (text + "\r\n").encode("utf-8", errors="replace")
        self._data_sock.sendall(text)

    def _recv_data_chunk(self, timeout: float = 2.0) -> bytes:
        """Read a chunk from the VARA data port.

        NOTE: Do NOT set connected=False on empty recv — VARA's data socket
        goes quiet during PTT turnarounds, which is normal.  Disconnection
        is signalled by the COMMAND port ("DISCONNECTED"), not by the data
        socket going empty.  Setting connected=False here caused read_until
        to exit early mid-LM-list when VARA flipped PTT between RF frames.
        """
        if self._data_sock is None:
            return b""
        self._data_sock.settimeout(timeout)
        try:
            chunk = self._data_sock.recv(4096)
            return chunk  # b"" (remote closed) is handled by caller
        except socket.timeout:
            return b""
        except OSError:
            return b""

    def flush_input(self):
        """Discard any unread bytes in the data buffer and socket.
        Call before sending a new command to avoid stale data bleed."""
        if self._data_buf:
            self._log("SYS",
                f"flush_input: discarding {len(self._data_buf)} stale bytes")
        self._data_buf = b""
        # Drain anything sitting on the socket with a very short timeout
        if self._data_sock:
            old_to = self._data_sock.gettimeout()
            self._data_sock.settimeout(0.1)
            try:
                while True:
                    chunk = self._data_sock.recv(4096)
                    if not chunk:
                        break
            except (socket.timeout, OSError):
                pass
            self._data_sock.settimeout(old_to)

    def read_until(self, expected: str, timeout: int = 30) -> str:
        """
        Read from VARA data port until expected string appears.
        Same interface as TelnetTransport.read_until().
        """
        if isinstance(expected, str):
            expected_b = expected.encode("utf-8")
        else:
            expected_b = expected

        deadline = time.time() + timeout
        while True:
            if expected_b.lower() in self._data_buf.lower():
                idx    = self._data_buf.lower().find(expected_b.lower())
                result = self._data_buf[:idx + len(expected_b)]
                self._data_buf = self._data_buf[idx + len(expected_b):]
                return result.decode("utf-8", errors="replace")

            remaining = deadline - time.time()
            if remaining <= 0:
                result = self._data_buf
                self._data_buf = b""
                return result.decode("utf-8", errors="replace")

            chunk = self._recv_data_chunk(timeout=min(remaining, 2.0))
            if chunk:
                self._data_buf += chunk
            # chunk == b"" means socket timeout (normal between RF frames) — keep waiting

    def read_eager(self) -> str:
        """Non-blocking read of whatever is immediately available."""
        chunk = self._recv_data_chunk(timeout=0.1)
        if chunk:
            self._data_buf += chunk
        result = self._data_buf
        self._data_buf = b""
        return result.decode("utf-8", errors="replace")

    def read_all_pending(self, settle_time: float = 0.5) -> str:
        """Read all pending data, waiting briefly for more."""
        time.sleep(settle_time)
        result = ""
        chunk = self.read_eager()
        while chunk:
            result += chunk
            time.sleep(0.2)
            chunk = self.read_eager()
        return result


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

            return self._sock is not None

    def close(self):
        """Close both sockets."""
        with self._lock:
            for sock in (self._sock, self._data_sock):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._sock      = None
            self._data_sock = None

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
        """Send BW500 or BW2300.  bw should be '500' or '2300'."""
        return self.send(f"BW{bw}")

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
