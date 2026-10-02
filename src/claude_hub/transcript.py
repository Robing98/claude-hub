"""Read Claude Code transcripts (JSONL).

The transcript format is internal to Claude Code and changes between
versions. Everything here therefore reads defensively: unknown line types
and missing fields are skipped, never treated as errors.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# User-role lines that Claude Code writes itself, not text the person typed.
_NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
    "<user-prompt-submit-hook>",
)

# Raise this when parse_meta derives something new. The server re-parses
# stored transcripts that were read with an older version.
PARSER_VERSION = 2

_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
_CD = re.compile(r"""(?:^|&&|;|\|)\s*cd\s+(?:/d\s+)?["']?((?:[A-Za-z]:[\\/]|/)[^"'&;|\n]*)""")
_PATH_KEYS = ("file_path", "path", "notebook_path")
WORK_DIR_LIMIT = 40

TITLE_LENGTH = 90
PROMPT_LENGTH = 600
TOOL_INPUT_LENGTH = 600
TOOL_RESULT_LENGTH = 1500


@dataclass
class SessionMeta:
    session_id: str | None = None
    title: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    user_prompts: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    entrypoint: str | None = None
    cc_version: str | None = None
    models: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    # What the transcript ends with: "prompt", "reply", "tool_call", or "tool_result".
    last_event: str | None = None
    last_tool: str | None = None
    # Folders that the session touched, with a count. A session that starts
    # in a parent folder is assigned to a repository through these.
    work_dirs: dict[str, int] = field(default_factory=dict)
    lines: int = 0
    parse_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode(line: bytes | str) -> dict[str, Any] | None:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def human_text(obj: dict[str, Any]) -> str | None:
    """Return the text a person typed, or None for every other line."""
    if obj.get("type") != "user" or obj.get("isSidechain") or obj.get("isMeta"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip() and any(b.get("type") in ("image", "document") for b in blocks):
            text = "[attachment]"
    else:
        return None
    text = _REMINDER.sub("", text).strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None
    return text


def _parent(path: str) -> str:
    return re.split(r"[\\/](?=[^\\/]*$)", path.rstrip("\\/"), maxsplit=1)[0]


def _tool_dirs(block: dict[str, Any]) -> Iterator[str]:
    """Yield the absolute folders that one tool call refers to."""
    data = block.get("input")
    if not isinstance(data, dict):
        return
    for key in _PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str) and _ABSOLUTE.match(value):
            # "path" names a folder for search tools, the other keys name a file.
            yield value if key == "path" else _parent(value)
    command = data.get("command")
    if isinstance(command, str):
        for match in _CD.finditer(command):
            yield match.group(1).strip()


def _has_tool_result(obj: dict[str, Any]) -> bool:
    content = (obj.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def parse_meta(lines: Iterable[bytes | str]) -> SessionMeta:
    """Derive the session summary fields from a transcript."""
    meta = SessionMeta()
    dirs: dict[str, int] = {}
    models: list[str] = []
    ai_title = legacy_summary = None
    counted_messages: set[str] = set()

    for raw in lines:
        if not raw.strip():
            continue
        meta.lines += 1
        obj = _decode(raw)
        if obj is None:
            meta.parse_errors += 1
            continue

        kind = obj.get("type")
        meta.session_id = meta.session_id or obj.get("sessionId")
        stamp = obj.get("timestamp")
        if isinstance(stamp, str):
            if meta.started_at is None or stamp < meta.started_at:
                meta.started_at = stamp
            if meta.ended_at is None or stamp > meta.ended_at:
                meta.ended_at = stamp

        if isinstance(obj.get("cwd"), str):
            dirs[obj["cwd"]] = dirs.get(obj["cwd"], 0) + 1
        # Subagent lines carry their own directory and must not move the session.
        if not obj.get("isSidechain"):
            meta.cwd = meta.cwd or obj.get("cwd")
            branch = obj.get("gitBranch")
            if branch and branch != "HEAD":
                meta.git_branch = branch
            meta.entrypoint = meta.entrypoint or obj.get("entrypoint")
            meta.cc_version = obj.get("version") or meta.cc_version

        if kind == "ai-title" and obj.get("aiTitle"):
            ai_title = obj["aiTitle"]
        elif kind == "summary" and obj.get("summary"):
            legacy_summary = obj["summary"]
        elif kind == "cost-state" and isinstance(obj.get("totalCostUSD"), (int, float)):
            meta.cost_usd = float(obj["totalCostUSD"])
        elif kind == "user":
            text = human_text(obj)
            if text:
                meta.user_prompts += 1
                meta.first_prompt = meta.first_prompt or _shorten(text, PROMPT_LENGTH)
                meta.last_prompt = _shorten(text, PROMPT_LENGTH)
                meta.last_event = "prompt"
            elif not obj.get("isSidechain") and _has_tool_result(obj):
                meta.last_event = "tool_result"
        if kind == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    for folder in _tool_dirs(block):
                        dirs[folder] = dirs.get(folder, 0) + 1
        if kind == "assistant" and not obj.get("isSidechain"):
            message = obj.get("message") or {}
            # One API message is written as several lines, one per content block.
            message_id = message.get("id") or obj.get("uuid") or str(meta.lines)
            if message_id not in counted_messages:
                counted_messages.add(message_id)
                meta.assistant_messages += 1
            model = message.get("model")
            if model and model not in models and not model.startswith("<"):
                models.append(model)
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    meta.tool_calls += 1
                    meta.last_event, meta.last_tool = "tool_call", block.get("name")
                elif block.get("type") == "text" and block.get("text", "").strip():
                    # Text that precedes a tool call belongs to an unfinished turn.
                    unfinished = message.get("stop_reason") == "tool_use"
                    meta.last_event = "tool_call" if unfinished else "reply"

    meta.models = models
    top = sorted(dirs.items(), key=lambda item: -item[1])[:WORK_DIR_LIMIT]
    meta.work_dirs = dict(top)
    title = ai_title or legacy_summary or meta.first_prompt
    meta.title = _shorten(title.splitlines()[0], TITLE_LENGTH) if title else None
    return meta


def first_cwd(lines: Iterable[bytes | str]) -> str | None:
    """Find the working directory without a full parse. Used by the collector."""
    for raw in lines:
        if b'"cwd"' not in raw if isinstance(raw, bytes) else '"cwd"' not in raw:
            continue
        obj = _decode(raw)
        if obj and obj.get("cwd") and not obj.get("isSidechain"):
            return obj["cwd"]
    return None


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or f"[{item.get('type', 'data')}]")
        return "\n".join(parts)
    return ""


def iter_turns(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    """Yield display turns: one per human prompt and one per assistant reply.

    Consecutive assistant lines are merged, because Claude Code writes each
    content block of a reply as its own line.
    """
    current: dict[str, Any] | None = None
    tools_by_id: dict[str, dict[str, Any]] = {}

    for raw in lines:
        obj = _decode(raw)
        if obj is None or obj.get("isSidechain"):
            continue
        kind = obj.get("type")

        if kind == "user":
            text = human_text(obj)
            if text is not None:
                if current:
                    yield current
                    current = None
                yield {"role": "user", "ts": obj.get("timestamp"), "text": text}
                continue
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool = tools_by_id.get(block.get("tool_use_id", ""))
                        if tool is not None:
                            tool["result"] = _shorten(_tool_result_text(block), TOOL_RESULT_LENGTH)
                            tool["error"] = bool(block.get("is_error"))

        elif kind == "assistant":
            if current is None:
                current = {"role": "assistant", "ts": obj.get("timestamp"), "parts": []}
            for block in (obj.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    current["parts"].append({"kind": "text", "text": block["text"].strip()})
                elif block.get("type") == "tool_use":
                    tool = {
                        "kind": "tool",
                        "name": block.get("name", "tool"),
                        "input": _shorten(
                            json.dumps(block.get("input", {}), ensure_ascii=False, indent=1),
                            TOOL_INPUT_LENGTH,
                        ),
                        "result": None,
                        "error": False,
                    }
                    tools_by_id[block.get("id", "")] = tool
                    current["parts"].append(tool)

    if current:
        yield current
