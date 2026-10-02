"""Find the instruction files that Claude Code loads. Read-only.

Only Markdown instruction files are read. Settings and credential files in
the same folders are never opened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..rules import find_imports
from .gitscan import SKIP_DIRS

MAX_BYTES = 512 * 1024
NESTED_DEPTH = 4
IMPORT_DEPTH = 4

# Files in the repository root that hold instructions under a neutral name.
# Claude Code does not load them by itself, but teams keep their rules there.
ROOT_CONVENTIONS = {"conventions.md", "agents.md"}

# Folder below the base, file pattern, kind.
_FOLDERS = [
    ("rules", "**/*.md", "rule"),
    ("skills", "*/SKILL.md", "skill"),
    ("agents", "**/*.md", "agent"),
    ("commands", "**/*.md", "command"),
    ("output-styles", "**/*.md", "output_style"),
]


def _entry(base: Path, path: Path, kind: str) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_BYTES:
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {
        "rel_path": path.relative_to(base).as_posix(),
        "kind": kind,
        "content": content,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }


def _folder_files(base: Path, claude_dir: Path) -> Iterator[dict[str, Any]]:
    for folder, pattern, kind in _FOLDERS:
        root = claude_dir / folder
        if root.is_dir():
            for path in sorted(root.glob(pattern)):
                entry = _entry(base, path, kind)
                if entry:
                    yield entry


def _with_imports(base: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the files that CLAUDE.md files import with ``@path``.

    Imports are followed only inside ``base``. A file elsewhere on the
    machine is not read, even when an instruction file points to it.
    """
    known = {entry["rel_path"] for entry in files}
    queue = [(entry, 1) for entry in files if entry["kind"] == "claude_md"]
    root = base.resolve()
    while queue:
        source, depth = queue.pop(0)
        folder = (base / source["rel_path"]).parent
        for target in find_imports(source["content"]):
            if target.startswith("~/"):
                path = Path(target).expanduser()
            else:
                path = folder / target.replace("\\", "/")
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if rel in known:
                continue
            entry = _entry(root, resolved, "import")
            if entry is None:
                continue
            entry["rel_path"] = rel
            known.add(rel)
            files.append(entry)
            if depth < IMPORT_DEPTH:
                # An imported file can import further files.
                queue.append(({**entry, "kind": "claude_md"}, depth + 1))
    return files


def scan_user(config_dir: Path) -> list[dict[str, Any]]:
    """Instruction files of one Claude Code configuration directory."""
    files = []
    entry = _entry(config_dir, config_dir / "CLAUDE.md", "claude_md")
    if entry:
        files.append(entry)
    files.extend(_folder_files(config_dir, config_dir))
    return _with_imports(config_dir, files)


def _nested_claude_md(root: Path) -> Iterator[Path]:
    def walk(folder: Path, remaining: int) -> Iterator[Path]:
        if remaining <= 0:
            return
        try:
            children = sorted(entry for entry in folder.iterdir() if entry.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DIRS or child.is_symlink():
                continue
            if (child / ".git").exists():
                # Another repository or a linked worktree reports its own files.
                continue
            candidate = child / "CLAUDE.md"
            if candidate.is_file():
                yield candidate
            yield from walk(child, remaining - 1)

    yield from walk(root, NESTED_DEPTH)


def scan_repo(root: Path) -> list[dict[str, Any]]:
    """Instruction files of one repository."""
    files = []
    for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
        entry = _entry(root, root / name, "claude_md")
        if entry:
            files.append(entry)
    try:
        neutral = sorted(p for p in root.iterdir() if p.name.lower() in ROOT_CONVENTIONS)
    except OSError:
        neutral = []
    for path in neutral:
        entry = _entry(root, path, "conventions")
        if entry:
            files.append(entry)
    files.extend(_folder_files(root, root / ".claude"))
    for path in _nested_claude_md(root):
        entry = _entry(root, path, "claude_md")
        if entry:
            files.append(entry)
    return _with_imports(root, files)
