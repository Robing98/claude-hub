"""API for Google Calendar and Gmail. Every call needs the token of a machine."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import db
from .google import REQUIRED_SCOPES, SCOPES, Google, GoogleError, event_body, valid_label
from .ingest import current_machine, get_conn

router = APIRouter(prefix="/api/v1/google")


def service(request: Request) -> Google:
    return request.app.state.google


def machine_row(conn: sqlite3.Connection = Depends(get_conn),
                machine: sqlite3.Row = Depends(current_machine)) -> sqlite3.Row:
    """The calling machine, with the database lock released.

    Reading the token takes the write lock. A call to Google can take
    seconds, and uploads of collectors must not wait for it.
    """
    conn.commit()
    return machine


def log(conn: sqlite3.Connection, machine_id: int | None, account: str, action: str,
        detail: str) -> None:
    conn.execute("INSERT INTO google_log (at, machine_id, account, action, detail) VALUES (?, ?, ?, ?, ?)",
                 (db.now_iso(), machine_id, account, action, detail[:300]))


class AccountBody(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    scopes: list[str] = []


class EventFields(BaseModel):
    summary: str | None = Field(default=None, max_length=300)
    start: str | None = None
    end: str | None = None
    description: str | None = Field(default=None, max_length=8000)
    location: str | None = Field(default=None, max_length=500)
    private: bool | None = None

    def fields(self) -> dict[str, Any]:
        return {name: getattr(self, name)
                for name in ("summary", "start", "end", "description", "location", "private")}


class EventBody(EventFields):
    account: str
    calendar: str = "primary"
    source: str = Field(default="", max_length=200)


class EventUpdate(EventFields):
    account: str
    calendar: str = "primary"
    event_id: str = Field(min_length=1)
    source: str = Field(default="", max_length=200)


class EventProposal(EventBody):
    reason: str = Field(default="", max_length=2000)


@router.get("/accounts")
def get_accounts(request: Request, machine: sqlite3.Row = Depends(machine_row),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"accounts": service(request).store.public(), "scopes": SCOPES}


@router.put("/accounts/{label}")
def put_account(label: str, body: AccountBody, request: Request,
                machine: sqlite3.Row = Depends(machine_row),
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not valid_label(label):
        raise HTTPException(400, "A label has lower-case letters, digits, and hyphens, up to 30 characters.")
    missing = [scope for scope in REQUIRED_SCOPES if scope not in body.scopes]
    if missing:
        raise HTTPException(400, "The sign-in lacks permissions. In the Google window, tick the "
                                 f"boxes for the calendar and for reading mail. Missing: {', '.join(missing)}")
    account = service(request).connect(label, body.client_id, body.client_secret,
                                       body.refresh_token, body.scopes, db.now_iso())
    log(conn, machine["id"], label, "account connected", str(account.get("email") or ""))
    conn.commit()
    return account


@router.delete("/accounts/{label}")
def delete_account(label: str, request: Request, machine: sqlite3.Row = Depends(machine_row),
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    removed = service(request).store.remove(label)
    if removed:
        log(conn, machine["id"], label, "account removed", "")
    conn.commit()
    return {"removed": removed}


@router.get("/calendars")
def get_calendars(request: Request, account: str = "",
                  machine: sqlite3.Row = Depends(machine_row),
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    google = service(request)
    calendars: list[dict[str, Any]] = []
    problems: list[str] = []
    for entry in google.store.public():
        if account and entry["label"] != account:
            continue
        try:
            calendars += [{"account": entry["label"], "account_email": entry["email"], **item}
                          for item in google.calendars(entry["label"])]
        except GoogleError as exc:
            problems.append(str(exc))
    if account and not calendars and not problems:
        raise GoogleError(404, f"No Google account '{account}'.")
    return {"calendars": calendars, "problems": problems}


@router.get("/events")
def get_events(request: Request, start: str, end: str, q: str = "", account: str = "",
               calendar: str = "", machine: sqlite3.Row = Depends(machine_row),
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return service(request).agenda(start, end, q, account, calendar)


@router.post("/events", status_code=201)
def post_event(body: EventBody, request: Request, machine: sqlite3.Row = Depends(machine_row),
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    event = service(request).create_event(body.account, body.calendar, body.fields(), body.source)
    log(conn, machine["id"], body.account, "event created",
        f"{event['summary']} · {event['start']} · {event['calendar']} · {body.source}")
    conn.commit()
    return event


@router.patch("/events")
def patch_event(body: EventUpdate, request: Request, machine: sqlite3.Row = Depends(machine_row),
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    fields = {name: value for name, value in body.fields().items() if value is not None}
    event = service(request).update_event(body.account, body.calendar, body.event_id, fields)
    log(conn, machine["id"], body.account, "event changed",
        f"{event['summary']} · {event['start']} · {event['calendar']} · {body.source}")
    conn.commit()
    return event


@router.post("/event-proposals", status_code=201)
def post_event_proposal(body: EventProposal, request: Request,
                        machine: sqlite3.Row = Depends(machine_row),
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    google = service(request)
    checked = event_body(body.fields(), google.time_zone)
    if not checked.get("summary") or "start" not in checked:
        raise HTTPException(400, "An event needs a summary and a start.")
    # Resolve the calendar now, so that a wrong name fails here and not when
    # the person accepts.
    target = google.find_calendar(body.account, body.calendar)
    proposal_id = conn.execute(
        """INSERT INTO event_proposals (created_at, machine_id, account, calendar_id, calendar_name,
               summary, starts, ends, description, location, private, reason, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (db.now_iso(), machine["id"], body.account, target["id"], target["name"] or "",
         body.summary, body.start, body.end or "", body.description or "", body.location or "",
         int(bool(body.private)), body.reason.strip(), body.source.strip())).lastrowid
    conn.commit()
    return {"id": proposal_id, "status": "pending", "calendar": target["name"], "account": body.account}


@router.get("/mail")
def get_mail(request: Request, q: str, account: str = "", limit: int = 10,
             machine: sqlite3.Row = Depends(machine_row),
             conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    google = service(request)
    messages: list[dict[str, Any]] = []
    problems: list[str] = []
    labels = [account] if account else google.store.labels()
    for label in labels:
        try:
            messages += [{"account": label, **item} for item in google.mail_search(label, q, limit)]
        except GoogleError as exc:
            if account:
                raise
            problems.append(str(exc))
    for label in labels:
        log(conn, machine["id"], label, "mail searched", q)
    conn.commit()
    return {"messages": messages, "problems": problems}


@router.get("/mail/{message_id}")
def get_mail_message(message_id: str, request: Request, account: str,
                     machine: sqlite3.Row = Depends(machine_row),
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    message = service(request).mail_read(account, message_id)
    log(conn, machine["id"], account, "mail read", f"{message['subject']} · {message['from']}")
    conn.commit()
    return {"account": account, **message}
