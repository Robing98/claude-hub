"""Collector configuration (TOML)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no TOML reader in the standard library.
    import tomli as tomllib

EXAMPLE = """\
# Claude hub collector configuration.

# Address of the hub server, reachable from this machine (for example over the VPN).
server_url = "http://hub.example.internal:8787"

# Token from `claude-hub token add MACHINE` on the server. It identifies this machine.
token = "PASTE_TOKEN_HERE"

# Folders that contain Git repositories. The collector reports every repository
# and worktree below them. Repositories that sessions ran in are found without this.
scan_roots = []
scan_depth = 4

# Cowork sessions that ran on this computer are collected as well, under the
# account label "cowork". The Claude desktop app keeps their transcripts in its
# own data folder. Set cowork = false to leave them out, or set cowork_dir when
# the app keeps them somewhere else.
# cowork = true
# cowork_dir = "C:/Users/NAME/AppData/Roaming/Claude/local-agent-mode-sessions"

# One entry per Claude Code account on this machine. Each account has its own
# configuration directory (CLAUDE_CONFIG_DIR). The label is free text.
[[accounts]]
label = "default"
config_dir = "~/.claude"
"""


COWORK = "cowork"


@dataclass
class Account:
    label: str
    config_dir: Path
    # "claude-code" for a configuration directory of Claude Code, or "cowork"
    # for the folder of one Cowork session. Only transcripts are read from
    # the second kind: no instruction files, and no hook is installed there.
    kind: str = "claude-code"


@dataclass
class Config:
    server_url: str
    token: str
    accounts: list[Account]
    scan_roots: list[Path] = field(default_factory=list)
    scan_depth: int = 4
    chunk_bytes: int = 4 * 1024 * 1024


class ConfigError(Exception):
    pass


def default_cowork_dir() -> Path | None:
    """Where the Claude desktop app keeps the Cowork sessions that ran on this computer."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        return None
    return base / "Claude" / "local-agent-mode-sessions"


def cowork_accounts(root: Path | None) -> list[Account]:
    """One entry per Cowork session folder that holds transcripts.

    The layout is ``<account>/<organization>/local_<session>/.claude/projects``,
    the same shape as a Claude Code configuration directory.
    """
    if root is None or not root.is_dir():
        return []
    return [Account(COWORK, path, COWORK)
            for path in sorted(root.glob("*/*/local_*/.claude")) if (path / "projects").is_dir()]


def default_path() -> Path:
    if os.environ.get("CLAUDE_HUB_CONFIG"):
        return Path(os.environ["CLAUDE_HUB_CONFIG"]).expanduser()
    # An empty variable counts as unset. Otherwise the file would land in
    # whatever folder the command runs in.
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "claude-hub" / "collector.toml"


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"No configuration at {path}. Run `claude-hub-collector init` first.")
    with open(path, "rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    server_url = str(raw.get("server_url", "")).rstrip("/")
    token = os.environ.get("CLAUDE_HUB_TOKEN") or str(raw.get("token", ""))
    if not server_url.startswith(("http://", "https://")):
        raise ConfigError("server_url must start with http:// or https://")
    if not token or token == "PASTE_TOKEN_HERE":
        raise ConfigError(
            f"No token in {path}. Create one on the server with `claude-hub token add MACHINE`, "
            "then replace PASTE_TOKEN_HERE in that file with it."
        )

    accounts = [
        Account(str(item.get("label", "default")), Path(str(item["config_dir"])).expanduser())
        for item in raw.get("accounts", [])
        if item.get("config_dir")
    ]
    if not accounts:
        fallback = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        accounts = [Account("default", Path(fallback).expanduser())]

    if raw.get("cowork", True):
        folder = raw.get("cowork_dir")
        accounts += cowork_accounts(Path(str(folder)).expanduser() if folder else default_cowork_dir())

    return Config(
        server_url=server_url,
        token=token,
        accounts=accounts,
        scan_roots=[Path(str(root)).expanduser() for root in raw.get("scan_roots", [])],
        scan_depth=int(raw.get("scan_depth", 4)),
        chunk_bytes=int(raw.get("chunk_bytes", 4 * 1024 * 1024)),
    )
