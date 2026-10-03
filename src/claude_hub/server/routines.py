"""Routines: things that come back on a schedule.

A checkup every six months, a dentist appointment once a year, a request
for a new prescription every twelve weeks. A routine has a next due day.
From ``lead_days`` before that day it counts as due and shows on the Now
page and in the status feed, until the person marks it as done or skips it.

A routine can carry a prepared mail. What happens with it is a choice per
routine, and only the person makes it, on the page:

- ``link``: the page offers a link that opens the mail in the mail program
  of the person, who sends it. This is the default, and all a session can draft.
- ``send``: the page has a button, and the hub sends the mail when it is pressed.
- ``auto``: the hub sends the mail by itself when the routine becomes due.

The hub sends only from a Google account for which the person ticked the
box for sending, only to the one address of the routine, and at most once
per round.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

UNITS = ("days", "weeks", "months")
# "done": the next time counts from the day it was done, as for an
# appointment. "due": it counts from the due day, as for a supply that runs
# out on a fixed day no matter when the request went out.
ANCHORS = ("done", "due")
MAIL_MODES = ("link", "send", "auto")
# The hub sends by itself only during the day, so that a mail does not go out at night.
SEND_HOURS = range(8, 19)
# Whatever else goes wrong, a routine never sends by itself twice within this time.
RESEND_PAUSE = timedelta(hours=20)


class RoutineError(ValueError):
    pass


def add_interval(day: date, every: int, unit: str) -> date:
    if unit == "days":
        return day + timedelta(days=every)
    if unit == "weeks":
        return day + timedelta(weeks=every)
    months = day.month - 1 + every
    year, month = day.year + months // 12, months % 12 + 1
    # The 31st plus one month is the last day of the next month.
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def parse_day(value: str, what: str = "day") -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise RoutineError(f"Not a {what}: {value!r}. Use the form 2026-11-03.") from exc


def clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Check the fields of a routine and bring them into the stored form."""
    name = " ".join(str(fields.get("name") or "").split())
    if not name:
        raise RoutineError("A routine needs a name.")
    try:
        every = int(fields.get("every") or 0)
        lead = int(fields.get("lead_days") if fields.get("lead_days") not in (None, "") else 14)
    except (TypeError, ValueError) as exc:
        raise RoutineError("The interval and the lead time are whole numbers.") from exc
    unit = str(fields.get("unit") or "months")
    anchor = str(fields.get("anchor") or "done")
    if every < 1 or unit not in UNITS:
        raise RoutineError(f"The interval is a number of {', '.join(UNITS)}.")
    if anchor not in ANCHORS or not 0 <= lead <= 365:
        raise RoutineError("The lead time is 0 to 365 days.")
    # A lead time as long as the interval would make the routine due again the
    # moment it is done. With automatic sending, that would be a mail per check.
    if lead >= every * {"days": 1, "weeks": 7, "months": 28}[unit]:
        raise RoutineError("The lead time must be shorter than the interval.")
    mail_to = str(fields.get("mail_to") or "").strip()
    if mail_to and ("@" not in mail_to or any(c in mail_to for c in " ,;<>\n")):
        raise RoutineError(f"Not one mail address: {mail_to!r}")
    mode = str(fields.get("mail_mode") or "link")
    account = str(fields.get("mail_account") or "").strip()
    subject = " ".join(str(fields.get("mail_subject") or "").split())[:300]
    text = str(fields.get("mail_text") or "").strip()[:8000]
    if mode not in MAIL_MODES:
        raise RoutineError(f"The mail mode is one of {', '.join(MAIL_MODES)}.")
    if mode != "link" and not (mail_to and subject and text and account):
        raise RoutineError("For the hub to send the mail, the routine needs a recipient, a subject, "
                           "a text, and the Google account to send from.")
    return {
        "name": name[:200], "note": str(fields.get("note") or "").strip()[:4000],
        "every": every, "unit": unit, "anchor": anchor, "lead_days": lead,
        "next_due": parse_day(str(fields.get("next_due") or ""), "due day").isoformat(),
        "mail_to": mail_to, "mail_subject": subject, "mail_text": text,
        "mail_mode": mode, "mail_account": account,
    }


def state(row: dict[str, Any] | sqlite3.Row, today: date) -> str:
    """``off``, ``later``, ``due`` (inside the lead time), or ``overdue``."""
    if not row["enabled"]:
        return "off"
    due = date.fromisoformat(row["next_due"])
    if today > due:
        return "overdue"
    return "due" if today >= due - timedelta(days=row["lead_days"]) else "later"


def mailto(row: dict[str, Any] | sqlite3.Row) -> str:
    """A link that opens the prepared mail in the mail program. Empty without a recipient."""
    if not row["mail_to"]:
        return ""
    query = "&".join(f"{key}={quote(value, safe='')}" for key, value in (
        ("subject", row["mail_subject"]), ("body", row["mail_text"])) if value)
    return f"mailto:{quote(row['mail_to'], safe='@.+-_')}" + (f"?{query}" if query else "")


def view(row: sqlite3.Row, today: date) -> dict[str, Any]:
    item = dict(row)
    item["state"] = state(row, today)
    item["days_left"] = (date.fromisoformat(row["next_due"]) - today).days
    item["mailto"] = mailto(row)
    item["interval"] = f"{row['every']} {row['unit'] if row['every'] != 1 else row['unit'][:-1]}"
    return item


def load(conn: sqlite3.Connection, today: date) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM routines ORDER BY next_due, name").fetchall()
    return [view(row, today) for row in rows]


def due_count(conn: sqlite3.Connection, today: date) -> int:
    return sum(1 for item in load(conn, today) if item["state"] in ("due", "overdue"))


def create(conn: sqlite3.Connection, fields: dict[str, Any], now: str, source: str = "",
           enabled: bool = True) -> int:
    data = clean(fields)
    return conn.execute(
        f"INSERT INTO routines ({', '.join(data)}, enabled, created_at, source) "
        f"VALUES ({', '.join('?' * len(data))}, ?, ?, ?)",
        (*data.values(), int(enabled), now, source[:200])).lastrowid


def update(conn: sqlite3.Connection, routine_id: int, fields: dict[str, Any], enabled: bool) -> None:
    data = clean(fields)
    # A saved routine starts without the error of an earlier attempt to send.
    conn.execute(f"UPDATE routines SET {', '.join(f'{key} = ?' for key in data)}, enabled = ?, "
                 "mail_error = NULL WHERE id = ?", (*data.values(), int(enabled), routine_id))


def close(conn: sqlite3.Connection, routine_id: int, action: str, day: date, now: str) -> str:
    """Mark the current round as done or skipped, and set the next due day."""
    row = conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
    if row is None:
        raise RoutineError("Unknown routine.")
    due = date.fromisoformat(row["next_due"])
    if action in ("done", "sent"):
        base = day if row["anchor"] == "done" else due
    elif action == "skip":
        base = due
    else:
        raise RoutineError("Unknown action.")
    following = add_interval(base, row["every"], row["unit"])
    # A skipped or late round must not leave the next one in the past.
    while action == "skip" and following <= day:
        following = add_interval(following, row["every"], row["unit"])
    conn.execute("UPDATE routines SET next_due = ?, last_done = CASE WHEN ? = 'skip' THEN last_done ELSE ? END "
                 "WHERE id = ?", (following.isoformat(), action, day.isoformat(), routine_id))
    conn.execute("INSERT INTO routine_log (routine_id, at, action, day, next_due) VALUES (?, ?, ?, ?, ?)",
                 (routine_id, now, action, day.isoformat(), following.isoformat()))
    return following.isoformat()


def send(conn: sqlite3.Connection, google: Any, routine_id: int, today: date, now: str) -> dict[str, Any]:
    """Send the prepared mail of a routine, and close the round.

    The time of the attempt is stored and committed before the mail goes
    out. If the hub stops between the two, the routine does not send again
    by itself.
    """
    row = conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
    if row is None:
        raise RoutineError("Unknown routine.")
    if row["mail_mode"] == "link":
        raise RoutineError("This routine is not set to send from the hub.")
    if state(row, today) not in ("due", "overdue"):
        # A second press on a page that is still open must not send a second mail.
        raise RoutineError("This routine is not due. Its mail for this round is sent, or it is turned off.")
    conn.execute("UPDATE routines SET last_sent = ?, mail_error = NULL WHERE id = ?", (now, routine_id))
    conn.commit()
    try:
        sent = google.send_mail(row["mail_account"], row["mail_to"], row["mail_subject"], row["mail_text"])
    except Exception as exc:  # noqa: BLE001 - every failure must show on the page
        conn.execute("UPDATE routines SET mail_error = ? WHERE id = ?", (str(exc)[:500], routine_id))
        conn.commit()
        raise
    conn.execute("INSERT INTO google_log (at, machine_id, account, action, detail) VALUES (?, NULL, ?, ?, ?)",
                 (now, row["mail_account"], "mail sent",
                  f"{row['mail_subject']} · to {row['mail_to']} · routine {row['name']}"[:300]))
    close(conn, routine_id, "sent", today, now)
    conn.commit()
    return sent


def tick(conn: sqlite3.Connection, google: Any, moment: datetime) -> list[str]:
    """Send the mails of routines that are due and set to send by themselves.

    ``moment`` is the local time. Returns what happened, one line per routine.
    """
    if moment.hour not in SEND_HOURS:
        return []
    today = moment.date()
    done: list[str] = []
    for row in conn.execute("SELECT * FROM routines WHERE enabled = 1 AND mail_mode = 'auto'").fetchall():
        if state(row, today) not in ("due", "overdue") or row["mail_error"]:
            # After a failed attempt the person decides. The hub does not try again by itself.
            continue
        if row["last_sent"]:
            try:
                last = datetime.fromisoformat(row["last_sent"].replace("Z", "+00:00"))
                if moment.astimezone() - last.astimezone() < RESEND_PAUSE:
                    continue
            except ValueError:
                continue
        try:
            send(conn, google, row["id"], today, _stamp(moment))
            done.append(f"sent: {row['name']}")
        except Exception as exc:  # noqa: BLE001 - one routine must not stop the others
            done.append(f"failed: {row['name']}: {exc}")
    return done


def _stamp(moment: datetime) -> str:
    """A time in the form that the database uses everywhere: UTC with a Z."""
    from datetime import timezone
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
