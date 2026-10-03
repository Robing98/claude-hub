"""Handoffs on the side of a session: send, list, take, finish, and receive with a prompt."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .briefing import where
from .client import Client, HubError
from .config import Config

# The prompt hook runs before every prompt. It must be quick, also when the
# hub cannot be reached, for example on a laptop away from home.
PROMPT_TIMEOUT = 3
OFFLINE_PAUSE = 300
WHERE_KEEP = 24 * 3600


def reason(exc: HubError) -> str:
    text = str(exc)
    _, _, payload = text.partition(": ")
    try:
        detail = json.loads(payload).get("detail")
    except (ValueError, AttributeError):
        return text
    return detail if isinstance(detail, str) else text


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def where_kept(cache_dir: Path, folder: str) -> dict[str, str]:
    """``where`` asks Git several times. Keep its answer per folder for a day."""
    path = cache_dir / "where.json"
    kept = _load(path)
    entry = kept.get(folder)
    if isinstance(entry, dict) and time.time() - float(entry.get("at", 0)) < WHERE_KEEP:
        return entry["where"]
    found = where(folder)
    kept = {key: value for key, value in kept.items()
            if isinstance(value, dict) and time.time() - float(value.get("at", 0)) < WHERE_KEEP}
    kept[folder] = {"at": time.time(), "where": found}
    _save(path, kept)
    return found


def new_for_prompt(cfg: Config, cache_dir: Path, cwd: str, session_id: str) -> list[dict[str, Any]]:
    """Handoffs that this session has not seen yet. Empty when the hub does not answer."""
    marker = cache_dir / "hub-offline.json"
    if time.time() - float(_load(marker).get("at", 0)) < OFFLINE_PAUSE:
        return []
    query = urlencode({"session": session_id, **where_kept(cache_dir, cwd)})
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=PROMPT_TIMEOUT).request(
            "GET", f"/api/v1/handoffs/new?{query}")
    except (HubError, ValueError):
        # Do not make the next prompts wait for a hub that is away.
        _save(marker, {"at": time.time()})
        return []
    return answer.get("handoffs") or []


def lanes(item: dict[str, Any]) -> str:
    sender = f'from the lane "{item["from"]}"' if item.get("from") else "from a session"
    return sender + (f', for the lane "{item["to"]}"' if item.get("to") else ", for any lane")


def render_new(items: list[dict[str, Any]], command: str) -> str:
    """What a Claude Code session is shown when handoffs wait in its project."""
    if not items:
        return ""
    parts = ["[Claude hub] Another session, or Robin, left work for a session in this project. "
             "If a handoff names a lane that is not yours, leave it alone and do not bring it up. "
             "Otherwise tell Robin that it arrived, take it, and act on it.\n"]
    for item in items:
        parts.append(f'Handoff #{item["id"]}: "{item["title"]}" ({lanes(item)})')
        parts.append(f'Before you act on it, take it: {command} handoff show {item["id"]} --take '
                     f'--by YOUR_LANE\nIf the answer says that another session has it, stop.')
        parts.append("--- text ---\n" + item["text"]
                     + ("\n[cut: the command above prints the whole text]" if item.get("cut") else "")
                     + "\n--- end ---")
        parts.append(f'When it is dealt with: {command} handoff done {item["id"]} "RESULT IN ONE SENTENCE"\n')
    return "\n".join(parts)


def send(cfg: Config, folder: str, title: str, text: str, to_lane: str = "",
         from_lane: str = "") -> tuple[bool, str]:
    body = {"title": title, "text": text, "to_lane": to_lane, "from_lane": from_lane,
            **where(folder)}
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=30).request(
            "POST", "/api/v1/handoffs", body)
    except HubError as exc:
        return False, f"The handoff was not filed: {reason(exc)}"
    target = f' for the lane "{to_lane}"' if to_lane else ""
    return True, (f"Handoff {answer['id']} is filed in the project {answer['project']}{target}. "
                  "A Claude Code session there receives it with its next prompt.")


def listing(cfg: Config, folder: str, status: str = "open") -> tuple[bool, str]:
    query = urlencode({"status": status, **where(folder)})
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=30).request(
            "GET", f"/api/v1/handoffs?{query}")
    except HubError as exc:
        return False, reason(exc)
    lines = [f'#{item["id"]} [{item["status"]}] "{item["title"]}" ({lanes(item)}), '
             f'{item["size"]} characters' + (f', taken by {item["taken_by"]}' if item["taken_by"] else "")
             for item in answer["handoffs"]]
    if lines:
        return True, "\n".join(lines)
    kind = "" if status == "all" else f" with the status {status}"
    return True, f"No handoffs{kind} in the project {answer['project']}."


def show(cfg: Config, handoff_id: int, take: bool = False, by: str = "") -> tuple[bool, str]:
    query = urlencode({"take": int(take), "by": by})
    try:
        _, item = Client(cfg.server_url, cfg.token, timeout=30).request(
            "GET", f"/api/v1/handoffs/{int(handoff_id)}?{query}")
    except HubError as exc:
        return False, reason(exc)
    head = f'Handoff #{item["id"]}: "{item["title"]}" ({lanes(item)}), status {item["status"]}'
    mine = bool(by) and item["status"] == "taken" and item["taken_by"] == " ".join(by.lower().split())
    if take and mine and not item["taken_now"]:
        head += "\nYour lane has it already."
    elif take and not item["taken_now"]:
        who = item["taken_by"] or "another session"
        head += (f"\nYou did not get it: it was already {item['status']}"
                 + (f" by {who}." if item["status"] == "taken" else ".")
                 + " Do not work on it unless Robin tells you to.")
    elif take:
        head += "\nIt is yours now."
    return True, f"{head}\n\n{item['text']}" + (f"\n\nResult: {item['result']}" if item["result"] else "")


def done(cfg: Config, handoff_id: int, result: str = "") -> tuple[bool, str]:
    try:
        Client(cfg.server_url, cfg.token, timeout=30).request(
            "POST", f"/api/v1/handoffs/{int(handoff_id)}/done", {"result": result})
    except HubError as exc:
        return False, reason(exc)
    return True, f"Handoff {handoff_id} is marked as done."
