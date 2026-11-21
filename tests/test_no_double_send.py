#!/usr/bin/env python3
"""An Outbox message must go to the BBS once, however the sends overlap.

Seen on air 2026-11-18: the Mail view connect asked
"1 message(s) waiting in outbox — send now?", Yes started the send, and
a Send / Receive press three seconds later queued the same message again
— its row stays 'pending' until the BBS confirms it. The BBS stored it
twice. Offscreen, throwaway data folder, fake worker.
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_tmp = tempfile.mkdtemp(prefix="qtc-dblsend-")
os.environ["XDG_DATA_HOME"] = _tmp
os.environ["APPDATA"] = _tmp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtWidgets import QApplication, QMessageBox
app = QApplication(sys.argv)
import main_window as mw
assert mw._APP_DIR.startswith(_tmp), mw._APP_DIR

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)


class _Sig:
    def emit(self, *a):
        pass


class FakeWorker:
    def __init__(self):
        self.session = object()
        self.sends = []
        self.mail_checks = 0
        self.sig_progress = _Sig()

    def do_send(self, to_call, subject, body, msg_type, at_bbs, index=1, total=1):
        self.sends.append((to_call, subject))

    def do_mail_check(self, new_only=True):
        self.mail_checks += 1


w = mw.MainWindow()
w._auto_disconnect_if_done = lambda: None
w.db.queue_outgoing(to_call="N0CALL", subject="Outbox test 1", body="hello", msg_type="P")


def fresh():
    w.worker = FakeWorker()
    w._sr_active = False
    w._sending_batch = False


# ── 1. connect's "send now?" then Send / Receive while it is sending ──
fresh()
w._on_send_outbox()                  # the connect flow, answered Yes
w._on_send_receive()                 # the press, 3 s later
check("message queued once", len(w.worker.sends) == 1, w.worker.sends)
check("Send / Receive joined the batch", w._sr_active)
w._on_send_result(True, "N0CALL")    # BBS confirms
check("then the mail check ran", w.worker.mail_checks == 1, w.worker.mail_checks)
check("outbox empty, one in Sent",
      len(w.db.get_pending_outbox()) == 0 and len(w.db.get_sent()) == 1)

# ── 2. Send / Receive first, then the Outbox button ──────────────────
w.db.queue_outgoing(to_call="N0CALL", subject="Outbox test 2", body="hello", msg_type="P")
fresh()
w._on_send_receive()
w._on_send_outbox()
check("S/R first, then Outbox: still once", len(w.worker.sends) == 1, w.worker.sends)
w._on_send_result(True, "N0CALL")
check("…mail check once", w.worker.mail_checks == 1, w.worker.mail_checks)

# ── 3. a link drop mid-send clears the flag ──────────────────────────
w.db.queue_outgoing(to_call="N0CALL", subject="Outbox test 3", body="hello", msg_type="P")
fresh()
w._on_send_outbox()
w._on_disconnected()
check("drop clears the sending flag", w._sending_batch is False)
w.worker = FakeWorker()
w._on_send_outbox()
check("the next send goes out", len(w.worker.sends) == 1, w.worker.sends)

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
