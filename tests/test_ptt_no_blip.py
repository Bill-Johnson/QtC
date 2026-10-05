"""Opening Settings or the PTT port must not key the radio (2026-10-04).

On the HP laptop every click on Settings gave a short PTT blip. The PTT
tab's port list opened and closed COM1..COM32 in turn, and opening a port
raises RTS and DTR on Windows — a Digirig keys on RTS. PTTController.open()
did a smaller version of the same: open with the lines up, then drop them.

A fake `serial` module records every open and the line states at that
moment, so this runs on any platform with no hardware.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory

fails = 0
def check(name, cond, extra=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {extra}" if extra and not cond else ""))

opens = []   # (port, rts, dtr) at the moment each port was opened

class FakeSerial:
    def __init__(self, port=None, **kw):
        self.port, self.rts, self.dtr, self.is_open = port, True, True, False
        for k, v in kw.items():
            setattr(self, k, v)
        if port is not None:          # pyserial opens at once when given a port
            self.open()
    def open(self):
        opens.append((self.port, self.rts, self.dtr))
        self.is_open = True
    def close(self):
        self.is_open = False

class Port:
    def __init__(self, device): self.device = device

fake = types.ModuleType("serial")
fake.Serial = FakeSerial
tools = types.ModuleType("serial.tools")
lp = types.ModuleType("serial.tools.list_ports")
lp.comports = lambda: [Port("COM10"), Port("COM7"), Port("COM3")]
fake.tools, tools.list_ports = tools, lp
sys.modules.update({"serial": fake, "serial.tools": tools,
                    "serial.tools.list_ports": lp})

import ptt

# ── 1. Port list on Windows opens nothing ───────────────────────────────
real_platform = sys.platform
sys.platform = "win32"
try:
    ports = ptt.list_serial_ports()
finally:
    sys.platform = real_platform
check("Windows port list opens no port (no RTS/DTR blip)", opens == [], str(opens))
check("it lists what Windows reports, COM3 before COM10",
      ports == ["COM3", "COM7", "COM10"], str(ports))

# ── 2. PTTController.open() has both lines low at the moment it opens ───
for mode in ("rts", "dtr", "rts+dtr"):
    opens.clear()
    c = ptt.PTTController("COM7", mode)
    c.open()
    check(f"PTT open ({mode}): one open, RTS and DTR already low",
          opens == [("COM7", False, False)], str(opens))
    check(f"PTT open ({mode}): port is usable afterwards", c.is_open)
    c.close()

# ── 3. Mode 'none' never touches a port ─────────────────────────────────
opens.clear()
ptt.PTTController("COM7", "none").open()
check("mode 'none' opens nothing", opens == [])

print(f"\nFAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
