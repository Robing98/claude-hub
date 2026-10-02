"""Transcript files on disk.

Each transcript is stored as concatenated gzip members. That makes an
upload an append of the bytes the collector sent, and the file still reads
as one gzip stream.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import Iterator

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def valid_session_id(session_id: str) -> bool:
    # The ID becomes a file name, so it must not carry path separators.
    return bool(_SAFE_ID.match(session_id)) and ".." not in session_id


def transcript_path(data_dir: Path, machine_id: int, session_id: str) -> Path:
    return data_dir / "transcripts" / str(machine_id) / f"{session_id}.jsonl.gz"


def append(path: Path, gz_member: bytes, keep_bytes: int) -> int:
    """Write one gzip member after the first ``keep_bytes`` of the file.

    Anything beyond ``keep_bytes`` comes from an upload whose database commit
    never happened, so it is cut off instead of being kept twice. Returns the
    new file size.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "r+b" if path.exists() else "wb") as handle:
        handle.truncate(keep_bytes)
        handle.seek(keep_bytes)
        handle.write(gz_member)
        return handle.tell()


def read_lines(path: Path) -> Iterator[bytes]:
    if not path.exists():
        return
    with gzip.open(path, "rb") as handle:
        yield from handle
