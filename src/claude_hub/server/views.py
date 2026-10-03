"""Web view: server-rendered pages, plain forms, no client-side framework."""

from __future__ import annotations

import difflib
import json
import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..collector import mcp_server
from ..remote import is_within
from ..rules import FILE_NAMED_KINDS
from ..rulesets import applies, build_briefing, parse_ruleset, split_file, valid_name, with_addition, with_body
from ..transcript import iter_turns
from . import projects as project_rules
from . import store, usage
from .db import now_iso as now_stamp
from .ingest import current_rulesets, get_conn, project_view
from .pricing import load_prices
from .status import (
    ACTION_STATES,
    RECENT_DAYS,
    reltime,
    session_status,
    utcnow,
    worktree_state,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["reltime"] = reltime

TURN_LIMIT = 300
STATE_ORDER = ["dirty", "unpushed", "missing", "stale", "merged", "idle", "active", "main"]

SESSION_COLUMNS = """
    s.*, m.name AS machine, a.label AS account, p.name AS project_name, p.kind AS project_kind,
    COALESCE(p.archived, 0) AS project_archived
"""
SESSION_JOINS = """
    FROM sessions s
    JOIN machines m ON m.id = s.machine_id
    LEFT JOIN accounts a ON a.id = s.account_id
    LEFT JOIN projects p ON p.id = s.project_id
"""


def display_key(key: str) -> str:
    """Show a project key without its internal prefix."""
    prefix, _, rest = key.partition(":")
    if prefix == "git":
        return rest
    machine, _, path = rest.partition(":")
    return f"{path} on {machine}"


templates.env.filters["display_key"] = display_key


def tokens(value: int | None) -> str:
    """Shorten a token count: 1,234,567 becomes 1.2M."""
    value = value or 0
    for limit, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if value >= limit:
            return f"{value / limit:.1f}{unit}"
    return str(value)


def usd(value: float | None) -> str:
    value = value or 0.0
    return f"${value:,.0f}" if value >= 100 else f"${value:,.2f}"


templates.env.filters["tokens"] = tokens
templates.env.filters["usd"] = usd


def back(request: Request, conn: sqlite3.Connection, fallback: str = "/") -> RedirectResponse:
    """Commit the form's changes and return to the page it came from."""
    conn.commit()
    # 303 turns the form POST into a GET, so a reload does not resubmit.
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def load_worktrees(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT w.*, r.project_id, r.path AS repo_path, r.default_branch, r.machine_id,
               m.name AS machine, p.name AS project_name, p.archived AS project_archived
        FROM worktrees w
        JOIN repos r ON r.id = w.repo_id
        JOIN machines m ON m.id = r.machine_id
        JOIN projects p ON p.id = r.project_id
    """
    params: tuple = ()
    if project_id is not None:
        query += " WHERE r.project_id = ?"
        params = (project_id,)
    now = utcnow()
    result = []
    for row in conn.execute(query, params).fetchall():
        item = dict(row)
        item["state"] = worktree_state(row, row["default_branch"], now)
        # Inside the repository, the part below it is enough to recognize a worktree.
        inside = not row["is_main"] and is_within(row["path"], row["repo_path"])
        item["short_path"] = row["path"][len(row["repo_path"]):].lstrip("\\/") if inside else row["path"]
        result.append(item)
    # Newest first, then stable-sort so that work needing a decision leads.
    result.sort(key=lambda w: w["last_commit_at"] or "", reverse=True)
    result.sort(key=lambda w: STATE_ORDER.index(w["state"]))
    return result


def load_sessions(conn: sqlite3.Connection, where: str = "", params: tuple = (), limit: int = 200):
    now = utcnow()
    # Subagent transcripts are stored as sessions, but they are not listed.
    top_level = "s.parent_session_id IS NULL"
    where = f"WHERE {top_level} AND ({where.strip()[6:]})" if where.strip() else f"WHERE {top_level}"
    rows = conn.execute(
        f"SELECT {SESSION_COLUMNS} {SESSION_JOINS} {where} "
        "ORDER BY COALESCE(s.ended_at, s.updated_at) DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["status"] = session_status(row, now)
        result.append(item)
    return result


def project_summaries(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    summaries: dict[int, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM projects").fetchall():
        summaries[row["id"]] = {
            **dict(row),
            "sessions": 0,
            "last_activity": None,
            "states": Counter(),
            "machines": set(),
            "waiting": 0,
        }

    def touch(summary: dict[str, Any], stamp: str | None) -> None:
        if stamp and (summary["last_activity"] is None or stamp > summary["last_activity"]):
            summary["last_activity"] = stamp

    now = utcnow()
    for row in conn.execute(
        "SELECT s.project_id, s.ended_at, s.last_event, s.last_tool, m.name AS machine "
        "FROM sessions s JOIN machines m ON m.id = s.machine_id "
        "WHERE s.project_id IS NOT NULL AND s.parent_session_id IS NULL"
    ).fetchall():
        summary = summaries.get(row["project_id"])
        if summary is None:
            continue
        summary["sessions"] += 1
        summary["machines"].add(row["machine"])
        touch(summary, row["ended_at"])
        if session_status(row, now) == "waiting":
            summary["waiting"] += 1

    for wt in load_worktrees(conn):
        summary = summaries.get(wt["project_id"])
        if summary is None:
            continue
        summary["machines"].add(wt["machine"])
        summary["states"][wt["state"]] += 1
        touch(summary, wt["last_commit_at"])
    return summaries


def targets(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Options for an assignment select: a workspace, or a class inside one."""
    options: list[tuple[str, str]] = []
    for ws in conn.execute("SELECT * FROM workspaces ORDER BY position, name").fetchall():
        options.append((str(ws["id"]), ws["name"]))
        for cls in conn.execute(
            "SELECT * FROM classes WHERE workspace_id = ? ORDER BY position, name", (ws["id"],)
        ).fetchall():
            options.append((f"{ws['id']}:{cls['id']}", f"{ws['name']} / {cls['name']}"))
    return options


def parse_target(conn: sqlite3.Connection, target: str) -> tuple[int | None, int | None]:
    if not target:
        return None, None
    ws_text, _, cls_text = target.partition(":")
    try:
        workspace_id = int(ws_text)
        class_id = int(cls_text) if cls_text else None
    except ValueError as exc:
        raise HTTPException(400, "Invalid target") from exc
    if conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
        raise HTTPException(400, "Unknown workspace")
    if class_id is not None and conn.execute(
        "SELECT 1 FROM classes WHERE id = ? AND workspace_id = ?", (class_id, workspace_id)
    ).fetchone() is None:
        raise HTTPException(400, "Unknown class")
    return workspace_id, class_id


def nav(conn: sqlite3.Connection) -> dict[str, Any]:
    inbox = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE workspace_id IS NULL AND archived = 0").fetchone()[0]
    proposals = conn.execute(
        "SELECT COUNT(*) FROM rule_proposals WHERE status = 'pending'").fetchone()[0]
    return {"inbox_count": inbox, "proposal_count": proposals}


def render(request: Request, conn: sqlite3.Connection, name: str, **context: Any):
    return templates.TemplateResponse(request, name, {"nav": nav(conn), **context})


# --- pages -----------------------------------------------------------------


@router.get("/")
def now_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    cutoff = (utcnow() - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    recent = [s for s in load_sessions(conn, "WHERE s.ended_at >= ?", (cutoff,), limit=500)
              if not s["project_archived"]]
    groups = {status: [s for s in recent if s["status"] == status]
              for status in ("waiting", "running", "paused")}

    attention: dict[int, dict[str, Any]] = {}
    removable = stale = 0
    for wt in load_worktrees(conn):
        if wt["project_archived"]:
            continue
        if wt["state"] in ACTION_STATES:
            entry = attention.setdefault(
                wt["project_id"],
                {"project_id": wt["project_id"], "name": wt["project_name"], "states": Counter()},
            )
            entry["states"][wt["state"]] += 1
        removable += wt["state"] == "merged"
        stale += wt["state"] == "stale"
    return render(
        request, conn, "now.html",
        groups=groups,
        attention=sorted(attention.values(), key=lambda e: -sum(e["states"].values())),
        removable=removable, stale=stale, recent_days=RECENT_DAYS,
    )


# A reply that is older than this no longer counts as "waiting for you" in
# the status feed. The Now page keeps showing it.
STATUS_FRESH_HOURS = 2
STATUS_DETAIL_LENGTH = 200


@router.get("/status.json")
def status_json(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """A short status for other systems, such as a phone widget fed by Home Assistant."""
    now = utcnow()
    cutoff = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = (now - timedelta(hours=STATUS_FRESH_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    recent = [s for s in load_sessions(conn, "WHERE s.ended_at >= ?", (cutoff,), limit=500)
              if not s["project_archived"]]
    groups = {
        "waiting": [s for s in recent if s["status"] == "waiting" and (s["ended_at"] or "") >= fresh],
        "running": [s for s in recent if s["status"] == "running"],
        "paused": [s for s in recent if s["status"] == "paused" and (s["ended_at"] or "") >= fresh],
    }
    labels = {"waiting": "waiting for you", "running": "running", "paused": "stopped mid-turn"}
    headline = ", ".join(f"{len(items)} {labels[name]}" for name, items in groups.items() if items)
    listed = [s for name in ("waiting", "paused", "running") for s in groups[name]]
    detail = " · ".join(f"{s['project_name'] or 'no project'}: {s['title'] or '(no prompt)'}"
                        for s in listed[:3])
    if len(detail) > STATUS_DETAIL_LENGTH:
        detail = detail[:STATUS_DETAIL_LENGTH - 1].rstrip() + "…"
    proposals = conn.execute(
        "SELECT COUNT(*) FROM rule_proposals WHERE status = 'pending'").fetchone()[0]
    if proposals:
        headline = ", ".join(part for part in (
            headline, f"{proposals} rule proposal{'s' if proposals != 1 else ''}") if part)
    return {
        **{name: len(items) for name, items in groups.items()},
        "active": len(listed),
        "proposals": proposals,
        "headline": headline or "Nothing open",
        "detail": detail,
        "sessions": [{"status": s["status"], "project": s["project_name"], "title": s["title"],
                      "machine": s["machine"], "last_activity": s["ended_at"]} for s in listed[:10]],
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/overview")
def overview(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    summaries = project_summaries(conn)

    def ordered(items):
        # Pinned projects lead in the order you gave them. The rest follows by activity.
        items = sorted(items, key=lambda p: p["last_activity"] or "", reverse=True)
        pinned = sorted((p for p in items if p["pin_position"] is not None),
                        key=lambda p: (p["pin_position"], p["id"]))
        for index, project in enumerate(pinned):
            project["pin_first"], project["pin_last"] = index == 0, index == len(pinned) - 1
        return pinned + [p for p in items if p["pin_position"] is None]

    archived = sorted((p for p in summaries.values() if p["archived"]),
                      key=lambda p: p["name"].lower())
    tree = []
    for ws in conn.execute("SELECT * FROM workspaces ORDER BY position, name").fetchall():
        mine = [p for p in summaries.values() if p["workspace_id"] == ws["id"] and not p["archived"]]
        classes = []
        for cls in conn.execute(
            "SELECT * FROM classes WHERE workspace_id = ? ORDER BY position, name", (ws["id"],)
        ).fetchall():
            classes.append({"name": cls["name"],
                            "projects": ordered(p for p in mine if p["class_id"] == cls["id"])})
        unclassed = [p for p in mine if p["class_id"] is None]
        # Plain folders are the "random chats" of a workspace, so they get their own group.
        classes.append({"name": "Projects without class",
                        "projects": ordered(p for p in unclassed if p["kind"] == "repo")})
        classes.append({"name": "Loose chats",
                        "projects": ordered(p for p in unclassed if p["kind"] == "dir")})
        tree.append({"name": ws["name"], "total": len(mine),
                     "classes": [c for c in classes if c["projects"]]})
    return render(request, conn, "overview.html", tree=tree, archived=archived)


@router.get("/inbox")
def inbox(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    items = [p for p in project_summaries(conn).values()
             if p["workspace_id"] is None and not p["archived"]]
    items.sort(key=lambda p: p["last_activity"] or "", reverse=True)
    return render(request, conn, "inbox.html", projects=items, targets=targets(conn))


@router.get("/projects/{project_id}")
def project_page(project_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    project = conn.execute(
        """SELECT p.*, w.name AS workspace, c.name AS class FROM projects p
           LEFT JOIN workspaces w ON w.id = p.workspace_id
           LEFT JOIN classes c ON c.id = p.class_id WHERE p.id = ?""",
        (project_id,),
    ).fetchone()
    if project is None:
        raise HTTPException(404, "Unknown project")
    sessions = load_sessions(conn, "WHERE s.project_id = ?", (project_id,), limit=500)
    worktrees = load_worktrees(conn, project_id)
    for wt in worktrees:
        wt["sessions"] = []
    for session in sessions:
        # Linked worktrees can live inside the main one, so the longest match wins.
        candidates = [wt for wt in worktrees if wt["machine_id"] == session["machine_id"]
                      and is_within(session["cwd"], wt["path"])]
        if candidates:
            max(candidates, key=lambda wt: len(wt["path"]))["sessions"].append(session)
    current = ""
    if project["workspace_id"]:
        current = str(project["workspace_id"])
        if project["class_id"]:
            current += f":{project['class_id']}"
    rulesets = current_rulesets(request.app)
    view = project_view(conn, project_id)
    mine = [rs for rs in rulesets if applies(rs, view)]
    briefing = build_briefing(rulesets, view)
    spent = usage.for_project(conn, load_prices(request.app.state.pricing_file), project_id, 30)
    others = conn.execute(
        "SELECT id, name, key FROM projects WHERE id != ? AND archived = 0 ORDER BY name COLLATE NOCASE",
        (project_id,)).fetchall()
    aliases = conn.execute(
        "SELECT * FROM project_aliases WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
    return render(request, conn, "project.html", project=project, sessions=sessions,
                  worktrees=worktrees, targets=targets(conn), current_target=current,
                  rulesets=mine, briefing=briefing, spent=spent, others=others, aliases=aliases)


@router.get("/sessions")
def sessions_page(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    where, params = "", ()
    if q.strip():
        like = f"%{q.strip()}%"
        where = "WHERE s.title LIKE ? OR s.first_prompt LIKE ? OR s.last_prompt LIKE ? OR p.name LIKE ?"
        params = (like, like, like, like)
    return render(request, conn, "sessions.html",
                  sessions=load_sessions(conn, where, params, limit=300), q=q)


@router.get("/sessions/{pk}")
def session_page(pk: int, request: Request, all: int = 0,
                 conn: sqlite3.Connection = Depends(get_conn)):
    found = load_sessions(conn, "WHERE s.pk = ?", (pk,), limit=1)
    if not found:
        raise HTTPException(404, "Unknown session")
    session = found[0]
    path = store.transcript_path(request.app.state.data_dir, session["machine_id"], session["session_id"])
    turns = list(iter_turns(store.read_lines(path)))
    hidden = 0
    if not all and len(turns) > TURN_LIMIT:
        hidden = len(turns) - TURN_LIMIT
        turns = turns[-TURN_LIMIT:]
    spent = usage.for_session(conn, load_prices(request.app.state.pricing_file), pk)
    choices = conn.execute(
        "SELECT id, name FROM projects WHERE archived = 0 OR id = ? ORDER BY name COLLATE NOCASE",
        (session["project_id"],)).fetchall()
    return render(request, conn, "session.html", session=session, turns=turns, hidden=hidden,
                  spent=spent, choices=choices)


@router.get("/usage")
def usage_page(request: Request, days: int = 30, conn: sqlite3.Connection = Depends(get_conn)):
    if days not in usage.PERIODS:
        days = 30
    prices = load_prices(request.app.state.pricing_file)
    return render(request, conn, "usage.html", days=days, periods=usage.PERIODS, prices=prices,
                  report=usage.report(conn, prices, days), pricing_file=request.app.state.pricing_file)


@router.get("/worktrees")
def worktrees_page(request: Request, state: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    items = load_worktrees(conn)
    counts = Counter(wt["state"] for wt in items)
    if state:
        items = [wt for wt in items if wt["state"] == state]
    return render(request, conn, "worktrees.html", worktrees=items, counts=counts, state=state)


@router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    workspaces = []
    for ws in conn.execute("SELECT * FROM workspaces ORDER BY position, name").fetchall():
        classes = conn.execute(
            "SELECT * FROM classes WHERE workspace_id = ? ORDER BY position, name", (ws["id"],)
        ).fetchall()
        workspaces.append({**dict(ws), "classes": classes})
    rules = conn.execute(
        """SELECT r.*, w.name AS workspace, c.name AS class FROM rules r
           JOIN workspaces w ON w.id = r.workspace_id
           LEFT JOIN classes c ON c.id = r.class_id ORDER BY r.position, r.id"""
    ).fetchall()
    machines = conn.execute(
        """SELECT m.*, u.name AS user,
                  (SELECT COUNT(*) FROM sessions s
                    WHERE s.machine_id = m.id AND s.parent_session_id IS NULL) AS sessions,
                  (SELECT COUNT(*) FROM repos r WHERE r.machine_id = m.id) AS repos
           FROM machines m JOIN users u ON u.id = m.user_id ORDER BY m.name"""
    ).fetchall()
    accounts = conn.execute(
        """SELECT a.*, u.name AS user,
                  (SELECT COUNT(*) FROM sessions s
                    WHERE s.account_id = a.id AND s.parent_session_id IS NULL) AS sessions
           FROM accounts a JOIN users u ON u.id = a.user_id ORDER BY a.label"""
    ).fetchall()
    return render(request, conn, "settings.html", workspaces=workspaces, rules=rules,
                  machines=machines, accounts=accounts, targets=targets(conn))


@router.get("/settings/connector")
def connector_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """How to set up the local connector, with what the hub knows about its use."""
    machines = conn.execute(
        "SELECT name, platform, last_seen, connector_seen FROM machines ORDER BY name").fetchall()
    # The entry that "mcp install" writes, with a placeholder for the one
    # value that differs per machine.
    entry = {"mcpServers": {mcp_server.SERVER_NAME: {
        "command": "PYTHON_PATH", "args": ["-m", "claude_hub.collector.cli", "mcp"]}}}
    return render(request, conn, "settings_connector.html", machines=machines,
                  tools=mcp_server.TOOLS, instructions=mcp_server.INSTRUCTIONS,
                  server_name=mcp_server.SERVER_NAME, entry=json.dumps(entry, indent=2))


KIND_LABELS = {
    "claude_md": "CLAUDE.md",
    "conventions": "Conventions",
    "import": "Imported file",
    "rule": "Rule file",
    "skill": "Skill",
    "agent": "Subagent",
    "command": "Command",
    "output_style": "Output style",
}
templates.env.globals["kind_labels"] = KIND_LABELS


def load_rule_files(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT f.id, f.scope, f.kind, f.name, f.rel_path, f.base_path, f.description,
                  f.applies_to, f.sha, f.bytes, f.modified_at, f.project_id, f.machine_id,
                  m.name AS machine, a.label AS account, p.name AS project_name,
                  (SELECT COUNT(*) FROM rule_items i WHERE i.file_id = f.id) AS rule_count
           FROM rule_files f
           JOIN machines m ON m.id = f.machine_id
           LEFT JOIN accounts a ON a.id = f.account_id
           LEFT JOIN projects p ON p.id = f.project_id"""
    ).fetchall()
    files = [dict(row) for row in rows]
    # Files with the same kind and name are copies of one another. Every
    # CLAUDE.md shares its name, so those are compared rule by rule instead.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in files:
        if item["kind"] not in FILE_NAMED_KINDS:
            groups.setdefault((item["kind"], item["name"].lower()), []).append(item)
    for item in files:
        others = [o for o in groups.get((item["kind"], item["name"].lower()), []) if o is not item]
        item["identical"] = sum(o["sha"] == item["sha"] for o in others)
        item["differing"] = len(others) - item["identical"]
    files.sort(key=lambda f: (f["scope"] != "user", (f["project_name"] or "").lower(),
                              f["kind"], f["name"].lower()))
    return files


@router.get("/rules")
def rules_page(request: Request, kind: str = "", project: int = 0, q: str = "",
               conn: sqlite3.Connection = Depends(get_conn)):
    files = load_rule_files(conn)
    counts = Counter(item["kind"] for item in files)
    total_items = sum(item["rule_count"] for item in files)
    if kind:
        files = [item for item in files if item["kind"] == kind]
    if project:
        files = [item for item in files if item["project_id"] == project]
    if q.strip():
        like = f"%{q.strip()}%"
        hits = {row["id"] for row in conn.execute(
            "SELECT id FROM rule_files WHERE content LIKE ? OR name LIKE ?", (like, like))}
        files = [item for item in files if item["id"] in hits]
    drifted = sum(1 for item in files if item["differing"])
    return render(request, conn, "rules.html", files=files, counts=counts, kind=kind, q=q,
                  project=project, total_items=total_items, drifted=drifted)


@router.get("/rules/{file_id}")
def rule_file_page(file_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    files = load_rule_files(conn)
    current = next((item for item in files if item["id"] == file_id), None)
    if current is None:
        raise HTTPException(404, "Unknown instruction file")
    copies = [item for item in files if item is not current and item["kind"] not in FILE_NAMED_KINDS
              and (item["kind"], item["name"].lower()) == (current["kind"], current["name"].lower())]
    by_id = {item["id"]: item for item in files}
    items = [dict(row) for row in conn.execute(
        "SELECT * FROM rule_items WHERE file_id = ? ORDER BY position", (file_id,))]
    for item in items:
        # The same rule text in other files, found by its normalized hash.
        twins = conn.execute(
            "SELECT DISTINCT file_id FROM rule_items WHERE sha = ? AND file_id != ?",
            (item["sha"], file_id)).fetchall()
        item["also_in"] = [by_id[row["file_id"]] for row in twins if row["file_id"] in by_id]
    return render(request, conn, "rule_file.html", file=current, copies=copies, items=items)


@router.get("/rulesets")
def rulesets_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    rulesets = current_rulesets(request.app)
    imported = conn.execute("SELECT COUNT(*) FROM rule_files").fetchone()[0]
    rows = []
    for row in conn.execute(
        """SELECT p.id FROM projects p
           WHERE (p.kind = 'repo' OR p.workspace_id IS NOT NULL) AND p.archived = 0
           ORDER BY p.name"""
    ).fetchall():
        project = project_view(conn, row["id"])
        briefing = build_briefing(rulesets, project)
        on_demand = sum(rs.tokens for rs in rulesets if rs.name in briefing["on_demand"])
        rows.append({**project, "start_tokens": briefing["tokens"],
                     "always": len(briefing["always"]), "on_demand": len(briefing["on_demand"]),
                     "on_demand_tokens": on_demand})
    return render(request, conn, "rulesets.html", rulesets=rulesets, imported=imported,
                  projects=rows, folder=request.app.state.rulesets_dir)


@router.get("/rulesets/{name}")
def ruleset_page(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ruleset = next((rs for rs in current_rulesets(request.app)
                    if rs.name == name), None)
    if ruleset is None:
        raise HTTPException(404, "Unknown rule set")
    return render(request, conn, "ruleset.html", ruleset=ruleset)


@router.get("/projects/{project_id}/briefing")
def project_briefing_page(project_id: int, request: Request,
                          conn: sqlite3.Connection = Depends(get_conn)):
    project = project_view(conn, project_id)
    if project is None:
        raise HTTPException(404, "Unknown project")
    briefing = build_briefing(current_rulesets(request.app), project)
    return render(request, conn, "briefing.html", project=project, briefing=briefing)


# --- forms -----------------------------------------------------------------


@router.post("/projects/{project_id}/assign")
def assign_project(project_id: int, request: Request, target: str = Form(""),
                   owner_rule: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(404, "Unknown project")
    workspace_id, class_id = parse_target(conn, target)
    conn.execute(
        "UPDATE projects SET workspace_id = ?, class_id = ?, assigned_by = ? WHERE id = ?",
        (workspace_id, class_id, "manual" if workspace_id else None, project_id),
    )
    if owner_rule and workspace_id and project["key"].startswith("git:"):
        # "host/owner/*" sorts every repository of the same owner from now on.
        pattern = project["key"][4:].rsplit("/", 1)[0] + "/*"
        exists = conn.execute(
            "SELECT 1 FROM rules WHERE field = 'remote' AND pattern = ?", (pattern,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO rules (position, field, pattern, workspace_id, class_id) "
                "VALUES ((SELECT COALESCE(MAX(position), 0) + 1 FROM rules), 'remote', ?, ?, ?)",
                (pattern, workspace_id, class_id),
            )
        project_rules.apply_rules(conn)
    return back(request, conn, f"/projects/{project_id}")


@router.post("/projects/{project_id}/rename")
def rename_project(project_id: int, request: Request, name: str = Form(...),
                   conn: sqlite3.Connection = Depends(get_conn)):
    if name.strip():
        conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name.strip(), project_id))
    return back(request, conn, f"/projects/{project_id}")


@router.post("/settings/workspaces")
def add_workspace(request: Request, name: str = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn)):
    if name.strip():
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (name, position) "
            "VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM workspaces))",
            (name.strip(),),
        )
    return back(request, conn, "/settings")


@router.post("/settings/workspaces/{workspace_id}/delete")
def delete_workspace(workspace_id: int, request: Request,
                     conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    # Projects of a deleted workspace return to the inbox as unassigned.
    conn.execute("UPDATE projects SET assigned_by = NULL WHERE workspace_id IS NULL")
    return back(request, conn, "/settings")


@router.post("/settings/classes")
def add_class(request: Request, workspace_id: int = Form(...), name: str = Form(...),
              conn: sqlite3.Connection = Depends(get_conn)):
    if name.strip():
        conn.execute(
            "INSERT OR IGNORE INTO classes (workspace_id, name, position) "
            "VALUES (?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM classes))",
            (workspace_id, name.strip()),
        )
    return back(request, conn, "/settings")


@router.post("/settings/classes/{class_id}/delete")
def delete_class(class_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM classes WHERE id = ?", (class_id,))
    return back(request, conn, "/settings")


@router.post("/settings/rules")
def add_rule(request: Request, field: str = Form(...), pattern: str = Form(...),
             target: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
    if field not in ("remote", "path"):
        raise HTTPException(400, "Unknown rule field")
    workspace_id, class_id = parse_target(conn, target)
    if pattern.strip() and workspace_id:
        conn.execute(
            "INSERT INTO rules (position, field, pattern, workspace_id, class_id) "
            "VALUES ((SELECT COALESCE(MAX(position), 0) + 1 FROM rules), ?, ?, ?, ?)",
            (field, pattern.strip(), workspace_id, class_id),
        )
        project_rules.apply_rules(conn)
    return back(request, conn, "/settings")


@router.post("/settings/rules/{rule_id}/delete")
def delete_rule(rule_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return back(request, conn, "/settings")


@router.post("/settings/rules/apply")
def apply_rules(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    project_rules.apply_rules(conn)
    return back(request, conn, "/settings")


@router.post("/projects/{project_id}/session-rules")
def set_session_rules(project_id: int, request: Request, hub_rules: str = Form(""),
                      ai_ok: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("UPDATE projects SET hub_rules = ?, ai_ok = ? WHERE id = ?",
                 (1 if hub_rules == "1" else 0, 1 if ai_ok == "1" else 0, project_id))
    return back(request, conn, f"/projects/{project_id}")


@router.post("/projects/{project_id}/archive")
def archive_project(project_id: int, request: Request, archived: str = Form(""),
                    conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("UPDATE projects SET archived = ? WHERE id = ?",
                 (1 if archived == "1" else 0, project_id))
    return back(request, conn, f"/projects/{project_id}")


@router.post("/projects/{project_id}/pin")
def pin_project(project_id: int, request: Request, action: str = Form(...),
                conn: sqlite3.Connection = Depends(get_conn)):
    if action not in ("pin", "unpin", "up", "down"):
        raise HTTPException(400, "Unknown action")
    project_rules.move_pin(conn, project_id, action)
    return back(request, conn, "/overview")


@router.post("/projects/{project_id}/merge")
def merge_project(project_id: int, target: int = Form(...),
                  conn: sqlite3.Connection = Depends(get_conn)):
    try:
        project_rules.merge_projects(conn, project_id, target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    conn.commit()
    # The page of the merged project is gone, so the target is the place to return to.
    return RedirectResponse(f"/projects/{target}", status_code=303)


@router.post("/projects/{project_id}/aliases/delete")
def delete_alias(project_id: int, request: Request, key: str = Form(...),
                 conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM project_aliases WHERE project_id = ? AND key = ?", (project_id, key))
    return back(request, conn, f"/projects/{project_id}")


@router.post("/sessions/{pk}/project")
def move_session(pk: int, request: Request, project: str = Form(""),
                 conn: sqlite3.Connection = Depends(get_conn)):
    if not project:
        # Back to automatic: the next upload or re-parse assigns the session again.
        conn.execute("UPDATE sessions SET project_manual = 0 WHERE pk = ?", (pk,))
        return back(request, conn, f"/sessions/{pk}")
    try:
        project_id = int(project)
    except ValueError as exc:
        raise HTTPException(400, "Invalid project") from exc
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise HTTPException(400, "Unknown project")
    conn.execute("UPDATE sessions SET project_id = ?, project_manual = 1 WHERE pk = ?",
                 (project_id, pk))
    return back(request, conn, f"/sessions/{pk}")


# --- rule sets: edits and proposals ----------------------------------------


def save_live_ruleset(request: Request, name: str, text: str) -> None:
    """Store a rule set in the hub. It applies from the next session start."""
    if not valid_name(name):
        raise HTTPException(400, "Invalid rule set name")
    folder: Path = request.app.state.live_rulesets_dir
    folder.mkdir(parents=True, exist_ok=True)
    text = text.replace("\r\n", "\n").strip() + "\n"
    (folder / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")


def find_ruleset(request: Request, name: str):
    return next((rs for rs in current_rulesets(request.app) if rs.name == name), None)


def proposal_result(ruleset, mode: str, text: str) -> str:
    """The file that a rule set becomes when a proposal is accepted."""
    return with_body(ruleset.text, text) if mode == "replace" else with_addition(ruleset.text, text)


def load_proposals(conn: sqlite3.Connection, request: Request, status: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.*, m.name AS machine, p.name AS project_name FROM rule_proposals r
           LEFT JOIN machines m ON m.id = r.machine_id
           LEFT JOIN projects p ON p.id = r.project_id
           WHERE (r.status = 'pending') = ? ORDER BY r.created_at DESC LIMIT 200""",
        (status == "pending",),
    ).fetchall()
    sets = {rs.name: rs for rs in current_rulesets(request.app)}
    result = []
    for row in rows:
        item = dict(row)
        ruleset = sets.get(row["ruleset"])
        item["title"] = ruleset.title if ruleset else row["ruleset"]
        item["missing"] = ruleset is None
        item["diff"] = ""
        if ruleset and row["mode"] == "replace" and row["status"] == "pending":
            # A replacement is hard to judge as a whole, so the page shows what changes.
            item["diff"] = "\n".join(difflib.unified_diff(
                split_file(ruleset.text)[1].strip().splitlines(), row["text"].splitlines(),
                "now", "proposed", lineterm="", n=2))
        result.append(item)
    return result


@router.get("/proposals")
def proposals_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "proposals.html",
                  pending=load_proposals(conn, request, "pending"),
                  decided=load_proposals(conn, request, "decided"))


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: int, request: Request, text: str = Form(""),
                    conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM rule_proposals WHERE id = ? AND status = 'pending'",
                       (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown proposal, or it is already decided")
    ruleset = find_ruleset(request, row["ruleset"])
    if ruleset is None:
        raise HTTPException(409, "The rule set of this proposal no longer exists")
    # The person may have edited the proposed text before accepting it.
    applied = (text.replace("\r\n", "\n").strip() or row["text"])
    save_live_ruleset(request, ruleset.name, proposal_result(ruleset, row["mode"], applied))
    conn.execute(
        "UPDATE rule_proposals SET status = 'accepted', decided_at = ?, applied_text = ? WHERE id = ?",
        (now_stamp(), applied, proposal_id))
    return back(request, conn, "/proposals")


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, request: Request,
                    conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute(
        "UPDATE rule_proposals SET status = 'rejected', decided_at = ? "
        "WHERE id = ? AND status = 'pending'", (now_stamp(), proposal_id))
    return back(request, conn, "/proposals")


@router.post("/rulesets/{name}/edit")
def edit_ruleset(name: str, request: Request, text: str = Form(...),
                 conn: sqlite3.Connection = Depends(get_conn)):
    if find_ruleset(request, name) is None:
        raise HTTPException(404, "Unknown rule set")
    if not text.strip():
        raise HTTPException(400, "The text is empty")
    save_live_ruleset(request, name, text)
    return back(request, conn, f"/rulesets/{name}")


@router.post("/rulesets/{name}/reset")
def reset_ruleset(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Drop the edit made in the hub and return to the repository version."""
    ruleset = find_ruleset(request, name)
    if ruleset is None or not valid_name(name):
        raise HTTPException(404, "Unknown rule set")
    if not ruleset.in_repo:
        raise HTTPException(409, "This rule set exists only in the hub")
    (request.app.state.live_rulesets_dir / f"{name}.md").unlink(missing_ok=True)
    return back(request, conn, f"/rulesets/{name}")
