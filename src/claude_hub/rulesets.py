"""Rule sets: the instructions that the hub hands to Claude Code sessions.

A rule set is one Markdown file in the ``rulesets`` folder of the hub
repository. Its frontmatter says which projects it applies to and whether
a session gets it at start or loads it on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rules import split_frontmatter

# The session hook replaces this with the command that works on its machine.
COMMAND_PLACEHOLDER = "{{RULES_COMMAND}}"


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


def load_rulesets(directory: Path | None) -> list[RuleSet]:
    """Read all rule sets. Files are read on every call, so an edit is live at once."""
    if directory is None or not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            found.append(parse_ruleset(path.stem, path.read_text(encoding="utf-8")))
        except OSError:
            continue
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
    key = (project.get("key") or "").lower()
    identities = {key, key.partition(":")[2], (project.get("name") or "").lower()}
    return any(name in identities for name in ruleset.projects)


def build_briefing(rulesets: list[RuleSet], project: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble what a session receives at its start."""
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
        lines += ["", f"## {ruleset.title}", "", ruleset.body]
    if on_demand:
        lines += [
            "", "## More rule sets", "",
            "Load a set before the work it covers. It is binding once the work touches its area.",
            f"Command: `{COMMAND_PLACEHOLDER} NAME`", "",
        ]
        for ruleset in on_demand:
            entry = f"- `{ruleset.name}`: {ruleset.description}"
            if ruleset.when:
                entry += f" Load when: {ruleset.when}"
            lines.append(entry)
    text = "\n".join(lines).strip() + "\n"
    return {
        "text": text,
        "always": [rs.name for rs in always],
        "on_demand": [rs.name for rs in on_demand],
        "tokens": max(1, len(text) // 4),
    }
