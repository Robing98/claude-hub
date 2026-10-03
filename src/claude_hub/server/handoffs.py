"""Handoffs: a text that one session, or the person, leaves for another session.

Example: the design lane of a project, which runs in Cowork, writes a brief.
The code lane, which runs in Claude Code, receives it with its next prompt.
Nobody copies text between two apps by hand.

A handoff belongs to a project. It is ``open`` until a session takes it,
``taken`` while that session works on it, and ``done`` afterwards.
"""

from __future__ import annotations

import sqlite3
from typing import Any

TEXT_LIMIT = 100_000
# A handoff up to this size is shown in full where it is delivered. A longer
# one is shown with its beginning, and the session loads the rest.
INLINE_LIMIT = 6000
STATES = ("open", "taken", "done")


class HandoffError(ValueError):
    pass


def lane(value: str | None) -> str:
    """A lane name in its stored form: lower case, single spaces."""
    return " ".join((value or "").lower().split())[:60]


def create(conn: sqlite3.Connection, project_id: int, title: str, text: str, from_lane: str,
           to_lane: str, now: str, machine_id: int | None = None) -> int:
    title = " ".join((title or "").split())
    text = (text or "").strip()
    if not title or not text:
        raise HandoffError("A handoff needs a title and a text.")
    if len(text) > TEXT_LIMIT:
        raise HandoffError(f"The text is longer than {TEXT_LIMIT} characters. Put it into a file "
                           "of the project and hand over the path.")
    return conn.execute(
        """INSERT INTO handoffs (created_at, machine_id, project_id, from_lane, to_lane, title, text)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now, machine_id, project_id, lane(from_lane), lane(to_lane), title[:200], text)).lastrowid


def brief(row: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    text = row["text"]
    cut = not full and len(text) > INLINE_LIMIT
    return {
        "id": row["id"], "title": row["title"], "from": row["from_lane"], "to": row["to_lane"],
        "status": row["status"], "created_at": row["created_at"], "taken_by": row["taken_by"],
        "result": row["result"], "size": len(text),
        "text": text[:INLINE_LIMIT] if cut else text, "cut": cut,
    }


def listing(conn: sqlite3.Connection, project_id: int, status: str = "open") -> list[sqlite3.Row]:
    wanted = STATES if status == "all" else (status,)
    return conn.execute(
        f"SELECT * FROM handoffs WHERE project_id = ? AND status IN ({', '.join('?' * len(wanted))}) "
        "ORDER BY id", (project_id, *wanted)).fetchall()


def new_for_session(conn: sqlite3.Connection, project_id: int, session_id: str,
                    now: str) -> list[sqlite3.Row]:
    """Open handoffs of the project that this session has not been shown yet. Marks them as shown."""
    rows = conn.execute(
        """SELECT h.* FROM handoffs h
           WHERE h.project_id = ? AND h.status = 'open'
             AND NOT EXISTS (SELECT 1 FROM handoff_deliveries d
                             WHERE d.handoff_id = h.id AND d.session_id = ?)
           ORDER BY h.id""", (project_id, session_id)).fetchall()
    conn.executemany("INSERT OR IGNORE INTO handoff_deliveries (handoff_id, session_id, at) VALUES (?, ?, ?)",
                     [(row["id"], session_id, now) for row in rows])
    return rows


def take(conn: sqlite3.Connection, handoff_id: int, by: str, now: str) -> sqlite3.Row:
    """Mark an open handoff as taken. Returns the row as it is afterwards."""
    conn.execute("UPDATE handoffs SET status = 'taken', taken_at = ?, taken_by = ? "
                 "WHERE id = ? AND status = 'open'", (now, lane(by), handoff_id))
    return get(conn, handoff_id)


def get(conn: sqlite3.Connection, handoff_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
    if row is None:
        raise HandoffError(f"No handoff {handoff_id}.")
    return row


def finish(conn: sqlite3.Connection, handoff_id: int, result: str, now: str) -> sqlite3.Row:
    get(conn, handoff_id)
    conn.execute("UPDATE handoffs SET status = 'done', done_at = ?, result = ? WHERE id = ?",
                 (now, (result or "").strip()[:4000], handoff_id))
    return get(conn, handoff_id)


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM handoffs WHERE status = 'open'").fetchone()[0]
