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


# A Cowork session runs in a folder of its own inside the data folder of the
# desktop app. That folder says nothing about the project it worked on.
_COWORK = re.compile(r"^(.*/local-agent-mode-sessions)/[^/]+/[^/]+/local_[^/]+(?:/.*)?$")
# Inside its sandbox, Cowork sees a connected folder under this path.
_VM_MOUNT = re.compile(r"^/sessions/[^/]+/mnt/([^/]+)(?:/.*)?$")


def cowork_root(path: str | None) -> str | None:
    """Return the folder that holds all Cowork sessions, if ``path`` lies in one of them."""
    match = _COWORK.match(norm_path(path))
    return match.group(1) if match else None


def _mounted(paths: list[tuple[str, int]], folder: str) -> int | None:
    """Find the project of a connected folder that a Cowork session names by its sandbox path."""
    match = _VM_MOUNT.match(norm_path(folder))
    if not match:
        return None
    name = match.group(1).lower()
    hits = {project_id for path, project_id in paths if basename(path).lower() == name}
    # Two repositories with the same folder name cannot be told apart from here.
    return hits.pop() if len(hits) == 1 else None


# Mounts that belong to the Cowork session itself, not to a folder of the person.
_SESSION_MOUNTS = {"outputs", "uploads", ".claude", "skills", "guide"}
_SANDBOX = re.compile(r"^/sessions/[^/]+/mnt/([^/]+)(/.*)?$")
# Folders that hold many unrelated things. The project is the first folder below one.
_CONTAINER = re.compile(
    r"^((?:[a-z]:)?/users/[^/]+/(?:onedrive[^/]*|documents|desktop|dropbox|google drive|"
    r"icloud ?drive|nextcloud)/[^/]+)", re.I)
_HOME = re.compile(r"^(?:[a-z]:)?/(?:users|home)/[^/]+(?:/|$)", re.I)
_DRIVE_FOLDER = re.compile(r"^([a-z]:/[^/]+/[^/]+)", re.I)


def host_dirs(work_dirs: dict[str, int] | None) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return what a session touched as folders of the computer, and its connected folders.

    Cowork names one folder in two ways: by its real path in file tools, and
    by a sandbox path in shell commands. The sandbox path is translated when
    the same session shows the real path of that folder. Folders of the
    session itself are left out.

    Each result maps a folder to how often it was touched: the real folders,
    the connected folders whose real path is known, and the sandbox paths
    that could not be translated.
    """
    host: dict[str, int] = {}
    sandbox: list[tuple[str, str, int]] = []
    for folder, count in (work_dirs or {}).items():
        path = folder.replace("\\", "/").rstrip("/")
        match = _SANDBOX.match(path)
        if match:
            if match.group(1).lower() not in _SESSION_MOUNTS:
                sandbox.append((match.group(1), match.group(2) or "", count))
        elif path.startswith("/sessions/") or cowork_root(path):
            continue
        elif path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
            host[path] = host.get(path, 0) + count

    roots: dict[str, int] = {}
    unknown: dict[str, int] = {}
    seen = sorted(host, key=lambda item: -host[item])
    for name, rest, count in sandbox:
        # The real path of a connected folder ends in the name of its mount.
        for path in seen:
            parts = path.split("/")
            index = next((i for i, part in enumerate(parts) if part.lower() == name.lower()), None)
            if index is not None:
                root = "/".join(parts[:index + 1])
                roots.setdefault(root, 0)
                host[root + rest] = host.get(root + rest, 0) + count
                break
        else:
            unknown[f"/sessions/x/mnt/{name}{rest}"] = unknown.get(f"/sessions/x/mnt/{name}{rest}", 0) + count
    for root in roots:
        roots[root] = sum(count for path, count in host.items() if is_within(path, root))
    return host, roots, unknown


def folder_root(work_dirs: dict[str, int] | None) -> str | None:
    """Pick the folder that a Cowork session worked in, when it is no repository.

    A connected folder wins. Without one, the first folder below a container
    such as OneDrive or Documents counts, or the second level of a drive.
    """
    host, roots, _ = host_dirs(work_dirs)
    if roots:
        best = max(roots, key=lambda root: roots[root])
        if roots[best] >= 2:
            return best
    score: dict[str, int] = {}
    for path, count in host.items():
        match = _CONTAINER.match(path)
        if not match and not _HOME.match(path):
            # Application data and caches below the home folder are no projects.
            match = _DRIVE_FOLDER.match(path)
        if match:
            score[match.group(1)] = score.get(match.group(1), 0) + count
    if not score:
        return None
    best = max(score, key=lambda root: score[root])
    return best if score[best] >= 3 else None


def probe_dirs(conn: sqlite3.Connection, machine_id: int, limit: int = 60) -> list[str]:
    """Folders that loose sessions worked in and that no known repository contains.

    The collector checks whether they are repositories. The hub cannot: it
    sees paths, not disks.
    """
    paths = _inventory_paths(conn, machine_id)
    rows = conn.execute(
        """SELECT s.work_dirs FROM sessions s LEFT JOIN projects p ON p.id = s.project_id
           WHERE s.machine_id = ? AND s.work_dirs IS NOT NULL AND s.project_manual = 0
                 AND s.parent_session_id IS NULL AND (p.id IS NULL OR p.kind = 'dir')""",
        (machine_id,)).fetchall()
    found: dict[str, int] = {}
    for row in rows:
        host, _, _ = host_dirs(json.loads(row["work_dirs"]))
        for path, count in host.items():
            if count >= 2 and not path.startswith("/tmp") and not _containing(paths, path):
                found[path] = found.get(path, 0) + count
    return sorted(found, key=lambda path: -found[path])[:limit]


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
    # Sandbox paths of Cowork count under the real folder when the session shows it.
    # The rest of them can still name a repository by the name of their mount.
    candidates, _, unknown = host_dirs(work_dirs)
    for folder, count in {**candidates, **unknown}.items():
        project_id = _containing(paths, folder) or _mounted(paths, folder)
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
        shared = cowork_root(cwd)
        if shared:
            # No repository: the folder that the session worked in is the project.
            folder = folder_root(work_dirs)
            if folder:
                # The name keeps its spelling. The key is compared without case on Windows.
                return _get_or_create(conn, f"dir:{machine['name']}:{norm_path(folder)}", "dir",
                                      folder.rsplit("/", 1)[-1])
            # Cowork sessions that touched no folder of the person share one project,
            # so that each of them does not open an inbox entry of its own.
            return _get_or_create(conn, f"dir:{machine['name']}:{norm_path(shared)}", "dir", "Cowork")
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
