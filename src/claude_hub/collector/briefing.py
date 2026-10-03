"""Fetch rules from the hub for a Claude Code session.

Called by a SessionStart hook. Whatever goes wrong, a session must still
start, so every failure ends in the cached copy or in no output at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from .client import Client, HubError
from .config import Config
from .gitscan import resolve_dir

PLACEHOLDER = "{{RULES_COMMAND}}"
HUB_PLACEHOLDER = "{{HUB_COMMAND}}"
HOOK_MARKER = "claude_hub.collector.cli"
# A session start must not hang on a server that does not answer.
TIMEOUT = 4.0


def rules_command(config_path: Path | None = None) -> str:
    """The command an agent runs on this machine to load a rule set."""
    python = sys.executable.replace("\\", "/")
    command = f'"{python}" -m {HOOK_MARKER}'
    if config_path is not None:
        command += f' --config "{str(config_path).replace(chr(92), "/")}"'
    return command


def _cache_file(cache_dir: Path, key: str) -> Path:
    return cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".md")


def _read_cache(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_cache(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def fetch_briefing(cfg: Config, cache_dir: Path, cwd: str) -> tuple[str, bool]:
    """Return (text, from_cache) for a session in ``cwd``."""
    info = resolve_dir(cwd)
    cache = _cache_file(cache_dir, "briefing:" + (info["remote"] or os.path.normcase(cwd)))
    # v=2 tells the hub that this collector can fill in the propose command.
    query = urlencode({"cwd": cwd, "remote": info["remote"] or "",
                       "repo_root": info["repo_root"] or "", "v": 2})
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=TIMEOUT).request(
            "GET", f"/api/v1/briefing?{query}")
    except (HubError, ValueError):
        cached = _read_cache(cache)
        return (cached or "", cached is not None)
    text = answer.get("text", "")
    _write_cache(cache, text)
    return text, False


def fetch_ruleset(cfg: Config, cache_dir: Path, name: str) -> tuple[str | None, str]:
    """Return (text, state) for one rule set.

    The state is "fresh", "cached" (the hub did not answer), "unknown" (the
    hub has no such set), or "offline" (no answer and no saved copy).
    """
    cache = _cache_file(cache_dir, "ruleset:" + name)
    try:
        status, answer = Client(cfg.server_url, cfg.token, timeout=TIMEOUT).request(
            "GET", f"/api/v1/rulesets/{quote(name, safe='')}", accept=(404,))
    except (HubError, ValueError):
        cached = _read_cache(cache)
        return cached, "cached" if cached is not None else "offline"
    if status == 404:
        return None, "unknown"
    text = f"# {answer['title']}\n\n{answer['body']}\n"
    _write_cache(cache, text)
    return text, "fresh"


def fill_commands(text: str, config_path: Path | None) -> str:
    """Put the commands that work on this machine into a briefing."""
    command = rules_command(config_path)
    return text.replace(PLACEHOLDER, command + " rules show").replace(HUB_PLACEHOLDER, command)


def where(folder: str) -> dict[str, str]:
    """What the hub needs to find the project of a folder."""
    info = resolve_dir(folder)
    return {"cwd": folder, "remote": info["remote"] or "", "repo_root": info["repo_root"] or ""}


def propose(cfg: Config, folder: str, ruleset: str, text: str, reason: str = "",
            mode: str = "add", source: str = "") -> tuple[bool, str]:
    """File a rule proposal. Returns (filed, message for the agent)."""
    body = {"ruleset": ruleset, "text": text, "reason": reason, "mode": mode, "source": source,
            **where(folder)}
    try:
        status, answer = Client(cfg.server_url, cfg.token, timeout=15).request(
            "POST", "/api/v1/proposals", body, accept=(400, 404, 422))
    except HubError as exc:
        return False, f"The proposal was not filed: {exc}"
    if status != 201:
        detail = answer.get("detail") if isinstance(answer, dict) else answer
        return False, f"The hub refused the proposal: {detail}"
    return True, (f"Proposal {answer['id']} is filed for the set '{ruleset}'. "
                  "It changes nothing until Robin accepts it in the hub.")


def emit(text: str) -> None:
    """Print as UTF-8. A Windows console defaults to a legacy code page."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(text)


def hook_entry(config_path: Path | None) -> dict:
    return {"hooks": [{"type": "command",
                       "command": rules_command(config_path) + " hook session-start",
                       "timeout": 20}]}


def install_hook(config_dir: Path, config_path: Path | None, remove: bool = False) -> str:
    """Add the SessionStart hook to a Claude Code settings file, or remove it.

    Other settings and other hooks are left as they are. The first change
    keeps a backup beside the file.
    """
    settings_path = config_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return f"{settings_path}: not valid JSON ({exc}). Nothing changed."
        if not isinstance(settings, dict):
            return f"{settings_path}: unexpected content. Nothing changed."

    hooks = settings.setdefault("hooks", {})
    entries = hooks.get("SessionStart") or []
    kept = [entry for entry in entries
            if HOOK_MARKER not in json.dumps(entry) or "hook session-start" not in json.dumps(entry)]
    had = len(kept) != len(entries)
    if remove:
        if not had:
            return f"{settings_path}: no hub hook found. Nothing changed."
        action = "removed"
    else:
        kept.append(hook_entry(config_path))
        action = "updated" if had else "installed"
    if kept:
        hooks["SessionStart"] = kept
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        settings.pop("hooks", None)

    backup = settings_path.with_name("settings.json.claude-hub.bak")
    if settings_path.exists() and not backup.exists():
        backup.write_bytes(settings_path.read_bytes())
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"{settings_path}: hub hook {action}."
