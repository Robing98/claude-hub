"""API for collectors: session uploads and the repository inventory."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..transcript import parse_meta
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
                                     raw_bytes, stored_bytes, head_sha, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, machine["id"], account_id, body.config_dir, body.source_path,
             new_size, stored_bytes, body.head_sha, now),
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

    if body.final:
        refresh_session(conn, data_dir, pk)
        cwd = conn.execute("SELECT cwd FROM sessions WHERE pk = ?", (pk,)).fetchone()["cwd"]
        resolved = projects.resolve_session_project(
            conn, machine, body.cwd or cwd, body.remote, body.repo_root
        )
        # Keep an existing assignment unless Git now identifies the repository.
        if resolved and (project_id is None or body.remote):
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


def refresh_session(conn: sqlite3.Connection, data_dir: Path, pk: int) -> None:
    """Re-derive the summary fields from the stored transcript."""
    row = conn.execute("SELECT machine_id, session_id FROM sessions WHERE pk = ?", (pk,)).fetchone()
    path = store.transcript_path(data_dir, row["machine_id"], row["session_id"])
    meta = parse_meta(store.read_lines(path))
    conn.execute(
        """UPDATE sessions SET cwd = ?, git_branch = ?, title = ?, first_prompt = ?, last_prompt = ?,
               started_at = ?, ended_at = ?, user_prompts = ?, assistant_messages = ?, tool_calls = ?,
               entrypoint = ?, cc_version = ?, models = ?, cost_usd = ?, last_event = ?, last_tool = ?
           WHERE pk = ?""",
        (meta.cwd, meta.git_branch, meta.title, meta.first_prompt, meta.last_prompt,
         meta.started_at, meta.ended_at, meta.user_prompts, meta.assistant_messages, meta.tool_calls,
         meta.entrypoint, meta.cc_version, ", ".join(meta.models), meta.cost_usd,
         meta.last_event, meta.last_tool, pk),
    )


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

    # Sessions that were filed as plain folders may belong to a repository
    # that this inventory has just made known.
    for session in conn.execute(
        """SELECT s.pk, s.cwd FROM sessions s JOIN projects p ON p.id = s.project_id
           WHERE s.machine_id = ? AND p.kind = 'dir' AND s.cwd IS NOT NULL""",
        (machine["id"],),
    ).fetchall():
        found = projects.project_from_inventory(conn, machine["id"], session["cwd"])
        if found:
            conn.execute("UPDATE sessions SET project_id = ? WHERE pk = ?", (found, session["pk"]))
    conn.execute(
        "DELETE FROM projects WHERE kind = 'dir' AND id NOT IN "
        "(SELECT project_id FROM sessions WHERE project_id IS NOT NULL)"
    )
    conn.commit()
    return {"repos": len(body.repos), "worktrees": worktrees}
