from datetime import datetime, timedelta, timezone

from claude_hub.server.status import reltime, session_status, worktree_state

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def ago(**kwargs) -> str:
    return (NOW - timedelta(**kwargs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def wt(**overrides):
    base = {"prunable": 0, "dirty_files": 0, "branch": "feature", "merged": 0, "upstream": "origin/feature",
            "ahead": 0, "behind": 0, "last_commit_at": ago(days=1)}
    return {**base, **overrides}


def test_worktree_states():
    assert worktree_state(wt(), "main", NOW) == "active"
    assert worktree_state(wt(last_commit_at=ago(days=12)), "main", NOW) == "idle"
    assert worktree_state(wt(last_commit_at=ago(days=40)), "main", NOW) == "stale"
    assert worktree_state(wt(ahead=2), "main", NOW) == "unpushed"
    assert worktree_state(wt(upstream=None), "main", NOW) == "unpushed"
    assert worktree_state(wt(merged=1), "main", NOW) == "merged"
    assert worktree_state(wt(prunable=1), "main", NOW) == "missing"


def test_unsaved_work_outranks_everything_else():
    assert worktree_state(wt(dirty_files=3, merged=1, last_commit_at=ago(days=90)), "main", NOW) == "dirty"


def test_merged_branch_without_upstream_is_safe_to_remove():
    assert worktree_state(wt(merged=1, upstream=None), "main", NOW) == "merged"


def test_default_branch_is_never_called_merged():
    assert worktree_state(wt(branch="main", merged=1), "main", NOW) == "main"
    assert worktree_state(wt(branch="main", merged=0, ahead=1), "main", NOW) == "unpushed"


def session(event, tool=None, **age):
    return {"ended_at": ago(**age), "last_event": event, "last_tool": tool}


def test_session_status():
    assert session_status(session("reply", hours=2), NOW) == "waiting"
    assert session_status(session("tool_call", "AskUserQuestion", hours=2), NOW) == "waiting"
    assert session_status(session("tool_call", "Bash", minutes=2), NOW) == "running"
    assert session_status(session("tool_result", minutes=2), NOW) == "running"
    assert session_status(session("tool_call", "Bash", hours=2), NOW) == "paused"
    assert session_status(session("reply", days=5), NOW) == "idle"
    assert session_status({"ended_at": None, "last_event": None, "last_tool": None}, NOW) == "idle"


def test_reltime():
    assert reltime(ago(seconds=20), NOW) == "now"
    assert reltime(ago(minutes=5), NOW) == "5 min ago"
    assert reltime(ago(hours=3), NOW) == "3 h ago"
    assert reltime(ago(days=4), NOW) == "4 d ago"
    assert reltime(ago(days=100), NOW) == "2026-06-24"
    assert reltime(None, NOW) == ""
