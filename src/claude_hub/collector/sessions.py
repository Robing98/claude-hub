"""Find Claude Code transcripts and upload what the server does not have yet."""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from ..transcript import first_cwd
from .client import Client, HubError
from .config import Account
from .gitscan import resolve_dir

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
HEAD_LIMIT = 64 * 1024


def find_transcripts(config_dir: Path) -> Iterator[Path]:
    """Yield top-level session files. Subagent files live one level deeper."""
    projects = config_dir / "projects"
    if not projects.is_dir():
        return
    for folder in sorted(projects.iterdir()):
        if folder.is_dir():
            yield from sorted(folder.glob("*.jsonl"))


def head_sha(path: Path) -> str | None:
    """Hash the first line. It never changes while the file is only appended to."""
    with open(path, "rb") as handle:
        line = handle.readline(HEAD_LIMIT)
    return hashlib.sha256(line).hexdigest() if line.endswith(b"\n") else None


def _batches(path: Path, offset: int, chunk_bytes: int) -> Iterator[list[bytes]]:
    """Yield groups of complete lines, starting at ``offset``."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        while True:
            lines = handle.readlines(chunk_bytes)
            if not lines:
                return
            if not lines[-1].endswith(b"\n"):
                # Claude Code is writing this line right now. Take it next time.
                lines.pop()
                if lines:
                    yield lines
                return
            yield lines


def sync_file(client: Client, account: Account, path: Path, known: dict[str, Any] | None,
              chunk_bytes: int, git_cache: dict[str, dict], repos_seen: set[str]) -> int:
    """Upload the new part of one transcript. Returns the bytes sent."""
    session_id = path.stem
    if not _SESSION_ID.match(session_id):
        return 0
    size = path.stat().st_size
    head = head_sha(path)
    if head is None:
        return 0

    offset = known["raw_bytes"] if known else 0
    truncate = False
    if known and (size < offset or (known.get("head_sha") and known["head_sha"] != head)):
        # The file was rewritten, so the stored copy no longer matches.
        offset, truncate = 0, True
    if known and not truncate and size == offset:
        return 0

    sent = 0
    for attempt in range(3):
        try:
            sent = _upload_from(client, account, path, session_id, offset, truncate, head,
                                chunk_bytes, git_cache, repos_seen)
            return sent
        except _OffsetConflict as conflict:
            offset = conflict.expected
            truncate = offset == 0
    return sent


class _OffsetConflict(Exception):
    def __init__(self, expected: int):
        self.expected = expected


def _upload_from(client: Client, account: Account, path: Path, session_id: str, offset: int,
                 truncate: bool, head: str, chunk_bytes: int, git_cache: dict[str, dict],
                 repos_seen: set[str]) -> int:
    sent = 0
    context: dict[str, Any] = {}
    batches = _batches(path, offset, chunk_bytes)
    current = next(batches, None)
    while current is not None:
        following = next(batches, None)
        if not context:
            cwd = first_cwd(current)
            if cwd:
                if cwd not in git_cache:
                    git_cache[cwd] = resolve_dir(cwd)
                info = git_cache[cwd]
                if info["main_repo"] or info["repo_root"]:
                    repos_seen.add(info["main_repo"] or info["repo_root"])
                context = {"cwd": cwd, "remote": info["remote"], "repo_root": info["repo_root"]}
        raw = b"".join(current)
        body = {
            "offset": offset,
            "data": base64.b64encode(gzip.compress(raw, compresslevel=6)).decode(),
            "head_sha": head,
            "truncate": truncate,
            "final": following is None,
            "account": account.label,
            "config_dir": str(account.config_dir),
            "source_path": str(path),
            **context,
        }
        status, answer = client.request("POST", f"/api/v1/sessions/{session_id}/append", body,
                                        accept=(409,))
        if status == 409:
            raise _OffsetConflict(int(answer.get("expected_offset", 0)))
        offset += len(raw)
        sent += len(raw)
        truncate = False
        current = following
    return sent


def sync_account(client: Client, account: Account, state: dict[str, Any], chunk_bytes: int,
                 git_cache: dict[str, dict], repos_seen: set[str],
                 log: Callable[[str], None]) -> tuple[int, int]:
    """Upload all changed transcripts of one account. Returns (files, bytes)."""
    files = total = 0
    for path in find_transcripts(account.config_dir):
        try:
            sent = sync_file(client, account, path, state.get(path.stem), chunk_bytes,
                             git_cache, repos_seen)
        except HubError as exc:
            if exc.status in (0, 401):
                raise
            log(f"  skipped {path.name}: {exc}")
            continue
        except OSError as exc:
            log(f"  skipped {path.name}: {exc}")
            continue
        if sent:
            files += 1
            total += sent
    return files, total
