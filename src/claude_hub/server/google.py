"""Access to Google Calendar and Gmail for several Google accounts.

The hub keeps one sign-in per account and talks to the Google APIs itself,
so that every Claude session, on every machine, reads and writes through
one place. The sign-ins live in one file in the data folder that only the
service user can read. They never appear in a page or in an API answer.

Standard library only. The transport is a parameter, so tests run without
the network.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Calendar: read and write events. Gmail: read, and optionally send.
# Sending is used only by routines that the person set up in the hub, and only
# for an account where the person ticked the box for it. No session can send mail.
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# Without these, an account is not connected.
REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]
# What the sign-in asks for. The person can leave the box for sending unticked.
SCOPES = [*REQUIRED_SCOPES, SEND_SCOPE]

STORE_FILE = "google.json"
LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,29}")
# One plain address. A routine sends to exactly one recipient.
ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DEFAULT_TIME_ZONE = "Europe/Berlin"
# How much of a mail or of an event description an answer carries.
BODY_LIMIT = 20000
DESCRIPTION_LIMIT = 500
# Events are read in pages. A span with more events than this is cut, and the
# answer says so.
PAGE_SIZE = 250
MAX_PAGES = 8
# The marker on events that the hub created, so that they can be told apart.
MARKER = "claudeHub"

# method, url, headers, body -> status, parsed JSON
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]


class GoogleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def urllib_transport(method: str, url: str, headers: dict[str, str],
                     body: bytes | None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, {"error": {"message": raw.decode("utf-8", "replace")[:300]}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GoogleError(0, f"Google is not reachable: {exc}") from exc


def valid_label(label: str) -> bool:
    return bool(LABEL.fullmatch(label or ""))


def _problem(payload: Any) -> str:
    """The reason in an error answer of Google, in one line."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or error)
        if error:
            return f"{error}: {payload.get('error_description', '')}".strip(": ")
    return "no detail"


class Store:
    """The sign-ins, in one file that only the service user can read."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / STORE_FILE
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        return accounts if isinstance(accounts, dict) else {}

    def _write(self, accounts: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        # Create the file with its final mode, so that the secrets are never
        # readable by others, not even for a moment.
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"accounts": accounts}, stream, indent=2)
        os.replace(temp, self.path)

    def get(self, label: str) -> dict[str, Any] | None:
        return self._read().get(label)

    def labels(self) -> list[str]:
        return sorted(self._read())

    def put(self, label: str, entry: dict[str, Any]) -> None:
        with self._lock:
            accounts = self._read()
            accounts[label] = entry
            self._write(accounts)

    def remove(self, label: str) -> bool:
        with self._lock:
            accounts = self._read()
            if label not in accounts:
                return False
            del accounts[label]
            self._write(accounts)
            return True

    def note(self, label: str, error: str | None) -> None:
        """Record whether the last call worked. Written only when it changes."""
        with self._lock:
            accounts = self._read()
            entry = accounts.get(label)
            if entry is None or entry.get("last_error") == error:
                return
            entry["last_error"] = error
            self._write(accounts)

    def public(self) -> list[dict[str, Any]]:
        """What pages and API answers may show: no secret."""
        return [{"label": label, "email": entry.get("email"), "scopes": entry.get("scopes") or [],
                 "can_send": SEND_SCOPE in (entry.get("scopes") or []),
                 "added_at": entry.get("added_at"), "last_error": entry.get("last_error")}
                for label, entry in sorted(self._read().items())]


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise GoogleError(400, f"Not a day: {value!r}. Use 2026-11-03.") from exc


def event_time(value: str, time_zone: str) -> dict[str, str]:
    """A start or end in the shape of the Calendar API.

    ``2026-11-03`` is a whole day. ``2026-11-03T14:30`` is a local time in
    ``time_zone``. A value with an offset or ``Z`` is taken as it is.
    """
    value = (value or "").strip()
    if DATE.fullmatch(value):
        return {"date": _day(value).isoformat()}
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleError(400, f"Not a date or time: {value!r}. Use 2026-11-03 or "
                               "2026-11-03T14:30.") from exc
    if parsed.tzinfo is not None:
        return {"dateTime": parsed.isoformat()}
    return {"dateTime": parsed.isoformat(timespec="seconds"), "timeZone": time_zone}


def zone(time_zone: str):
    """The time zone by name. UTC when the system has no time zone data."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(time_zone)
    except Exception:  # noqa: BLE001 - a missing database must not break the calendar
        return timezone.utc


def local_today(time_zone: str) -> date:
    return datetime.now(zone(time_zone)).date()


def range_bound(value: str, time_zone: str, end: bool = False) -> str:
    """One end of a time range, as the Calendar API wants it.

    A day counts from its first moment. As the end of a range, a day is
    included as a whole.
    """
    value = (value or "").strip()
    try:
        if DATE.fullmatch(value):
            day = date.fromisoformat(value) + timedelta(days=1 if end else 0)
            parsed = datetime(day.year, day.month, day.day)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleError(400, f"Not a date or time: {value!r}. Use 2026-11-03 or "
                               "2026-11-03T14:30.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone(time_zone))
    return parsed.isoformat(timespec="seconds")


def event_body(fields: dict[str, Any], time_zone: str) -> dict[str, Any]:
    """The request body for an event. Only the given fields appear."""
    body: dict[str, Any] = {}
    for name in ("summary", "description", "location"):
        if fields.get(name) is not None:
            body[name] = str(fields[name])
    start, end = fields.get("start"), fields.get("end")
    if start:
        body["start"] = event_time(start, time_zone)
        whole_day = "date" in body["start"]
        if whole_day:
            # The API wants the day after the last day. People name the last day.
            if end and not DATE.fullmatch(end.strip()):
                raise GoogleError(400, "Start and end must both be days or both be times.")
            last = max(_day(end), _day(body["start"]["date"])) if end else _day(body["start"]["date"])
            body["end"] = {"date": (last + timedelta(days=1)).isoformat()}
        elif end:
            body["end"] = event_time(end, time_zone)
        else:
            raise GoogleError(400, "An event with a time needs an end.")
        if "date" in body["end"] and not whole_day:
            raise GoogleError(400, "Start and end must both be days or both be times.")
    elif end:
        raise GoogleError(400, "Give the start together with the end.")
    if fields.get("private") is not None:
        body["visibility"] = "private" if fields["private"] else "default"
    return body


def brief_event(item: dict[str, Any]) -> dict[str, Any]:
    start, end = item.get("start") or {}, item.get("end") or {}
    whole_day = "date" in start
    if whole_day and end.get("date"):
        # Turn the exclusive end of the API back into the last day.
        last = (date.fromisoformat(end["date"]) - timedelta(days=1)).isoformat()
    else:
        last = end.get("dateTime")
    description = item.get("description") or ""
    return {
        "id": item.get("id"),
        "summary": item.get("summary") or "(no title)",
        "start": start.get("date") or start.get("dateTime"),
        "end": last,
        "all_day": whole_day,
        "location": item.get("location") or "",
        "description": description[:DESCRIPTION_LIMIT],
        "status": item.get("status"),
        "from_mail": item.get("eventType") == "fromGmail",
        "by_hub": MARKER in ((item.get("extendedProperties") or {}).get("private") or {}),
        "link": item.get("htmlLink"),
    }


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "replace")


def _plain(markup: str) -> str:
    """Readable text from an HTML mail, without a parser dependency."""
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mail_text(payload: dict[str, Any]) -> str:
    """The body of a mail: the plain part when there is one, otherwise the HTML part as text."""
    found: dict[str, str] = {}

    def walk(part: dict[str, Any]) -> None:
        kind = part.get("mimeType") or ""
        data = (part.get("body") or {}).get("data")
        if data and kind in ("text/plain", "text/html") and kind not in found:
            found[kind] = _decode(data)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    if found.get("text/plain", "").strip():
        return found["text/plain"].strip()
    return _plain(found.get("text/html", ""))


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (payload or {}).get("headers") or []}


class Google:
    def __init__(self, store: Store, http: Transport = urllib_transport,
                 time_zone: str = DEFAULT_TIME_ZONE):
        self.store = store
        self.http = http
        self.time_zone = time_zone
        self._tokens: dict[str, tuple[str, str, float]] = {}
        self._agendas: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # --- sign-in ------------------------------------------------------------

    def _entry(self, label: str) -> dict[str, Any]:
        entry = self.store.get(label)
        if entry is None:
            known = ", ".join(self.store.labels()) or "none"
            raise GoogleError(404, f"No Google account '{label}'. Connected: {known}.")
        return entry

    def access_token(self, label: str, entry: dict[str, Any] | None = None,
                     fresh: bool = False) -> str:
        entry = entry or self._entry(label)
        with self._lock:
            cached = self._tokens.get(label)
            if cached and not fresh and cached[0] == entry["refresh_token"] and cached[2] > time.time():
                return cached[1]
        form = urlencode({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                          "client_id": entry["client_id"],
                          "client_secret": entry["client_secret"]}).encode()
        status, payload = self.http("POST", TOKEN_URL,
                                    {"Content-Type": "application/x-www-form-urlencoded"}, form)
        if status != 200 or not isinstance(payload, dict) or not payload.get("access_token"):
            raise GoogleError(401, f"Google refused the sign-in of '{label}' ({_problem(payload)}). "
                                   f"Connect the account again: hub google-add {label}")
        token = str(payload["access_token"])
        with self._lock:
            self._tokens[label] = (entry["refresh_token"], token,
                                   time.time() + int(payload.get("expires_in", 3600)) - 60)
        return token

    def call(self, label: str, method: str, url: str, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None, entry: dict[str, Any] | None = None) -> Any:
        if params:
            pairs = [(key, value) for key, values in params.items() if values is not None
                     for value in (values if isinstance(values, (list, tuple)) else [values])]
            url = f"{url}?{urlencode(pairs)}"
        data = json.dumps(body).encode() if body is not None else None
        stored = entry is None
        try:
            for attempt in (0, 1):
                headers = {"Authorization": f"Bearer {self.access_token(label, entry, fresh=bool(attempt))}"}
                if data is not None:
                    headers["Content-Type"] = "application/json"
                status, payload = self.http(method, url, headers, data)
                # An access token can end early. One fresh token is worth a try.
                if status != 401 or attempt:
                    break
            if status >= 400:
                raise GoogleError(status, f"Google answered {status} for '{label}': {_problem(payload)}")
        except GoogleError as exc:
            # Only a refused sign-in marks the account. A wrong calendar name does not.
            if stored and exc.status == 401:
                self.store.note(label, str(exc))
            raise
        if stored:
            self.store.note(label, None)
        return payload

    def connect(self, label: str, client_id: str, client_secret: str, refresh_token: str,
                scopes: list[str], added_at: str) -> dict[str, Any]:
        """Check a new sign-in against Google, then keep it."""
        entry = {"client_id": client_id, "client_secret": client_secret,
                 "refresh_token": refresh_token, "scopes": scopes, "added_at": added_at,
                 "last_error": None}
        # Ask both services once. A service that is not enabled in the Google
        # project fails here, with the reason, and not later in a session.
        self.call(label, "GET", f"{CALENDAR_API}/calendars/primary", entry=entry)
        profile = self.call(label, "GET", f"{GMAIL_API}/profile", entry=entry)
        entry["email"] = (profile or {}).get("emailAddress")
        self.store.put(label, entry)
        return {"label": label, "email": entry["email"], "scopes": scopes}

    # --- calendar -----------------------------------------------------------

    def calendars(self, label: str) -> list[dict[str, Any]]:
        payload = self.call(label, "GET", f"{CALENDAR_API}/users/me/calendarList",
                            {"maxResults": 250})
        return [{"id": item.get("id"), "name": item.get("summaryOverride") or item.get("summary"),
                 "primary": bool(item.get("primary")),
                 "can_write": item.get("accessRole") in ("owner", "writer"),
                 "shown": bool(item.get("selected"))}
                for item in (payload or {}).get("items") or []]

    def events(self, label: str, calendar: str, start: str, end: str,
               query: str = "") -> tuple[list[dict[str, Any]], bool]:
        """The events of one calendar, and whether there are more than were read."""
        url = f"{CALENDAR_API}/calendars/{quote(calendar, safe='')}/events"
        params: dict[str, Any] = {
            "timeMin": range_bound(start, self.time_zone),
            "timeMax": range_bound(end, self.time_zone, end=True),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": PAGE_SIZE,
            "q": query or None}
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            payload = self.call(label, "GET", url, params) or {}
            items += payload.get("items") or []
            params["pageToken"] = payload.get("nextPageToken")
            if not params["pageToken"]:
                break
        events = [brief_event(item) for item in items if item.get("status") != "cancelled"]
        return events, bool(params["pageToken"])

    def agenda(self, start: str, end: str, query: str = "", account: str = "",
               calendar: str = "") -> dict[str, Any]:
        """Events of every connected account and calendar, in order of time.

        One account that fails does not hide the others. Its problem is
        returned beside the events.
        """
        # A span that cannot be read is a mistake of the caller, not of an account.
        if datetime.fromisoformat(range_bound(start, self.time_zone)) >= \
                datetime.fromisoformat(range_bound(end, self.time_zone, end=True)):
            raise GoogleError(400, "The end of the span lies before its start.")
        events: list[dict[str, Any]] = []
        problems: list[str] = []
        for label in ([account] if account else self.store.labels()):
            try:
                email = self._entry(label).get("email")
                wanted = [c for c in self.calendars(label)
                          if (c["id"] == calendar or c["name"] == calendar) or (not calendar and c["shown"])]
                if calendar and not wanted and account:
                    raise GoogleError(404, f"No calendar '{calendar}' in account '{label}'.")
                for cal in wanted:
                    found, more = self.events(label, cal["id"], start, end, query)
                    events += [{**event, "account": label, "account_email": email,
                                "calendar": cal["name"], "calendar_id": cal["id"]} for event in found]
                    if more:
                        # Say so, instead of passing a part off as the whole.
                        problems.append(f"'{cal['name']}' of '{label}' has more events in this span than "
                                        f"the first {len(found)}. Narrow the span or add a search text.")
            except GoogleError as exc:
                if account:
                    raise
                problems.append(str(exc))
        events.sort(key=lambda item: (str(item["start"])[:10], not item["all_day"], str(item["start"])))
        return {"events": events, "problems": problems}

    def overview(self, start: str, end: str, keep_seconds: int = 120) -> dict[str, Any]:
        """The agenda for a page. Kept for a short time, because a page reloads often."""
        key = (start, end)
        kept = self._agendas.get(key)
        if kept and kept[0] > time.time():
            return kept[1]
        result = self.agenda(start, end)
        self._agendas = {key: (time.time() + keep_seconds, result)}
        return result

    def find_calendar(self, label: str, calendar: str) -> dict[str, Any]:
        """A calendar by its ID or its name. ``primary`` and an empty value mean the main calendar."""
        calendars = self.calendars(label)
        wanted = (calendar or "primary").strip()
        for item in calendars:
            if item["id"] == wanted or (wanted == "primary" and item["primary"]):
                return item
        named = [item for item in calendars if (item["name"] or "").casefold() == wanted.casefold()]
        if len(named) == 1:
            return named[0]
        names = ", ".join(sorted(str(item["name"]) for item in calendars))
        raise GoogleError(404, f"No calendar '{calendar}' in account '{label}'. It has: {names}.")

    def create_event(self, label: str, calendar: str, fields: dict[str, Any],
                     source: str = "") -> dict[str, Any]:
        target = self.find_calendar(label, calendar)
        if not target["can_write"]:
            raise GoogleError(403, f"The account '{label}' cannot write to '{target['name']}'.")
        body = event_body(fields, self.time_zone)
        if not body.get("summary") or "start" not in body:
            raise GoogleError(400, "An event needs a summary and a start.")
        body["extendedProperties"] = {"private": {MARKER: (source or "hub")[:100]}}
        created = self.call(label, "POST",
                            f"{CALENDAR_API}/calendars/{quote(target['id'], safe='')}/events", body=body)
        self._agendas = {}
        return self._written(label, target, created)

    def update_event(self, label: str, calendar: str, event_id: str,
                     fields: dict[str, Any]) -> dict[str, Any]:
        target = self.find_calendar(label, calendar)
        body = event_body(fields, self.time_zone)
        if not body:
            raise GoogleError(400, "Nothing to change.")
        # A change merges into the stored event. When a timed event becomes a
        # whole-day one, or the reverse, the old kind of time must be cleared.
        for edge in ("start", "end"):
            if edge in body:
                body[edge] = {"date": None, "dateTime": None, "timeZone": None, **body[edge]}
        changed = self.call(
            label, "PATCH",
            f"{CALENDAR_API}/calendars/{quote(target['id'], safe='')}/events/{quote(event_id, safe='')}",
            body=body)
        self._agendas = {}
        return self._written(label, target, changed)

    def _written(self, label: str, calendar: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        return {**brief_event(item or {}), "account": label,
                "account_email": self._entry(label).get("email"),
                "calendar": calendar["name"], "calendar_id": calendar["id"]}

    # --- mail (read only) ---------------------------------------------------

    def mail_search(self, label: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 25))
        found = self.call(label, "GET", f"{GMAIL_API}/messages", {"q": query, "maxResults": limit})
        result = []
        for ref in (found or {}).get("messages") or []:
            message = self.call(label, "GET", f"{GMAIL_API}/messages/{quote(ref['id'], safe='')}", {
                "format": "metadata", "metadataHeaders": ["Subject", "From", "To", "Date"]})
            result.append(self._mail_head(message))
        return result

    def mail_read(self, label: str, message_id: str) -> dict[str, Any]:
        message = self.call(label, "GET", f"{GMAIL_API}/messages/{quote(message_id, safe='')}",
                            {"format": "full"})
        text = mail_text((message or {}).get("payload") or {})
        return {**self._mail_head(message), "text": text[:BODY_LIMIT],
                "truncated": len(text) > BODY_LIMIT}

    def send_mail(self, label: str, to: str, subject: str, text: str) -> dict[str, Any]:
        """Send a plain-text mail from the account, to one recipient. Only routines call this."""
        entry = self._entry(label)
        if SEND_SCOPE not in (entry.get("scopes") or []):
            raise GoogleError(403, f"The account '{label}' may not send mail. Connect it again and "
                                   f"tick the box for sending: hub google-add {label}")
        to, subject = (to or "").strip(), " ".join((subject or "").split())
        if not ADDRESS.fullmatch(to):
            raise GoogleError(400, f"Not one mail address: {to!r}")
        if not subject or not (text or "").strip():
            raise GoogleError(400, "A mail needs a subject and a text.")
        message = EmailMessage()
        message["To"] = to
        if entry.get("email"):
            message["From"] = entry["email"]
        message["Subject"] = subject
        message.set_content(text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.call(label, "POST", f"{GMAIL_API}/messages/send", body={"raw": raw})
        return {"id": (sent or {}).get("id"), "thread": (sent or {}).get("threadId"),
                "account": label, "from": entry.get("email"), "to": to, "subject": subject}

    @staticmethod
    def _mail_head(message: dict[str, Any]) -> dict[str, Any]:
        headers = _headers((message or {}).get("payload") or {})
        return {"id": message.get("id"), "thread": message.get("threadId"),
                "subject": headers.get("subject", ""), "from": headers.get("from", ""),
                "to": headers.get("to", ""), "date": headers.get("date", ""),
                "snippet": html.unescape(message.get("snippet") or "")}
