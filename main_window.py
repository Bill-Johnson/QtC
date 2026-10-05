# QtC v0.15.0-beta — main_window.py  (built 2026-10-01)
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
APP_VERSION = "0.15.0-beta"  # keep in sync with header comment
"""
QtC — Main Window (PyQt6)
v0.2 — Quick-connect bar, auto-download, terminal swap button
"""
import sys, json, os, copy, time, threading
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QToolBar, QStatusBar,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    QTextEdit, QLabel, QPushButton, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox,
    QFrame, QHeaderView, QAbstractItemView, QCheckBox,
    QStackedWidget, QInputDialog, QSpacerItem, QSizePolicy,
    QTabWidget, QListWidget, QListWidgetItem, QCompleter,
    QProgressBar, QSpinBox, QFileDialog, QSplashScreen,
    QRadioButton, QButtonGroup, QGroupBox, QTimeEdit
)
from PyQt6.QtCore import (Qt, QThread, pyqtSignal, QSize, QTimer, QTime,
                          QObject, QEvent)
from PyQt6.QtGui import (QFont, QColor, QTextCursor, QPalette, QAction, QPixmap,
                         QShortcut, QKeySequence)

from transport import TelnetTransport

# AX.25 packet modems reached over AGW — transport key -> display name.
AGW_MODEMS = {"direwolf": "Direwolf", "soundmodem": "(Qt)SoundModem"}
AGW_DEFAULT_PORT = 8000   # Direwolf and QtSoundModem both default here
from bbs_session import BBSSession, BBSMailSummary, DownloadAborted

from database import MessageDatabase, ContactsDB

# Carried on sig_yapp_error to mean "the user pressed Abort", which is not
# a failure and must not raise an error box. A sentinel rather than a second
# signal, so nothing downstream of the worker had to change.
_YAPP_ABORTED = "__user_aborted__"


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def _get_app_dir() -> str:
    """Return the QtC application directory, creating it if needed.
    Linux/Mac: ~/.local/share/qtc   Windows: %APPDATA%\\qtc
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        d = os.path.join(base, "qtc")
    else:
        xdg = os.environ.get("XDG_DATA_HOME",
                              os.path.join(os.path.expanduser("~"),
                                           ".local", "share"))
        d = os.path.join(xdg, "qtc")
    os.makedirs(d, exist_ok=True)
    return d

_APP_DIR     = _get_app_dir()
_CONFIG_PATH = os.path.join(_APP_DIR, "config.json")

def load_config():
    if not os.path.exists(_CONFIG_PATH):
        # First run — create a default config and continue
        _write_default_config()
    # Check for empty file — installer may have written a blank
    if os.path.getsize(_CONFIG_PATH) == 0:
        _write_default_config()
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # Corrupted config — overwrite with defaults
            _write_default_config()
            with open(_CONFIG_PATH, encoding="utf-8") as f2:
                return json.load(f2)

def _write_default_config():
    """Write a valid default config.json — used on first run or if file is corrupt."""
    default = {
        "user": {"callsign": "NOCALL", "name": "", "qth": "",
                 "zip": "", "home_bbs": ""},
        "bbs_list": [],
        "vara": {"hf_host": "127.0.0.1",
                 "hf_cmd_port": 8300, "hf_data_port": 8301},
        "ptt": {"mode": "none", "port": "COM3", "signal": "rts"},
        "app": {"page_limit": 20}
    }
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(default, f, indent=4)

def save_config(cfg):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)


# ─────────────────────────────────────────────────────────────────────────────
# Background worker thread
# ─────────────────────────────────────────────────────────────────────────────

class SessionWorker(QThread):
    sig_log           = pyqtSignal(str)
    sig_connected     = pyqtSignal()
    sig_rf_connected  = pyqtSignal()   # fires when RF link up, before login
    sig_disconnected  = pyqtSignal()
    sig_mail_summary  = pyqtSignal(object)
    sig_download_done = pyqtSignal(int)
    sig_send_result   = pyqtSignal(bool, str)
    sig_error         = pyqtSignal(str)
    sig_ll_ready      = pyqtSignal(str, object, object, object)  # bbs_call, new_only, all_personal, bulletins
    sig_progress      = pyqtSignal(str, int, int, str)  # op, current, total, detail
    sig_bulletin_check = pyqtSignal(object)   # {category: [BBSMessage]} — show dialog
    sig_bulletin_done  = pyqtSignal(int)       # count of bulletins downloaded
    sig_categories_ready = pyqtSignal(object, bool)  # list[(cat,count)], open_dialog
    sig_yapp_progress  = pyqtSignal(int, int, str)  # bytes_done, total, filename
    sig_yapp_done      = pyqtSignal(str, str)        # save_path, display_name
    sig_yapp_error     = pyqtSignal(str)

    def __init__(self, config, bbs_entry, db):
        super().__init__()
        self.config    = config
        self.bbs_entry = bbs_entry
        self.db        = db
        self.session   = None
        self._task     = None
        import queue as _queue
        import threading as _threading
        self._queue    = _queue.Queue()
        # Guards _task and _busy — see _submit()
        self._task_lock = _threading.Lock()
        self._busy      = False

    def _submit(self, task=None):
        """Hand a task to the worker thread and make sure it actually runs.

        The GUI chains session steps off this thread's own signals: LM
        finishes, sig_ll_ready fires, and 200 ms later the GUI asks for the
        bulletin sweep. That request can land while run() is still on its
        way out. The old `if not self.isRunning(): self.start()` then
        skipped the start, the task sat in _task forever, and the session
        hung with the link up until the BBS timed it out (08:00 Mail-Call,
        2026-09-11: LM done, no `l> BDN`, BBS hung up 15 minutes later).

        run() now loops until nothing is left, and only stops after
        clearing _busy under the same lock taken here — so a task stored
        here is always either picked up by the running loop or starts a
        fresh one.
        """
        with self._task_lock:
            if task is not None:
                self._task = task
            if self._busy:
                return          # the running loop picks it up
            self._busy = True
        # run() may have just decided to stop without the thread having
        # finished yet, and start() on a running QThread is ignored.
        self.wait()
        self.start()

    def _enqueue(self, task):
        """Add task to queue and start thread if not running."""
        self._queue.put(task)
        self._submit()

    def do_connect_and_check(self, login_only: bool = False):
        """login_only=True (Terminal / Debug view): log in, then stop."""
        self._submit(("connect_check", login_only))

    def do_download(self, messages):
        self._submit(("download", messages))

    def do_send(self, to_call, subject, body, msg_type, at_bbs,
                index: int = 1, total: int = 1):
        """index/total are this message's place in the batch. The GUI hands
        every queued message over at once, so only the worker knows when
        one actually starts going out — which is when the counter has to
        move (Bill, 2026-09-23)."""
        self._enqueue(("send", to_call, subject, body, msg_type, at_bbs,
                       index, total))

    def do_disconnect(self):
        self._submit(("disconnect",))

    def do_abort(self):
        """Stop an in-progress download immediately (called from the GUI
        thread). Sets the transport abort flag so the worker's blocked read
        returns at once; the download path then sends 'A' to the BBS and
        resyncs to the command prompt, leaving the session connected. Safe
        to call mid-download — it only flips a threading.Event."""
        s = self.session
        if s and getattr(s, "transport", None) \
                and hasattr(s.transport, "request_abort"):
            s.transport.request_abort()

    def do_terminal_send(self, cmd: str):
        self._submit(("terminal_send", cmd))

    def do_mail_check(self, new_only: bool = True):
        self._submit(("mail_check", new_only))

    def do_check_bulletins(self, subscriptions: list,
                           unattended: bool = False):
        """unattended=True (Mail-Call) reads each category's bulletins as
        soon as its listing pauses, with no selection dialog in between."""
        self._submit(("check_bulletins", subscriptions, unattended))

    def do_yapp_download(self, filename: str, save_dir: str):
        self._submit(("yapp_download", filename, save_dir))

    def do_download_bulletins(self, messages_by_cat: dict):
        """messages_by_cat = {category: [BBSMessage, ...]}"""
        self._submit(("download_bulletins", messages_by_cat))

    def do_list_categories(self, open_dialog: bool = False):
        """Run LC on the active session, persist known_categories +
        last_lc_at, and drop one inbox notification per newly-appeared
        category. Triggered by the auto-update connect-flow hook (when
        check_on_connect is on and the 10-day window has elapsed) and
        by the Refresh-now button on the Bulletins settings tab.
        `open_dialog` is retained on the signal signature for
        forward-compat but is currently unused — all subscription
        editing happens offline in the checkable category list."""
        self._submit(("list_categories", open_dialog))

    def run(self):
        import queue as _queue
        # Keep going until nothing is waiting: a step finishing here usually
        # makes the GUI ask for the next one before this thread has exited.
        while True:
            with self._task_lock:
                t = self._task
                self._task = None   # clear immediately so re-entry never re-runs connect
                if t is None:
                    try:
                        t = self._queue.get_nowait()   # queued sends
                    except _queue.Empty:
                        self._busy = False
                        return
            try:
                # A Terminal / Debug connect never ran the login that finds
                # the BBS prompt — pick it up before a button's step runs.
                if (self.session and t[0] not in
                        ("connect_check", "disconnect", "terminal_send")):
                    self.session.learn_prompt()
                if   t[0] == "connect_check": self._run_connect_and_check(*t[1:])
                elif t[0] == "download":      self._run_download(t[1])
                elif t[0] == "disconnect":    self._run_disconnect()
                elif t[0] == "mail_check":    self._run_mail_check(t[1])
                elif t[0] == "terminal_send": self._run_terminal_send(t[1])
                elif t[0] == "send":          self._run_send(*t[1:])
                elif t[0] == "check_bulletins":    self._run_check_bulletins(*t[1:])
                elif t[0] == "download_bulletins": self._run_download_bulletins(t[1])
                elif t[0] == "list_categories":    self._run_list_categories(t[1])
                elif t[0] == "yapp_download":      self._run_yapp_download(t[1], t[2])
            except Exception as e:
                self.sig_error.emit(str(e))

    def _make_session(self):
        e         = self.bbs_entry
        transport = e.get("transport", "vara_hf")

        if transport == "telnet":
            t = TelnetTransport(e["host"], e["telnet_port"], timeout=30)
            t._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")
            user_info = {k: self.config["user"].get(k, "")
                         for k in ("name", "qth", "zip", "home_bbs")}
            s = BBSSession(t,
                           mycall=self.config["user"]["callsign"],
                           password=self.config["user"].get("password", ""),
                           telnet_user=self.config["user"].get("telnet_user", ""),
                           user_info=user_info)

        elif transport in ("vara_hf", "vara_fm"):
            from transport import VaraTransport
            vara_cfg      = self.config.get("vara", {})
            prefix        = "hf" if transport == "vara_hf" else "fm"
            vara_host     = vara_cfg.get(f"{prefix}_host",
                            vara_cfg.get("hf_host", "127.0.0.1"))
            vara_cmd_port = vara_cfg.get(f"{prefix}_cmd_port",
                            vara_cfg.get("hf_cmd_port", 8300))
            vara_data_port= vara_cfg.get(f"{prefix}_data_port",
                            vara_cfg.get("hf_data_port", 8301))
            mycall        = self.config["user"]["callsign"]
            target_call   = e.get("callsign", "")
            if not target_call:
                raise ValueError("No BBS callsign set for VARA connection.")
            # Pick a sensible default BW per variant in case the entry
            # lacks a saved value (legacy entries pre-FM-BW support).
            default_bw = "500" if prefix == "hf" else "NARROW"
            t = VaraTransport(
                vara_host=vara_host,
                cmd_port=vara_cmd_port,
                data_port=vara_data_port,
                mycall=mycall,
                target_call=target_call,
                timeout=90,
                bandwidth=e.get("bw") or default_bw,
                vara_type=prefix,
            )
            t._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")

            # Wire remote-disconnect callback so BBS/VARA timeout updates the GUI
            t._on_disconnected_cb = lambda: self.sig_disconnected.emit()

            # Attach PTT controller if configured
            ptt_cfg = self.config.get("ptt", {})
            ptt_mode = ptt_cfg.get("mode", "none")
            ptt_port = ptt_cfg.get("port", "")
            ptt_signal = ptt_cfg.get("signal", "rts")
            if ptt_mode != "none" and ptt_port:
                from ptt import PTTController
                ptt = PTTController(port=ptt_port, mode=ptt_signal)
                ptt._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")
                ptt.open()
                if not ptt.is_open:
                    # Don't abort the connect — VOX users and "I'll fix it later"
                    # users can still operate. But shout about it so the user
                    # isn't watching the waterfall transmit with a silent radio.
                    msg = (f"PTT port {ptt_port!r} did not open: "
                           f"{ptt.last_error or 'unknown error'}. "
                           f"This port may be open in Vara Terminal or another "
                           f"app — close it and reconnect.")
                    self.sig_log.emit(f"[PTT] *** {msg} ***")
                    self.sig_error.emit(msg)
                t.ptt = ptt

            user_info = {k: self.config["user"].get(k, "")
                         for k in ("name", "qth", "zip", "home_bbs")}
            s = BBSSession(t, mycall=mycall, user_info=user_info)
            s._rf_connected_cb = lambda: self.sig_rf_connected.emit()

        elif transport in AGW_MODEMS:
            from transport import AGWTransport
            mycall      = self.config["user"]["callsign"]
            target_call = e.get("callsign", "")
            if not target_call:
                raise ValueError("No BBS callsign set for packet connection.")
            t = AGWTransport(
                host=e.get("host") or "127.0.0.1",
                port=e.get("agw_port") or AGW_DEFAULT_PORT,
                mycall=mycall,
                target_call=target_call,
                radio_port=e.get("agw_radio_port", 0),
                modem_name=AGW_MODEMS[transport],
                # QtSoundModem sends each 'D' frame as ONE I-frame with no
                # PACLEN split of its own, so QtC's chunk is the on-air
                # frame size. Direwolf re-splits to its PACLEN either way.
                max_frame_data=128 if transport == "soundmodem" else 256,
            )
            t._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")
            # BBS or modem ended the link — update the GUI like VARA does
            t._on_disconnected_cb = lambda: self.sig_disconnected.emit()
            # No PTTController here: the modem keys the radio itself, or
            # VOX does. AGW has no PTT ON/OFF message for QtC to act on.

            user_info = {k: self.config["user"].get(k, "")
                         for k in ("name", "qth", "zip", "home_bbs")}
            s = BBSSession(t, mycall=mycall, user_info=user_info)
            s._rf_connected_cb = lambda: self.sig_rf_connected.emit()

        else:
            raise ValueError(f"Unknown transport: {transport}")

        # Apply the user-selected text codec for decoding BBS content
        # (utf-8 default; cp437/cp850 render DOS box-drawing bulletins —
        # GitHub issue #2). The transport validates the name and silently
        # falls back to utf-8 if it is unknown.
        if hasattr(t, "set_text_codec"):
            t.set_text_codec(self.config.get("app", {}).get("text_codec", "utf-8"))

        s._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")
        return s

    # ── Paging (OP) ───────────────────────────────────────────────
    OP_REFRESH_DAYS = 30

    def _maybe_set_page_length(self, mycall_bbs: str):
        """
        Send `OP n` if this BBS has never been told, the number changed in
        Settings, or the last one is older than OP_REFRESH_DAYS.

        The BBS stores this in the user record, so it is one line on the
        wire every 30 days — not every connect. See
        feedback_rf_airtime_politeness: recurring TX is gated by a time
        delta, never by "every connect".
        """
        import datetime as _dt
        want = int(self.config.get("app", {}).get("page_limit", 20))
        store = self.config.setdefault("bbs_paging", {})
        rec   = store.get(mycall_bbs) or {}

        due = True
        if rec.get("value") == want and rec.get("set_at"):
            try:
                last = _dt.datetime.strptime(
                    str(rec["set_at"]).replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
                due = (_dt.datetime.utcnow() - last) >= \
                      _dt.timedelta(days=self.OP_REFRESH_DAYS)
            except (ValueError, TypeError):
                due = True   # unreadable stamp — treat as never set

        if not due:
            self.session.page_length = want
            self.sig_log.emit(
                f"[SYS] Page limit already set on this BBS (OP {want}) "
                f"— not resending")
            return

        self.sig_log.emit(f"[SYS] Setting BBS page limit — OP {want}")
        result = self.session.set_page_length(want)

        if result == "refused":
            # The value is out of range for this BBS, so paging is still
            # at whatever it was. Do NOT record it: recording would mean
            # not trying again for OP_REFRESH_DAYS, and the user has not
            # had a chance to correct the setting yet.
            self.sig_log.emit(
                f"[SYS] BBS refused OP {want} — the page limit in "
                f"Settings → App is below what this BBS accepts "
                f"(LinBPQ wants {BBSSession.PAGE_MIN} or more, or 0 for "
                f"no paging). Nothing recorded; QtC will try again.")
            return

        store[mycall_bbs] = {
            "value":  want,
            "set_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        save_config(self.config)
        if result != "confirmed":
            self.sig_log.emit(
                f"[SYS] BBS did not confirm OP {want} — it may not support "
                f"paging. QtC handles either case.")

    def _bump_watermark(self, mycall_bbs: str, messages):
        """
        Keep bbs_watermarks current from whatever we listed this session.

        Nothing reads it any more — the LM / L> scheme does not need it.
        It is maintained so a future Advanced mode can offer the old
        BBS-wide `L <watermark>-` sweep without needing a catch-up
        connect first. See CLAUDE.md → ARCHITECTURE NOTES.
        """
        if not messages:
            return
        high = max(m.msg_number for m in messages)
        prev = self.db.get_watermark(mycall_bbs) or 0
        if high > prev:
            self.db.set_watermark(mycall_bbs, high)

    def _run_terminal_connect(self):
        """Terminal / Debug view: a dumb terminal (Bill, 2026-09-13). Bring
        up the link, stream whatever the BBS sends, and stop — no OP, no LM,
        no login script. The one exception is Telnet's username and password
        (BBSSession.connect_only). The `bbs` command, registration answers
        and a parked page menu are the user's to type.
        Buttons still work: the worker picks up the BBS prompt from what
        was shown (BBSSession.learn_prompt), and OP is caught up before a
        bulletin sweep (_run_check_bulletins)."""
        t = self.session.transport
        if hasattr(t, "set_terminal_mode"):
            t.set_terminal_mode(True)     # before connect, so the banner shows
        self.session.connect_only()
        self.sig_connected.emit()
        self.sig_log.emit(
            "[SYS] Terminal connect — from here nothing is sent or read "
            "until you type it")

    def _run_connect_and_check(self, login_only: bool = False):
        self.session = self._make_session()
        if login_only:
            self._run_terminal_connect()
            return
        if not self.session.connect_and_login():
            self.sig_error.emit("Login failed — check callsign and BBS address.")
            return
        self.sig_connected.emit()

        if self.session.new_user:
            self.sig_log.emit("[SYS] New user registration completed.")

        mycall    = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        bbs_call  = self.bbs_entry.get("callsign", "").upper()
        mycall_bbs = f"{mycall}@{bbs_call}"

        # Pause the terminal monitor for the whole OP / LM sequence
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        self.session.transport.flush_input()

        try:
            # ── Page limit — first connect here, or every 30 days ──
            self._maybe_set_page_length(mycall_bbs)

            # ── Personal mail: LM, and only LM ─────────────────────
            # LM lists just the messages addressed to this callsign, so
            # its size tracks YOUR mailbox and not the BBS's traffic.
            # The old scheme listed the whole BBS from a watermark
            # (`L 279-`), which after a fortnight away from a busy node
            # meant hundreds or thousands of header lines on the air
            # before a single byte of your own mail moved.
            self.sig_log.emit("[SYS] Checking personal mail — sending LM")
            summary  = self.session.check_mail(new_only=False)
            messages = summary.all_messages
        finally:
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)

        self._bump_watermark(mycall_bbs, messages)

        # LM already guarantees "addressed to me", so the filtering here is
        # only about type and origin. Match on the base callsign so mail to
        # an SSID of ours (N0CALL-1) is not thrown away.
        mybase = mycall.split("-")[0]

        def _is_my_personal(m):
            return (m.msg_type == "P"
                    and m.to_call.split("-")[0] == mybase
                    and m.to_call != "SYSOP"
                    and m.from_call != "SYSTEM")

        # PN only — new unread personal mail (normal auto-download)
        new_only = [
            m for m in messages if _is_my_personal(m) and m.status == "N"
        ]
        # PN + PY — all personal mail (first-visit "All" choice)
        all_personal = [
            m for m in messages if _is_my_personal(m)
        ]

        self.sig_log.emit(
            f"[SYS] LM: {len(messages)} listed, "
            f"{len(new_only)} new personal, "
            f"{len(all_personal)} personal total")

        # Bulletins are no longer scraped out of a BBS-wide listing — they
        # get their own bounded `L> CATEGORY` sweep after the mail stage,
        # so the third list is always empty here. The signal signature is
        # unchanged so nothing downstream had to move.
        self.sig_ll_ready.emit(bbs_call, new_only, all_personal, [])

    def _run_mail_check(self, new_only: bool):
        # Disable data streaming while _expect handles the LM response,
        # then always hand the monitor back — _run_download turns it off
        # again itself if there is anything to fetch.
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        # Whatever was typed and answered in the Terminal is still in the
        # receive buffer — LM would match the first stale prompt in it.
        self.session.transport.flush_input()
        try:
            summary = self.session.check_mail(new_only=new_only)
        finally:
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)
        self.sig_mail_summary.emit(summary)

    def _run_terminal_send(self, cmd: str):
        """
        Send a raw command to the BBS in terminal mode.
        The data monitor thread streams all responses directly to the
        terminal display as [RX] log lines — no _expect() needed here.
        """
        if not self.session:
            self.sig_log.emit("[SYS] Not connected — cannot send command")
            return
        try:
            self.session._send(cmd)
            # B command — clean up session after RF disconnect
            if cmd.strip().upper() in ("B", "BYE", "LOGOUT", "LOGOFF", "Q", "QUIT"):
                import time; time.sleep(0.5)
                self.session.logout(skip_bye=True)
                self.session = None
                self.sig_disconnected.emit()
        except Exception as e:
            self.sig_log.emit(f"[SYS] Terminal send error: {e}")

    def _run_download(self, messages):
        mycall = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        bbs_id = f"{mycall}@{self.bbs_entry['callsign']}"
        count = 0
        total = len(messages)
        # Pause the terminal monitor for the entire download sequence.
        # Without this the monitor thread races _expect on the same socket,
        # consuming bytes that download_message needs and causing hangs.
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        # Clear any stale abort flag so a previous bail can't kill this run.
        if hasattr(self.session.transport, "clear_abort"):
            self.session.transport.clear_abort()
        self.session.transport.flush_input()
        try:
            for i, msg in enumerate(messages, 1):
                if not self.db.message_exists(msg.msg_number, bbs_id):
                    detail = f"msg #{msg.msg_number} · ~{msg.size} bytes"
                    # 1-based: this is the message going over the air now,
                    # not the count already finished. See _on_progress.
                    self.sig_progress.emit("downloading", i, total, detail)
                    self.sig_log.emit(
                        f"[SYS] Downloading message {i} of {total} "
                        f"(#{msg.msg_number}, ~{msg.size} bytes)")
                    msg.body = self.session.download_message(
                        msg.msg_number, size_hint=msg.size)
                    msg.downloaded = True
                    self.db.save_to_inbox(msg, bbs_id)
                    count += 1
        except DownloadAborted:
            # User hit Abort: read was already interrupted; tell the BBS to
            # stop sending ('A') and resync to the prompt. Stay connected.
            self.session._abort_to_prompt()
        finally:
            # Hand back a session that is at the command prompt, whatever
            # happened above. A no-op unless the last read left us parked
            # at a page menu; _abort_to_prompt has already cleared it on
            # the abort path.
            self.session.resync_to_prompt("downloads finished")
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)
        self.sig_progress.emit("done", 0, 0, "")
        self.sig_download_done.emit(count)

    def _run_send(self, to_call, subject, body, msg_type, at_bbs,
                  index: int = 1, total: int = 1):
        # Emitted here, before a byte goes out, so "Sending 2 of 3" means
        # the one on the air right now.
        self.sig_progress.emit("sending", index, total, f"to {to_call}")
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        self.session.transport.flush_input()
        try:
            ok = self.session.send_message(to_call, subject, body, msg_type, at_bbs)
        finally:
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)
        self.sig_send_result.emit(ok, to_call)

    def _run_disconnect(self):
        if self.session:
            self.session.logout()
            self.session = None
        self.sig_disconnected.emit()

    def _run_check_bulletins(self, subscriptions: list,
                             unattended: bool = False):
        """
        Sweep each subscribed category with `L> CATEGORY`.

        Bounded by category, not by BBS traffic: with OP 20 one page is
        the 20 newest bulletins in that category — roughly three weeks of
        a daily bulletin. Anything older simply is not offered, which is
        the deal we make to keep a long absence off the air.

        Status flags (N / Y / $ / F / D) are ignored throughout. They
        record what the BBS's forwarding and housekeeping have done with a
        bulletin, not what this user has read. The only test is "do we
        already have it" — the messages table plus the tombstone table.

        unattended=True (a Mail-Call session) reads each category's wanted
        bulletins the moment the listing pauses, sending `R <msg#>`
        straight to the page menu. That both ends the listing and starts
        the read, saving an 'A' round trip per category. The interactive
        path cannot do that: it shows one selection dialog covering every
        category, so each listing has to be closed before the next one
        starts, and we must never hold the BBS at a page prompt while a
        modal window waits on the user.
        """
        import time as _time
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        if hasattr(self.session.transport, "clear_abort"):
            self.session.transport.clear_abort()
        self.session.transport.flush_input()
        _time.sleep(0.5)

        mycall   = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        bbs_call = self.bbs_entry["callsign"]
        bbs_id   = f"{mycall}@{bbs_call}"
        bull_key = f"bulletins_seen@{bbs_call}"

        # A Terminal / Debug connect skips OP, and `L> CATEGORY` is only held
        # to one page once OP is on — catch it up first. Sends nothing when
        # this BBS already has it and it isn't OP_REFRESH_DAYS old.
        if not self.session.page_length:
            self._maybe_set_page_length(f"{mycall}@{bbs_call.upper()}")
        visited  = self.config.get("visited_bbs", {})

        # First bulletin connect to this BBS — keep only the newest few per
        # category and tombstone the rest, so a new user is not handed a
        # backlog. VARA HF and packet are slow, so cap tighter there.
        first_connect = bull_key not in visited
        first_keep = 2 if self.bbs_entry.get(
            "transport", "vara_hf") in ("vara_hf", *AGW_MODEMS) else 3

        def _already_have(m):
            return (self.db.bulletin_exists(m.msg_number, bbs_id)
                    or self.db.bulletin_tombstone_exists(m.msg_number, bbs_id))

        results   = {}
        listed    = []
        dl_count  = 0
        try:
            for cat in subscriptions:
                cat = cat.upper().strip()
                if not cat:
                    continue
                msgs, parked = self.session.list_category(cat)
                bulls = [m for m in msgs if m.msg_type == "B"]
                listed.extend(bulls)

                if first_connect and bulls:
                    newest = sorted(bulls, key=lambda m: m.msg_number,
                                    reverse=True)
                    stale = newest[first_keep:]
                    if stale:
                        self.db.add_bulletin_tombstones_batch(stale, bbs_id)
                        self.sig_log.emit(
                            f"[SYS] First bulletin connect — tombstoned "
                            f"{len(stale)} old {cat} bulletins, keeping "
                            f"{len(newest[:first_keep])} newest")

                wanted = [m for m in bulls if not _already_have(m)]

                if unattended and wanted:
                    # Parked at the page prompt — `R <msg#>` answers the
                    # menu directly, so no 'A' is needed first.
                    dl_count += self._download_bulletins_now(
                        cat, wanted, bbs_id)
                    continue

                if parked:
                    self.session.end_listing(
                        "nothing wanted" if not wanted
                        else f"{len(wanted)} wanted, dialog first")
                if wanted:
                    results[cat] = wanted
                    self.sig_log.emit(
                        f"[SYS] Bulletins: {len(wanted)} wanted in {cat}")
                else:
                    self.sig_log.emit(f"[SYS] Bulletins: nothing new in {cat}")
        except DownloadAborted:
            self.session._abort_to_prompt()
        finally:
            # Always hand the terminal monitor back. _run_download_bulletins
            # turns it off again itself if the user picks something, so this
            # is safe on every path — and it stops the terminal view going
            # dead for the rest of the session when a sweep finds nothing.
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)

        if first_connect:
            self.config.setdefault("visited_bbs", {})[bull_key] = True
            save_config(self.config)

        self._bump_watermark(bbs_id, listed)

        if unattended:
            self.sig_progress.emit("done", 0, 0, "")
            self.sig_bulletin_done.emit(dl_count)
        else:
            self.sig_bulletin_check.emit(results)

    def _download_bulletins_now(self, cat: str, wanted: list,
                                bbs_id: str) -> int:
        """Read and save one category's bulletins immediately.

        Used by the unattended sweep, where the first `R <msg#>` is the
        reply to the listing's page prompt. Returns how many were saved.
        """
        import re as _re
        import time as _time
        total = len(wanted)
        saved = 0
        for i, msg in enumerate(wanted, 1):
            if getattr(self.session.transport, "abort_requested",
                       lambda: False)():
                raise DownloadAborted()
            self.sig_progress.emit(
                "downloading", i - 1, total,
                f"{cat} #{msg.msg_number} · ~{msg.size} bytes")
            self.sig_log.emit(
                f"[SYS] Downloading bulletin {i}/{total} "
                f"{cat} #{msg.msg_number} ~{msg.size} bytes")
            msg.body = self.session.download_message(
                msg.msg_number, size_hint=msg.size)
            bid_m = _re.search(r'Bid:\s*(\S+)', msg.body, _re.I)
            self.db.save_bulletin(msg, bbs_id,
                                  bid=bid_m.group(1) if bid_m else "")
            saved += 1
            _time.sleep(0.3)
        # The last `R <msg#>` answered the page menu, so BPQ has returned
        # to that menu rather than to the command prompt. Close it before
        # the sweep moves on — the next category's `l> CAT` (or the `b` at
        # the end of the session) would otherwise be typed into the menu.
        self.session.resync_to_prompt(f"{cat} downloads finished")
        return saved

    def _run_download_bulletins(self, messages_by_cat: dict):
        """Download selected bulletins and save to database."""
        import time as _time
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        # Clear any stale abort flag so a previous bail can't kill this run.
        if hasattr(self.session.transport, "clear_abort"):
            self.session.transport.clear_abort()
        self.session.transport.flush_input()
        bbs_id = (f"{self.config.get('user',{}).get('callsign','NOCALL').upper()}"
                  f"@{self.bbs_entry['callsign']}")
        count = 0
        total = sum(len(msgs) for msgs in messages_by_cat.values())
        i = 0
        try:
            for cat, msgs in messages_by_cat.items():
                for msg in msgs:
                    i += 1
                    detail = f"{cat} #{msg.msg_number} · ~{msg.size} bytes"
                    self.sig_progress.emit("downloading", i, total, detail)
                    self.sig_log.emit(
                        f"[SYS] Downloading bulletin {i}/{total} "
                        f"{cat} #{msg.msg_number} ~{msg.size} bytes")
                    body = self.session.download_message(
                        msg.msg_number, size_hint=msg.size)
                    msg.body = body
                    # Extract BID from body header if present
                    import re as _re
                    bid_m = _re.search(r'Bid:\s*(\S+)', body, _re.I)
                    bid = bid_m.group(1) if bid_m else ""
                    self.db.save_bulletin(msg, bbs_id, bid=bid)
                    count += 1
                    _time.sleep(0.3)   # brief pause between bulletins
        except DownloadAborted:
            # User hit Abort: read was already interrupted; tell the BBS to
            # stop sending ('A') and resync to the prompt. Stay connected.
            self.session._abort_to_prompt()
        finally:
            # Hand back a session that is at the command prompt, whatever
            # happened above. A no-op unless the last read left us parked
            # at a page menu; _abort_to_prompt has already cleared it on
            # the abort path.
            self.session.resync_to_prompt("downloads finished")
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)
        self.sig_progress.emit("done", 0, 0, "")
        self.sig_bulletin_done.emit(count)

    def _run_list_categories(self, open_dialog: bool):
        """Run LC on the active session, diff against known_categories,
        emit one inbox notification per newly-appeared category, persist
        last_lc_at + known_categories, then emit sig_categories_ready."""
        import time as _time
        import datetime as _dt
        if not self.session:
            self.sig_error.emit("Not connected to BBS.")
            return
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        self.session.transport.flush_input()
        _time.sleep(0.5)
        try:
            categories = self.session.list_categories()
        finally:
            if hasattr(self.session.transport, "set_terminal_mode"):
                self.session.transport.set_terminal_mode(True)

        bull_cfg  = self.config.setdefault("bulletins", {})
        auto_cfg  = bull_cfg.setdefault("auto_category_update", {})
        prev      = auto_cfg.get("known_categories") or []
        prev_names = {c[0] if isinstance(c, (list, tuple)) else c
                      for c in prev}
        mycall    = self.config.get("user", {}).get(
            "callsign", "NOCALL").upper()
        home_bbs  = self.config.get("user", {}).get(
            "home_bbs", "").strip().upper()

        # Drop one inbox notification per newly-appeared category name.
        # Skip on the very first run (prev empty) — that's the baseline,
        # not a "new" event.
        if prev_names:
            for cat, count in categories:
                if cat not in prev_names:
                    subject = (f"New bulletin category on "
                               f"{home_bbs or 'Home BBS'}: {cat}")
                    body = (
                        f"Your Home BBS ({home_bbs or '?'}) has a new "
                        f"bulletin category since the last check:\n\n"
                        f"    {cat}  ({count} bulletins currently posted)\n\n"
                        f"To subscribe:\n"
                        f"  - Open Settings → Bulletins\n"
                        f"  - Click \"📡 Get categories from BBS…\" "
                        f"and tick {cat}, or\n"
                        f"  - Add {cat} on its own line in the "
                        f"Subscriptions box\n\n"
                        f"You'll then receive new {cat} bulletins on "
                        f"your next Home BBS connect.\n\n"
                        f"-- QtC Category Update\n")
                    try:
                        self.db.create_system_message(mycall, subject, body)
                        self.sig_log.emit(
                            f"[SYS] New bulletin category detected: {cat} "
                            f"(see 🔔 Notifications folder)")
                    except Exception as e:
                        self.sig_log.emit(
                            f"[SYS] Could not insert category notification "
                            f"for {cat}: {e}")

        # Persist cache + timestamp. Stored as list-of-lists for JSON.
        auto_cfg["known_categories"] = [[c, n] for c, n in categories]
        auto_cfg["last_lc_at"] = _dt.datetime.utcnow().strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        save_config(self.config)

        self.sig_categories_ready.emit(categories, open_dialog)

    def _run_yapp_download(self, filename: str, save_dir: str):
        """
        Download a file from the BBS using YAPP protocol.
        Sends 'yapp <filename>', receives the binary transfer, saves to save_dir.
        Emits sig_yapp_progress during transfer, sig_yapp_done on success,
        or sig_yapp_error on failure.
        """
        if not self.session:
            self.sig_yapp_error.emit("Not connected to BBS.")
            return
        try:
            def _progress(done: int, total: int):
                self.sig_yapp_progress.emit(done, total, filename)

            save_path, _nbytes = self.session.download_file(
                filename, save_dir, progress_cb=_progress)
            import os as _os
            self.sig_yapp_done.emit(save_path, _os.path.basename(save_path))
        except DownloadAborted:
            # The user pressed Abort. The receiver has already sent CN and
            # drained the wire, and download_file's finally block has put
            # the BBS back at its prompt — nothing is wrong, so don't
            # report it as an error.
            self.sig_yapp_error.emit(_YAPP_ABORTED)
        except Exception as e:
            self.sig_yapp_error.emit(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Address Book Dialog
# ─────────────────────────────────────────────────────────────────────────────

class AddressBookDialog(QDialog):
    """Full address book management window — File → Address Book."""

    contact_selected = pyqtSignal(dict)   # emits contact dict when user clicks Select

    def __init__(self, contacts_db, parent=None, select_mode=False):
        super().__init__(parent)
        self.cdb = contacts_db
        self.select_mode = select_mode   # True when opened from Compose
        self.setWindowTitle("Address Book")
        # Sized to fit a full HA Home BBS address ("N0CALL.#REGION.ST.USA.NOAM")
        # without horizontal scrolling, plus the Edit/Delete action cell.
        self.setMinimumSize(880, 480)
        self.resize(960, 560)
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Toolbar ───────────────────────────────────────────────
        tb = QHBoxLayout()
        tb.setContentsMargins(10, 8, 10, 8)
        tb.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "search callsign, name, city, state…")
        self.search_edit.textChanged.connect(self._refresh)
        tb.addWidget(self.search_edit)
        self.add_btn = QPushButton("+ Add Contact")
        self.add_btn.clicked.connect(self._on_add)
        tb.addWidget(self.add_btn)
        layout.addLayout(tb)

        # Divider
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(0,0,0,0.1)")
        layout.addWidget(line)

        # ── Contact list ──────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Call", "Name", "City / State", "Home BBS", ""])
        self.table.horizontalHeader().setStretchLastSection(False)
        # City/State stretches to soak up extra width. Home BBS is sized
        # to fit a typical HA address ("N0CALL.#REGION.ST.USA.NOAM") in
        # full. Action column fits Edit + Delete buttons side-by-side.
        self.table.horizontalHeader().setSectionResizeMode(
            2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(0, 80)    # Call
        self.table.setColumnWidth(1, 110)   # Name
        self.table.setColumnWidth(3, 230)   # Home BBS — full HA address
        self.table.setColumnWidth(4, 140)   # Actions (Edit / Delete)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self._on_edit_row)
        layout.addWidget(self.table)

        self._hint = QLabel()
        self._hint.setStyleSheet(
            "font-size:11px; color: #aaaaaa; padding: 4px 10px;")
        layout.addWidget(self._hint)

        self._refresh()

    def _refresh(self):
        q = self.search_edit.text().strip()
        contacts = self.cdb.search(q) if q else self.cdb.get_all()
        self.table.setRowCount(0)
        for c in contacts:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(c["callsign"]))
            self.table.setItem(r, 1, QTableWidgetItem(c.get("name") or ""))
            self.table.setItem(r, 2, QTableWidgetItem(c.get("qth") or ""))
            self.table.setItem(r, 3, QTableWidgetItem(c.get("home_bbs") or ""))
            # Action buttons cell
            cell = QWidget()
            hb = QHBoxLayout(cell)
            hb.setContentsMargins(4, 2, 4, 2)
            hb.setSpacing(4)
            if self.select_mode:
                sel = QPushButton("Select")
                sel.setFixedHeight(24)
                sel.clicked.connect(lambda _, cc=c: self._on_select(cc))
                hb.addWidget(sel)
            edit = QPushButton("Edit")
            edit.setFixedHeight(24)
            edit.clicked.connect(lambda _, cc=c: self._on_edit(cc))
            hb.addWidget(edit)
            dlt = QPushButton("Delete")
            dlt.setFixedHeight(24)
            dlt.clicked.connect(lambda _, cs=c["callsign"]: self._on_delete(cs))
            hb.addWidget(dlt)
            self.table.setCellWidget(r, 4, cell)
        self.table.resizeRowsToContents()
        n = len(contacts)
        self._hint.setText(
            f"{n} contact{'s' if n != 1 else ''}"
            + (f' matching "{q}"' if q else
               " · search by callsign, name, city, or state"))

    def _on_select(self, contact: dict):
        self.contact_selected.emit(contact)
        self.accept()

    def _on_add(self):
        dlg = _ContactEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.get_values()
            self.cdb.save(**v)
            self._refresh()

    def _on_edit(self, contact: dict):
        dlg = _ContactEditDialog(contact=contact, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.get_values()
            self.cdb.save(**v)
            self._refresh()

    def _on_edit_row(self, index):
        r = index.row()
        call_item = self.table.item(r, 0)
        if call_item:
            c = self.cdb.get_by_callsign(call_item.text())
            if c:
                self._on_edit(c)

    def _on_delete(self, callsign: str):
        r = QMessageBox.question(
            self, "Delete Contact",
            f"Remove {callsign} from address book?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.cdb.delete(callsign)
            self._refresh()


class _ContactEditDialog(QDialog):
    """Add / edit a single contact."""

    def __init__(self, contact: dict = None, parent=None):
        super().__init__(parent)
        c = contact or {}
        self.setWindowTitle("Edit Contact" if contact else "Add Contact")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setContentsMargins(12, 12, 12, 4)

        self.e_call     = QLineEdit(c.get("callsign", ""))
        self.e_name     = QLineEdit(c.get("name", ""))
        self.e_qth      = QLineEdit(c.get("qth", ""))
        self.e_home_bbs = QLineEdit(c.get("home_bbs", ""))

        self.e_call.setPlaceholderText("e.g. N0CALL")
        self.e_name.setPlaceholderText("e.g. Bob Novak")
        self.e_qth.setPlaceholderText("e.g. Flint, MI")
        self.e_home_bbs.setPlaceholderText("e.g. N0CALL.ST.USA.NOAM")

        if contact:
            self.e_call.setReadOnly(True)   # can't change callsign on edit

        form.addRow("Callsign:", self.e_call)
        form.addRow("Name:",     self.e_name)
        form.addRow("City/St:",  self.e_qth)
        form.addRow("Home BBS:", self.e_home_bbs)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_accept(self):
        if not self.e_call.text().strip():
            QMessageBox.warning(self, "Missing Field", "Callsign cannot be blank.")
            return
        self.accept()

    def get_values(self) -> dict:
        return {
            "callsign": self.e_call.text().strip().upper(),
            "name":     self.e_name.text().strip(),
            "qth":      self.e_qth.text().strip(),
            "home_bbs": self.e_home_bbs.text().strip().upper(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Compose Dialog
# ─────────────────────────────────────────────────────────────────────────────

class ComposeDialog(QDialog):
    def __init__(self, parent=None, reply_to=None, contacts_db=None, font_size=10):
        super().__init__(parent)
        self.cdb = contacts_db
        self._contacts = []   # cached for dropdown
        self._font_size = font_size
        self.setWindowTitle("Compose Message")
        self.setMinimumSize(540, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setContentsMargins(12, 12, 12, 4)

        # ── To field: input + dropdown arrow + address book icon ──
        to_row = QHBoxLayout()
        to_row.setSpacing(4)
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("callsign")
        self.to_edit.textChanged.connect(self._on_to_changed)
        to_row.addWidget(self.to_edit)

        # Dropdown arrow button — shows quick-pick popup
        self.drop_btn = QPushButton("▾")
        self.drop_btn.setFixedSize(26, 32)
        self.drop_btn.setToolTip("Recent / frequent contacts")
        self.drop_btn.clicked.connect(self._show_dropdown)
        to_row.addWidget(self.drop_btn)

        # Address book icon button
        self.ab_btn = QPushButton("👤")
        self.ab_btn.setFixedSize(32, 32)
        self.ab_btn.setToolTip("Open address book")
        self.ab_btn.clicked.connect(self._open_address_book)
        to_row.addWidget(self.ab_btn)
        form.addRow("To (callsign):", to_row)

        self.at_bbs_edit  = QLineEdit()
        self.subject_edit = QLineEdit()
        self.type_combo   = QComboBox()
        self.type_combo.addItems(["Personal (P)", "Bulletin (B)"])
        self.at_bbs_edit.setPlaceholderText(
            "e.g. N0CALL.ST.USA.NOAM  (leave blank if unknown)")
        form.addRow("@ Home BBS:",  self.at_bbs_edit)
        form.addRow("Subject:",     self.subject_edit)
        form.addRow("Type:",        self.type_combo)

        # "Send immediately on next connection to any BBS" was here, and
        # it never controlled anything: _on_send_outbox sends every pending
        # row to whichever BBS is connected and has never consulted the
        # flag. Unticking it did not hold a message back. Removed rather
        # than implemented — the Outbox now means one thing, "these go on
        # the next Send / Receive", and that is one less decision per
        # message (Bill, 2026-09-23). The send_now columns stay in the
        # database so existing rows still load.

        self.save_link = QPushButton("+ save to address book")
        self.save_link.setFlat(True)
        self.save_link.setStyleSheet(
            "color: palette(highlight); font-size: 12px; text-align: left;")
        self.save_link.setVisible(False)
        self.save_link.clicked.connect(self._save_to_address_book)
        form.addRow("", self.save_link)

        layout.addLayout(form)

        self.body_edit = QTextEdit()
        self.body_edit.setFont(QFont("Courier New", self._font_size))
        self.body_edit.setPlaceholderText("Type your message here...")
        layout.addWidget(self.body_edit)

        if reply_to:
            self.to_edit.setText(reply_to.get("from_call", ""))
            self.at_bbs_edit.setText(reply_to.get("bbs_source", ""))
            self.subject_edit.setText("Re: " + (reply_to.get("subject", "") or ""))
            quoted = "\n".join(
                f"> {l}" for l in (reply_to.get("body", "") or "").splitlines())
            self.body_edit.setPlainText(f"\n\n--- Original ---\n{quoted}")

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # Cache top contacts for dropdown
        if self.cdb:
            self._contacts = self.cdb.get_top(5)

    def _show_dropdown(self):
        """Show a popup menu with top contacts for quick selection."""
        from PyQt6.QtWidgets import QMenu
        if not self._contacts:
            return
        menu = QMenu(self)
        for c in self._contacts:
            call = c["callsign"]
            name = c.get("name") or ""
            qth  = c.get("qth") or ""
            label = call
            if name or qth:
                label += f"   {name}"
                if qth: label += f" — {qth}"
            act = menu.addAction(label)
            act.setData(c)
        chosen = menu.exec(
            self.drop_btn.mapToGlobal(
                self.drop_btn.rect().bottomLeft()))
        if chosen:
            self._fill_from_contact(chosen.data())

    def _fill_from_contact(self, c: dict):
        self.to_edit.setText(c["callsign"])
        self.at_bbs_edit.setText(c.get("home_bbs") or "")
        self.save_link.setVisible(False)
        self.subject_edit.setFocus()

    def _on_to_changed(self, text):
        text = text.strip().upper()
        if self.cdb and text:
            contact = self.cdb.get_by_callsign(text)
            if contact:
                self.at_bbs_edit.setText(contact.get("home_bbs") or "")
                self.save_link.setVisible(False)
            else:
                self.save_link.setVisible(True)
        else:
            self.save_link.setVisible(False)

    def _open_address_book(self):
        if not self.cdb:
            QMessageBox.information(self, "Address Book",
                "No address book available.")
            return
        dlg = AddressBookDialog(self.cdb, parent=self, select_mode=True)
        dlg.contact_selected.connect(self._fill_from_contact)
        dlg.exec()

    def _save_to_address_book(self):
        if not self.cdb:
            return
        call = self.to_edit.text().strip().upper()
        if not call:
            return
        dlg = _ContactEditDialog(
            contact={"callsign": call,
                     "home_bbs": self.at_bbs_edit.text().strip().upper()},
            parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.get_values()
            self.cdb.save(**v)
            self.save_link.setVisible(False)
            self._contacts = self.cdb.get_top(5)
            QMessageBox.information(self, "Saved",
                f"{v['callsign']} added to address book.")

    def get_values(self):
        return {
            "to_call":  self.to_edit.text().strip().upper(),
            "at_bbs":   self.at_bbs_edit.text().strip().upper(),
            "subject":  self.subject_edit.text().strip(),
            "body":     self.body_edit.toPlainText(),
            "msg_type": "P" if self.type_combo.currentIndex() == 0 else "B",
        }
# ─────────────────────────────────────────────────────────────────────────────
# Terminal Widget
# ─────────────────────────────────────────────────────────────────────────────

class TerminalWidget(QWidget):
    """
    Full-screen terminal view.
    Shown when user clicks the Terminal toolbar button.
    In Phase 2 this will be wired to a live session for real command I/O.
    """
    # Signal to send a command — will be connected to worker in Phase 2
    sig_send_cmd = pyqtSignal(str)
    # Signal emitted when user clicks the Get File button
    sig_get_file = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # Header bar
        hdr = QHBoxLayout()
        hdr_label = QLabel("📟  Terminal — Raw BBS Session")
        hdr_label.setStyleSheet("font-weight:bold; font-size:13px;")
        self.get_file_btn = QPushButton("📁 File Download - YAPP")
        self.get_file_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.get_file_btn.setFixedWidth(190)
        self.get_file_btn.setToolTip(
            "Download a file from the BBS using YAPP.\n"
            "Type 'files' first to see what's available.")
        self.get_file_btn.setEnabled(False)   # enabled on connect
        self.get_file_btn.clicked.connect(self.sig_get_file.emit)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setFixedWidth(60)
        self.clear_btn.clicked.connect(self._clear)
        hdr.addWidget(hdr_label)
        hdr.addStretch()
        hdr.addWidget(self.get_file_btn)
        hdr.addWidget(self.clear_btn)
        layout.addLayout(hdr)

        # Output area
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Courier New", 10))
        self.output.setStyleSheet(
            "background:#0d0d0d; color:#00ff00;"
            "border:1px solid #2a2a2a; border-radius:4px;")
        layout.addWidget(self.output)

        # Input row
        il = QHBoxLayout()
        prompt = QLabel(">")
        prompt.setFont(QFont("Courier New", 11))
        prompt.setStyleSheet("color:#00ff00; background:#0d0d0d; padding:2px 6px;")

        self.input_line = QLineEdit()
        self.input_line.setFont(QFont("Courier New", 10))
        self.input_line.setStyleSheet(
            "background:#1a1a1a; color:#00ff00;"
            "border:1px solid #444; padding:3px;")
        self.input_line.setPlaceholderText("Type BBS command and press Enter...")
        self.input_line.returnPressed.connect(self._send)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(64)
        # No button in the Terminal may hold keyboard focus. On GNOME /
        # Wayland, Enter PRESSES whichever button has focus, so clicking the
        # LL 20 quick button and then pressing Enter at the page prompt sent
        # LL 20 again instead of a bare CR (J5, 2026-09-28, VARA HF — the
        # BBS log shows both). Offscreen Qt does not do this, so the test
        # harness never saw it. Typing always stays in the input box.
        send_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        send_btn.clicked.connect(self._send)

        il.addWidget(prompt)
        il.addWidget(self.input_line)
        il.addWidget(send_btn)
        layout.addLayout(il)

        # Quick-command buttons
        ql = QHBoxLayout()
        ql.addWidget(QLabel("Quick:"))
        for label, cmd, tip in [
            ("L",      "L",      "L — new messages since your last L"),
            ("LM",     "LM",     "LM — list the messages addressed to you"),
            ("LL 20",  "LL 20",  "LL 20 — list the last 20 messages"),
            ("RM",     "RM",     "RM — read your new messages"),
            ("KM",     "KM",     ""),          # set below, it needs the warning
            ("I",      "I",      "I — information about this BBS"),
            ("?",      "?",      "? — the BBS command list"),
            ("B",      "B",      "B — say goodbye and disconnect"),
            ("Files",  "files",  "FILES — list the files available for download"),
            ("Read",   "read ",  "READ <name> — read a text file from the Files "
                                 "area,\ne.g.  read a_file_name.txt\n"
                                 "Click FILES first to see what is there. For "
                                 "binary files\nuse the YAPP button instead."),
            # The two answers a paged listing will accept. Without these the
            # only way out of an OP page prompt was to know that a bare CR
            # continues and A aborts (Bill, 2026-09-23).
            ("⏎ CR",   "",       "Send a bare carriage return — answers the "
                                 "BBS page prompt\n<CR> Continue, and is what "
                                 "Enter on an empty box sends."),
            ("A",      "A",      "A — abort the BBS output while it is paging, "
                                 "and get\nback to the command prompt."),
        ]:
            b = QPushButton(label)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # see send_btn
            b.setFixedHeight(26)
            b.setFixedWidth(54)
            if tip:
                b.setToolTip(tip)
            if cmd.strip().upper() == "KM":
                # Destructive — don't let it sit here looking like L or LM.
                # MainWindow._confirm_destructive also asks before it goes.
                b.setStyleSheet("QPushButton { color:#ffcccc; "
                                "background-color:#5a2a2a; "
                                "border:1px solid #aa4444; }")
                b.setToolTip("KM — Kill Mine: kills your \"read\" "
                             "messages ON THE BBS.\nAsks first. Your mail "
                             "is still in the QtC Inbox.")
            if cmd.endswith(" "):
                # Commands that need a filename: put them in the input box
                # so the user can complete the name before sending
                b.clicked.connect(lambda _, c=cmd: self._prefill(c))
            else:
                b.clicked.connect(lambda _, c=cmd: self._quick(c))
            ql.addWidget(b)
        ql.addStretch()
        layout.addLayout(ql)

    def set_connected(self, connected: bool):
        """Enable or disable the Get File button based on connection state."""
        self.get_file_btn.setEnabled(connected)

    def append(self, text: str, color: str = "#00ff00"):
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        safe = (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
                .replace(" ", "&nbsp;"))
        self.output.insertHtml(f'<span style="color:{color};">{safe}</span>')
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _clear(self):
        self.output.clear()

    def _send(self):
        """Send what is typed — including nothing at all.

        An empty box used to return here, which meant there was no way to
        answer BPQ's page prompt: `<A>bort, <CR> Continue..>` takes only A
        or a bare CR, and Enter on an empty line was being swallowed. A
        listing that hit the OP limit could not be continued from the
        Terminal at all (Bill, 2026-09-23). session._send("") already puts
        a bare CR on the air — the paging code answers page prompts with
        exactly that."""
        cmd = self.input_line.text().strip()
        self.input_line.clear()
        self.input_line.setFocus()   # the next Enter belongs to the box
        self.sig_send_cmd.emit(cmd)

    def _quick(self, cmd: str):
        self.input_line.setText(cmd)
        self._send()

    # Shown in the terminal when a prefilled command is waiting for an
    # argument. The input box has the command in it, so its placeholder
    # text can never appear — the hint has to go on screen instead.
    _PREFILL_HINTS = {
        "read ": "[Type the file name after READ, e.g.  read a_file_name.txt "
                 "— click FILES to see what is there]",
    }

    def _prefill(self, cmd: str):
        """Put cmd in the input box and focus it — user adds filename then sends."""
        self.input_line.setText(cmd)
        self.input_line.setFocus()
        hint = self._PREFILL_HINTS.get(cmd)
        if hint:
            self.append(f"\n{hint}\n", "#888888")


# ─────────────────────────────────────────────────────────────────────────────
# Debug Widget  — verbose monitoring output (PTT, BITRATE, SYS, etc.)
# ─────────────────────────────────────────────────────────────────────────────

class DebugWidget(QWidget):
    """
    Debug view — shows all verbose session output:
    [PTT], [RX-CMD], [TX-CMD], [SYS], [RX], [TX], [BITRATE], [IAMALIVE], etc.
    Useful for development and troubleshooting.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        hdr = QHBoxLayout()
        hdr_label = QLabel("🔬  Debug — Verbose Session Monitor")
        hdr_label.setStyleSheet("font-weight:bold; font-size:13px;")
        self.save_btn = QPushButton("Save Log…")
        self.save_btn.setFixedWidth(90)
        self.save_btn.clicked.connect(self._save)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFixedWidth(60)
        self.clear_btn.clicked.connect(self._clear)
        hdr.addWidget(hdr_label)
        hdr.addStretch()
        hdr.addWidget(self.save_btn)
        hdr.addWidget(self.clear_btn)
        layout.addLayout(hdr)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Courier New", 9))
        self.output.setStyleSheet(
            "background:#0a0a1a; color:#8888ff;"
            "border:1px solid #2a2a4a; border-radius:4px;")
        layout.addWidget(self.output)

    def append(self, text: str, color: str = "#8888ff"):
        # Prepend a millisecond-precision timestamp to each call so debug
        # logs are timing-analyzable (block stalls, BBS round-trips, etc.).
        # Leading newlines are kept BEFORE the timestamp so visual separators
        # like "\n=== Connected ===\n" still render with their blank line.
        stripped = text.lstrip("\n")
        leading_nl = text[:len(text) - len(stripped)]
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]   # HH:MM:SS.mmm
        text = f"{leading_nl}[{ts}] {stripped}"

        self.output.moveCursor(QTextCursor.MoveOperation.End)
        safe = (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
                .replace(" ", "&nbsp;"))
        self.output.insertHtml(f'<span style="color:{color};">{safe}</span>')
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _clear(self):
        self.output.clear()

    def _save(self):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = os.path.join(
            os.path.expanduser("~"), f"qtc-debug-{ts}.log")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Debug Log", default, "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.output.toPlainText())
        except OSError as e:
            QMessageBox.warning(self, "Save Log", f"Could not write log:\n{e}")


# ─────────────────────────────────────────────────────────────────────────────
# Mail View Widget  (extracted so it can be swapped in/out cleanly)
# ─────────────────────────────────────────────────────────────────────────────

class MailView(QWidget):
    """The full inbox/outbox/sent three-pane mail view."""

    # Signals up to MainWindow
    sig_new_message    = pyqtSignal()
    sig_reply          = pyqtSignal()
    sig_delete         = pyqtSignal()
    sig_send_outbox    = pyqtSignal()
    sig_mark_all_read  = pyqtSignal()
    sig_search         = pyqtSignal()       # search button clicked
    sig_folder_changed = pyqtSignal(str)        # folder name
    sig_row_selected   = pyqtSignal(int, str)   # (row_id, folder)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        h = QSplitter(Qt.Orientation.Horizontal)

        # ── Folder tree ───────────────────────────────────────
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setMaximumWidth(185)
        self.folder_tree.setMinimumWidth(135)

        self._fi = QTreeWidgetItem(self.folder_tree, ["📥  Inbox"])
        self._fo = QTreeWidgetItem(self.folder_tree, ["📤  Outbox"])
        self._fs = QTreeWidgetItem(self.folder_tree, ["📨  Sent"])
        self._fn = QTreeWidgetItem(self.folder_tree, ["🔔  Notifications"])

        # Bulletins parent folder — children added dynamically
        self._fb = QTreeWidgetItem(self.folder_tree, ["📋  Bulletins"])
        self._fb.setExpanded(True)
        self._bulletin_cat_items = {}   # category -> QTreeWidgetItem

        self.folder_tree.expandAll()
        self.folder_tree.currentItemChanged.connect(self._folder_changed)
        self.folder_tree.setCurrentItem(self._fi)
        h.addWidget(self.folder_tree)

        # ── Right: message list + preview ─────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        v = QSplitter(Qt.Orientation.Vertical)

        # Search bar — hidden by default, shown when search button clicked
        self._search_bar = QWidget()
        sb_layout = QHBoxLayout(self._search_bar)
        sb_layout.setContentsMargins(0, 2, 0, 2)
        sb_layout.setSpacing(6)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search From, Subject, Body…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        self._search_scope = QComboBox()
        self._search_scope.addItems(
            ["All folders", "Inbox", "Outbox", "Sent",
             "Notifications", "Bulletins"])
        self._search_scope.setFixedWidth(120)
        self._search_scope.currentIndexChanged.connect(self._on_search_changed)
        self._search_close = QPushButton("✕")
        self._search_close.setFixedWidth(28)
        self._search_close.setToolTip("Close search")
        self._search_close.clicked.connect(self._close_search)
        sb_layout.addWidget(QLabel("🔍"))
        sb_layout.addWidget(self._search_edit)
        sb_layout.addWidget(self._search_scope)
        sb_layout.addWidget(self._search_close)
        self._search_bar.setVisible(False)
        rl.addWidget(self._search_bar)

        # Message table
        self.msg_table = QTableWidget(0, 5)
        self.msg_table.setHorizontalHeaderLabels(["", "From", "Subject", "Date", "Size"])
        hh = self.msg_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.msg_table.setColumnWidth(0, 20)
        self.msg_table.setColumnWidth(1, 115)
        self.msg_table.setColumnWidth(3, 72)
        self.msg_table.setColumnWidth(4, 52)
        self.msg_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.msg_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.msg_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.msg_table.setAlternatingRowColors(True)
        self.msg_table.verticalHeader().setVisible(False)
        self.msg_table.currentCellChanged.connect(self._row_selected)
        self.msg_table.selectionModel().selectionChanged.connect(
            self._on_selection_changed)
        v.addWidget(self.msg_table)

        # Preview pane
        pf = QFrame()
        pf.setFrameShape(QFrame.Shape.StyledPanel)
        pl = QVBoxLayout(pf)
        pl.setContentsMargins(6, 6, 6, 6)

        self.preview_header = QLabel("")
        self.preview_header.setWordWrap(True)
        self.preview_header.setStyleSheet(
            "padding:7px; border-radius:4px;"
            "font-size:11px;")
        self.preview_body = QTextEdit()
        self.preview_body.setReadOnly(True)
        self.preview_body.setFont(QFont("Courier New", 10))
        pl.addWidget(self.preview_header)
        pl.addWidget(self.preview_body)
        v.addWidget(pf)
        v.setSizes([290, 330])

        rl.addWidget(v)

        # Button bar
        bl = QHBoxLayout()
        self.btn_new    = QPushButton("✏  New Message")
        self.btn_reply  = QPushButton("↩  Reply")
        self.btn_delete = QPushButton("🗑  Delete")
        self.btn_search = QPushButton("🔍  Search")
        self.btn_mark_all_read = QPushButton("✓  Mark All Read")
        self.btn_send_outbox = QPushButton("🔄  Send / Receive")

        self.btn_reply.setEnabled(False)
        self.btn_delete.setEnabled(False)
        # Enabled whenever a session is up (MainWindow._on_connected), not
        # only when the outbox has something — it receives as well as sends.
        self.btn_send_outbox.setEnabled(False)
        self.btn_send_outbox.setToolTip(
            "Send anything waiting in the Outbox, then check for new mail "
            "(LM) and download it.\nBulletins are not included — those run "
            "on a Mail view connect.")

        self.btn_new.clicked.connect(self.sig_new_message)
        self.btn_reply.clicked.connect(self.sig_reply)
        self.btn_delete.clicked.connect(self.sig_delete)
        self.btn_search.clicked.connect(self._toggle_search)
        self.btn_mark_all_read.clicked.connect(self.sig_mark_all_read)
        self.btn_send_outbox.clicked.connect(self.sig_send_outbox)

        bl.addWidget(self.btn_new)
        bl.addWidget(self.btn_reply)
        bl.addWidget(self.btn_delete)
        bl.addWidget(self.btn_search)
        bl.addWidget(self.btn_mark_all_read)
        bl.addStretch()
        bl.addWidget(self.btn_send_outbox)
        rl.addLayout(bl)

        h.addWidget(right)
        h.setSizes([160, 900])
        layout.addWidget(h)

        self._setup_list_hotkeys()
        self._setup_tab_cycle()
        self._setup_pane_focus_style()

    def _setup_pane_focus_style(self):
        """Subtle 1px green edge marks the active reading pane (issue #3).
        Paired with the dimmed inactive selection in _apply_dark_palette,
        this makes it obvious which of the three panes Tab — or a click —
        landed on, instead of every pane looking equally highlighted."""
        edge = "#00a050"   # theme green accent (matches dark-mode Highlight)

        def qss(cls, idle):
            return (f"{cls} {{ border: 1px solid {idle}; border-radius: 3px; }}"
                    f"{cls}:focus {{ border: 1px solid {edge}; }}")

        self.folder_tree.setStyleSheet(qss("QTreeWidget",  "palette(mid)"))
        self.msg_table.setStyleSheet(qss("QTableWidget", "palette(mid)"))
        # The body already sits inside a framed panel, so keep it borderless
        # until focused to avoid a double edge.
        self.preview_body.setStyleSheet(qss("QTextEdit",  "transparent"))

    def focus_default(self):
        """Put keyboard focus on the first pane of the Tab cycle so Tab stays
        inside the three reading panes from the start, instead of beginning up
        in the connection toolbar (issue #3)."""
        self.folder_tree.setFocus(Qt.FocusReason.OtherFocusReason)

    def _setup_tab_cycle(self):
        """Tab cycles focus through exactly three reading panes (issue #3):
        folder tree → message list → message body → back to the folder tree
        (Shift+Tab reverses). The cycle is closed — Tab never escapes up to
        the connection/toolbar controls. Arrow keys keep their natural job in
        each pane: select the folder, select the message, or (in the body)
        scroll the text for reading. Implemented as an event filter so Tab is
        intercepted before the item views consume it for cell navigation."""
        self._tab_cycle = (self.folder_tree, self.msg_table, self.preview_body)
        for wdg in self._tab_cycle:
            wdg.installEventFilter(self)
        # Stop the body from swallowing Tab as a literal tab character.
        self.preview_body.setTabChangesFocus(True)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and obj in self._tab_cycle:
            key = event.key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                idx  = self._tab_cycle.index(obj)
                step = -1 if key == Qt.Key.Key_Backtab else 1
                nxt  = self._tab_cycle[(idx + step) % len(self._tab_cycle)]
                nxt.setFocus(Qt.FocusReason.TabFocusReason)
                return True
            # In the reading pane, Up/Down scroll the body a line at a time so
            # there's immediate feedback (a read-only QTextEdit otherwise just
            # walks an invisible cursor before the view moves).
            if obj is self.preview_body and key in (
                    Qt.Key.Key_Up, Qt.Key.Key_Down):
                sb = self.preview_body.verticalScrollBar()
                step = sb.singleStep() or 1
                sb.setValue(sb.value()
                            + (step if key == Qt.Key.Key_Down else -step))
                return True
        return super().eventFilter(obj, event)

    def _setup_list_hotkeys(self):
        """Single-keystroke commands for keyboard users while the message
        list has focus (issue #3). Each one clicks the matching button, so
        the enabled/disabled state is honored exactly and the same code path
        runs. They are scoped to the table (WidgetWithChildrenShortcut), so
        typing in the search box or a dialog is never hijacked. The
        point-n-click buttons stay for mouse users."""
        def bind(keys, button):
            for k in keys:
                sc = QShortcut(QKeySequence(k), self.msg_table)
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(button.click)

        bind(["N"],           self.btn_new)
        bind(["R"],           self.btn_reply)
        bind(["D", "Delete"], self.btn_delete)
        bind(["F"],           self.btn_search)
        bind(["M"],           self.btn_mark_all_read)

        # Enter on a selected row jumps into the reading pane so the body can
        # be scrolled with PageUp/PageDown / arrows; Esc returns to the list.
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            sc = QShortcut(QKeySequence(key), self.msg_table)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self._kb_enter_reading_pane)
        sc_back = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.preview_body)
        sc_back.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_back.activated.connect(self.msg_table.setFocus)

    def _kb_enter_reading_pane(self):
        if self.msg_table.currentRow() >= 0:
            self.preview_body.setFocus()

    def _folder_changed(self, current, _):
        if current is None:
            return
        if   current is self._fi: self.sig_folder_changed.emit("inbox")
        elif current is self._fo: self.sig_folder_changed.emit("outbox")
        elif current is self._fs: self.sig_folder_changed.emit("sent")
        elif current is self._fn: self.sig_folder_changed.emit("notifications")
        elif current is self._fb: self.sig_folder_changed.emit("bulletins")
        else:
            # Check if it's a bulletin category child
            for cat, item in self._bulletin_cat_items.items():
                if current is item:
                    self.sig_folder_changed.emit(f"bulletin:{cat}")
                    return

    def update_bulletin_categories(self, categories: list):
        """
        Refresh bulletin category subfolders in the folder tree.
        categories = list of dicts: {category, total, unread}
        """
        # Remember current selection so we can restore it after rebuild
        current = self.folder_tree.currentItem()
        current_cat = None
        for cat, item in self._bulletin_cat_items.items():
            if current is item:
                current_cat = cat
                break

        # Block signals while rebuilding to prevent spurious folder changes
        self.folder_tree.blockSignals(True)
        try:
            # Remove old category items
            for item in self._bulletin_cat_items.values():
                self._fb.removeChild(item)
            self._bulletin_cat_items.clear()

            # Add current categories
            for c in categories:
                cat    = c["category"]
                unread = c.get("unread", 0)
                label  = f"📄  {cat} ({unread} new)" if unread else f"📄  {cat}"
                item   = QTreeWidgetItem(self._fb, [label])
                self._bulletin_cat_items[cat] = item

            # Update parent label
            total_unread = sum(c.get("unread", 0) for c in categories)
            self._fb.setText(
                0, f"📋  Bulletins ({total_unread} new)"
                if total_unread else "📋  Bulletins")
            self._fb.setExpanded(True)

            # Restore selection to the category item the user was on
            if current_cat and current_cat in self._bulletin_cat_items:
                self.folder_tree.setCurrentItem(
                    self._bulletin_cat_items[current_cat])
        finally:
            self.folder_tree.blockSignals(False)

    def _toggle_search(self):
        """Show or hide the search bar."""
        visible = not self._search_bar.isVisible()
        self._search_bar.setVisible(visible)
        self.btn_search.setStyleSheet(
            "QPushButton { background-color: #2a4a6a; color: #88ccff; "
            "border: 1px solid #4488aa; }" if visible else "")
        if visible:
            self._search_edit.setFocus()
        else:
            self._close_search()

    def _close_search(self):
        """Close search bar and restore normal folder view."""
        self._search_bar.setVisible(False)
        self.btn_search.setStyleSheet("")
        self._search_edit.blockSignals(True)
        self._search_edit.clear()
        self._search_edit.blockSignals(False)
        # Re-emit current folder to restore full list
        self._folder_changed(self.folder_tree.currentItem(), None)

    def _on_search_changed(self):
        """Called when search text or scope changes — runs search immediately."""
        term = self._search_edit.text().strip().lower()
        if not term:
            # Empty search — restore normal folder view
            self._folder_changed(self.folder_tree.currentItem(), None)
            return
        self.sig_search.emit()

    def run_search(self, term: str, scope: str, all_rows: dict):
        """
        Filter and display search results.
        all_rows = {"inbox": [...], "outbox": [...], "sent": [...]}
        scope = "All folders" / "Inbox" / "Outbox" / "Sent" / "Bulletins"
        """
        term = term.lower()
        results = []

        scope_map = {
            "All folders":   ["inbox", "outbox", "sent",
                              "notifications", "bulletins"],
            "Inbox":         ["inbox"],
            "Outbox":        ["outbox"],
            "Sent":          ["sent"],
            "Notifications": ["notifications"],
            "Bulletins":     ["bulletins"],
        }
        folders = scope_map.get(scope, ["inbox", "outbox", "sent"])

        for folder in folders:
            for rd in all_rows.get(folder, []):
                from_call = (rd.get("from_call") or rd.get("to_call") or "").lower()
                to_call   = (rd.get("to_call") or rd.get("category") or "").lower()
                subject   = (rd.get("subject") or "").lower()
                body      = (rd.get("body") or "").lower()
                if (term in from_call or term in to_call
                        or term in subject or term in body):
                    # For bulletins use bulletin: prefix so preview works
                    disp_folder = (f"bulletin:{rd['category']}"
                                   if folder == "bulletins" else folder)
                    results.append((rd, disp_folder))

        # Display results in table
        self.msg_table.setRowCount(0)
        self.preview_header.setText("")
        self.preview_body.clear()
        self.msg_table.setHorizontalHeaderLabels(
            ["", "From/To", "Subject", "Date", "Size"])
        for rd, folder in results:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            unread = not rd.get("read", 0)
            font = QFont(); font.setBold(unread)
            dot = QTableWidgetItem("●" if unread else "")
            dot.setForeground(QColor("#0066cc" if unread else "#cccccc"))
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.msg_table.setItem(r, 0, dot)
            call = rd.get("from_call") or rd.get("to_call") or ""
            date_str = self._short_date(
                rd.get("date") or rd.get("sent_at") or "")
            size_str = str(rd.get("size") or rd.get("body_size") or "")
            for c, v in enumerate(
                    [call, rd.get("subject") or "", date_str, size_str], start=1):
                it = QTableWidgetItem(v)
                it.setFont(font)
                it.setData(Qt.ItemDataRole.UserRole, rd["id"])
                it.setData(Qt.ItemDataRole.UserRole + 1, folder)
                self.msg_table.setItem(r, c, it)
        self.msg_table.resizeRowsToContents()

    def get_search_term(self) -> str:
        return self._search_edit.text().strip()

    def get_search_scope(self) -> str:
        return self._search_scope.currentText()

    def _row_selected(self, row, col, pr, pc):
        it = self.msg_table.item(row, 1)
        if not it:
            return
        row_id = it.data(Qt.ItemDataRole.UserRole)
        folder_override = it.data(Qt.ItemDataRole.UserRole + 1)
        if row_id is not None:
            folder = folder_override if folder_override else self._current_folder()
            self.sig_row_selected.emit(row_id, folder)

    def _on_selection_changed(self):
        """Update delete button label to show count when multiple selected."""
        rows = self.get_selected_ids()
        count = len(rows)
        if count > 1:
            self.btn_delete.setText(f"🗑  Delete ({count})")
            self.btn_delete.setEnabled(True)
        elif count == 1:
            self.btn_delete.setText("🗑  Delete")
            self.btn_delete.setEnabled(True)
        else:
            self.btn_delete.setText("🗑  Delete")
            self.btn_delete.setEnabled(False)

    def get_selected_ids(self) -> list:
        """Return list of (row_id, folder) tuples for all selected rows."""
        seen = set()
        result = []
        for idx in self.msg_table.selectionModel().selectedRows():
            it = self.msg_table.item(idx.row(), 1)
            if not it:
                continue
            row_id = it.data(Qt.ItemDataRole.UserRole)
            if row_id is None or row_id in seen:
                continue
            seen.add(row_id)
            folder_override = it.data(Qt.ItemDataRole.UserRole + 1)
            folder = folder_override if folder_override else self._current_folder()
            result.append((row_id, folder))
        return result

    def _current_folder(self):
        cur = self.folder_tree.currentItem()
        if   cur is self._fi: return "inbox"
        elif cur is self._fo: return "outbox"
        elif cur is self._fs: return "sent"
        elif cur is self._fn: return "notifications"
        return "inbox"

    # ── Public methods called by MainWindow ────────────────────

    def update_folder_counts(self, unread: int, pending: int, sent: int = 0,
                             notifications: int = 0):
        self._fi.setText(0, f"📥  Inbox ({unread} new)" if unread else "📥  Inbox")
        self._fo.setText(0, f"📤  Outbox ({pending})"   if pending else "📤  Outbox")
        self._fs.setText(0, f"📨  Sent ({sent})"        if sent    else "📨  Sent")
        self._fn.setText(
            0, f"🔔  Notifications ({notifications} new)"
            if notifications else "🔔  Notifications")

    def load_table(self, rows, folder: str):
        """Populate the message table for the given folder."""
        self.msg_table.setRowCount(0)
        self.preview_header.setText("")
        self.preview_body.clear()
        self.btn_reply.setEnabled(False)
        self.btn_delete.setEnabled(False)
        is_bulletin = folder == "bulletins" or folder.startswith("bulletin:")
        is_notif    = folder == "notifications"
        self.btn_mark_all_read.setVisible(folder == "inbox" or is_notif)
        # No composing or replying to bulletins or QtC notifications
        self.btn_new.setVisible(not is_bulletin and not is_notif)
        self.btn_reply.setVisible(not is_bulletin and not is_notif)
        self.btn_delete.setVisible(True)

        if folder == "outbox":
            self._fill_outbox(rows)
        elif is_bulletin:
            self._fill_bulletins(rows, folder=folder)
        else:
            self._fill_inbox_sent(
                rows, show_from=(folder == "inbox" or is_notif),
                notifications=is_notif)

    def _short_date(self, raw: str) -> str:
        """Normalize any date string to short DD-Mon format.
        Handles both BBS format (16-Mar) and ISO (2026-03-16T...)."""
        if not raw:
            return ""
        raw = str(raw)[:10]
        # Already short BBS format e.g. "16-Mar"
        if len(raw) <= 6 and "-" in raw:
            return raw
        # ISO format YYYY-MM-DD
        try:
            from datetime import datetime
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
            return dt.strftime("%-d-%b")
        except Exception:
            return raw[:10]

    def _fill_inbox_sent(self, rows, show_from: bool, notifications: bool = False):
        self.msg_table.setHorizontalHeaderLabels(
            ["", "From" if show_from else "To", "Subject", "Date", "Size"])
        for rd in rows:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            unread = not rd.get("read", 0)
            font = QFont(); font.setBold(unread)

            # QtC notifications get a bell icon so they read as "from QtC,
            # not radio mail" — kept (greyed) even after they're read.
            if notifications:
                dot = QTableWidgetItem("🔔")
                dot.setForeground(QColor("#cc8800" if unread else "#cccccc"))
            else:
                dot = QTableWidgetItem("●" if unread else "")
                dot.setForeground(QColor("#0066cc" if unread else "#cccccc"))
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.msg_table.setItem(r, 0, dot)

            call = rd.get("from_call") or rd.get("to_call") or "" if show_from else rd.get("to_call") or ""
            date_str = self._short_date(rd.get("date") or rd.get("sent_at") or rd.get("date_local") or "")
            size_str = str(rd.get("size") or rd.get("size_bytes") or rd.get("body_size") or "")
            for c, v in enumerate(
                    [call, rd.get("subject") or "", date_str, size_str], start=1):
                it = QTableWidgetItem(v)
                it.setFont(font)
                it.setData(Qt.ItemDataRole.UserRole, rd["id"])
                self.msg_table.setItem(r, c, it)
        self.msg_table.resizeRowsToContents()

    def _fill_outbox(self, rows):
        # Size replaced a "Status" column that could only ever read
        # "Pending" — the outbox folder shows pending rows and nothing
        # else, so it never told anyone anything (Bill, 2026-09-23).
        # Bytes, counted the same way mark_sent() counts them, so the
        # number does not change when the message moves to Sent.
        self.msg_table.setHorizontalHeaderLabels(
            ["", "To", "Subject", "Queued", "Size"])
        for rd in rows:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            dot = QTableWidgetItem("📤")
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.msg_table.setItem(r, 0, dot)
            size_str = str(len((rd.get("body") or "").encode("utf-8")))
            for c, v in enumerate(
                    [rd["to_call"], rd["subject"] or "",
                     (rd["created_at"] or "")[:16], size_str], start=1):
                it = QTableWidgetItem(v)
                it.setData(Qt.ItemDataRole.UserRole, rd["id"])
                self.msg_table.setItem(r, c, it)
        self.msg_table.resizeRowsToContents()

    def _fill_bulletins(self, rows, folder: str = ""):
        self.msg_table.setHorizontalHeaderLabels(
            ["", "From", "Subject", "Date", "Size"])
        for rd in rows:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            unread = not rd.get("read", 0)
            font = QFont(); font.setBold(unread)
            dot = QTableWidgetItem("●" if unread else "")
            dot.setForeground(QColor("#0066cc" if unread else "#cccccc"))
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.msg_table.setItem(r, 0, dot)
            date_str = self._short_date(rd.get("date") or "")
            size_str = str(rd.get("size") or "")
            for c, v in enumerate(
                    [rd.get("from_call") or "",
                     rd.get("subject") or "",
                     date_str, size_str], start=1):
                it = QTableWidgetItem(v)
                it.setFont(font)
                it.setData(Qt.ItemDataRole.UserRole, rd["id"])
                it.setData(Qt.ItemDataRole.UserRole + 1, folder)
                self.msg_table.setItem(r, c, it)
        self.msg_table.resizeRowsToContents()

    def show_preview(self, rd, folder: str):
        """Show message in preview pane, updating button states."""
        is_bulletin = folder == "bulletins" or folder.startswith("bulletin:")
        self.btn_delete.setEnabled(True)   # enabled for all folders including bulletins
        self.btn_reply.setEnabled(folder == "inbox")

        if folder == "outbox":
            h = (f"<b>To:</b> {rd.get('to_call','')} &nbsp;"
                 f"<b>Subject:</b> {rd.get('subject') or '(none)'} &nbsp;"
                 f"<b>Queued:</b> {str(rd.get('created_at',''))[:16]}")
        elif folder == "sent":
            h = (f"<b>To:</b> {rd.get('to_call','')} &nbsp;"
                 f"<b>Subject:</b> {rd.get('subject') or '(none)'} &nbsp;"
                 f"<b>Sent:</b> {str(rd.get('sent_at',''))[:16]}")
        elif is_bulletin:
            cat = rd.get("category", "")
            at  = rd.get("at_bbs", "")
            h = (f"<b>Category:</b> {cat}"
                 + (f"@{at}" if at else "") + " &nbsp;"
                 f"<b>From:</b> {rd.get('from_call','')} &nbsp;"
                 f"<b>Subject:</b> {rd.get('subject') or '(none)'} &nbsp;"
                 f"<b>Date:</b> {str(rd.get('date') or '')[:16]}")
        else:
            h = (f"<b>From:</b> {rd.get('from_call','')} &nbsp;"
                 f"<b>To:</b> {rd.get('to_call','')} &nbsp;"
                 f"<b>Subject:</b> {rd.get('subject') or '(none)'} &nbsp;"
                 f"<b>Date:</b> {str(rd.get('date') or '')[:16]}")
        self.preview_header.setText(h)
        self.preview_body.setPlainText(
            rd["body"] or "(message body not yet downloaded)")

        # Highlight search term if search bar is active
        term = self._search_edit.text().strip()
        if self._search_bar.isVisible() and term:
            self._highlight_search_term(term)

    def _highlight_search_term(self, term: str):
        """Highlight all occurrences of term in the preview body."""
        from PyQt6.QtGui import QTextCharFormat, QTextCursor
        from PyQt6.QtCore import QRegularExpression
        # Clear any existing highlights first
        self.preview_body.setExtraSelections([])

        if not term:
            return

        highlight_fmt = QTextCharFormat()
        highlight_fmt.setBackground(QColor("#ccaa00"))   # amber highlight
        highlight_fmt.setForeground(QColor("#000000"))   # black text on amber

        selections = []
        doc = self.preview_body.document()
        cursor = QTextCursor(doc)

        # Use QRegularExpression for case-insensitive search
        regex = QRegularExpression(
            QRegularExpression.escape(term),
            QRegularExpression.PatternOption.CaseInsensitiveOption)

        while True:
            cursor = doc.find(regex, cursor)
            if cursor.isNull():
                break
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = highlight_fmt
            selections.append(sel)

        self.preview_body.setExtraSelections(selections)

        # Scroll to first match
        if selections:
            self.preview_body.setTextCursor(selections[0].cursor)

    def mark_row_read(self, row: int):
        """Remove bold and unread marker from a table row.
        Notification rows keep their 🔔 bell (just greyed); mail rows lose
        the blue dot."""
        dot = self.msg_table.item(row, 0)
        if dot:
            if dot.text() != "🔔":
                dot.setText("")
            dot.setForeground(QColor("#cccccc"))
        for c in range(1, 5):
            it = self.msg_table.item(row, c)
            if it:
                f = it.font(); f.setBold(False); it.setFont(f)

    def current_row_index(self) -> int:
        return self.msg_table.currentRow()

    def enable_send_outbox(self, enabled: bool):
        self.btn_send_outbox.setEnabled(enabled)


# ─────────────────────────────────────────────────────────────────────────────
# Settings Dialog
# ─────────────────────────────────────────────────────────────────────────────

class BulletinSelectDialog(QDialog):
    """
    Checkbox selection dialog shown when new bulletins are found.
    User can select which to download, see sizes and estimated time.
    """
    def __init__(self, bulletins_by_cat: dict, link_bps: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Bulletins Available")
        self.setMinimumSize(600, 400)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Header
        total_msgs = sum(len(v) for v in bulletins_by_cat.values())
        total_bytes = sum(m.size for msgs in bulletins_by_cat.values()
                          for m in msgs)
        hdr = QLabel(f"<b>{total_msgs} new bulletin(s) found across "
                     f"{len(bulletins_by_cat)} category(s) "
                     f"— {total_bytes:,} bytes total</b>")
        hdr.setWordWrap(True)
        layout.addWidget(hdr)

        # Warning
        warn = QLabel(
            "⚠️  Bulletins can be large. At slow VARA speeds, downloading many "
            "at once may exceed your BBS session time limit and get cut off. "
            "Select only what you need.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #cc8800; font-size: 11px;")
        layout.addWidget(warn)

        # Estimate
        if link_bps > 0:
            secs = int(total_bytes * 8 / link_bps)
            mins = secs // 60
            est_str = f"  ·  est. {mins}m {secs%60}s at {link_bps} bps" \
                      if mins else f"  ·  est. {secs}s at {link_bps} bps"
        else:
            est_str = ""
        self._est_label = QLabel(f"Selected: {total_bytes:,} bytes{est_str}")
        self._est_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._est_label)

        # Scroll area with checkboxes grouped by category
        scroll = QWidget()
        scroll_layout = QVBoxLayout(scroll)
        scroll_layout.setSpacing(4)
        self._checkboxes = []   # (checkbox, msg, category)
        self._link_bps = link_bps

        for cat, msgs in sorted(bulletins_by_cat.items()):
            # Category header
            cat_label = QLabel(f"<b>📋 {cat}</b> — {len(msgs)} new")
            cat_label.setStyleSheet("margin-top: 6px;")
            scroll_layout.addWidget(cat_label)
            for msg in msgs:
                chk = QCheckBox(
                    f"  #{msg.msg_number}  {msg.date}  "
                    f"{msg.size:>5} bytes  "
                    f"{msg.from_call:<10}  {msg.subject}")
                chk.setChecked(True)
                chk.setFont(QFont("Courier New", 9))
                chk.stateChanged.connect(self._update_estimate)
                scroll_layout.addWidget(chk)
                self._checkboxes.append((chk, msg, cat))

        from PyQt6.QtWidgets import QScrollArea
        sa = QScrollArea()
        sa.setWidget(scroll)
        sa.setWidgetResizable(True)
        sa.setMinimumHeight(200)
        layout.addWidget(sa)

        # Buttons
        btn_row = QHBoxLayout()
        btn_all  = QPushButton("✓  Select All")
        btn_none = QPushButton("✗  Select None")
        btn_all.clicked.connect(self._select_all)
        btn_none.clicked.connect(self._select_none)
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        btns = QDialogButtonBox()
        btn_dl   = btns.addButton("📥  Download Selected",
                                   QDialogButtonBox.ButtonRole.AcceptRole)
        btn_skip = btns.addButton("⏭  Skip Bulletins This Session",
                                   QDialogButtonBox.ButtonRole.RejectRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _select_all(self):
        for chk, _, _ in self._checkboxes:
            chk.setChecked(True)

    def _select_none(self):
        for chk, _, _ in self._checkboxes:
            chk.setChecked(False)

    def _update_estimate(self):
        selected_bytes = sum(msg.size for chk, msg, _ in self._checkboxes
                             if chk.isChecked())
        if self._link_bps > 0:
            secs = int(selected_bytes * 8 / self._link_bps)
            mins = secs // 60
            est_str = f"  ·  est. {mins}m {secs%60}s at {self._link_bps} bps" \
                      if mins else f"  ·  est. {secs}s at {self._link_bps} bps"
        else:
            est_str = ""
        self._est_label.setText(f"Selected: {selected_bytes:,} bytes{est_str}")

    def get_selected(self) -> dict:
        """Return {category: [BBSMessage]} for checked items only."""
        result = {}
        for chk, msg, cat in self._checkboxes:
            if chk.isChecked():
                result.setdefault(cat, []).append(msg)
        return result


class SettingsDialog(QDialog):
    """
    Full settings editor — covers User Identity, BBS List, and App preferences.
    Reads/writes the config dict in-memory; caller saves to disk on Accept.
    """

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._cfg = copy.deepcopy(config)
        self._dark = bool(config.get("app", {}).get("dark_mode", False))
        self._note_color = "#aaaaaa" if self._dark else "#666666"

        self.setWindowTitle("QtC — Settings")
        # Sized so the BBS List table fits all 8 columns (Type/Name/
        # Callsign/Freq/BW/Host/Port/Notes) at their default widths
        # without horizontal scrolling, and Notes has room to show
        # comment text — no need for the user to resize on each open.
        # Sized so the Bulletins tab's new Auto-Update + Recover groups
        # have room without squishing — the listbox needs ~110px and the
        # two groups together need ~440px of vertical real estate.
        self.setMinimumSize(820, 720)
        self.resize(900, 880)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_user_tab(),      "👤  My Station")
        tabs.addTab(self._build_bbs_tab(),       "📡  BBS List")
        tabs.addTab(self._build_ptt_tab(),       "🎙️  PTT")
        tabs.addTab(self._build_bulletins_tab(), "📋  Bulletins")
        tabs.addTab(self._build_mailcall_tab(),  "📬  Mail-Call !!!")
        tabs.addTab(self._build_app_tab(),       "⚙️  App")
        layout.addWidget(tabs)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ── Tab: My Station ───────────────────────────────────────────

    def _build_user_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(18, 18, 18, 18)
        form.setVerticalSpacing(12)

        u = self._cfg.get("user", {})

        self.e_callsign    = QLineEdit(u.get("callsign", ""))
        self.e_name        = QLineEdit(u.get("name", ""))
        self.e_qth         = QLineEdit(u.get("qth", ""))
        self.e_zip         = QLineEdit(u.get("zip", ""))
        self.e_home_bbs    = QLineEdit(u.get("home_bbs", ""))
        self.e_telnet_user = QLineEdit(u.get("telnet_user", ""))
        self.e_password    = QLineEdit(u.get("password", ""))
        self.e_password.setEchoMode(QLineEdit.EchoMode.Password)

        self.e_callsign.setPlaceholderText("e.g. N0CALL")
        self.e_name.setPlaceholderText("e.g. Bill  (sent on first BBS registration)")
        self.e_qth.setPlaceholderText("e.g. Anytown ST  (optional)")
        self.e_zip.setPlaceholderText("e.g. 12345  (optional)")
        self.e_home_bbs.setPlaceholderText(
            "e.g. N0CALL.#REGION.ST.USA.NOAM  (hierarchical routing address)")
        self.e_telnet_user.setPlaceholderText("e.g. n0call  (lowercase, case-sensitive)")
        self.e_password.setPlaceholderText("Telnet sysop password (blank for radio)")

        self.chk_show_pw = QCheckBox("Show")
        self.chk_show_pw.toggled.connect(
            lambda v: self.e_password.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password))

        pw_row = QHBoxLayout()
        pw_row.addWidget(self.e_password)
        pw_row.addWidget(self.chk_show_pw)

        form.addRow("Callsign:",         self.e_callsign)
        form.addRow("Name:",             self.e_name)
        form.addRow("QTH:",              self.e_qth)
        form.addRow("Zip/Postcode:",     self.e_zip)
        form.addRow("Home BBS:",         self.e_home_bbs)
        form.addRow("Telnet username:",  self.e_telnet_user)
        form.addRow("Telnet password:",  pw_row)

        note = QLabel(
            "<i>Name, QTH, Zip and Home BBS are auto-sent during new user "
            "registration on first connect to a BBS.<br>"
            "Telnet username is case-sensitive on LinBPQ nodes.<br>"
            "Callsign is always sent uppercase over radio.</i>")
        note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        note.setWordWrap(True)
        form.addRow("", note)

        return w

    # ── Tab: BBS List ─────────────────────────────────────────────

    # ── BBS List table: column layout ──────────────────────────────
    # Type-grouped columns. VARA entries fill Freq/BW (Host/Port show em-
    # dash); Telnet entries fill Host/Port (Freq/BW show em-dash). Makes
    # the table self-documenting for new VARA users — no confusion about
    # why a VARA row has an empty Host cell.
    _BBS_COLS = ["Type", "Name", "Callsign", "Freq", "BW",
                 "Host", "Port", "Notes"]
    _BBS_TRANSPORT_LABEL = {
        "vara_hf":    "VARA HF",
        "vara_fm":    "VARA FM",
        "telnet":     "Telnet",
        **AGW_MODEMS,
    }
    _BBS_NA = "—"   # em-dash placeholder for inapplicable cells

    def _build_bbs_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)

        # Table
        self.bbs_table = QTableWidget(0, len(self._BBS_COLS))
        self.bbs_table.setHorizontalHeaderLabels(self._BBS_COLS)
        # Fixed-ish widths for type/callsign/freq/bw/port; Name and Host
        # share whatever's left, Notes stretches. Widths picked so the
        # widest plausible content fits without truncation: "VARA HF"
        # in Type, "NARROW" in BW, "192.168.1.50" in Host, "8110" in Port.
        hdr = self.bbs_table.horizontalHeader()
        self.bbs_table.setColumnWidth(0, 85)    # Type
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.bbs_table.setColumnWidth(1, 140)   # Name
        self.bbs_table.setColumnWidth(2, 95)    # Callsign
        self.bbs_table.setColumnWidth(3, 90)    # Freq
        self.bbs_table.setColumnWidth(4, 90)    # BW   (fits NARROW)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.bbs_table.setColumnWidth(5, 130)   # Host (fits IPv4 dotted)
        self.bbs_table.setColumnWidth(6, 75)    # Port (fits 5-digit)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)  # Notes
        self.bbs_table.setSortingEnabled(True)
        self.bbs_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.bbs_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bbs_table.verticalHeader().setVisible(False)
        self.bbs_table.currentCellChanged.connect(self._bbs_row_changed)
        layout.addWidget(self.bbs_table)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_bbs_add  = QPushButton("➕  Add")
        self.btn_bbs_edit = QPushButton("✏  Edit")
        self.btn_bbs_del  = QPushButton("🗑  Remove")
        self.btn_bbs_edit.setEnabled(False)
        self.btn_bbs_del.setEnabled(False)
        self.btn_bbs_add.clicked.connect(self._bbs_add)
        self.btn_bbs_edit.clicked.connect(self._bbs_edit)
        self.btn_bbs_del.clicked.connect(self._bbs_del)
        btn_row.addWidget(self.btn_bbs_add)
        btn_row.addWidget(self.btn_bbs_edit)
        btn_row.addWidget(self.btn_bbs_del)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._reload_bbs_table()
        return w

    def _reload_bbs_table(self):
        # Disable sorting while populating — Qt re-sorts on every setItem
        # which would shuffle rows out from under us mid-fill.
        self.bbs_table.setSortingEnabled(False)
        self.bbs_table.setRowCount(0)
        for idx, e in enumerate(self._cfg.get("bbs_list", [])):
            transport = e.get("transport", "telnet")
            is_vara   = transport in ("vara_hf", "vara_fm")
            is_agw    = transport in AGW_MODEMS
            has_host  = transport == "telnet" or is_agw   # Host/Port apply
            type_lbl  = self._BBS_TRANSPORT_LABEL.get(transport, transport)
            port_val  = (e.get("agw_port", AGW_DEFAULT_PORT) if is_agw
                         else e.get("telnet_port", ""))
            cells = [
                type_lbl,
                e.get("name", ""),
                e.get("callsign", ""),
                e.get("freq", "") if is_vara else self._BBS_NA,
                e.get("bw", "")   if is_vara else self._BBS_NA,
                e.get("host", "") if has_host else self._BBS_NA,
                str(port_val)     if has_host and port_val != "" else
                                       (self._BBS_NA if not has_host else ""),
                e.get("notes", ""),
            ]
            r = self.bbs_table.rowCount()
            self.bbs_table.insertRow(r)
            for c, v in enumerate(cells):
                item = QTableWidgetItem(v)
                # Stash the source bbs_list index on column 0 so sorting
                # doesn't desync edit/delete from the underlying config.
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, idx)
                self.bbs_table.setItem(r, c, item)
        self.bbs_table.setSortingEnabled(True)

    def _bbs_selected_cfg_index(self) -> int:
        """Map the currently selected table row back to its index in
        self._cfg['bbs_list']. Returns -1 if no row selected. Needed
        because the table is sortable — row N may not be config[N]."""
        row = self.bbs_table.currentRow()
        if row < 0:
            return -1
        item = self.bbs_table.item(row, 0)
        if item is None:
            return -1
        idx = item.data(Qt.ItemDataRole.UserRole)
        return int(idx) if idx is not None else -1

    def _bbs_row_changed(self, row, *_):
        has = row >= 0
        self.btn_bbs_edit.setEnabled(has)
        self.btn_bbs_del.setEnabled(has)

    def _bbs_add(self):
        dlg = _BBSEntryDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._cfg.setdefault("bbs_list", []).append(dlg.get_entry())
            self._reload_bbs_table()

    def _bbs_edit(self):
        idx = self._bbs_selected_cfg_index()
        if idx < 0:
            return
        entry = self._cfg["bbs_list"][idx]
        dlg = _BBSEntryDialog(entry=entry, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._cfg["bbs_list"][idx] = dlg.get_entry()
            self._reload_bbs_table()

    def _bbs_del(self):
        idx = self._bbs_selected_cfg_index()
        if idx < 0:
            return
        name = self._cfg["bbs_list"][idx].get("callsign", "?")
        r = QMessageBox.question(self, "Remove BBS",
            f"Remove {name} from your BBS list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            del self._cfg["bbs_list"][idx]
            self._reload_bbs_table()

    # ── Tab: PTT ──────────────────────────────────────────────────

    def _build_ptt_tab(self) -> QWidget:
        from ptt import list_serial_ports

        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        ptt = self._cfg.get("ptt", {})

        # ── Mode + port + signal ───────────────────────
        mode_group = QFrame()
        mode_group.setFrameShape(QFrame.Shape.StyledPanel)
        mode_layout = QFormLayout(mode_group)
        mode_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        mode_layout.setVerticalSpacing(10)
        mode_layout.setContentsMargins(12, 12, 12, 12)

        self.ptt_mode = QComboBox()
        self.ptt_mode.addItems(["None (VOX / disabled)", "COM Port"])
        self.ptt_mode.setCurrentIndex(0 if ptt.get("mode", "none") == "none" else 1)
        self.ptt_mode.currentIndexChanged.connect(self._ptt_mode_changed)
        mode_layout.addRow("PTT Mode:", self.ptt_mode)

        port_row = QHBoxLayout()
        self.ptt_port = QComboBox()
        self.ptt_port.setMinimumWidth(160)
        self._ptt_refresh_ports(ptt.get("port", ""))
        port_row.addWidget(self.ptt_port)
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.setFixedWidth(90)
        btn_refresh.clicked.connect(
            lambda: self._ptt_refresh_ports(self.ptt_port.currentText()))
        port_row.addWidget(btn_refresh)
        port_row.addStretch()
        mode_layout.addRow("Serial port:", port_row)

        self.ptt_signal = QComboBox()
        self.ptt_signal.addItems(["RTS", "DTR", "RTS + DTR"])
        sig_map = {"rts": 0, "dtr": 1, "rts+dtr": 2}
        self.ptt_signal.setCurrentIndex(sig_map.get(ptt.get("signal", "rts"), 0))
        mode_layout.addRow("PTT signal:", self.ptt_signal)

        outer.addWidget(mode_group)

        # ── Test PTT ───────────────────────────────────
        test_row = QHBoxLayout()
        self.btn_ptt_test = QPushButton("🔴  Test PTT  (1 second key)")
        self.btn_ptt_test.setFixedHeight(32)
        self.btn_ptt_test.setStyleSheet(
            "QPushButton { background:#5a2020; color:#ffaaaa; border:1px solid #aa4444; }"
            "QPushButton:hover { background:#7a2020; }"
            "QPushButton:pressed { background:#aa2020; color:#ffffff; }")
        self.btn_ptt_test.clicked.connect(self._ptt_test)
        test_row.addWidget(self.btn_ptt_test)
        test_row.addStretch()
        outer.addLayout(test_row)

        # ── Help note ──────────────────────────────────
        note = QLabel(
            "<i><b>Digirig Mobile:</b> select its ttyUSB port, signal = RTS.<br>"
            "VARA sends PTT ON / PTT OFF on its command port — this app keys<br>"
            "the radio accordingly.  Set VARA's own PTT setting to <b>None</b>.<br><br>"
            "<b>Only one program can hold the serial port at a time.</b><br>"
            "If <i>Vara Terminal</i> or another PTT-driving client is running,<br>"
            "close it before testing here or before connecting.<br><br>"
            "<b>CP2105 dual-port devices</b> (some Digirig models, etc.):<br>"
            "PTT is typically on the <i>Standard</i> port, not the <i>Enhanced</i> port.<br><br>"
            "Test PTT keys the radio for 1 second without connecting to a station.</i>")
        note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        note.setWordWrap(True)
        outer.addWidget(note)
        outer.addStretch()

        self._ptt_mode_changed(self.ptt_mode.currentIndex())
        return w

    def _ptt_refresh_ports(self, select_port: str = ""):
        from ptt import list_serial_ports
        self.ptt_port.blockSignals(True)
        self.ptt_port.clear()
        ports = list_serial_ports()
        if not ports:
            self.ptt_port.addItem("(no ports found)")
        else:
            self.ptt_port.addItems(ports)
            if select_port and select_port in ports:
                self.ptt_port.setCurrentText(select_port)
        self.ptt_port.blockSignals(False)

    def _ptt_mode_changed(self, index: int):
        enabled = (index == 1)
        self.ptt_port.setEnabled(enabled)
        self.ptt_signal.setEnabled(enabled)
        self.btn_ptt_test.setEnabled(enabled)

    def _ptt_test(self):
        """Key the radio for 1 second using current settings — no station needed."""
        import threading
        from ptt import PTTController
        sig_map = {0: "rts", 1: "dtr", 2: "rts+dtr"}
        port   = self.ptt_port.currentText()
        signal = sig_map[self.ptt_signal.currentIndex()]
        if not port or port.startswith("("):
            QMessageBox.warning(self, "No Port", "Select a serial port first.")
            return
        self.btn_ptt_test.setEnabled(False)
        self.btn_ptt_test.setText("🔴  Keying…")
        def _do_test():
            try:
                ptt = PTTController(port=port, mode=signal)
                ptt.open()
                if not ptt.is_open:
                    QMessageBox.warning(
                        self, "PTT Error",
                        f"Could not open {port}:\n\n"
                        f"{ptt.last_error or 'unknown error'}\n\n"
                        f"This port may be open in Vara Terminal or another "
                        f"PTT-driving app — only one program can hold the "
                        f"COM port. Close it and try again.")
                    return
                ptt.test(duration=1.0)
                ptt.close()
            except Exception as exc:
                QMessageBox.warning(self, "PTT Error", str(exc))
            finally:
                self.btn_ptt_test.setEnabled(True)
                self.btn_ptt_test.setText("🔴  Test PTT  (1 second key)")
        threading.Thread(target=_do_test, daemon=True).start()

    # ── Tab: App preferences ──────────────────────────────────────

    def _build_bulletins_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(14, 14, 14, 10)
        outer.setSpacing(8)

        b = self._cfg.get("bulletins", {})
        # Cache is read from the parent's *live* config (not the dialog's
        # snapshot) so an in-flight LC fetch's result shows up here.
        p = self.parent()
        live_auto = ((p.config.get("bulletins", {}).get(
                        "auto_category_update", {}) if p else {}) or {})
        home_bbs_raw = self._cfg.get("user", {}).get(
            "home_bbs", "").strip().upper()
        home_bbs = home_bbs_raw.split(".")[0].split("-")[0]
        self._bull_connected_at_open = self._is_connected_for_bulletins()

        # Master switch — gates both LC seeding/refresh AND the per-
        # category L> bulletin check on connect.
        self.chk_bull_auto = QCheckBox(
            "Check for new bulletins on connect")
        self.chk_bull_auto.setChecked(
            bool(b.get("check_on_connect", False)))
        outer.addWidget(self.chk_bull_auto)

        note = QLabel(
            "<i>When enabled, QtC fetches the list of available "
            "categories from your Home BBS on first connect and "
            "refreshes once every 10 days.<br>"
            "Tick the categories below to subscribe.</i>")
        note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        # Home BBS warning box (unchanged)
        warn = QLabel(
            "<b>Home BBS only</b><br>"
            "Bulletin download only runs when connected to your Home BBS "
            "(set in My Station). This prevents re-downloading the same "
            "bulletins when visiting other nodes, since BBS-to-BBS "
            "duplicate checking (BID) is not available in this version.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background: #FAEEDA; border-left: 3px solid #BA7517; "
            "color: #633806; font-size: 11px; padding: 8px 12px;")
        outer.addWidget(warn)

        # Header row for the categories list
        home_label = home_bbs or "Home BBS"
        outer.addWidget(QLabel(
            f"Available categories on {home_label} — "
            f"tick to subscribe:"))

        ts_row = QHBoxLayout()
        ts_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_bull_last_lc = QLabel(self._bull_format_last_lc(
            live_auto.get("last_lc_at")))
        self.lbl_bull_last_lc.setStyleSheet("font-size: 11px;")
        ts_row.addWidget(self.lbl_bull_last_lc)
        ts_row.addStretch()
        self.btn_bull_refresh = QPushButton("🔄  Refresh now")
        self.btn_bull_refresh.clicked.connect(
            self._bull_fetch_categories)
        self.btn_bull_refresh.setEnabled(self._bull_connected_at_open)
        if not self._bull_connected_at_open:
            self.btn_bull_refresh.setToolTip(
                "Connect to your Home BBS first to use this button "
                "and update the category list instantly.\n"
                "Telnet users should connect while in Terminal View.")
        ts_row.addWidget(self.btn_bull_refresh)
        outer.addLayout(ts_row)

        # The checkable category list IS the subscription UI.
        self.lst_bull_cats = QListWidget()
        self.lst_bull_cats.setFont(QFont("Courier New", 10))
        self.lst_bull_cats.setMinimumHeight(200)
        # Render: merge cached categories with existing subscriptions
        # (the latter so migration from the old free-text UI doesn't
        # lose the user's picks before LC has ever run).
        known = live_auto.get("known_categories") or []
        existing_subs = [s.upper().strip() for s in
                         (b.get("subscriptions") or []) if s.strip()]
        self._bull_populate_cat_list(known, existing_subs)
        outer.addWidget(self.lst_bull_cats)

        list_footnote = QLabel(
            "<i>Categories shown with '?' are subscribed but had no "
            "bulletins in the last bulletin category check (LC). "
            "Untick to remove the subscription.</i>")
        list_footnote.setStyleSheet(
            f"color: {self._note_color}; font-size: 11px;")
        list_footnote.setWordWrap(True)
        outer.addWidget(list_footnote)

        # ── Recover a lost bulletin group (unchanged) ─────────────
        self._bull_reset_box = QGroupBox("Recover a lost bulletin")
        reset_layout = QVBoxLayout(self._bull_reset_box)
        reset_layout.setContentsMargins(12, 8, 12, 10)
        reset_layout.setSpacing(6)

        reset_note = QLabel(
            "Skipped a bulletin in the selection dialog and wish you "
            "hadn't? QtC remembers what you skipped so it doesn't keep "
            "asking. Resetting clears those 'skipped' markers for your "
            "Home BBS so those bulletins become eligible again on your "
            "next check.<br><br>"
            "<i>This does NOT delete any downloaded bulletins, mail, "
            "or contacts.</i>")
        reset_note.setStyleSheet(
            f"color: {self._note_color}; font-size: 11px;")
        reset_note.setWordWrap(True)
        reset_layout.addWidget(reset_note)

        reset_btn_row = QHBoxLayout()
        reset_btn_row.setContentsMargins(0, 0, 0, 0)
        home_label_reset = home_bbs or "Home BBS"
        self.btn_bull_reset = QPushButton(
            f"🔄  Reset bulletin check state for {home_label_reset}…")
        self.btn_bull_reset.clicked.connect(self._bull_reset_clicked)
        reset_btn_row.addWidget(self.btn_bull_reset)
        reset_btn_row.addStretch()
        reset_layout.addLayout(reset_btn_row)
        outer.addWidget(self._bull_reset_box)

        # No-home-BBS gate — the Reset section is meaningless without it.
        if not home_bbs:
            self._bull_reset_box.setEnabled(False)
            no_home = QLabel(
                "<i>Set a Home BBS on the My Station tab to enable "
                "bulletin reset.</i>")
            no_home.setStyleSheet(
                f"color: {self._note_color}; font-size: 11px;")
            no_home.setWordWrap(True)
            outer.addWidget(no_home)

        first_connect_note = QLabel(
            "<i><b>First-time connects to your Home BBS:</b> the very "
            "first bulletin check is kept short on purpose — 2 newest "
            "per category on VARA HF or packet, 3 on VARA FM or Telnet. Sorry, "
            "RF just isn't 1 Gb/s fiber, and both the BBS and the "
            "frequency are shared with other ops.</i>")
        first_connect_note.setStyleSheet(
            f"color: {self._note_color}; font-size: 11px;")
        first_connect_note.setWordWrap(True)
        outer.addWidget(first_connect_note)

        # Live-wire to the worker so an in-flight LC refreshes the UI
        worker = self._bull_parent_worker()
        if worker is not None:
            worker.sig_categories_ready.connect(self._bull_on_cats_ready)
            self.finished.connect(
                lambda _: self._bull_disconnect_worker_sig())

        outer.addStretch()
        return w

    # ── Bulletins tab — helpers ───────────────────────────────────

    def _bull_parent_worker(self):
        p = self.parent()
        return getattr(p, "worker", None) if p else None

    def _is_connected_for_bulletins(self) -> bool:
        """True when the parent main window is connected AND logged in.

        Gates the "📡 Get categories from BBS…" button, which sends LC —
        so the gate has to mean logged in, not just "a session object
        exists". Until v0.15.0 this read a hidden Refresh button's enabled
        flag through getattr, which would have failed silently (button
        gone -> permanently "not connected") rather than loudly."""
        p = self.parent()
        return bool(p is not None and getattr(p, "_logged_in", False))

    def _bull_format_last_lc(self, ts) -> str:
        if not ts:
            return "Last checked: never"
        s = str(ts).replace("Z", "")
        if "T" in s:
            d, t = s.split("T", 1)
            return f"Last checked: {d} {t[:5]} UTC"
        return f"Last checked: {ts}"

    def _bull_populate_cat_list(self, known, subs):
        """Render the checkable category list.

        known = [[cat, count], ...] — cached LC output for the Home BBS
        subs  = uppercase category names currently subscribed
        Items present in `subs` but missing from `known` are rendered
        with count '?' so the user's pre-existing picks survive the
        migration from the old free-text UI until the next LC run.
        Checked state = name in `subs`."""
        self.lst_bull_cats.clear()
        subs_set = {s.upper() for s in subs if s}
        rows = []
        seen = set()
        for c in known:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                name, count = str(c[0]).upper(), c[1]
            else:
                name, count = str(c).upper(), 0
            rows.append((name, count))
            seen.add(name)
        for s in sorted(subs_set - seen):
            rows.append((s, None))   # None → render as "?"

        if not rows:
            empty = QListWidgetItem(
                "(empty — connect to your Home BBS with the master "
                "checkbox above enabled to populate)")
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)  # non-selectable
            self.lst_bull_cats.addItem(empty)
            return

        for name, count in sorted(rows):
            count_str = "?" if count is None else str(count)
            item = QListWidgetItem(f"  {name:<12}  {count_str}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in subs_set
                else Qt.CheckState.Unchecked)
            self.lst_bull_cats.addItem(item)

    def _bull_collect_checked(self) -> list:
        """Return uppercase category names currently checked in the
        listbox. Empty list if the list is the (empty) placeholder row."""
        result = []
        for i in range(self.lst_bull_cats.count()):
            item = self.lst_bull_cats.item(i)
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if item.checkState() != Qt.CheckState.Checked:
                continue
            txt = item.text().strip()
            if not txt:
                continue
            cat = txt.split()[0].upper()
            if cat:
                result.append(cat)
        return result

    def _bull_fetch_categories(self):
        """Triggered by the small Refresh now button. Power-user
        override — runs LC immediately. Auto-update already handles
        the 10-day cadence; this is the 'I want it updated NOW' path."""
        worker = self._bull_parent_worker()
        if worker is None or getattr(worker, "session", None) is None:
            QMessageBox.warning(
                self, "Not Connected",
                "Connect to your Home BBS first, then try again.")
            return
        self.btn_bull_refresh.setEnabled(False)
        self.btn_bull_refresh.setText("Fetching…")
        worker.do_list_categories(open_dialog=False)

    def _bull_on_cats_ready(self, categories, _open_dialog):
        """Worker has fetched LC and persisted the cache. Re-render the
        checkbox list preserving the user's current selections so an
        in-flight LC doesn't lose what they've ticked."""
        p = self.parent()
        live_auto = ((p.config.get("bulletins", {}).get(
                        "auto_category_update", {}) if p else {}) or {})
        new_known = [[c, n] for c, n in categories]
        currently_checked = self._bull_collect_checked()
        self._bull_populate_cat_list(new_known, currently_checked)
        self.lbl_bull_last_lc.setText(
            self._bull_format_last_lc(live_auto.get("last_lc_at")))
        self.btn_bull_refresh.setText("🔄  Refresh now")
        self.btn_bull_refresh.setEnabled(self._bull_connected_at_open)

    def _bull_disconnect_worker_sig(self):
        worker = self._bull_parent_worker()
        if worker is None:
            return
        try:
            worker.sig_categories_ready.disconnect(self._bull_on_cats_ready)
        except (TypeError, RuntimeError):
            pass

    def _bull_reset_clicked(self):
        home_bbs_raw = self._cfg.get("user", {}).get(
            "home_bbs", "").strip().upper()
        home_bbs = home_bbs_raw.split(".")[0].split("-")[0]
        if not home_bbs:
            return
        msg = (
            f"Reset bulletin check state for {home_bbs}?\n\n"
            f"This will:\n"
            f"  • Forget that you've checked bulletins on {home_bbs} "
            f"before (so the next check will re-tombstone all but the "
            f"2 newest per category, capping any backlog)\n"
            f"  • Clear all 'previously skipped' markers for {home_bbs} "
            f"(so bulletins you skipped before become eligible again)\n\n"
            f"This does NOT delete any downloaded bulletins from your "
            f"folders. Your personal mail, contacts, and watermarks "
            f"are untouched.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Reset bulletin check state")
        box.setText(msg)
        btn_reset = box.addButton(
            "Reset", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not btn_reset:
            return

        p = self.parent()
        if p is None:
            return
        flag_key = f"bulletins_seen@{home_bbs}"
        had_flag = (p.config.setdefault("visited_bbs", {})
                    .pop(flag_key, None) is not None)
        try:
            n_tombs = p.db.clear_bulletin_tombstones_for_bbs(home_bbs)
        except Exception as e:
            QMessageBox.critical(
                self, "Reset failed", f"Could not clear tombstones:\n{e}")
            return
        save_config(p.config)
        # Mirror into our snapshot so OK won't re-add the flag
        self._cfg.setdefault("visited_bbs", {}).pop(flag_key, None)

        QMessageBox.information(
            self, "Reset complete",
            f"Bulletin check state for {home_bbs} has been reset.\n\n"
            f"• First-connect flag "
            f"{'cleared' if had_flag else 'was not set'}.\n"
            f"• {n_tombs} 'previously skipped' marker"
            f"{'s' if n_tombs != 1 else ''} cleared.\n\n"
            f"Your next bulletin check on {home_bbs} will re-cap any "
            f"backlog and let previously-skipped bulletins through.")

    # ── Tab: Mail-Call !!! ─────────────────────────────────────────
    #
    # Scheduled auto-connect to the user's Home BBS at preset clock times.
    # Design contract (see CLAUDE.md → MAIL-CALL):
    #   - 2-hour MINIMUM gap between any two scheduled fires (sysop politeness)
    #   - Run-mode = app-open-only (no tray, no autostart in v0.13.0)
    #   - Per-fire = same path as the Connect button (full mail+bulletin run)
    #   - Collision (manual session active) = skip silently + log
    #   - Missed slot on launch = skip to next slot, NEVER catch up
    #     (users routinely open QtC to compose outbox before connecting —
    #      auto-firing on launch would transmit before they're ready)

    PRESET_TIMES = {
        "once":   [QTime(8, 0)],
        "twice":  [QTime(8, 0), QTime(20, 0)],
        "thrice": [QTime(6, 0), QTime(14, 0), QTime(22, 0)],
    }

    # Bump this when the RF responsibility text below materially changes —
    # users who accepted an older version will be re-prompted on next enable.
    # Telnet connections do not require acceptance (no RF safety concerns).
    # Bump ONLY for a change an operator who already accepted must see
    # again — _validate() stops firing RF Mail-Call until the current
    # version is accepted, so a bump silently disables scheduled sessions
    # until the user reopens Settings. The §97.221 line added in v0.15.0
    # did not bump it for exactly that reason.
    MAILCALL_RESPONSIBILITY_VERSION = 1

    MAILCALL_RESPONSIBILITY_HTML_RF = (
        "<h3>⚠️&nbsp; Unattended Automatic Operation — Responsibility (RF)</h3>"
        "<p>By enabling <b>Mail-Call !!!</b>, you are allowing QtC to key "
        "your radio and transmit data on a schedule — with no one watching.</p>"
        "<p>You remain the control operator. You are responsible for:</p>"
        "<ul>"
        "<li>Your station being in safe operating condition before you "
        "walk away</li>"
        "<li>Your antenna being tuned, or auto-tune being enabled and "
        "reliable</li>"
        "<li>Your RF output power being set sensibly, <b>not</b> at "
        "maximum</li>"
        "<li>Your Mail-Call frequency meeting <b>§97.221</b> — unattended "
        "automatic operation is limited to certain segments. Please verify "
        "before you enable it. QtC cannot see your dial frequency and does "
        "not check</li>"
        "<li>Watching for failures the software <b>cannot</b> detect — "
        "stuck PTT, bumped VFO knob, antenna falls, coax failure, radio "
        "fault, software hang with PTT still asserted</li>"
        "</ul>"
        "<p>If any of these fail, QtC will not know, and the radio may "
        "key anyway.</p>"
        "<p>QtC is still <b>Beta</b>. Do not walk away from a radio on a "
        "timer until you have built trust through supervised cycles first.</p>"
    )

    def _build_mailcall_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(18, 18, 18, 12)
        outer.setSpacing(10)

        mc = self._cfg.get("mail_call", {})
        u  = self._cfg.get("user", {})

        # Enable checkbox at the very top — gates everything below.
        # setChecked here does NOT fire the responsibility dialog because
        # toggled.connect happens after this initial set.
        self.chk_mc_enabled = QCheckBox("Enable scheduled Home BBS connections")
        self.chk_mc_enabled.setChecked(bool(mc.get("enabled", False)))
        self.chk_mc_enabled.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.chk_mc_enabled)

        # Home BBS read-only display + time zone row
        info_form = QFormLayout()
        info_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        info_form.setHorizontalSpacing(10)
        info_form.setVerticalSpacing(8)
        info_form.setContentsMargins(0, 4, 0, 4)

        home_bbs = u.get("home_bbs", "").strip()
        home_bbs_display = home_bbs or "(not set — configure in My Station)"
        self.lbl_mc_home = QLabel(home_bbs_display)
        self.lbl_mc_home.setStyleSheet("font-family: 'Courier New'; font-weight: bold;")
        info_form.addRow("Home BBS:", self.lbl_mc_home)

        # Connection picker — every bbs_list entry whose callsign matches
        # the Home BBS. Stored as a composite key so reorders/edits to
        # bbs_list don't lose the user's choice.
        self.combo_mc_connection = QComboBox()
        self.combo_mc_connection.setMinimumWidth(280)
        matching = self._mc_matching_entries()
        for entry in matching:
            self.combo_mc_connection.addItem(
                self._mc_entry_label(entry), userData=entry)
        # Restore saved selection by composite key, else default to first
        saved_key = mc.get("bbs_key") or {}
        for i, entry in enumerate(matching):
            if self._mc_entry_key(entry) == saved_key:
                self.combo_mc_connection.setCurrentIndex(i)
                break
        # If no matches, dropdown stays empty and gets disabled
        # alongside the rest of the tab below.
        info_form.addRow("Connection:", self.combo_mc_connection)

        self.rb_mc_tz_local = QRadioButton("Local")
        self.rb_mc_tz_utc   = QRadioButton("UTC")
        tz_group = QButtonGroup(w)
        tz_group.addButton(self.rb_mc_tz_local)
        tz_group.addButton(self.rb_mc_tz_utc)
        if mc.get("time_zone", "local") == "utc":
            self.rb_mc_tz_utc.setChecked(True)
        else:
            self.rb_mc_tz_local.setChecked(True)
        tz_row = QHBoxLayout()
        tz_row.addWidget(self.rb_mc_tz_local)
        tz_row.addWidget(self.rb_mc_tz_utc)
        tz_row.addStretch()
        tz_wrap = QWidget()
        tz_wrap.setLayout(tz_row)
        info_form.addRow("Time zone:", tz_wrap)

        outer.addLayout(info_form)

        # Schedule group box — 5 radio modes
        sched_box = QGroupBox("Schedule")
        sched_layout = QVBoxLayout(sched_box)
        sched_layout.setContentsMargins(12, 8, 12, 8)
        sched_layout.setSpacing(6)

        self.rb_mc_once   = QRadioButton("Once daily       (08:00)")
        self.rb_mc_twice  = QRadioButton("Twice daily      (08:00, 20:00)")
        self.rb_mc_thrice = QRadioButton("Three times/day  (06:00, 14:00, 22:00)")

        # "Every N hours: [ N ]"
        self.rb_mc_every  = QRadioButton("Every")
        self.spin_mc_every = QSpinBox()
        self.spin_mc_every.setRange(2, 12)     # 2hr minimum enforced here
        self.spin_mc_every.setValue(int(mc.get("every_n_hours", 4)))
        self.spin_mc_every.setSuffix(" hours")
        self.spin_mc_every.setFixedWidth(110)
        every_row = QHBoxLayout()
        every_row.setContentsMargins(0, 0, 0, 0)
        every_row.addWidget(self.rb_mc_every)
        every_row.addWidget(self.spin_mc_every)
        every_row.addStretch()
        every_wrap = QWidget()
        every_wrap.setLayout(every_row)

        # "Custom times" + inline list
        self.rb_mc_custom = QRadioButton("Custom times:")

        self.lst_mc_custom = QListWidget()
        self.lst_mc_custom.setMaximumHeight(110)
        self.lst_mc_custom.setStyleSheet("font-family: 'Courier New';")
        for t_str in mc.get("custom_times", []):
            self.lst_mc_custom.addItem(t_str)

        # Time picker + Add/Remove buttons under the list
        self.te_mc_custom = QTimeEdit(QTime(12, 0))
        self.te_mc_custom.setDisplayFormat("HH:mm")
        self.te_mc_custom.setFixedWidth(90)
        self.btn_mc_add_time = QPushButton("➕ Add Time")
        self.btn_mc_rm_time  = QPushButton("➖ Remove")
        self.btn_mc_add_time.clicked.connect(self._mc_add_custom_time)
        self.btn_mc_rm_time.clicked.connect(self._mc_remove_custom_time)

        custom_btn_row = QHBoxLayout()
        custom_btn_row.setContentsMargins(20, 0, 0, 0)   # indent to align under list
        custom_btn_row.addWidget(self.te_mc_custom)
        custom_btn_row.addWidget(self.btn_mc_add_time)
        custom_btn_row.addWidget(self.btn_mc_rm_time)
        custom_btn_row.addStretch()

        # Indent the custom list to sit under the radio button
        list_wrap = QHBoxLayout()
        list_wrap.setContentsMargins(20, 0, 0, 0)
        list_wrap.addWidget(self.lst_mc_custom)

        # Schedule radio group
        sched_group = QButtonGroup(w)
        for rb in (self.rb_mc_once, self.rb_mc_twice, self.rb_mc_thrice,
                   self.rb_mc_every, self.rb_mc_custom):
            sched_group.addButton(rb)
        mode = mc.get("schedule_mode", "twice")
        {"once":   self.rb_mc_once,
         "twice":  self.rb_mc_twice,
         "thrice": self.rb_mc_thrice,
         "every":  self.rb_mc_every,
         "custom": self.rb_mc_custom}.get(mode, self.rb_mc_twice).setChecked(True)

        sched_layout.addWidget(self.rb_mc_once)
        sched_layout.addWidget(self.rb_mc_twice)
        sched_layout.addWidget(self.rb_mc_thrice)
        sched_layout.addWidget(every_wrap)
        sched_layout.addWidget(self.rb_mc_custom)
        sched_layout.addLayout(list_wrap)
        sched_layout.addLayout(custom_btn_row)

        outer.addWidget(sched_box)

        # Warning text — Variant 2 (peer-friendly)
        warn = QLabel(
            "<b>⚠️  Be kind to your sysop and the frequency</b><br>"
            "Schedule no more often than you actually need. Twice a day "
            "(e.g. 08:00 and 20:00) is plenty for most operators. The "
            "2-hour minimum is a guardrail, not a recommendation. Manual "
            "<b>Connect</b> is always available if something can't wait.")
        warn.setWordWrap(True)
        if self._dark:
            warn.setStyleSheet(
                "QLabel { background:#3a2a14; color:#ffd9a8; "
                "border:1px solid #6a4a24; border-radius:4px; padding:8px; }")
        else:
            warn.setStyleSheet(
                "QLabel { background:#fff4d6; color:#5a3a08; "
                "border:1px solid #d4a84a; border-radius:4px; padding:8px; }")
        outer.addWidget(warn)

        # The live "Next Mail-Call" countdown is shown in the main window
        # status bar (bottom right). After saving Settings it reflects the
        # new schedule. We don't duplicate it here because the values in
        # this tab can be edited but not yet saved.
        self.lbl_mc_next = QLabel(
            "Next scheduled run is shown in the main window status bar.")
        self.lbl_mc_next.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        outer.addWidget(self.lbl_mc_next)

        outer.addStretch()

        # Bottom: compact responsibility reminder (visible only when enabled)
        # + always-visible "Review Responsibility…" link.
        self.lbl_mc_reminder = QLabel(
            "<b>⚠️  Mail-Call is armed</b> — you remain the control "
            "operator. Stuck PTT, bumped VFO, antenna/coax failures, and "
            "software hangs are <b>not</b> detected by software.")
        self.lbl_mc_reminder.setWordWrap(True)
        if self._dark:
            self.lbl_mc_reminder.setStyleSheet(
                "QLabel { background:#3a1414; color:#ffc8c8; "
                "border:1px solid #6a2424; border-radius:4px; padding:6px; }")
        else:
            self.lbl_mc_reminder.setStyleSheet(
                "QLabel { background:#fde4e4; color:#7a1010; "
                "border:1px solid #c84a4a; border-radius:4px; padding:6px; }")
        outer.addWidget(self.lbl_mc_reminder)

        review_row = QHBoxLayout()
        review_row.addStretch()
        self.btn_mc_review = QPushButton("📖  Review Responsibility…")
        self.btn_mc_review.setFlat(True)
        if self._dark:
            self.btn_mc_review.setStyleSheet(
                "QPushButton { color:#88aaff; text-decoration:underline; }"
                "QPushButton:hover { color:#aaccff; }")
        else:
            self.btn_mc_review.setStyleSheet(
                "QPushButton { color:#1a4488; text-decoration:underline; }"
                "QPushButton:hover { color:#0a2266; }")
        self.btn_mc_review.clicked.connect(self._mc_review_clicked)
        review_row.addWidget(self.btn_mc_review)
        outer.addLayout(review_row)

        # Wire enable-state and mode-change to gate child widgets.
        # The enable checkbox routes through _mc_on_enable_toggled which
        # fires the responsibility-acceptance dialog on first ON-transition.
        self.chk_mc_enabled.toggled.connect(self._mc_on_enable_toggled)
        for rb in (self.rb_mc_once, self.rb_mc_twice, self.rb_mc_thrice,
                   self.rb_mc_every, self.rb_mc_custom):
            rb.toggled.connect(self._mc_update_enabled_state)
        # Switching the Connection between RF and Telnet changes whether
        # the RF-specific reminder banner + Review link apply.
        self.combo_mc_connection.currentIndexChanged.connect(
            self._mc_update_reminder)
        self._mc_update_enabled_state()
        self._mc_update_reminder()

        # Disable the whole tab content if there's no usable connection to
        # the Home BBS — either Home BBS isn't set, or no bbs_list entry
        # matches it.
        if not home_bbs:
            self._mc_disable_tab(outer, sched_box,
                "<i>Set a Home BBS in the My Station tab to enable Mail-Call !!!.</i>")
        elif not matching:
            home_base = self._base_callsign(home_bbs)
            self._mc_disable_tab(outer, sched_box,
                f"<i>No BBS List entry matches Home BBS <b>{home_bbs}</b>. "
                f"Add a BBS List entry with callsign <b>{home_base}</b> "
                f"(or <b>{home_base}-N</b>) to enable Mail-Call !!!.</i>")

        return w

    def _mc_disable_tab(self, outer_layout, sched_box, hint_html: str):
        """Grey out the Mail-Call tab content and show a hint at the top."""
        for widget in (self.chk_mc_enabled, sched_box, self.lbl_mc_home,
                       self.combo_mc_connection):
            widget.setEnabled(False)
        hint = QLabel(hint_html)
        hint.setStyleSheet("color:#ff8844; font-size:11px;")
        hint.setWordWrap(True)
        outer_layout.insertWidget(1, hint)

    def _mc_update_enabled_state(self):
        """Gate child widgets on the enable checkbox + selected schedule mode."""
        on = self.chk_mc_enabled.isChecked()
        # Time-zone radios + schedule radios live or die with the master switch
        for w in (self.rb_mc_tz_local, self.rb_mc_tz_utc,
                  self.rb_mc_once, self.rb_mc_twice, self.rb_mc_thrice,
                  self.rb_mc_every, self.rb_mc_custom):
            w.setEnabled(on)

        # Per-mode child widgets are also gated on which mode is selected
        self.spin_mc_every.setEnabled(on and self.rb_mc_every.isChecked())
        custom_active = on and self.rb_mc_custom.isChecked()
        self.lst_mc_custom.setEnabled(custom_active)
        self.te_mc_custom.setEnabled(custom_active)
        self.btn_mc_add_time.setEnabled(custom_active)
        self.btn_mc_rm_time.setEnabled(custom_active)

    def _mc_add_custom_time(self):
        t = self.te_mc_custom.time()
        t_str = t.toString("HH:mm")
        # Reject duplicates
        existing = [self.lst_mc_custom.item(i).text()
                    for i in range(self.lst_mc_custom.count())]
        if t_str in existing:
            return
        existing.append(t_str)
        existing.sort()
        self.lst_mc_custom.clear()
        for s in existing:
            self.lst_mc_custom.addItem(s)

    def _mc_remove_custom_time(self):
        row = self.lst_mc_custom.currentRow()
        if row >= 0:
            self.lst_mc_custom.takeItem(row)

    # ── Connection-picker helpers ──────────────────────────────────

    @staticmethod
    def _base_callsign(addr: str) -> str:
        """
        Extract the base callsign from any of the formats QtC stores.

        BBS hierarchical addressing (HA) lets other BBSes route mail to
        you via your home BBS. Examples of what this normalizes:

          'N0CALL.#REGION.ST.USA.NOAM'  →  'N0CALL'   (My Station home_bbs,
                                                     contact home_bbs)
          'N0CALL-1'                  →  'N0CALL'   (bbs_list.callsign)
          'N0CALL'                    →  'N0CALL'   (bare call)

        So Mail-Call can find every bbs_list entry that belongs to the
        user's home BBS station, regardless of which form was typed.
        """
        s = (addr or "").strip().upper()
        s = s.split('.', 1)[0]   # drop H-routing tail
        s = s.split('-', 1)[0]   # drop SSID suffix
        return s

    def _mc_matching_entries(self) -> list:
        """Return all bbs_list entries whose base callsign matches the
        base callsign of the user's home_bbs (HA address)."""
        home_base = self._base_callsign(
            self._cfg.get("user", {}).get("home_bbs", ""))
        if not home_base:
            return []
        return [e for e in self._cfg.get("bbs_list", [])
                if self._base_callsign(e.get("callsign", "")) == home_base]

    def _mc_has_visited_home_bbs(self) -> bool:
        """Return True if the user has connected at least once to any
        bbs_list entry whose base callsign matches their Home BBS.

        Mail-Call relies on per-BBS state that's only stamped on a
        completed manual connect (visited_bbs, bbs_watermarks, etc.).
        Without that first connect the unattended scheduler can't tell
        new mail from a backlog or pick the right bulletin baseline."""
        mycall = (self._cfg.get("user", {}).get("callsign", "") or
                  "").upper().strip()
        if not mycall:
            return False
        visited = self._cfg.get("visited_bbs", {}) or {}
        for entry in self._mc_matching_entries():
            bbs_call = entry.get("callsign", "")
            if f"{mycall}@{bbs_call}" in visited:
                return True
        return False

    @staticmethod
    def _mc_entry_label(entry: dict) -> str:
        """Human-readable label for the Connection dropdown."""
        transport_map = {
            "vara_hf": "VARA HF",
            "vara_fm": "VARA FM",
            "telnet":  "Telnet",
            **AGW_MODEMS,
        }
        tname = transport_map.get(entry.get("transport", ""),
                                   entry.get("transport", "?"))
        host = entry.get("host", "")
        if entry.get("transport") == "telnet":
            port = entry.get("telnet_port", "")
        elif entry.get("transport") in AGW_MODEMS:
            port = entry.get("agw_port", AGW_DEFAULT_PORT)
        else:
            port = entry.get("vara_cmd_port", "")
        location = f"{host}:{port}" if host and port else (host or "?")
        return f"{entry.get('callsign','?')} — {tname} ({location})"

    @staticmethod
    def _mc_entry_key(entry: dict) -> dict:
        """Composite key that survives bbs_list reorders/edits."""
        return {
            "callsign":  entry.get("callsign", "").upper(),
            "transport": entry.get("transport", ""),
            "host":      entry.get("host", ""),
        }

    def _mc_selected_entry(self):
        """The bbs_list entry the user has currently selected, or None."""
        if not hasattr(self, "combo_mc_connection"):
            return None
        return self.combo_mc_connection.currentData()

    @staticmethod
    def _mc_transport_class(entry: dict) -> str:
        """Return 'rf' or 'telnet' for the chosen entry — used to pick which
        responsibility text to show and which acceptance flag to check."""
        return "telnet" if entry.get("transport") == "telnet" else "rf"

    # ── Responsibility acceptance ──────────────────────────────────
    #
    # Acceptance is required only for RF transports (VARA HF/FM) because
    # those key a radio unattended. Telnet connections need no acceptance —
    # no PTT, no antenna, no RF safety surface.

    def _mc_already_accepted_rf(self) -> bool:
        """Has the user accepted the current RF responsibility text?"""
        mc = self._cfg.get("mail_call", {})
        return int(mc.get("responsibility_accepted_rf_version", 0)) == \
            self.MAILCALL_RESPONSIBILITY_VERSION

    def _mc_on_enable_toggled(self, checked: bool):
        """
        Handle the Enable Mail-Call !!! checkbox toggle.

        On ON-transition over an RF transport, show the RF responsibility
        dialog if not already accepted. Telnet selections skip the dialog
        entirely. If the user cancels the RF dialog, snap the checkbox
        back off.
        """
        if checked:
            entry = self._mc_selected_entry()
            if entry is None:
                # No selectable connection — revert. The tab's grey-out
                # logic should normally prevent reaching this branch.
                self.chk_mc_enabled.blockSignals(True)
                self.chk_mc_enabled.setChecked(False)
                self.chk_mc_enabled.blockSignals(False)
                self._mc_update_enabled_state()
                self._mc_update_reminder()
                return
            # Refuse to enable until the user has completed at least one
            # manual connect to their Home BBS. That first connect stamps
            # visited_bbs and watermarks — without those, Mail-Call would
            # treat the entire mailbox as "new" on its first unattended
            # fire and could ingest a huge backlog over slow RF.
            if not self._mc_has_visited_home_bbs():
                home_base = self._base_callsign(
                    self._cfg.get("user", {}).get("home_bbs", "")) or "(none)"
                QMessageBox.information(
                    self, "Mail-Call !!! — Connect to Home BBS first",
                    f"<b>Mail-Call can't be enabled yet.</b><br><br>"
                    f"You need to connect to your Home BBS "
                    f"<b>{home_base}</b> at least once from the toolbar "
                    f"Connect button before scheduling unattended "
                    f"sessions.<br><br>"
                    f"That first connect lets QtC learn your mailbox "
                    f"watermark and the current bulletin baseline so "
                    f"Mail-Call doesn't tie up the frequency downloading "
                    f"an entire backlog on its first unattended run.<br><br>"
                    f"Once you've connected to <b>{home_base}</b> once "
                    f"and confirmed everything looks right, come back "
                    f"here and enable Mail-Call !!!.")
                self.chk_mc_enabled.blockSignals(True)
                self.chk_mc_enabled.setChecked(False)
                self.chk_mc_enabled.blockSignals(False)
                self._mc_update_enabled_state()
                self._mc_update_reminder()
                return
            if self._mc_transport_class(entry) == "rf" \
                    and not self._mc_already_accepted_rf():
                accepted = self._show_responsibility_dialog(accept_mode=True)
                if accepted:
                    self._cfg.setdefault("mail_call", {})
                    self._cfg["mail_call"][
                        "responsibility_accepted_rf_version"] = \
                        self.MAILCALL_RESPONSIBILITY_VERSION
                else:
                    self.chk_mc_enabled.blockSignals(True)
                    self.chk_mc_enabled.setChecked(False)
                    self.chk_mc_enabled.blockSignals(False)
        self._mc_update_enabled_state()
        self._mc_update_reminder()

    def _mc_review_clicked(self):
        """Review-only display of the RF responsibility text."""
        self._show_responsibility_dialog(accept_mode=False)

    def _mc_update_reminder(self):
        """Show the in-tab RF reminder banner and Review link only when
        Mail-Call is enabled AND the selected connection is RF. Telnet
        has no PTT/antenna/coax surface, so the RF-specific reminder is
        hidden for it."""
        entry = self._mc_selected_entry()
        is_rf = entry is not None and self._mc_transport_class(entry) == "rf"
        show = self.chk_mc_enabled.isChecked() and is_rf
        self.lbl_mc_reminder.setVisible(show)
        if hasattr(self, "btn_mc_review"):
            self.btn_mc_review.setVisible(is_rf)

    def _show_responsibility_dialog(self, accept_mode: bool = True) -> bool:
        """
        Display the RF responsibility text.

        accept_mode=True   — checkbox-gated Accept button. Returns True iff
                             the user ticked the box and clicked Accept.
        accept_mode=False  — read-only review. Returns True (no decision).
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("QtC — Mail-Call !!! Responsibility (RF)")
        dlg.setMinimumSize(560, 480)

        v = QVBoxLayout(dlg)
        v.setContentsMargins(18, 18, 18, 14)
        v.setSpacing(12)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setHtml(self.MAILCALL_RESPONSIBILITY_HTML_RF)
        if self._dark:
            body.setStyleSheet(
                "QTextEdit { background:#1a0e0e; color:#ffe0c8; "
                "border:1px solid #6a2424; border-radius:4px; padding:8px; }")
        else:
            body.setStyleSheet(
                "QTextEdit { background:#fff8f0; color:#1a1010; "
                "border:1px solid #c84a4a; border-radius:4px; padding:8px; }")
        v.addWidget(body)

        chk = QCheckBox("I understand and accept these responsibilities.")
        chk.setStyleSheet("font-weight: bold;")
        v.addWidget(chk)

        btns = QDialogButtonBox()
        if accept_mode:
            accept_btn = btns.addButton(
                "Enable Mail-Call !!!", QDialogButtonBox.ButtonRole.AcceptRole)
            cancel_btn = btns.addButton(
                "Cancel", QDialogButtonBox.ButtonRole.RejectRole)
            accept_btn.setEnabled(False)
            chk.toggled.connect(accept_btn.setEnabled)
            accept_btn.clicked.connect(dlg.accept)
            cancel_btn.clicked.connect(dlg.reject)
        else:
            # Review mode — pre-tick the checkbox as a memory aid, OK only
            chk.setChecked(True)
            chk.setEnabled(False)
            ok_btn = btns.addButton(
                "Close", QDialogButtonBox.ButtonRole.AcceptRole)
            ok_btn.clicked.connect(dlg.accept)
        v.addWidget(btns)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _build_app_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(18, 18, 18, 18)
        form.setVerticalSpacing(12)

        a = self._cfg.get("app", {})

        # "Auto-download new personal mail on connect" lived here. It was
        # written to config and read back into itself and was never
        # consulted anywhere else — unticking it changed nothing, because
        # what actually decides is whether the session came from Mail view
        # or from Terminal (see _on_mail_summary). Removed rather than
        # renamed: a control that does nothing is worse than no control
        # (Bill, 2026-09-23). The config key is left alone so an older
        # config.json still loads.

        self.chk_dark_mode = QCheckBox("Dark mode  (takes effect on next launch)")
        self.chk_dark_mode.setChecked(bool(a.get("dark_mode", False)))

        # Font size spinner
        font_row = QHBoxLayout()
        self.spin_font = QSpinBox()
        self.spin_font.setRange(8, 20)
        self.spin_font.setValue(int(a.get("font_size", 10)))
        self.spin_font.setFixedWidth(60)
        self.spin_font.setSuffix(" pt")
        font_row.addWidget(self.spin_font)
        font_row.addStretch()

        # Live preview box
        self._font_preview = QTextEdit()
        self._font_preview.setReadOnly(True)
        self._font_preview.setFixedHeight(80)
        self._font_preview.setFont(QFont("Courier New", self.spin_font.value()))
        self._font_preview.setPlainText(
            "The quick brown fox jumped over the lazy dog.\n"
            "Pack my box with five dozen liquor jugs.\n"
            "de N0CALL>")
        self.spin_font.valueChanged.connect(
            lambda v: self._font_preview.setFont(QFont("Courier New", v)))

        self.e_data_dir = QLineEdit(a.get("data_dir", "data"))
        self.e_data_dir.setPlaceholderText("data")

        # "Max message size (KB)" was here. Nothing ever checked a message
        # against it — no send path, no download path, nowhere. Removed in
        # v0.15.0 with the rest of the dead controls. Worth building for
        # real one day: 50 KB at 300 baud is half an hour of airtime.

        # Page limit — the BBS "OP" command. Sets how many lines the BBS
        # sends before it stops and waits for us. Low is good RF manners:
        # it stops a month of daily bulletins arriving as one enormous
        # uninterruptible listing that ties up the frequency.
        self.spin_page_limit = QSpinBox()
        self.spin_page_limit.setRange(0, 255)
        self.spin_page_limit.setValue(int(a.get("page_limit", 20)))
        self.spin_page_limit.setFixedWidth(70)
        self.spin_page_limit.setSuffix(" lines")
        self.spin_page_limit.setToolTip(
            "Sent to the BBS as \"OP n\" on your first connect to each BBS,\n"
            f"then refreshed every 30 days. 0 turns BBS paging off.\n"
            f"LinBPQ refuses anything from 1 to "
            f"{BBSSession.PAGE_MIN - 1} — it wants "
            f"{BBSSession.PAGE_MIN} or more.")
        # 1–9 is a value LinBPQ answers with "Page Length n is too short",
        # and the spinner used to offer them (Bill, 2026-09-23: "OP 10 is
        # the shortest you can pick"). Snap up rather than refuse to type:
        # 0 still means no paging.
        self.spin_page_limit.valueChanged.connect(
            lambda v: self.spin_page_limit.setValue(BBSSession.PAGE_MIN)
            if 0 < v < BBSSession.PAGE_MIN else None)
        page_row = QHBoxLayout()
        page_row.addWidget(self.spin_page_limit)
        page_row.addStretch()

        # Character-set selector (issue #2). utf-8 default; cp437/cp850 let
        # DOS-art bulletins in the TECH area render their box-drawing graphics
        # instead of showing replacement glyphs. Applies to the next text the
        # BBS sends (and to a live session immediately on save).
        self.combo_codec = QComboBox()
        self.combo_codec.addItem("UTF-8 (default)", "utf-8")
        self.combo_codec.addItem("CP437 (DOS graphics)", "cp437")
        self.combo_codec.addItem("CP850 (DOS Latin-1)", "cp850")
        self.combo_codec.setToolTip(
            "Character set used to decode incoming BBS text.\n"
            "Switch to CP437/CP850 to read bulletins built with\n"
            "8-bit ASCII (DOS) box-drawing graphics, then view\n"
            "the bulletin in the Terminal view.")
        _codec_idx = self.combo_codec.findData(
            str(a.get("text_codec", "utf-8")).lower())
        self.combo_codec.setCurrentIndex(_codec_idx if _codec_idx >= 0 else 0)
        codec_row = QHBoxLayout()
        codec_row.addWidget(self.combo_codec)
        codec_row.addStretch()

        form.addRow("", self.chk_dark_mode)
        form.addRow("Message font size:", font_row)
        form.addRow("", self._font_preview)
        form.addRow("Character set:", codec_row)
        form.addRow("Data directory:", self.e_data_dir)
        form.addRow("Page limit  (OP):", page_row)

        page_note = QLabel(
            "<i>How many lines the BBS sends before it pauses. QtC sends "
            "<b>OP 20</b> on your first connect to each BBS and refreshes it "
            "every 30 days.<br>"
            "Keeping this low is good RF manners — it stops a month of daily "
            "bulletins from arriving as one long listing that ties up the "
            "frequency for everyone else. QtC reads past the pause "
            "automatically when it needs to.<br>"
            "<b>Default 20.</b> 0 turns BBS paging off entirely and the BBS "
            "sends everything in one go. Change only if you are sure of the "
            "result.</i>")
        page_note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        page_note.setWordWrap(True)
        form.addRow("", page_note)

        note = QLabel(
            "<i>Data directory stores the local SQLite database.<br>"
            "Changing it takes effect on next launch.</i>")
        note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        note.setWordWrap(True)
        form.addRow("", note)

        return w

    # ── Accept: write edited values back into _cfg ─────────────────

    def _on_accept(self):
        # User tab
        self._cfg.setdefault("user", {})
        self._cfg["user"]["callsign"]    = self.e_callsign.text().strip().upper()
        self._cfg["user"]["name"]        = self.e_name.text().strip()
        self._cfg["user"]["qth"]         = self.e_qth.text().strip()
        self._cfg["user"]["zip"]         = self.e_zip.text().strip()
        self._cfg["user"]["home_bbs"]    = self.e_home_bbs.text().strip().upper()
        self._cfg["user"]["telnet_user"] = self.e_telnet_user.text().strip()
        self._cfg["user"]["password"]    = self.e_password.text()

        if not self._cfg["user"]["callsign"]:
            QMessageBox.warning(self, "Missing Field",
                "Callsign cannot be blank.")
            return

        # App tab
        self._cfg.setdefault("app", {})
        self._cfg["app"]["dark_mode"]            = self.chk_dark_mode.isChecked()
        self._cfg["app"]["font_size"]            = self.spin_font.value()
        self._cfg["app"]["data_dir"]             = self.e_data_dir.text().strip() or "data"
        self._cfg["app"]["text_codec"]           = self.combo_codec.currentData()
        self._cfg["app"]["page_limit"]           = self.spin_page_limit.value()

        # Bulletins tab — single master switch gates LC seeding +
        # bulletin check; subscriptions are derived from the checkable
        # category list (the user's tick marks).
        self._cfg.setdefault("bulletins", {})
        self._cfg["bulletins"]["check_on_connect"] = (
            self.chk_bull_auto.isChecked())
        self._cfg["bulletins"]["subscriptions"] = (
            self._bull_collect_checked())

        # last_lc_at and known_categories are runtime state owned by
        # SessionWorker; pull the latest from the live parent config so
        # a Refresh-now done while this dialog was open isn't wiped by
        # our stale snapshot.
        p = self.parent()
        live_auto = ((p.config.get("bulletins", {}).get(
                        "auto_category_update", {}) if p else {}) or {})
        self._cfg["bulletins"]["auto_category_update"] = {
            "last_lc_at":       live_auto.get("last_lc_at"),
            "known_categories": live_auto.get("known_categories") or [],
        }

        # PTT tab
        sig_map = {0: "rts", 1: "dtr", 2: "rts+dtr"}
        self._cfg["ptt"] = {
            "mode":   "none" if self.ptt_mode.currentIndex() == 0 else "com",
            "port":   self.ptt_port.currentText() if self.ptt_mode.currentIndex() == 1 else "",
            "signal": sig_map[self.ptt_signal.currentIndex()],
        }

        # Mail-Call !!! tab — validate custom-times 2hr gap before accepting
        mc_mode = (
            "once"   if self.rb_mc_once.isChecked()   else
            "twice"  if self.rb_mc_twice.isChecked()  else
            "thrice" if self.rb_mc_thrice.isChecked() else
            "every"  if self.rb_mc_every.isChecked()  else
            "custom"
        )
        custom_times = [self.lst_mc_custom.item(i).text()
                        for i in range(self.lst_mc_custom.count())]
        custom_times.sort()
        if mc_mode == "custom":
            err = self._mc_validate_gap(custom_times)
            if err:
                QMessageBox.warning(self, "Mail-Call !!! schedule",
                    f"{err}\n\n"
                    "BBS sysops appreciate at least a 2-hour gap between "
                    "scheduled connections. Please adjust the times.")
                return

        # Refuse OK if Mail-Call is enabled but no connection is picked
        # (defensive — the tab is greyed out when there are no matches).
        selected_entry = self._mc_selected_entry()
        if self.chk_mc_enabled.isChecked() and selected_entry is None:
            QMessageBox.warning(self, "Mail-Call !!!",
                "Mail-Call is enabled but no connection is selected.\n\n"
                "Either add a BBS List entry matching your Home BBS "
                "callsign, or disable Mail-Call.")
            return

        # If the user switched to an RF connection without ever accepting
        # the RF responsibility text, refuse OK and tell them how to fix.
        # Telnet connections need no acceptance.
        if self.chk_mc_enabled.isChecked() and selected_entry is not None:
            if self._mc_transport_class(selected_entry) == "rf" \
                    and not self._mc_already_accepted_rf():
                QMessageBox.warning(self, "Mail-Call !!!",
                    "You have selected an RF (VARA or packet) connection but have not "
                    "accepted the RF responsibility text.\n\n"
                    "Toggle the Enable checkbox off, then back on, to see "
                    "the RF responsibility prompt.")
                return

        # Persist the chosen connection by composite key + RF acceptance
        # version (set in _mc_on_enable_toggled).
        existing_mc = self._cfg.get("mail_call", {})
        self._cfg["mail_call"] = {
            "enabled":       self.chk_mc_enabled.isChecked(),
            "time_zone":     "utc" if self.rb_mc_tz_utc.isChecked() else "local",
            "schedule_mode": mc_mode,
            "every_n_hours": self.spin_mc_every.value(),
            "custom_times":  custom_times,
            "bbs_key":       (self._mc_entry_key(selected_entry)
                              if selected_entry else None),
            "responsibility_accepted_rf_version": int(existing_mc.get(
                "responsibility_accepted_rf_version", 0)),
        }

        self.accept()

    @staticmethod
    def _mc_validate_gap(times: list) -> str:
        """
        Returns "" if all adjacent gaps (incl. wrap across midnight) are
        >= 2 hours, else a human-readable error string. `times` is a
        sorted list of "HH:MM" strings.
        """
        if len(times) < 2:
            return ""
        mins = []
        for s in times:
            h, m = s.split(":")
            mins.append(int(h) * 60 + int(m))
        for i in range(len(mins) - 1):
            if mins[i + 1] - mins[i] < 120:
                return (f"Times {times[i]} and {times[i + 1]} are less "
                        f"than 2 hours apart.")
        wrap = (mins[0] + 24 * 60) - mins[-1]
        if wrap < 120:
            return (f"Times {times[-1]} and {times[0]} are less than "
                    f"2 hours apart across midnight.")
        return ""

    def get_config(self) -> dict:
        return self._cfg


# ─────────────────────────────────────────────────────────────────────────────
# BBS Entry sub-dialog  (used by SettingsDialog BBS tab)
# ─────────────────────────────────────────────────────────────────────────────

class _BBSEntryDialog(QDialog):
    """Add or edit a single BBS entry."""

    TRANSPORTS = [
        ("VARA HF",      "vara_hf"),
        ("VARA FM",      "vara_fm"),
        ("Telnet",       "telnet"),
        ("Direwolf",     "direwolf"),
        ("(Qt)SoundModem", "soundmodem"),
    ]
    FUTURE = set()

    def __init__(self, entry: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit BBS Entry" if entry else "Add BBS Entry")
        self.setMinimumWidth(420)
        e = entry or {}
        # Derive note color from parent's dark mode setting if available
        dark = False
        if parent and hasattr(parent, 'config'):
            dark = bool(parent.config.get("app", {}).get("dark_mode", False))
        self._note_color = "#aaaaaa" if dark else "#666666"

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(10)
        form.setContentsMargins(12, 12, 12, 4)

        # ── Always-visible fields ──────────────────────
        self.e_name     = QLineEdit(e.get("name", ""))
        self.e_name.setPlaceholderText("e.g. Home Node")
        self.e_callsign = QLineEdit(e.get("callsign", ""))
        self.e_callsign.setPlaceholderText("e.g. N0CALL-1")
        self.e_notes    = QLineEdit(e.get("notes", ""))

        form.addRow("Name:",      self.e_name)
        form.addRow("Callsign:",  self.e_callsign)

        # ── Transport selector ─────────────────────────
        self.transport_combo = QComboBox()
        for label, _ in self.TRANSPORTS:
            self.transport_combo.addItem(label)
        # Set current transport
        current_transport = e.get("transport", "vara_hf")
        for i, (_, key) in enumerate(self.TRANSPORTS):
            if key == current_transport:
                self.transport_combo.setCurrentIndex(i)
                break
        self.transport_combo.currentIndexChanged.connect(self._on_transport_changed)
        form.addRow("Transport:", self.transport_combo)

        layout.addLayout(form)

        # ── VARA fields panel ──────────────────────────
        self.vara_group = QFrame()
        self.vara_group.setFrameShape(QFrame.Shape.StyledPanel)
        vara_form = QFormLayout(self.vara_group)
        vara_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        vara_form.setVerticalSpacing(8)
        vara_form.setContentsMargins(12, 10, 12, 10)

        self.e_freq = QLineEdit(e.get("freq", ""))
        self.e_freq.setPlaceholderText("e.g. 14.105.000")
        self.e_freq.setToolTip("Frequency in MHz (for future rig control)")

        self.e_bw = QComboBox()
        # Wide enough for "NARROW" + dropdown arrow without clipping.
        self.e_bw.setFixedWidth(120)
        # Items are populated by _on_transport_changed so the dropdown
        # matches HF (500/2300 kHz) vs FM (NARROW/WIDE).
        self._vara_initial_bw = e.get("bw", "")

        vara_form.addRow("Frequency (MHz):", self.e_freq)
        vara_form.addRow("Bandwidth:",       self.e_bw)
        layout.addWidget(self.vara_group)

        # ── Telnet fields panel ────────────────────────
        self.telnet_group = QFrame()
        self.telnet_group.setFrameShape(QFrame.Shape.StyledPanel)
        telnet_form = QFormLayout(self.telnet_group)
        telnet_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        telnet_form.setVerticalSpacing(8)
        telnet_form.setContentsMargins(12, 10, 12, 10)

        self.e_host = QLineEdit(e.get("host", ""))
        self.e_host.setPlaceholderText("IP address or hostname")
        self.e_port = QLineEdit(str(e.get("telnet_port", "8010")))
        self.e_port.setPlaceholderText("8010")
        self.e_port.setFixedWidth(70)

        telnet_form.addRow("Host:", self.e_host)
        telnet_form.addRow("Port:", self.e_port)
        layout.addWidget(self.telnet_group)

        # ── Packet (AGW modem) fields panel ────────────
        self.agw_group = QFrame()
        self.agw_group.setFrameShape(QFrame.Shape.StyledPanel)
        agw_form = QFormLayout(self.agw_group)
        agw_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        agw_form.setVerticalSpacing(8)
        agw_form.setContentsMargins(12, 10, 12, 10)

        is_agw_entry = e.get("transport") in AGW_MODEMS
        self.e_agw_host = QLineEdit(
            (e.get("host", "") if is_agw_entry else "") or "127.0.0.1")
        self.e_agw_host.setPlaceholderText("127.0.0.1")
        self.e_agw_host.setToolTip(
            "Computer running the modem — 127.0.0.1 when it's this one")
        self.e_agw_port = QLineEdit(str(e.get("agw_port", AGW_DEFAULT_PORT)))
        self.e_agw_port.setPlaceholderText(str(AGW_DEFAULT_PORT))
        self.e_agw_port.setFixedWidth(70)
        self.e_agw_port.setToolTip(
            f"The modem's AGW port (AGWPORT in direwolf.conf, or the AGW "
            f"port in QtSoundModem's Setup) — default {AGW_DEFAULT_PORT}")
        self.e_agw_radio = QSpinBox()
        self.e_agw_radio.setRange(0, 15)
        self.e_agw_radio.setValue(int(e.get("agw_radio_port", 0)))
        self.e_agw_radio.setFixedWidth(60)
        # Labelled in the modem's own words, not AGW's "radio port" — a new
        # Direwolf user will look for the word in direwolf.conf and find
        # CHANNEL there (Bill, 2026-09-29). Text is set per modem in
        # _on_transport_changed.
        self.l_agw_radio = QLabel()
        self.l_agw_radio_hint = QLabel()
        self.l_agw_radio_hint.setStyleSheet(
            f"color: {self._note_color}; font-size:11px;")
        agw_radio_row = QHBoxLayout()
        agw_radio_row.setSpacing(8)
        agw_radio_row.addWidget(self.e_agw_radio)
        agw_radio_row.addWidget(self.l_agw_radio_hint, 1)

        agw_form.addRow("Host:",       self.e_agw_host)
        agw_form.addRow("AGW Port:",   self.e_agw_port)
        agw_form.addRow(self.l_agw_radio, agw_radio_row)
        agw_note = QLabel(
            "Packet length, retries and PTT are set in the modem's "
            "own configuration.")
        agw_note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        agw_note.setWordWrap(True)
        agw_form.addRow("", agw_note)
        layout.addWidget(self.agw_group)

        # ── Future transport placeholder ───────────────
        self.future_group = QFrame()
        self.future_group.setFrameShape(QFrame.Shape.StyledPanel)
        future_layout = QVBoxLayout(self.future_group)
        future_layout.setContentsMargins(12, 10, 12, 10)
        self.future_label = QLabel()
        self.future_label.setStyleSheet(f"color: {self._note_color}; font-style:italic;")
        self.future_label.setWordWrap(True)
        future_layout.addWidget(self.future_label)
        layout.addWidget(self.future_group)

        # ── Notes (always visible) ─────────────────────
        notes_form = QFormLayout()
        notes_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        notes_form.setContentsMargins(12, 0, 12, 4)
        notes_form.addRow("Notes:", self.e_notes)
        layout.addLayout(notes_form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # Set initial panel visibility
        self._on_transport_changed(self.transport_combo.currentIndex())

    # VARA HF bandwidth values are kHz numbers ("BW500" / "BW2300" on the
    # wire). VARA FM uses bare keyword commands ("NARROW" / "WIDE") with
    # no BW prefix — verified against VARA FM modem behavior.
    _VARA_BW_OPTIONS = {
        "vara_hf": ["500", "2300"],
        "vara_fm": ["NARROW", "WIDE"],
    }
    _VARA_BW_DEFAULT = {"vara_hf": "500", "vara_fm": "NARROW"}

    # What each AGW modem calls the "radio port" field in its own config.
    _AGW_CHANNEL_WORDS = {
        "direwolf": (
            "Channel:",
            "CHANNEL in direwolf.conf — leave at 0 for one radio",
            "The CHANNEL number from direwolf.conf. 0 is the first radio; "
            "1 is the second (right audio channel with ACHANNELS 2)."),
        "soundmodem": (
            "Channel:",
            "0 = Modem A in QtSoundModem — leave at 0 for one radio",
            "AGW port number in QtSoundModem: 0 is Modem A (left audio), "
            "1 is Modem B (right audio), and so on."),
    }

    def _on_transport_changed(self, index: int):
        _, key = self.TRANSPORTS[index]
        is_vara    = key in ("vara_hf", "vara_fm")
        is_telnet  = key == "telnet"
        is_future  = key in self.FUTURE
        is_agw     = key in AGW_MODEMS and not is_future

        self.vara_group.setVisible(is_vara)
        self.telnet_group.setVisible(is_telnet)
        self.agw_group.setVisible(is_agw)
        self.future_group.setVisible(is_future)

        if is_agw:
            label, hint, tip = self._AGW_CHANNEL_WORDS.get(
                key, self._AGW_CHANNEL_WORDS["direwolf"])
            self.l_agw_radio.setText(label)
            self.l_agw_radio_hint.setText(hint)
            self.e_agw_radio.setToolTip(tip)

        if is_agw and not self.e_host.text().strip():
            # A modem nearly always runs on the same machine as QtC, and
            # the connect code already falls back to 127.0.0.1 for a blank
            # host — but the dialog never said so, leaving a new Direwolf
            # entry with an empty Host box and a placeholder reading only
            # "IP address or hostname" (found running B1, 2026-09-20).
            # Only filled when blank, so a host typed for Telnet and then
            # switched over is never clobbered.
            self.e_host.setText("127.0.0.1")

        if is_vara:
            # Repopulate the BW dropdown with the right options for this
            # VARA variant. Pick the saved value when it's valid for the
            # new variant, otherwise fall back to the variant default.
            opts = self._VARA_BW_OPTIONS[key]
            current = self.e_bw.currentText()
            saved = getattr(self, "_vara_initial_bw", "") or ""
            self.e_bw.blockSignals(True)
            self.e_bw.clear()
            self.e_bw.addItems(opts)
            picked = (saved if saved in opts else
                      current if current in opts else
                      self._VARA_BW_DEFAULT[key])
            self.e_bw.setCurrentText(picked)
            self.e_bw.blockSignals(False)
            # Saved value is only used to seed the first paint; clear it
            # so subsequent transport switches use the live combo value.
            self._vara_initial_bw = ""

        if is_future:
            label_map = {
                "soundmodem": "(Qt)SoundModem support is planned for a future "
                              "release — it will use the same AGW connection "
                              "as Direwolf.",
            }
            self.future_label.setText(label_map.get(key, "Coming soon."))

        self.adjustSize()

    def _on_accept(self):
        if not self.e_callsign.text().strip():
            QMessageBox.warning(self, "Missing Field", "Callsign cannot be blank.")
            return
        _, key = self.TRANSPORTS[self.transport_combo.currentIndex()]
        if key == "telnet" and not self.e_host.text().strip():
            QMessageBox.warning(self, "Missing Field", "Host cannot be blank for Telnet.")
            return
        if key in AGW_MODEMS and key not in self.FUTURE:
            p = self.e_agw_port.text().strip()
            if not (p.isdigit() and 0 < int(p) < 65536):
                QMessageBox.warning(self, "Invalid Port",
                    f"AGW Port must be a number from 1 to 65535 "
                    f"(default {AGW_DEFAULT_PORT}).")
                return
        self.accept()

    def get_entry(self) -> dict:
        _, key = self.TRANSPORTS[self.transport_combo.currentIndex()]
        port = self.e_port.text().strip()
        agw_port = self.e_agw_port.text().strip()
        is_agw = key in AGW_MODEMS
        return {
            "name":        self.e_name.text().strip(),
            "callsign":    self.e_callsign.text().strip().upper(),
            "transport":   key,
            "vara_type":   ("hf" if key == "vara_hf" else
                            "fm" if key == "vara_fm" else None),
            "freq":        self.e_freq.text().strip(),
            "bw":          "" if is_agw else self.e_bw.currentText(),
            # Telnet and packet both keep their address in "host"
            "host":        ((self.e_agw_host.text().strip() or "127.0.0.1")
                            if is_agw else self.e_host.text().strip()),
            "telnet_port": int(port) if port.isdigit() else 8010,
            "agw_port":    int(agw_port) if agw_port.isdigit() else AGW_DEFAULT_PORT,
            "agw_radio_port": self.e_agw_radio.value(),
            "notes":       self.e_notes.text().strip(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Mail-Call !!! Scheduler
# ─────────────────────────────────────────────────────────────────────────────

class MailCallScheduler(QObject):
    """
    Computes next-fire times from the user's Mail-Call config and triggers
    a Connect when a slot is crossed. Runs entirely on the Qt main thread
    via a QTimer — no background work, no signals across threads.

    Design contract (see CLAUDE.md / Mail-Call design memo):
      - Run-mode: app-open-only. Scheduler dies with the app.
      - Per-fire: same code path as the toolbar Connect button.
      - Collision (manual session active): skip silently + log [SCHED].
      - Missed slot on launch: NEVER catch up — compute "next slot > now"
        and wait. Users open QtC to compose outbox before connecting; an
        auto-connect on launch would transmit before they're ready.
      - Pre-fire validation: BBS entry must still exist in bbs_list, AND
        responsibility must be accepted for that entry's transport class.
    """

    sig_fire    = pyqtSignal(dict)   # the bbs_list entry to connect to
    sig_status  = pyqtSignal(str)    # status text for the main-window indicator
    sig_skipped = pyqtSignal(str)    # reason for a skipped fire (for [SCHED] log)

    TICK_MS = 15_000   # 15 s — fine for minute-precision slots

    def __init__(self, get_config, is_busy, parent=None):
        """
        get_config: callable returning the current config dict.
        is_busy:    callable returning True if a manual session is active.
        """
        super().__init__(parent)
        self._get_config = get_config
        self._is_busy    = is_busy
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._next_fire = None
        self._last_status = None
        # External code (MainWindow's busy-retry) sets this to mute scheduler
        # status updates so its own countdown isn't clobbered every tick.
        self._status_paused = False

    def set_status_paused(self, paused: bool):
        """When True, the scheduler stops emitting sig_status updates.

        Used by MainWindow during a Mail-Call busy-retry window so the
        retry countdown ("channel busy, retry 2/3 in 02:45") doesn't get
        overwritten on the next scheduler tick. Caller restores via
        set_status_paused(False) + refresh().
        """
        self._status_paused = paused

    def start(self):
        self._refresh()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def refresh(self):
        """Reload from config — call after Settings is OK'd."""
        self._next_fire = None
        self._refresh()

    # ── core ──────────────────────────────────────────────────────

    def _now(self):
        mc = self._get_config().get("mail_call", {}) or {}
        if mc.get("time_zone", "local") == "utc":
            return datetime.utcnow()
        return datetime.now()

    @staticmethod
    def _slots_for_day(day_date, mc):
        """Return sorted [datetime] slots for the given calendar day."""
        mode  = mc.get("schedule_mode", "twice")
        times = []
        if   mode == "once":   times = [(8, 0)]
        elif mode == "twice":  times = [(8, 0), (20, 0)]
        elif mode == "thrice": times = [(6, 0), (14, 0), (22, 0)]
        elif mode == "every":
            n = max(2, int(mc.get("every_n_hours", 4)))
            times = [(h, 0) for h in range(0, 24, n)]
        elif mode == "custom":
            for s in mc.get("custom_times", []) or []:
                try:
                    h, m = s.split(":")
                    times.append((int(h), int(m)))
                except (ValueError, AttributeError):
                    continue
        times.sort()
        return [datetime(day_date.year, day_date.month, day_date.day, h, m)
                for h, m in times]

    def _compute_next_fire(self, after):
        """First slot strictly after `after`, scanning today + tomorrow + +2d."""
        mc = self._get_config().get("mail_call", {}) or {}
        for offset in range(3):
            day = (after + timedelta(days=offset)).date()
            for s in self._slots_for_day(day, mc):
                if s > after:
                    return s
        return None

    def _find_entry(self):
        cfg = self._get_config()
        key = (cfg.get("mail_call", {}) or {}).get("bbs_key") or {}
        if not key.get("callsign"):
            return None
        for e in cfg.get("bbs_list", []):
            if (e.get("callsign", "").upper() == key.get("callsign", "")
                and e.get("transport", "")    == key.get("transport", "")
                and e.get("host", "")         == key.get("host", "")):
                return e
        return None

    def _validate(self):
        """
        Return (entry, error_text). entry is None when Mail-Call cannot
        currently fire (and error_text describes why for the status line).
        """
        cfg = self._get_config()
        mc  = cfg.get("mail_call", {}) or {}
        if not mc.get("enabled"):
            return None, "Mail-Call: disabled"
        entry = self._find_entry()
        if entry is None:
            return None, "Mail-Call: chosen BBS no longer in BBS List"
        # Only RF requires responsibility acceptance — Telnet has no RF
        # safety surface, so the scheduler does not gate Telnet fires on it.
        if entry.get("transport") != "telnet":
            if int(mc.get("responsibility_accepted_rf_version", 0)) \
                    < SettingsDialog.MAILCALL_RESPONSIBILITY_VERSION:
                return None, "Mail-Call: RF responsibility not accepted"
        return entry, ""

    def _refresh(self):
        entry, err = self._validate()
        if err:
            self._next_fire = None
            self._emit_status(err)
            return
        self._next_fire = self._compute_next_fire(self._now())
        self._update_status()

    def _tick(self):
        # Re-validate on every tick — config or bbs_list may have changed
        entry, err = self._validate()
        if err:
            self._next_fire = None
            self._emit_status(err)
            return

        if self._next_fire is None:
            self._next_fire = self._compute_next_fire(self._now())

        now = self._now()
        if self._next_fire is not None and now >= self._next_fire:
            self._do_fire(entry)
            # Compute the slot AFTER the one we just fired, so an immediate
            # re-tick doesn't loop on the same time.
            self._next_fire = self._compute_next_fire(self._next_fire + timedelta(minutes=1))
        self._update_status()

    def _do_fire(self, entry):
        if self._is_busy():
            self.sig_skipped.emit("manual session already active")
            return
        self.sig_fire.emit(entry)

    # ── status text ───────────────────────────────────────────────

    def _update_status(self):
        if self._next_fire is None:
            return   # _refresh already emitted the "disabled / not ready" text
        now   = self._now()
        delta = self._next_fire - now
        secs  = max(0, int(delta.total_seconds()))
        h, m  = divmod(secs // 60, 60)
        tz    = (self._get_config().get("mail_call", {}) or {}).get("time_zone", "local")
        tz_lbl = "UTC" if tz == "utc" else "Local"
        time_str = self._next_fire.strftime("%H:%M")
        self._emit_status(f"Next Mail-Call: {time_str} {tz_lbl}  (in {h}h {m:02d}m)")

    def _emit_status(self, text):
        if self._status_paused:
            return
        if text != self._last_status:
            self._last_status = text
            self.sig_status.emit(text)


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    VIEW_MAIL     = 0
    VIEW_TERMINAL = 1
    VIEW_DEBUG    = 2

    def __init__(self):
        super().__init__()
        self.config  = load_config()
        _default_data = os.path.join(_APP_DIR, "data")
        _data_dir_cfg = self.config["app"].get("data_dir") or ""
        # Treat relative or empty data_dir as relative to _APP_DIR, not cwd.
        # This ensures the database is always found regardless of where the
        # exe or script is launched from.
        if not _data_dir_cfg or not os.path.isabs(_data_dir_cfg):
            _data_dir = _default_data
        else:
            _data_dir = _data_dir_cfg
        self.db      = MessageDatabase(data_dir=_data_dir)
        self.cdb     = ContactsDB(self.db._db_file)
        self.worker  = None
        self._current_folder = "inbox"
        self._current_row_id = None

        # Persistent lightweight VARA command connection for pre-session config
        from transport import VaraControl
        vara_cfg = self.config.get("vara", {})
        self._vara_ctrl = VaraControl(
            vara_host=vara_cfg.get("hf_host", "127.0.0.1"),
            cmd_port=vara_cfg.get("hf_cmd_port", 8300),
            data_port=vara_cfg.get("hf_data_port", 8301),
        )
        self._vara_ctrl.open()   # silent — ok if VARA not running yet

        # Quiet re-link. VARA takes one client, and until QtC holds its
        # port VARA's TUNE button refuses ("link VARA with RMS Express…").
        # open() above only ran at startup, so starting VARA after QtC left
        # it unlinked until the dropdown changed or a session ended (Bill,
        # 2026-09-28). Every few seconds, while a VARA entry is selected
        # and QtC is idle, try again — off the GUI thread, since a dead
        # remote host takes the full 2 s connect timeout.
        self._vara_relink_busy = False
        self._vara_was_linked  = self._vara_ctrl.is_open
        self._vara_relink_timer = QTimer(self)
        self._vara_relink_timer.setInterval(5000)
        self._vara_relink_timer.timeout.connect(self._vara_relink_tick)
        self._vara_relink_timer.start()

        mycall = self.config.get("user", {}).get("callsign", "").upper()
        self.setWindowTitle(f"QtC - {mycall}" if mycall else "QtC")
        self.setMinimumSize(920, 620)
        self.resize(1120, 720)

        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._pending_summary = None   # held summary waiting for Mail view switch
        # Connected AND logged in — not merely "a session object exists".
        # worker.session is set the moment the session is constructed,
        # before connect_and_login() runs, so it is NOT the same thing and
        # cannot stand in for this. Written in exactly four places:
        # _on_connected (True), _on_disconnected / _on_error / the VARA
        # recovery timer (False). Until v0.15.0 this state was carried by
        # the enabled flag of a permanently hidden Refresh button, read
        # back through getattr — so deleting that button would have left
        # the Bulletins tab quietly believing it was never connected.
        self._logged_in = False
        self._login_only_connect = False   # last Connect was from Terminal/Debug
        self._sr_active       = False   # a Send / Receive round trip is running
        self._pending_outbox  = False   # outbox ready, waiting for Mail view switch
        self._send_total      = 0
        self._send_current    = 0
        self._last_disconnect_time = 0.0   # used to add extra VARA reset delay
        self._last_link_bps   = 0          # last seen VARA link speed for estimates
        self._post_lc_action  = None   # callable to run after sig_categories_ready
                                       # (used to chain LC → bulletin check in the
                                       # connect flow; cleared as it fires)
        self._update_folder_counts()   # populate badges on startup

        self._refresh_folder("inbox")
        self._set_status("Ready — not connected", connected=False)
        # Apply saved font size to all text panes
        font_size = self.config.get("app", {}).get("font_size", 10)
        self._apply_font_size(font_size)

        # Mail-Call !!! scheduler — fires Connect at configured times.
        # is_busy uses btn_connect.isEnabled() since worker isn't cleared
        # back to None on disconnect (see _on_disconnected).
        self._mc_scheduler = MailCallScheduler(
            get_config=lambda: self.config,
            is_busy=lambda: not self.btn_connect.isEnabled(),
            parent=self,
        )
        self._mc_scheduler.sig_fire.connect(self._mc_handle_fire)
        self._mc_scheduler.sig_status.connect(self._mc_update_status)
        self._mc_scheduler.sig_skipped.connect(self._mc_handle_skipped)
        self._mc_scheduler.start()

        # Mail-Call retry state. The scheduler fires sig_fire when a slot is
        # due; the handler then runs a combined-budget retry loop that counts
        # both channel-busy detections AND VARA connect-failures against a
        # single MC_MAX_TRIES budget. A wall-clock MC_SLOT_DEADLINE_SECS cap
        # rolls the slot over no matter what, preventing busy↔fail ping-pong.
        self._mc_active_entry   = None   # presence = "Mail-Call owns this connect"
        self._mc_tries_used     = 0      # combined attempt counter for current slot
        self._mc_slot_deadline  = None   # datetime; stamped on first fire of slot
        self._mc_retry_pending  = False  # countdown active between attempts
        self._mc_retry_target   = None
        self._mc_retry_deadline = None
        # Stays True from the moment Mail-Call claims a slot through the
        # full session lifecycle until _on_disconnected runs. Used by the
        # bulletin path to bypass the selection dialog (unattended runs
        # auto-download all new bulletins). _mc_active_entry is too narrow
        # — it clears on connect success, before bulletins are even seen.
        self._mc_session_owned  = False
        # _on_disconnected clears _mc_session_owned, but when the link drops
        # mid-session the worker's sig_error can land just after it (the
        # modem reader and the worker are different threads). This keeps
        # "that session was Mail-Call's" until the next connect, so
        # _on_error can still send the drop to Notifications, not a popup.
        self._mc_owned_at_disconnect = False
        self._mc_drop_noted          = False   # one notification per drop
        self._mc_retry_timer = QTimer(self)
        self._mc_retry_timer.setInterval(1000)   # 1s status countdown tick
        self._mc_retry_timer.timeout.connect(self._mc_retry_tick)

    # ── Menu bar ──────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")
        act_settings = QAction("&Settings…", self)
        act_settings.triggered.connect(self._on_settings)
        act_addrbook = QAction("&Address Book…", self)
        act_addrbook.triggered.connect(self._on_address_book)
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_settings)
        file_menu.addAction(act_addrbook)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        # Message — mail actions (issue #3). Each also works as a single
        # keystroke when the message list has focus (see Help → Keyboard
        # Shortcuts). Lambdas click the MailView buttons, which exist by the
        # time these fire, so disabled/enabled state is honored.
        msg_menu = mb.addMenu("&Message")
        act_new = QAction("✏  &New Message", self)
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        act_new.triggered.connect(lambda: self.mail_view.btn_new.click())
        act_reply = QAction("↩  &Reply", self)
        act_reply.setShortcut(QKeySequence("Ctrl+R"))
        act_reply.triggered.connect(lambda: self.mail_view.btn_reply.click())
        act_del = QAction("🗑  &Delete", self)
        act_del.triggered.connect(lambda: self.mail_view.btn_delete.click())
        act_find = QAction("🔍  &Search", self)
        act_find.setShortcut(QKeySequence("Ctrl+F"))
        act_find.triggered.connect(lambda: self.mail_view.btn_search.click())
        act_markall = QAction("✓  Mark All R&ead", self)
        act_markall.setShortcut(QKeySequence("Ctrl+Shift+M"))
        act_markall.triggered.connect(
            lambda: self.mail_view.btn_mark_all_read.click())
        act_outbox = QAction("🔄  Send / &Receive", self)
        act_outbox.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_outbox.triggered.connect(
            lambda: self.mail_view.btn_send_outbox.click())
        for a in (act_new, act_reply, act_del, act_find):
            msg_menu.addAction(a)
        msg_menu.addSeparator()
        msg_menu.addAction(act_markall)
        msg_menu.addAction(act_outbox)

        # View — function keys switch the central pane
        view_menu = mb.addMenu("&View")
        act_mail = QAction("📬  Mail View", self)
        act_mail.setShortcut(QKeySequence("F2"))
        act_term = QAction("💻  Terminal View", self)
        act_term.setShortcut(QKeySequence("F3"))
        act_debug = QAction("🔬  Debug View", self)
        act_debug.setShortcut(QKeySequence("F4"))
        act_mail.triggered.connect(lambda: self._switch_view(self.VIEW_MAIL))
        act_term.triggered.connect(lambda: self._switch_view(self.VIEW_TERMINAL))
        act_debug.triggered.connect(lambda: self._switch_view(self.VIEW_DEBUG))
        view_menu.addAction(act_mail)
        view_menu.addAction(act_term)
        view_menu.addAction(act_debug)

        # Help
        help_menu = mb.addMenu("&Help")
        act_keys = QAction("⌨  &Keyboard Shortcuts", self)
        act_keys.setShortcut(QKeySequence("F1"))
        act_keys.triggered.connect(self._on_shortcuts_help)
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_keys)
        help_menu.addAction(act_about)

    # ── Toolbar ───────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("Connection")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setStyleSheet("QToolBar { spacing: 4px; padding: 3px; }")
        self.addToolBar(tb)

        # ── BBS dropdown (shared by both panels) ───────
        tb.addWidget(QLabel(" BBS: "))
        self.bbs_combo = QComboBox()
        self.bbs_combo.setMinimumWidth(200)
        self.bbs_combo.setToolTip("Select a saved BBS station")
        self._reload_bbs_combo()
        self.bbs_combo.currentIndexChanged.connect(self._on_combo_changed)
        tb.addWidget(self.bbs_combo)

        tb.addSeparator()

        # ── Stacked panel: VARA (0) / Telnet (1) ───────
        self.tb_stack = QStackedWidget()
        self.tb_stack.setFixedHeight(28)

        # ── Panel 0: VARA ──────────────────────────────
        vara_panel = QWidget()
        vara_layout = QHBoxLayout(vara_panel)
        vara_layout.setContentsMargins(0, 0, 0, 0)
        vara_layout.setSpacing(4)

        vara_layout.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["VARA HF", "VARA FM", *AGW_MODEMS.values()])
        self.mode_combo.setFixedWidth(
            self.mode_combo.fontMetrics().horizontalAdvance("(Qt)SoundModem") + 36)
        self.mode_combo.setToolTip(
            "RF modem — VARA, or Direwolf / (Qt)SoundModem for AX.25 packet")
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        vara_layout.addWidget(self.mode_combo)

        vara_layout.addWidget(QLabel("  Call:"))
        self.call_edit = QLineEdit()
        self.call_edit.setFixedWidth(88)
        self.call_edit.setPlaceholderText("N0CALL-1")
        self.call_edit.setToolTip("BBS callsign to connect to over RF")
        vara_layout.addWidget(self.call_edit)

        self.bw_label = QLabel("  BW:")   # hidden with bw_combo for packet
        vara_layout.addWidget(self.bw_label)
        self.bw_combo = QComboBox()
        # Populated lazily by _toolbar_bw_set_for_mode — HF starts with
        # 500/2300 kHz options, FM swaps in NARROW/WIDE when picked.
        # Width fits "NARROW" + arrow without clipping.
        self.bw_combo.setFixedWidth(110)
        self.bw_combo.setToolTip("VARA bandwidth — must match the BBS node setting")
        self.bw_combo.currentTextChanged.connect(lambda bw: self._vara_set_bw(bw))
        vara_layout.addWidget(self.bw_combo)
        self._toolbar_bw_set_for_mode("VARA HF", preferred="")

        self.tb_stack.addWidget(vara_panel)   # index 0

        # ── Panel 1: Telnet ────────────────────────────
        telnet_panel = QWidget()
        telnet_layout = QHBoxLayout(telnet_panel)
        telnet_layout.setContentsMargins(0, 0, 0, 0)
        telnet_layout.setSpacing(4)

        telnet_layout.addWidget(QLabel("Call:"))
        self.telnet_call_edit = QLineEdit()
        self.telnet_call_edit.setFixedWidth(88)
        self.telnet_call_edit.setPlaceholderText("N0CALL-1")
        self.telnet_call_edit.setToolTip("BBS callsign")
        telnet_layout.addWidget(self.telnet_call_edit)

        telnet_layout.addWidget(QLabel("  IP:"))
        self.host_edit = QLineEdit()
        self.host_edit.setFixedWidth(130)
        self.host_edit.setPlaceholderText("IP or hostname")
        self.host_edit.setToolTip("IP address or hostname")
        telnet_layout.addWidget(self.host_edit)

        telnet_layout.addWidget(QLabel("  Port:"))
        self.port_edit = QLineEdit()
        self.port_edit.setFixedWidth(50)
        self.port_edit.setPlaceholderText("8010")
        self.port_edit.setToolTip("Telnet port")
        telnet_layout.addWidget(self.port_edit)

        self.tb_stack.addWidget(telnet_panel)  # index 1

        tb.addWidget(self.tb_stack)

        # ── Save / Connect / Disconnect (shared) ───────
        tb.addSeparator()

        self.btn_save_bbs = QPushButton("💾")
        self.btn_save_bbs.setFixedWidth(30)
        self.btn_save_bbs.setToolTip("Save current settings as a new BBS entry")
        self.btn_save_bbs.clicked.connect(self._on_save_bbs)
        tb.addWidget(self.btn_save_bbs)

        self.btn_connect    = QPushButton("⚡ Connect")
        self.btn_disconnect = QPushButton("✖ Disconnect")
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        tb.addWidget(self.btn_connect)
        tb.addWidget(self.btn_disconnect)

        # Stop a slow download without dropping the link: interrupts the
        # read and sends 'A' to the BBS to return to the command prompt.
        # Hidden until a download is actually running (see _on_progress).
        # "Abort" is what the BBS itself calls this — `A` is BPQ's
        # abort-paged-output command (Bill, 2026-09-23). Naming the button
        # after the command means the button and the command list agree.
        self.btn_abort = QPushButton("⏹ Abort")
        self.btn_abort.setToolTip(self._ABORT_TIP)
        self.btn_abort.setStyleSheet(
            "QPushButton { background-color:#5a2a2a; color:#ffcccc; "
            "border:1px solid #aa4444; }"
            "QPushButton:disabled { background-color:transparent; "
            "color:#777777; border:1px solid #555555; }")
        self.btn_abort.clicked.connect(self._on_abort)
        # Always visible so it's easy to find; enabled only while a download
        # is actually running (see _on_progress). Greyed-out at rest tells
        # the user the capability exists.
        self.btn_abort.setEnabled(False)
        tb.addWidget(self.btn_abort)

        # A hidden "🔄 Refresh" button lived here. It was made invisible on
        # 2026-09-20 — in Terminal / Debug view you type `lm` yourself, and
        # asking QtC to fetch mail is what Mail view is for — but it stayed
        # in the toolbar because its enabled flag had become the app's
        # is-logged-in state. That state is now self._logged_in, and the
        # button is gone with its handler (Bill, 2026-09-23). Send / Receive
        # is the one button for "get my mail".

        tb.addSeparator()

        # ── Connection indicator ───────────────────────
        self.conn_light = QLabel("●")
        self.conn_light.setStyleSheet("color:#888888; font-size:18px;")
        self.conn_label = QLabel("  Not connected")
        self.conn_label.setMinimumWidth(120)
        self.conn_label.setMaximumWidth(480)
        self.conn_label.setStyleSheet("padding-right: 4px;")
        tb.addWidget(self.conn_light)
        tb.addWidget(self.conn_label)

        # ── Spacer ────────────────────────────────────
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        # Progress shown in conn_label during operations (no separate widget needed)

        # View picker — one dropdown in place of the old Terminal and Debug
        # toggle buttons (Bill, 2026-09-23). It also gives the toolbar a Mail
        # entry it never had: before this, the only way back to Mail view was
        # F2 or the View menu. Index == VIEW_* constant, so no mapping table.
        self.view_combo = QComboBox()
        self.view_combo.setFixedWidth(132)
        self.view_combo.setToolTip(
            "Switch the main view\n"
            "Mail (F2) · Terminal (F3) · Debug (F4)")
        self.view_combo.addItem("📬  Mail",     self.VIEW_MAIL)
        self.view_combo.addItem("💻  Terminal", self.VIEW_TERMINAL)
        self.view_combo.addItem("🔬  Debug",    self.VIEW_DEBUG)
        self.view_combo.currentIndexChanged.connect(self._on_view_combo)
        tb.addWidget(self.view_combo)

        # Populate fields from first entry (or last used)
        last_idx = self.config.get("app", {}).get("last_bbs_index", 0)
        if last_idx and last_idx < self.bbs_combo.count():
            self.bbs_combo.setCurrentIndex(last_idx)
        else:
            self._on_combo_changed(0)

    # ── Central stacked widget ────────────────────────────────────

    def _build_central(self):
        self.stack = QStackedWidget()

        # Page 0 — Mail view
        self.mail_view = MailView()
        self.mail_view.sig_new_message.connect(self._on_new_message)
        self.mail_view.sig_reply.connect(self._on_reply)
        self.mail_view.sig_delete.connect(self._on_delete)
        self.mail_view.sig_send_outbox.connect(self._on_send_receive)
        self.mail_view.sig_mark_all_read.connect(self._on_mark_all_read)
        self.mail_view.sig_search.connect(self._on_search)
        self.mail_view.sig_folder_changed.connect(self._on_folder_changed)
        self.mail_view.sig_row_selected.connect(self._on_row_selected)
        self.stack.addWidget(self.mail_view)   # index 0

        # Page 1 — Terminal view (clean readable text only)
        self.terminal = TerminalWidget()
        self.terminal.sig_send_cmd.connect(self._on_terminal_cmd)
        self.terminal.sig_get_file.connect(self._on_get_file_clicked)
        self.stack.addWidget(self.terminal)    # index 1

        # Page 2 — Debug view (all verbose output)
        self.debug_view = DebugWidget()
        self.stack.addWidget(self.debug_view)  # index 2

        self.setCentralWidget(self.stack)
        self.stack.setCurrentIndex(self.VIEW_MAIL)

    # ── Status bar ────────────────────────────────────────────────

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)

        # Main status label — occupies full width when idle
        self.status_label = QLabel("Ready")
        sb.addWidget(self.status_label, 1)

        # Progress detail label — replaces status text during operations
        self._prog_detail = QLabel("")
        self._prog_detail.setVisible(False)
        sb.addWidget(self._prog_detail, 1)

        # VARA link info — SN, bitrate, BW — shown right side during connection
        self._vara_info_label = QLabel("")
        self._vara_info_label.setStyleSheet(
            "color: #888888; font-size: 11px; padding: 0 6px;")
        self._vara_info_label.setVisible(False)
        sb.addPermanentWidget(self._vara_info_label)

        # Mail-Call status — "Next Mail-Call: 14:00 Local (in 1h 47m)" or
        # "Mail-Call: disabled" / error text. Hidden when empty.
        self._mc_status_label = QLabel("")
        self._mc_status_label.setStyleSheet(
            "color:#88aa88; font-size: 11px; padding: 0 6px;")
        self._mc_status_label.setVisible(False)
        sb.addPermanentWidget(self._mc_status_label)

        # Progress bar — fixed width, shown during operations
        self._prog_bar = QProgressBar()
        self._prog_bar.setTextVisible(True)
        self._prog_bar.setFixedHeight(14)
        self._prog_bar.setFixedWidth(200)
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setVisible(False)
        sb.addPermanentWidget(self._prog_bar)

    # ── View switching ────────────────────────────────────────────

    VIEW_NAMES = {0: "Mail", 1: "Terminal", 2: "Debug"}

    def _on_view_combo(self, idx: int):
        """Dropdown chose a view. F2/F3/F4 and the View menu still call
        _switch_view directly — this is only the combo's way in."""
        view = self.view_combo.itemData(idx)
        if view is not None:
            self._switch_view(view)

    def _switch_view(self, view: int):
        # Log every view change. Sessions can finish in a few seconds, so
        # without this the log cannot say what was on screen when something
        # happened — which is exactly what D10b needed (Bill, 2026-09-20).
        # Never affects what goes on the air.
        if view != self.stack.currentIndex():
            # Straight to the debug view, not through _on_log — that would put
            # the line in the status bar over the connection text.
            self.debug_view.append(
                f"[SYS] View: {self.VIEW_NAMES.get(view, view)}\n", "#8888ff")
        self.stack.setCurrentIndex(view)
        # Reflect the change without re-entering through the combo's signal.
        if self.view_combo.currentIndex() != view:
            self.view_combo.blockSignals(True)
            self.view_combo.setCurrentIndex(view)
            self.view_combo.blockSignals(False)
        # Enable data streaming when in Terminal/Debug view
        self._set_transport_terminal_mode(view in (self.VIEW_TERMINAL, self.VIEW_DEBUG))
        if view == self.VIEW_TERMINAL:
            self.terminal.input_line.setFocus()
        elif view == self.VIEW_MAIL:
            # Land focus inside the three-pane Tab cycle so Tab never starts
            # up in the connection toolbar (issue #3).
            self.mail_view.focus_default()
        if view == self.VIEW_MAIL and self._pending_summary is not None:
            # User switched to Mail view after choosing to download from Terminal
            summary = self._pending_summary
            self._pending_summary = None
            self._on_mail_summary(summary)
        elif view == self.VIEW_MAIL and self._pending_outbox:
            # Outbox is waiting. Light up Send / Receive and say so — do
            # NOT start transmitting just because a view changed. Switching
            # views is not consent to key the radio (2026-09-20 flow audit;
            # see feedback_rf_airtime_politeness). The user presses the
            # button when they are ready.
            self._pending_outbox = False
            self.mail_view.enable_send_outbox(True)
            self._set_status(
                "Outbox ready — press Send / Receive when you are.",
                connected=bool(self.worker and self.worker.session))

    def _hands_on_view(self) -> bool:
        """True when a person is driving the BBS by hand: Terminal or Debug
        view is open AND this is not a Mail-Call session. Checked once, at
        Connect: a Connect from these views is a dumb terminal — link up,
        nothing sent or read automatically (Bill, 2026-09-13).

        Mail-Call is unattended, so it always does its full run — LM,
        bulletins, outbox, then `b` — whatever view happened to be left
        open (Bill, 2026-09-12). Before this, a Mail-Call that fired with
        Terminal view up logged in, ran LM, and then sat there as a dumb
        terminal until the BBS timed it out. Switching views after Connect
        changes nothing."""
        return (self.stack.currentIndex() in (self.VIEW_TERMINAL,
                                              self.VIEW_DEBUG)
                and not self._mc_session_owned)

    def showEvent(self, event):
        """On first show, seed keyboard focus into the mail panes (the default
        view) so the initial Tab stays in the three-pane cycle rather than
        walking the connection toolbar (issue #3)."""
        super().showEvent(event)
        if not getattr(self, "_initial_focus_done", False):
            self._initial_focus_done = True
            if self.stack.currentIndex() == self.VIEW_MAIL:
                self.mail_view.focus_default()

    # ── BBS combo helpers ─────────────────────────────────────────

    def _reload_bbs_combo(self):
        self.bbs_combo.blockSignals(True)
        self.bbs_combo.clear()
        for entry in self.config.get("bbs_list", []):
            label = f"{entry['callsign']}  —  {entry.get('name', entry.get('host',''))}"
            self.bbs_combo.addItem(label, userData=entry)
        self.bbs_combo.blockSignals(False)

    def _on_combo_changed(self, index: int):
        entry = self.bbs_combo.itemData(index)
        if not entry:
            return
        transport = entry.get("transport", "vara_hf")
        is_telnet = (transport == "telnet")

        # Switch stacked panel
        self.tb_stack.setCurrentIndex(1 if is_telnet else 0)

        if is_telnet:
            self.telnet_call_edit.setText(entry.get("callsign", ""))
            self.host_edit.setText(entry.get("host", ""))
            self.port_edit.setText(str(entry.get("telnet_port", 8010)))
        else:
            mode_str = {"vara_hf": "VARA HF",
                        **AGW_MODEMS}.get(transport, "VARA FM")
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(mode_str)
            self.mode_combo.blockSignals(False)
            self.call_edit.setText(entry.get("callsign", ""))
            # Populate BW combo for the selected mode using the saved
            # value when valid; the mode default otherwise.
            saved_bw = entry.get("bw", "")
            self._toolbar_bw_set_for_mode(mode_str, preferred=saved_bw)
            self._vara_set_bw(self.bw_combo.currentText())

    # VARA bandwidth dropdown options — mirrors _BBSEntryDialog so the
    # toolbar and the entry dialog stay in sync. HF uses kHz numbers
    # (BW500/BW2300 on the wire); FM uses bare NARROW/WIDE keywords.
    _TB_VARA_BW = {
        "VARA HF": (["500", "2300"], "500"),
        "VARA FM": (["NARROW", "WIDE"], "NARROW"),
    }

    def _toolbar_bw_set_for_mode(self, mode_str: str, preferred: str = ""):
        """Repopulate the toolbar BW combo for the given VARA mode.

        preferred is the value to select if it's valid for the new mode
        (e.g. the saved bw on a BBS entry); otherwise the previous combo
        text is kept if still valid, else the mode default.
        """
        is_vara = mode_str in self._TB_VARA_BW
        # Packet has no bandwidth on QtC's side — hide the control
        self.bw_label.setVisible(is_vara)
        self.bw_combo.setVisible(is_vara)
        if not is_vara:
            return
        opts, default = self._TB_VARA_BW[mode_str]
        current = self.bw_combo.currentText()
        pick = (preferred if preferred in opts else
                current if current in opts else
                default)
        self.bw_combo.blockSignals(True)
        self.bw_combo.clear()
        self.bw_combo.addItems(opts)
        self.bw_combo.setCurrentText(pick)
        self.bw_combo.blockSignals(False)

    def _on_mode_changed(self, mode_str: str):
        # Swap BW dropdown options when the user toggles HF vs FM in the
        # toolbar. Don't push to VARA here — the bw_combo change signal
        # fires _vara_set_bw on its own once the new value is selected.
        self._toolbar_bw_set_for_mode(mode_str)

    def _on_save_bbs(self):
        is_telnet = (self.tb_stack.currentIndex() == 1)
        if is_telnet:
            if not self.host_edit.text().strip():
                QMessageBox.warning(self, "Missing Host", "Enter a hostname or IP address first.")
                return
        else:
            if not self.call_edit.text().strip():
                QMessageBox.warning(self, "Missing Callsign", "Enter the BBS callsign first.")
                return
        name, ok = QInputDialog.getText(self, "Save BBS", "Friendly name (e.g. Home Node):")
        if not ok:
            return
        mode_str = self.mode_combo.currentText() if not is_telnet else "Telnet"
        transport_map = {"VARA HF": "vara_hf", "VARA FM": "vara_fm",
                         "Telnet": "telnet",
                         **{v: k for k, v in AGW_MODEMS.items()}}
        is_agw   = transport_map[mode_str] in AGW_MODEMS
        active   = self._get_active_bbs_entry()
        port_str = self.port_edit.text().strip()
        new_entry = {
            "name":        name.strip(),
            "callsign":    (self.call_edit.text().strip().upper() if not is_telnet
                            else self.telnet_call_edit.text().strip().upper()
                                 or self.host_edit.text().strip().upper().split(".")[0]),
            "transport":   transport_map[mode_str],
            "vara_type":   ("hf" if mode_str == "VARA HF" else
                            "fm" if mode_str == "VARA FM" else None),
            "bw":          (self.bw_combo.currentText()
                            if not (is_telnet or is_agw) else ""),
            "host":        (self.host_edit.text().strip() if is_telnet else
                            active.get("host") or "127.0.0.1" if is_agw else ""),
            "agw_port":    active.get("agw_port", AGW_DEFAULT_PORT),
            "agw_radio_port": active.get("agw_radio_port", 0),
            "telnet_port": int(port_str) if port_str.isdigit() else 8010,
            "notes":       ""
        }
        self.config.setdefault("bbs_list", []).append(new_entry)
        save_config(self.config)
        self._reload_bbs_combo()
        self.bbs_combo.setCurrentIndex(self.bbs_combo.count() - 1)
        QMessageBox.information(self, "Saved", f"{new_entry['callsign']} saved to your BBS list.")

    def _is_home_bbs(self) -> bool:
        """Return True if currently connected BBS matches the user's Home BBS."""
        home = self.config.get("user", {}).get("home_bbs", "").upper().strip()
        if not home:
            return True   # no home BBS set — allow bulletins anywhere
        connected = self._get_active_bbs_entry().get("callsign", "").upper().strip()
        # Home BBS may be in hierarchical form e.g. N0CALL.#REGION.ST.USA.NOAM
        # Match on the first segment (node callsign) only
        home_call = home.split(".")[0].split("-")[0]
        conn_call = connected.split(".")[0].split("-")[0]
        return home_call == conn_call

    def _get_active_bbs_entry(self):
        is_telnet = (self.tb_stack.currentIndex() == 1)
        base  = self.bbs_combo.currentData() or {}
        entry = dict(base)
        if is_telnet:
            host     = self.host_edit.text().strip()
            port_str = self.port_edit.text().strip()
            call     = self.telnet_call_edit.text().strip().upper()
            entry["transport"]   = "telnet"
            entry["host"]        = host or entry.get("host", "127.0.0.1")
            entry["telnet_port"] = int(port_str) if port_str.isdigit() else 8010
            entry["callsign"]    = call or entry.get("callsign",
                                       host.upper().split(".")[0])
        else:
            mode_str = self.mode_combo.currentText()
            call     = self.call_edit.text().strip().upper()
            entry["callsign"]  = call or entry.get("callsign", "")
            agw_key = {v: k for k, v in AGW_MODEMS.items()}.get(mode_str)
            if agw_key:
                if base.get("transport") != agw_key:
                    # Selected entry is VARA/Telnet or the other modem —
                    # its host/port aren't this modem's
                    entry["host"]           = "127.0.0.1"
                    entry["agw_port"]       = AGW_DEFAULT_PORT
                    entry["agw_radio_port"] = 0
                entry["transport"] = agw_key
                entry["vara_type"] = None
                entry["bw"]        = ""
            else:
                entry["transport"] = "vara_hf" if mode_str == "VARA HF" else "vara_fm"
                entry["vara_type"] = "hf"      if mode_str == "VARA HF" else "fm"
                entry["bw"]        = self.bw_combo.currentText()
        return entry

    # ── VARA pre-session control ──────────────────────────────────

    def _vara_set_bw(self, bw: str):
        """
        Send BW500 or BW2300 to VARA immediately via the persistent
        VaraControl socket.  Silently re-opens the socket if it closed.
        Called on BBS dropdown change and BW combo change so the
        waterfall markers always reflect the selected entry.
        """
        if self.mode_combo.currentText() not in self._TB_VARA_BW:
            return   # packet mode — nothing to tell VARA
        if not self._vara_ctrl.is_open:
            self._vara_ctrl.open()
        if self._vara_ctrl.is_open:
            self._vara_ctrl.set_bandwidth(bw)

    def _vara_relink_tick(self):
        """Re-link VaraControl to VARA if it is not held. GUI thread.

        Idle only: Connect enabled means no session, no connect in
        progress and no post-error VARA recovery — the times QtC has
        deliberately let go of the port for VaraTransport."""
        linked = self._vara_ctrl.is_open
        if linked and not self._vara_was_linked:
            # Just linked (by this timer or anywhere else): put the
            # selected entry's bandwidth on VARA's waterfall.
            self.debug_view.append("[SYS] Linked to VARA\n", "#888888")
            self._vara_set_bw(self.bw_combo.currentText())
        self._vara_was_linked = linked
        if (linked or self._vara_relink_busy
                or (self._get_active_bbs_entry() or {}).get("transport")
                    not in ("vara_hf", "vara_fm")
                or not self.btn_connect.isEnabled() or self._logged_in):
            return
        self._vara_relink_busy = True
        gen = self._vara_ctrl.gen

        def _try():
            try:
                self._vara_ctrl.open(gen=gen)
            finally:
                self._vara_relink_busy = False
        threading.Thread(target=_try, name="VaraRelink", daemon=True).start()

    # ── Connection ────────────────────────────────────────────────

    def _on_connect(self):
        entry     = self._get_active_bbs_entry()
        transport = entry.get("transport", "vara_hf")

        if transport == "telnet" and not entry.get("host"):
            QMessageBox.warning(self, "No Host", "Enter a hostname or IP address.")
            return
        if transport != "telnet" and not entry.get("callsign"):
            QMessageBox.warning(self, "No Callsign", "Enter the BBS callsign to connect to.")
            return

        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        self._mc_owned_at_disconnect = False
        self._mc_drop_noted          = False

        if transport == "telnet":
            conn_desc = f"{entry['host']}:{entry['telnet_port']}"
        elif transport in AGW_MODEMS:
            conn_desc = (f"{AGW_MODEMS[transport]}  "
                         f"{entry.get('host') or '127.0.0.1'}:"
                         f"{entry.get('agw_port') or AGW_DEFAULT_PORT}  "
                         f"channel {entry.get('agw_radio_port', 0)}")
        else:
            is_hf = (entry.get("vara_type") == "hf")
            mode_label = "VARA HF" if is_hf else "VARA FM"
            freq = entry.get("freq", "")
            bw   = entry.get("bw", "500" if is_hf else "NARROW")
            # HF prints as "BW500"/"BW2300"; FM prints the keyword as-is.
            bw_label = f"BW{bw}" if is_hf else str(bw).upper()
            conn_desc = f"{mode_label}  {freq}  {bw_label}".strip()

        self._set_status(f"Connecting to {entry['callsign']}  ({conn_desc})...", connecting=True)
        self.terminal.append(
            f"\n=== Connecting to {entry['callsign']}  [{conn_desc}] ===\n", "#aaffaa")

        self.worker = SessionWorker(self.config, entry, self.db)
        self.worker.sig_log.connect(self._on_log)
        self.worker.sig_connected.connect(self._on_connected)
        self.worker.sig_rf_connected.connect(self._on_rf_connected)
        self.worker.sig_disconnected.connect(self._on_disconnected)
        self.worker.sig_mail_summary.connect(self._on_mail_summary)
        self.worker.sig_download_done.connect(self._on_download_done)
        self.worker.sig_send_result.connect(self._on_send_result)
        self.worker.sig_error.connect(self._on_error)
        self.worker.sig_ll_ready.connect(self._on_ll_ready)
        self.worker.sig_progress.connect(self._on_progress)
        self.worker.sig_bulletin_check.connect(self._on_bulletin_check)
        self.worker.sig_bulletin_done.connect(self._on_bulletin_done)
        self.worker.sig_categories_ready.connect(self._on_categories_ready)
        self.worker.sig_yapp_progress.connect(self._on_yapp_progress)
        self.worker.sig_yapp_done.connect(self._on_yapp_done)
        self.worker.sig_yapp_error.connect(self._on_yapp_error)

        # Release the pre-session VaraControl socket so VaraTransport
        # can open its own connection to port 8300.
        # After a recent disconnect, VARA needs extra time to reset its TCP
        # listener — especially after a remote BBS-initiated disconnect where
        # VARA completes a full RF clean-disconnect sequence (PTT, CW ID, etc).
        # We calculate how long we still need to wait based on time elapsed
        # since the last disconnect, with a minimum of 8s after a recent one.
        if entry.get("transport", "") in ("vara_hf", "vara_fm"):
            self._vara_ctrl.close()
            import time as _time
            elapsed = _time.time() - self._last_disconnect_time
            VARA_RESET_SECS = 8.0   # VARA needs up to 8s after RF disconnect
            still_needed = VARA_RESET_SECS - elapsed
            if still_needed > 0:
                self._set_status(
                    f"Waiting for VARA to reset… ({int(still_needed)}s)",
                    connecting=True)
                _time.sleep(still_needed)

        # Remember this BBS so it pre-selects on next launch
        self.config.setdefault("app", {})["last_bbs_index"] = self.bbs_combo.currentIndex()
        save_config(self.config)

        # Connecting from Terminal / Debug view is a dumb terminal. Decided
        # here, at Connect — switching views afterwards doesn't change it.
        self._login_only_connect = self._hands_on_view()
        # Record the decision, not just the view: this one line explains
        # everything the session does or doesn't do afterwards, however the
        # views get switched later (Bill, 2026-09-20).
        self.debug_view.append(
            f"[SYS] Connect from {self.VIEW_NAMES.get(self.stack.currentIndex(), '?')} "
            f"view — {'dumb terminal, QtC drives nothing' if self._login_only_connect else 'QtC drives this session'}\n",
            "#8888ff")
        self.worker.do_connect_and_check(login_only=self._login_only_connect)

    def _on_disconnect(self):
        if self.worker:
            self.worker.do_disconnect()
        self._set_status("Disconnecting…", connecting=True)

    def _set_transport_terminal_mode(self, enabled: bool):
        """Enable/disable background data streaming on the active transport."""
        if self.worker and self.worker.session:
            t = self.worker.session.transport
            if hasattr(t, "set_terminal_mode"):
                t.set_terminal_mode(enabled)

    def _apply_text_codec(self, codec: str):
        """Apply the selected character set (issue #2) to the live transport
        so the next BBS text decodes with it. The choice is set in App
        settings and persisted there; this just pushes it to a running
        session. cp437/cp850 let DOS-art bulletins (e.g. the TECH area)
        render their box-drawing graphics instead of replacement glyphs."""
        if self.worker and self.worker.session:
            t = self.worker.session.transport
            if hasattr(t, "set_text_codec"):
                t.set_text_codec(codec)
            self.terminal.append(f"\n[charset set to {codec}]\n", "#888888")

    def _on_rf_connected(self):
        """Fires immediately when RF link is up — before login/registration."""
        self.terminal.append("\n=== Connected ===\n", "#00ff88")
        self.debug_view.append("\n=== Connected ===\n", "#00ff88")

    def _on_connected(self):
        entry      = self._get_active_bbs_entry()
        transport  = entry.get("transport", "")
        if transport == "telnet":
            detail = entry.get("host", "")
        elif transport in AGW_MODEMS:
            detail = f"{AGW_MODEMS[transport]}  AX.25"
        elif transport == "vara_hf":
            bw = entry.get("bw", "500")
            detail = f"VARA HF  BW{bw}"
        else:
            # VARA FM — bw is a bare keyword ("NARROW"/"WIDE"), no prefix
            bw = entry.get("bw", "NARROW")
            detail = f"VARA FM  {str(bw).upper()}"
        self._set_status(
            f"Connected  ·  {entry['callsign']}  ({detail})",
            connected=True)
        self._logged_in = True
        # Send / Receive is available for the whole session, on any
        # transport — it is how a Terminal connect asks QtC to do a mail
        # round trip. _on_disconnected turns it off again.
        self.mail_view.enable_send_outbox(True)
        self.terminal.set_connected(True)

        # If Mail-Call owns this slot, the connect succeeded — clear all
        # retry state so the scheduler returns to normal "Next Mail-Call: …"
        # status. The session itself proceeds (mail check, disconnect) just
        # like a manual connect from here on.
        if self._mc_active_entry is not None:
            self._on_log(
                f"[SCHED] Mail-Call: connected on try "
                f"{self._mc_tries_used}/{self.MC_MAX_TRIES} — slot complete")
            self._mc_end_retry()

        if self._login_only_connect:
            self._set_status("Terminal mode — type commands manually.",
                             connected=True)
            self._check_outbox_for_terminal()

    def _check_outbox_for_terminal(self):
        """In Terminal/Debug view with no mail check running, still notify
        user if there are outbox messages ready to send for this BBS."""
        pending = self.db.get_pending_outbox()
        if not pending:
            return
        # Everything pending is sendable: Send / Receive sends the lot.
        # The old filter here counted only the send_now rows, so the notice
        # could say "1 message ready" when three were about to go.
        sendable = pending
        if sendable:
            self._pending_outbox = True
            self.mail_view.enable_send_outbox(True)
            notice = (f"\n[MAIL] {len(sendable)} message(s) ready to send "
                      f"— Mail view → Send / Receive when you are ready.\n")
            self.terminal.append(notice, "#ffff00")
            self.debug_view.append(notice, "#ffff00")
            self._set_status(
                f"{len(sendable)} message(s) in outbox — "
                f"Mail view → Send / Receive.",
                connected=True)

    def _on_ll_ready(self, bbs_call: str, new_only: list, all_personal: list,
                     bulletins: list):
        """
        Receives the filtered personal-mail lists from
        _run_connect_and_check via sig_ll_ready — safe GUI-thread
        delivery, no shared worker attributes. Stores them on self then
        dispatches to _on_first_visit.
        """
        self._ll_new_only     = new_only       # PN — status N, to mycall
        self._ll_all_personal = all_personal   # PN + PY — all personal to mycall
        # `bulletins` is always empty now — bulletins get their
        # own bounded `L> CATEGORY` sweep in _start_bulletin_stage() rather
        # than being scraped out of a BBS-wide listing. The signal keeps
        # its shape so the Terminal/Mail dispatch below did not have to move.
        self._on_first_visit(bbs_call)

    def _on_first_visit(self, bbs_callsign: str):
        """
        Called by _on_ll_ready after login and the LM mail check.
        Both mail lists are on self._ll_new_only and self._ll_all_personal,
        delivered safely on the GUI thread by _on_ll_ready.

        Mail View:
          - First visit: ask All / New only → marks BBS visited
          - Return visit: auto-download PN new only

        Only a Mail-view Connect gets here: a Terminal / Debug Connect is a
        dumb terminal and never runs the mail check. Switching to Terminal
        to watch doesn't change what happens.
        """
        mycall       = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        visit_key    = f"{mycall}@{bbs_callsign}"
        visited      = self.config.get("visited_bbs", {})
        new_only     = getattr(self, "_ll_new_only",     [])
        all_personal = getattr(self, "_ll_all_personal", [])

        # Migrate old-style keys (just "BBS_CALL") to new "MYCALL@BBS_CALL" format
        if bbs_callsign in visited and visit_key not in visited:
            visited[visit_key] = visited.pop(bbs_callsign)
            self.config["visited_bbs"] = visited
            save_config(self.config)

        # ── Mail View ─────────────────────────────────────────────
        if visit_key not in visited:
            # First visit — ask All / New only
            self.debug_view.append(
                f"[SYS] First visit to {bbs_callsign} — asking All (PN+PY) "
                f"or New only (PN); waiting for the operator\n", "#ffff00")
            msg = QMessageBox(self)
            msg.setWindowTitle(f"First visit to {bbs_callsign}")
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setText(
                f"<b>Welcome to {bbs_callsign}!</b><br><br>"
                f"This appears to be your first time connecting here as <b>{mycall}</b>.<br><br>"
                f"Would you like to download <b>all your personal messages</b> "
                f"from this BBS (PN and PY), or just <b>new unread messages</b> (PN only)?<br><br>"
                f"<i>Downloading all messages may take much longer depending on how "
                f"many you have and how fast the RF link is. Messages can accumulate "
                f"over 30 days before the BBS housekeeping removes them, and some "
                f"sysops set longer retention periods.<br><br>"
                f"On every future visit, only new unread personal messages (PN) "
                f"will be downloaded automatically.</i>"
            )
            btn_all = msg.addButton("📥  All personal messages (PN + PY)",
                                    QMessageBox.ButtonRole.YesRole)
            btn_new = msg.addButton("⚡  New messages only (PN)",
                                    QMessageBox.ButtonRole.NoRole)
            # Neither role makes Qt pick an escape button on its own, so
            # Escape / the window X left clickedButton() as None and
            # `.text()` on it crashed — on the FIRST connect to any new
            # BBS (2026-09-20 flow audit). Name the escape button, and
            # still treat None as the quiet choice: new mail only, the
            # least airtime and what every later visit does anyway.
            msg.setEscapeButton(btn_new)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked is None:
                self._on_log("[SYS] First-visit prompt dismissed — "
                             "fetching new messages only")
            full = (clicked is btn_all)

            self.config.setdefault("visited_bbs", {})[visit_key] = True
            save_config(self.config)

            if full:
                if all_personal:
                    QTimer.singleShot(50, lambda p=all_personal: self.worker.do_download(p))
                else:
                    self._set_status("No personal mail.", connected=True)
                    self._prompt_outbox()
            else:
                if new_only:
                    QTimer.singleShot(50, lambda p=new_only: self.worker.do_download(p))
                else:
                    self._set_status("No new personal mail.", connected=True)
                    self._prompt_outbox()
        else:
            # Returning visit — auto-download PN new only
            if new_only:
                n = len(new_only)
                self._set_status(f"Downloading {n} new message(s)…", connected=True)
                QTimer.singleShot(50, lambda p=new_only: self.worker.do_download(p))
            else:
                self._set_status("No new personal mail.", connected=True)
                self._start_bulletin_stage()

    def _on_progress(self, op: str, current: int, total: int, detail: str):
        """Update toolbar pill and status bar progress during operations.

        `current` is the 1-based item being worked on RIGHT NOW — the one
        on the air, not the number already finished. Both counters used to
        pass the finished count while labelling it "Downloading {n} of
        {total}", so a run of three read "1 of 3" while the second was
        still going out, and started at "0 of 3" (Bill, 2026-09-23).

        The bar stays on completed work, (current - 1) / total, so it
        reaches 100% only when everything really has gone. Text says what
        is happening; the bar says how much is behind you."""
        # The Stop/abort button only makes sense while we're RECEIVING — it
        # tells the BBS to quit sending. Show it during a download, hide it
        # otherwise (sending/outbox or done).
        self.btn_abort.setEnabled(op == "downloading")
        if op == "done" or total == 0:
            self.btn_abort.setEnabled(False)
            self._prog_bar.setVisible(False)
            self._prog_detail.setVisible(False)
            self.status_label.setVisible(True)
            # conn_label will be updated by next _set_status call
            return
        pct = int(max(0, current - 1) / total * 100)
        op_label = "downloading" if op == "downloading" else "sending"
        # Update conn_label to show progress — always visible in toolbar
        self.conn_label.setText(f"  {current} / {total}  {op_label}")
        # Status bar: hide plain label, show detail + progress bar
        self.status_label.setVisible(False)
        verb = "Downloading" if op == "downloading" else "Sending"
        line = f"{verb} {current} of {total} · {detail}"
        self._prog_detail.setText(line)
        self._prog_detail.setVisible(True)
        self._prog_bar.setValue(pct)
        self._prog_bar.setVisible(True)
        self.debug_view.append(f"[SYS] {line}\n", "#8888ff")

    # Abort covers mail and bulletins only. File downloads cannot be
    # stopped: LinBPQ commits the whole file to the link the moment the
    # transfer starts, so a cancel saved nothing but the partial file —
    # the J7 runs (2026-09-23) cost minutes of airtime per try, and the
    # session was unusable until the rest had arrived anyway. Greyed out
    # with this reason for the whole of a YAPP transfer (Bill, 2026-09-28).
    _ABORT_TIP = (
        "Abort the mail or bulletin download in progress and return to\n"
        "the BBS command prompt (sends 'A' to the BBS). You stay connected,\n"
        "so you can disconnect cleanly or carry on.\n\n"
        "File downloads cannot be aborted.")
    _ABORT_TIP_YAPP = (
        "File downloads cannot be aborted.\n\n"
        "Once a file transfer starts, the BBS has already committed the\n"
        "whole file to the link and keeps sending it whatever QtC does.\n"
        "Stopping early would only throw away the part received so far.")

    def _on_abort(self):
        """Abort button — bail out of a slow mail or bulletin download
        without dropping the link. Interrupts the worker's blocked read
        immediately; the download path then sends 'A' to the BBS and
        resyncs to the command prompt, and the session stays up.

        Never live during a YAPP file transfer — see _ABORT_TIP_YAPP."""
        if self.worker:
            self.worker.do_abort()
        # Disable so a second click doesn't queue another abort; the progress
        # 'done' update hides it once the resync finishes.
        self.btn_abort.setEnabled(False)
        self.terminal.append("\n[aborting — returning to BBS prompt]\n",
                             "#ffaa55")
        self._set_status("Aborting…", connected=True)

    def _on_disconnected(self):
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self._logged_in = False
        self.mail_view.enable_send_outbox(False)
        self._set_status("Disconnected", connected=False)
        self.terminal.append("\n=== Disconnected ===\n", "#ff8844")
        self.debug_view.append("\n=== Disconnected ===\n", "#ff8844")
        self.terminal.set_connected(False)
        self._pending_summary = None
        self._pending_outbox  = False
        self._send_total      = 0
        self._send_current    = 0
        self._send_done       = 0
        # A link that drops mid Send / Receive must not leave the flag set,
        # or the next one would be refused as "already running".
        self._sr_active       = False
        self._prog_bar.setVisible(False)
        self._prog_detail.setVisible(False)
        self.status_label.setVisible(True)
        self._vara_info_label.setVisible(False)
        self._vara_info_label.setText("")
        # Mark that we just disconnected — _on_connect will apply
        # a longer VARA reset delay if this was a remote disconnect
        self._last_disconnect_time = __import__("time").time()
        # Mail-Call session (if any) is now over — any subsequent connect
        # from the toolbar must be treated as manual until the scheduler
        # claims another slot.
        self._mc_owned_at_disconnect = self._mc_session_owned
        self._mc_session_owned = False
        # Reclaim the VaraControl socket for pre-session BW commands
        self._vara_ctrl.open()

    # ── YAPP file download ────────────────────────────────────────

    @property
    def _downloads_dir(self) -> str:
        """Return (and create) the QtC downloads directory."""
        d = os.path.join(_APP_DIR, "downloads")
        os.makedirs(d, exist_ok=True)
        return d

    def _on_get_file_clicked(self):
        """
        Show the YAPP download dialog.
        User types 'files' in the terminal first to see what's available,
        then clicks Get File and enters the filename.
        """
        if not self.worker or not self.worker.session:
            QMessageBox.warning(self, "Not Connected",
                "Connect to a BBS first, then type 'files' to see available files.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Get File via YAPP")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        form = QFormLayout()

        filename_edit = QLineEdit()
        filename_edit.setPlaceholderText("e.g. testfile1.txt")
        filename_edit.setToolTip(
            "Enter the filename exactly as shown by the 'files' command.\n"
            "Filenames with spaces are not supported by LinBPQ YAPP.")
        form.addRow("Filename:", filename_edit)

        savedir_row = QHBoxLayout()
        savedir_edit = QLineEdit(self._downloads_dir)
        savedir_edit.setToolTip("Local directory where the file will be saved.")
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(72)
        savedir_row.addWidget(savedir_edit)
        savedir_row.addWidget(browse_btn)
        form.addRow("Save to:", savedir_row)

        layout.addLayout(form)

        note = QLabel(
            "<i>Note: filenames with spaces cannot be downloaded via YAPP on LinBPQ.</i>")
        note.setStyleSheet("color: #888; font-size: 11px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        def _browse():
            from PyQt6.QtWidgets import QFileDialog
            d = QFileDialog.getExistingDirectory(
                dlg, "Choose Download Folder", savedir_edit.text())
            if d:
                savedir_edit.setText(d)

        browse_btn.clicked.connect(_browse)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        filename = filename_edit.text().strip()
        save_dir = savedir_edit.text().strip() or self._downloads_dir

        if not filename:
            QMessageBox.warning(self, "No Filename", "Enter a filename to download.")
            return

        if " " in filename:
            QMessageBox.warning(
                self, "Filename Contains Spaces",
                "LinBPQ YAPP does not support filenames with spaces.\n"
                "The BBS will report 'File not found'.\n\n"
                "Please rename the file on the BBS or use 'read <filename>' "
                "to display it as plain text.")
            return

        # A file transfer runs to completion once it starts — there is no
        # Abort for it (see _ABORT_TIP_YAPP), and over RF that is real
        # airtime: 5 KB is about 7 minutes at 300 baud (F2, 2026-09-28).
        # Say so before the radio keys, not after. Telnet is instant, so
        # it gets no dialog.
        transport = self._get_active_bbs_entry().get("transport", "")
        if transport != "telnet":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Start file download?")
            box.setText(
                f"<b>A file download cannot be stopped once it starts.</b>"
                f"<br><br>The BBS sends the whole of <b>{filename}</b>, and "
                f"the session cannot be used until it has finished. On a "
                f"slow link that can be many minutes of airtime — about 7 "
                f"minutes for a 5 KB file at 300 baud.<br><br>"
                f"Download <b>{filename}</b>?")
            go = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(go)
            box.exec()
            if box.clickedButton() is not go:
                self.terminal.append(
                    "\n[YAPP] Download cancelled — nothing sent.\n", "#ffaa55")
                self.debug_view.append(
                    "[YAPP] Download cancelled before it started\n", "#ffaa55")
                return

        self.terminal.append(
            f"\n[YAPP] Requesting '{filename}'…\n", "#00ccff")
        self.btn_abort.setEnabled(False)
        self.btn_abort.setToolTip(self._ABORT_TIP_YAPP)
        self.worker.do_yapp_download(filename, save_dir)

    def _on_yapp_progress(self, done: int, total: int, filename: str):
        """Update terminal with YAPP transfer progress. Abort stays greyed
        out — a file transfer cannot be stopped (see _ABORT_TIP_YAPP)."""
        if total > 0:
            pct = int(done / total * 100)
            self.terminal.append(
                f"[YAPP] {done}/{total} bytes  ({pct}%)\n", "#00ccff")
        else:
            self.terminal.append(
                f"[YAPP] {done} bytes received…\n", "#00ccff")

    def _on_yapp_done(self, save_path: str, display_name: str):
        """Show completion notice in terminal."""
        self.btn_abort.setEnabled(False)
        self.btn_abort.setToolTip(self._ABORT_TIP)
        line = f"[YAPP] ✓ Saved: {save_path}\n"
        self.terminal.append(line, "#00ff88")
        self.debug_view.append(line, "#00ff88")
        self._set_status(f"File saved: {display_name}", connected=True)

    def _on_yapp_error(self, msg: str):
        """Show YAPP error in terminal — or a plain note when the user is
        the one who stopped it, which is not an error."""
        self.btn_abort.setEnabled(False)
        self.btn_abort.setToolTip(self._ABORT_TIP)
        if msg == _YAPP_ABORTED:
            line = "[YAPP] Transfer aborted — partial file discarded.\n"
            self.terminal.append(line, "#ffaa55")
            self.debug_view.append(line, "#ffaa55")
            self._set_status("File download aborted", connected=True)
            return
        line = f"[YAPP] Error: {msg}\n"
        self.terminal.append(line, "#ff4444")
        self.debug_view.append(line, "#ff4444")
        self._set_status("YAPP transfer failed", connected=True)

    def _on_error(self, msg: str):
        self.btn_connect.setEnabled(False)   # briefly disabled while VARA recovers
        self.btn_disconnect.setEnabled(False)
        self._logged_in = False
        self._set_status(f"Error: {msg}", connected=False)
        self.terminal.append(f"\n[ERROR] {msg}\n", "#ff4444")
        self.debug_view.append(f"\n[ERROR] {msg}\n", "#ff4444")

        # If Mail-Call owns this attempt, suppress the modal popup (it would
        # block unattended operation) and trigger a retry — or roll over if
        # the budget/deadline is exhausted. The 8-sec VARA recovery still
        # runs; by the time the retry fires (~3 min later) btn_connect is
        # long since re-enabled.
        if self._mc_active_entry is not None:
            self._mc_handle_connect_failure(msg)
        elif self._mc_link_dropped():
            # Mail-Call had connected and the link went mid-session —
            # nobody is at the radio to click a popup away.
            self._mc_notify_link_drop(msg)
        else:
            QMessageBox.critical(self, "Connection Error", msg)

        if self._get_active_bbs_entry().get("transport") in AGW_MODEMS:
            # Packet modems have no TCP listener to reset — the next
            # connect can go straight away.
            self.btn_connect.setEnabled(True)
            return

        # Give VARA a moment to reset its TCP listener before allowing reconnect
        # and reclaiming VaraControl — a failed RF connect can take ~10-15s to recover
        self.terminal.append("[SYS] Waiting for VARA to reset…\n", "#888888")
        QTimer.singleShot(8000, self._vara_recover)

    def _vara_recover(self):
        """Called ~8s after a connection error to re-enable connect and reclaim VARA."""
        self._vara_ctrl.open()
        self.btn_connect.setEnabled(True)
        self._logged_in = False   # not connected yet — stays off until next connect
        self.terminal.append("[SYS] VARA ready — you may reconnect.\n", "#888888")

    def _on_log(self, line: str):
        """
        Route session log lines to the correct view(s):
        - Debug view: ALL lines (full verbose output)
        - Terminal view: only [RX] and [TX] lines (readable BBS text)
        - Status bar: last line regardless
        - VARA info label: updated from [BITRATE] lines
        """
        # Always goes to debug
        self.debug_view.append(line + "\n", "#8888ff")

        # Parse VARA link statistics from [BITRATE] log lines
        # Format: [BITRATE] VARA HF  CONNECTED  BW500  1200 BPS
        # or:     [BITRATE] SPEED 1200  SN 18  BW 500
        if "[BITRATE]" in line.upper() or "[IAMALIVE]" in line.upper():
            self._update_vara_info(line)

        # Terminal gets only readable BBS content — not PTT/CMD/SYS noise
        tag = line.split("]")[0].lstrip("[").strip().upper() if "]" in line else ""
        if tag == "TX":
            clean = line[line.find("]")+1:].strip()
            self.terminal.append(clean + "\n", "#aaffaa")
        elif tag == "RX":
            chunk = line[line.find("]")+1:].strip()
            if chunk:
                clean = self._format_bbs_output(chunk)
                self.terminal.append(clean + "\n", "#ffffff")

        self.status_label.setText(line[:100])

    def _update_vara_info(self, line: str):
        """Extract and display VARA link stats from a log line."""
        import re
        bps_m = re.search(r'(\d+)\s*BPS', line, re.I)
        sn_m  = re.search(r'\bSN[:\s]+(\d+)', line, re.I)
        bw_m  = re.search(r'BW[:\s]*(\d+)', line, re.I)
        parts = []
        if bps_m:
            parts.append(f"{bps_m.group(1)} bps")
            self._last_link_bps = int(bps_m.group(1))
        if sn_m:
            parts.append(f"SN {sn_m.group(1)}")
        if bw_m:
            parts.append(f"BW{bw_m.group(1)}")
        if parts:
            self._vara_info_label.setText("  ".join(parts))
            self._vara_info_label.setVisible(True)

    def _format_bbs_output(self, text: str) -> str:
        """
        Reformat BBS output for clean terminal display.
        Message listings arrive as a run-on string — split them into
        one-per-line format. Each entry starts with a message number
        followed by a date (e.g. "259    14-Mar").

        Column format from LinBPQ:
          NUM   DATE   TYPE  SIZE  TO [@HOMEBBS]  FROM  SUBJECT

        We pad the @HOMEBBS field (or insert blank padding if absent)
        so the FROM callsign and subject always align.
        Also replaces bare \r with newlines for multi-line responses.
        """
        import re
        # Replace carriage returns with newlines
        text = text.replace("\r", "\n")
        # Split message list entries — each starts with digits + spaces + date
        text = re.sub(r'(\s+)(\d{3,5}\s{2,}\d{2}-[A-Z][a-z]{2})',
                      r'\n\2', text)

        # Process each line to fix @HOMEBBS alignment
        lines = text.split("\n")
        fixed = []
        # Matches a full message listing line:
        # NUM  DATE  TYPE  SIZE  TO  [@BBS]  FROM  SUBJECT
        msg_re = re.compile(
            r'^(\d{3,5}\s+\d{2}-\w+\s+\w+\s+\d+\s+)(\S+)(\s+)(@\S+)?(\s+)(\S+\s*.*)$'
        )
        for line in lines:
            m = msg_re.match(line.strip())
            if m:
                prefix   = m.group(1)       # num date type size
                to_call  = m.group(2)       # TO callsign
                at_bbs   = m.group(4) or "" # @HOMEBBS (may be absent)
                rest     = m.group(6)       # FROM + SUBJECT
                # Pad @HOMEBBS to 10 chars (or insert blank if absent)
                at_padded = at_bbs.ljust(10) if at_bbs else " " * 10
                # Split FROM callsign from subject — pad FROM to 10 chars
                rest_parts = rest.strip().split(None, 1)
                if len(rest_parts) == 2:
                    from_call, subject = rest_parts
                    fixed.append(
                        f"{prefix}{to_call:<10} {at_padded}   {from_call:<10}   {subject}")
                else:
                    fixed.append(f"{prefix}{to_call:<10} {at_padded}   {rest.strip()}")
            else:
                fixed.append(line)

        text = "\n".join(fixed)
        # Clean up multiple blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ── Mail summary — AUTO-DOWNLOAD (no dialog) ──────────────────

    def _on_mail_summary(self, summary: BBSMailSummary):
        self._update_folder_counts()

        new_count    = len(summary.new_personal)
        bullet_count = len(summary.new_bulletins)

        mail_line = f"\n[MAIL] {new_count} new personal, {bullet_count} new bulletins\n"
        self.terminal.append(mail_line, "#aaffff")
        self.debug_view.append(mail_line, "#aaffff")

        if summary.new_personal:
            # A Send / Receive downloads straight away whatever view the
            # session was started from — the user pressed the button, and
            # they are already looking at Mail view. Holding the summary
            # for a "switch to Mail view" that has already happened would
            # be a dead end (2026-09-20 flow audit).
            if self._login_only_connect and not self._sr_active:
                self._pending_summary = summary
                notice = (f"\n[MAIL] {new_count} message(s) ready — "
                          f"Mail view → Send / Receive to download.\n")
                self.terminal.append(notice, "#ffff00")
                self.debug_view.append(notice, "#ffff00")
                self._set_status(
                    f"{new_count} new message(s) available — "
                    f"Mail view → Send / Receive to download.",
                    connected=True)
            else:
                # Auto-download — no dialog, per user preference
                self._set_status(
                    f"Downloading {new_count} new message(s)…", connected=True)
                self.worker.do_download(summary.new_personal)
        else:
            # Send / Receive stops here: no mail, and bulletins are not
            # part of this button.
            if self._sr_active:
                self._sr_finish("no new mail")
                return
            self._set_status("No new personal mail.", connected=True)
            self._start_bulletin_stage()

    def _start_bulletin_stage(self):
        """
        Single entry point for the bulletin stage, used by every path that
        finishes the mail stage: connect-with-mail, connect-without-mail,
        and the manual Refresh check.

        Runs LC first when it is due (Home BBS, every LC_REFRESH_DAYS) so
        newly-created categories are discovered, then chains into the
        `L> CATEGORY` sweep. Falls through to the outbox when bulletins
        are off, there are no subscriptions, or this is not the Home BBS.
        """
        bull_cfg  = self.config.get("bulletins", {})
        subs      = bull_cfg.get("subscriptions", [])
        on_home   = self._is_home_bbs()
        want_bull = bool(bull_cfg.get("check_on_connect", False)
                         and subs and on_home)

        # Auto Category Update — LC at most once per LC_REFRESH_DAYS. Per
        # feedback_rf_airtime_politeness this is gated by an absolute time
        # delta, never by "every connect".
        if on_home and self._auto_category_update_due():
            self._set_status("Updating bulletin categories…", connected=True)
            self._post_lc_action = (
                (lambda s=subs: self._begin_bulletin_check(s)) if want_bull
                else self._prompt_outbox)
            QTimer.singleShot(200, lambda: self.worker
                .do_list_categories(open_dialog=False))
            return

        if want_bull:
            self._begin_bulletin_check(subs)
            return
        self._prompt_outbox()

    def _begin_bulletin_check(self, subs):
        """Kick off the per-category `L> CATEGORY` sweep.

        A Mail-Call session is unattended, so it reads each category as
        soon as the listing pauses — no selection dialog, and no 'A'
        round trip. An interactive session collects candidates from every
        category first and shows one dialog."""
        self._set_status("Checking bulletins…", connected=True)
        unattended = bool(self._mc_session_owned)
        QTimer.singleShot(200, lambda: self.worker.do_check_bulletins(
            subs, unattended=unattended))

    LC_REFRESH_DAYS = 30

    def _auto_category_update_due(self) -> bool:
        """True iff 'Check for new bulletins on connect' is on AND
        (last_lc_at is None OR (now - last_lc_at) >= LC_REFRESH_DAYS).
        Caller is expected to verify we're on Home BBS first.

        30 days matches SessionWorker.OP_REFRESH_DAYS deliberately, so LC
        and OP fall due on the same connect and a normal session carries
        no housekeeping traffic at all."""
        bull_cfg = self.config.get("bulletins", {}) or {}
        if not bull_cfg.get("check_on_connect", False):
            return False
        auto_cfg = bull_cfg.get("auto_category_update", {}) or {}
        last = auto_cfg.get("last_lc_at")
        if not last:
            return True
        import datetime as _dt
        try:
            last_dt = _dt.datetime.strptime(
                str(last).replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return True   # bad stored value — treat as never
        return (_dt.datetime.utcnow() - last_dt) >= \
               _dt.timedelta(days=self.LC_REFRESH_DAYS)

    def _on_categories_ready(self, categories, open_dialog):
        """Worker has fetched LC, persisted the cache to config, and may
        have inserted system-message notifications for newly-appeared
        categories. Refresh the inbox so the user sees them, then chain
        into any pending post-LC action."""
        try:
            self._refresh_folder("inbox")
            self._update_folder_counts()
        except Exception:
            pass
        action = self._post_lc_action
        self._post_lc_action = None
        if action:
            try:
                action()
            except Exception as e:
                self._on_log(f"[SYS] Post-LC action failed: {e}")

    def _on_download_done(self, count: int):
        self._set_status(
            f"Downloaded {count} message(s).", connected=True)
        self._refresh_folder("inbox")
        self._update_folder_counts()
        dl_line = f"\n[MAIL] Downloaded {count} message(s) to inbox.\n"
        self.terminal.append(dl_line, "#aaffff")
        self.debug_view.append(dl_line, "#aaffff")

        # Send / Receive ends here — no bulletin sweep, and no outbox
        # prompt (it just sent the outbox itself).
        if self._sr_active:
            self._sr_finish(
                f"{count} message(s) received" if count else "nothing new")
            return

        # Bulletin stage — its own bounded `L> CATEGORY` sweep. Returns
        # here only if there is nothing to sweep; otherwise the outbox
        # prompt happens after the bulletin check completes.
        bull_cfg = self.config.get("bulletins", {})
        if (self._is_home_bbs()
                and bull_cfg.get("check_on_connect", False)
                and bull_cfg.get("subscriptions", [])):
            self._start_bulletin_stage()
            return

        # Prompt about outbox / auto-disconnect
        pending = self.db.get_pending_outbox()
        if pending:
            self.mail_view.enable_send_outbox(True)
            # Mail-Call session: auto-send without prompting — same
            # premise as the bulletin bypass (unattended overnight run).
            if self._mc_session_owned:
                auto_line = (f"[OUTBOX] Mail-Call session — auto-sending "
                             f"{len(pending)} message(s).\n")
                self.terminal.append(auto_line, "#aaffff")
                self.debug_view.append(auto_line, "#aaffff")
                self._on_send_outbox()
                return
            r = QMessageBox.question(
                self, "Outbox",
                f"{len(pending)} message(s) waiting in outbox — send now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r == QMessageBox.StandardButton.Yes:
                self._on_send_outbox()
                return
        self._prompt_outbox()

    def _on_bulletin_check(self, bulletins_by_cat: dict):
        """Called when bulletin check completes — show selection dialog."""
        if not bulletins_by_cat:
            bull_line = "\n[BULL] No new bulletins.\n"
            self.terminal.append(bull_line, "#aaffff")
            self.debug_view.append(bull_line, "#aaffff")
            self._set_status("No new bulletins.", connected=True)
            # Now check outbox
            self._prompt_outbox()
            return

        total = sum(len(v) for v in bulletins_by_cat.values())
        bull_line = f"\n[BULL] {total} new bulletin(s) found.\n"
        self.terminal.append(bull_line, "#aaffff")
        self.debug_view.append(bull_line, "#aaffff")

        # Get current link speed from last seen BITRATE for estimate
        link_bps = getattr(self, "_last_link_bps", 0)

        bbs_id = (f"{self.config.get('user',{}).get('callsign','NOCALL').upper()}"
                  f"@{self._get_active_bbs_entry().get('callsign','')}")

        # Mail-Call session: skip the selection dialog and auto-download
        # every new bulletin. Premise (see Mail-Call design): a station
        # running scheduled connects stays current, so the unattended
        # batch is small. Forcing a user-input dialog would stall the
        # session forever overnight.
        if self._mc_session_owned:
            auto_line = (f"[BULL] Mail-Call session — auto-selecting all "
                         f"{total} bulletin(s), no dialog.\n")
            self.terminal.append(auto_line, "#aaffff")
            self.debug_view.append(auto_line, "#aaffff")
            self._set_status(
                f"Downloading {total} bulletin(s)…", connected=True)
            self.worker.do_download_bulletins(bulletins_by_cat)
            return

        dlg = BulletinSelectDialog(bulletins_by_cat,
                                   link_bps=link_bps, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            selected = dlg.get_selected()

            # Tombstone anything the user unchecked — never show again
            for cat, msgs in bulletins_by_cat.items():
                selected_nums = {m.msg_number
                                 for m in selected.get(cat, [])}
                skipped = [m for m in msgs
                           if m.msg_number not in selected_nums]
                if skipped:
                    self.db.add_bulletin_tombstones_batch(skipped, bbs_id)

            if selected:
                total_sel = sum(len(v) for v in selected.values())
                self._set_status(
                    f"Downloading {total_sel} bulletin(s)…", connected=True)
                self.worker.do_download_bulletins(selected)
                return

        # User skipped entire dialog or selected none — tombstone everything
        for cat, msgs in bulletins_by_cat.items():
            self.db.add_bulletin_tombstones_batch(msgs, bbs_id)
        self._set_status("Bulletins skipped.", connected=True)
        self._prompt_outbox()

    def _on_bulletin_done(self, count: int):
        """Called when bulletin downloads complete."""
        self._update_folder_counts()
        bull_line = f"\n[BULL] Downloaded {count} bulletin(s).\n"
        self.terminal.append(bull_line, "#aaffff")
        self.debug_view.append(bull_line, "#aaffff")
        self._set_status(f"Downloaded {count} bulletin(s).", connected=True)
        self._prompt_outbox()

    def _prompt_outbox(self):
        """Check outbox and prompt to send if pending — shared by mail and bulletin flows."""
        pending = self.db.get_pending_outbox()
        if pending:
            self.mail_view.enable_send_outbox(True)
            # Mail-Call session: same premise as the bulletin bypass — the
            # session is unattended, so prompting for "send now?" would
            # stall forever. Auto-send the queued outbox messages.
            if self._mc_session_owned:
                auto_line = (f"[OUTBOX] Mail-Call session — auto-sending "
                             f"{len(pending)} message(s).\n")
                self.terminal.append(auto_line, "#aaffff")
                self.debug_view.append(auto_line, "#aaffff")
                self._on_send_outbox()
                return
            r = QMessageBox.question(
                self, "Outbox",
                f"{len(pending)} message(s) waiting in outbox — send now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r == QMessageBox.StandardButton.Yes:
                self._on_send_outbox()
                return   # disconnect will happen after send completes

        self._auto_disconnect_if_done()

    def _auto_disconnect_if_done(self):
        """Hang up once the automatic work (mail, bulletins, outbox) is done,
        in the cases where nobody will do it by hand:

          - A Mail-Call session, on ANY transport — it is unattended.
            This used to be Telnet-only, so every packet and VARA Mail-Call
            sat connected doing nothing until LinBPQ's idle timer dropped
            it: 15 minutes holding the BBS port, with RR polls on the air
            every 3 minutes (300 baud Direwolf run, 2026-09-11).
          - ANY session QtC drove itself — i.e. one started from Mail
            view. Until 2026-09-20 this was Telnet-only, on the reasoning
            that LinBPQ has no idle timer for Telnet while RF has its own.
            But an RF session left sitting costs far more than a Telnet
            one: LinBPQ holds the BBS port for 15 minutes and polls RR
            every ~3 minutes on a shared channel. Bill hit it on air
            (2026-09-20, Direwolf, bulletin sweep ended with no new
            bulletins and the link just sat there). One rule now:

                Mail view      — QtC drives, and QtC hangs up.
                Terminal/Debug — you drive, and you hang up.

        Which of the two it is was decided at Connect
        (_login_only_connect), not by whichever view happens to be open
        now, so switching views to watch changes nothing.
        """
        if not (self.worker and self.worker.session):
            return
        if self._mc_session_owned:
            why = "Mail-Call session complete"
        elif not self._login_only_connect:
            why = "Session complete"
        else:
            return
        self._set_status(f"{why} — disconnecting…", connected=True)
        QTimer.singleShot(800, self._on_disconnect)

    # ── Outbox ────────────────────────────────────────────────────

    def _on_send_receive(self):
        """Send / Receive — one button, one mail round trip (Bill,
        2026-09-20).

            outbox (SP/SB, if anything is queued)  →  LM  →  download new

        Bulletins are deliberately NOT part of it: they run on a Mail view
        connect, and keeping them out keeps this button one simple idea.
        If people ask for them later we can add them.

        This is what makes the Terminal-connect workflow whole — connect as
        a dumb terminal, drive the BBS by hand, then switch to Mail view and
        press one button to do the mail. It is also the only automatic
        read in such a session, and it happens because the user asked.

        The chain is driven by _sr_active through _on_send_result,
        _on_mail_summary and _on_download_done, and always ends in
        _sr_finish — including on the paths where nothing is sent, nothing
        is new, or a send fails.
        """
        if not (self.worker and self.worker.session):
            QMessageBox.warning(self, "Not Connected",
                "Connect to a BBS first, then press Send / Receive.")
            return
        if self._sr_active:
            # Log it, don't just flash the status bar. A press that is
            # deliberately ignored should leave a trace — otherwise the
            # only record of the refusal is a line that the next log
            # message overwrites a second later (2026-09-20, D13c).
            busy = "[S/R] Send / Receive already running — second press ignored"
            self.terminal.append("\n" + busy + "\n", "#ffaa55")
            self.debug_view.append(busy + "\n", "#ffaa55")
            self._set_status("Send / Receive already running…", connected=True)
            return

        self._sr_active = True
        pending = self.db.get_pending_outbox()
        line = ("\n[S/R] Send / Receive — "
                + (f"sending {len(pending)} message(s), then checking mail\n"
                   if pending else "nothing to send, checking mail\n"))
        self.terminal.append(line, "#aaffff")
        self.debug_view.append(line, "#aaffff")

        if pending:
            self._on_send_outbox()      # _on_send_result chains onward
        else:
            self._sr_mail_check()

    def _sr_mail_check(self):
        """Second leg of Send / Receive: LM, then download whatever is new."""
        if not (self.worker and self.worker.session):
            self._sr_finish("Link gone before the mail check")
            return
        self._set_status("Send / Receive — checking mail…", connected=True)
        self.worker.do_mail_check(new_only=True)

    def _sr_finish(self, summary_text: str):
        """Every Send / Receive path ends here — success, nothing to do, or
        a failure. Clears the flag first so the button works again even if
        the hang-up below does nothing."""
        was_active = self._sr_active
        self._sr_active = False
        if not was_active:
            return
        done = f"\n[S/R] Send / Receive complete — {summary_text}.\n"
        self.terminal.append(done, "#aaffff")
        self.debug_view.append(done, "#aaffff")
        self._set_status(f"Send / Receive complete — {summary_text}.",
                         connected=bool(self.worker and self.worker.session))
        # A Mail-view session hangs up here; a Terminal one stays up for
        # the user to carry on typing (see _auto_disconnect_if_done).
        self._auto_disconnect_if_done()

    def _on_send_outbox(self):
        pending = self.db.get_pending_outbox()
        if not pending:
            if self._sr_active:
                self._sr_finish("nothing to send")
                return
            QMessageBox.information(self, "Outbox", "Outbox is empty.")
            return
        if not self.worker or not self.worker.session:
            if self._sr_active:
                self._sr_finish("not connected")
                return
            QMessageBox.warning(self, "Not Connected",
                "Connect to your home BBS first.")
            return
        total = len(pending)
        self._set_status(f"Sending {total} outbox message(s)…", connected=True)
        self._send_total   = total
        self._send_current = 0
        # _send_done counts every reply, sent or failed; _send_current
        # counts only the successes. The batch is finished when _send_done
        # reaches total — if it were keyed to the successes instead, one
        # failed send would leave the batch waiting forever, and with it
        # the Send / Receive chain and the end-of-session hang-up. Found
        # in the 2026-09-20 flow audit. (The progress pill no longer comes
        # from either of them — _run_send drives it as each send starts.)
        self._send_done    = 0
        for i, row in enumerate(pending, 1):
            self.worker.do_send(
                row["to_call"], row["subject"],
                row["body"],    row["msg_type"],
                row["at_bbs"] or "", index=i, total=total)

    def _on_send_result(self, success: bool, to_call: str):
        total = getattr(self, "_send_total", 1) or 1
        if success:
            for row in self.db.get_pending_outbox():
                if row["to_call"].upper() == to_call.upper():
                    self.db.mark_sent(row["id"])
                    break
            # Increment address book use count so this contact rises in dropdown
            self.cdb.increment_use(to_call)
            # The counter is driven from _run_send now, at the moment each
            # send begins. Emitting here as well would put it back one
            # behind — this is where it used to be done, and it is what
            # made a three-message batch read "1 / 3" while the second was
            # still going out.
            self._send_current = getattr(self, "_send_current", 0) + 1
            self._refresh_folder(self._current_folder)
            self._update_folder_counts()
            self._set_status(f"Message sent to {to_call}", connected=True)
            self.terminal.append(
                f"\n[SEND] Message sent to {to_call}\n", "#aaffff")
        else:
            self._set_status(f"Send to {to_call} failed", connected=True)
            self.terminal.append(
                f"\n[SEND] Send to {to_call} FAILED — still in the Outbox\n",
                "#ffaa55")
            QMessageBox.warning(self, "Send Failed",
                f"Could not send message to {to_call}.\n"
                "It stays in the Outbox so you can try again.\n"
                "Check the terminal view for details.")

        # Batch bookkeeping runs for BOTH outcomes, so one failed send can
        # never leave the batch (and the Send / Receive chain, and the
        # end-of-session hang-up) waiting on a reply that already came.
        self._send_done = getattr(self, "_send_done", 0) + 1
        if self._send_done < total:
            return
        self.worker.sig_progress.emit("done", 0, 0, "")
        if self._sr_active:
            self._sr_mail_check()       # second leg: LM + download
        else:
            self._auto_disconnect_if_done()

    # ── Folder / message display ──────────────────────────────────

    def _on_folder_changed(self, folder: str):
        self._refresh_folder(folder)

    def _refresh_folder(self, folder: str):
        self._current_folder = folder
        if   folder == "inbox":    rows = self.db.get_inbox()
        elif folder == "outbox":   rows = self.db.get_outbox()
        elif folder == "sent":     rows = self.db.get_sent()
        elif folder == "notifications": rows = self.db.get_notifications()
        elif folder == "bulletins": rows = self.db.get_bulletins()
        elif folder.startswith("bulletin:"):
            cat  = folder.split(":", 1)[1]
            rows = self.db.get_bulletins(category=cat)
        else:
            rows = self.db.get_sent()
        self.mail_view.load_table(rows, folder)
        # Sync folder tree selection
        item_map = {"inbox":         self.mail_view._fi,
                    "outbox":        self.mail_view._fo,
                    "sent":          self.mail_view._fs,
                    "notifications": self.mail_view._fn,
                    "bulletins":     self.mail_view._fb}
        item = item_map.get(folder)
        if item:
            self.mail_view.folder_tree.setCurrentItem(item)
        elif folder.startswith("bulletin:"):
            cat  = folder.split(":", 1)[1]
            item = self.mail_view._bulletin_cat_items.get(cat)
            if item:
                self.mail_view.folder_tree.setCurrentItem(item)

    def _on_row_selected(self, row_id: int, folder: str):
        self._current_row_id = row_id

        if folder == "inbox":
            rows = self.db.get_inbox()
        elif folder == "outbox":
            rows = self.db.get_outbox()
        elif folder == "notifications":
            rows = self.db.get_notifications()
        elif folder == "bulletins":
            rows = self.db.get_bulletins()
        elif folder.startswith("bulletin:"):
            cat  = folder.split(":", 1)[1]
            rows = self.db.get_bulletins(category=cat)
        else:
            rows = self.db.get_sent()

        for rd in rows:
            if rd["id"] == row_id:
                self.mail_view.show_preview(dict(rd), folder)
                if folder in ("inbox", "notifications") and not rd.get("read", 0):
                    self.db.mark_read(row_id)
                    self._update_folder_counts()
                    self.mail_view.mark_row_read(
                        self.mail_view.current_row_index())
                elif (folder == "bulletins" or folder.startswith("bulletin:")) \
                        and not rd.get("read", 0):
                    self.db.mark_bulletin_read(row_id)
                    self._update_folder_counts()
                    self.mail_view.mark_row_read(
                        self.mail_view.current_row_index())
                break

    # ── Compose / Reply / Delete ──────────────────────────────────

    def _queue_outgoing(self, v: dict):
        """Put a composed message in the outbox.

        This used to be _queue_with_conflict_check, whose whole job was
        asking what to do when two pending messages to the same callsign
        disagreed about a send mode that never did anything. Both are gone
        (Bill, 2026-09-23)."""
        self.db.queue_outgoing(
            to_call=v["to_call"],      subject=v["subject"],
            body=v["body"],            msg_type=v["msg_type"],
            at_bbs=v.get("at_bbs", ""))

    def _on_new_message(self):
        font_size = self.config.get("app", {}).get("font_size", 10)
        dlg = ComposeDialog(parent=self, contacts_db=self.cdb, font_size=font_size)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.get_values()
        if not v["to_call"]:
            QMessageBox.warning(self, "Missing Field",
                "Please enter a To: callsign.")
            return
        self._queue_outgoing(v)
        self._refresh_folder("outbox")   # switch to outbox so user sees queued msg
        self._update_folder_counts()
        if self.worker and self.worker.session:
            self.mail_view.enable_send_outbox(True)
        QMessageBox.information(self, "Queued",
            f"Message to {v['to_call']} saved to outbox.\n"
            f"It goes out on your next Send / Receive.")

    def _on_mark_all_read(self):
        folder = self._current_folder
        if folder == "notifications":
            self.db.mark_all_notifications_read()
            self._refresh_folder("notifications")
        else:
            self.db.mark_all_read()
            self._refresh_folder("inbox")
        self._update_folder_counts()

    def _on_search(self):
        """Run search across requested folders and display results."""
        term  = self.mail_view.get_search_term()
        scope = self.mail_view.get_search_scope()
        if not term:
            return
        all_rows = {
            "inbox":         self.db.get_inbox(),
            "outbox":        self.db.get_outbox(),
            "sent":          self.db.get_sent(),
            "notifications": self.db.get_notifications(),
            "bulletins":     self.db.get_bulletins(),
        }
        self.mail_view.run_search(term, scope, all_rows)

    def _on_reply(self):
        if not self._current_row_id:
            return
        font_size = self.config.get("app", {}).get("font_size", 10)
        for rd in self.db.get_inbox():
            if rd["id"] == self._current_row_id:
                dlg = ComposeDialog(parent=self, reply_to=dict(rd),
                                    contacts_db=self.cdb, font_size=font_size)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    v = dlg.get_values()
                    self._queue_outgoing(v)
                    self._update_folder_counts()   # updates outbox badge count only
                    if self.worker and self.worker.session:
                        self.mail_view.enable_send_outbox(True)
                    QMessageBox.information(self, "Queued",
                        "Reply saved to outbox.")
                break

    def _on_delete(self):
        selected = self.mail_view.get_selected_ids()
        if not selected:
            return

        count = len(selected)
        # Determine label — all bulletins, all messages, or mixed
        bulletin_ids = [(rid, f) for rid, f in selected
                        if f.startswith("bulletin:") or f == "bulletins"]
        msg_ids      = [(rid, f) for rid, f in selected
                        if not (f.startswith("bulletin:") or f == "bulletins")]

        if count == 1:
            label = "bulletin" if bulletin_ids else "message"
            prompt = f"Remove this {label} from local storage?"
        else:
            if bulletin_ids and not msg_ids:
                label = f"{count} bulletins"
            elif msg_ids and not bulletin_ids:
                label = f"{count} messages"
            else:
                label = f"{count} items"
            prompt = f"Remove {label} from local storage?"

        r = QMessageBox.question(
            self, "Delete",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if r == QMessageBox.StandardButton.Yes:
            for row_id, folder in bulletin_ids:
                self.db.delete_bulletin(row_id)
            for row_id, folder in msg_ids:
                self.db.delete_message(row_id)
            self._current_row_id = None
            self.mail_view.preview_header.setText("")
            self.mail_view.preview_body.clear()
            self._refresh_folder(self._current_folder)
            self._update_folder_counts()

    # ── Terminal command (Phase 2 stub) ───────────────────────────

    # Commands that destroy mail ON THE BBS. The BBS has no undo, and for
    # anything QtC has not downloaded yet the BBS copy is the only copy.
    DESTRUCTIVE_BBS_CMDS = {"KM"}

    def _on_terminal_cmd(self, cmd: str):
        """Send a raw command through the live BBS session.

        Every terminal command funnels through here — the input box, the
        quick buttons and the test API alike — so this is the one place a
        guard has to go."""
        if not self.worker or not self.worker.session:
            self.terminal.append(
                "[Not connected — connect to a BBS first]\n", "#ff4444")
            return
        if not self._confirm_destructive(cmd):
            return
        self.worker.do_terminal_send(cmd)

    def _confirm_destructive(self, cmd: str) -> bool:
        """Ask before a command that deletes mail on the BBS.

        KM is "Kill Mine". On LinBPQ it kills the messages you have already
        READ (PY); unread mail survives (D17 on the Pi, 2026-10-01). It
        still cannot be undone from here. It sat in the Terminal
        quick-command row between RM and I, same size and colour as L and
        LM, and went out on a single click with nothing in the way
        (Bill, 2026-09-20: "It will wipe all my mail still on the BBS").

        The dialog names the consequence rather than asking a bare yes/no,
        and defaults to No."""
        verb = cmd.strip().upper().split()[0] if cmd.strip() else ""
        if verb not in self.DESTRUCTIVE_BBS_CMDS:
            return True
        bbs = self._get_active_bbs_entry().get("callsign", "the BBS")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        # Wording per Bill, 2026-10-01. D17 on the Pi showed LinBPQ's KM
        # kills only mail you have already READ (PY); unread PN survives,
        # and BPQ's own help says "KM (Kill my read messages)". The old
        # text claimed every undownloaded message would be gone.
        box.setWindowTitle("Kill your read messages on the BBS?")
        box.setText(
            f"<b>KM kills \"read\" messages in the BBS ({bbs}).</b>"
            f"<br><br>"
            f"Doing this helps speed up future connections. Your mail is "
            f"still in the QtC Inbox.<br><br>"
            f"Send <b>KM</b> to {bbs}?")
        btn_send = box.addButton("Yes — kill my read messages",
                                 QMessageBox.ButtonRole.DestructiveRole)
        btn_no   = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_no)
        box.setEscapeButton(btn_no)
        box.exec()
        if box.clickedButton() is not btn_send:      # None (Esc/X) lands here
            self.terminal.append("\n[KM cancelled — nothing sent]\n", "#ffaa55")
            self.debug_view.append("[SYS] KM cancelled by the user\n", "#ffaa55")
            return False
        self.debug_view.append(
            f"[SYS] KM CONFIRMED by the user — deleting mail on {bbs}\n",
            "#ff4444")
        return True

    # ── Settings dialog ───────────────────────────────────────────

    def _on_address_book(self):
        dlg = AddressBookDialog(self.cdb, parent=self, select_mode=False)
        dlg.exec()

    def _on_settings(self):
        old_mc = dict(self.config.get("mail_call", {}) or {})
        dlg = SettingsDialog(self.config, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.config = dlg.get_config()
            save_config(self.config)
            self._reload_bbs_combo()
            self._on_combo_changed(self.bbs_combo.currentIndex())
            mycall = self.config.get("user", {}).get("callsign", "").upper()
            self.setWindowTitle(f"QtC - {mycall}" if mycall else "QtC")
            # Apply font size immediately — no restart needed
            self._apply_font_size(self.config.get("app", {}).get("font_size", 10))
            # Push the chosen character set to a live session right away
            self._apply_text_codec(
                self.config.get("app", {}).get("text_codec", "utf-8"))
            # Reload Mail-Call config (schedule/bbs_key/enabled may have changed)
            self._mc_scheduler.refresh()
            QMessageBox.information(self, "Settings Saved",
                f"Settings saved to {_CONFIG_PATH}.\n"
                "Reconnect to apply any connection changes.")
            self._mc_check_modem_after_save(old_mc)

    # ── Mail-Call !!! scheduler handlers ──────────────────────────

    def _mc_check_modem_after_save(self, old_mc: dict):
        """Warn at once when Mail-Call was just turned on or any of its
        settings changed (BBS, schedule, times, time zone), and that BBS's
        modem program isn't running. Unrelated edits stay quiet.

        Settings OK is the moment the operator is still at the desk. A
        warning at slot time could land after they've left for the weekend
        (Bill, 2026-09-13). A slot that fires with the modem down still
        takes the normal retry path — nothing reaches the radio."""
        mc = self.config.get("mail_call", {}) or {}
        if not mc.get("enabled"):
            return
        if old_mc.get("enabled") and old_mc == mc:
            return          # nothing on the Mail-Call tab changed
        if not self.btn_connect.isEnabled():
            return          # a live session holds the modem's ports
        entry, _err = self._mc_scheduler._validate()
        if entry is None:
            return          # the Mail-Call status line already says why
        down = self._modem_not_running(entry)
        if not down:
            return
        name, where = down
        nxt = self._mc_scheduler._next_fire
        tz = " UTC" if mc.get("time_zone") == "utc" else ""
        when = f" at {nxt.strftime('%H:%M')}{tz}" if nxt else ""
        self._on_log(
            f"[SCHED] Mail-Call: {name} not answering on {where} — "
            f"warned at Settings save")
        QMessageBox.warning(self, "Mail-Call: modem not running",
            f"Mail-Call is set to connect to {entry.get('callsign', '?')} "
            f"through {name}, but nothing is answering on {where}. "
            f"{name} doesn't seem to be running.\n\n"
            f"Start {name} before the next Mail-Call{when}. If it is still "
            f"down then, Mail-Call retries a few times without "
            f"transmitting and waits for the following slot.")

    def _modem_not_running(self, entry: dict):
        """(modem name, "host:port") when the entry's modem program isn't
        listening, else None. Local TCP only — nothing is transmitted.
        Telnet has no modem to check."""
        import socket
        transport = entry.get("transport", "")
        if transport in AGW_MODEMS:
            name = AGW_MODEMS[transport]
            host = entry.get("host") or "127.0.0.1"
            port = int(entry.get("agw_port") or AGW_DEFAULT_PORT)
        elif transport in ("vara_hf", "vara_fm"):
            prefix = "hf" if transport == "vara_hf" else "fm"
            vara = self.config.get("vara", {})
            name = "VARA HF" if prefix == "hf" else "VARA FM"
            host = vara.get(f"{prefix}_host", vara.get("hf_host", "127.0.0.1"))
            port = int(vara.get(f"{prefix}_cmd_port",
                                vara.get("hf_cmd_port", 8300)))
            ctrl = self._vara_ctrl
            if (ctrl.vara_host, int(ctrl.cmd_port)) == (host, port):
                # QtC holds this port itself while idle, and VARA takes one
                # client — a second connection is refused even when it runs.
                return None if ctrl.open() else (name, f"{host}:{port}")
        else:
            return None
        try:
            with socket.create_connection((host, port), timeout=2):
                return None
        except OSError:
            return (name, f"{host}:{port}")

    # Mail-Call retry tunables. The combined budget covers both channel-busy
    # detections and VARA connect-failures — every attempt of either kind
    # burns one credit. The slot deadline is a wall-clock safety net that
    # overrides the counter if anything keeps the slot active too long
    # (e.g. ping-pong between busy and connect-fail conditions).
    MC_RETRY_SECONDS      = 180    # 3 minutes between attempts (busy or fail)
    MC_MAX_TRIES          = 5      # combined budget per slot (busy + connect-fail)
    MC_SLOT_DEADLINE_SECS = 1200   # 20-minute wall-clock cap per slot

    def _mc_handle_fire(self, entry: dict):
        """
        Triggered by MailCallScheduler when a scheduled slot is crossed.

        Stamps per-slot state (_mc_active_entry, _mc_slot_deadline) and
        delegates to _mc_attempt_vara_fire on VARA transports. The combined
        retry budget (busy + connect-fail) and the wall-clock deadline are
        enforced inside that method. Telnet has no busy/RF failure modes
        and fires directly.

        Note: sig_log lives on SessionWorker, NOT on MainWindow. We log
        directly via self._on_log(...).
        """
        if not self.btn_connect.isEnabled():
            self._on_log("[SCHED] Skipped — manual session already active")
            return

        # New slot — claim ownership and stamp the wall-clock deadline.
        # _mc_active_entry being set tells _on_error/_on_connected that
        # Mail-Call (not the user) initiated this connect attempt.
        self._mc_active_entry  = entry
        self._mc_tries_used    = 0
        self._mc_slot_deadline = (
            datetime.now() + timedelta(seconds=self.MC_SLOT_DEADLINE_SECS))
        # Survives the connect-success clear of _mc_active_entry so the
        # rest of the session (mail download, bulletin check, outbox,
        # disconnect) knows it ran unattended.
        self._mc_session_owned = True

        transport = entry.get("transport", "")
        if transport in ("vara_hf", "vara_fm", *AGW_MODEMS):
            # RF — VARA or packet: retry budget + slot deadline apply
            self._mc_attempt_vara_fire(entry)
        else:
            # Telnet — no RF retry concept, fire and let _on_connected
            # clear the slot state.
            self._mc_tries_used = 1
            self._mc_do_connect(entry)

    def _mc_attempt_vara_fire(self, entry: dict):
        """
        Make a VARA Mail-Call attempt. Order of checks:

          1. Wall-clock slot deadline — rolls over if exceeded, no matter
             what the counter says (this is the ping-pong safety net).
          2. Combined try budget (busy + connect-fail) — rolls over if
             MC_MAX_TRIES has already been spent.
          3. Channel busy (VaraControl DCD) — burns one credit, schedules
             a +MC_RETRY_SECONDS retry. RF politeness on top of VARA's own
             internal channel-clear wait.
          4. Clear — burns one credit and fires the connect. A connect-fail
             later triggers another retry via _on_error, which calls
             _mc_handle_connect_failure.

        Counter rule: every attempt of either kind (busy detection OR a
        fired connect) burns one credit. This makes it mathematically
        impossible for a busy/fail bounce to extend the slot indefinitely.
        """
        # 1. Wall-clock safety net
        if (self._mc_slot_deadline
                and datetime.now() >= self._mc_slot_deadline):
            self._on_log(
                f"[SCHED] Mail-Call: slot window "
                f"({self.MC_SLOT_DEADLINE_SECS // 60} min) expired — "
                f"rolling over to next slot")
            self._mc_end_retry()
            return

        # 2. Budget exhausted
        if self._mc_tries_used >= self.MC_MAX_TRIES:
            self._on_log(
                f"[SCHED] Mail-Call: retry budget exhausted "
                f"({self._mc_tries_used}/{self.MC_MAX_TRIES}) — "
                f"rolling over to next slot")
            self._mc_end_retry()
            return

        # 3. Channel busy — increment and schedule retry. VARA only: AGW
        #    has no BUSY report, and the packet modems carrier-sense on
        #    their own before every transmission.
        if (entry.get("transport") in ("vara_hf", "vara_fm")
                and self._vara_ctrl.is_busy()):
            self._mc_tries_used += 1
            self._on_log(
                f"[SCHED] Mail-Call: channel busy "
                f"(try {self._mc_tries_used}/{self.MC_MAX_TRIES}) — "
                f"retry in {self.MC_RETRY_SECONDS // 60} minutes")
            self._mc_schedule_retry(entry)
            return

        # 4. Clear — increment and fire
        self._mc_tries_used += 1
        if self._mc_tries_used > 1:
            self._on_log(
                f"[SCHED] Mail-Call: try {self._mc_tries_used}/"
                f"{self.MC_MAX_TRIES}, firing connect")
        self._mc_do_connect(entry)

    def _mc_schedule_retry(self, entry: dict):
        """Arm the +MC_RETRY_SECONDS countdown for the next attempt. Shared
        by the busy path (in _mc_attempt_vara_fire) and the connect-fail
        path (in _mc_handle_connect_failure) — they use the same interval
        so there's only one knob to tune."""
        self._mc_retry_pending  = True
        self._mc_retry_target   = entry
        self._mc_retry_deadline = (
            datetime.now() + timedelta(seconds=self.MC_RETRY_SECONDS))
        self._mc_scheduler.set_status_paused(True)
        if not self._mc_retry_timer.isActive():
            self._mc_retry_timer.start()
        self._mc_retry_tick()   # show countdown immediately

    def _mc_retry_tick(self):
        """1-second tick between Mail-Call attempts — update the status bar
        countdown and re-fire when the deadline hits."""
        if not self._mc_retry_pending:
            self._mc_retry_timer.stop()
            return

        # Cancel if external state makes the retry moot
        mc = self.config.get("mail_call", {}) or {}
        if not mc.get("enabled"):
            self._on_log("[SCHED] Mail-Call: cancelled retry — "
                         "feature disabled")
            self._mc_end_retry()
            return

        # btn_connect goes False both when the user clicks Connect AND when
        # VARA is recovering from our own failed attempt. _mc_active_entry
        # being set tells us the slot is still ours — only treat the disabled
        # button as a "manual session" if Mail-Call no longer owns the slot.
        if (not self.btn_connect.isEnabled()
                and self._mc_active_entry is None):
            self._on_log("[SCHED] Mail-Call: cancelled retry — "
                         "manual session active")
            self._mc_end_retry()
            return

        now = datetime.now()
        if now >= self._mc_retry_deadline:
            target = self._mc_retry_target
            self._mc_retry_pending = False
            self._mc_attempt_vara_fire(target)
        else:
            remaining = self._mc_retry_deadline - now
            secs = max(0, int(remaining.total_seconds()))
            m, s = divmod(secs, 60)
            self._mc_status_label.setText(
                f"Mail-Call: retry "
                f"{self._mc_tries_used}/{self.MC_MAX_TRIES} "
                f"in {m}:{s:02d}")
            self._mc_status_label.setVisible(True)

    def _mc_link_dropped(self) -> bool:
        """True when an error belongs to a Mail-Call session that had
        connected and has since lost its link — the unattended case."""
        if not (self._mc_session_owned or self._mc_owned_at_disconnect):
            return False
        session = self.worker.session if self.worker else None
        return not getattr(getattr(session, "transport", None),
                           "connected", False)

    def _mc_notify_link_drop(self, msg: str):
        """Record a mid-session Mail-Call link drop in 🔔 Notifications.
        No retry: the next Mail-Call runs at its usual time — see
        feedback_rf_airtime_politeness."""
        bbs = self.worker.bbs_entry.get("callsign", "?") if self.worker else "?"
        self._on_log(
            f"[SCHED] Mail-Call: link to {bbs} lost mid-session — {msg} "
            f"(see 🔔 Notifications)")
        if self._mc_drop_noted:
            return          # a second error from the same drop
        self._mc_drop_noted = True
        mycall = self.config.get("user", {}).get("callsign", "NOCALL")
        body = (f"The {datetime.now().strftime('%H:%M')} Mail-Call session "
                f"with {bbs} ended early: the link dropped while QtC was "
                f"waiting on the BBS.\n\n"
                f"  {msg}\n\n"
                f"Nothing is retried now. The next Mail-Call runs at its "
                f"usual time.\n\n"
                f"-- QtC Mail-Call\n")
        try:
            self.db.create_system_message(
                mycall, f"Mail-Call: link to {bbs} lost", body)
            self._update_folder_counts()
            if getattr(self, "_current_folder", "") == "notifications":
                self._refresh_folder("notifications")
        except Exception as e:
            self._on_log(f"[SYS] Could not add Mail-Call notification: {e}")

    def _mc_end_retry(self):
        """Tear down all Mail-Call slot/retry state and let the scheduler
        resume its normal Next-Mail-Call countdown. Called on slot success
        (in _on_connected), on roll-over (deadline or budget exhausted),
        and on cancellation (feature disabled, manual session)."""
        # If we never got connected (i.e. abandoning the slot), clear the
        # session-owned flag too — there's no _on_disconnected coming to
        # do it. When called from _on_connected we are logged in, so the
        # flag stays set for the rest of the session.
        # This used to test btn_connect.isEnabled(), but _on_error greys
        # Connect for the modem recovery BEFORE the connect-fail roll-over
        # runs, so after an exhausted slot the flag stayed True and the
        # next MANUAL connect ran as Mail-Call: autopilot even from
        # Terminal view, outbox sent and bulletins taken unasked
        # (H3, Direwolf, radio off, 2026-10-01).
        if not self._logged_in:
            self._mc_session_owned = False
        self._mc_active_entry   = None
        self._mc_tries_used     = 0
        self._mc_slot_deadline  = None
        self._mc_retry_pending  = False
        self._mc_retry_target   = None
        self._mc_retry_deadline = None
        self._mc_retry_timer.stop()
        self._mc_scheduler.set_status_paused(False)
        self._mc_scheduler.refresh()

    def _mc_handle_connect_failure(self, msg: str):
        """Called from _on_error when Mail-Call owns the failed connect.

        The try counter was already incremented inside _mc_attempt_vara_fire
        before the connect was fired, so it already reflects this attempt.
        Check the deadline and budget, then either schedule the next retry
        or roll over to the next slot.
        """
        entry = self._mc_active_entry
        if entry is None:
            return   # defensive — shouldn't be called outside an MC slot

        # Always log the failure with the current counter
        self._on_log(
            f"[SCHED] Mail-Call: connect failed "
            f"(try {self._mc_tries_used}/{self.MC_MAX_TRIES}) — {msg}")

        # Wall-clock deadline beats counter
        if (self._mc_slot_deadline
                and datetime.now() >= self._mc_slot_deadline):
            self._on_log(
                f"[SCHED] Mail-Call: slot window expired after connect-fail "
                f"— rolling over to next slot")
            self._mc_end_retry()
            return

        # Budget exhausted
        if self._mc_tries_used >= self.MC_MAX_TRIES:
            self._on_log(
                f"[SCHED] Mail-Call: retry budget exhausted after connect-fail "
                f"({self._mc_tries_used}/{self.MC_MAX_TRIES}) — "
                f"rolling over to next slot")
            self._mc_end_retry()
            return

        # Schedule next attempt
        self._on_log(
            f"[SCHED] Mail-Call: retrying in "
            f"{self.MC_RETRY_SECONDS // 60} minutes "
            f"(next try {self._mc_tries_used + 1}/{self.MC_MAX_TRIES})")
        self._mc_schedule_retry(entry)

    def _mc_do_connect(self, entry: dict):
        """Final step — select the right BBS-combo entry, fire Connect."""
        want = (entry.get("callsign", "").upper(),
                entry.get("transport", ""),
                entry.get("host", ""))
        matched_idx = -1
        for i in range(self.bbs_combo.count()):
            e = self.bbs_combo.itemData(i) or {}
            if (e.get("callsign", "").upper(),
                e.get("transport", ""),
                e.get("host", "")) == want:
                matched_idx = i
                break
        if matched_idx < 0:
            self._on_log("[SCHED] Skipped — chosen BBS no longer in list")
            return

        self.bbs_combo.setCurrentIndex(matched_idx)
        self._on_log(
            f"[SCHED] Mail-Call firing → {entry.get('callsign','?')} "
            f"via {entry.get('transport','?')}")
        self._on_connect()

    def _mc_update_status(self, text: str):
        """Show/hide and update the Mail-Call status label in the status bar."""
        self._mc_status_label.setText(text)
        self._mc_status_label.setVisible(bool(text))

    def _mc_handle_skipped(self, reason: str):
        """Scheduler reported a skipped fire — log it to the Debug view."""
        self._on_log(f"[SCHED] Skipped — {reason}")

    def _on_about(self):
        QMessageBox.about(self, "About QtC",
            f"<b>QtC</b> v{APP_VERSION}<br><br>"
            "A modern BBS client for LinBPQ/BPQ32 nodes<br>"
            "via VARA HF, VARA FM, AX.25 packet, and Telnet.<br><br>"
            "Built with Python + PyQt6.")

    def _on_shortcuts_help(self):
        """Keyboard cheat sheet (issue #3). Everything here is also a button
        or menu item — these keys just make QtC fast to drive without a
        mouse. Single-letter keys act on the message list when it has focus;
        the Ctrl/F-key bindings work anywhere in the main window."""
        rows = [
            ("Navigation", ""),
            ("Tab / Shift+Tab",  "Cycle: folders → messages → body"),
            ("↑ / ↓",            "Folders/list: select · body: scroll"),
            ("Enter",            "Jump into the reading pane"),
            ("Esc",              "Back to the list from the body"),
            ("Views", ""),
            ("F2 / F3 / F4",   "Mail / Terminal / Debug view"),
            ("Message list (when focused)", ""),
            ("N",                "New message"),
            ("R",                "Reply"),
            ("D  /  Del",        "Delete"),
            ("F",                "Search"),
            ("M",                "Mark all read"),
            ("Anywhere in the window", ""),
            ("Ctrl+N",           "New message"),
            ("Ctrl+R",           "Reply"),
            ("Ctrl+F",           "Search"),
            ("Ctrl+Shift+M",     "Mark all read"),
            ("Ctrl+Shift+O",     "Send outbox now"),
            ("F1",               "This cheat sheet"),
            ("Ctrl+Q",           "Quit"),
        ]
        lines = []
        for key, desc in rows:
            if desc == "":
                lines.append(f"\n  {key}")
                lines.append("  " + "─" * len(key))
            else:
                lines.append(f"    {key:<16}{desc}")
        body = "\n".join(lines).lstrip("\n")

        dlg = QDialog(self)
        dlg.setWindowTitle("QtC — Keyboard Shortcuts")
        dlg.setMinimumWidth(440)
        lay = QVBoxLayout(dlg)
        view = QTextEdit()
        view.setReadOnly(True)
        # Match the app's configured message font size so readers who bump
        # the font for their eyes get a bigger cheat sheet too. NoWrap keeps
        # the key/description columns aligned at larger sizes (a horizontal
        # scrollbar appears instead of breaking the layout).
        font_size = int(self.config.get("app", {}).get("font_size", 10))
        view.setFont(QFont("Courier New", font_size))
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(body)
        lay.addWidget(view)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)

        # Open large enough that the whole sheet fits at the current font with
        # no scrollbars — comfortably handles the bigger sizes (≥14 pt) that
        # readers bump to for their eyes. Measured from the font itself so it
        # stays right at any size; the floor matches the old compact size.
        fm = view.fontMetrics()
        text_w = max(fm.horizontalAdvance(ln) for ln in body.split("\n"))
        n_lines = body.count("\n") + 1
        w = text_w + 80                              # frame + margins + scrollbar
        h = fm.lineSpacing() * (n_lines + 2) + 90    # + button box + margins
        dlg.resize(max(460, w), max(540, h))
        dlg.exec()

    # ── Helpers ───────────────────────────────────────────────────

    def _apply_font_size(self, size: int):
        """Apply font size to all message text panes."""
        font = QFont("Courier New", size)
        self.mail_view.preview_body.setFont(font)
        self.terminal.output.setFont(font)
        self.terminal.input_line.setFont(font)
        self.debug_view.output.setFont(font)

    def _update_folder_counts(self):
        unread  = self.db.get_unread_count()
        notifs  = self.db.get_notification_unread_count()
        pending = len(self.db.get_pending_outbox())
        sent    = len(self.db.get_sent())
        self.mail_view.update_folder_counts(unread, pending, sent, notifs)
        # Update bulletin category subfolders
        cats = self.db.get_bulletin_categories()
        self.mail_view.update_bulletin_categories(cats)

    def _set_status(self, text: str,
                    connected: bool = False,
                    connecting: bool = False):
        # Elide long text with … so it doesn't get hard-clipped in the toolbar
        fm = self.conn_label.fontMetrics()
        elided = fm.elidedText(
            f"  {text}", Qt.TextElideMode.ElideRight,
            self.conn_label.maximumWidth() - 8)
        self.conn_label.setText(elided)
        # Status bar always shows full text
        if not self._prog_bar.isVisible():
            self.status_label.setText(text)
        if connecting:
            color = "#ffaa00"
        elif connected:
            color = "#00cc44"
        else:
            color = "#888888"
        self.conn_light.setStyleSheet(f"color:{color}; font-size:18px;")

    # How long a close may wait for a clean hang-up before quitting anyway.
    # AGW waits up to HANGUP_WAIT_SECS (10) for the BBS's DISC; VARA HF's
    # own DISCONNECT handshake can take 10-20 s.
    CLOSE_DISCONNECT_CAP_MS = 30000

    def closeEvent(self, event):
        # Closing while linked: hang up FIRST, then close. Waiting only 2 s
        # here let QtC exit with the link still up — D9, 1200 baud,
        # 2026-10-01: `b` went out, the BBS answered with DISC, but QtC had
        # already dropped its AGW socket, so Direwolf never sent the UA
        # and the BBS kept repeating DISC on the air until it gave up.
        # Closing a second time quits at once.
        if (self.worker and self.worker.session
                and not getattr(self, "_close_after_disconnect", False)):
            self._close_after_disconnect = True
            event.ignore()
            self._set_status("Disconnecting before exit…", connecting=True)
            note = ("[SYS] Closing — disconnecting from the BBS first. "
                    "Close again to quit now.\n")
            self.terminal.append(note, "#888888")
            self.debug_view.append(note, "#888888")
            self.worker.sig_disconnected.connect(
                lambda: QTimer.singleShot(0, self.close))
            self.worker.do_disconnect()
            QTimer.singleShot(self.CLOSE_DISCONNECT_CAP_MS, self.close)
            return
        # Stop the Mail-Call scheduler so its QTimer doesn't keep ticking
        # during the shutdown teardown.
        if hasattr(self, "_mc_scheduler"):
            self._mc_scheduler.stop()
        # Clean up worker whether connected or mid-connect
        if self.worker:
            if self.worker.session:
                self.worker.do_disconnect()
            self.worker.quit()
            self.worker.wait(2000)
        self._vara_ctrl.close()
        # MessageDatabase uses per-operation connections — no close() needed
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _apply_dark_palette(app):
    """Apply a dark Fusion palette to the application."""
    from PyQt6.QtGui import QPalette, QColor
    palette = QPalette()
    # Base colors
    dark    = QColor(45,  45,  45)
    darker  = QColor(30,  30,  30)
    darkest = QColor(20,  20,  20)
    mid     = QColor(60,  60,  60)
    light   = QColor(220, 220, 220)
    dimmed  = QColor(140, 140, 140)
    accent  = QColor(0,   160, 80)   # green accent for highlights

    palette.setColor(QPalette.ColorRole.Window,          dark)
    palette.setColor(QPalette.ColorRole.WindowText,      light)
    palette.setColor(QPalette.ColorRole.Base,            darker)
    palette.setColor(QPalette.ColorRole.AlternateBase,   darkest)
    palette.setColor(QPalette.ColorRole.ToolTipBase,     dark)
    palette.setColor(QPalette.ColorRole.ToolTipText,     light)
    palette.setColor(QPalette.ColorRole.Text,            light)
    palette.setColor(QPalette.ColorRole.Button,          mid)
    palette.setColor(QPalette.ColorRole.ButtonText,      light)
    palette.setColor(QPalette.ColorRole.BrightText,      QColor(255, 80, 80))
    palette.setColor(QPalette.ColorRole.Link,            accent)
    palette.setColor(QPalette.ColorRole.Highlight,       accent)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    # Inactive selection — a pane that doesn't have keyboard focus dims its
    # selected row to a neutral gray, so it's clear which of the three mail
    # panes is active. (setColor above paints every color group the same
    # green, which is why an unfocused Inbox row otherwise stayed bright.)
    palette.setColor(QPalette.ColorGroup.Inactive,
                     QPalette.ColorRole.Highlight,       QColor(70, 70, 70))
    palette.setColor(QPalette.ColorGroup.Inactive,
                     QPalette.ColorRole.HighlightedText, QColor(205, 205, 205))
    palette.setColor(QPalette.ColorRole.Mid,             mid)
    palette.setColor(QPalette.ColorRole.Shadow,          darkest)
    palette.setColor(QPalette.ColorRole.Dark,            darkest)
    # Disabled state
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.WindowText, dimmed)
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.Text,        dimmed)
    palette.setColor(QPalette.ColorGroup.Disabled,
                     QPalette.ColorRole.ButtonText,  dimmed)
    app.setPalette(palette)


def _take_test_api_flag(argv):
    """--test-api[=PORT] — development/QA only. Pulled out of argv before
    Qt sees it. Returns the port, or None when the flag is absent.

    Off unless asked for, and the socket binds to 127.0.0.1 only. See
    qtc_test_api.py: it presses the same buttons a person would, and some
    of those key the transmitter, so the control operator has to be there.
    """
    port = None
    for arg in list(argv[1:]):
        if arg == "--test-api" or arg.startswith("--test-api="):
            argv.remove(arg)
            _, _, val = arg.partition("=")
            port = int(val) if val.isdigit() else 8787
    return port


def main():
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland*=false")
    test_api_port = _take_test_api_flag(sys.argv)
    app = QApplication(sys.argv)
    app.setApplicationName("QtC")
    app.setStyle("Fusion")

    # Apply dark mode palette if enabled in config
    try:
        _cfg = load_config()
        if _cfg.get("app", {}).get("dark_mode", False):
            _apply_dark_palette(app)
    except Exception:
        pass   # if config can't be read yet, default light mode is fine

    # Resolve resource paths — works both as a script and as a frozen exe.
    # PyInstaller 6 one-folder layout puts datas in _internal/ (sys._MEIPASS),
    # not next to the exe — fall through to that if present.
    if getattr(sys, 'frozen', False):
        _base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        _base = os.path.dirname(os.path.abspath(__file__))

    # Set application icon
    _icon_path = os.path.join(_base, "qtc_icon.svg")
    if os.path.exists(_icon_path):
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(_icon_path))

    # Splash screen — shown while MainWindow constructs, then dismissed as
    # soon as the main window paints. No forced minimum hold; the splash
    # lives exactly as long as MainWindow takes to build. On Windows that's
    # ~5s of continuous splash; on Linux source-runs it's a brief flash,
    # which is acceptable for the dev path. PNG is generated by
    # make_splash.py at install/build time; if missing we just skip.
    splash = None
    _splash_path = os.path.join(_base, "qtc_splash.png")
    if os.path.exists(_splash_path):
        pm = QPixmap(_splash_path)
        if not pm.isNull():
            splash = QSplashScreen(pm, Qt.WindowType.WindowStaysOnTopHint)
            splash.show()
            app.processEvents()

    win = MainWindow()
    win.show()
    if splash is not None:
        splash.finish(win)

    if test_api_port is not None:
        try:
            import qtc_test_api
        except ImportError:
            # Release builds leave the QA control socket out (the Windows
            # exe's QtC.spec excludes it; the tarball never carries it).
            print("\n*** --test-api is not available in this build — it is "
                  "for development from a source checkout.\n", flush=True)
            return 2
        try:
            qtc_test_api.install(win, test_api_port)
        except RuntimeError as e:
            # Asked for the API and could not have it — say why and stop,
            # rather than running on silently without the thing that was
            # requested, or dying on a raw traceback.
            print(f"\n*** {e}\n", flush=True)
            return 2
        print(f"\n*** QtC TEST API on 127.0.0.1:{test_api_port} ***\n"
              f"    Development/QA control socket. Anything that reaches the\n"
              f"    BBS goes out over your radio: connect, type and\n"
              f"    click send_receive all key the transmitter. Stay at the\n"
              f"    set. Every command is written into the Debug log.\n",
              flush=True)

    # First-run check — if no real callsign set, open settings immediately
    callsign = win.config.get("user", {}).get("callsign", "").strip().upper()
    if not callsign or callsign in ("NOCALL", "N0CALL", "CALL", "MYCALL"):
        QMessageBox.information(
            win, "Welcome to QtC",
            "<b>Welcome to QtC!</b><br><br>"
            "Before connecting to a BBS, please set up your station:<br><br>"
            "<b>My Station</b> — your callsign, name, and home BBS<br>"
            "<b>BBS List</b> — add at least one BBS to connect to<br><br>"
            "The Settings window will open now."
        )
        win._on_settings()

    sys.exit(app.exec())

if __name__ == "__main__":
    # Propagate main()'s status. It returns non-zero when a requested
    # --test-api port could not be opened, and a test run needs to be able
    # to tell that apart from a clean exit.
    sys.exit(main())
