"""Pages for routines, handoffs, and devices."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from . import handoffs, routines, wake
from .db import now_iso as now_stamp
from .google import GoogleError, local_today
from .hub_api import wake_machine
from .ingest import get_conn
from .views import back, hidden_names, hiding, render

router = APIRouter()


def today(request: Request):
    return local_today(request.app.state.google.time_zone)


# --- routines ----------------------------------------------------------------


ROUTINE_FIELDS = ("name", "note", "every", "unit", "anchor", "lead_days", "next_due", "mail_to",
                  "mail_subject", "mail_text", "mail_mode", "mail_account")


async def routine_form(request: Request) -> tuple[dict[str, Any], bool]:
    form = await request.form()
    return {name: form.get(name) for name in ROUTINE_FIELDS}, form.get("enabled") == "1"


@router.get("/routines")
def routines_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    items = routines.load(conn, today(request))
    log = conn.execute(
        """SELECT l.*, r.name FROM routine_log l JOIN routines r ON r.id = l.routine_id
           ORDER BY l.id DESC LIMIT 15""").fetchall()
    return render(request, conn, "routines.html", hidden=hiding(request), log=log,
                  due=[item for item in items if item["state"] in ("due", "overdue")],
                  routines=items, today=today(request).isoformat(), units=routines.UNITS,
                  senders=[a for a in request.app.state.google.store.public() if a["can_send"]])


@router.post("/routines")
async def add_routine(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    fields, enabled = await routine_form(request)
    try:
        routines.create(conn, fields, now_stamp(), source="hub", enabled=enabled)
    except routines.RoutineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return back(request, conn, "/routines")


@router.post("/routines/{routine_id}")
async def edit_routine(routine_id: int, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn)):
    fields, enabled = await routine_form(request)
    try:
        routines.update(conn, routine_id, fields, enabled)
    except routines.RoutineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return back(request, conn, "/routines")


@router.post("/routines/{routine_id}/delete")
def delete_routine(routine_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
    return back(request, conn, "/routines")


@router.post("/routines/{routine_id}/{action}")
def close_routine(routine_id: int, action: str, request: Request, day: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
    if action not in ("done", "skip", "send"):
        raise HTTPException(404, "Unknown action")
    try:
        if action == "send":
            try:
                routines.send(conn, request.app.state.google, routine_id, today(request), now_stamp())
            except GoogleError:
                # The reason is stored with the routine, and the page shows it.
                pass
        else:
            when = routines.parse_day(day) if day.strip() else today(request)
            routines.close(conn, routine_id, action, when, now_stamp())
    except routines.RoutineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return back(request, conn, "/routines")


# --- handoffs ----------------------------------------------------------------


@router.get("/handoffs")
def handoffs_page(request: Request, show: str = "open", conn: sqlite3.Connection = Depends(get_conn)):
    mask = hidden_names(request, conn)
    wanted = ("open", "taken") if show != "all" else handoffs.STATES
    rows = conn.execute(
        f"""SELECT h.*, p.name AS project_name, m.name AS machine FROM handoffs h
            JOIN projects p ON p.id = h.project_id LEFT JOIN machines m ON m.id = h.machine_id
            WHERE h.status IN ({', '.join('?' * len(wanted))})
            ORDER BY h.status = 'done', h.id DESC LIMIT 200""", wanted).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if row["project_id"] in mask:
            # A private project keeps its handoffs to itself while names are hidden.
            item.update(project_name=mask[row["project_id"]], title="(hidden)", text="", result="")
        items.append(item)
    choices = conn.execute(
        "SELECT id, name FROM projects WHERE archived = 0 ORDER BY name COLLATE NOCASE").fetchall()
    return render(request, conn, "handoffs.html", handoffs=items, show=show,
                  projects=[p for p in choices if p["id"] not in mask])


@router.post("/handoffs")
def add_handoff(request: Request, project: int = Form(...), title: str = Form(...),
                text: str = Form(...), to_lane: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project,)).fetchone() is None:
        raise HTTPException(400, "Unknown project")
    try:
        handoffs.create(conn, project, title, text, "robin", to_lane, now_stamp())
    except handoffs.HandoffError as exc:
        raise HTTPException(400, str(exc)) from exc
    return back(request, conn, "/handoffs")


@router.post("/handoffs/{handoff_id}/{action}")
def change_handoff(handoff_id: int, action: str, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
    if action == "done":
        conn.execute("UPDATE handoffs SET status = 'done', done_at = ? WHERE id = ?",
                     (now_stamp(), handoff_id))
    elif action == "reopen":
        # Sessions that saw it before are shown it again.
        conn.execute("UPDATE handoffs SET status = 'open', taken_at = NULL, taken_by = '', done_at = NULL "
                     "WHERE id = ?", (handoff_id,))
        conn.execute("DELETE FROM handoff_deliveries WHERE handoff_id = ?", (handoff_id,))
    elif action == "delete":
        conn.execute("DELETE FROM handoffs WHERE id = ?", (handoff_id,))
    else:
        raise HTTPException(404, "Unknown action")
    return back(request, conn, "/handoffs")


# --- devices ----------------------------------------------------------------


@router.get("/settings/devices")
def devices_page(request: Request, woke: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    machines = []
    for row in conn.execute("SELECT * FROM machines ORDER BY name").fetchall():
        adapters = wake.adapters_of(row)
        chosen = wake.normalize(row["wake_mac"] or "")
        machines.append({**dict(row), "adapters": adapters, "chosen": chosen,
                         "guess": "" if chosen else wake.likely_adapter(adapters)})
    return render(request, conn, "settings_devices.html", machines=machines, woke=woke)


@router.post("/settings/devices/{machine_id}/adapter")
def set_adapter(machine_id: int, request: Request, mac: str = Form(""), other: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    value = (other or mac).strip()
    address = wake.normalize(value)
    if value and not address:
        raise HTTPException(400, f"Not a hardware address: {value!r}. Use the form aa:bb:cc:dd:ee:ff.")
    conn.execute("UPDATE machines SET wake_mac = ? WHERE id = ?", (address or None, machine_id))
    return back(request, conn, "/settings/devices")


@router.post("/settings/devices/{machine_id}/wake")
def wake_from_page(machine_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT name FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown machine")
    wake_machine(request, conn, row["name"])
    conn.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/settings/devices?woke={row['name']}", status_code=303)
