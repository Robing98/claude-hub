"""A local MCP server that gives the hub to Cowork and other MCP clients: rules, calendars, mail.

It speaks MCP over standard input and output with the standard library
only, so that it runs wherever the collector runs. The Claude desktop app
starts it and passes its tools on to Cowork sessions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlencode

from .. import __version__
from . import briefing, google_tools, handoff, hub_tools, locks
from .client import Client, HubError
from .config import Config
from .gitscan import resolve_dir

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "claude-hub"

INSTRUCTIONS = (
    "The hub holds the rules of Robin's projects. Before you work in a project folder, call "
    "hub_rules with the path of that folder and follow what it returns. When the returned text "
    "lists more rule sets, load one with hub_ruleset before the work it covers. Never edit rule "
    "files in a project. To change a rule, call hub_propose_rule. Robin decides in the hub.\n\n"
    + hub_tools.INSTRUCTIONS + "\n\n" + google_tools.INSTRUCTIONS
)

FOLDER = {"type": "string",
          "description": "Absolute path of the project folder on Robin's computer, "
                         "for example D:\\dev\\orbis."}

TOOLS = [
    {
        "name": "hub_rules",
        "title": "Rules for a project folder",
        "description": "Return the rules that apply to work in a project folder. Call this "
                       "before you start work in a folder. An empty result means that the "
                       "project still keeps its rules in its own files.",
        "inputSchema": {"type": "object", "properties": {"folder": FOLDER}, "required": ["folder"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "hub_ruleset",
        "title": "One rule set",
        "description": "Return one rule set by its name. Use the names that hub_rules lists "
                       "under 'More rule sets'.",
        "inputSchema": {"type": "object",
                        "properties": {"name": {"type": "string", "description": "Name of the set."}},
                        "required": ["name"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "hub_propose_rule",
        "title": "Propose a rule change",
        "description": "Propose a change to a rule set. Nothing changes until Robin accepts it "
                       "in the hub. Use mode 'add' for one new rule, written as it should appear "
                       "in the set. Use mode 'replace' with the whole new text of the set.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ruleset": {"type": "string", "description": "Name of the set to change."},
                "text": {"type": "string", "description": "The rule text, in Markdown."},
                "reason": {"type": "string", "description": "Why the change is needed."},
                "mode": {"type": "string", "enum": ["add", "replace"]},
                "folder": FOLDER,
                "source": {"type": "string",
                           "description": "Who proposes, for example 'Orbis design lane'."},
            },
            "required": ["ruleset", "text", "reason"],
        },
    },
    {
        "name": "hub_unlock",
        "title": "Remove Git lock files that were left behind",
        "description": "Remove stale Git lock files in the repository of a folder. Call it when "
                       "Git reports that index.lock exists, and after you ran a Git command in "
                       "the shell on Robin's computer: that shell cannot delete files, so Git "
                       "leaves its lock behind there. A lock that Git may still use stays.",
        "inputSchema": {"type": "object", "properties": {"folder": FOLDER}, "required": ["folder"]},
    },
    *hub_tools.TOOLS,
    *google_tools.TOOLS,
]

# The configuration file that this server was started with, when it is not
# the default one. Commands in rule texts must name it.
CONFIG_PATH: Path | None = None


def _text(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(cfg: Config, cache_dir: Path, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "hub_rules":
        folder = str(args.get("folder") or "").strip()
        if not folder:
            return _text("Give the path of the project folder.", error=True)
        query = urlencode({**briefing.where(folder), "channel": "mcp", "v": 2})
        try:
            _, answer = Client(cfg.server_url, cfg.token, timeout=15).request(
                "GET", f"/api/v1/briefing?{query}")
        except HubError as exc:
            return _text(f"The hub is not reachable: {exc}", error=True)
        # Work that another session left for this project belongs to the start as well.
        listed, waiting = handoff.listing(cfg, folder)
        waiting = (f"\n\n## Handoffs waiting in this project\n\n{waiting}\n\nBefore you act on one, "
                   "call handoff_take.") if listed and waiting.startswith("#") else ""
        if not answer.get("hub_rules", True):
            return _text(f"The hub serves no rules for {answer.get('project')}. This project "
                         "keeps its rules in its own files. Read those." + waiting)
        return _text((briefing.fill_commands(answer.get("text") or "", CONFIG_PATH)
                      or "No rule set applies to this folder.") + waiting)
    if name == "hub_ruleset":
        wanted = str(args.get("name") or "").strip()
        text, state = briefing.fetch_ruleset(cfg, cache_dir, wanted)
        if text is None:
            return _text(f"No rule set '{wanted}' ({state}).", error=True)
        note = "\n(The hub was not reachable. This is the last saved copy.)" if state == "cached" else ""
        return _text(briefing.fill_commands(text, CONFIG_PATH) + note)
    if name == "hub_propose_rule":
        filed, message = briefing.propose(
            cfg, str(args.get("folder") or ""), str(args.get("ruleset") or ""),
            str(args.get("text") or ""), str(args.get("reason") or ""),
            str(args.get("mode") or "add"), str(args.get("source") or "Cowork"))
        return _text(message, error=not filed)
    if name == "hub_unlock":
        folder = str(args.get("folder") or "").strip()
        info = resolve_dir(folder)
        root = info["main_repo"] or info["repo_root"]
        if not root:
            return _text(f"{folder or 'That'} is not inside a Git repository on this computer.", error=True)
        found = locks.sweep([root], locks.MANUAL_SECONDS)
        if not found:
            return _text(f"No Git lock files in {root}.")
        return _text("\n".join(locks.describe(entry) for entry in found),
                     error=any(entry["state"] != "removed" for entry in found))
    if name in hub_tools.NAMES:
        text, failed = hub_tools.call(cfg, name, args)
        return _text(text, error=failed)
    if name in google_tools.NAMES:
        text, failed = google_tools.call(cfg, name, args)
        return _text(text, error=failed)
    return _text(f"Unknown tool: {name}", error=True)


def handle(cfg: Config, cache_dir: Path, message: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. Notifications get no answer."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        # Answer with the version of the client when it names one, so that an
        # older or newer client still connects. The tool calls are the same.
        version = params.get("protocolVersion")
        return ok({
            "protocolVersion": version if isinstance(version, str) and version else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            return ok(call_tool(cfg, cache_dir, str(params.get("name")), arguments))
        except Exception as exc:  # noqa: BLE001 - a tool failure must not end the server
            return ok(_text(f"The tool failed: {exc}", error=True))
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve(cfg: Config, cache_dir: Path, stdin: BinaryIO | None = None,
          stdout: BinaryIO | None = None) -> None:
    """Read one JSON-RPC message per line until the client closes the stream."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            answer: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32700, "message": "Parse error"}}
        else:
            answer = handle(cfg, cache_dir, message) if isinstance(message, dict) else None
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


def desktop_config_path() -> Path:
    """Where the Claude desktop app keeps its list of local MCP servers."""
    import os
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / \
            "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / \
        "claude_desktop_config.json"


def install(path: Path, config_path: Path | None, remove: bool = False) -> str:
    """Add this server to the desktop app configuration, or remove it.

    Other servers and settings stay as they are. The first change keeps a
    backup beside the file.
    """
    settings: dict[str, Any] = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            return f"{path}: not valid JSON ({exc}). Nothing changed."
        if not isinstance(settings, dict):
            return f"{path}: unexpected content. Nothing changed."
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    had = SERVER_NAME in servers
    if remove:
        if not had:
            return f"{path}: no hub entry found. Nothing changed."
        del servers[SERVER_NAME]
        action = "removed"
    else:
        args = ["-m", briefing.HOOK_MARKER]
        if config_path is not None:
            args += ["--config", str(config_path)]
        servers[SERVER_NAME] = {"command": sys.executable, "args": args + ["mcp"]}
        action = "updated" if had else "added"
    if servers:
        settings["mcpServers"] = servers
    else:
        settings.pop("mcpServers", None)
    backup = path.with_name(path.name + ".claude-hub.bak")
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"{path}: hub entry {action}. Quit the Claude desktop app completely and start it again."
