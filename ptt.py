# QtC v0.16.0-dev — ptt.py  (built 2026-11-26)
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
# VARA BBS Client — A modern BBS client for LinBPQ/BPQ32 nodes
# via VARA HF, VARA FM, and Telnet.
#
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
# ---------------------------------------------------------------------------

"""
ptt.py — PTT (Push-To-Talk) controller for VARA BBS Client

Handles keying the radio transmitter via serial port RTS/DTR signals,
driven by PTT ON / PTT OFF commands received from VARA on the cmd port.

Supported PTT modes:
    none    — No PTT control (VOX, or VARA handles it internally)
    rts     — Assert RTS high on serial port
    dtr     — Assert DTR high on serial port
    rts+dtr — Assert both RTS and DTR high

CAT PTT (config "mode"): QtC asks a rig-control program to key the radio,
so the radio's one CAT port stays with that program and no second serial
port is needed (Xiegu X6100 and most radios with a built-in USB sound card):
    flrig   — FlrigPTT, flrig's XML-RPC server (default 127.0.0.1:12345)
    rigctld — RigctldPTT, Hamlib's rigctld daemon (default 127.0.0.1:4532)
Both hold a 30 s maximum-TX watchdog (MAX_TX_S): a network PTT can be left
keyed by a crashed or hung rig program in a way an RTS line cannot.
make_ptt(cfg) builds the right controller from the config's "ptt" dict.

Typical hardware:
    Digirig Mobile — uses RTS on its ttyUSB serial port
    SignaLink USB   — VOX only (mode = none)
    RigBlaster      — typically RTS or DTR

Usage:
    ptt = PTTController(port="/dev/ttyUSB0", mode="rts")
    ptt.open()
    ptt.tx()      # Key transmitter
    ptt.rx()      # Release transmitter
    ptt.close()
"""

import threading
import glob
import socket
import sys
import time


# ── Port scanner ──────────────────────────────────────────────────────────────

def list_serial_ports() -> list[str]:
    """
    Return a list of available serial port device paths.
    Scans the typical locations for Linux (ttyUSB*, ttyACM*, ttyS*)
    and Windows (COM*).
    """
    ports = []
    if sys.platform.startswith("win"):
        # Windows — ask the OS for its COM ports; never open one to find
        # out. This used to open and close COM1..COM32 in turn, and
        # opening a port raises RTS and DTR on Windows: every time the
        # Settings dialog opened, a Digirig (PTT on RTS) keyed the radio
        # for a blip (Bill, HP laptop, 2026-10-04).
        from serial.tools import list_ports
        ports = sorted((p.device for p in list_ports.comports()),
                       key=lambda n: (len(n), n))   # COM3 before COM10
    else:
        # Linux / macOS
        for pattern in (
            "/dev/ttyUSB*",
            "/dev/ttyACM*",
            "/dev/ttyS*",
            "/dev/cu.usbserial*",   # macOS Digirig / FTDI
            "/dev/cu.usbmodem*",    # macOS
        ):
            ports.extend(sorted(glob.glob(pattern)))

    return ports


# ── PTT controller ────────────────────────────────────────────────────────────

class PTTController:
    """
    Controls TX/RX state via serial port RTS and/or DTR lines.

    Thread-safe — tx() / rx() may be called from the VARA cmd-monitor thread.
    """

    MODES = ("none", "rts", "dtr", "rts+dtr")

    def __init__(self, port: str = "", mode: str = "rts"):
        """
        port  — serial device path, e.g. '/dev/ttyUSB0' or 'COM3'
        mode  — one of 'none', 'rts', 'dtr', 'rts+dtr'
        """
        self.port = port
        self.mode = mode.lower().strip()
        self._ser  = None
        self._lock = threading.Lock()
        self._log  = None          # optional callable(direction, text)
        self.last_error = ""       # populated by open() on failure; "" on success

    # ── Lifecycle ─────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        """True iff the serial port is currently open."""
        return self._ser is not None and getattr(self._ser, "is_open", False)

    def open(self):
        """Open the serial port.  No-op if mode is 'none' or port is empty."""
        if self.mode == "none" or not self.port:
            return
        try:
            import serial
        except ImportError:
            self._emit("PTT", "pyserial not installed — PTT disabled. "
                               "Run: pip install pyserial")
            self.mode = "none"
            return

        with self._lock:
            try:
                # Disable hardware/software flow control so pyserial doesn't
                # auto-manage RTS/DTR for us — we drive those lines for PTT.
                # Build the port closed and set both lines low BEFORE
                # open(): serial.Serial(port=...) opens at once with RTS
                # and DTR raised, which keys a Digirig for a blip until
                # _set_lines(False) catches up.
                ser = serial.Serial()
                ser.port     = self.port
                ser.baudrate = 9600        # baudrate irrelevant for RTS/DTR
                ser.timeout  = 0
                ser.rtscts   = False
                ser.dsrdtr   = False
                ser.xonxoff  = False
                ser.rts      = False
                ser.dtr      = False
                ser.open()
                self._ser = ser
                # Start in RX (lines low)
                self._set_lines(False)
                self.last_error = ""
                self._emit("PTT", f"PTT port open: {self.port}  mode={self.mode}")
            except Exception as e:
                self._ser = None
                self.last_error = str(e)
                self._emit("PTT", f"Cannot open PTT port {self.port}: {e}")

    def close(self):
        """Release PTT and close the serial port."""
        with self._lock:
            if self._ser and self._ser.is_open:
                try:
                    self._set_lines(False)   # ensure RX
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

    # ── PTT control ───────────────────────────────────────────────

    def tx(self):
        """Assert PTT — key the transmitter."""
        with self._lock:
            if self._ser and self._ser.is_open:
                self._set_lines(True)
                self._emit("PTT", f"TX  ({self.mode} HIGH on {self.port})")

    def rx(self):
        """Release PTT — return to receive."""
        with self._lock:
            if self._ser and self._ser.is_open:
                self._set_lines(False)
                self._emit("PTT", f"RX  ({self.mode} LOW on {self.port})")

    def test(self, duration: float = 1.0):
        """
        Key the transmitter for `duration` seconds then release.
        Runs in the calling thread — use a QThread wrapper in the GUI.
        """
        import time
        self.tx()
        time.sleep(duration)
        self.rx()

    # ── Internal helpers ──────────────────────────────────────────

    def _set_lines(self, state: bool):
        """Set RTS and/or DTR according to mode.  Must hold _lock."""
        if self._ser is None:
            return
        if self.mode in ("rts", "rts+dtr"):
            self._ser.rts = state
        if self.mode in ("dtr", "rts+dtr"):
            self._ser.dtr = state

    def _emit(self, direction: str, text: str):
        if self._log:
            self._log(direction, text)

    # ── Context manager ───────────────────────────────────────────

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ── Repr ──────────────────────────────────────────────────────

    def __repr__(self):
        return (f"PTTController(port={self.port!r}, mode={self.mode!r}, "
                f"open={self._ser is not None})")


# ── CAT PTT through a rig-control program ─────────────────────────────────────

MAX_TX_S     = 30.0   # watchdog: force PTT off after this long keyed (Bill, 2026-11-26)
NET_TIMEOUT  = 2.0    # every flrig / rigctld call; tx()/rx() run on VARA's cmd thread
RX_ATTEMPTS  = 3      # unkey is the one that must not fail

FLRIG_DEFAULT_PORT   = 12345
RIGCTLD_DEFAULT_PORT = 4532


class _NetPTT:
    """
    Base for PTT keyed by a rig-control program over TCP.

    Same public face as PTTController (open / close / tx / rx / test /
    is_open / last_error / _log), so VaraTransport and the Settings test
    button use it unchanged. Subclasses supply _connect(), _disconnect(),
    _set_ptt(bool) and _get_ptt() -> bool|None.
    """

    KIND = "CAT"

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 max_tx_s: float = MAX_TX_S):
        self.host = (host or "127.0.0.1").strip()
        self.port = int(port)
        self.max_tx_s = max_tx_s
        self.mode = self.KIND.lower()
        self._lock = threading.RLock()
        self._log = None            # optional callable(direction, text)
        self._on_alert = None       # optional callable(text): stuck key / watchdog
        self.last_error = ""
        self._open = False
        self._keyed = False
        self._watchdog = None
        self.watchdog_fired = False  # tests / status: did the 30 s cap trip?

    # ── Lifecycle ─────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}"

    def open(self):
        """Reach the rig program and make sure the radio starts in RX."""
        with self._lock:
            try:
                self._connect()
                self._set_ptt(False)
                self._open = True
                self.last_error = ""
                self._emit("PTT", f"PTT via {self.KIND} at {self.where}"
                                  f"{self._describe()}")
            except Exception as e:
                self._open = False
                self.last_error = self._explain(e)
                self._disconnect()
                self._emit("PTT", f"Cannot reach {self.KIND} at "
                                  f"{self.where}: {self.last_error}")

    def close(self):
        with self._lock:
            if self._open:
                self.rx()
            self._cancel_watchdog()
            self._disconnect()
            self._open = False

    # ── PTT control ───────────────────────────────────────────────

    def tx(self):
        with self._lock:
            if not self._open:
                return
            try:
                self._set_ptt(True)
            except Exception as e:
                # Reconnect once: rigctld/flrig may have been restarted.
                try:
                    self._disconnect()
                    self._connect()
                    self._set_ptt(True)
                except Exception:
                    self._emit("PTT", f"*** {self.KIND} did not key the radio: "
                                      f"{self._explain(e)} ***")
                    return
            self._keyed = True
            self._start_watchdog()
            self._emit("PTT", f"TX  ({self.KIND} {self.where})")

    def rx(self):
        with self._lock:
            if not self._open:
                return
            self._cancel_watchdog()
            last = None
            for attempt in range(RX_ATTEMPTS):
                try:
                    if attempt:
                        self._disconnect()
                        self._connect()
                    self._set_ptt(False)
                    if self._get_ptt() is not True:   # None = can't tell
                        self._keyed = False
                        self._emit("PTT", f"RX  ({self.KIND} {self.where})")
                        return
                    last = "radio still reports TX"
                except Exception as e:
                    last = self._explain(e)
            msg = (f"{self.KIND} could not unkey the radio ({last}). "
                   f"Check the radio NOW — it may still be transmitting.")
            self._emit("PTT", f"*** {msg} ***")
            if self._on_alert:
                self._on_alert(msg)

    def test(self, duration: float = 1.0):
        self.tx()
        time.sleep(duration)
        self.rx()

    # ── Watchdog ──────────────────────────────────────────────────

    def _start_watchdog(self):
        self._cancel_watchdog()
        if self.max_tx_s and self.max_tx_s > 0:
            t = threading.Timer(self.max_tx_s, self._watchdog_expired)
            t.daemon = True
            self._watchdog = t
            t.start()

    def _cancel_watchdog(self):
        t, self._watchdog = self._watchdog, None
        if t is not None and t is not threading.current_thread():
            t.cancel()

    def _watchdog_expired(self):
        with self._lock:
            if not self._keyed:
                return
            self.watchdog_fired = True
            msg = (f"PTT was keyed for {self.max_tx_s:.0f} s without a "
                   f"PTT OFF — QtC released it (maximum TX time).")
            self._emit("PTT", f"*** {msg} ***")
            self._watchdog = None
            self.rx()
        if self._on_alert:
            self._on_alert(msg)

    # ── Helpers ───────────────────────────────────────────────────

    def _describe(self) -> str:
        return ""

    def _explain(self, e: Exception) -> str:
        if isinstance(e, ConnectionRefusedError):
            return (f"connection refused — is {self.KIND} running, "
                    f"and listening on port {self.port}?")
        if isinstance(e, (socket.timeout, TimeoutError)):
            return f"no answer within {NET_TIMEOUT:.0f} s"
        return str(e) or e.__class__.__name__

    def _emit(self, direction: str, text: str):
        if self._log:
            self._log(direction, text)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def __repr__(self):
        return (f"{self.__class__.__name__}(host={self.host!r}, "
                f"port={self.port}, open={self._open})")


class FlrigPTT(_NetPTT):
    """PTT through flrig's XML-RPC server (rig.set_ptt / rig.get_ptt)."""

    KIND = "flrig"

    def __init__(self, host="127.0.0.1", port=FLRIG_DEFAULT_PORT, **kw):
        super().__init__(host, port, **kw)
        self._proxy = None

    def _connect(self):
        import xmlrpc.client

        class _T(xmlrpc.client.Transport):
            def make_connection(self, host):
                conn = super().make_connection(host)
                conn.timeout = NET_TIMEOUT
                return conn

        self._proxy = xmlrpc.client.ServerProxy(
            f"http://{self.host}:{self.port}/RPC2", transport=_T())
        self._rig = str(self._proxy.rig.get_xcvr() or "").strip()

    def _disconnect(self):
        p, self._proxy = self._proxy, None
        if p is not None:
            try:
                p("close")()
            except Exception:
                pass

    def _set_ptt(self, on: bool):
        self._proxy.rig.set_ptt(1 if on else 0)

    def _get_ptt(self):
        try:
            return bool(int(self._proxy.rig.get_ptt()))
        except (ValueError, TypeError):
            return None

    def _describe(self):
        # flrig can answer get_xcvr with "" (seen with 2.0.08 before the
        # rig had finished connecting), so a blank name is not an error.
        return f"  (rig: {self._rig})" if self._rig else ""


class RigctldPTT(_NetPTT):
    """PTT through Hamlib's rigctld (T 1 / T 0, t)."""

    KIND = "rigctld"

    def __init__(self, host="127.0.0.1", port=RIGCTLD_DEFAULT_PORT, **kw):
        super().__init__(host, port, **kw)
        self._sock = None
        self._buf = b""

    def _connect(self):
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=NET_TIMEOUT)
        self._buf = b""

    def _disconnect(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def _cmd(self, line: str) -> str:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.sendall(line.encode() + b"\n")
        while b"\n" not in self._buf:
            chunk = self._sock.recv(256)
            if not chunk:
                raise ConnectionError("rigctld closed the connection")
            self._buf += chunk
        reply, self._buf = self._buf.split(b"\n", 1)
        return reply.decode(errors="replace").strip()

    def _set_ptt(self, on: bool):
        reply = self._cmd(f"T {1 if on else 0}")
        if reply.startswith("RPRT") and reply != "RPRT 0":
            raise RuntimeError(f"rigctld refused PTT ({reply})")

    def _get_ptt(self):
        reply = self._cmd("t")
        if reply.startswith("RPRT"):
            return None
        try:
            return bool(int(reply))
        except ValueError:
            return None


# ── Factory ───────────────────────────────────────────────────────────────────

PTT_MODES = ("none", "com", "flrig", "rigctld")


def make_ptt(cfg: dict):
    """
    Build the PTT controller the config's "ptt" dict asks for, or None.

        {"mode": "com",     "port": "/dev/ttyUSB0", "signal": "rts"}
        {"mode": "flrig",   "flrig_host": "127.0.0.1",   "flrig_port": 12345}
        {"mode": "rigctld", "rigctld_host": "127.0.0.1", "rigctld_port": 4532}

    Not opened — the caller calls open() and checks is_open.
    """
    cfg = cfg or {}
    mode = (cfg.get("mode") or "none").lower()
    if mode == "flrig":
        return FlrigPTT(cfg.get("flrig_host") or "127.0.0.1",
                        int(cfg.get("flrig_port") or FLRIG_DEFAULT_PORT))
    if mode == "rigctld":
        return RigctldPTT(cfg.get("rigctld_host") or "127.0.0.1",
                          int(cfg.get("rigctld_port") or RIGCTLD_DEFAULT_PORT))
    if mode != "none" and cfg.get("port"):
        return PTTController(port=cfg["port"], mode=cfg.get("signal", "rts"))
    return None
