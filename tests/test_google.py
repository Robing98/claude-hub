from __future__ import annotations

import base64
import io
import json
import stat
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_hub.collector import cli, google_auth, google_tools, mcp_server
from claude_hub.collector.config import Account, Config
from claude_hub.server import db
from claude_hub.server.google import (
    SCOPES,
    Google,
    GoogleError,
    Store,
    event_body,
    mail_text,
    range_bound,
)

ZONE = "Europe/Berlin"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGoogle:
    """Answers like the Google APIs, for two accounts, and records every call."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refreshes = 0
        self.expire_once = False
        self.gmail_off = False
        self.page_size = 0
        self.refuse: set[str] = set()
        self.created: list[dict] = []
        self.emails = {"refresh-private": "robin@example.com", "refresh-work": "robin@work.example"}
        self.calendars = {
            "robin@example.com": [
                {"id": "robin@example.com", "summary": "Robin Golle", "primary": True,
                 "accessRole": "owner", "selected": True},
                {"id": "aerzte@group", "summary": "Ärzte", "accessRole": "owner", "selected": True},
                {"id": "holidays@group", "summary": "Feiertage", "accessRole": "reader"},
            ],
            "robin@work.example": [
                {"id": "robin@work.example", "summary": "Work", "primary": True,
                 "accessRole": "owner", "selected": True},
            ],
        }
        self.events = {
            "robin@example.com": [
                {"id": "e1", "summary": "Ihr Zahnarzttermin", "eventType": "fromGmail",
                 "start": {"dateTime": "2026-10-06T09:00:00+02:00"},
                 "end": {"dateTime": "2026-10-06T09:30:00+02:00"}, "htmlLink": "https://cal/e1"},
            ],
            "aerzte@group": [
                {"id": "e2", "summary": "Checkup", "start": {"date": "2026-10-05"},
                 "end": {"date": "2026-10-06"}},
                {"id": "gone", "summary": "Old", "status": "cancelled",
                 "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}},
            ],
            "robin@work.example": [
                {"id": "e3", "summary": "Standup",
                 "start": {"dateTime": "2026-10-05T10:00:00+02:00"},
                 "end": {"dateTime": "2026-10-05T10:15:00+02:00"}},
            ],
        }
        self.mails = {"m1": {
            "id": "m1", "threadId": "t1", "snippet": "Ihr Termin am 6. Oktober &amp; mehr",
            "payload": {"mimeType": "multipart/alternative", "headers": [
                {"name": "Subject", "value": "Terminbestätigung"},
                {"name": "From", "value": "Praxis <praxis@example.com>"},
                {"name": "To", "value": "robin@example.com"},
                {"name": "Date", "value": "Mon, 28 Sep 2026 10:00:00 +0200"}],
                "parts": [{"mimeType": "text/html", "body": {
                    "data": b64("<style>p{}</style><p>Ihr Termin:</p><p>6. Oktober, 9 Uhr</p>")}}]}}}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        query = {key: values for key, values in parse_qs(parts.query).items()}
        if parts.netloc == "oauth2.googleapis.com":
            form = parse_qs(body.decode())
            token = form["refresh_token"][0]
            if token not in self.emails or token in self.refuse:
                return 400, {"error": "invalid_grant", "error_description": "Token has been revoked."}
            self.refreshes += 1
            return 200, {"access_token": f"access:{token}:{self.refreshes}", "expires_in": 3600}
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        _, token, _ = bearer.split(":")
        email = self.emails[token]
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload))
        if self.expire_once:
            self.expire_once = False
            return 401, {"error": {"message": "Invalid Credentials"}}
        path = urllib.request.unquote(parts.path)
        if path.endswith("/calendars/primary"):
            return 200, {"id": email}
        if path.endswith("/users/me/profile"):
            if self.gmail_off:
                return 403, {"error": {"message": "Gmail API has not been used in project 1 before or it is disabled."}}
            return 200, {"emailAddress": email}
        if path.endswith("/users/me/calendarList"):
            return 200, {"items": self.calendars[email]}
        if "/calendar/v3/calendars/" in path and path.endswith("/events"):
            calendar = path.split("/calendars/")[1].removesuffix("/events")
            if method == "POST":
                made = {"id": f"new{len(self.created) + 1}", "htmlLink": "https://cal/new", **payload}
                self.created.append({"calendar": calendar, **made})
                return 200, made
            wanted = (query.get("q") or [""])[0].lower()
            items = [item for item in self.events.get(calendar, []) if wanted in item["summary"].lower()]
            if self.page_size:
                # Hand the events out in pages, and never run out, like a very full calendar.
                at = int((query.get("pageToken") or ["0"])[0])
                return 200, {"items": (items * 99)[at:at + self.page_size],
                             "nextPageToken": str(at + self.page_size)}
            return 200, {"items": items}
        if "/calendar/v3/calendars/" in path and "/events/" in path:
            if path.rsplit("/", 1)[1] == "missing":
                return 404, {"error": {"message": "Not Found"}}
            return 200, {"id": path.rsplit("/", 1)[1], "summary": "Changed", **payload}
        if path.endswith("/users/me/messages"):
            return 200, {"messages": [{"id": "m1"}] if "zahn" in query["q"][0].lower() else []}
        if "/users/me/messages/" in path:
            return 200, self.mails[path.rsplit("/", 1)[1]]
        return 404, {"error": {"message": f"no route {path}"}}


@pytest.fixture
def fake(app) -> FakeGoogle:
    fake = FakeGoogle()
    app.state.google.http = fake
    return fake


def connect(client, label: str = "private", scopes: list[str] | None = None):
    return client.put(f"/api/v1/google/accounts/{label}", json={
        "client_id": "cid", "client_secret": "csecret", "refresh_token": f"refresh-{label}",
        "scopes": SCOPES if scopes is None else scopes})


@pytest.fixture
def two_accounts(client, fake):
    assert connect(client, "private").json()["email"] == "robin@example.com"
    assert connect(client, "work").json()["email"] == "robin@work.example"
    return fake


# --- time and event shapes ---------------------------------------------------


def test_event_body_days_and_times():
    assert event_body({"summary": "Checkup", "start": "2026-11-03"}, ZONE) == {
        "summary": "Checkup", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}
    # For a whole-day event, people name the last day. The API wants the day after.
    assert event_body({"start": "2026-11-03", "end": "2026-11-05"}, ZONE)["end"] == {"date": "2026-11-06"}
    timed = event_body({"start": "2026-11-03T14:30", "end": "2026-11-03T15:00", "private": True}, ZONE)
    assert timed["start"] == {"dateTime": "2026-11-03T14:30:00", "timeZone": ZONE}
    assert timed["visibility"] == "private"
    assert event_body({"start": "2026-11-03T14:30:00+01:00", "end": "2026-11-03T15:00:00Z"}, ZONE)["end"] == {
        "dateTime": "2026-11-03T15:00:00+00:00"}
    assert event_body({"description": "x"}, ZONE) == {"description": "x"}


@pytest.mark.parametrize("fields", [
    {"start": "tomorrow"}, {"start": "2026-13-40"}, {"start": "2026-11-03T14:30"},
    {"end": "2026-11-03"}, {"start": "2026-11-03T14:30", "end": "2026-11-04"},
])
def test_event_body_refuses_unclear_times(fields):
    with pytest.raises(GoogleError) as caught:
        event_body(fields, ZONE)
    assert caught.value.status == 400


def test_range_bound_includes_the_last_day():
    assert range_bound("2026-10-05", ZONE) == "2026-10-05T00:00:00+02:00"
    assert range_bound("2026-10-05", ZONE, end=True) == "2026-10-06T00:00:00+02:00"
    assert range_bound("2026-12-01T08:00", ZONE) == "2026-12-01T08:00:00+01:00"
    with pytest.raises(GoogleError):
        range_bound("next week", ZONE)


def test_mail_text_prefers_plain_and_falls_back_to_html():
    both = {"parts": [{"mimeType": "text/plain", "body": {"data": b64("Plain text")}},
                      {"mimeType": "text/html", "body": {"data": b64("<b>HTML</b>")}}]}
    assert mail_text(both) == "Plain text"
    only_html = {"mimeType": "text/html", "body": {"data": b64(
        "<head><title>t</title></head><p>Line&nbsp;one</p><script>x()</script><div>Line two</div>")}}
    assert mail_text(only_html) == "Line one\n\nLine two" or mail_text(only_html) == "Line one\nLine two"


# --- store ---------------------------------------------------------------------


def test_store_keeps_secrets_private(tmp_path: Path):
    store = Store(tmp_path)
    store.put("private", {"email": "a@b", "client_secret": "s", "refresh_token": "r", "scopes": []})
    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    public = json.dumps(store.public())
    assert "a@b" in public and "refresh_token" not in public and "client_secret" not in public
    assert store.remove("private") and not store.remove("private") and store.labels() == []


# --- sign-in ---------------------------------------------------------------------


def test_connect_checks_the_sign_in_and_hides_secrets(client, fake, data_dir: Path):
    assert connect(client).status_code == 200
    listed = client.get("/api/v1/google/accounts").json()
    assert listed["accounts"][0]["email"] == "robin@example.com" and listed["scopes"] == SCOPES
    assert "refresh" not in json.dumps(listed) and "csecret" not in json.dumps(listed)
    page = client.get("/settings/google").text
    assert "robin@example.com" in page and "refresh-private" not in page and "csecret" not in page
    assert "account connected" in page
    assert "refresh-private" in (data_dir / "google.json").read_text()


def test_connect_names_a_service_that_is_not_enabled(client, fake):
    fake.gmail_off = True
    refused = connect(client)
    assert refused.status_code == 403 and "Gmail API has not been used" in refused.json()["detail"]
    assert client.get("/api/v1/google/accounts").json()["accounts"] == []


def test_a_full_calendar_is_read_in_pages_and_a_cut_is_named(client, two_accounts):
    two_accounts.page_size = 1
    answer = client.get("/api/v1/google/events", params={
        "start": "2026-10-05", "end": "2026-10-06", "account": "work"}).json()
    assert len(answer["events"]) == 8
    assert "has more events in this span than the first 8" in answer["problems"][0]


def test_connect_refuses_bad_input(client, fake):
    assert connect(client, "Private!").status_code == 400
    missing = connect(client, scopes=SCOPES[:1])
    assert missing.status_code == 400 and "gmail.readonly" in missing.json()["detail"]
    fake.refuse.add("refresh-private")
    refused = connect(client)
    assert refused.status_code == 401 and "hub google-add private" in refused.json()["detail"]
    assert client.get("/api/v1/google/accounts").json()["accounts"] == []


def test_api_needs_a_machine_token(app, fake):
    from fastapi.testclient import TestClient
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/google/accounts").status_code == 401
        assert anonymous.get("/api/v1/google/events", params={"start": "2026-10-05", "end": "2026-10-06"}).status_code == 401
        assert anonymous.post("/api/v1/google/events", json={"account": "private"}).status_code == 401
        assert anonymous.get("/api/v1/google/mail", params={"q": "x"}).status_code == 401


def test_access_token_is_reused_and_renewed(app, two_accounts):
    google: Google = app.state.google
    before = two_accounts.refreshes
    google.calendars("private")
    google.calendars("private")
    assert two_accounts.refreshes == before
    # Google can end an access token early. One fresh token is tried.
    two_accounts.expire_once = True
    assert len(google.calendars("private")) == 3 and two_accounts.refreshes == before + 1


def test_a_refused_sign_in_shows_on_the_settings_page(client, app, two_accounts):
    app.state.google._tokens.clear()
    two_accounts.refuse.add("refresh-work")
    answer = client.get("/api/v1/google/events", params={"start": "2026-10-05", "end": "2026-10-06"}).json()
    # The other account still answers.
    assert [e["summary"] for e in answer["events"]] == ["Checkup", "Ihr Zahnarzttermin"]
    assert "hub google-add work" in answer["problems"][0]
    assert "needs attention" in client.get("/settings/google").text
    two_accounts.refuse.clear()
    client.get("/api/v1/google/calendars")
    assert "needs attention" not in client.get("/settings/google").text


# --- calendar ---------------------------------------------------------------------


def test_agenda_merges_accounts_in_order(client, two_accounts):
    answer = client.get("/api/v1/google/events", params={"start": "2026-10-05", "end": "2026-10-06"}).json()
    assert [(e["summary"], e["account"], e["calendar"]) for e in answer["events"]] == [
        ("Checkup", "private", "Ärzte"), ("Standup", "work", "Work"),
        ("Ihr Zahnarzttermin", "private", "Robin Golle")]
    checkup, _, dentist = answer["events"]
    assert checkup["all_day"] and checkup["end"] == "2026-10-05"
    assert dentist["from_mail"] and dentist["account_email"] == "robin@example.com"
    # The calendar that is not ticked in Google is left out, and so are cancelled events.
    listed = [call for call in two_accounts.calls if "holidays" in call[1]]
    assert not listed and "Old" not in json.dumps(answer)
    sent = [call[1] for call in two_accounts.calls if call[1].count("/events?")][-1]
    assert "timeMin=2026-10-05T00%3A00%3A00%2B02%3A00" in sent and "timeMax=2026-10-07" in sent


def test_agenda_filters(client, two_accounts):
    params = {"start": "2026-01-01", "end": "2026-12-31"}
    found = client.get("/api/v1/google/events", params={**params, "q": "zahnarzt"}).json()["events"]
    assert [e["id"] for e in found] == ["e1"]
    doctors = client.get("/api/v1/google/events", params={**params, "account": "private", "calendar": "Ärzte"})
    assert [e["id"] for e in doctors.json()["events"]] == ["e2"]
    assert client.get("/api/v1/google/events", params={**params, "account": "nobody"}).status_code == 404
    unknown = client.get("/api/v1/google/events", params={**params, "account": "work", "calendar": "Ärzte"})
    assert unknown.status_code == 404
    assert client.get("/api/v1/google/events", params={"start": "soon", "end": "later"}).status_code == 400


def test_calendars_endpoint(client, two_accounts):
    calendars = client.get("/api/v1/google/calendars").json()["calendars"]
    assert [(c["account"], c["name"], c["can_write"]) for c in calendars] == [
        ("private", "Robin Golle", True), ("private", "Ärzte", True), ("private", "Feiertage", False),
        ("work", "Work", True)]
    assert len(client.get("/api/v1/google/calendars", params={"account": "work"}).json()["calendars"]) == 1


def test_create_event_by_calendar_name(client, two_accounts, data_dir: Path):
    made = client.post("/api/v1/google/events", json={
        "account": "private", "calendar": "ärzte", "summary": "Zahnarzt", "start": "2027-04-06T09:00",
        "end": "2027-04-06T09:30", "location": "Hanau", "source": "test lane"})
    assert made.status_code == 201
    event = made.json()
    assert event["calendar"] == "Ärzte" and event["account_email"] == "robin@example.com"
    assert event["by_hub"] and event["link"] == "https://cal/new"
    sent = two_accounts.created[0]
    assert sent["calendar"] == "aerzte@group" and sent["location"] == "Hanau"
    assert sent["start"] == {"dateTime": "2027-04-06T09:00:00", "timeZone": ZONE}
    assert sent["extendedProperties"] == {"private": {"claudeHub": "test lane"}}
    log = db.connect(data_dir).execute("SELECT * FROM google_log WHERE action = 'event created'").fetchone()
    assert log["account"] == "private" and "Zahnarzt" in log["detail"] and "test lane" in log["detail"]


def test_create_event_refusals(client, two_accounts):
    base = {"account": "private", "summary": "X", "start": "2027-04-06"}
    assert client.post("/api/v1/google/events", json={**base, "calendar": "Feiertage"}).status_code == 403
    unknown = client.post("/api/v1/google/events", json={**base, "calendar": "Nope"})
    assert unknown.status_code == 404 and "Ärzte" in unknown.json()["detail"]
    assert client.post("/api/v1/google/events", json={"account": "private", "start": "2027-04-06"}).status_code == 400
    assert client.post("/api/v1/google/events", json={**base, "account": "nobody"}).status_code == 404
    assert two_accounts.created == []


def test_update_event(client, two_accounts):
    changed = client.patch("/api/v1/google/events", json={
        "account": "private", "calendar": "Ärzte", "event_id": "e2", "start": "2026-10-12"})
    assert changed.status_code == 200
    method, url, payload = two_accounts.calls[-1]
    assert method == "PATCH" and url.endswith("/calendars/aerzte%40group/events/e2")
    # The other kind of time is cleared, so that a timed event can become a whole-day one.
    assert payload == {"start": {"date": "2026-10-12", "dateTime": None, "timeZone": None},
                       "end": {"date": "2026-10-13", "dateTime": None, "timeZone": None}}
    assert client.patch("/api/v1/google/events", json={
        "account": "private", "event_id": "e2"}).status_code == 400
    missing = client.patch("/api/v1/google/events", json={
        "account": "private", "event_id": "missing", "summary": "x"})
    assert missing.status_code == 404
    # A wrong event does not mark the account as broken.
    assert client.get("/api/v1/google/accounts").json()["accounts"][0]["last_error"] is None


# --- proposals ---------------------------------------------------------------------


PROPOSAL = {"account": "private", "calendar": "Ärzte", "summary": "Zahnarzt Kontrolle",
            "start": "2027-04-06", "reason": "Six months after the last visit", "source": "weekly check"}


def test_event_proposal_waits_for_acceptance(client, two_accounts, data_dir: Path):
    filed = client.post("/api/v1/google/event-proposals", json=PROPOSAL)
    assert filed.status_code == 201 and filed.json()["calendar"] == "Ärzte"
    assert two_accounts.created == []
    page = client.get("/calendar").text
    assert "Zahnarzt Kontrolle" in page and "Six months after the last visit" in page
    assert client.get("/status.json").json()["event_proposals"] == 1
    assert "1 proposed event" in client.get("/status.json").json()["headline"]

    # The person can change the event before accepting.
    accepted = client.post("/calendar/proposals/1/accept", follow_redirects=False, data={
        "summary": "Zahnarzt", "starts": "2027-04-07T09:00", "ends": "2027-04-07T09:30",
        "description": "", "location": "Hanau"})
    assert accepted.status_code == 303
    sent = two_accounts.created[0]
    assert sent["calendar"] == "aerzte@group" and sent["summary"] == "Zahnarzt"
    assert sent["start"]["dateTime"] == "2027-04-07T09:00:00"
    row = db.connect(data_dir).execute("SELECT * FROM event_proposals").fetchone()
    assert row["status"] == "accepted" and row["event_link"] == "https://cal/new"
    assert client.get("/status.json").json()["event_proposals"] == 0
    # A decided proposal cannot be accepted twice.
    again = client.post("/calendar/proposals/1/accept", data={"summary": "x", "starts": "2027-04-07"})
    assert again.status_code == 404 and len(two_accounts.created) == 1


def test_event_proposal_checks_its_input(client, two_accounts):
    assert client.post("/api/v1/google/event-proposals", json={**PROPOSAL, "calendar": "Nope"}).status_code == 404
    assert client.post("/api/v1/google/event-proposals", json={**PROPOSAL, "start": "soon"}).status_code == 400
    assert client.post("/api/v1/google/event-proposals", json={**PROPOSAL, "summary": ""}).status_code == 400
    assert "No proposed events" in client.get("/calendar").text


def test_a_failed_acceptance_stays_open(client, app, two_accounts, data_dir: Path):
    client.post("/api/v1/google/event-proposals", json=PROPOSAL)
    app.state.google._tokens.clear()
    two_accounts.refuse.add("refresh-private")
    client.post("/calendar/proposals/1/accept", follow_redirects=False,
                data={"summary": "Zahnarzt", "starts": "2027-04-06"})
    row = db.connect(data_dir).execute("SELECT * FROM event_proposals").fetchone()
    assert row["status"] == "pending" and "refused" in row["error"] and row["summary"] == "Zahnarzt"
    two_accounts.refuse.clear()
    page = client.get("/calendar").text
    assert "Google did not take the event" in page
    bad = client.post("/calendar/proposals/1/accept", follow_redirects=False,
                      data={"summary": "Zahnarzt", "starts": "whenever"})
    assert bad.status_code == 303 and two_accounts.created == []
    client.post("/calendar/proposals/1/reject", follow_redirects=False)
    assert db.connect(data_dir).execute("SELECT status FROM event_proposals").fetchone()[0] == "rejected"


# --- pages ---------------------------------------------------------------------


def test_calendar_page_shows_the_agenda(client, app, two_accounts, monkeypatch):
    from datetime import date
    monkeypatch.setattr("claude_hub.server.calendar_views.local_today", lambda zone: date(2026, 10, 5))
    page = client.get("/calendar").text
    assert "Today, 2026-10-05" in page and "Tomorrow, 2026-10-06" in page
    assert "Ihr Zahnarzttermin" in page and "09:00 to 09:30" in page and "from mail" in page
    assert page.index("Checkup") < page.index("Standup") < page.index("Ihr Zahnarzttermin")
    # A second load within two minutes asks Google for nothing.
    calls = len(two_accounts.calls)
    client.get("/calendar")
    assert len(two_accounts.calls) == calls
    # A written event shows on the next load.
    client.post("/api/v1/google/events", json={"account": "work", "summary": "Blocker", "start": "2026-10-07"})
    client.get("/calendar")
    assert len(two_accounts.calls) > calls + 2


def test_calendar_page_without_accounts_and_while_hiding(client, fake, two_accounts=None):
    page = client.get("/calendar")
    assert page.status_code == 200 and "No Google account is connected" in page.text
    assert 'href="/calendar"' in client.get("/").text
    assert "Google accounts" in client.get("/settings").text
    connect(client)
    client.post("/api/v1/google/event-proposals", json=PROPOSAL)
    hidden = client.get("/calendar", params={"hide": 1}).text
    assert "Zahnarzt" not in hidden and "Hidden while" in hidden
    calls = len(fake.calls)
    client.get("/calendar", params={"hide": 1})
    assert len(fake.calls) == calls


def test_remove_account_in_the_hub(client, two_accounts):
    client.post("/settings/google/work/remove", follow_redirects=False)
    assert [a["label"] for a in client.get("/api/v1/google/accounts").json()["accounts"]] == ["private"]
    assert client.delete("/api/v1/google/accounts/private").json() == {"removed": True}
    assert client.delete("/api/v1/google/accounts/private").json() == {"removed": False}


# --- mail ---------------------------------------------------------------------


def test_mail_search_and_read(client, two_accounts, data_dir: Path):
    found = client.get("/api/v1/google/mail", params={"q": "Zahnarzttermin"}).json()["messages"]
    assert [(m["account"], m["subject"]) for m in found] == [
        ("private", "Terminbestätigung"), ("work", "Terminbestätigung")]
    assert found[0]["snippet"] == "Ihr Termin am 6. Oktober & mehr"
    assert client.get("/api/v1/google/mail", params={"q": "nothing", "account": "work"}).json()["messages"] == []
    mail = client.get("/api/v1/google/mail/m1", params={"account": "private"}).json()
    assert mail["text"] == "Ihr Termin:\n6. Oktober, 9 Uhr" and mail["from"].startswith("Praxis")
    actions = [row["action"] for row in db.connect(data_dir).execute("SELECT action FROM google_log")]
    assert actions.count("mail searched") == 3 and actions.count("mail read") == 1
    # The hub has no way to send mail.
    assert not [call for call in two_accounts.calls if "send" in call[1]]


# --- connector tools ---------------------------------------------------------------------


@pytest.fixture
def cfg(live_server, tmp_path: Path) -> Config:
    from tests.conftest import TOKEN
    return Config(live_server, TOKEN, [Account("default", tmp_path / ".claude")])


def rpc(cfg: Config, tmp_path: Path, name: str, arguments: dict) -> dict:
    stdin = io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}}).encode() + b"\n")
    stdout = io.BytesIO()
    mcp_server.serve(cfg, tmp_path / "cache", stdin, stdout)
    return json.loads(stdout.getvalue())["result"]


def test_connector_tools(cfg, app, tmp_path: Path):
    fake = FakeGoogle()
    app.state.google.http = fake
    from claude_hub.collector.client import Client
    hub = Client(cfg.server_url, cfg.token)
    for label in ("private", "work"):
        hub.request("PUT", f"/api/v1/google/accounts/{label}", {
            "client_id": "cid", "client_secret": "cs", "refresh_token": f"refresh-{label}", "scopes": SCOPES})

    names = [tool["name"] for tool in mcp_server.TOOLS]
    assert {"hub_rules", "calendar_agenda", "calendar_propose_event", "mail_read"} <= set(names)
    assert "calendar_propose_event" in mcp_server.INSTRUCTIONS and "data" in mcp_server.INSTRUCTIONS

    listed = rpc(cfg, tmp_path, "calendar_list", {})["content"][0]["text"]
    assert "Account private (robin@example.com):" in listed and "Ärzte | id aerzte@group" in listed
    assert "Feiertage [read only, not shown]" in listed and "Robin Golle [main calendar]" in listed

    agenda = rpc(cfg, tmp_path, "calendar_agenda", {"start": "2026-10-05", "end": "2026-10-06"})
    lines = agenda["content"][0]["text"].splitlines()
    assert lines[0] == "2026-10-05 | Checkup | Ärzte (private) | id e2"
    assert lines[2] == "2026-10-06 09:00 to 09:30 | Ihr Zahnarzttermin | Robin Golle (private) | from a mail | id e1"

    proposed = rpc(cfg, tmp_path, "calendar_propose_event", {
        "account": "private", "calendar": "Ärzte", "summary": "Zahnarzt", "start": "2027-04-06",
        "reason": "due"})
    assert not proposed["isError"] and "Nothing is written yet" in proposed["content"][0]["text"]
    assert fake.created == []

    created = rpc(cfg, tmp_path, "calendar_create_event", {
        "account": "work", "summary": "Arzt", "start": "2027-04-06T09:00", "end": "2027-04-06T10:00",
        "private": True, "source": "chat"})
    text = created["content"][0]["text"]
    # The answer names the account that was written to.
    assert text.startswith("Created: Arzt | 2027-04-06 09:00 to 10:00 | calendar Work of robin@work.example")
    assert fake.created[0]["visibility"] == "private"

    changed = rpc(cfg, tmp_path, "calendar_update_event", {
        "account": "private", "calendar": "Ärzte", "event_id": "e2", "location": "Marburg"})
    assert changed["content"][0]["text"].startswith("Changed: Changed")

    wrong = rpc(cfg, tmp_path, "calendar_create_event", {"account": "private", "calendar": "Nope",
                                                        "summary": "x", "start": "2027-04-06"})
    assert wrong["isError"] and wrong["content"][0]["text"].startswith("No calendar 'Nope'")

    mails = rpc(cfg, tmp_path, "mail_search", {"query": "Zahnarzttermin", "account": "private"})
    assert "Terminbestätigung | private | id m1" in mails["content"][0]["text"]
    mail = rpc(cfg, tmp_path, "mail_read", {"account": "private", "id": "m1"})["content"][0]["text"]
    assert mail.startswith("From: Praxis") and mail.endswith("6. Oktober, 9 Uhr")
    assert "No mail matches" in rpc(cfg, tmp_path, "mail_search", {"query": "nothing"})["content"][0]["text"]


def test_connector_page_lists_the_google_tools(client):
    assert "calendar_agenda" in client.get("/settings/connector").text


# --- sign-in command ---------------------------------------------------------------------


def write_client(path: Path, kind: str = "installed") -> Path:
    path.write_text(json.dumps({kind: {"client_id": "cid.apps", "client_secret": "csecret"}}))
    return path


def test_read_client_file(tmp_path: Path):
    assert google_auth.read_client(write_client(tmp_path / "c.json")) == ("cid.apps", "csecret")
    with pytest.raises(google_auth.SignInError, match="Desktop app"):
        google_auth.read_client(write_client(tmp_path / "web.json", "web"))
    with pytest.raises(google_auth.SignInError, match="Cannot read"):
        google_auth.read_client(tmp_path / "missing.json")
    config = tmp_path / "conf" / "collector.toml"
    with pytest.raises(google_auth.SignInError, match="--client"):
        google_auth.find_client(None, config)
    google_auth.find_client(tmp_path / "c.json", config)
    # The copy makes later calls work without the file.
    assert google_auth.find_client(None, config) == ("cid.apps", "csecret")


def test_auth_url_asks_for_a_lasting_sign_in():
    verifier, challenge = google_auth.pkce()
    assert len(verifier) >= 43 and "=" not in challenge
    query = parse_qs(urlsplit(google_auth.auth_url("cid", "http://127.0.0.1:1", SCOPES, "st", challenge)).query)
    assert query["access_type"] == ["offline"] and "consent" in query["prompt"][0]
    assert query["scope"] == [" ".join(SCOPES)] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://127.0.0.1:1"] and query["state"] == ["st"]


def sign_in(cfg, tmp_path, monkeypatch, browser, post, **kwargs):
    said: list[str] = []
    return google_auth.add_account(
        cfg, tmp_path / "conf" / "collector.toml", "private", write_client(tmp_path / "c.json"),
        open_browser=browser, post=post, say=said.append, **kwargs), said


def test_sign_in_end_to_end(cfg, app, tmp_path: Path, monkeypatch):
    app.state.google.http = FakeGoogle()
    seen: dict = {}

    def browser(url: str) -> None:
        # Stand in for Google: send the browser back to the local port with a code.
        query = parse_qs(urlsplit(url).query)
        seen["query"] = query
        target = f"{query['redirect_uri'][0]}/?state={query['state'][0]}&code=the-code"
        threading.Thread(target=lambda: urllib.request.urlopen(target).read(), daemon=True).start()

    def post(url: str, fields: dict) -> tuple[int, dict]:
        seen["exchange"] = fields
        return 200, {"refresh_token": "refresh-private", "access_token": "a", "scope": " ".join(SCOPES)}

    email, said = sign_in(cfg, tmp_path, monkeypatch, browser, post)
    assert email == "robin@example.com" and said[1].startswith("https://accounts.google.com/")
    assert seen["exchange"]["code"] == "the-code" and seen["exchange"]["code_verifier"]
    assert seen["exchange"]["redirect_uri"] == seen["query"]["redirect_uri"][0]
    assert app.state.google.store.get("private")["refresh_token"] == "refresh-private"


def test_sign_in_failures(cfg, app, tmp_path: Path, monkeypatch):
    app.state.google.http = FakeGoogle()

    def answer(extra: str):
        def browser(url: str) -> None:
            query = parse_qs(urlsplit(url).query)
            state = "forged" if extra == "forged" else query["state"][0]
            tail = "code=c" if extra in ("ok", "forged") else "error=access_denied"
            threading.Thread(target=lambda: urllib.request.urlopen(
                f"{query['redirect_uri'][0]}/?state={state}&{tail}").read(), daemon=True).start()
        return browser

    with pytest.raises(google_auth.SignInError, match="access_denied"):
        sign_in(cfg, tmp_path, monkeypatch, answer("denied"), None)
    # An answer that does not carry the state of this run is not used.
    with pytest.raises(google_auth.SignInError, match="wrong state"):
        sign_in(cfg, tmp_path, monkeypatch, answer("forged"), None)
    with pytest.raises(google_auth.SignInError, match="no lasting sign-in"):
        sign_in(cfg, tmp_path, monkeypatch, answer("ok"), lambda url, fields: (200, {"access_token": "a"}))
    with pytest.raises(google_auth.SignInError, match="refused the code"):
        sign_in(cfg, tmp_path, monkeypatch, answer("ok"),
                lambda url, fields: (400, {"error": "invalid_grant"}))
    with pytest.raises(google_auth.SignInError, match="in time"):
        sign_in(cfg, tmp_path, monkeypatch, lambda url: None, None, wait_seconds=0)
    # Fewer permissions than the hub needs: the hub says what is missing.
    with pytest.raises(google_auth.SignInError, match="tick every"):
        sign_in(cfg, tmp_path, monkeypatch, answer("ok"), lambda url, fields: (
            200, {"refresh_token": "refresh-private", "scope": SCOPES[0]}))
    assert app.state.google.store.labels() == []


def test_google_commands(cfg, app, tmp_path: Path, capsys, monkeypatch):
    app.state.google.http = FakeGoogle()
    config = tmp_path / "collector.toml"
    config.write_text(f'server_url = "{cfg.server_url}"\ntoken = "{cfg.token}"\ncowork = false\n')
    assert cli.main(["--config", str(config), "google", "list"]) == 0
    assert "No Google account is connected" in capsys.readouterr().out
    monkeypatch.setattr(google_auth, "add_account", lambda *args, **kwargs: "robin@example.com")
    assert cli.main(["--config", str(config), "google", "add", "private"]) == 0
    assert "Connected robin@example.com as 'private'" in capsys.readouterr().out
    from claude_hub.collector.client import Client
    Client(cfg.server_url, cfg.token).request("PUT", "/api/v1/google/accounts/private", {
        "client_id": "c", "client_secret": "s", "refresh_token": "refresh-private", "scopes": SCOPES})
    assert cli.main(["--config", str(config), "google", "list"]) == 0
    assert "private: robin@example.com (works)" in capsys.readouterr().out
    assert cli.main(["--config", str(config), "google", "remove", "private"]) == 0
    assert "Removed 'private'" in capsys.readouterr().out


def test_forms_refuse_posts_from_other_sites(client, two_accounts):
    client.post("/api/v1/google/event-proposals", json=PROPOSAL)
    data = {"summary": "Injected", "starts": "2027-04-06"}
    foreign = client.post("/calendar/proposals/1/accept", data=data, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403 and two_accounts.created == []
    assert client.post("/rulesets/x/edit", data={"text": "y"}, headers={"Origin": "null"}).status_code == 403
    own = client.post("/calendar/proposals/1/accept", data=data, follow_redirects=False,
                      headers={"Origin": "http://testserver"})
    assert own.status_code == 303 and len(two_accounts.created) == 1
