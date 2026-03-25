# QtC v0.9.11-beta — database.py  (built 2026-03-25)
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
database.py — SQLite storage for VARA BBS Client
Inbox, outbox, sent, drafts — all per-operation connections (no persistent handle).
"""

import sqlite3
import os


DB_FILE = os.path.join(os.path.dirname(__file__), "data", "messages.db")


def _get_conn(db_file=None):
    path = db_file if db_file else DB_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_file=None):
    conn = _get_conn(db_file)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS inbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_number  INTEGER,
            bbs_id      TEXT,
            date        TEXT,
            msg_type    TEXT,
            status      TEXT,
            to_call     TEXT,
            from_call   TEXT,
            subject     TEXT,
            body        TEXT,
            size        INTEGER DEFAULT 0,
            downloaded  INTEGER DEFAULT 0,
            read        INTEGER DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(msg_number, bbs_id)
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            to_call     TEXT,
            at_bbs      TEXT,
            subject     TEXT,
            body        TEXT,
            msg_type    TEXT DEFAULT 'P',
            status      TEXT DEFAULT 'pending',
            send_now    INTEGER DEFAULT 1,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            to_call     TEXT,
            at_bbs      TEXT,
            subject     TEXT,
            body        TEXT,
            msg_type    TEXT DEFAULT 'P',
            size        INTEGER DEFAULT 0,
            sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            callsign    TEXT UNIQUE NOT NULL,
            name        TEXT DEFAULT '',
            qth         TEXT DEFAULT '',
            home_bbs    TEXT DEFAULT '',
            send_now    INTEGER DEFAULT 1,
            use_count   INTEGER DEFAULT 0,
            last_used   DATETIME
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            to_call     TEXT,
            at_bbs      TEXT,
            subject     TEXT,
            body        TEXT,
            msg_type    TEXT DEFAULT 'P',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bulletins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_number  INTEGER,
            bbs_id      TEXT,
            category    TEXT,
            at_bbs      TEXT DEFAULT '',
            from_call   TEXT,
            date        TEXT,
            subject     TEXT,
            body        TEXT,
            size        INTEGER DEFAULT 0,
            bid         TEXT DEFAULT '',
            read        INTEGER DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(msg_number, bbs_id)
        );
        CREATE TABLE IF NOT EXISTS bulletin_tombstones (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_number  INTEGER,
            bbs_id      TEXT,
            deleted_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(msg_number, bbs_id)
        );
    """)
    conn.commit()
    # Migration: add size to existing databases
    try:
        conn.execute("ALTER TABLE inbox ADD COLUMN size INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    # Migration: add send_now to existing outbox
    try:
        conn.execute("ALTER TABLE outbox ADD COLUMN send_now INTEGER DEFAULT 1")
        conn.commit()
    except Exception:
        pass
    # Migration: add size to existing sent table
    try:
        conn.execute("ALTER TABLE sent ADD COLUMN size INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.close()

    # Trim bulletin tombstones older than 120 days
    # BBS messages expire in 30-60 days — no need to keep tombstones longer
    conn2 = _get_conn(db_file)
    conn2.execute("""
        DELETE FROM bulletin_tombstones
        WHERE deleted_at < datetime('now', '-120 days')
    """)
    conn2.commit()
    conn2.close()


# ── Contacts / Address Book ───────────────────────────────────────────────────

class ContactsDB:
    def __init__(self, db_path: str):
        self._path = db_path

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM contacts ORDER BY use_count DESC, callsign ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_top(self, n: int = 5) -> list:
        """Return top N contacts by use_count then last_used."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM contacts ORDER BY use_count DESC, last_used DESC LIMIT ?",
                (n,)).fetchall()
            return [dict(r) for r in rows]

    def get_by_callsign(self, callsign: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE callsign=? COLLATE NOCASE",
                (callsign.upper(),)).fetchone()
            return dict(row) if row else None

    def save(self, callsign: str, name: str = "", qth: str = "",
             home_bbs: str = "", send_now: bool = True):
        """Insert or update a contact."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO contacts (callsign, name, qth, home_bbs, send_now) "
                "VALUES (?,?,?,?,?) ON CONFLICT(callsign) DO UPDATE SET "
                "name=excluded.name, qth=excluded.qth, "
                "home_bbs=excluded.home_bbs, send_now=excluded.send_now",
                (callsign.upper(), name, qth, home_bbs.upper(),
                 1 if send_now else 0))

    def increment_use(self, callsign: str):
        """Bump use_count and last_used when a message is sent."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE contacts SET use_count=use_count+1, "
                "last_used=CURRENT_TIMESTAMP WHERE callsign=? COLLATE NOCASE",
                (callsign.upper(),))

    def delete(self, callsign: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM contacts WHERE callsign=? COLLATE NOCASE",
                         (callsign.upper(),))

    def search(self, query: str) -> list:
        """Search callsign, name, qth."""
        q = f"%{query.lower()}%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE "
                "lower(callsign) LIKE ? OR lower(name) LIKE ? OR lower(qth) LIKE ? "
                "ORDER BY use_count DESC, callsign ASC",
                (q, q, q)).fetchall()
            return [dict(r) for r in rows]


class MessageDatabase:
    def __init__(self, data_dir=None):
        if data_dir:
            self._db_file = os.path.join(data_dir, "messages.db")
        else:
            self._db_file = DB_FILE
        init_db(self._db_file)

    def _conn(self):
        return _get_conn(self._db_file)

    def close(self):
        pass   # Per-operation connections — nothing persistent to close

    # ── Inbox ──────────────────────────────────────────────────────

    def message_exists(self, msg_number: int, bbs_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM inbox WHERE msg_number=? AND bbs_id=?",
                (msg_number, bbs_id)).fetchone()
            return row is not None

    def save_to_inbox(self, msg, bbs_id: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO inbox
                  (msg_number, bbs_id, date, msg_type, status,
                   to_call, from_call, subject, body, size, downloaded, read)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
            """, (msg.msg_number, bbs_id, msg.date, msg.msg_type,
                  msg.status, msg.to_call, msg.from_call,
                  msg.subject, msg.body, msg.size, int(msg.downloaded)))

    def get_inbox(self, bbs_id: str = None) -> list:
        with self._conn() as conn:
            if bbs_id:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE bbs_id=? ORDER BY msg_number DESC",
                    (bbs_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inbox ORDER BY msg_number DESC").fetchall()
            return [dict(r) for r in rows]

    def get_unread_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE read=0").fetchone()
            return row[0] if row else 0

    def mark_read(self, row_id: int):
        with self._conn() as conn:
            conn.execute("UPDATE inbox SET read=1 WHERE id=?", (row_id,))

    def mark_all_read(self):
        """Mark every inbox message as read."""
        with self._conn() as conn:
            conn.execute("UPDATE inbox SET read=1 WHERE read=0")

    def delete_message(self, row_id: int):
        """Delete from whichever table contains this id (inbox first, then outbox/sent)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM inbox  WHERE id=?", (row_id,))
            conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            conn.execute("DELETE FROM sent   WHERE id=?", (row_id,))

    # ── Outbox ─────────────────────────────────────────────────────

    def queue_outgoing(self, to_call: str, subject: str, body: str,
                       msg_type: str = "P", at_bbs: str = "",
                       send_now: bool = True):
        """Add a message to the outbox with status='pending'."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO outbox (to_call, at_bbs, subject, body, msg_type, status, send_now) "
                "VALUES (?,?,?,?,?,'pending',?)",
                (to_call.upper(), at_bbs.upper(), subject, body, msg_type,
                 1 if send_now else 0))

    def get_outbox(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox ORDER BY created_at ASC").fetchall()
            return [dict(r) for r in rows]

    def get_pending_outbox(self) -> list:
        """Return only outbox rows with status='pending'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_sent(self, row_id: int):
        """Move outbox row to sent table and delete from outbox."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
            if row:
                body = row["body"] or ""
                conn.execute(
                    "INSERT INTO sent (to_call, at_bbs, subject, body, msg_type, size) "
                    "VALUES (?,?,?,?,?,?)",
                    (row["to_call"], row["at_bbs"], row["subject"],
                     body, row["msg_type"], len(body.encode("utf-8"))))
                conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    def update_send_now(self, row_id: int, send_now: bool):
        """Update the send_now flag on a pending outbox message."""
        with self._conn() as conn:
            conn.execute("UPDATE outbox SET send_now=? WHERE id=?",
                         (1 if send_now else 0, row_id))

    def delete_outbox(self, row_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    # ── Sent ───────────────────────────────────────────────────────

    def get_sent(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sent ORDER BY sent_at DESC").fetchall()
            return [dict(r) for r in rows]

    # ── Drafts ─────────────────────────────────────────────────────

    def save_draft(self, to_call: str, subject: str, body: str,
                   msg_type: str = "P", at_bbs: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO drafts (to_call, at_bbs, subject, body, msg_type) "
                "VALUES (?,?,?,?,?)",
                (to_call.upper(), at_bbs.upper(), subject, body, msg_type))

    def get_drafts(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM drafts ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def delete_draft(self, row_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM drafts WHERE id=?", (row_id,))

    # ── Bulletins ──────────────────────────────────────────────────

    def bulletin_exists(self, msg_number: int, bbs_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM bulletins WHERE msg_number=? AND bbs_id=?",
                (msg_number, bbs_id)).fetchone()
            return row is not None

    def save_bulletin(self, msg, bbs_id: str, bid: str = ""):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO bulletins
                  (msg_number, bbs_id, category, at_bbs, from_call,
                   date, subject, body, size, bid, read)
                VALUES (?,?,?,?,?,?,?,?,?,?,0)
            """, (msg.msg_number, bbs_id,
                  msg.to_call.upper(),
                  msg.at_bbs or "",
                  msg.from_call,
                  msg.date, msg.subject,
                  msg.body or "", msg.size, bid))

    def get_bulletins(self, category: str = None) -> list:
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM bulletins WHERE category=? "
                    "ORDER BY msg_number DESC",
                    (category.upper(),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bulletins ORDER BY msg_number DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_bulletin_categories(self) -> list:
        """Return list of (category, total, unread) tuples."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) as total, "
                "SUM(CASE WHEN read=0 THEN 1 ELSE 0 END) as unread "
                "FROM bulletins GROUP BY category ORDER BY category ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_bulletin_unread_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bulletins WHERE read=0").fetchone()
            return row[0] if row else 0

    def mark_bulletin_read(self, row_id: int):
        with self._conn() as conn:
            conn.execute("UPDATE bulletins SET read=1 WHERE id=?", (row_id,))

    def delete_bulletin(self, row_id: int):
        """Delete bulletin and add tombstone so it won't be re-downloaded."""
        with self._conn() as conn:
            # Get msg_number and bbs_id before deleting
            row = conn.execute(
                "SELECT msg_number, bbs_id FROM bulletins WHERE id=?",
                (row_id,)).fetchone()
            if row:
                conn.execute("""
                    INSERT OR IGNORE INTO bulletin_tombstones
                        (msg_number, bbs_id)
                    VALUES (?,?)
                """, (row["msg_number"], row["bbs_id"]))
            conn.execute("DELETE FROM bulletins WHERE id=?", (row_id,))

    def bulletin_tombstone_exists(self, msg_number: int, bbs_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM bulletin_tombstones "
                "WHERE msg_number=? AND bbs_id=?",
                (msg_number, bbs_id)).fetchone()
            return row is not None

    def add_bulletin_tombstone(self, msg_number: int, bbs_id: str):
        """Add a tombstone directly — used when skipping bulletins in the
        selection dialog or auto-tombstoning old backlog on first connect."""
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO bulletin_tombstones (msg_number, bbs_id)
                VALUES (?,?)
            """, (msg_number, bbs_id))

    def add_bulletin_tombstones_batch(self, items: list, bbs_id: str):
        """Tombstone a list of BBSMessage objects in one transaction."""
        with self._conn() as conn:
            conn.executemany("""
                INSERT OR IGNORE INTO bulletin_tombstones (msg_number, bbs_id)
                VALUES (?,?)
            """, [(m.msg_number, bbs_id) for m in items])
