"""API for sessions and collectors: status, routines, handoffs, and waking machines."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import __version__
from . import db, handoffs, projects, routines, wake
from .google import local_today
from .ingest import current_machine, get_conn

router = APIRouter(prefix="/api/v1")


def today(request: Request):
    return local_today(request.app.state.google.time_zone)


def project_of(conn: sqlite3.Connection, machine: sqlite3.Row, cwd: str, remote: str,
               repo_root: str) -> sqlite3.Row:
    """The project that a folder belongs to. Handoffs are kept per project."""
    project_id = projects.resolve_session_project(
        conn, machine, cwd or None, remote or None, repo_root or None)
    row = conn.execute("SELECT id, name FROM projects WHERE id = ?", (project_id,)).fetchone() \
        if project_id else None
    if row is None:
        raise HTTPException(404, "The hub knows no project for this folder. Give a folder inside a "
                                 "repository or a project folder.")
    return row


# --- status ----------------------------------------------------------------


@router.get("/status")
def get_status(request: Request, machine: sqlite3.Row = Depends(current_machine),
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """What a session needs to see the state of the hub without a browser."""
    from .views import status_json  # the pages import this module's siblings

    feed = status_json(request, conn)
    machines = [dict(row) for row in conn.execute(
        "SELECT name, platform, last_seen, connector_seen FROM machines ORDER BY name")]
    pending = conn.execute(
        "SELECT id, ruleset, mode, source, created_at FROM rule_proposals WHERE status = 'pending' "
        "ORDER BY id").fetchall()
    conn.commit()
    return {
        "server_version": __version__,
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "machine": machine["name"],
        "feed": feed,
        "machines": machines,
        "google_accounts": request.app.state.google.store.public(),
        "rule_proposals": [dict(row) for row in pending],
        "open_handoffs": handoffs.open_count(conn),
    }


# --- routines ----------------------------------------------------------------


class RoutineDraft(BaseModel):
    name: str
    every: int
    unit: str = "months"
    next_due: str
    anchor: str = "done"
    lead_days: int = 14
    note: str = ""
    mail_to: str = ""
    mail_subject: str = ""
    mail_text: str = ""
    source: str = Field(default="", max_length=200)


@router.get("/routines")
def get_routines(request: Request, machine: sqlite3.Row = Depends(current_machine),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    conn.commit()
    return {"routines": routines.load(conn, today(request)), "today": today(request).isoformat()}


@router.post("/routines", status_code=201)
def post_routine(body: RoutineDraft, machine: sqlite3.Row = Depends(current_machine),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """File a routine as a draft. It reminds of nothing until the person turns it on."""
    try:
        routine_id = routines.create(conn, body.model_dump(), db.now_iso(),
                                     source=body.source or machine["name"], enabled=False)
    except routines.RoutineError as exc:
        raise HTTPException(400, str(exc)) from exc
    conn.commit()
    return {"id": routine_id, "enabled": False}


# --- handoffs ----------------------------------------------------------------


class HandoffBody(BaseModel):
    title: str
    text: str
    from_lane: str = ""
    to_lane: str = ""
    cwd: str = ""
    remote: str = ""
    repo_root: str = ""


class HandoffDone(BaseModel):
    result: str = ""


def _refuse(exc: handoffs.HandoffError) -> HTTPException:
    return HTTPException(404 if str(exc).startswith("No handoff") else 400, str(exc))


@router.post("/handoffs", status_code=201)
def post_handoff(body: HandoffBody, machine: sqlite3.Row = Depends(current_machine),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    project = project_of(conn, machine, body.cwd, body.remote, body.repo_root)
    try:
        handoff_id = handoffs.create(conn, project["id"], body.title, body.text, body.from_lane,
                                     body.to_lane, db.now_iso(), machine["id"])
    except handoffs.HandoffError as exc:
        raise _refuse(exc) from exc
    conn.commit()
    return {"id": handoff_id, "project": project["name"]}


@router.get("/handoffs")
def get_handoffs(cwd: str = "", remote: str = "", repo_root: str = "", status: str = "open",
                 machine: sqlite3.Row = Depends(current_machine),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if status not in (*handoffs.STATES, "all"):
        raise HTTPException(400, f"status is one of {', '.join(handoffs.STATES)}, or all.")
    project = project_of(conn, machine, cwd, remote, repo_root)
    rows = handoffs.listing(conn, project["id"], status)
    conn.commit()
    return {"project": project["name"], "handoffs": [handoffs.brief(row) for row in rows]}


@router.get("/handoffs/new")
def get_new_handoffs(session: str, cwd: str = "", remote: str = "", repo_root: str = "",
                     machine: sqlite3.Row = Depends(current_machine),
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Open handoffs that the session has not been shown yet. Each is returned once per session."""
    try:
        project = project_of(conn, machine, cwd, remote, repo_root)
    except HTTPException:
        conn.commit()
        return {"project": None, "handoffs": []}
    rows = handoffs.new_for_session(conn, project["id"], session[:200], db.now_iso())
    conn.commit()
    return {"project": project["name"], "handoffs": [handoffs.brief(row) for row in rows]}


@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: int, take: int = 0, by: str = "",
                machine: sqlite3.Row = Depends(current_machine),
                conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        before = handoffs.get(conn, handoff_id)
        row = handoffs.take(conn, handoff_id, by, db.now_iso()) if take else before
    except handoffs.HandoffError as exc:
        raise _refuse(exc) from exc
    conn.commit()
    # Tell a session that came second, so that two sessions do not do the same work.
    return {**handoffs.brief(row, full=True), "taken_now": bool(take) and before["status"] == "open"}


@router.post("/handoffs/{handoff_id}/done")
def post_handoff_done(handoff_id: int, body: HandoffDone,
                      machine: sqlite3.Row = Depends(current_machine),
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        row = handoffs.finish(conn, handoff_id, body.result, db.now_iso())
    except handoffs.HandoffError as exc:
        raise _refuse(exc) from exc
    conn.commit()
    return handoffs.brief(row)


# --- wake ----------------------------------------------------------------


def wake_machine(request: Request, conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    target = conn.execute("SELECT * FROM machines WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if target is None:
        names = ", ".join(row["name"] for row in conn.execute("SELECT name FROM machines ORDER BY name"))
        raise HTTPException(404, f"No machine '{name}'. Known: {names}.")
    mac = wake.normalize(target["wake_mac"] or "") or wake.likely_adapter(wake.adapters_of(target))
    if not mac:
        raise HTTPException(409, f"The hub does not know which network adapter wakes '{target['name']}'. "
                                 "Pick it under Settings > Devices.")
    sender = getattr(request.app.state, "wake_sender", wake.send)
    try:
        sender(mac, os.environ.get("HUB_WAKE_BROADCAST", "255.255.255.255"))
    except OSError as exc:
        raise HTTPException(502, f"The wake signal could not be sent: {exc}") from exc
    return {"machine": target["name"], "mac": mac, "last_seen": target["last_seen"]}


@router.post("/machines/{name}/wake")
def post_wake(name: str, request: Request, machine: sqlite3.Row = Depends(current_machine),
              conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    conn.commit()
    return wake_machine(request, conn, name)
