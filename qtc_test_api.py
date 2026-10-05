# QtC v0.15.0-beta — qtc_test_api.py  (built 2026-10-01)
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# Development / QA only. Not imported unless QtC is started with
# --test-api, and deliberately NOT listed in QtC.spec, so it never ships
# inside the Windows build.
#
# A small line-oriented control socket on 127.0.0.1 so the QA plan in
# qa_tests/ can be driven step by step instead of by hand. It presses the
# same buttons a person would — there is no back door into the BBS session,
# and nothing here bypasses a confirmation.
#
#   ⚠ SOME COMMANDS KEY THE TRANSMITTER (connect, type, click send_receive).
#     The licensed control operator must be present and must have checked
#     the modem and audio levels first. That is the operator's call, not
#     this script's. Every command is written into the Debug log as an
#     [API] line, so a saved log always shows what was driven and what was
#     typed by hand.

import json
import re
import socket
import threading
import queue
import time

from PyQt6.QtCore import QObject, pyqtSignal, Qt

API_HOST = "127.0.0.1"
API_PORT = 8787


class _Request:
    """One command in flight, with somewhere to put the answer."""
    __slots__ = ("verb", "args", "reply")

    def __init__(self, verb, args):
        self.verb = verb
        self.args = args
        self.reply = queue.Queue(maxsize=1)


class TestAPI(QObject):
    """Control socket. Socket threads never touch Qt objects directly:
    each request is handed to the GUI thread over a queued signal and the
    answer comes back through a per-request queue."""

    _sig_exec = pyqtSignal(object)

    def __init__(self, win, port=API_PORT):
        super().__init__()
        self.win = win
        self.port = port
        self._log = []                     # (timestamp, text) tee of the Debug view
        self._log_lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = None
        self._sig_exec.connect(self._on_exec, Qt.ConnectionType.QueuedConnection)

    # ── wiring ───────────────────────────────────────────────────────
    def start(self):
        self._tee_debug_log()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind((API_HOST, self.port))  # localhost only, never 0.0.0.0
        except OSError as e:
            # Almost always another QtC already holding the port. Say so in
            # one line instead of dying on a traceback from inside main()
            # after the window has already appeared (2026-09-20).
            raise RuntimeError(
                f"Cannot open the test API on {API_HOST}:{self.port} — {e}. "
                f"Another QtC is probably already running with --test-api; "
                f"use that one, or start this with --test-api=<other port>."
            ) from e
        self._srv.listen(4)
        threading.Thread(target=self._accept_loop, daemon=True,
                         name="qtc-test-api").start()
        self.win.setWindowTitle(self.win.windowTitle() + "   [TEST API]")
        self._note(f"[API] Test API listening on {API_HOST}:{self.port} — "
                   f"every command is logged here")

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass

    def _tee_debug_log(self):
        """Record everything the Debug view is told, so `wait` has something
        to match on without polling a growing QTextEdit."""
        original = self.win.debug_view.append

        def tee(text, color=None):
            with self._log_lock:
                self._log.append((time.time(), text))
                if len(self._log) > 4000:
                    del self._log[:1000]
            return original(text, color) if color is not None else original(text)

        self.win.debug_view.append = tee

    def _note(self, line):
        self.win.debug_view.append(line + "\n", "#ffaa55")

    # ── socket side ──────────────────────────────────────────────────
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        buf = b""
        # Per-connection read cursor into the tee'd log. A `wait` scans from
        # here, so it sees whatever the PREVIOUS command logged as well as
        # what arrives next — otherwise a script like
        #     view terminal
        #     wait View: Terminal
        # always times out, because the line landed before the wait began.
        # It moves forward ONLY when a wait matches, never over lines a wait
        # merely looked at, so a later step can still find an event that
        # happened before the one it just waited for. Two waits on the same
        # pattern still mean two separate events, because a match consumes
        # the line it matched.
        with self._log_lock:
            cursor = [len(self._log)]
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    out = self._handle(text, cursor)
                    try:
                        conn.sendall(
                            (json.dumps(out, default=str) + "\n").encode())
                    except OSError:
                        return

    def _handle(self, text, cursor):
        parts = text.split(None, 1)
        verb = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # `wait` and `seen` run on the socket thread — they only read the
        # tee'd log, so there is no need to cross to the GUI thread.
        if verb == "wait":
            return self._wait(rest, cursor)
        if verb == "seen":
            return self._wait(rest, cursor, anywhere=True)
        if verb == "pause":
            # pause <seconds> — let the FAR END settle. A BBS does not
            # finish tearing a session down the instant it says goodbye:
            # reconnecting ~1 s after `b` lands in the old session's
            # teardown and the banner comes back as
            # "Username:*** Disconnected from Stream 1" (2026-09-23,
            # found running d13cd). Handled here, on the socket thread —
            # sleeping on the GUI thread would freeze the window.
            try:
                secs = min(float(rest.strip() or 2), 30.0)
            except ValueError:
                return {"ok": False, "error": "usage: pause <seconds>"}
            time.sleep(secs)
            return {"ok": True, "paused": secs}

        req = _Request(verb, rest)
        self._sig_exec.emit(req)
        try:
            return req.reply.get(timeout=30)
        except queue.Empty:
            return {"ok": False, "error": "GUI thread did not answer in 30 s"}

    def _wait(self, rest, cursor, anywhere=False):
        """Two related questions, deliberately kept apart:

        wait <regex> [seconds]
            "tell me when this NEXT happens." Scans forward from this
            connection's cursor and, on a match, moves the cursor past it.
            Two waits on the same pattern therefore mean two real events.

        seen <regex> [seconds]
            "has this happened AT ALL yet?" Searches the whole session log
            from the start and never moves the cursor. Use it for an event
            that may already have gone by.

        The difference is not academic. On air, the outbox notice is logged
        the moment the link comes up, but "de N0CALL>" only arrives ~17 s
        later. A step file that waits for the prompt and then asks about
        the notice is asking about the PAST, and `wait` will never find it
        because the prompt match dragged the cursor past it. That failed
        D16 leg 1 on 2026-09-20 and the test had actually passed."""
        args = rest.rsplit(None, 1)
        if len(args) == 2 and _is_number(args[1]):
            pattern, timeout = args[0], float(args[1])
        else:
            pattern, timeout = rest, 60.0
        if not pattern:
            return {"ok": False, "error": "usage: wait <regex> [seconds]"}
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"ok": False, "error": f"bad regex: {e}"}

        started = time.time()
        deadline = started + timeout
        # `scan` is local to THIS wait so the poll loop doesn't re-read the
        # same lines every 150 ms. The shared cursor advances ONLY on a
        # match. That distinction matters: events do not arrive in the order
        # a test asks about them. On air, the outbox notice is logged the
        # instant the link comes up, while "de N0CALL>" only arrives ~17 s
        # later — so a step file that waits for the prompt and THEN checks
        # the notice was looking at a line the earlier wait had already
        # scanned past. Advancing the shared cursor over non-matches made
        # that line unreachable and failed a test that had actually passed
        # (2026-09-20, D16 leg 1).
        scan = 0 if anywhere else cursor[0]
        while True:
            with self._log_lock:
                fresh = list(enumerate(self._log[scan:], scan))
            for idx, (_ts, line) in fresh:
                if rx.search(line):
                    if not anywhere:             # `seen` never consumes
                        cursor[0] = idx + 1
                    return {"ok": True, "matched": line.strip(),
                            "waited": round(time.time() - started, 2)}
            if fresh:
                scan = fresh[-1][0] + 1          # local only — see above
            if time.time() >= deadline:
                return {"ok": False, "error": "timeout", "pattern": pattern,
                        "seconds": timeout}
            time.sleep(0.15)

    # ── GUI side — everything below runs on the GUI thread ───────────
    def _on_exec(self, req):
        try:
            result = self._dispatch(req.verb, req.args)
        except Exception as e:                      # never kill the GUI
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            req.reply.put_nowait(result)
        except queue.Full:
            pass

    def _state(self):
        """One snapshot of everything worth asserting on. GUI thread only."""
        w = self.win
        sess = getattr(w.worker, "session", None) if w.worker else None
        entry = w._get_active_bbs_entry() or {}
        return {
            # The real logged-in flag. worker.session outlives a FAILED
            # connect, so bool(sess) said True after one (2026-09-28);
            # it is kept as worker_session for debugging only.
            "connected": w._logged_in,
            "worker_session": bool(sess),
            "view": w.VIEW_NAMES.get(w.stack.currentIndex()),
            "bbs": entry.get("name"),
            "callsign": entry.get("callsign"),
            "transport": entry.get("transport"),
            "login_only_connect": w._login_only_connect,
            "send_receive_running": w._sr_active,
            "mail_call_owned": w._mc_session_owned,
            "send_receive_enabled": w.mail_view.btn_send_outbox.isEnabled(),
            "connect_enabled": w.btn_connect.isEnabled(),
            "outbox_pending": len(w.db.get_pending_outbox()),
            "status_line": w.status_label.text(),
            # During a transfer the status label is hidden and _prog_detail
            # carries the text ("Sending 2 of 3 · to N0CALL"). Empty when
            # nothing is running.
            "progress": (w._prog_detail.text()
                         if w._prog_detail.isVisible() else ""),
            "progress_pct": (w._prog_bar.value()
                             if w._prog_bar.isVisible() else -1),
            "abort_enabled": w.btn_abort.isEnabled(),
        }

    def _dispatch(self, verb, args):
        w = self.win
        if verb not in ("status", "log", "help"):
            self._note(f"[API] {verb} {args}".rstrip())

        if verb == "help":
            return {"ok": True, "commands": sorted(_HELP), "detail": _HELP}

        if verb == "status":
            return {"ok": True, **self._state()}

        if verb == "expect":
            # expect <field> <regex> — assert on any field `status` shows.
            # Not everything lands in the log: the status line is one of
            # them, so without this a step file could only wait for things
            # that happen to be logged.
            bits = args.split(None, 1)
            if len(bits) != 2:
                return {"ok": False,
                        "error": "usage: expect <field> <regex>",
                        "fields": sorted(self._state())}
            key, pattern = bits[0], bits[1]
            state = self._state()
            if key not in state:
                return {"ok": False, "error": f"no such field: {key}",
                        "fields": sorted(state)}
            actual = str(state[key])
            try:
                hit = re.search(pattern, actual, re.IGNORECASE) is not None
            except re.error as e:
                return {"ok": False, "error": f"bad regex: {e}"}
            return {"ok": hit, "field": key, "actual": actual,
                    "expected": pattern,
                    **({} if hit else {"error": "does not match"})}

        if verb == "dialog":
            # Drive a modal dialog. QMessageBox.exec() spins a NESTED event
            # loop, and queued signals are still delivered into it, so this
            # runs even while the GUI is "blocked" on the dialog.
            #   dialog            describe the modal that is up
            #   dialog escape     dismiss it (Escape / window X)
            #   dialog <text>     click the button whose label contains text
            from PyQt6.QtWidgets import QApplication, QMessageBox, QDialog
            dlg = QApplication.activeModalWidget()
            if dlg is None:
                return {"ok": False, "error": "no modal dialog is open"}
            buttons = []
            if isinstance(dlg, QMessageBox):
                buttons = [b.text() for b in dlg.buttons()]
            want = args.strip()
            if not want:
                return {"ok": True, "title": dlg.windowTitle(),
                        "type": type(dlg).__name__, "buttons": buttons,
                        "text": (dlg.text()[:300]
                                 if isinstance(dlg, QMessageBox) else "")}
            if want.lower() in ("escape", "esc", "close", "dismiss"):
                dlg.reject() if isinstance(dlg, QDialog) else dlg.close()
                return {"ok": True, "dismissed": dlg.windowTitle(),
                        "how": "reject (same as Escape / window X)"}
            if isinstance(dlg, QMessageBox):
                for b in dlg.buttons():
                    if want.lower() in b.text().lower():
                        b.click()
                        return {"ok": True, "clicked": b.text()}
            return {"ok": False, "error": "no button matched",
                    "buttons": buttons}

        if verb == "log":
            # Timestamped. The tee records its own time because the Debug
            # view adds its timestamp inside append(), after we see the
            # text — and "how long did that take" is most of what a log is
            # for when the link is slow.
            n = int(args) if args.strip().isdigit() else 40
            with self._log_lock:
                tail = self._log[-n:]
            return {"ok": True,
                    "lines": [f"{time.strftime('%H:%M:%S', time.localtime(ts))}"
                              f" {t.rstrip()}"
                              for ts, t in tail if t.strip()]}

        if verb == "inbox":
            n = int(args) if args.strip().isdigit() else 10
            rows = w.db.get_inbox()[:n]
            return {"ok": True, "count": len(rows),
                    "messages": [{"num": r["msg_number"],
                                  "from": r["from_call"],
                                  "subject": r["subject"],
                                  "read": bool(r["read"])}
                                 for r in rows]}

        if verb == "view":
            want = {"mail": w.VIEW_MAIL, "terminal": w.VIEW_TERMINAL,
                    "debug": w.VIEW_DEBUG}.get(args.strip().lower())
            if want is None:
                return {"ok": False, "error": "usage: view mail|terminal|debug"}
            w._switch_view(want)
            return {"ok": True, "view": w.VIEW_NAMES.get(want)}

        if verb == "bbs":
            names = [(w.bbs_combo.itemData(i) or {}).get("name", "")
                     for i in range(w.bbs_combo.count())]
            if not args.strip():
                return {"ok": True, "entries": names,
                        "current": w.bbs_combo.currentIndex()}
            want = args.strip().lower()
            for i, n in enumerate(names):
                if n.lower() == want:
                    w.bbs_combo.setCurrentIndex(i)
                    return {"ok": True, "selected": n, "index": i}
            return {"ok": False, "error": "no such BBS entry", "entries": names}

        if verb == "connect":
            if not w.btn_connect.isEnabled():
                return {"ok": False, "error": "Connect is not available "
                                              "(already connected?)"}
            w._on_connect()
            return {"ok": True, "note": "connect started — use `wait` for the "
                                        "result"}

        if verb == "disconnect":
            w._on_disconnect()
            return {"ok": True}

        if verb == "type":
            if not args:
                return {"ok": False,
                        "error": "usage: type <text>   (or `type cr` for a "
                                 "bare carriage return)"}
            if not (w.worker and w.worker.session):
                return {"ok": False, "error": "not connected"}
            if args.strip().lower() == "cr":
                w._on_terminal_cmd("")
                return {"ok": True, "sent": "<CR>"}
            w._on_terminal_cmd(args)
            return {"ok": True, "sent": args}

        if verb == "yapp":
            # yapp <filename> — start a YAPP download into the normal
            # downloads folder.  ⚠ TRANSMITS.
            name = args.strip()
            if not name:
                return {"ok": False, "error": "usage: yapp <filename>"}
            if not (w.worker and w.worker.session):
                return {"ok": False, "error": "not connected"}
            if " " in name:
                return {"ok": False,
                        "error": "LinBPQ YAPP does not support spaces in "
                                 "filenames"}
            w.terminal.append(f"\n[YAPP] Requesting '{name}'…\n", "#00ccff")
            w.debug_view.append(f"\n[YAPP] Requesting '{name}'…\n", "#00ccff")
            w.worker.do_yapp_download(name, w._downloads_dir)
            return {"ok": True, "requested": name, "save_dir": w._downloads_dir}

        if verb == "click":
            name = args.strip().lower()
            buttons = {
                "send_receive": w.mail_view.btn_send_outbox,
                "abort":        w.btn_abort,
                "stop":         w.btn_abort,   # old name, kept for step files
                "disconnect":   w.btn_disconnect,
                "connect":      w.btn_connect,
            }
            btn = buttons.get(name)
            if btn is None:
                return {"ok": False, "error": "unknown button",
                        "buttons": sorted(buttons)}
            if not btn.isEnabled():
                return {"ok": False, "error": f"{name} is disabled right now"}
            btn.click()
            return {"ok": True, "clicked": name}

        if verb == "queue":
            # queue <to_call> | <subject> | <body>   — no transmit
            bits = [b.strip() for b in args.split("|")]
            if len(bits) < 3 or not bits[0]:
                return {"ok": False,
                        "error": "usage: queue <to> | <subject> | <body>"}
            to_call, subject, body = bits[0].upper(), bits[1], bits[2]
            w.db.queue_outgoing(to_call, subject, body, "P", "")
            w._refresh_folder(w._current_folder)
            w._update_folder_counts()
            return {"ok": True, "queued": to_call, "subject": subject,
                    "outbox_pending": len(w.db.get_pending_outbox())}

        if verb == "find":
            # find <text> — is it in the inbox yet? This is what closes a
            # send-to-yourself round trip: the same text has to come back
            # down off the BBS before the test can pass.
            needle = args.strip().lower()
            if not needle:
                return {"ok": False, "error": "usage: find <text>"}
            for r in w.db.get_inbox():
                hay = f"{r['subject']} {r['body'] or ''}".lower()
                if needle in hay:
                    return {"ok": True, "found": True,
                            "num": r["msg_number"], "from": r["from_call"],
                            "subject": r["subject"]}
            return {"ok": False, "found": False, "error": "not in the inbox",
                    "searched": needle}


        if verb == "screenshot":
            path = args.strip() or "/tmp/qtc.png"
            w.grab().save(path)
            return {"ok": True, "path": path}

        if verb == "quit":
            self.stop()
            return {"ok": True, "note": "API stopped; QtC keeps running"}

        return {"ok": False, "error": f"unknown command: {verb}",
                "commands": sorted(_HELP)}


_HELP = {
    "status":      "current view, BBS, connection and session flags",
    "expect":      "expect <field> <regex>  — assert on any status field; "
                   "fails the step file if it does not match",
    "view":        "view mail|terminal|debug",
    "bbs":         "bbs            — list entries\n"
                   "bbs <name>     — select one",
    "connect":     "press Connect  ⚠ KEYS THE TRANSMITTER on an RF entry",
    "disconnect":  "press Disconnect",
    "type":        "type <text>    — type into the Terminal  ⚠ TRANSMITS",
    "click":       "click send_receive|abort|connect|disconnect"
                   "   ⚠ send_receive TRANSMITS",
    "queue":       "queue <to> | <subject> | <body>  — into the Outbox, no TX",
    "yapp":        "yapp <filename>  — start a YAPP file download "
                   "⚠ TRANSMITS. Type `files` first to see what is there",
    "wait":        "wait <regex> [seconds]  — block until this NEXT happens; "
                   "consumes the matched line",
    "seen":        "seen <regex> [seconds]  — has this happened at all yet? "
                   "searches the whole session, consumes nothing",
    "pause":       "pause <seconds>  — let the far end settle. Use it "
                   "between a disconnect and the next connect; a BBS is "
                   "still tearing the old session down for a second or two",
    "inbox":       "inbox [n]      — most recent inbox messages (default 10)",
    "find":        "find <text>    — is that text in the inbox yet? "
                   "fails if not, so a round trip can assert on it",
    "log":         "log [n]        — last n Debug lines (default 40)",
    "dialog":      "dialog                 describe the open modal\n"
                   "dialog escape          dismiss it (Escape / window X)\n"
                   "dialog <text>          click that button",
    "screenshot":  "screenshot [path]",
    "quit":        "stop the API (QtC keeps running)",
    "help":        "this list",
}


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def install(win, port=API_PORT):
    """Called from main() only when --test-api was passed."""
    api = TestAPI(win, port)
    api.start()
    win._test_api = api          # keep a reference so it is not collected
    return api
