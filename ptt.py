# QtC v0.15.0-beta — ptt.py  (built 2026-10-04)
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
import sys


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
