"""H1 — Mail-Call demands the RF responsibility acceptance for any transport
that keys a radio, and never for Telnet.

Direwolf was added after the acceptance was written, so the question is
whether a packet entry is treated like VARA (it keys a radio unattended) or
slipped through like Telnet (it does not). Drives the real predicates.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw

SD = mw.SettingsDialog
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}" +
          (f"   {detail}" if detail and not cond else ""))


# ── Which transports must ask? ───────────────────────────────────────
# Anything that keys a radio unattended. Telnet has no PTT, no antenna and
# no RF safety surface, so it is the only exemption.
CASES = [
    ("direwolf",   "rf",     "packet keys the radio, same as VARA"),
    ("soundmodem", "rf",     "the other AGW modem, when it lands"),
    ("vara_hf",    "rf",     "the original case"),
    ("vara_fm",    "rf",     "the original case"),
    ("telnet",     "telnet", "no PTT, no antenna — exempt"),
]
for transport, want, why in CASES:
    got = SD._mc_transport_class({"transport": transport})
    check(f"H1 {transport:<10} -> {want:<6} ({why})", got == want, f"got {got!r}")

# ── Fail safe on anything unrecognised ───────────────────────────────
# A transport added later, or a config with the key missing, must land on
# the ASK side. Being asked once too often is harmless; keying a radio
# unattended without having accepted is not.
for entry, label in (({}, "no transport key at all"),
                     ({"transport": ""}, "empty transport"),
                     ({"transport": "something_new"}, "a transport from the future")):
    got = SD._mc_transport_class(entry)
    check(f"H1 {label} -> rf (fails safe)", got == "rf", f"got {got!r}")

# ── The acceptance flag is version-gated ─────────────────────────────
V = SD.MAILCALL_RESPONSIBILITY_VERSION
for mc, want, label in (
    ({"responsibility_accepted_rf_version": V},     True,  "current version accepted"),
    ({"responsibility_accepted_rf_version": V - 1}, False, "an older version re-prompts"),
    ({"responsibility_accepted_rf_version": 0},     False, "never accepted"),
    ({},                                            False, "key missing entirely"),
    ({"responsibility_accepted_rf_version": str(V)}, True, "stored as a string still counts"),
):
    stub = types.SimpleNamespace(
        _cfg={"mail_call": mc},
        MAILCALL_RESPONSIBILITY_VERSION=V)
    got = SD._mc_already_accepted_rf(stub)
    check(f"H1 {label} -> {'accepted' if want else 'must ask'}",
          got == want, f"got {got!r}")

# A non-numeric value must not crash the check
stub = types.SimpleNamespace(_cfg={"mail_call": {"responsibility_accepted_rf_version": "yes"}},
                             MAILCALL_RESPONSIBILITY_VERSION=V)
try:
    SD._mc_already_accepted_rf(stub)
    check("H1 a junk acceptance value does not crash the check", False,
          "it returned instead of raising — see note below")
except (ValueError, TypeError):
    check("H1 a junk acceptance value raises rather than silently accepting", True)

total = len(CASES) + 3 + 5 + 1
print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
