"""Connector tools for the hub itself: its state, routines, handoffs, and waking machines."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import handoff
from .client import Client, HubError
from .config import Config

INSTRUCTIONS = (
    "Sessions hand work to each other through the hub. To leave a text for another lane of a "
    "project, call handoff_send. To see what waits for you, call handoff_list with the project "
    "folder, then handoff_take before you act on one, and handoff_done afterwards. hub_status "
    "shows the state of the hub without a browser."
)

FOLDER = {"type": "string",
          "description": "Absolute path of the project folder on Robin's computer, "
                         "for example D:\\dev\\orbis."}
LANE = {"type": "string",
        "description": "A lane is a line of work in a project, for example 'design' or 'code'."}

TOOLS = [
    {
        "name": "hub_status",
        "title": "State of the hub",
        "description": "Return the state of the hub: version, what waits for Robin, machines and "
                       "when they last reported, Google accounts, open rule proposals, and "
                       "handoffs. Use it instead of opening the hub in a browser.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "handoff_send",
        "title": "Leave a text for another session",
        "description": "Leave a text for another session of the same project, for example a design "
                       "brief for the code lane. A Claude Code session in that project receives it "
                       "with its next prompt. Write the text so that it stands on its own.",
        "inputSchema": {"type": "object",
                        "properties": {"folder": FOLDER, "title": {"type": "string"},
                                       "text": {"type": "string", "description": "The text, in Markdown."},
                                       "to": {**LANE, "description": "The lane it is meant for. " + LANE["description"]},
                                       "from": {**LANE, "description": "Your own lane. " + LANE["description"]}},
                        "required": ["folder", "title", "text"]},
    },
    {
        "name": "handoff_list",
        "title": "Handoffs of a project",
        "description": "List the handoffs of the project that a folder belongs to. By default only "
                       "the open ones.",
        "inputSchema": {"type": "object",
                        "properties": {"folder": FOLDER,
                                       "all": {"type": "boolean",
                                               "description": "Also taken and done ones."}},
                        "required": ["folder"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "handoff_take",
        "title": "Take a handoff",
        "description": "Return the whole text of a handoff and mark it as taken by you. Call it "
                       "before you act on a handoff. If the answer says that another session has "
                       "it, do not work on it.",
        "inputSchema": {"type": "object",
                        "properties": {"id": {"type": "integer"},
                                       "by": {**LANE, "description": "Your own lane. " + LANE["description"]}},
                        "required": ["id"]},
    },
    {
        "name": "handoff_done",
        "title": "Finish a handoff",
        "description": "Mark a handoff as done, with what came of it in one sentence.",
        "inputSchema": {"type": "object",
                        "properties": {"id": {"type": "integer"}, "result": {"type": "string"}},
                        "required": ["id"]},
    },
    {
        "name": "routine_list",
        "title": "Routines",
        "description": "List Robin's routines: things that come back on a schedule, such as a "
                       "checkup or a new prescription, with their next due day and state.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "routine_draft",
        "title": "Draft a routine",
        "description": "File a routine as a draft. It is turned off until Robin turns it on in "
                       "the hub. A routine only reminds. It can carry a prepared mail that Robin "
                       "sends himself.",
        "inputSchema": {"type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "every": {"type": "integer", "minimum": 1},
                            "unit": {"type": "string", "enum": ["days", "weeks", "months"]},
                            "next_due": {"type": "string", "description": "The next due day, 2026-11-03."},
                            "anchor": {"type": "string", "enum": ["done", "due"],
                                       "description": "'done': the next time counts from the day it "
                                                      "was done, as for an appointment. 'due': from "
                                                      "the due day, as for a supply that runs out."},
                            "lead_days": {"type": "integer", "minimum": 0, "maximum": 365,
                                          "description": "Remind this many days before the due day."},
                            "note": {"type": "string"},
                            "mail_to": {"type": "string"}, "mail_subject": {"type": "string"},
                            "mail_text": {"type": "string"},
                            "source": {"type": "string", "description": "Who drafts it."}},
                        "required": ["name", "every", "unit", "next_due"]},
    },
    {
        "name": "machine_wake",
        "title": "Wake a computer",
        "description": "Let the hub wake one of Robin's computers over the network. Only when "
                       "Robin asked for it. The computer needs up to a minute, and it stops at "
                       "its sign-in screen.",
        "inputSchema": {"type": "object",
                        "properties": {"name": {"type": "string",
                                                "description": "Name of the machine, as hub_status lists it."}},
                        "required": ["name"]},
    },
]

NAMES = {tool["name"] for tool in TOOLS}


def status_text(answer: dict[str, Any]) -> str:
    feed = answer["feed"]
    lines = [f"Hub {answer['server_version']} (schema {answer['schema_version']}), asked from the "
             f"machine {answer['machine']}.",
             f"Now: {feed['headline']}" + (f" ({feed['detail']})" if feed.get("detail") else "")]
    lines += [f"Machine {m['name']} ({m['platform'] or 'unknown'}): last report {m['last_seen'] or 'never'}"
              + (f", connector used {m['connector_seen']}" if m.get("connector_seen") else "")
              for m in answer["machines"]]
    if answer["google_accounts"]:
        lines += [f"Google account {a['label']}: {a['email']}, "
                  + (f"problem: {a['last_error']}" if a["last_error"] else "works")
                  for a in answer["google_accounts"]]
    else:
        lines.append("No Google account is connected.")
    lines += [f"Rule proposal {p['id']} for {p['ruleset']} waits for Robin"
              for p in answer["rule_proposals"]]
    lines.append(f"Proposed events: {feed.get('event_proposals', 0)}. Open handoffs: "
                 f"{answer['open_handoffs']}. Routines due: {feed.get('routines_due', 0)}. "
                 f"Stuck Git locks: {feed.get('stuck_locks', 0)}.")
    return "\n".join(lines)


def routine_line(item: dict[str, Any]) -> str:
    return (f"#{item['id']} {item['name']}: every {item['interval']}, next due {item['next_due']} "
            f"({item['state']})" + (f", last done {item['last_done']}" if item.get("last_done") else "")
            + (", has a prepared mail" if item.get("mail_to") else ""))


def call(cfg: Config, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool. Returns the text and whether it is an error."""
    hub = Client(cfg.server_url, cfg.token, timeout=30)
    try:
        if name == "hub_status":
            _, answer = hub.request("GET", "/api/v1/status")
            return status_text(answer), False
        if name == "handoff_send":
            ok, text = handoff.send(cfg, str(args.get("folder") or ""), str(args.get("title") or ""),
                                    str(args.get("text") or ""), str(args.get("to") or ""),
                                    str(args.get("from") or ""))
            return text, not ok
        if name == "handoff_list":
            ok, text = handoff.listing(cfg, str(args.get("folder") or ""),
                                       "all" if args.get("all") else "open")
            return text, not ok
        if name == "handoff_take":
            ok, text = handoff.show(cfg, int(args.get("id") or 0), take=True, by=str(args.get("by") or ""))
            return text, not ok
        if name == "handoff_done":
            ok, text = handoff.done(cfg, int(args.get("id") or 0), str(args.get("result") or ""))
            return text, not ok
        if name == "routine_list":
            _, answer = hub.request("GET", "/api/v1/routines")
            return "\n".join(routine_line(item) for item in answer["routines"]) or "No routines yet.", False
        if name == "routine_draft":
            body = {key: args[key] for key in (
                "name", "every", "unit", "next_due", "anchor", "lead_days", "note", "mail_to",
                "mail_subject", "mail_text", "source") if args.get(key) is not None}
            _, answer = hub.request("POST", "/api/v1/routines", body)
            return (f"Routine {answer['id']} is filed as a draft. It is turned off until Robin turns "
                    "it on in the hub under Routines."), False
        if name == "machine_wake":
            _, answer = hub.request("POST", f"/api/v1/machines/{quote(str(args.get('name') or ''), safe='')}/wake")
            return (f"The wake signal for {answer['machine']} is sent. Its last report was "
                    f"{answer['last_seen'] or 'never'}. Call hub_status in a minute to see whether it is up."), False
    except HubError as exc:
        return handoff.reason(exc), True
    return f"Unknown tool: {name}", True
