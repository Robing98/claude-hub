"""Rule sets: the instructions that the hub hands to Claude Code sessions.

A rule set is one Markdown file in the ``rulesets`` folder of the hub
repository. Its frontmatter says which projects it applies to and whether
a session gets it at start or loads it on demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rules import split_frontmatter

# The session hook replaces these with the commands that work on its machine.
COMMAND_PLACEHOLDER = "{{RULES_COMMAND}}"
HUB_PLACEHOLDER = "{{HUB_COMMAND}}"
# Where an exported copy of the rules lives inside a project folder.
EXPORT_DIR = ".claude-hub"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# How a session loads an on-demand set and proposes a change, by the way
# the rules reach it: the session hook, an exported file, or the connector.
CHANNELS = {
    "hook": {
        "load": f"Command: `{COMMAND_PLACEHOLDER} NAME`",
        "propose": f'Command: `{HUB_PLACEHOLDER} rules propose SET "TEXT" --reason "WHY"`',
    },
    "file": {
        "load": f"Read the file `{EXPORT_DIR}/rules/NAME.md` in the project folder.",
        "propose": "Call the tool `hub_propose_rule` if you have it. If not, tell Robin the set "
                   "and the text.",
    },
    "mcp": {
        "load": "Call the tool `hub_ruleset` with the name of the set.",
        "propose": "Call the tool `hub_propose_rule`.",
    },
}


def valid_name(name: str) -> bool:
    """A set name becomes a file name, so it stays plain."""
    return bool(_NAME.match(name))


def split_file(text: str) -> tuple[str, str]:
    """Split a rule set file into its frontmatter block and its body, unparsed."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            cut = text.find("\n", end + 1)
            cut = len(text) if cut == -1 else cut + 1
            return text[:cut], text[cut:]
    return "", text


def with_body(text: str, body: str) -> str:
    """Replace the body of a rule set file and keep its frontmatter."""
    return split_file(text)[0] + body.strip() + "\n"


def with_addition(text: str, addition: str) -> str:
    """Add text to the end of a rule set file."""
    head, body = split_file(text)
    return head + body.rstrip() + "\n" + addition.strip() + "\n"


@dataclass
class RuleSet:
    name: str
    title: str
    description: str = ""
    load: str = "always"
    when: str = ""
    all_projects: bool = False
    workspaces: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    ai: str | None = None
    order: int = 100
    body: str = ""
    # The whole file, as it is delivered now.
    text: str = ""
    # True while the hub holds a version that differs from the repository.
    edited: bool = False
    in_repo: bool = True

    @property
    def tokens(self) -> int:
        """A rough size. One token is about four characters of English text."""
        return max(1, len(self.body) // 4)

    @property
    def scope(self) -> str:
        parts = []
        if self.all_projects:
            parts.append("all projects")
        parts += [f"workspace {name}" for name in self.workspaces]
        parts += [f"project {name}" for name in self.projects]
        text = ", ".join(parts) or "nothing"
        if self.ai:
            text += f", only where AI traces are {'allowed' if self.ai == 'yes' else 'not allowed'}"
        return text


def _names(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_ruleset(name: str, text: str) -> RuleSet:
    fields, body = split_frontmatter(text)
    load = fields.get("load", "always").strip().lower()
    ai = fields.get("ai", "").strip().lower()
    try:
        order = int(fields.get("order", "100"))
    except ValueError:
        order = 100
    return RuleSet(
        name=name,
        title=fields.get("title") or name,
        description=fields.get("description", ""),
        load="on-demand" if load in ("on-demand", "on_demand", "demand") else "always",
        when=fields.get("when", ""),
        all_projects=fields.get("all", "").strip().lower() in ("true", "yes", "1"),
        workspaces=_names(fields.get("workspaces")),
        projects=[name.lower() for name in _names(fields.get("projects"))],
        ai=ai if ai in ("yes", "no") else None,
        order=order,
        body=body.strip(),
    )


def read_folder(directory: Path | None) -> dict[str, str]:
    """Return {set name: file text} for one folder of rule sets."""
    if directory is None or not directory.is_dir():
        return {}
    found = {}
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            found[path.stem] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return found


def load_rulesets(directory: Path | None, live: Path | None = None) -> list[RuleSet]:
    """Read all rule sets. Files are read on every call, so an edit is live at once.

    ``directory`` holds the sets from the repository. ``live`` holds the sets
    that were changed in the hub since the last deployment. A live file wins.
    """
    shipped = read_folder(directory)
    changed = read_folder(live)
    found = []
    for name, text in {**shipped, **changed}.items():
        ruleset = parse_ruleset(name, text)
        ruleset.text = text
        ruleset.in_repo = name in shipped
        ruleset.edited = name in changed and changed[name] != shipped.get(name)
        found.append(ruleset)
    return sorted(found, key=lambda rs: (rs.order, rs.name))


def applies(ruleset: RuleSet, project: dict[str, Any] | None) -> bool:
    """Decide whether a rule set belongs to a project.

    ``project`` has ``key``, ``name``, ``workspace``, and ``ai_ok``. A
    session outside any project passes None and gets only the sets for all
    projects, under the default that AI traces are not allowed.
    """
    ai_ok = bool(project and project.get("ai_ok"))
    if ruleset.ai == "yes" and not ai_ok:
        return False
    if ruleset.ai == "no" and ai_ok:
        return False
    if ruleset.all_projects:
        return True
    if project is None:
        return False
    workspace = (project.get("workspace") or "").lower()
    if workspace and workspace in (name.lower() for name in ruleset.workspaces):
        return True
    identities = {(project.get("name") or "").lower()}
    for key in (project.get("key") or "", *(project.get("aliases") or ())):
        identities |= {key.lower(), key.lower().partition(":")[2]}
    return any(name in identities for name in ruleset.projects)


_HEADING = re.compile(r"^(#{1,6})(?=\s)")
SET_BODY_LEVEL = 3


def nest_headings(body: str) -> str:
    """Shift the headings of a set body below the title of the set.

    In a briefing every set title is a second-level heading. A body that
    uses the same level would read as several sets.
    """
    levels, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            levels.append(len(match.group(1)))
    shift = SET_BODY_LEVEL - min(levels) if levels else 0
    if shift <= 0:
        return body
    result, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and _HEADING.match(line):
            line = "#" * shift + line
        result.append(line)
    return "\n".join(result)


def build_briefing(rulesets: list[RuleSet], project: dict[str, Any] | None,
                   channel: str = "hook", propose: bool = False) -> dict[str, Any]:
    """Assemble what a session receives at its start.

    ``channel`` picks the wording for loading a set. ``propose`` adds how a
    session proposes a rule change. Collectors that predate proposals do not
    ask for it, because they cannot fill in the command.
    """
    how = CHANNELS.get(channel, CHANNELS["hook"])
    mine = [rs for rs in rulesets if applies(rs, project)]
    always = [rs for rs in mine if rs.load == "always"]
    on_demand = [rs for rs in mine if rs.load == "on-demand"]
    if not mine:
        return {"text": "", "always": [], "on_demand": [], "tokens": 0}

    lines = ["# Session rules from the hub", ""]
    if project:
        workspace = project.get("workspace") or "no workspace"
        traces = "allowed" if project.get("ai_ok") else "not allowed"
        lines.append(f"Project: {project.get('name')} ({workspace}). AI traces in Git: {traces}.")
    lines.append("These rules are binding for this session.")
    for ruleset in always:
        lines += ["", f"## {ruleset.title}", "", nest_headings(ruleset.body)]
    if on_demand:
        lines += [
            "", "## More rule sets", "",
            "Load a set before the work it covers. It is binding once the work touches its area.",
            how["load"], "",
        ]
        for ruleset in on_demand:
            entry = f"- `{ruleset.name}`: {ruleset.description}"
            if ruleset.when:
                entry += f" Load when: {ruleset.when}"
            lines.append(entry)
    if propose:
        lines += [
            "", "## Changing a rule", "",
            "The rules live in the hub. Do not edit rule files in a project. When a rule is "
            "wrong or missing, propose the change. Robin decides in the hub.",
            how["propose"],
        ]
    text = "\n".join(lines).strip() + "\n"
    return {
        "text": text,
        "always": [rs.name for rs in always],
        "on_demand": [rs.name for rs in on_demand],
        "tokens": max(1, len(text) // 4),
    }
