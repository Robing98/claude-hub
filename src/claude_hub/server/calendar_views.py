"""Pages for the calendar: the agenda of every Google account, and event proposals."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .db import now_iso as now_stamp
from .google import SCOPES, GoogleError, local_today
from .ingest import get_conn
from .views import back, hiding, render

router = APIRouter()

AGENDA_DAYS = 14
MAX_AGENDA_DAYS = 120
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def day_label(day: str, today: date) -> str:
    """``Monday, 2026-10-05``, with "today" and "tomorrow" where they apply."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day
    near = {0: "Today", 1: "Tomorrow"}.get((parsed - today).days)
    return f"{near or WEEKDAYS[parsed.weekday()]}, {day}"


def clock(event: dict[str, Any]) -> str:
    """The time span of an event, as it appears in the agenda."""
    if event["all_day"]:
        last = event.get("end")
        return "all day" if not last or last == event["start"] else f"until {last}"
    start, end = str(event["start"]), str(event.get("end") or "")
    return f"{start[11:16]} to {end[11:16]}" if end[:10] == start[:10] else f"from {start[11:16]}"


def by_day(events: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    days: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        # A longer event that began earlier belongs under today, not in the past.
        day = max(str(event["start"])[:10], today.isoformat())
        days.setdefault(day, []).append({**event, "clock": clock(event)})
    return [{"label": day_label(day, today), "events": items} for day, items in sorted(days.items())]


@router.get("/calendar")
def calendar_page(request: Request, days: int = AGENDA_DAYS,
                  conn: sqlite3.Connection = Depends(get_conn)):
    google = request.app.state.google
    days = max(1, min(days, MAX_AGENDA_DAYS))
    pending = conn.execute(
        """SELECT e.*, m.name AS machine FROM event_proposals e
           LEFT JOIN machines m ON m.id = e.machine_id
           WHERE e.status = 'pending' ORDER BY e.created_at""").fetchall()
    decided = conn.execute(
        """SELECT e.*, m.name AS machine FROM event_proposals e
           LEFT JOIN machines m ON m.id = e.machine_id
           WHERE e.status != 'pending' ORDER BY e.decided_at DESC LIMIT 20""").fetchall()
    accounts = google.store.public()
    hidden = hiding(request)
    agenda: dict[str, Any] = {"events": [], "problems": []}
    today = local_today(google.time_zone)
    if accounts and not hidden:
        try:
            agenda = google.overview(today.isoformat(), (today + timedelta(days=days - 1)).isoformat())
        except GoogleError as exc:
            agenda = {"events": [], "problems": [str(exc)]}
    return render(request, conn, "calendar.html", pending=pending, decided=decided,
                  accounts=accounts, hidden=hidden, days=days, problems=agenda["problems"],
                  agenda=by_day(agenda["events"], today), event_count=len(agenda["events"]))


@router.post("/calendar/proposals/{proposal_id}/accept")
def accept_event(proposal_id: int, request: Request, summary: str = Form(...),
                 starts: str = Form(...), ends: str = Form(""), description: str = Form(""),
                 location: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM event_proposals WHERE id = ? AND status = 'pending'",
                       (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown proposal, or it is already decided")
    fields = {"summary": summary.strip(), "start": starts.strip(), "end": ends.strip() or None,
              "description": description, "location": location, "private": bool(row["private"])}
    # Keep what the person changed, also when Google refuses the event.
    conn.execute(
        "UPDATE event_proposals SET summary = ?, starts = ?, ends = ?, description = ?, location = ? "
        "WHERE id = ?", (fields["summary"], fields["start"], ends.strip(), description, location,
                         proposal_id))
    conn.commit()
    try:
        event = request.app.state.google.create_event(
            row["account"], row["calendar_id"], fields, row["source"] or "event proposal")
    except GoogleError as exc:
        conn.execute("UPDATE event_proposals SET error = ? WHERE id = ?", (str(exc), proposal_id))
        return back(request, conn, "/calendar")
    conn.execute(
        "UPDATE event_proposals SET status = 'accepted', decided_at = ?, event_link = ?, error = NULL "
        "WHERE id = ?", (now_stamp(), event.get("link"), proposal_id))
    conn.execute("INSERT INTO google_log (at, machine_id, account, action, detail) VALUES (?, NULL, ?, ?, ?)",
                 (now_stamp(), row["account"], "event created",
                  f"{event['summary']} · {event['start']} · {event['calendar']} · accepted proposal"[:300]))
    return back(request, conn, "/calendar")


@router.post("/calendar/proposals/{proposal_id}/reject")
def reject_event(proposal_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("UPDATE event_proposals SET status = 'rejected', decided_at = ? "
                 "WHERE id = ? AND status = 'pending'", (now_stamp(), proposal_id))
    return back(request, conn, "/calendar")


@router.get("/settings/google")
def google_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    entries = conn.execute(
        """SELECT g.*, m.name AS machine FROM google_log g
           LEFT JOIN machines m ON m.id = g.machine_id ORDER BY g.id DESC LIMIT 30""").fetchall()
    return render(request, conn, "settings_google.html",
                  accounts=request.app.state.google.store.public(), scopes=SCOPES, entries=entries,
                  time_zone=request.app.state.google.time_zone)


@router.post("/settings/google/{label}/remove")
def remove_google_account(label: str, request: Request,
                          conn: sqlite3.Connection = Depends(get_conn)):
    if request.app.state.google.store.remove(label):
        conn.execute("INSERT INTO google_log (at, machine_id, account, action, detail) "
                     "VALUES (?, NULL, ?, 'account removed', 'in the hub')", (now_stamp(), label))
    return back(request, conn, "/settings/google")
