"""Per-modem VARA-protocol profiles (2026-11-14).

Mercury, Data2G and VardeFM speak VARA's TCP interface but need their
own timing: Mercury's slowest frames key ~27 s and it calls for up to
300 s, so VARA's 90 s connect / 20 s login nudge / 60 s disconnect cut
it off (seen on air). Checks:

  * VARA HF / FM keep exactly the values QtC always used
  * a Mercury session gets its connect timeout, reply floor, nudge floor,
    disconnect wait and its name in messages
  * BBSSession floors reply waits and the login 'A' nudge by the profile
  * VardeFM sends no bandwidth command at all
  * host/port: a modem's own keys, else VARA HF's
  * the Experimental switch decides which modems are offered, and an
    entry already saved with one still shows
"""
import os, sys, socket, threading, time, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from transport import VARA_MODEMS, VaraTransport, vara_address
from bbs_session import BBSSession
import main_window as mw
from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

fails = 0
total = 0
def check(name, cond, detail=""):
    global fails, total
    total += 1
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {detail}" if detail and not cond else ""))


# 1. VARA unchanged
for k in ("vara_hf", "vara_fm"):
    p = VARA_MODEMS[k]
    check(f"{k}: connect 90 / reply floor 0 / nudge 0 / disconnect 60",
          (p["connect_timeout"], p["min_reply_wait"], p["min_nudge_wait"],
           p["disconnect_wait"]) == (90, 0, 0, 60))
    check(f"{k}: not experimental", not p["experimental"])
check("VARA HF bandwidths unchanged",
      VARA_MODEMS["vara_hf"]["bw"] == ["500", "2300"])
check("VARA FM bandwidths unchanged",
      VARA_MODEMS["vara_fm"]["bw"] == ["NARROW", "WIDE"])
t = VaraTransport("127.0.0.1", 1, 2, "N0CALL", "N0BBS", timeout=90,
                  bandwidth="500", vara_type="hf")
check("no profile = old VARA behaviour",
      (t.timeout, t.MIN_REPLY_WAIT, t.MIN_NUDGE_WAIT, t.disconnect_wait,
       t.modem_name) == (90, 0, 0, 60, "VARA"))

# 2. Mercury profile applied
t = VaraTransport("127.0.0.1", 1, 2, "N0CALL", "N0BBS", bandwidth="500",
                  profile=VARA_MODEMS["mercury"])
check("Mercury connect timeout outlasts its 300 s of calling",
      t.timeout > 300, f"got {t.timeout}")
check("Mercury reply floor exceeds a 27 s frame plus turnaround",
      t.MIN_REPLY_WAIT >= 60, f"got {t.MIN_REPLY_WAIT}")
check("Mercury nudge floor exceeds the 29 s banner gap seen on air",
      t.MIN_NUDGE_WAIT > 29, f"got {t.MIN_NUDGE_WAIT}")
check("Mercury disconnect wait is minutes", t.disconnect_wait >= 180)
check("Mercury name used in messages", t.modem_name == "Mercury")
check("Mercury BW style is HF (BW500)", t.vara_type == "hf")
for k, p in VARA_MODEMS.items():
    if p["experimental"]:
        check(f"{k} is experimental and has every timing field",
              all(f in p for f in ("connect_timeout", "min_reply_wait",
                                   "min_nudge_wait", "disconnect_wait")))

# 3. BBSSession uses the floors
s = BBSSession(t, mycall="N0CALL")
check("reply wait floored to Mercury's", s._reply_wait(10) == t.MIN_REPLY_WAIT)
sv = BBSSession(VaraTransport("127.0.0.1", 1, 2, "N0CALL", "N0BBS",
                              profile=VARA_MODEMS["vara_hf"]), mycall="N0CALL")
check("reply wait for VARA unchanged", sv._reply_wait(10) == 10)

def nudge_used(transport):
    seen = {}
    fake = types.SimpleNamespace(connect=lambda: None,
                                 MIN_NUDGE_WAIT=getattr(transport, "MIN_NUDGE_WAIT", 0),
                                 MIN_REPLY_WAIT=getattr(transport, "MIN_REPLY_WAIT", 0),
                                 modem_name="x")
    ss = BBSSession(fake, mycall="N0CALL")
    def capture(quiet, nudge):
        seen["q"], seen["n"] = quiet, nudge
        return "de N0BBS>"
    ss._await_bbs_prompt = capture
    ss._rf_login()
    return seen
n = nudge_used(t)
check("login nudge floored for Mercury", n["n"] == t.MIN_NUDGE_WAIT,
      f"got {n}")
check("login quiet floored for Mercury", n["q"] == max(60, t.MIN_REPLY_WAIT))
n = nudge_used(sv.transport)
check("login nudge for VARA still 20 s", n["n"] == 20, f"got {n}")


# 4. VardeFM sends no BW command
class Modem:
    """Answers OK to everything, CONNECTED to CONNECT; records lines."""
    def __init__(self):
        self.lines = []
        self.ls = [socket.socket() for _ in range(2)]
        for l in self.ls:
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", 0)); l.listen(2)
        self.cmd, self.data = (l.getsockname()[1] for l in self.ls)
        threading.Thread(target=self._c, daemon=True).start()
        threading.Thread(target=lambda: self.ls[1].accept(), daemon=True).start()
    def _c(self):
        c, _ = self.ls[0].accept(); buf = b""
        try:
            self._serve(c, buf)
        except OSError:
            pass                   # the test hung up on us — expected
    def _serve(self, c, buf):
        while (b := c.recv(512)):
            buf += b
            while b"\r" in buf:
                ln, buf = buf.split(b"\r", 1); self.lines.append(ln.decode())
                c.sendall(b"OK\r")
                if ln.startswith(b"CONNECT "):
                    a = ln.decode().split()
                    c.sendall(f"CONNECTED {a[1]} {a[2]} NARROW\r".encode())

m = Modem()
tv = VaraTransport("127.0.0.1", m.cmd, m.data, "N0CALL", "N0BBS-1",
                   bandwidth="", profile=VARA_MODEMS["vardefm"])
tv._log = lambda *a: None
tv.connect()
check("VardeFM connects", tv.connected)
check("VardeFM: no bandwidth command sent",
      not any(l.startswith(("BW", "NARROW", "WIDE")) for l in m.lines),
      f"got {m.lines}")
check("VardeFM: MYCALL then CONNECT", [l.split()[0] for l in m.lines] == ["MYCALL", "CONNECT"])
tv.abort()

# 5. host / port
cfg = {"hf_host": "127.0.0.1", "hf_cmd_port": 8400, "hf_data_port": 8401}
check("Mercury falls back to VARA HF's ports",
      vara_address(cfg, "mercury") == ("127.0.0.1", 8400, 8401))
cfg2 = dict(cfg, mercury_cmd_port=8500)
check("Mercury's own port wins, data = cmd + 1",
      vara_address(cfg2, "mercury") == ("127.0.0.1", 8500, 8501))
check("VARA FM keeps its fm_ keys",
      vara_address({"fm_cmd_port": 8310}, "vara_fm")[1] == 8310)
check("empty config -> 8300/8301",
      vara_address({}, "data2g") == ("127.0.0.1", 8300, 8301))

# 6. Experimental switch
off = {"app": {"experimental_modems": False}}
on  = {"app": {"experimental_modems": True}}
check("switch off: only VARA HF / FM offered",
      mw.vara_modem_names(off) == ["VARA HF", "VARA FM"])
check("switch on: Mercury, Data2G, VardeFM offered too",
      mw.vara_modem_names(on) == ["VARA HF", "VARA FM", "Mercury", "Data2G", "VardeFM"])
check("switch off, entry saved with Mercury: Mercury still shown",
      "Mercury" in mw.vara_modem_names(off, extra="Mercury"))

parent_off = types.SimpleNamespace(_cfg=off)
parent_on  = types.SimpleNamespace(_cfg=on)
D = mw._BBSEntryDialog
keys = lambda d: [k for _, k in d.TRANSPORTS]
check("entry dialog, switch off: no experimental modems",
      not {"mercury", "data2g", "vardefm"} & set(keys(D())))
d_on = D(parent=None)
check("entry dialog keeps Telnet and packet",
      {"telnet", "direwolf", "soundmodem"} <= set(keys(d_on)))
d_merc = D(entry={"transport": "mercury", "callsign": "N0BBS-1", "bw": "500"})
check("editing a Mercury entry with the switch off still offers Mercury",
      "mercury" in keys(d_merc) and d_merc.get_entry()["transport"] == "mercury")
d_v = D(entry={"transport": "vardefm", "callsign": "N0BBS-1"})
check("VardeFM entry: bandwidth box disabled and empty",
      not d_v.e_bw.isEnabled() and d_v.get_entry()["bw"] == "")
check("VardeFM entry saves vara_type 'none'",
      d_v.get_entry()["vara_type"] == "none")

# 7. Per-entry modem host / port (2026-11-14)
from transport import entry_vara_address, VaraControl
vcfg = {"hf_host": "127.0.0.1", "hf_cmd_port": 8300, "hf_data_port": 8301}
check("old entry (no address saved) uses config.json's",
      entry_vara_address(vcfg, {"transport": "vara_hf"}) == ("127.0.0.1", 8300, 8301))
check("entry's own port wins; data = cmd + 1",
      entry_vara_address(vcfg, {"transport": "mercury", "vara_cmd_port": 8400})
      == ("127.0.0.1", 8400, 8401))
check("entry's own host wins",
      entry_vara_address(vcfg, {"transport": "data2g", "vara_host": "192.0.2.10",
                                "vara_cmd_port": "8500"}) == ("192.0.2.10", 8500, 8501))
check("blank saved fields fall back",
      entry_vara_address(vcfg, {"transport": "vara_hf", "vara_host": " ",
                                "vara_cmd_port": None}) == ("127.0.0.1", 8300, 8301))

from PyQt6.QtWidgets import QWidget
parent = QWidget()          # a dialog's parent must be a real widget
parent._cfg = {"vara": vcfg, "app": {"experimental_modems": True}}
d = D(entry={"transport": "mercury", "callsign": "N0BBS-9", "bw": "500"}, parent=parent)
check("dialog prefills host 127.0.0.1 and port from config",
      (d.e_vara_host.text(), d.e_vara_port.text()) == ("127.0.0.1", "8300"))
check("dialog shows the data port", d.l_vara_data.text() == "data port 8301",
      d.l_vara_data.text())
d.e_vara_port.setText("8400")
check("data port hint follows", d.l_vara_data.text() == "data port 8401")
got = d.get_entry()
check("dialog saves vara_host / vara_cmd_port",
      (got["vara_host"], got["vara_cmd_port"]) == ("127.0.0.1", 8400), str(got))
d2 = D(entry=dict(got), parent=parent)
check("saved address survives reopen",
      (d2.e_vara_host.text(), d2.e_vara_port.text()) == ("127.0.0.1", "8400"))
dt = D(entry={"transport": "telnet", "callsign": "N0BBS", "host": "bbs.example"},
       parent=parent)
check("Telnet entry saves no modem address",
      (dt.get_entry()["vara_host"], dt.get_entry()["vara_cmd_port"]) == ("", None))

# VaraControl.retarget
vc = VaraControl("127.0.0.1", 8300, 8301)
check("retarget to same address does nothing", vc.retarget("127.0.0.1", 8300, 8301) is False)
check("retarget to a new address moves",
      vc.retarget("127.0.0.1", 8400, 8401)
      and (vc.vara_host, vc.cmd_port, vc.data_port) == ("127.0.0.1", 8400, 8401))
vc.lend()
check("retarget refused while a session has the ports",
      vc.retarget("127.0.0.1", 8500, 8501) is False and vc.cmd_port == 8400)
vc.close()

# MainWindow._vara_retarget follows the active entry, idle only
def rt_stub(entry, connect_enabled=True):
    st = types.SimpleNamespace(moves=[], opened=[], config={"vara": vcfg},
                               _vara_was_linked=True)
    st._vara_ctrl = types.SimpleNamespace(
        retarget=lambda h, c, dd: st.moves.append((h, c, dd)) or True,
        open=lambda: st.opened.append(1))
    st.btn_connect = types.SimpleNamespace(isEnabled=lambda: connect_enabled)
    st._get_active_bbs_entry = lambda: entry
    st.debug_view = types.SimpleNamespace(append=lambda *a: None)
    return st
st = rt_stub({"transport": "mercury", "vara_cmd_port": 8400})
mw.MainWindow._vara_retarget(st)
check("idle: link moves to the entry's modem and reopens",
      st.moves == [("127.0.0.1", 8400, 8401)] and st.opened == [1]
      and st._vara_was_linked is False)
st = rt_stub({"transport": "mercury", "vara_cmd_port": 8400}, connect_enabled=False)
mw.MainWindow._vara_retarget(st)
check("connecting / connected: link left alone", st.moves == [])
st = rt_stub({"transport": "telnet", "host": "x"})
mw.MainWindow._vara_retarget(st)
check("Telnet entry: modem link left alone", st.moves == [])

print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
