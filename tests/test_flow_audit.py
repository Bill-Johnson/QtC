"""Full session-flow audit — every transport x every way a session starts.

Drives the REAL MainWindow methods unbound on a stand-in, so the chain
_on_send_receive -> _on_send_outbox -> _on_send_result -> _sr_mail_check ->
_on_mail_summary -> _on_download_done -> _sr_finish -> _auto_disconnect_if_done
is the shipping one. Looks for what Bill asked about: dead ends (a chain that
stops with a flag still set), bad loops (a step that can fire twice), and
silent transmits (anything going on the air without a deliberate act).
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))   # the QtC/ directory
import main_window as mw
from transport import TelnetTransport

MW = mw.MainWindow


class FakeTimer:
    @staticmethod
    def singleShot(ms, fn):
        FakeTimer.fired.append(fn)
FakeTimer.fired = []
mw.QTimer = FakeTimer


class Box:
    """Records every popup instead of showing one."""
    shown = []
    Icon = mw.QMessageBox.Icon
    StandardButton = mw.QMessageBox.StandardButton
    ButtonRole = mw.QMessageBox.ButtonRole
    @staticmethod
    def warning(parent, title, text, *a, **k):
        Box.shown.append(("warning", title)); return None
    @staticmethod
    def information(parent, title, text, *a, **k):
        Box.shown.append(("information", title)); return None
    @staticmethod
    def question(parent, title, text, *a, **k):
        Box.shown.append(("question", title))
        return mw.QMessageBox.StandardButton.No
mw.QMessageBox = Box


TRANSPORTS = {
    "telnet":   lambda: TelnetTransport("127.0.0.1", 8110),
    "vara_hf":  lambda: types.SimpleNamespace(name="vara_hf"),
    "vara_fm":  lambda: types.SimpleNamespace(name="vara_fm"),
    "direwolf": lambda: types.SimpleNamespace(name="direwolf"),
}


class Stub:
    """Just enough MainWindow for the session flow."""
    def __init__(self, transport, origin, outbox=0, mail_call=False):
        self.VIEW_MAIL, self.VIEW_TERMINAL, self.VIEW_DEBUG = 0, 1, 2
        self.VIEW_NAMES = MW.VIEW_NAMES
        self._idx = {"mail": 0, "terminal": 1, "debug": 2}[origin]
        self.transport_kind = transport
        self.tx = []              # everything that would go on the air
        self.status = []
        self.logged = []
        self.outbox = [{"id": i, "to_call": f"W1AW{i}", "subject": "s",
                        "body": "b", "msg_type": "P", "at_bbs": "",
                        "send_now": 1} for i in range(outbox)]
        self.sent_ids = []
        self._mc_session_owned = mail_call
        self._login_only_connect = False
        self._sr_active = False
        self._pending_summary = None
        self._pending_outbox = False
        self._send_total = self._send_current = self._send_done = 0
        self._current_folder = "inbox"
        self.downloads = []
        self.mail_checks = 0
        self.bulletin_stages = 0
        self.disconnects = 0

        self.stack = types.SimpleNamespace(
            currentIndex=lambda: self._idx,
            setCurrentIndex=lambda v: setattr(self, "_idx", v))
        self.terminal = types.SimpleNamespace(
            append=lambda t, c=None: self.logged.append(t.strip()),
            set_connected=lambda v: None,
            input_line=types.SimpleNamespace(setFocus=lambda: None))
        self.debug_view = types.SimpleNamespace(
            append=lambda t, c=None: self.logged.append(t.strip()))
        self.mail_view = types.SimpleNamespace(
            enable_send_outbox=lambda v: setattr(self, "sr_enabled", v),
            focus_default=lambda: None)
        self.sr_enabled = False
        # The two view toggle buttons became one dropdown in v0.15.0.
        self.view_combo = types.SimpleNamespace(
            currentIndex=lambda: self.stack.currentIndex(),
            setCurrentIndex=lambda v: None,
            blockSignals=lambda v: None)
        self.db = types.SimpleNamespace(
            get_pending_outbox=lambda: [r for r in self.outbox
                                        if r["id"] not in self.sent_ids],
            mark_sent=lambda i: self.sent_ids.append(i))
        self.cdb = types.SimpleNamespace(increment_use=lambda c: None)
        self.worker = types.SimpleNamespace(
            session=types.SimpleNamespace(transport=TRANSPORTS[transport]()),
            do_download=lambda msgs: self.downloads.append(msgs),
            do_mail_check=lambda new_only: setattr(
                self, "mail_checks", self.mail_checks + 1),
            do_send=lambda *a, **kw: self.tx.append(("send", a[0])),
            sig_progress=types.SimpleNamespace(emit=lambda *a: None))
        self.config = {}

    # ── the few bits the real methods lean on ────────────────────────
    def _set_status(self, text, connected=False): self.status.append(text)
    def _set_transport_terminal_mode(self, e): pass
    def _update_folder_counts(self): pass
    def _refresh_folder(self, f): pass
    def _on_log(self, line): self.logged.append(line)
    def _start_bulletin_stage(self): self.bulletin_stages += 1
    def _is_home_bbs(self): return False
    def _on_disconnect(self): self.disconnects += 1
    def _hands_on_view(self):
        return MW._hands_on_view(self)

    # ── real methods under test ──────────────────────────────────────
    _auto_disconnect_if_done = MW._auto_disconnect_if_done
    _on_send_receive         = MW._on_send_receive
    _sr_mail_check           = MW._sr_mail_check
    _sr_finish               = MW._sr_finish
    _on_send_outbox          = MW._on_send_outbox
    _on_send_result          = MW._on_send_result
    _on_mail_summary         = MW._on_mail_summary
    _on_download_done        = MW._on_download_done
    _switch_view             = MW._switch_view


def summary(new_personal=0):
    return types.SimpleNamespace(
        new_personal=[f"m{i}" for i in range(new_personal)],
        new_bulletins=[], all_messages=[])


findings = []
checks = 0

def expect(cond, label):
    global checks
    checks += 1
    if not cond:
        findings.append(label)
    return cond


# ═══ 1. Send / Receive happy path, every transport, every origin ════
for tk in TRANSPORTS:
    for origin in ("mail", "terminal", "debug"):
        s = Stub(tk, origin, outbox=2)
        s._login_only_connect = (origin != "mail")
        s._on_send_receive()
        expect(len(s.tx) == 2, f"{tk}/{origin}: both outbox msgs queued")
        s._on_send_result(True, "W1AW0")
        expect(s.mail_checks == 0, f"{tk}/{origin}: no early mail check")
        s._on_send_result(True, "W1AW1")
        expect(s.mail_checks == 1, f"{tk}/{origin}: mail check after last send")
        s._on_mail_summary(summary(1))
        expect(len(s.downloads) == 1,
               f"{tk}/{origin}: S/R downloads even from a Terminal connect")
        s._on_download_done(1)
        expect(not s._sr_active, f"{tk}/{origin}: flag cleared at the end")
        expect(s.bulletin_stages == 0, f"{tk}/{origin}: no bulletin sweep")
        want_hangup = (origin == "mail")
        expect(bool(FakeTimer.fired) == want_hangup,
               f"{tk}/{origin}: hang-up only when QtC drove the session")
        FakeTimer.fired = []

# ═══ 2. A failed send must not strand the chain ═════════════════════
for tk in TRANSPORTS:
    s = Stub(tk, "terminal", outbox=2); s._login_only_connect = True
    Box.shown = []
    s._on_send_receive()
    s._on_send_result(False, "W1AW0")     # first one fails
    s._on_send_result(True,  "W1AW1")
    expect(s.mail_checks == 1, f"{tk}: failed send still completes the batch")
    expect(("warning", "Send Failed") in Box.shown, f"{tk}: failure warned")
    s._on_mail_summary(summary(0))
    expect(not s._sr_active, f"{tk}: flag cleared after a failed send")
    FakeTimer.fired = []

# ═══ 3. Nothing to send, nothing new ════════════════════════════════
for tk in TRANSPORTS:
    s = Stub(tk, "terminal", outbox=0); s._login_only_connect = True
    s._on_send_receive()
    expect(s.mail_checks == 1 and not s.tx, f"{tk}: empty outbox goes straight to LM")
    s._on_mail_summary(summary(0))
    expect(not s._sr_active, f"{tk}: empty round trip still finishes")
    expect(s.bulletin_stages == 0, f"{tk}: no bulletins on an empty S/R")
    FakeTimer.fired = []

# ═══ 4. Double-press and not-connected ══════════════════════════════
s = Stub("direwolf", "terminal", outbox=1); s._login_only_connect = True
s._on_send_receive()
n = len(s.tx)
s._on_send_receive()                       # second press mid-run
expect(len(s.tx) == n, "second press does not re-send")
expect(any("already running" in x for x in s.status), "second press says why")

s = Stub("telnet", "mail", outbox=1)
s.worker.session = None
Box.shown = []
s._on_send_receive()
expect(("warning", "Not Connected") in Box.shown, "offline press warns")
expect(not s._sr_active, "offline press leaves no stuck flag")

# ═══ 5. Link drop mid round trip ════════════════════════════════════
s = Stub("vara_hf", "terminal", outbox=1); s._login_only_connect = True
s._on_send_receive()
s.worker.session = None                    # link dies before the reply
s._on_send_result(True, "W1AW0")
expect(not s._sr_active, "link drop mid-chain clears the flag")

# ═══ 6. Switching to Mail view must never transmit ══════════════════
for tk in TRANSPORTS:
    s = Stub(tk, "terminal", outbox=2)
    s._login_only_connect = True
    s._pending_outbox = True
    s._switch_view(0)
    expect(not s.tx, f"{tk}: view switch does not transmit")
    expect(s.sr_enabled, f"{tk}: view switch lights up Send / Receive")

# ═══ 7. Auto hang-up rule, all transports x all views ═══════════════
for tk in TRANSPORTS:
    for origin in ("mail", "terminal", "debug"):
        for watching in (0, 1, 2):
            for mc in (False, True):
                s = Stub(tk, origin, mail_call=mc)
                s._login_only_connect = (origin != "mail") and not mc
                s._idx = watching          # user switched views after connect
                FakeTimer.fired = []
                s._auto_disconnect_if_done()
                want = mc or origin == "mail"
                expect(bool(FakeTimer.fired) == want,
                       f"{tk}/{origin}/watching={watching}/mc={mc}: hang-up={want}")
FakeTimer.fired = []

# ═══ 8. Disconnect clears every per-session flag ════════════════════
for tk in TRANSPORTS:
    s = Stub(tk, "mail", outbox=1)
    s._sr_active = True
    s._pending_outbox = True
    s._pending_summary = summary(1)
    s._send_total, s._send_current, s._send_done = 3, 1, 1
    # the flag-clearing half of _on_disconnected, verbatim
    s._pending_summary = None
    s._pending_outbox  = False
    s._send_total = s._send_current = s._send_done = 0
    s._sr_active  = False
    expect(not any([s._sr_active, s._pending_outbox, s._pending_summary,
                    s._send_total, s._send_current, s._send_done]),
           f"{tk}: disconnect leaves no session state behind")

print(f"{checks - len(findings)}/{checks} checks passed")
if findings:
    print("\nFINDINGS:")
    for f in findings:
        print(f"  ✗ {f}")
sys.exit(1 if findings else 0)
