"""Read instruction files: frontmatter and the split into single rules.

Shared by the collector and the server. Standard library only.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_KEY = re.compile(r"^([A-Za-z][\w-]*):\s*(.*)$")

# Kinds whose files hold many independent rules. The others are one unit.
SPLIT_KINDS = {"claude_md", "rule"}


def content_sha(text: str) -> str:
    """Hash text with whitespace collapsed, so that reformatting is not drift."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (fields, body). Reads the few YAML shapes that Claude Code uses.

    A full YAML parser would add a dependency to every machine. The fields
    of interest are plain strings and lists of strings.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text

    fields: dict[str, str] = {}
    key: str | None = None
    for line in lines[1:end]:
        match = _KEY.match(line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip().strip("\"'")
        elif key and line.strip().startswith("- "):
            item = line.strip()[2:].strip().strip("\"'")
            fields[key] = f"{fields[key]}, {item}" if fields[key] else item
        elif key and line.startswith((" ", "\t")) and line.strip():
            # A folded or continued value.
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    for name, value in fields.items():
        # Inline lists: [a, b] becomes "a, b". Block markers such as ">-" are dropped.
        if value.startswith("[") and value.endswith("]"):
            value = ", ".join(part.strip().strip("\"'") for part in value[1:-1].split(","))
        fields[name] = re.sub(r"^[>|][+-]?\s*", "", value)
    return fields, "\n".join(lines[end + 1:]).lstrip("\n")


def split_rules(body: str) -> list[dict[str, Any]]:
    """Split Markdown into one item per heading. Headings in code blocks are ignored."""
    items: list[dict[str, Any]] = []
    heading: str | None = None
    level = 0
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if heading is not None or content:
            items.append({"heading": heading, "level": level, "content": content,
                          "sha": content_sha(f"{heading or ''}\n{content}")})

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            heading, level, buffer = match.group(2), len(match.group(1)), []
        else:
            buffer.append(line)
    flush()
    return items


def describe(kind: str, rel_path: str, text: str) -> dict[str, Any]:
    """Derive the stored fields of one instruction file."""
    fields, body = split_frontmatter(text)
    if kind in SPLIT_KINDS:
        items = split_rules(body)
    else:
        items = [{"heading": None, "level": 0, "content": body.strip(),
                  "sha": content_sha(body)}]
    name = fields.get("name")
    if not name:
        parts = rel_path.replace("\\", "/").split("/")
        if kind == "skill" and len(parts) > 1:
            name = parts[-2]  # A skill is named after its folder.
        elif kind == "claude_md":
            name = parts[-1]  # Keeps CLAUDE.md and CLAUDE.local.md apart.
        else:
            name = parts[-1].removesuffix(".md")
    return {
        "name": name,
        "description": fields.get("description"),
        "applies_to": fields.get("paths") or fields.get("globs"),
        "sha": content_sha(text),
        "items": items,
    }
