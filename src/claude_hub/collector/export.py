"""Write the rules of a project into its folder, for sessions that cannot ask the hub.

Cowork and other tools without the session hook read the exported files.
The files live in one folder that the hub owns, and Git never sees them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from .briefing import where
from .client import Client, HubError
from .config import Config

FOLDER = ".claude-hub"
MARKER = "RULES.md"
EXCLUDE_LINE = f"/{FOLDER}/"


def is_exported(folder: Path) -> bool:
    return (folder / FOLDER / MARKER).is_file()


def _git_exclude(folder: Path) -> Path | None:
    """The local exclude file of the repository that ``folder`` belongs to."""
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip())
    return (common if common.is_absolute() else folder / common) / "info" / "exclude"


def hide_from_git(folder: Path) -> str:
    """Keep the export out of Git through the local exclude file.

    Unlike .gitignore, that file is not part of the repository, so the
    repository shows no trace of the export.
    """
    exclude = _git_exclude(folder)
    if exclude is None:
        return "not a Git repository, nothing to hide"
    try:
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if EXCLUDE_LINE in (line.strip() for line in lines):
            return "already hidden from Git"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(("" if not lines or lines[-1] == "" else "\n") + EXCLUDE_LINE + "\n")
    except OSError as exc:
        return f"could not write {exclude}: {exc}"
    return "hidden from Git"


def export_rules(cfg: Config, folder: Path) -> tuple[bool, str]:
    """Write or refresh the export in ``folder``. Returns (changed, message)."""
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=30).request(
            "GET", "/api/v1/export?" + urlencode(where(str(folder))))
    except HubError as exc:
        return False, f"{folder}: {exc}"
    if not answer.get("hub_rules"):
        name = answer.get("project") or "this folder"
        return False, (f"{folder}: the hub serves no rules for {name}. Turn on "
                       '"Sessions get their rules from the hub" on its project page first.')
    target = folder / FOLDER
    wanted = {target / name: text for name, text in answer["files"].items()
              if name == MARKER or (name.startswith("rules/") and name.count("/") == 1
                                    and ".." not in name and name.endswith(".md"))}
    changed = 0
    for path, text in wanted.items():
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed += 1
    # Sets that no longer apply leave the folder. Only generated files are touched.
    rules_dir = target / "rules"
    if rules_dir.is_dir():
        for path in rules_dir.glob("*.md"):
            if path not in wanted:
                path.unlink()
                changed += 1
    hidden = hide_from_git(folder)
    return bool(changed), (f"{target}: {len(wanted)} files, {changed} changed, {hidden}.")
