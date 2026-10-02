"""Turn Git remote URLs and local paths into stable identities.

The collector runs this before anything leaves the machine, so credentials
that are embedded in a remote URL never reach the server.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# scp-like syntax: [user@]host:owner/name.git
_SCP = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^/\s].*)$")
_DRIVE = re.compile(r"^[a-zA-Z]:")


def normalize_remote(url: str | None) -> str | None:
    """Return ``host/owner/name`` in lowercase, or None for local remotes.

    The same repository cloned over HTTPS on one machine and over SSH on
    another must map to the same project, so scheme, user, port, and the
    ``.git`` suffix are dropped.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None

    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme == "file" or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    else:
        match = _SCP.match(url)
        # A one-letter "host" is a Windows drive letter, so this is a local path.
        if not match or len(match.group("host")) == 1:
            return None
        host, path = match.group("host"), match.group("path")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4].rstrip("/")
    if not path:
        return None
    return f"{host}/{path}".lower()


def norm_path(path: str | None) -> str:
    """Normalize a filesystem path for comparison across reports.

    Windows paths arrive with either separator and are case-insensitive.
    """
    if not path:
        return ""
    result = path.replace("\\", "/").rstrip("/")
    if _DRIVE.match(result):
        result = result.lower()
    return result


def is_within(child: str | None, parent: str | None) -> bool:
    """True when ``child`` is ``parent`` or lies below it."""
    child_n, parent_n = norm_path(child), norm_path(parent)
    if not child_n or not parent_n:
        return False
    return child_n == parent_n or child_n.startswith(parent_n + "/")


def basename(path: str) -> str:
    return norm_path(path).rsplit("/", 1)[-1] or path
