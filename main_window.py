# QtC v0.9.11-beta — main_window.py  (built 2026-03-25)
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
APP_VERSION = "0.9.11-beta"  # keep in sync with header comment
"""
QtC — Main Window (PyQt6)
v0.2 — Quick-connect bar, auto-download, terminal swap button
"""
import sys, json, os, copy
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QToolBar, QStatusBar,
    QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    QTextEdit, QLabel, QPushButton, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox,
    QFrame, QHeaderView, QAbstractItemView, QCheckBox,
    QStackedWidget, QInputDialog, QSpacerItem, QSizePolicy,
    QTabWidget, QListWidget, QListWidgetItem, QCompleter,
    QProgressBar, QSpinBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCursor, QPalette, QAction

from transport import TelnetTransport
from bbs_session import BBSSession, BBSMailSummary

from database import MessageDatabase, ContactsDB


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
        "app": {"auto_check_mail": True}
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
    sig_first_visit   = pyqtSignal(str)   # emits bbs callsign — GUI shows dialog
    sig_progress      = pyqtSignal(str, int, int, str)  # op, current, total, detail
    sig_bulletin_check = pyqtSignal(object)   # {category: [BBSMessage]} — show dialog
    sig_bulletin_done  = pyqtSignal(int)       # count of bulletins downloaded

    def __init__(self, config, bbs_entry, db):
        super().__init__()
        self.config    = config
        self.bbs_entry = bbs_entry
        self.db        = db
        self.session   = None
        self._task     = None
        import queue as _queue
        self._queue    = _queue.Queue()

    def _enqueue(self, task):
        """Add task to queue and start thread if not running."""
        self._queue.put(task)
        if not self.isRunning():
            self.start()

    def do_connect_and_check(self):
        self._task = ("connect_check",)
        if not self.isRunning(): self.start()

    def do_download(self, messages):
        self._task = ("download", messages)
        if not self.isRunning(): self.start()

    def do_send(self, to_call, subject, body, msg_type, at_bbs):
        self._enqueue(("send", to_call, subject, body, msg_type, at_bbs))

    def do_disconnect(self):
        self._task = ("disconnect",)
        if not self.isRunning(): self.start()

    def do_terminal_send(self, cmd: str):
        self._task = ("terminal_send", cmd)
        if not self.isRunning(): self.start()

    def do_mail_check(self, new_only: bool = True):
        self._task = ("mail_check", new_only)
        if not self.isRunning(): self.start()

    def do_check_bulletins(self, subscriptions: list):
        self._task = ("check_bulletins", subscriptions)
        if not self.isRunning(): self.start()

    def do_download_bulletins(self, messages_by_cat: dict):
        """messages_by_cat = {category: [BBSMessage, ...]}"""
        self._task = ("download_bulletins", messages_by_cat)
        if not self.isRunning(): self.start()

    def run(self):
        try:
            t = self._task
            self._task = None   # clear immediately so re-entry never re-runs connect
            if t is None:
                pass  # nothing to do — was started only for queued sends
            elif t[0] == "connect_check": self._run_connect_and_check()
            elif t[0] == "download":      self._run_download(t[1])
            elif t[0] == "disconnect":    self._run_disconnect()
            elif t[0] == "mail_check":    self._run_mail_check(t[1])
            elif t[0] == "terminal_send": self._run_terminal_send(t[1])
            elif t[0] == "send":          self._run_send(*t[1:])
            elif t[0] == "check_bulletins":    self._run_check_bulletins(t[1])
            elif t[0] == "download_bulletins": self._run_download_bulletins(t[1])
            # Drain any queued send tasks
            import queue as _queue
            while True:
                try:
                    qt = self._queue.get_nowait()
                    if qt[0] == "send":
                        self._run_send(*qt[1:])
                except _queue.Empty:
                    break
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
            t = VaraTransport(
                vara_host=vara_host,
                cmd_port=vara_cmd_port,
                data_port=vara_data_port,
                mycall=mycall,
                target_call=target_call,
                timeout=90,
                bandwidth=e.get("bw", "500"),
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
                t.ptt = ptt

            user_info = {k: self.config["user"].get(k, "")
                         for k in ("name", "qth", "zip", "home_bbs")}
            s = BBSSession(t, mycall=mycall, user_info=user_info)
            s._rf_connected_cb = lambda: self.sig_rf_connected.emit()

        else:
            raise ValueError(f"Unknown transport: {transport}")

        s._log = lambda d, txt: self.sig_log.emit(f"[{d}] {txt.strip()}")
        return s

    def _run_connect_and_check(self):
        self.session = self._make_session()
        if not self.session.connect_and_login():
            self.sig_error.emit("Login failed — check callsign and BBS address.")
            return
        self.sig_connected.emit()
        # Always emit first_visit — whether new user or returning.
        # Registration is handled automatically in connect_and_login.
        # The download choice (All/New/Skip) is always the user's decision.
        if self.session.new_user:
            self.sig_log.emit("[SYS] New user registration completed.")
        self.sig_first_visit.emit(self.bbs_entry.get("callsign", ""))

    def _run_mail_check(self, new_only: bool):
        # Disable data streaming while _expect handles the LM response
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        self.sig_mail_summary.emit(self.session.check_mail(new_only=new_only))

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
        for i, msg in enumerate(messages, 1):
            if not self.db.message_exists(msg.msg_number, bbs_id):
                detail = f"msg #{msg.msg_number} · ~{msg.size} bytes"
                # Emit i-1 before download so bar shows progress correctly
                # e.g. 1 of 3 shows 0% before starting, 33% after first done
                self.sig_progress.emit("downloading", i - 1, total, detail)
                self.sig_log.emit(
                    f"[SYS] Downloading message {i} of {total} "
                    f"(#{msg.msg_number}, ~{msg.size} bytes)")
                msg.body = self.session.download_message(
                    msg.msg_number, size_hint=msg.size)
                msg.downloaded = True
                self.db.save_to_inbox(msg, bbs_id)
                count += 1
        self.sig_progress.emit("done", 0, 0, "")
        self.sig_download_done.emit(count)

    def _run_send(self, to_call, subject, body, msg_type, at_bbs):
        # Progress emitted by caller (_on_send_outbox) before queuing
        ok = self.session.send_message(to_call, subject, body, msg_type, at_bbs)
        self.sig_send_result.emit(ok, to_call)

    def _run_disconnect(self):
        if self.session:
            self.session.logout()
            self.session = None
        self.sig_disconnected.emit()

    def _run_check_bulletins(self, subscriptions: list):
        """Run L> for each subscription, emit results for GUI to show dialog."""
        import time as _time
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        # Flush stale bytes and settle before sending L> commands
        self.session.transport.flush_input()
        _time.sleep(0.5)
        results = self.session.check_bulletins(subscriptions)
        # Terminal mode stays OFF — download_bulletins will re-enable it after

        # Build bbs_id for tombstone/exists checks
        bbs_id = (f"{self.config.get('user',{}).get('callsign','NOCALL').upper()}"
                  f"@{self.bbs_entry['callsign']}")

        # ── First bulletin connect — auto-tombstone all but 2 newest ──────
        # Detect first time bulletins have been checked on this BBS by looking
        # for a "bulletins_seen" key in visited_bbs. If absent, tombstone all
        # but the 2 most recent per category so the user doesn't see a huge
        # backlog on first connect.
        mycall    = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        visit_key = f"{mycall}@{self.bbs_entry['callsign']}"
        bull_key  = f"bulletins_seen@{self.bbs_entry['callsign']}"
        visited   = self.config.get("visited_bbs", {})

        if bull_key not in visited:
            # First time — tombstone everything except 2 newest per category
            for cat, msgs in results.items():
                # msgs are already newest-first from L> (descending)
                to_keep     = msgs[:2]
                to_tombstone = msgs[2:]
                if to_tombstone:
                    self.db.add_bulletin_tombstones_batch(to_tombstone, bbs_id)
                    self.sig_log.emit(
                        f"[SYS] First bulletin connect — tombstoned "
                        f"{len(to_tombstone)} old {cat} bulletins, "
                        f"keeping {len(to_keep)} newest")
            # Mark bulletins as seen so we never do this again
            self.config.setdefault("visited_bbs", {})[bull_key] = True
            save_config(self.config)

        # Filter out already downloaded and tombstoned
        filtered = {}
        for cat, msgs in results.items():
            new_msgs = [m for m in msgs
                        if not self.db.bulletin_exists(m.msg_number, bbs_id)
                        and not self.db.bulletin_tombstone_exists(
                            m.msg_number, bbs_id)]
            if new_msgs:
                filtered[cat] = new_msgs
        self.sig_bulletin_check.emit(filtered)

    def _run_download_bulletins(self, messages_by_cat: dict):
        """Download selected bulletins and save to database."""
        import time as _time
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(False)
        self.session.transport.flush_input()
        bbs_id = (f"{self.config.get('user',{}).get('callsign','NOCALL').upper()}"
                  f"@{self.bbs_entry['callsign']}")
        count = 0
        total = sum(len(msgs) for msgs in messages_by_cat.values())
        i = 0
        for cat, msgs in messages_by_cat.items():
            for msg in msgs:
                i += 1
                detail = f"{cat} #{msg.msg_number} · ~{msg.size} bytes"
                self.sig_progress.emit("downloading", i - 1, total, detail)
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
        if hasattr(self.session.transport, "set_terminal_mode"):
            self.session.transport.set_terminal_mode(True)
        self.sig_progress.emit("done", 0, 0, "")
        self.sig_bulletin_done.emit(count)


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
        self.setMinimumSize(560, 480)
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
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Call", "Name", "City / State", "Home BBS", "Send Mode", ""])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 110)
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
            mode = "Immediate" if c.get("send_now", 1) else "Home BBS only"
            self.table.setItem(r, 4, QTableWidgetItem(mode))
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
            self.table.setCellWidget(r, 5, cell)
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
        self.chk_send   = QCheckBox("Send immediately on next connection to any BBS")
        self.chk_send.setChecked(bool(c.get("send_now", 1)))

        self.e_call.setPlaceholderText("e.g. KD8NOA")
        self.e_name.setPlaceholderText("e.g. Bob Novak")
        self.e_qth.setPlaceholderText("e.g. Flint, MI")
        self.e_home_bbs.setPlaceholderText("e.g. KD8NOA.MI.USA.NOAM")

        if contact:
            self.e_call.setReadOnly(True)   # can't change callsign on edit

        form.addRow("Callsign:", self.e_call)
        form.addRow("Name:",     self.e_name)
        form.addRow("City/St:",  self.e_qth)
        form.addRow("Home BBS:", self.e_home_bbs)
        form.addRow("",          self.chk_send)
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
            "send_now": self.chk_send.isChecked(),
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
            "e.g. KD8NOA.MI.USA.NOAM  (leave blank if unknown)")
        form.addRow("@ Home BBS:",  self.at_bbs_edit)
        form.addRow("Subject:",     self.subject_edit)
        form.addRow("Type:",        self.type_combo)

        self.chk_send_now = QCheckBox(
            "Send immediately on next connection to any BBS")
        self.chk_send_now.setChecked(True)
        form.addRow("Send mode:", self.chk_send_now)
        self.at_bbs_edit.textChanged.connect(self._on_at_bbs_changed)

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
        self.chk_send_now.setChecked(bool(c.get("send_now", 1)))
        self.save_link.setVisible(False)
        self.subject_edit.setFocus()

    def _on_to_changed(self, text):
        text = text.strip().upper()
        if self.cdb and text:
            contact = self.cdb.get_by_callsign(text)
            if contact:
                self.at_bbs_edit.setText(contact.get("home_bbs") or "")
                self.chk_send_now.setChecked(bool(contact.get("send_now", 1)))
                self.save_link.setVisible(False)
            else:
                self.save_link.setVisible(True)
        else:
            self.save_link.setVisible(False)

    def _on_at_bbs_changed(self, text):
        has_bbs = bool(text.strip())
        self.chk_send_now.setEnabled(has_bbs)
        if not has_bbs:
            self.chk_send_now.setChecked(True)

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
                     "home_bbs": self.at_bbs_edit.text().strip().upper(),
                     "send_now": self.chk_send_now.isChecked()},
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
            "send_now": self.chk_send_now.isChecked(),
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

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # Header bar
        hdr = QHBoxLayout()
        hdr_label = QLabel("📟  Terminal — Raw BBS Session")
        hdr_label.setStyleSheet("font-weight:bold; font-size:13px;")
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFixedWidth(60)
        self.clear_btn.clicked.connect(self._clear)
        hdr.addWidget(hdr_label)
        hdr.addStretch()
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
        send_btn.clicked.connect(self._send)

        il.addWidget(prompt)
        il.addWidget(self.input_line)
        il.addWidget(send_btn)
        layout.addLayout(il)

        # Quick-command buttons
        ql = QHBoxLayout()
        ql.addWidget(QLabel("Quick:"))
        for label, cmd in [
            ("L",     "L"),
            ("LM",    "LM"),
            ("LL 20", "LL 20"),
            ("RM",    "RM"),
            ("KM",    "KM"),
            ("I",     "I"),
            ("?",     "?"),
            ("B",     "B"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(26)
            b.setFixedWidth(54)
            b.clicked.connect(lambda _, c=cmd: self._quick(c))
            ql.addWidget(b)
        ql.addStretch()
        layout.addLayout(ql)

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
        cmd = self.input_line.text().strip()
        if not cmd:
            return
        self.input_line.clear()
        self.sig_send_cmd.emit(cmd)

    def _quick(self, cmd: str):
        self.input_line.setText(cmd)
        self._send()


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
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFixedWidth(60)
        self.clear_btn.clicked.connect(self._clear)
        hdr.addWidget(hdr_label)
        hdr.addStretch()
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
            ["All folders", "Inbox", "Outbox", "Sent", "Bulletins"])
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
        self.btn_send_outbox = QPushButton("📤  Send Outbox Now")

        self.btn_reply.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_send_outbox.setEnabled(False)

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

    def _folder_changed(self, current, _):
        if current is None:
            return
        if   current is self._fi: self.sig_folder_changed.emit("inbox")
        elif current is self._fo: self.sig_folder_changed.emit("outbox")
        elif current is self._fs: self.sig_folder_changed.emit("sent")
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
            "All folders": ["inbox", "outbox", "sent", "bulletins"],
            "Inbox":       ["inbox"],
            "Outbox":      ["outbox"],
            "Sent":        ["sent"],
            "Bulletins":   ["bulletins"],
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
        return "inbox"

    # ── Public methods called by MainWindow ────────────────────

    def update_folder_counts(self, unread: int, pending: int, sent: int = 0):
        self._fi.setText(0, f"📥  Inbox ({unread} new)" if unread else "📥  Inbox")
        self._fo.setText(0, f"📤  Outbox ({pending})"   if pending else "📤  Outbox")
        self._fs.setText(0, f"📨  Sent ({sent})"        if sent    else "📨  Sent")

    def load_table(self, rows, folder: str):
        """Populate the message table for the given folder."""
        self.msg_table.setRowCount(0)
        self.preview_header.setText("")
        self.preview_body.clear()
        self.btn_reply.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_mark_all_read.setVisible(folder == "inbox")
        is_bulletin = folder == "bulletins" or folder.startswith("bulletin:")
        self.btn_new.setVisible(not is_bulletin)
        self.btn_reply.setVisible(not is_bulletin)
        self.btn_delete.setVisible(True)

        if folder == "outbox":
            self._fill_outbox(rows)
        elif is_bulletin:
            self._fill_bulletins(rows, folder=folder)
        else:
            self._fill_inbox_sent(rows, show_from=(folder == "inbox"))

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

    def _fill_inbox_sent(self, rows, show_from: bool):
        self.msg_table.setHorizontalHeaderLabels(
            ["", "From" if show_from else "To", "Subject", "Date", "Size"])
        for rd in rows:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            unread = not rd.get("read", 0)
            font = QFont(); font.setBold(unread)

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
        self.msg_table.setHorizontalHeaderLabels(
            ["", "To", "Subject", "Queued", "Status"])
        for rd in rows:
            r = self.msg_table.rowCount()
            self.msg_table.insertRow(r)
            dot = QTableWidgetItem("📤")
            dot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.msg_table.setItem(r, 0, dot)
            for c, v in enumerate(
                    [rd["to_call"], rd["subject"] or "",
                     (rd["created_at"] or "")[:16], "Pending"], start=1):
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
        """Remove bold and unread dot from a table row."""
        dot = self.msg_table.item(row, 0)
        if dot:
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
        self.setMinimumSize(580, 520)
        self.resize(620, 560)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_user_tab(),      "👤  My Station")
        tabs.addTab(self._build_bbs_tab(),       "📡  BBS List")
        tabs.addTab(self._build_ptt_tab(),       "🎙️  PTT")
        tabs.addTab(self._build_bulletins_tab(), "📋  Bulletins")
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

        self.e_callsign.setPlaceholderText("e.g. KC9MTP")
        self.e_name.setPlaceholderText("e.g. Bill  (sent on first BBS registration)")
        self.e_qth.setPlaceholderText("e.g. Valparaiso IN  (optional)")
        self.e_zip.setPlaceholderText("e.g. 46383  (optional)")
        self.e_home_bbs.setPlaceholderText("e.g. KC9MTP-1  (important for mail routing)")
        self.e_telnet_user.setPlaceholderText("e.g. kc9mtp  (lowercase, case-sensitive)")
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

    def _build_bbs_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)

        # Table
        self.bbs_table = QTableWidget(0, 5)
        self.bbs_table.setHorizontalHeaderLabels(
            ["Name", "Callsign", "Host", "Port", "Transport"])
        self.bbs_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.bbs_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.bbs_table.setColumnWidth(1, 90)
        self.bbs_table.setColumnWidth(3, 58)
        self.bbs_table.setColumnWidth(4, 72)
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
        self.bbs_table.setRowCount(0)
        for e in self._cfg.get("bbs_list", []):
            r = self.bbs_table.rowCount()
            self.bbs_table.insertRow(r)
            for c, v in enumerate([
                e.get("name", ""),
                e.get("callsign", ""),
                e.get("host", ""),
                str(e.get("telnet_port", "")),
                e.get("transport", "telnet"),
            ]):
                self.bbs_table.setItem(r, c, QTableWidgetItem(v))

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
        row = self.bbs_table.currentRow()
        if row < 0:
            return
        entry = self._cfg["bbs_list"][row]
        dlg = _BBSEntryDialog(entry=entry, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._cfg["bbs_list"][row] = dlg.get_entry()
            self._reload_bbs_table()

    def _bbs_del(self):
        row = self.bbs_table.currentRow()
        if row < 0:
            return
        name = self._cfg["bbs_list"][row].get("callsign", "?")
        r = QMessageBox.question(self, "Remove BBS",
            f"Remove {name} from your BBS list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            del self._cfg["bbs_list"][row]
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
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        b = self._cfg.get("bulletins", {})

        self.chk_bull_auto = QCheckBox(
            "Check for new bulletins on connect")
        self.chk_bull_auto.setChecked(bool(b.get("check_on_connect", False)))
        outer.addWidget(self.chk_bull_auto)

        note = QLabel(
            "<i>Enter one category per line (e.g. SITREP, EWN, WX).<br>"
            "QtC will run L&gt; CATEGORY for each subscription after "
            "personal mail is checked.<br>"
            "Category names are not case-sensitive.</i>")
        note.setStyleSheet(f"color: {self._note_color}; font-size:11px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        # Home BBS warning box
        warn = QLabel(
            "<b>Home BBS only</b><br>"
            "Bulletin download only runs when connected to your Home BBS "
            "(set in My Station). This prevents re-downloading the same "
            "bulletins when visiting other nodes, since BBS-to-BBS duplicate "
            "checking (BID) is not available in this version.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background: #FAEEDA; border-left: 3px solid #BA7517; "
            "color: #633806; font-size: 11px; padding: 8px 12px;")
        outer.addWidget(warn)

        outer.addWidget(QLabel("Subscriptions (one category per line):"))
        self.bull_list = QTextEdit()
        self.bull_list.setFont(QFont("Courier New", 10))
        self.bull_list.setFixedHeight(140)
        subs = b.get("subscriptions", [])
        self.bull_list.setPlainText("\n".join(subs))
        outer.addWidget(self.bull_list)

        outer.addStretch()
        return w

    def _build_app_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(18, 18, 18, 18)
        form.setVerticalSpacing(12)

        a = self._cfg.get("app", {})

        self.chk_auto_dl = QCheckBox("Auto-download new personal mail on connect")
        self.chk_auto_dl.setChecked(bool(a.get("auto_check_mail", True)))

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
            "de KC9MTP>")
        self.spin_font.valueChanged.connect(
            lambda v: self._font_preview.setFont(QFont("Courier New", v)))

        self.e_data_dir = QLineEdit(a.get("data_dir", "data"))
        self.e_data_dir.setPlaceholderText("data")

        self.e_max_size = QLineEdit(str(a.get("max_message_size_kb", 50)))
        self.e_max_size.setFixedWidth(70)

        form.addRow("", self.chk_auto_dl)
        form.addRow("", self.chk_dark_mode)
        form.addRow("Message font size:", font_row)
        form.addRow("", self._font_preview)
        form.addRow("Data directory:", self.e_data_dir)
        form.addRow("Max message size (KB):", self.e_max_size)

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
        self._cfg["app"]["auto_check_mail"]      = self.chk_auto_dl.isChecked()
        self._cfg["app"]["dark_mode"]            = self.chk_dark_mode.isChecked()
        self._cfg["app"]["font_size"]            = self.spin_font.value()
        self._cfg["app"]["data_dir"]             = self.e_data_dir.text().strip() or "data"
        sz = self.e_max_size.text().strip()
        self._cfg["app"]["max_message_size_kb"]  = int(sz) if sz.isdigit() else 50

        # Bulletins tab
        self._cfg.setdefault("bulletins", {})
        self._cfg["bulletins"]["check_on_connect"] = self.chk_bull_auto.isChecked()
        raw_subs = self.bull_list.toPlainText().strip()
        subs = []
        for line in raw_subs.splitlines():
            # Strip @ scope if user typed SITREP@USA — store category only
            cat = line.strip().upper().split("@")[0]
            if cat:
                subs.append(cat)
        self._cfg["bulletins"]["subscriptions"] = subs

        # PTT tab
        sig_map = {0: "rts", 1: "dtr", 2: "rts+dtr"}
        self._cfg["ptt"] = {
            "mode":   "none" if self.ptt_mode.currentIndex() == 0 else "com",
            "port":   self.ptt_port.currentText() if self.ptt_mode.currentIndex() == 1 else "",
            "signal": sig_map[self.ptt_signal.currentIndex()],
        }

        self.accept()

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
        ("Direwolf",     "direwolf"),    # future
        ("Soundmodem",   "soundmodem"),  # future
    ]
    FUTURE = {"direwolf", "soundmodem"}

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
        self.e_callsign.setPlaceholderText("e.g. KC9MTP-1")
        self.e_notes    = QLineEdit(e.get("notes", ""))

        form.addRow("Name:",      self.e_name)
        form.addRow("Callsign:",  self.e_callsign)

        # ── Terminal/Debug prompt option ───────────────
        self.chk_no_terminal_prompt = QCheckBox(
            "Never prompt for mail download in Terminal / Debug view")
        self.chk_no_terminal_prompt.setChecked(
            bool(e.get("no_terminal_prompt", False)))
        self.chk_no_terminal_prompt.setToolTip(
            "When checked, connecting in Terminal or Debug view will always "
            "act as a pure dumb terminal — no mail download dialog.")
        form.addRow("", self.chk_no_terminal_prompt)

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
        self.e_bw.addItems(["500", "2300"])
        self.e_bw.setFixedWidth(70)
        self.e_bw.setCurrentText(e.get("bw", "500"))

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

    def _on_transport_changed(self, index: int):
        _, key = self.TRANSPORTS[index]
        is_vara    = key in ("vara_hf", "vara_fm")
        is_telnet  = key == "telnet"
        is_future  = key in self.FUTURE

        self.vara_group.setVisible(is_vara)
        self.telnet_group.setVisible(is_telnet)
        self.future_group.setVisible(is_future)

        if is_future:
            label_map = {
                "direwolf":   "Direwolf / AX.25 packet support is planned for a future release.",
                "soundmodem": "Soundmodem support is planned for a future release.",
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
        self.accept()

    def get_entry(self) -> dict:
        _, key = self.TRANSPORTS[self.transport_combo.currentIndex()]
        port = self.e_port.text().strip()
        return {
            "name":        self.e_name.text().strip(),
            "callsign":    self.e_callsign.text().strip().upper(),
            "transport":   key,
            "vara_type":   ("hf" if key == "vara_hf" else
                            "fm" if key == "vara_fm" else None),
            "freq":        self.e_freq.text().strip(),
            "bw":          self.e_bw.currentText(),
            "host":        self.e_host.text().strip(),
            "telnet_port": int(port) if port.isdigit() else 8010,
            "notes":       self.e_notes.text().strip(),
            "no_terminal_prompt": self.chk_no_terminal_prompt.isChecked(),
        }


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
        self.db      = MessageDatabase(
            data_dir=self.config["app"].get("data_dir") or _default_data)
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

        mycall = self.config.get("user", {}).get("callsign", "").upper()
        self.setWindowTitle(f"QtC - {mycall}" if mycall else "QtC")
        self.setMinimumSize(920, 620)
        self.resize(1120, 720)

        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._rx_buf = ""   # buffer for partial RX lines across VARA frames
        self._pending_summary = None
        self._pending_summary = None   # held summary waiting for Mail view switch
        self._pending_outbox  = False   # outbox ready, waiting for Mail view switch
        self._send_total      = 0
        self._send_current    = 0
        self._last_disconnect_time = 0.0   # used to add extra VARA reset delay
        self._last_link_bps   = 0          # last seen VARA link speed for estimates
        self._update_folder_counts()   # populate badges on startup

        self._refresh_folder("inbox")
        self._set_status("Ready — not connected", connected=False)
        # Apply saved font size to all text panes
        font_size = self.config.get("app", {}).get("font_size", 10)
        self._apply_font_size(font_size)

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
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_settings)
        file_menu.addAction(act_addrbook)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        # View
        view_menu = mb.addMenu("&View")
        act_mail = QAction("📬  Mail View", self)
        act_term = QAction("💻  Terminal View", self)
        act_mail.triggered.connect(lambda: self._switch_view(self.VIEW_MAIL))
        act_term.triggered.connect(lambda: self._switch_view(self.VIEW_TERMINAL))
        act_debug = view_menu.addAction("🔬  Debug View")
        act_debug.triggered.connect(lambda: self._switch_view(self.VIEW_DEBUG))
        view_menu.addAction(act_mail)
        view_menu.addAction(act_term)

        # Help
        help_menu = mb.addMenu("&Help")
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._on_about)
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
        self.mode_combo.addItems(["VARA HF", "VARA FM"])
        self.mode_combo.setFixedWidth(88)
        self.mode_combo.setToolTip("VARA modem type")
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        vara_layout.addWidget(self.mode_combo)

        vara_layout.addWidget(QLabel("  Call:"))
        self.call_edit = QLineEdit()
        self.call_edit.setFixedWidth(88)
        self.call_edit.setPlaceholderText("KC9MTP-1")
        self.call_edit.setToolTip("BBS callsign to connect to via VARA")
        vara_layout.addWidget(self.call_edit)

        vara_layout.addWidget(QLabel("  BW:"))
        self.bw_combo = QComboBox()
        self.bw_combo.addItems(["500", "2300"])
        self.bw_combo.setFixedWidth(62)
        self.bw_combo.setToolTip("VARA HF bandwidth — must match the BBS node setting")
        self.bw_combo.currentTextChanged.connect(lambda bw: self._vara_set_bw(bw))
        vara_layout.addWidget(self.bw_combo)

        self.tb_stack.addWidget(vara_panel)   # index 0

        # ── Panel 1: Telnet ────────────────────────────
        telnet_panel = QWidget()
        telnet_layout = QHBoxLayout(telnet_panel)
        telnet_layout.setContentsMargins(0, 0, 0, 0)
        telnet_layout.setSpacing(4)

        telnet_layout.addWidget(QLabel("Call:"))
        self.telnet_call_edit = QLineEdit()
        self.telnet_call_edit.setFixedWidth(88)
        self.telnet_call_edit.setPlaceholderText("KC9MTP-1")
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

        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.setToolTip(
            "Check for new mail now (LM) — choose PN only or PN+PY")
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setVisible(False)   # hidden until tested in the field
        self.btn_refresh.clicked.connect(self._on_refresh)
        tb.addWidget(self.btn_refresh)

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

        self.btn_terminal = QPushButton("💻  Terminal")
        self.btn_terminal.setCheckable(True)
        self.btn_terminal.setToolTip("Clean terminal view — readable BBS text only")
        self.btn_terminal.setStyleSheet(
            "QPushButton:checked { background-color: #2a5a2a; color: #00ff88; "
            "border: 1px solid #00aa44; }")
        self.btn_terminal.clicked.connect(
            lambda: self._switch_view(self.VIEW_TERMINAL))
        tb.addWidget(self.btn_terminal)

        self.btn_debug = QPushButton("🔬  Debug")
        self.btn_debug.setCheckable(True)
        self.btn_debug.setToolTip("Debug view — verbose session monitoring output")
        self.btn_debug.setStyleSheet(
            "QPushButton:checked { background-color: #1a1a4a; color: #8888ff; "
            "border: 1px solid #4444aa; }")
        self.btn_debug.clicked.connect(
            lambda: self._switch_view(self.VIEW_DEBUG))
        tb.addWidget(self.btn_debug)

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
        self.mail_view.sig_send_outbox.connect(self._on_send_outbox)
        self.mail_view.sig_mark_all_read.connect(self._on_mark_all_read)
        self.mail_view.sig_search.connect(self._on_search)
        self.mail_view.sig_folder_changed.connect(self._on_folder_changed)
        self.mail_view.sig_row_selected.connect(self._on_row_selected)
        self.stack.addWidget(self.mail_view)   # index 0

        # Page 1 — Terminal view (clean readable text only)
        self.terminal = TerminalWidget()
        self.terminal.sig_send_cmd.connect(self._on_terminal_cmd)
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

        # Progress bar — fixed width, shown during operations
        self._prog_bar = QProgressBar()
        self._prog_bar.setTextVisible(True)
        self._prog_bar.setFixedHeight(14)
        self._prog_bar.setFixedWidth(200)
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setVisible(False)
        sb.addPermanentWidget(self._prog_bar)

    # ── View switching ────────────────────────────────────────────

    def _switch_view(self, view: int):
        self.stack.setCurrentIndex(view)
        self.btn_terminal.setChecked(view == self.VIEW_TERMINAL)
        self.btn_debug.setChecked(view == self.VIEW_DEBUG)
        # Enable data streaming when in Terminal/Debug view
        self._set_transport_terminal_mode(view in (self.VIEW_TERMINAL, self.VIEW_DEBUG))
        if view == self.VIEW_TERMINAL:
            self.terminal.input_line.setFocus()
        elif view == self.VIEW_MAIL and self._pending_summary is not None:
            # User switched to Mail view after choosing to download from Terminal
            summary = self._pending_summary
            self._pending_summary = None
            self._on_mail_summary(summary)
        elif view == self.VIEW_MAIL and self._pending_outbox:
            # User switched to Mail view with outbox ready to send
            self._pending_outbox = False
            self._on_send_outbox()

    def _on_terminal_toggle(self):
        # Legacy — kept for any menu wiring, routes to terminal view
        self._switch_view(self.VIEW_TERMINAL)

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
            mode_str = "VARA HF" if transport == "vara_hf" else "VARA FM"
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(mode_str)
            self.mode_combo.blockSignals(False)
            self.call_edit.setText(entry.get("callsign", ""))
            self.bw_combo.setCurrentText(entry.get("bw", "500"))
            self._vara_set_bw(entry.get("bw", "500"))

    def _on_mode_changed(self, mode_str: str):
        pass   # mode change within VARA panel — no visibility change needed

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
        transport_map = {"VARA HF": "vara_hf", "VARA FM": "vara_fm", "Telnet": "telnet"}
        port_str = self.port_edit.text().strip()
        new_entry = {
            "name":        name.strip(),
            "callsign":    (self.call_edit.text().strip().upper() if not is_telnet
                            else self.telnet_call_edit.text().strip().upper()
                                 or self.host_edit.text().strip().upper().split(".")[0]),
            "transport":   transport_map[mode_str],
            "vara_type":   ("hf" if mode_str == "VARA HF" else
                            "fm" if mode_str == "VARA FM" else None),
            "bw":          self.bw_combo.currentText() if not is_telnet else "",
            "host":        self.host_edit.text().strip() if is_telnet else "",
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
        # Home BBS may be in hierarchical form e.g. KC9MTP.#NWIN.IN.USA.NOAM
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
            entry["transport"] = "vara_hf" if mode_str == "VARA HF" else "vara_fm"
            entry["vara_type"] = "hf"      if mode_str == "VARA HF" else "fm"
            entry["callsign"]  = call or entry.get("callsign", "")
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
        if not self._vara_ctrl.is_open:
            self._vara_ctrl.open()
        if self._vara_ctrl.is_open:
            self._vara_ctrl.set_bandwidth(bw)

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

        if transport == "telnet":
            conn_desc = f"{entry['host']}:{entry['telnet_port']}"
        else:
            mode_label = "VARA HF" if entry.get("vara_type") == "hf" else "VARA FM"
            freq = entry.get("freq", "")
            bw   = entry.get("bw", "500")
            conn_desc = f"{mode_label}  {freq}  BW{bw}".strip()

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
        self.worker.sig_first_visit.connect(self._on_first_visit)
        self.worker.sig_progress.connect(self._on_progress)
        self.worker.sig_bulletin_check.connect(self._on_bulletin_check)
        self.worker.sig_bulletin_done.connect(self._on_bulletin_done)

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

        self.worker.do_connect_and_check()

    def _on_disconnect(self):
        if self.worker:
            self.worker.do_disconnect()
        self._set_status("Disconnecting…", connecting=True)

    def _on_refresh(self):
        """Manual mail check — ask PN only or PN+PY, then run LM."""
        if not self.worker or not self.worker.session:
            QMessageBox.warning(self, "Not Connected",
                "Connect to a BBS first.")
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("Check Mail")
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText("<b>Check for mail now?</b><br><br>"
                    "Choose which messages to list:")
        btn_new = msg.addButton(
            "⚡  New messages only (PN)", QMessageBox.ButtonRole.NoRole)
        btn_all = msg.addButton(
            "📥  All personal messages (PN + PY)", QMessageBox.ButtonRole.YesRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == btn_new:
            self.worker.do_mail_check(new_only=True)
        elif clicked == btn_all:
            self.worker.do_mail_check(new_only=False)

    def _set_transport_terminal_mode(self, enabled: bool):
        """Enable/disable background data streaming on the active transport."""
        if self.worker and self.worker.session:
            t = self.worker.session.transport
            if hasattr(t, "set_terminal_mode"):
                t.set_terminal_mode(enabled)

    def _on_rf_connected(self):
        """Fires immediately when RF link is up — before login/registration."""
        self.terminal.append("\n=== Connected ===\n", "#00ff88")
        self.debug_view.append("\n=== Connected ===\n", "#00ff88")

    def _on_connected(self):
        entry      = self._get_active_bbs_entry()
        transport  = entry.get("transport", "")
        if transport == "telnet":
            detail = entry.get("host", "")
        else:
            bw = entry.get("bw", "500")
            detail = f"VARA HF  BW{bw}" if transport == "vara_hf" else f"VARA FM  BW{bw}"
        self._set_status(
            f"Connected  ·  {entry['callsign']}  ({detail})",
            connected=True)
        self.btn_refresh.setEnabled(True)

    def _check_outbox_for_terminal(self):
        """In Terminal/Debug view with no mail check running, still notify
        user if there are outbox messages ready to send for this BBS."""
        pending = self.db.get_pending_outbox()
        if not pending:
            return
        connected_bbs = self._get_active_bbs_entry().get("callsign", "").upper()
        sendable = [r for r in pending
                    if bool(r.get("send_now", 1))
                    or not r.get("at_bbs", "")
                    or r.get("at_bbs","").upper().startswith(connected_bbs)]
        if sendable:
            self._pending_outbox = True
            self.mail_view.enable_send_outbox(True)
            notice = (f"\n[MAIL] {len(sendable)} message(s) ready "
                      f"to send — switch to Mail View to send.\n")
            self.terminal.append(notice, "#ffff00")
            self.debug_view.append(notice, "#ffff00")
            self._set_status(
                f"{len(sendable)} message(s) in outbox — "
                f"switch to Mail View to send.",
                connected=True)

    def _on_first_visit(self, bbs_callsign: str):
        """
        Called after login. Behaviour depends on current view:

        Mail View:
          - First visit: ask All / New only → marks BBS visited
          - Return visit: auto LM, new PN only

        Terminal / Debug View:
          - First visit: ask Skip / New only / All → Skip does NOT mark visited
          - Return visit: pure dumb terminal, no LM, no download at all
        """
        mycall    = self.config.get("user", {}).get("callsign", "NOCALL").upper()
        visit_key = f"{mycall}@{bbs_callsign}"
        visited   = self.config.get("visited_bbs", {})
        view      = self.stack.currentIndex()

        # Migrate old-style keys (just "BBS_CALL") to new "MYCALL@BBS_CALL" format
        if bbs_callsign in visited and visit_key not in visited:
            visited[visit_key] = visited.pop(bbs_callsign)
            self.config["visited_bbs"] = visited
            save_config(self.config)

        # ── Terminal / Debug view ──────────────────────────────────
        if view in (self.VIEW_TERMINAL, self.VIEW_DEBUG):
            # Check BBS-level setting — if set, always pure dumb terminal
            no_prompt = self._get_active_bbs_entry().get("no_terminal_prompt", False)
            if no_prompt:
                self._set_status("Terminal mode — type commands manually.",
                                 connected=True)
                self._set_transport_terminal_mode(True)
                self._check_outbox_for_terminal()
                return
            # Return visit — pure dumb terminal, outbox check only
            if visit_key in visited:
                self._set_status("Terminal mode — type commands manually.",
                                 connected=True)
                self._set_transport_terminal_mode(True)
                self._check_outbox_for_terminal()
                return
            # First visit — offer Skip / New / All
            msg = QMessageBox(self)
            msg.setWindowTitle(f"First visit to {bbs_callsign}")
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setText(
                f"<b>First visit to {bbs_callsign} as {mycall}</b><br><br>"
                f"Would you like to download any messages, or skip and use "
                f"the terminal manually?<br><br>"
                f"<i>Choosing Skip will not mark this BBS as visited — "
                f"you will be asked again next time you connect from Mail View.<br><br>"
                f"To disable this prompt permanently, go to "
                f"<b>File → Settings → BBS List → Edit</b> and check "
                f"'Never prompt for mail download in Terminal / Debug view'.</i>"
            )
            btn_skip = msg.addButton(
                "⏭  Skip — pure terminal", QMessageBox.ButtonRole.RejectRole)
            btn_new  = msg.addButton(
                "⚡  New messages only (PN)", QMessageBox.ButtonRole.NoRole)
            btn_all  = msg.addButton(
                "📥  All personal messages (PN + PY)", QMessageBox.ButtonRole.YesRole)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked == btn_skip:
                # Don't mark visited — pure terminal session
                self._set_status("Terminal mode — type commands manually.",
                                 connected=True)
                self._set_transport_terminal_mode(True)
                self._check_outbox_for_terminal()
                return
            elif clicked == btn_new:
                self.config.setdefault("visited_bbs", {})[visit_key] = True
                save_config(self.config)
                QTimer.singleShot(50, lambda: self.worker.do_mail_check(new_only=True))
            else:  # All
                self.config.setdefault("visited_bbs", {})[visit_key] = True
                save_config(self.config)
                QTimer.singleShot(50, lambda: self.worker.do_mail_check(new_only=False))
            return

        # ── Mail View ─────────────────────────────────────────────
        if visit_key not in visited:
            # First visit — ask All / New only
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
            msg.addButton("📥  All personal messages (PN + PY)", QMessageBox.ButtonRole.YesRole)
            msg.addButton("⚡  New messages only (PN)", QMessageBox.ButtonRole.NoRole)
            msg.exec()

            full_lm = (msg.clickedButton().text().startswith("📥"))

            # Mark this user+BBS combo as visited so we never ask again
            self.config.setdefault("visited_bbs", {})[visit_key] = True
            save_config(self.config)

            QTimer.singleShot(50, lambda nf=not full_lm: self.worker.do_mail_check(new_only=nf))
        else:
            # Returning visit — LM, new PN only
            QTimer.singleShot(50, lambda: self.worker.do_mail_check(new_only=True))

    def _on_progress(self, op: str, current: int, total: int, detail: str):
        """Update toolbar pill and status bar progress during operations."""
        if op == "done" or total == 0:
            self._prog_bar.setVisible(False)
            self._prog_detail.setVisible(False)
            self.status_label.setVisible(True)
            # conn_label will be updated by next _set_status call
            return
        pct = int(current / total * 100)
        op_label = "downloading" if op == "downloading" else "sending"
        # Update conn_label to show progress — always visible in toolbar
        self.conn_label.setText(f"  {current} / {total}  {op_label}")
        # Status bar: hide plain label, show detail + progress bar
        self.status_label.setVisible(False)
        if op == "downloading":
            self._prog_detail.setText(
                f"Downloading {current} of {total} · {detail}")
        else:
            self._prog_detail.setText(
                f"Sending {current} of {total} · {detail}")
        self._prog_detail.setVisible(True)
        self._prog_bar.setValue(pct)
        self._prog_bar.setVisible(True)

    def _on_disconnected(self):
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.mail_view.enable_send_outbox(False)
        self._set_status("Disconnected", connected=False)
        self.terminal.append("\n=== Disconnected ===\n", "#ff8844")
        self.debug_view.append("\n=== Disconnected ===\n", "#ff8844")
        self._rx_buf = ""
        self._pending_summary = None
        self._pending_outbox  = False
        self._send_total      = 0
        self._send_current    = 0
        self._prog_bar.setVisible(False)
        self._prog_detail.setVisible(False)
        self.status_label.setVisible(True)
        self._vara_info_label.setVisible(False)
        self._vara_info_label.setText("")
        # Mark that we just disconnected — _on_connect will apply
        # a longer VARA reset delay if this was a remote disconnect
        self._last_disconnect_time = __import__("time").time()
        # Reclaim the VaraControl socket for pre-session BW commands
        self._vara_ctrl.open()

    def _on_error(self, msg: str):
        self.btn_connect.setEnabled(False)   # briefly disabled while VARA recovers
        self.btn_disconnect.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self._set_status(f"Error: {msg}", connected=False)
        self.terminal.append(f"\n[ERROR] {msg}\n", "#ff4444")
        self.debug_view.append(f"\n[ERROR] {msg}\n", "#ff4444")
        QMessageBox.critical(self, "Connection Error", msg)
        # Give VARA a moment to reset its TCP listener before allowing reconnect
        # and reclaiming VaraControl — a failed RF connect can take ~10-15s to recover
        self.terminal.append("[SYS] Waiting for VARA to reset…\n", "#888888")
        QTimer.singleShot(8000, self._vara_recover)

    def _vara_recover(self):
        """Called ~8s after a connection error to re-enable connect and reclaim VARA."""
        self._vara_ctrl.open()
        self.btn_connect.setEnabled(True)
        self.btn_refresh.setEnabled(False)   # not connected yet — stays off until next connect
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
            self._rx_buf = ""

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
            # In Terminal/Debug view — hold the summary and notify user
            if self.stack.currentIndex() in (self.VIEW_TERMINAL, self.VIEW_DEBUG):
                self._pending_summary = summary
                notice = (f"\n[MAIL] {new_count} message(s) ready — "
                          f"switch to Mail View to download.\n")
                self.terminal.append(notice, "#ffff00")
                self.debug_view.append(notice, "#ffff00")
                self._set_status(
                    f"{new_count} new message(s) available — switch to Mail view to download.",
                    connected=True)
            else:
                # Auto-download — no dialog, per user preference
                self._set_status(
                    f"Downloading {new_count} new message(s)…", connected=True)
                self.worker.do_download(summary.new_personal)
        else:
            self._set_status("No new personal mail.", connected=True)
            # Trigger bulletin check if enabled, subscriptions exist, and on home BBS
            bull_cfg = self.config.get("bulletins", {})
            subs = bull_cfg.get("subscriptions", [])
            if bull_cfg.get("check_on_connect", False) and subs and self._is_home_bbs():
                self._set_status("Checking bulletins…", connected=True)
                QTimer.singleShot(200, lambda: self.worker.do_check_bulletins(subs))
                return
            # Route through _prompt_outbox — handles outbox and Telnet auto-disconnect
            self._prompt_outbox()

    def _on_download_done(self, count: int):
        self._set_status(
            f"Downloaded {count} message(s).", connected=True)
        self._refresh_folder("inbox")
        self._update_folder_counts()
        dl_line = f"\n[MAIL] Downloaded {count} message(s) to inbox.\n"
        self.terminal.append(dl_line, "#aaffff")
        self.debug_view.append(dl_line, "#aaffff")

        # Trigger bulletin check if enabled, subscriptions exist, and on home BBS
        bull_cfg = self.config.get("bulletins", {})
        subs = bull_cfg.get("subscriptions", [])
        if bull_cfg.get("check_on_connect", False) and subs and self._is_home_bbs():
            self._set_status("Checking bulletins…", connected=True)
            QTimer.singleShot(200, lambda: self.worker.do_check_bulletins(subs))
            return   # outbox prompt will happen after bulletin check

        # Prompt about outbox / auto-disconnect
        pending = self.db.get_pending_outbox()
        if pending:
            self.mail_view.enable_send_outbox(True)
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
            r = QMessageBox.question(
                self, "Outbox",
                f"{len(pending)} message(s) waiting in outbox — send now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r == QMessageBox.StandardButton.Yes:
                self._on_send_outbox()
                return   # disconnect will happen after send completes

        # Auto-disconnect Telnet sessions when initiated from Mail View
        # LinBPQ has no built-in idle timer for Telnet — we disconnect cleanly
        # after all downloads and outbox work is done so we don't hold a
        # connection slot on the BBS indefinitely.
        if (self.stack.currentIndex() == self.VIEW_MAIL
                and self.worker and self.worker.session
                and isinstance(self.worker.session.transport, TelnetTransport)):
            self._set_status("Telnet session complete — disconnecting…",
                             connected=True)
            QTimer.singleShot(800, self._on_disconnect)

    # ── Outbox ────────────────────────────────────────────────────

    def _on_send_outbox(self):
        pending = self.db.get_pending_outbox()
        if not pending:
            QMessageBox.information(self, "Outbox", "Outbox is empty.")
            return
        if not self.worker or not self.worker.session:
            QMessageBox.warning(self, "Not Connected",
                "Connect to your home BBS first.")
            return
        total = len(pending)
        self._set_status(f"Sending {total} outbox message(s)…", connected=True)
        self._send_total   = total
        self._send_current = 0
        for row in pending:
            self.worker.do_send(
                row["to_call"], row["subject"],
                row["body"],    row["msg_type"],
                row["at_bbs"] or "")

    def _on_send_result(self, success: bool, to_call: str):
        if success:
            for row in self.db.get_pending_outbox():
                if row["to_call"].upper() == to_call.upper():
                    self.db.mark_sent(row["id"])
                    break
            # Increment address book use count so this contact rises in dropdown
            self.cdb.increment_use(to_call)
            # Update send progress pill
            self._send_current = getattr(self, "_send_current", 0) + 1
            total = getattr(self, "_send_total", 1)
            if self._send_current < total:
                self.worker.sig_progress.emit(
                    "sending", self._send_current, total,
                    f"to {to_call}")
            else:
                self.worker.sig_progress.emit("done", 0, 0, "")
            self._refresh_folder(self._current_folder)
            self._update_folder_counts()
            self._set_status(f"Message sent to {to_call}", connected=True)
            self.terminal.append(
                f"\n[SEND] Message sent to {to_call}\n", "#aaffff")
            # Auto-disconnect Telnet after last message sent from Mail View
            if (self._send_current >= total
                    and self.stack.currentIndex() == self.VIEW_MAIL
                    and self.worker and self.worker.session
                    and isinstance(self.worker.session.transport,
                                   TelnetTransport)):
                self._set_status("Telnet session complete — disconnecting…",
                                 connected=True)
                QTimer.singleShot(800, self._on_disconnect)
        else:
            self._set_status(f"Send to {to_call} failed", connected=True)
            QMessageBox.warning(self, "Send Failed",
                f"Could not send message to {to_call}.\n"
                "Check the terminal view for details.")

    # ── Folder / message display ──────────────────────────────────

    def _on_folder_changed(self, folder: str):
        self._refresh_folder(folder)

    def _refresh_folder(self, folder: str):
        self._current_folder = folder
        if   folder == "inbox":    rows = self.db.get_inbox()
        elif folder == "outbox":   rows = self.db.get_outbox()
        elif folder == "sent":     rows = self.db.get_sent()
        elif folder == "bulletins": rows = self.db.get_bulletins()
        elif folder.startswith("bulletin:"):
            cat  = folder.split(":", 1)[1]
            rows = self.db.get_bulletins(category=cat)
        else:
            rows = self.db.get_sent()
        self.mail_view.load_table(rows, folder)
        # Sync folder tree selection
        item_map = {"inbox":     self.mail_view._fi,
                    "outbox":    self.mail_view._fo,
                    "sent":      self.mail_view._fs,
                    "bulletins": self.mail_view._fb}
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
                if folder == "inbox" and not rd.get("read", 0):
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

    def _queue_with_conflict_check(self, v: dict):
        """Queue outgoing message, warning if send_now conflicts with
        existing pending messages to the same callsign."""
        to_call  = v["to_call"]
        send_now = v.get("send_now", True)
        existing = [r for r in self.db.get_pending_outbox()
                    if r["to_call"].upper() == to_call.upper()]
        if existing:
            existing_mode = bool(existing[0].get("send_now", 1))
            if existing_mode != send_now:
                mode_str     = "immediately" if send_now else "only when connected to Home BBS"
                existing_str = "immediately" if existing_mode else "only when connected to Home BBS"
                r = QMessageBox.question(
                    self, "Send Mode Conflict",
                    f"You have {len(existing)} pending message(s) to "
                    f"<b>{to_call}</b> set to send <b>{existing_str}</b>.<br><br>"
                    f"Change all messages to <b>{mode_str}</b>?",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if r == QMessageBox.StandardButton.Yes:
                    for row in existing:
                        self.db.update_send_now(row["id"], send_now)
        self.db.queue_outgoing(
            to_call=to_call,          subject=v["subject"],
            body=v["body"],           msg_type=v["msg_type"],
            at_bbs=v.get("at_bbs",""), send_now=send_now)

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
        self._queue_with_conflict_check(v)
        self._refresh_folder("outbox")   # switch to outbox so user sees queued msg
        self._update_folder_counts()
        if self.worker and self.worker.session:
            self.mail_view.enable_send_outbox(True)
        mode = "immediately" if v.get("send_now", True) else "when connected to Home BBS"
        QMessageBox.information(self, "Queued",
            f"Message to {v['to_call']} saved to outbox.\n"
            f"Will send {mode}.")

    def _on_mark_all_read(self):
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
            "inbox":    self.db.get_inbox(),
            "outbox":   self.db.get_outbox(),
            "sent":     self.db.get_sent(),
            "bulletins": self.db.get_bulletins(),
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
                    self._queue_with_conflict_check(v)
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

    def _on_terminal_cmd(self, cmd: str):
        """Send a raw command through the live BBS session."""
        if not self.worker or not self.worker.session:
            self.terminal.append(
                "[Not connected — connect to a BBS first]\n", "#ff4444")
            return
        self.worker.do_terminal_send(cmd)

    # ── Settings dialog ───────────────────────────────────────────

    def _on_address_book(self):
        dlg = AddressBookDialog(self.cdb, parent=self, select_mode=False)
        dlg.exec()

    def _on_settings(self):
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
            QMessageBox.information(self, "Settings Saved",
                f"Settings saved to {_CONFIG_PATH}.\n"
                "Reconnect to apply any connection changes.")

    def _on_about(self):
        QMessageBox.about(self, "About QtC",
            f"<b>QtC</b> v{APP_VERSION}<br><br>"
            "A modern BBS client for LinBPQ/BPQ32 nodes<br>"
            "via VARA HF, VARA FM, and Telnet.<br><br>"
            "Built with Python + PyQt6.")

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
        pending = len(self.db.get_pending_outbox())
        sent    = len(self.db.get_sent())
        self.mail_view.update_folder_counts(unread, pending, sent)
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

    def closeEvent(self, event):
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


def main():
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland*=false")
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

    # Set application icon from qtc_icon.svg if present alongside the script
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qtc_icon.svg")
    if os.path.exists(_icon_path):
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(_icon_path))

    win = MainWindow()
    win.show()

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
    main()
