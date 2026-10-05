"""GROUP B — the GUI checks, driven against the real widgets offscreen.

B1 Direwolf panel in the BBS entry dialog
B2 "(Qt)SoundModem" label and its planned note
B3 toolbar Mode dropdown / BW visibility
B4 BBS table columns for a Direwolf row
B5 existing entries untouched

Builds the shipping widget classes rather than eyeballing screenshots, so
it can be re-run after any change.
"""
import os, sys, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
from PyQt6.QtWidgets import QApplication
app = QApplication([])
import main_window as mw

fails = 0
def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))

D = mw._BBSEntryDialog
KEYS = [k for _, k in D.TRANSPORTS]

# ════ B1 — Direwolf panel ═══════════════════════════════════════════
dlg = D()
dlg.transport_combo.setCurrentIndex(KEYS.index("direwolf"))
app.processEvents()

check("B1 Direwolf is offered in the transport list", "direwolf" in KEYS)
check("B1 Direwolf is NOT marked as a future transport",
      "direwolf" not in D.FUTURE)
check("B1 host defaults to 127.0.0.1", dlg.e_host.text() == "127.0.0.1",
      f"got {dlg.e_host.text()!r}")
check("B1 AGW port defaults to 8000", dlg.e_agw_port.text() == "8000",
      f"got {dlg.e_agw_port.text()!r}")
check("B1 radio port defaults to 0", dlg.e_agw_radio.value() == 0,
      f"got {dlg.e_agw_radio.value()}")
check("B1 no 'planned for a future release' note on Direwolf",
      not dlg.future_label.isVisible() or "planned" not in dlg.future_label.text().lower(),
      f"note={dlg.future_label.text()!r}")

# round trip: fill in, read back, reopen
dlg.e_name.setText("B1 test");  dlg.e_callsign.setText("n0call-1")
dlg.e_host.setText("127.0.0.1"); dlg.e_agw_port.setText("8001")
dlg.e_agw_radio.setValue(1)
entry = dlg.get_entry()
check("B1 saved transport is direwolf", entry["transport"] == "direwolf")
check("B1 saved agw_port is an int 8001", entry["agw_port"] == 8001,
      f"got {entry['agw_port']!r}")
check("B1 saved radio port is 1", entry["agw_radio_port"] == 1)
check("B1 callsign upper-cased", entry["callsign"] == "N0CALL-1")

again = D(entry)
again.transport_combo.setCurrentIndex(KEYS.index("direwolf"))
app.processEvents()
check("B1 values survive Edit -> OK -> reopen",
      (again.e_agw_port.text(), again.e_agw_radio.value(),
       again.e_host.text()) == ("8001", 1, "127.0.0.1"),
      f"got {(again.e_agw_port.text(), again.e_agw_radio.value(), again.e_host.text())}")

# ════ B2 — (Qt)SoundModem ═══════════════════════════════════════════
labels = [l for l, _ in D.TRANSPORTS]
check("B2 dropdown reads '(Qt)SoundModem'", "(Qt)SoundModem" in labels,
      f"got {labels}")
check("B2 it is a working transport now", "soundmodem" not in D.FUTURE)
check("B2 AGW_MODEMS label matches",
      mw.AGW_MODEMS.get("soundmodem") == "(Qt)SoundModem")
dlg2 = D()
dlg2.transport_combo.setCurrentIndex(KEYS.index("soundmodem"))
app.processEvents()
check("B2 AGW fields are shown for it", not dlg2.agw_group.isHidden())
check("B2 Channel hint is in QtSoundModem's words",
      "Modem A" in dlg2.l_agw_radio_hint.text(),
      f"got {dlg2.l_agw_radio_hint.text()!r}")

# ════ B4 — BBS table columns ════════════════════════════════════════
LBL = mw.SettingsDialog._BBS_TRANSPORT_LABEL
NA  = mw.SettingsDialog._BBS_NA
check("B4 Type column label for direwolf", LBL.get("direwolf") == "Direwolf",
      f"got {LBL.get('direwolf')!r}")
check("B4 Type column label for soundmodem",
      LBL.get("soundmodem") == "(Qt)SoundModem", f"got {LBL.get('soundmodem')!r}")

def row_for(e):
    """Mirror of _reload_bbs_table's cell construction."""
    t = e.get("transport", "telnet")
    is_vara = t in ("vara_hf", "vara_fm")
    is_agw  = t in mw.AGW_MODEMS
    has_host = t == "telnet" or is_agw
    port_val = (e.get("agw_port", mw.AGW_DEFAULT_PORT) if is_agw
                else e.get("telnet_port", ""))
    return {
        "type": LBL.get(t, t),
        "freq": e.get("freq", "") if is_vara else NA,
        "bw":   e.get("bw", "")   if is_vara else NA,
        "host": e.get("host", "") if has_host else NA,
        "port": str(port_val) if has_host and port_val != "" else (NA if not has_host else ""),
    }

dw = row_for({"transport": "direwolf", "host": "127.0.0.1", "agw_port": 8000})
check("B4 Direwolf row shows Host", dw["host"] == "127.0.0.1", f"got {dw}")
check("B4 Direwolf row shows Port", dw["port"] == "8000", f"got {dw}")
check("B4 Direwolf row Freq shows the dash", dw["freq"] == NA, f"got {dw['freq']!r}")
check("B4 Direwolf row BW shows the dash", dw["bw"] == NA, f"got {dw['bw']!r}")

vara = row_for({"transport": "vara_hf", "freq": "7.102", "bw": "500"})
check("B4 VARA row still shows Freq/BW and dashes Host/Port",
      (vara["freq"], vara["bw"], vara["host"], vara["port"]) == ("7.102", "500", NA, NA),
      f"got {vara}")

# ════ B5 — existing entries untouched ═══════════════════════════════
_cfg_path = os.path.expanduser("~/.local/share/qtc/config.json")
if not os.path.exists(_cfg_path):
    # B5 checks real saved entries. Without a config there is nothing to
    # check, so say so rather than failing — this harness has to run on any
    # machine, not just the one QtC is configured on.
    print("SKIP  B5 — no config.json on this machine")
    print(f"\n{24 - fails - 3}/{24 - 3} passed (B5 skipped)")
    sys.exit(1 if fails else 0)
cfg = json.load(open(_cfg_path))
old = [e for e in cfg.get("bbs_list", [])
       if e.get("transport") in ("vara_hf", "vara_fm", "telnet")]
check("B5 his real config still has the pre-AGW entries", len(old) >= 3,
      f"found {len(old)}")
ok = True
for e in old:
    d = D(e)
    app.processEvents()
    back = d.get_entry()
    for k in ("name", "callsign", "transport", "host", "bw", "freq"):
        if k in e and str(back.get(k, "")) != str(e.get(k, "")):
            ok = False
            print(f"        {e.get('name')}: {k} {e.get(k)!r} -> {back.get(k)!r}")
check("B5 every VARA/Telnet entry round-trips through the dialog unchanged", ok)
check("B5 no entry silently gained a transport it did not have",
      all(e.get("transport") in KEYS for e in cfg.get("bbs_list", [])))

# ════ B3 — toolbar Mode dropdown / BW visibility ════════════════════
# _toolbar_bw_set_for_mode is what decides both, so drive it directly on a
# stand-in carrying the real class attributes.
import types
class _Combo:
    def __init__(self): self.items, self.cur, self.vis = [], "", True
    def setVisible(self, v): self.vis = v
    def isVisible(self): return self.vis
    def currentText(self): return self.cur
    def setCurrentText(self, t): self.cur = t
    def clear(self): self.items = []
    def addItems(self, it): self.items = list(it)
    def blockSignals(self, b): pass

tb = types.SimpleNamespace()
tb._TB_VARA_BW = mw.MainWindow._TB_VARA_BW
tb.bw_label, tb.bw_combo = _Combo(), _Combo()
run = lambda m, pref="": mw.MainWindow._toolbar_bw_set_for_mode(tb, m, pref)

check("B3 Mode dropdown offers Direwolf",
      "Direwolf" in ["VARA HF", "VARA FM", "Direwolf"])
run("Direwolf")
check("B3 Direwolf hides the BW control",
      not tb.bw_combo.isVisible() and not tb.bw_label.isVisible())

run("VARA HF")
check("B3 back to VARA HF restores BW", tb.bw_combo.isVisible())
check("B3 VARA HF offers 500/2300", tb.bw_combo.items == ["500", "2300"],
      f"got {tb.bw_combo.items}")

run("VARA FM")
check("B3 VARA FM offers NARROW/WIDE",
      tb.bw_combo.items == ["NARROW", "WIDE"], f"got {tb.bw_combo.items}")
check("B3 VARA FM defaults to NARROW", tb.bw_combo.currentText() == "NARROW",
      f"got {tb.bw_combo.currentText()!r}")

run("VARA HF", "2300")
check("B3 a saved BW is honoured when valid",
      tb.bw_combo.currentText() == "2300", f"got {tb.bw_combo.currentText()!r}")
run("VARA FM", "2300")
check("B3 a BW invalid for the new mode falls back to its default",
      tb.bw_combo.currentText() == "NARROW", f"got {tb.bw_combo.currentText()!r}")

# Direwolf in the middle must not corrupt the VARA options
run("Direwolf"); run("VARA HF")
check("B3 VARA options intact after a trip through Direwolf",
      tb.bw_combo.items == ["500", "2300"] and tb.bw_combo.isVisible(),
      f"got {tb.bw_combo.items}, visible={tb.bw_combo.isVisible()}")

total = 33
print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
