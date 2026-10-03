"""Token usage per project, day, and model."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .pricing import FIELDS, Prices
from .status import utcnow

PERIODS = (7, 30, 0)

# A subagent transcript is its own session row. Its usage belongs to the
# session that started it, and to that session's project.
_FROM = """
    FROM usage_daily u
    JOIN sessions s ON s.pk = u.session_pk
    LEFT JOIN sessions parent
           ON parent.machine_id = s.machine_id AND parent.session_id = s.parent_session_id
    LEFT JOIN accounts a ON a.id = COALESCE(parent.account_id, s.account_id)
"""
_SUMS = ", ".join(f"SUM(u.{name}) AS {name}" for name in ("messages", *FIELDS))


def empty() -> dict[str, Any]:
    return {"messages": 0, **{name: 0 for name in FIELDS}, "tokens": 0, "cost": 0.0,
            "unpriced": 0, "sessions": set()}


def _add(total: dict[str, Any], row: sqlite3.Row, prices: Prices) -> None:
    counts = {name: row[name] or 0 for name in FIELDS}
    tokens = sum(counts.values())
    total["messages"] += row["messages"] or 0
    for name, value in counts.items():
        total[name] += value
    total["tokens"] += tokens
    cost = prices.cost(row["model"], counts)
    if cost is None:
        total["unpriced"] += tokens
    else:
        total["cost"] += cost


def since_day(days: int) -> str:
    """The first UTC day of a period that ends today. 0 means everything."""
    if days <= 0:
        return ""
    return (utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def report(conn: sqlite3.Connection, prices: Prices, days: int) -> dict[str, Any]:
    """Totals for a period, split by project, workspace, model, and day."""
    rows = conn.execute(
        f"""SELECT COALESCE(parent.project_id, s.project_id) AS project_id,
                   COALESCE(parent.pk, s.pk) AS root_pk, u.day, u.model,
                   COALESCE(a.label, '') AS account, {_SUMS}
            {_FROM} WHERE u.day >= ?
            GROUP BY 1, 2, u.day, u.model, 5""",
        (since_day(days),),
    ).fetchall()
    total = empty()
    by_project: dict[int | None, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_account: dict[str, dict[str, Any]] = {}
    for row in rows:
        for bucket in (total, by_project.setdefault(row["project_id"], empty()),
                       by_model.setdefault(row["model"], empty()),
                       by_day.setdefault(row["day"], empty()),
                       by_account.setdefault(row["account"], empty())):
            _add(bucket, row, prices)
            bucket["sessions"].add(row["root_pk"])

    names = {row["id"]: row for row in conn.execute(
        """SELECT p.id, p.name, p.archived, w.name AS workspace FROM projects p
           LEFT JOIN workspaces w ON w.id = p.workspace_id""")}

    def merge(into: dict[str, Any], bucket: dict[str, Any]) -> None:
        for name in ("messages", *FIELDS, "tokens", "cost", "unpriced"):
            into[name] += bucket[name]
        into["sessions"] |= bucket["sessions"]

    projects = []
    by_workspace: dict[str, dict[str, Any]] = {}
    archived, archived_count = empty(), 0
    for project_id, bucket in by_project.items():
        info = names.get(project_id)
        workspace = (info["workspace"] if info else None) or ""
        merge(by_workspace.setdefault(workspace, empty()), bucket)
        if info and info["archived"]:
            # Archived projects leave the list, but their usage stays in the totals.
            merge(archived, bucket)
            archived_count += 1
            continue
        projects.append({**bucket, "id": project_id, "workspace": workspace,
                         "name": info["name"] if info else "No project"})
    if archived_count:
        projects.append({**archived, "id": None, "workspace": "",
                         "name": f"Archived projects ({archived_count})"})

    def ranked(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: (-item["cost"], -item["tokens"]))

    def share(bucket: dict[str, Any]) -> float:
        # Without prices, the share falls back to tokens.
        if total["cost"] > 0:
            return 100 * bucket["cost"] / total["cost"]
        return 100 * bucket["tokens"] / total["tokens"] if total["tokens"] else 0.0

    result = {
        "total": total,
        "projects": ranked(projects),
        "workspaces": ranked([{**b, "name": name or "Not assigned"}
                              for name, b in by_workspace.items()]),
        "models": ranked([{**b, "name": name, "priced": prices.rates(name) is not None}
                          for name, b in by_model.items()]),
        "days": [{**b, "day": day} for day, b in sorted(by_day.items(), reverse=True)],
        "accounts": ranked([{**b, "name": name or "unknown"} for name, b in by_account.items()]),
    }
    for group in ("projects", "workspaces", "models", "days", "accounts"):
        for item in result[group]:
            item["share"] = share(item)
    top = max((item["cost"] for item in result["days"]), default=0.0)
    for item in result["days"]:
        item["bar"] = 100 * item["cost"] / top if top else 0.0
    read = total["input"] + total["cache_write_5m"] + total["cache_write_1h"] + total["cache_read"]
    result["cached_share"] = 100 * total["cache_read"] / read if read else 0.0
    return result


def for_project(conn: sqlite3.Connection, prices: Prices, project_id: int, days: int) -> dict[str, Any]:
    rows = conn.execute(
        f"""SELECT u.model, {_SUMS} {_FROM}
            WHERE COALESCE(parent.project_id, s.project_id) = ? AND u.day >= ?
            GROUP BY u.model""",
        (project_id, since_day(days)),
    ).fetchall()
    total = empty()
    for row in rows:
        _add(total, row, prices)
    return total


def for_session(conn: sqlite3.Connection, prices: Prices, pk: int) -> dict[str, Any]:
    """Usage of one session, with the part that its subagents used."""
    rows = conn.execute(
        f"""SELECT u.model, s.pk != ? AS subagent, {_SUMS} {_FROM}
            WHERE COALESCE(parent.pk, s.pk) = ?
            GROUP BY u.model, 2""",
        (pk, pk),
    ).fetchall()
    total, subagents = empty(), empty()
    for row in rows:
        _add(total, row, prices)
        if row["subagent"]:
            _add(subagents, row, prices)
    total["subagents"] = subagents
    return total
