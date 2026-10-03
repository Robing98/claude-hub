"""Project identity and the rules that sort projects into the hierarchy."""

from __future__ import annotations

import json
import re
import sqlite3
from fnmatch import fnmatchcase

from ..remote import basename, is_within, norm_path
from .db import now_iso


def _get_or_create(conn: sqlite3.Connection, key: str, kind: str, name: str) -> int:
    row = conn.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()
    if row:
        return row["id"]
    # A project that was merged into another one lives on as an alias.
    alias = conn.execute("SELECT project_id FROM project_aliases WHERE key = ?", (key,)).fetchone()
    if alias:
        return alias["project_id"]
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


_WORKTREE = re.compile(r"^(.+)/\.claude/worktrees/[^/]+(?:/.*)?$")


def worktree_base(path: str | None) -> str | None:
    """Return the repository folder of a Claude Code worktree path."""
    match = _WORKTREE.match(norm_path(path))
    return match.group(1) if match else None


def _inventory_paths(conn: sqlite3.Connection, machine_id: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT r.project_id, r.path AS repo_path, w.path AS wt_path
           FROM repos r LEFT JOIN worktrees w ON w.repo_id = r.id
           WHERE r.machine_id = ?""",
        (machine_id,),
    ).fetchall()
    paths = {(norm_path(path), row["project_id"])
             for row in rows for path in (row["repo_path"], row["wt_path"]) if path}
    # Longest first, so that a linked worktree wins over the repository around it.
    return sorted(paths, key=lambda item: -len(item[0]))


def _containing(paths: list[tuple[str, int]], target: str | None) -> int | None:
    for path, project_id in paths:
        if is_within(target, path):
            return project_id
    return None


def project_from_inventory(conn: sqlite3.Connection, machine_id: int, cwd: str) -> int | None:
    """Find the repository that contains ``cwd``, using the last inventory.

    This covers sessions whose worktree folder no longer exists, where the
    collector cannot ask Git for the remote any more.
    """
    paths = _inventory_paths(conn, machine_id)
    return _containing(paths, cwd) or _containing(paths, worktree_base(cwd))


def project_from_work_dirs(conn: sqlite3.Connection, machine_id: int,
                           work_dirs: dict[str, int] | None) -> int | None:
    """Pick the repository that a session touched most.

    Used for sessions that start in a parent folder, such as a drive root,
    and then work inside a repository.
    """
    if not work_dirs:
        return None
    paths = _inventory_paths(conn, machine_id)
    score: dict[int, int] = {}
    for folder, count in work_dirs.items():
        project_id = _containing(paths, folder)
        if project_id:
            score[project_id] = score.get(project_id, 0) + count
    if not score:
        return None
    best = max(score, key=lambda key: score[key])
    # One stray file is not enough to move a session.
    return best if score[best] >= 2 else None


def resolve_session_project(
    conn: sqlite3.Connection,
    machine: sqlite3.Row,
    cwd: str | None,
    remote: str | None,
    repo_root: str | None,
    work_dirs: dict[str, int] | None = None,
) -> int | None:
    if remote:
        return project_for_remote(conn, remote)
    if cwd:
        found = project_from_inventory(conn, machine["id"], cwd)
        if found:
            return found
    found = project_from_work_dirs(conn, machine["id"], work_dirs)
    if found:
        return found
    if repo_root:
        return project_for_local_repo(conn, machine["name"], repo_root)
    if cwd:
        # A removed worktree belongs to the folder of its repository.
        return project_for_dir(conn, machine["name"], worktree_base(cwd) or cwd)
    return None


def reassign_loose_sessions(conn: sqlite3.Connection, machine_id: int) -> int:
    """Move sessions out of plain-folder projects when a repository is known.

    Runs after each inventory and after a re-parse, because both can reveal
    where a session belongs.
    """
    machine = conn.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    moved = 0
    rows = conn.execute(
        """SELECT s.pk, s.cwd, s.work_dirs, s.project_id FROM sessions s
           LEFT JOIN projects p ON p.id = s.project_id
           WHERE s.machine_id = ? AND s.cwd IS NOT NULL AND s.project_manual = 0
                 AND (p.id IS NULL OR p.kind = 'dir')""",
        (machine_id,),
    ).fetchall()
    for row in rows:
        work_dirs = json.loads(row["work_dirs"]) if row["work_dirs"] else None
        found = resolve_session_project(conn, machine, row["cwd"], None, None, work_dirs)
        if found and found != row["project_id"]:
            conn.execute("UPDATE sessions SET project_id = ? WHERE pk = ?", (found, row["pk"]))
            moved += 1
    conn.execute(
        "DELETE FROM projects WHERE kind = 'dir' AND id NOT IN "
        "(SELECT project_id FROM sessions WHERE project_id IS NOT NULL)"
    )
    return moved


def merge_projects(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Move everything of one project into another and remember the old key."""
    source = conn.execute("SELECT * FROM projects WHERE id = ?", (source_id,)).fetchone()
    target = conn.execute("SELECT id FROM projects WHERE id = ?", (target_id,)).fetchone()
    if source is None or target is None or source_id == target_id:
        raise ValueError("Unknown project, or source and target are the same")
    for table in ("sessions", "repos", "rule_files", "project_aliases"):
        conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id = ?", (target_id, source_id))
    conn.execute(
        "INSERT OR REPLACE INTO project_aliases (key, project_id, name, created_at) VALUES (?, ?, ?, ?)",
        (source["key"], target_id, source["name"], now_iso()),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (source_id,))


def move_pin(conn: sqlite3.Connection, project_id: int, action: str) -> None:
    """Pin a project, unpin it, or move it among the pinned projects of its group."""
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return
    if action == "unpin":
        conn.execute("UPDATE projects SET pin_position = NULL WHERE id = ?", (project_id,))
        return
    if project["pin_position"] is None:
        if action == "pin":
            conn.execute(
                "UPDATE projects SET pin_position = "
                "(SELECT COALESCE(MAX(pin_position), 0) + 1 FROM projects) WHERE id = ?", (project_id,))
        return
    # Projects without a class are shown as two groups: repositories and plain folders.
    group = [row for row in conn.execute(
        """SELECT id, pin_position, kind FROM projects
           WHERE pin_position IS NOT NULL AND archived = 0 AND workspace_id IS ? AND class_id IS ?
           ORDER BY pin_position, id""",
        (project["workspace_id"], project["class_id"])).fetchall()
        if project["class_id"] is not None or row["kind"] == project["kind"]]
    index = next(i for i, row in enumerate(group) if row["id"] == project_id)
    other = index + {"up": -1, "down": 1}.get(action, 0)
    if other == index or not 0 <= other < len(group):
        return
    conn.execute("UPDATE projects SET pin_position = ? WHERE id = ?",
                 (group[other]["pin_position"], project_id))
    conn.execute("UPDATE projects SET pin_position = ? WHERE id = ?",
                 (project["pin_position"], group[other]["id"]))


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
