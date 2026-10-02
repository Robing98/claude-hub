"""Read-only Git inventory: repositories, worktrees, and their state.

The collector never fetches, commits, or pushes. It only asks the local
repository about itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..remote import normalize_remote

SKIP_DIRS = {"node_modules", "vendor", "venv", "__pycache__", "target", "dist", "build"}

_ENV = {
    **os.environ,
    # Without this, `git status` takes the index lock and can make a running
    # agent's own Git command fail.
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
# Keeps a console window from flashing when the collector runs in the background.
_FLAGS = 0x08000000 if sys.platform == "win32" else 0


def git(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), env=_ENV, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, creationflags=_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout.strip()


def git_out(cwd: str | Path, *args: str, timeout: float = 60) -> str | None:
    code, out = git(cwd, *args, timeout=timeout)
    return out if code == 0 else None


def remote_of(repo: str | Path) -> str | None:
    url = git_out(repo, "remote", "get-url", "origin")
    if url is None:
        names = (git_out(repo, "remote") or "").split()
        url = git_out(repo, "remote", "get-url", names[0]) if names else None
    return normalize_remote(url)


def resolve_dir(cwd: str) -> dict[str, str | None]:
    """Identify the repository that a session directory belongs to."""
    empty = {"remote": None, "repo_root": None, "main_repo": None}
    if not cwd or not os.path.isdir(cwd):
        return empty
    top = git_out(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return empty
    # A linked worktree shares the main repository's .git directory.
    common = git_out(cwd, "rev-parse", "--git-common-dir")
    main_repo = None
    if common:
        common_path = Path(common) if os.path.isabs(common) else Path(cwd, common)
        common_path = Path(os.path.abspath(common_path))
        if common_path.name == ".git":
            main_repo = str(common_path.parent)
    return {"remote": remote_of(top), "repo_root": main_repo or top, "main_repo": main_repo}


def find_repos(roots: list[Path], depth: int) -> Iterator[Path]:
    """Yield main repositories below the roots. Linked worktrees are skipped."""
    seen: set[str] = set()

    def walk(folder: Path, remaining: int) -> Iterator[Path]:
        marker = folder / ".git"
        if marker.is_dir():
            key = os.path.normcase(str(folder))
            if key not in seen:
                seen.add(key)
                yield folder
            return
        if marker.exists() or remaining <= 0:
            # A .git file marks a linked worktree or a submodule.
            return
        try:
            children = sorted(entry for entry in folder.iterdir() if entry.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DIRS or child.is_symlink():
                continue
            yield from walk(child, remaining - 1)

    for root in roots:
        if root.is_dir():
            yield from walk(root, depth)


def _default_branch(repo: Path) -> tuple[str | None, str | None]:
    """Return (branch name, ref to compare against)."""
    head = git_out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        return head.split("/", 1)[1], head
    for name in ("main", "master"):
        if git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}")[0] == 0:
            return name, f"origin/{name}"
    for name in ("main", "master"):
        if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0:
            return name, name
    return None, None


def _parse_worktree_list(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in ("locked", "prunable", "bare", "detached"):
            current[key] = True
    return entries


def _foreign(path: str) -> bool:
    """True for a path of the other system: a Linux path seen from Windows, or the reverse.

    A repository that is used from both Windows and WSL has worktrees of
    both kinds. Each collector can only judge the ones of its own system.
    """
    is_drive = len(path) > 1 and path[1] == ":"
    return path.startswith("/") if sys.platform == "win32" else is_drive


def _native(path: str) -> str:
    # Git prints forward slashes on Windows. Report the form the OS uses, so
    # paths match the working directories in the transcripts.
    return str(Path(path)) if sys.platform == "win32" else path


def _abs_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _dirty_count(path: str, nested: set[str]) -> int:
    """Count changed and untracked entries, ignoring linked worktrees.

    A worktree that lives inside another one (``.claude/worktrees/x``) shows
    up there as an untracked folder. That is not unsaved work.
    """
    status = git_out(path, "status", "--porcelain", "--untracked-files=normal", timeout=120)
    count = 0
    for line in (status or "").splitlines():
        entry = line[3:].strip('"')
        full = _abs_key(os.path.join(path, entry))
        holds_worktree = any(n == full or n.startswith(full + os.sep) for n in nested)
        if not (line.startswith("??") and holds_worktree):
            count += 1
            continue
        # Look inside the folder and count everything that is not a worktree.
        detail = git_out(path, "status", "--porcelain", "--untracked-files=all", "--", entry,
                         timeout=120)
        for inner in (detail or "").splitlines():
            if _abs_key(os.path.join(path, inner[3:].strip('"'))) not in nested:
                count += 1
    return count


def describe_repo(repo: Path) -> dict[str, Any] | None:
    repo = Path(os.path.abspath(repo))
    listing = git_out(repo, "worktree", "list", "--porcelain")
    if listing is None:
        return None
    default_branch, default_ref = _default_branch(repo)
    entries = _parse_worktree_list(listing)
    all_paths = {_abs_key(entry["path"]) for entry in entries if "path" in entry}
    worktrees = []
    for index, entry in enumerate(entries):
        if entry.get("bare") or "path" not in entry:
            continue
        if _foreign(entry["path"]):
            # The collector on the other system reports this one.
            continue
        path = entry["path"]
        item: dict[str, Any] = {
            "path": _native(path),
            "is_main": index == 0,
            "branch": entry.get("branch"),
            "head": entry.get("head"),
            "locked": bool(entry.get("locked")),
            "prunable": bool(entry.get("prunable")) or not os.path.isdir(path),
        }
        if not item["prunable"]:
            item["dirty_files"] = _dirty_count(path, all_paths - {_abs_key(path)})
            last = git_out(path, "log", "-1", "--format=%ct%x09%s")
            if last:
                stamp, _, subject = last.partition("\t")
                item["last_commit_at"] = datetime.fromtimestamp(int(stamp), timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                item["last_commit_subject"] = subject[:200]
            item["upstream"] = git_out(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                       "@{upstream}")
            if item["upstream"]:
                counts = (git_out(path, "rev-list", "--left-right", "--count",
                                  "@{upstream}...HEAD") or "").split()
                if len(counts) == 2:
                    item["behind"], item["ahead"] = int(counts[0]), int(counts[1])
            if default_ref and last:
                item["merged"] = git(path, "merge-base", "--is-ancestor", "HEAD", default_ref)[0] == 0
        worktrees.append(item)
    return {
        "path": _native(str(repo)),
        "remote": remote_of(repo),
        "default_branch": default_branch,
        "worktrees": worktrees,
    }


def build_inventory(roots: list[Path], depth: int, extra_repos: set[str]) -> list[dict[str, Any]]:
    paths: dict[str, Path] = {}
    for repo in find_repos(roots, depth):
        paths[os.path.normcase(str(repo))] = repo
    # Repositories that sessions ran in, even when they lie outside the roots.
    for extra in extra_repos:
        if os.path.isdir(os.path.join(extra, ".git")):
            paths.setdefault(os.path.normcase(extra), Path(extra))
    repos = []
    for repo in paths.values():
        described = describe_repo(repo)
        if described:
            repos.append(described)
    return repos
