"""Project identity and the rules that sort projects into the hierarchy."""

from __future__ import annotations

import sqlite3
from fnmatch import fnmatchcase

from ..remote import basename, is_within, norm_path
from .db import now_iso


def _get_or_create(conn: sqlite3.Connection, key: str, kind: str, name: str) -> int:
    row = conn.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO projects (key, kind, name, created_at) VALUES (?, ?, ?, ?)",
        (key, kind, name, now_iso()),
    )
    apply_rules(conn, cur.lastrowid)
    return cur.lastrowid


def project_for_remote(conn: sqlite3.Connection, remote: str) -> int:
    return _get_or_create(conn, f"git:{remote}", "repo", remote.rsplit("/", 1)[-1])


def project_for_local_repo(conn: sqlite3.Connection, machine: str, path: str) -> int:
    return _get_or_create(conn, f"local:{machine}:{norm_path(path)}", "repo", basename(path))


def project_for_dir(conn: sqlite3.Connection, machine: str, path: str) -> int:
    return _get_or_create(conn, f"dir:{machine}:{norm_path(path)}", "dir", basename(path))


def project_from_inventory(conn: sqlite3.Connection, machine_id: int, cwd: str) -> int | None:
    """Find the repository that contains ``cwd``, using the last inventory.

    This covers sessions whose worktree folder no longer exists, where the
    collector cannot ask Git for the remote any more.
    """
    rows = conn.execute(
        """SELECT r.project_id, r.path AS repo_path, w.path AS wt_path
           FROM repos r LEFT JOIN worktrees w ON w.repo_id = r.id
           WHERE r.machine_id = ?""",
        (machine_id,),
    ).fetchall()
    best: tuple[int, int] | None = None
    for row in rows:
        for path in (row["repo_path"], row["wt_path"]):
            if path and is_within(cwd, path):
                length = len(norm_path(path))
                if best is None or length > best[0]:
                    best = (length, row["project_id"])
    return best[1] if best else None


def resolve_session_project(
    conn: sqlite3.Connection,
    machine: sqlite3.Row,
    cwd: str | None,
    remote: str | None,
    repo_root: str | None,
) -> int | None:
    if remote:
        return project_for_remote(conn, remote)
    if cwd:
        found = project_from_inventory(conn, machine["id"], cwd)
        if found:
            return found
    if repo_root:
        return project_for_local_repo(conn, machine["name"], repo_root)
    if cwd:
        return project_for_dir(conn, machine["name"], cwd)
    return None


def _rule_matches(rule: sqlite3.Row, project: sqlite3.Row) -> bool:
    key: str = project["key"]
    pattern = rule["pattern"].strip()
    if rule["field"] == "remote":
        return key.startswith("git:") and fnmatchcase(key[4:], pattern.lower())
    if rule["field"] == "path":
        if key.startswith("git:"):
            return False
        # Keys look like "dir:machine:path", and the path may contain colons.
        path = key.split(":", 2)[2]
        return fnmatchcase(path, norm_path(pattern)) or fnmatchcase(path.lower(), norm_path(pattern).lower())
    return False


def apply_rules(conn: sqlite3.Connection, project_id: int | None = None) -> int:
    """Assign projects by the first matching rule. Manual assignments stay."""
    rules = conn.execute("SELECT * FROM rules ORDER BY position, id").fetchall()
    query = "SELECT * FROM projects WHERE COALESCE(assigned_by, 'rule') = 'rule'"
    params: tuple = ()
    if project_id is not None:
        query += " AND id = ?"
        params = (project_id,)
    changed = 0
    for project in conn.execute(query, params).fetchall():
        match = next((r for r in rules if _rule_matches(r, project)), None)
        if match is None:
            continue
        if (project["workspace_id"], project["class_id"]) != (match["workspace_id"], match["class_id"]):
            changed += 1
        conn.execute(
            "UPDATE projects SET workspace_id = ?, class_id = ?, assigned_by = 'rule' WHERE id = ?",
            (match["workspace_id"], match["class_id"], project["id"]),
        )
    return changed
