"""Derive display states for worktrees and sessions.

States are computed when a page renders, not stored, because they depend
on the current time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

ACTIVE_DAYS = 7
STALE_DAYS = 30
RUNNING_MINUTES = 10
RECENT_DAYS = 3

# Tool calls that stop a session until the person answers.
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

# States that need a decision from the person.
ACTION_STATES = ("dirty", "unpushed", "missing")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def worktree_state(wt: Mapping[str, Any], default_branch: str | None, now: datetime) -> str:
    """Return the single most important state of a worktree.

    Order matters: unsaved work outranks everything, and a clean branch that
    is contained in the default branch is safe to remove even without an
    upstream.
    """
    if wt["prunable"]:
        return "missing"
    if wt["dirty_files"]:
        return "dirty"
    on_default = bool(wt["branch"]) and wt["branch"] == default_branch
    if on_default:
        return "unpushed" if (wt["ahead"] or 0) > 0 else "main"
    if wt["merged"]:
        return "merged"
    if wt["upstream"] is None or (wt["ahead"] or 0) > 0:
        return "unpushed"
    last = parse_time(wt["last_commit_at"])
    if last is None:
        return "idle"
    age = now - last
    if age > timedelta(days=STALE_DAYS):
        return "stale"
    return "active" if age <= timedelta(days=ACTIVE_DAYS) else "idle"


def session_status(session: Mapping[str, Any], now: datetime) -> str:
    """Return "waiting", "running", "paused", or "idle".

    The collector reports with a delay, so "running" means activity within
    the last few minutes, not a live process check.
    """
    ended = parse_time(session["ended_at"])
    if ended is None:
        return "idle"
    age = now - ended
    if age > timedelta(days=RECENT_DAYS):
        return "idle"
    event = session["last_event"]
    if event == "reply" or (event == "tool_call" and session["last_tool"] in ASK_TOOLS):
        return "waiting"
    if age <= timedelta(minutes=RUNNING_MINUTES):
        return "running"
    # The turn stopped in the middle: interrupted, crashed, or held by a
    # permission prompt. The transcript does not say which.
    return "paused"


def reltime(value: str | None, now: datetime | None = None) -> str:
    moment = parse_time(value)
    if moment is None:
        return ""
    seconds = int(((now or utcnow()) - moment).total_seconds())
    if seconds < 90:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 86400 * 45:
        return f"{seconds // 86400} d ago"
    return moment.strftime("%Y-%m-%d")
