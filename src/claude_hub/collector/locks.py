"""Find Git lock files that were left behind, and remove the stale ones.

Git creates ``index.lock`` and similar files while it writes, and removes
them when it is done. A Git process that is killed, or one that runs where
files cannot be deleted, leaves the lock behind. Every later Git command in
that repository then fails until someone removes the file.

A lock is removed only when nothing can still own it: it is older than the
limit, and it is either empty or no Git process runs on this machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .gitscan import _FLAGS

# A lock that Git still uses is seconds old. The automatic run waits much
# longer, because it cannot see a Git process inside WSL or a sandbox.
STALE_SECONDS = 300
# For the command that a person or a session runs after Git has refused.
MANUAL_SECONDS = 15


def git_dirs(repo: Path) -> list[Path]:
    """The Git folder of a repository, and one per linked worktree."""
    main = repo / ".git"
    if not main.is_dir():
        return []
    linked = main / "worktrees"
    return [main] + (sorted(p for p in linked.iterdir() if p.is_dir()) if linked.is_dir() else [])


def lock_files(repo: Path) -> list[Path]:
    found: list[Path] = []
    for folder in git_dirs(repo):
        found += sorted(folder.glob("*.lock"))
        refs = folder / "refs"
        if refs.is_dir():
            found += sorted(refs.rglob("*.lock"))
    return found


def git_running() -> bool:
    """True when a Git process runs on this machine, or when that cannot be told."""
    try:
        if sys.platform == "win32":
            done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH", "/FO", "CSV"],
                                  capture_output=True, text=True, timeout=20, creationflags=_FLAGS)
            return done.returncode != 0 or "git.exe" in done.stdout.lower()
        done = subprocess.run(["pgrep", "-x", "git"], capture_output=True, text=True, timeout=20)
        # 0: found, 1: none. Anything else: unknown, so assume that Git runs.
        return done.returncode != 1
    except (OSError, subprocess.SubprocessError):
        return True


def sweep(repos: list[str | Path], stale_seconds: float = STALE_SECONDS, remove: bool = True,
          running: Callable[[], bool] = git_running, force: bool = False,
          now: float | None = None) -> list[dict[str, Any]]:
    """Look at every lock file of the repositories. Remove the stale ones.

    Returns one entry per lock: its path, repository, age, size, and what
    happened to it: ``removed``, ``young``, ``in use``, ``kept``, or ``failed``.
    """
    now = time.time() if now is None else now
    result: list[dict[str, Any]] = []
    busy: bool | None = None
    for repo in repos:
        for path in lock_files(Path(repo)):
            try:
                info = path.stat()
            except OSError:
                continue  # Git finished between the listing and this look.
            age = max(0, int(now - info.st_mtime))
            entry = {"path": str(path), "repo": str(repo), "age_seconds": age,
                     "size": info.st_size, "locked_at": int(info.st_mtime)}
            if not remove:
                entry["state"] = "kept"
            elif age < stale_seconds:
                entry["state"] = "young"
            else:
                # An empty lock holds nothing that Git could still be writing.
                # A lock with content can belong to a commit that waits for
                # its message, so it stays while any Git process runs.
                if info.st_size and not force:
                    busy = running() if busy is None else busy
                if info.st_size and busy and not force:
                    entry["state"] = "in use"
                else:
                    try:
                        os.remove(path)
                        entry["state"] = "removed"
                    except OSError as exc:
                        entry["state"], entry["note"] = "failed", str(exc)
            result.append(entry)
    return result


def describe(entry: dict[str, Any]) -> str:
    age = entry["age_seconds"]
    old = f"{age} s" if age < 120 else f"{age // 60} min" if age < 7200 else f"{age // 3600} h"
    what = {
        "removed": "removed",
        "young": "left, because Git may still use it",
        "in use": "left, because a Git process runs. Use --force if you are sure",
        "kept": "found",
        "failed": f"could not be removed ({entry.get('note', '')})",
    }[entry["state"]]
    return f"{entry['path']} ({old} old): {what}"
