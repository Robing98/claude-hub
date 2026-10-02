"""Find the instruction files that Claude Code loads. Read-only.

Only Markdown instruction files are read. Settings and credential files in
the same folders are never opened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .gitscan import SKIP_DIRS

MAX_BYTES = 512 * 1024
NESTED_DEPTH = 4

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


def scan_user(config_dir: Path) -> list[dict[str, Any]]:
    """Instruction files of one Claude Code configuration directory."""
    files = []
    entry = _entry(config_dir, config_dir / "CLAUDE.md", "claude_md")
    if entry:
        files.append(entry)
    files.extend(_folder_files(config_dir, config_dir))
    return files


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
    files.extend(_folder_files(root, root / ".claude"))
    for path in _nested_claude_md(root):
        entry = _entry(root, path, "claude_md")
        if entry:
            files.append(entry)
    return files
