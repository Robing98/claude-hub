"""API for collectors: session uploads and the repository inventory."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..rules import describe
from ..rulesets import build_briefing, load_rulesets
from ..transcript import PARSER_VERSION, parse_meta
from . import db, projects, store

router = APIRouter(prefix="/api/v1")

MAX_CHUNK_BYTES = 32 * 1024 * 1024


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a connection for one request.

    Endpoints that write must call ``conn.commit()`` before they return.
    FastAPI can run this cleanup after the response is sent, and a collector
    that sends its next chunk at once must already see the previous one.
    """
    conn = db.connect(request.app.state.data_dir)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_machine(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "Missing bearer token")
    machine = conn.execute(
        "SELECT * FROM machines WHERE token_hash = ?", (hash_token(token.strip()),)
    ).fetchone()
    if machine is None:
        raise HTTPException(401, "Unknown token")
    # This write also takes the database write lock for the rest of the request.
    # Two uploads for the same session therefore cannot interleave.
    conn.execute("UPDATE machines SET last_seen = ? WHERE id = ?", (db.now_iso(), machine["id"]))
    return machine


class Hello(BaseModel):
    platform: str | None = None


class AppendBody(BaseModel):
    offset: int = Field(ge=0)
    data: str
    head_sha: str | None = None
    truncate: bool = False
    final: bool = True
    account: str = "default"
    config_dir: str | None = None
    source_path: str | None = None
    cwd: str | None = None
    remote: str | None = None
    repo_root: str | None = None
    # Set for a subagent transcript: the session that started the subagent.
    parent: str | None = None


class WorktreeIn(BaseModel):
    path: str
    is_main: bool = False
    branch: str | None = None
    head: str | None = None
    last_commit_at: str | None = None
    last_commit_subject: str | None = None
    dirty_files: int = 0
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    merged: bool = False
    locked: bool = False
    prunable: bool = False


class RepoIn(BaseModel):
    path: str
    remote: str | None = None
    default_branch: str | None = None
    worktrees: list[WorktreeIn] = []


class RuleFileIn(BaseModel):
    rel_path: str
    kind: str
    content: str
    modified_at: str | None = None


class RuleSourceIn(BaseModel):
    scope: str
    base_path: str
    account: str | None = None
    remote: str | None = None
    files: list[RuleFileIn] = []


class RulesBody(BaseModel):
    sources: list[RuleSourceIn] = []


class InventoryBody(BaseModel):
    platform: str | None = None
    repos: list[RepoIn] = []


@router.post("/hello")
def hello(
    body: Hello,
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    if body.platform:
        conn.execute("UPDATE machines SET platform = ? WHERE id = ?", (body.platform, machine["id"]))
    conn.commit()
    return {"machine": machine["name"], "server_version": __version__}


@router.get("/sessions/state")
def sessions_state(
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Tell the collector what the server already holds, so it keeps no state."""
    rows = conn.execute(
        "SELECT session_id, raw_bytes, head_sha FROM sessions WHERE machine_id = ?",
        (machine["id"],),
    ).fetchall()
    conn.commit()
    return {
        "sessions": {
            r["session_id"]: {"raw_bytes": r["raw_bytes"], "head_sha": r["head_sha"]} for r in rows
        }
    }


def _conflict(expected_offset: int, reason: str) -> JSONResponse:
    return JSONResponse({"expected_offset": expected_offset, "reason": reason}, status_code=409)


@router.post("/sessions/{session_id}/append")
def append_session(
    session_id: str,
    body: AppendBody,
    request: Request,
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if not store.valid_session_id(session_id):
        raise HTTPException(400, "Invalid session ID")
    if body.parent is not None and not store.valid_session_id(body.parent):
        raise HTTPException(400, "Invalid parent session ID")
    try:
        member = base64.b64decode(body.data, validate=True)
        raw = gzip.decompress(member)
    except (binascii.Error, OSError, EOFError) as exc:
        raise HTTPException(400, f"Chunk is not valid base64 gzip data: {exc}") from exc
    if len(raw) > MAX_CHUNK_BYTES:
        raise HTTPException(413, "Chunk too large")

    data_dir: Path = request.app.state.data_dir
    path = store.transcript_path(data_dir, machine["id"], session_id)
    session = conn.execute(
        "SELECT * FROM sessions WHERE machine_id = ? AND session_id = ?",
        (machine["id"], session_id),
    ).fetchone()
    stored = session["raw_bytes"] if session else 0

    if body.truncate or session is None:
        # A new or rewritten transcript must start at the beginning.
        if body.offset != 0:
            return _conflict(0, "start at offset 0")
        truncate = True
    else:
        if session["head_sha"] and body.head_sha and session["head_sha"] != body.head_sha:
            return _conflict(0, "the local file was rewritten")
        if body.offset != stored:
            return _conflict(stored, "offset does not match the stored size")
        truncate = False

    keep = 0 if truncate else session["stored_bytes"]
    stored_bytes = store.append(path, member, keep)
    new_size = (0 if truncate else stored) + len(raw)
    account_id = _account_id(conn, machine["user_id"], body.account)
    now = db.now_iso()

    if session is None:
        cur = conn.execute(
            """INSERT INTO sessions (session_id, machine_id, account_id, config_dir, source_path,
                                     raw_bytes, stored_bytes, head_sha, updated_at,
                                     parent_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, machine["id"], account_id, body.config_dir, body.source_path,
             new_size, stored_bytes, body.head_sha, now, body.parent),
        )
        pk = cur.lastrowid
        project_id = None
    else:
        pk = session["pk"]
        project_id = session["project_id"]
        conn.execute(
            """UPDATE sessions SET account_id = ?, config_dir = ?, source_path = ?, raw_bytes = ?,
                                   stored_bytes = ?,
                                   head_sha = CASE WHEN ? THEN ? ELSE head_sha END, updated_at = ?
               WHERE pk = ?""",
            (account_id, body.config_dir, body.source_path, new_size, stored_bytes,
             truncate, body.head_sha, now, pk),
        )

    if body.final and body.parent:
        # A subagent has no project of its own. Its usage counts for the
        # project of its parent, which is looked up when a page needs it.
        refresh_session(conn, data_dir, pk)
    elif body.final:
        work_dirs = refresh_session(conn, data_dir, pk)
        cwd = conn.execute("SELECT cwd FROM sessions WHERE pk = ?", (pk,)).fetchone()["cwd"]
        resolved = projects.resolve_session_project(
            conn, machine, body.cwd or cwd, body.remote, body.repo_root, work_dirs
        )
        loose = project_id is None or conn.execute(
            "SELECT kind FROM projects WHERE id = ?", (project_id,)).fetchone()["kind"] == "dir"
        # A repository assignment stays. A plain-folder one is only a fallback,
        # so it gives way as soon as the session's work points to a repository.
        if resolved and (loose or body.remote):
            conn.execute("UPDATE sessions SET project_id = ? WHERE pk = ?", (resolved, pk))

    conn.commit()
    return {"raw_bytes": new_size}


def _account_id(conn: sqlite3.Connection, user_id: int, label: str) -> int:
    label = label.strip() or "default"
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? AND label = ?", (user_id, label)
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO accounts (user_id, label) VALUES (?, ?)", (user_id, label)
    ).lastrowid


def refresh_session(conn: sqlite3.Connection, data_dir: Path, pk: int) -> dict[str, int]:
    """Re-derive the summary fields from the stored transcript.

    Returns the folders that the session worked in.
    """
    row = conn.execute("SELECT machine_id, session_id FROM sessions WHERE pk = ?", (pk,)).fetchone()
    path = store.transcript_path(data_dir, row["machine_id"], row["session_id"])
    meta = parse_meta(store.read_lines(path))
    conn.execute(
        """UPDATE sessions SET cwd = ?, git_branch = ?, title = ?, first_prompt = ?, last_prompt = ?,
               started_at = ?, ended_at = ?, user_prompts = ?, assistant_messages = ?, tool_calls = ?,
               entrypoint = ?, cc_version = ?, models = ?, cost_usd = ?, last_event = ?, last_tool = ?,
               work_dirs = ?, parser_version = ?
           WHERE pk = ?""",
        (meta.cwd, meta.git_branch, meta.title, meta.first_prompt, meta.last_prompt,
         meta.started_at, meta.ended_at, meta.user_prompts, meta.assistant_messages, meta.tool_calls,
         meta.entrypoint, meta.cc_version, ", ".join(meta.models), meta.cost_usd,
         meta.last_event, meta.last_tool, json.dumps(meta.work_dirs), PARSER_VERSION, pk),
    )
    conn.execute("DELETE FROM usage_daily WHERE session_pk = ?", (pk,))
    conn.executemany(
        """INSERT INTO usage_daily (session_pk, day, model, messages, input, output,
                                    cache_write_5m, cache_write_1h, cache_read)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(pk, day, model, *counts) for day, models in meta.usage.items()
         for model, counts in models.items()],
    )
    return meta.work_dirs


@router.put("/inventory")
def put_inventory(
    body: InventoryBody,
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Replace this machine's repository and worktree inventory."""
    if body.platform:
        conn.execute("UPDATE machines SET platform = ? WHERE id = ?", (body.platform, machine["id"]))
    conn.execute("DELETE FROM repos WHERE machine_id = ?", (machine["id"],))
    now = db.now_iso()
    worktrees = 0
    for repo in body.repos:
        if repo.remote:
            project_id = projects.project_for_remote(conn, repo.remote)
        else:
            project_id = projects.project_for_local_repo(conn, machine["name"], repo.path)
        repo_id = conn.execute(
            """INSERT OR REPLACE INTO repos (machine_id, project_id, path, default_branch, seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            (machine["id"], project_id, repo.path, repo.default_branch, now),
        ).lastrowid
        for wt in repo.worktrees:
            conn.execute(
                """INSERT OR REPLACE INTO worktrees
                       (repo_id, path, is_main, branch, head, last_commit_at, last_commit_subject,
                        dirty_files, upstream, ahead, behind, merged, locked, prunable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (repo_id, wt.path, wt.is_main, wt.branch, wt.head, wt.last_commit_at,
                 wt.last_commit_subject, wt.dirty_files, wt.upstream, wt.ahead, wt.behind,
                 wt.merged, wt.locked, wt.prunable),
            )
            worktrees += 1

    # This inventory may have made known where loose sessions belong.
    projects.reassign_loose_sessions(conn, machine["id"])
    conn.commit()
    return {"repos": len(body.repos), "worktrees": worktrees}


@router.put("/rules")
def put_rules(
    body: RulesBody,
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Replace this machine's instruction files."""
    conn.execute("DELETE FROM rule_files WHERE machine_id = ?", (machine["id"],))
    files = items = 0
    for source in body.sources:
        account_id = project_id = None
        if source.scope == "user":
            account_id = _account_id(conn, machine["user_id"], source.account or "default")
        elif source.scope == "project":
            if source.remote:
                project_id = projects.project_for_remote(conn, source.remote)
            else:
                project_id = projects.project_for_local_repo(conn, machine["name"], source.base_path)
        else:
            raise HTTPException(400, "Unknown scope")
        for entry in source.files:
            info = describe(entry.kind, entry.rel_path, entry.content)
            file_id = conn.execute(
                """INSERT OR REPLACE INTO rule_files
                       (machine_id, scope, account_id, project_id, base_path, rel_path, kind, name,
                        description, applies_to, content, sha, bytes, modified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (machine["id"], source.scope, account_id, project_id, source.base_path,
                 entry.rel_path, entry.kind, info["name"], info["description"], info["applies_to"],
                 entry.content, info["sha"], len(entry.content.encode()), entry.modified_at),
            ).lastrowid
            files += 1
            for position, item in enumerate(info["items"]):
                conn.execute(
                    "INSERT INTO rule_items (file_id, position, level, heading, content, sha) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, position, item["level"], item["heading"], item["content"], item["sha"]),
                )
                items += 1
    conn.commit()
    return {"files": files, "items": items}


def project_view(conn: sqlite3.Connection, project_id: int | None) -> dict | None:
    """The fields of a project that decide which rule sets apply."""
    if project_id is None:
        return None
    row = conn.execute(
        """SELECT p.id, p.key, p.name, p.kind, p.ai_ok, p.hub_rules, w.name AS workspace
           FROM projects p LEFT JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = ?""",
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


@router.get("/briefing")
def get_briefing(
    request: Request,
    cwd: str = "",
    remote: str = "",
    repo_root: str = "",
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Return the rules for a session that starts in ``cwd``."""
    project_id = projects.resolve_session_project(
        conn, machine, cwd or None, remote or None, repo_root or None)
    project = project_view(conn, project_id)
    # A plain folder is not a project with rules of its own. The session
    # still gets the sets that apply everywhere.
    if project and project["kind"] == "dir" and not project["workspace"]:
        project = None
    conn.commit()
    if project and not project["hub_rules"]:
        # This project still keeps its rules in its own files. Sending hub
        # rules on top would load them twice and let the two copies disagree.
        return {"text": "", "always": [], "on_demand": [], "tokens": 0,
                "project": project["name"], "hub_rules": False}
    result = build_briefing(load_rulesets(request.app.state.rulesets_dir), project)
    result["project"] = project["name"] if project else None
    result["hub_rules"] = True
    return result


@router.get("/rulesets/{name}")
def get_ruleset(
    name: str,
    request: Request,
    machine: sqlite3.Row = Depends(current_machine),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    conn.commit()
    for ruleset in load_rulesets(request.app.state.rulesets_dir):
        if ruleset.name == name:
            return {"name": ruleset.name, "title": ruleset.title, "body": ruleset.body}
    raise HTTPException(404, "Unknown rule set")
