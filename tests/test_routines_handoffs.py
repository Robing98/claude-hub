from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pytest

from claude_hub.collector import briefing, cli, handoff, mcp_server, netinfo
from claude_hub.collector.config import Account, Config
from claude_hub.server import db, routines, wake
from tests.conftest import TOKEN

REMOTE = "github.com/robing98/orbis"
TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    for module in ("hub_api", "more_views", "views"):
        monkeypatch.setattr(f"claude_hub.server.{module}.local_today", lambda zone: TODAY)


# --- routines: the arithmetic ---------------------------------------------------------


def test_add_interval():
    assert routines.add_interval(date(2026, 10, 5), 12, "weeks") == date(2026, 12, 28)
    assert routines.add_interval(date(2026, 10, 5), 84, "days") == date(2026, 12, 28)
    assert routines.add_interval(date(2026, 10, 5), 6, "months") == date(2027, 4, 5)
    # The 31st has no counterpart in a shorter month.
    assert routines.add_interval(date(2026, 8, 31), 6, "months") == date(2027, 2, 28)
    assert routines.add_interval(date(2027, 11, 30), 3, "months") == date(2028, 2, 29)


@pytest.mark.parametrize("fields, message", [
    ({"name": "", "every": 6, "next_due": "2026-11-03"}, "needs a name"),
    ({"name": "x", "every": 0, "next_due": "2026-11-03"}, "interval"),
    ({"name": "x", "every": "often", "next_due": "2026-11-03"}, "whole numbers"),
    ({"name": "x", "every": 6, "unit": "years", "next_due": "2026-11-03"}, "interval"),
    ({"name": "x", "every": 6, "next_due": "soon"}, "due day"),
    ({"name": "x", "every": 6, "next_due": "2026-11-03", "lead_days": 900}, "lead time"),
    ({"name": "x", "every": 6, "next_due": "2026-11-03", "mail_to": "a@b.de, c@d.de"}, "mail address"),
    # Due again the moment it is done: with automatic sending, a mail per check.
    ({"name": "x", "every": 2, "unit": "weeks", "next_due": "2026-11-03", "lead_days": 14}, "shorter than the interval"),
    ({"name": "x", "every": 6, "next_due": "2026-11-03", "mail_mode": "always"}, "mail mode"),
    ({"name": "x", "every": 6, "next_due": "2026-11-03", "mail_mode": "auto", "mail_to": "a@b.de"}, "needs a recipient"),
])
def test_clean_refuses(fields, message):
    with pytest.raises(routines.RoutineError, match=message):
        routines.clean(fields)


def test_mailto_link():
    row = {"mail_to": "praxis@example.com", "mail_subject": "Folgerezept", "mail_text": "Guten Tag,\nbitte & danke"}
    assert routines.mailto(row) == ("mailto:praxis@example.com?subject=Folgerezept"
                                   "&body=Guten%20Tag%2C%0Abitte%20%26%20danke")
    assert routines.mailto({**row, "mail_to": ""}) == ""


# --- routines: the page ---------------------------------------------------------


CHECKUP = {"name": "Checkup", "every": "6", "unit": "months", "anchor": "done", "lead_days": "42",
           "next_due": "2026-11-01", "note": "Der Termin kommt per Post.", "enabled": "1"}
REZEPT = {"name": "Folgerezept", "every": "12", "unit": "weeks", "anchor": "due", "lead_days": "10",
          "next_due": "2026-10-01", "mail_to": "praxis@example.com", "mail_subject": "Folgerezept",
          "mail_text": "Guten Tag", "enabled": "1"}


def one(data_dir: Path, sql: str):
    return db.connect(data_dir).execute(sql).fetchone()


def test_routine_becomes_due_inside_its_lead_time(client, data_dir: Path):
    assert "Nothing is due" in client.get("/routines").text
    assert client.post("/routines", data=CHECKUP, follow_redirects=False).status_code == 303
    assert client.post("/routines", data={**CHECKUP, "name": "Dentist", "next_due": "2027-04-06"},
                       follow_redirects=False).status_code == 303
    page = client.get("/routines").text
    # 27 days to go, lead time 42 days: due. The dentist in April is not.
    assert "due in 27 days" in page and "Der Termin kommt per Post." in page
    assert page.count("Done on") == 1
    status = client.get("/status.json").json()
    assert status["routines_due"] == 1 and "1 routine due" in status["headline"]
    # The feed goes to a lock screen. It names no routine.
    assert "Checkup" not in json.dumps(status)
    assert "1 routine is due" in client.get("/").text


def test_done_counts_from_the_day_it_happened(client, data_dir: Path):
    client.post("/routines", data=CHECKUP)
    client.post("/routines/1/done", data={"day": "2026-10-20"}, follow_redirects=False)
    row = one(data_dir, "SELECT * FROM routines")
    assert (row["next_due"], row["last_done"]) == ("2027-04-20", "2026-10-20")
    assert client.get("/status.json").json()["routines_due"] == 0
    log = one(data_dir, "SELECT * FROM routine_log")
    assert (log["action"], log["day"], log["next_due"]) == ("done", "2026-10-20", "2027-04-20")
    assert "2027-04-20" in client.get("/routines").text
    assert client.post("/routines/1/done", data={"day": "20.10.2026"}).status_code == 400


def test_supply_counts_from_the_due_day_and_offers_the_mail(client, data_dir: Path):
    client.post("/routines", data=REZEPT)
    page = client.get("/routines").text
    assert "4 days overdue" in page
    assert 'href="mailto:praxis@example.com?subject=Folgerezept&amp;body=Guten%20Tag"' in page
    # Sent late, on the 5th. The supply still runs out twelve weeks after the due day.
    client.post("/routines/1/done", data={"day": ""}, follow_redirects=False)
    row = one(data_dir, "SELECT * FROM routines")
    assert (row["next_due"], row["last_done"]) == ("2026-12-24", "2026-10-05")


def test_skip_never_leaves_the_next_round_in_the_past(client, data_dir: Path):
    client.post("/routines", data={**REZEPT, "next_due": "2026-01-05", "every": "4"})
    client.post("/routines/1/skip", follow_redirects=False)
    row = one(data_dir, "SELECT * FROM routines")
    assert row["next_due"] == "2026-10-12" and row["last_done"] is None
    assert client.post("/routines/1/forget").status_code == 404
    assert client.post("/routines/99/done", data={"day": ""}).status_code == 400


def test_edit_turn_off_and_delete(client, data_dir: Path):
    client.post("/routines", data=CHECKUP)
    off = {key: value for key, value in CHECKUP.items() if key != "enabled"}
    client.post("/routines/1", data={**off, "every": "12", "name": "Dentist"}, follow_redirects=False)
    row = one(data_dir, "SELECT * FROM routines")
    assert (row["name"], row["every"], row["enabled"]) == ("Dentist", 12, 0)
    assert client.get("/status.json").json()["routines_due"] == 0
    assert client.post("/routines/1", data={**CHECKUP, "every": "never"}).status_code == 400
    client.post("/routines/1/delete", follow_redirects=False)
    assert one(data_dir, "SELECT COUNT(*) FROM routines")[0] == 0


def test_routines_are_hidden_with_private_projects(client):
    client.post("/routines", data=REZEPT)
    hidden = client.get("/routines", params={"hide": 1}).text
    assert "Folgerezept" not in hidden and "praxis@example.com" not in hidden
    assert "1 routine, 1 due" in hidden


def test_a_session_can_only_draft_a_routine(client, data_dir: Path):
    drafted = client.post("/api/v1/routines", json={
        "name": "Folgerezept", "every": 12, "unit": "weeks", "next_due": "2026-10-01",
        "anchor": "due", "mail_to": "praxis@example.com", "source": "Cerebellum"})
    assert drafted.status_code == 201 and drafted.json() == {"id": 1, "enabled": False}
    # Turned off: it reminds of nothing until the person turns it on.
    assert client.get("/status.json").json()["routines_due"] == 0
    assert "drafted by Cerebellum" in client.get("/routines").text
    listed = client.get("/api/v1/routines").json()
    assert listed["today"] == "2026-10-05" and listed["routines"][0]["state"] == "off"
    assert client.post("/api/v1/routines", json={"name": "x", "every": 0, "next_due": "2026-10-01"}).status_code == 400
    from fastapi.testclient import TestClient
    with TestClient(client.app) as anonymous:
        assert anonymous.get("/api/v1/routines").status_code == 401


# --- handoffs ---------------------------------------------------------


WHERE = {"cwd": "D:\\dev\\orbis", "remote": REMOTE, "repo_root": "D:\\dev\\orbis"}
BRIEF = {"title": "Sprite brief: forest biome", "text": "Trees need three growth stages.",
         "from_lane": "Design", "to_lane": "code", **WHERE}


def test_handoff_reaches_each_session_once(client, data_dir: Path):
    filed = client.post("/api/v1/handoffs", json=BRIEF)
    assert filed.status_code == 201 and filed.json() == {"id": 1, "project": "orbis"}
    first = client.get("/api/v1/handoffs/new", params={"session": "s1", **WHERE}).json()["handoffs"]
    assert [(h["id"], h["from"], h["to"], h["text"]) for h in first] == [
        (1, "design", "code", "Trees need three growth stages.")]
    # The same session is not shown it again. Another session is.
    assert client.get("/api/v1/handoffs/new", params={"session": "s1", **WHERE}).json()["handoffs"] == []
    assert len(client.get("/api/v1/handoffs/new", params={"session": "s2", **WHERE}).json()["handoffs"]) == 1
    # A session in another project gets nothing.
    other = {"cwd": "D:\\dev\\x", "remote": "github.com/robing98/other", "session": "s3"}
    assert client.get("/api/v1/handoffs/new", params=other).json()["handoffs"] == []
    assert client.get("/status.json").json()["open_handoffs"] == 1


def test_handoff_take_and_done(client, data_dir: Path):
    client.post("/api/v1/handoffs", json=BRIEF)
    looked = client.get("/api/v1/handoffs/1").json()
    assert looked["status"] == "open" and looked["taken_now"] is False
    taken = client.get("/api/v1/handoffs/1", params={"take": 1, "by": "Code"}).json()
    assert taken["status"] == "taken" and taken["taken_now"] and taken["taken_by"] == "code"
    # A second session that tries to take it learns that it came too late.
    late = client.get("/api/v1/handoffs/1", params={"take": 1, "by": "other"}).json()
    assert late["taken_now"] is False and late["taken_by"] == "code"
    # A taken handoff is not delivered to new sessions.
    assert client.get("/api/v1/handoffs/new", params={"session": "s9", **WHERE}).json()["handoffs"] == []
    assert client.get("/api/v1/handoffs", params=WHERE).json()["handoffs"] == []
    assert len(client.get("/api/v1/handoffs", params={**WHERE, "status": "all"}).json()["handoffs"]) == 1
    done = client.post("/api/v1/handoffs/1/done", json={"result": "Stages are in."}).json()
    assert done["status"] == "done" and done["result"] == "Stages are in."
    assert client.get("/api/v1/handoffs/7").status_code == 404
    assert client.post("/api/v1/handoffs/7/done", json={}).status_code == 404
    assert client.get("/api/v1/handoffs", params={**WHERE, "status": "lost"}).status_code == 400


def test_handoff_input_is_checked(client):
    assert client.post("/api/v1/handoffs", json={**BRIEF, "text": " "}).status_code == 400
    assert client.post("/api/v1/handoffs", json={**BRIEF, "text": "x" * 100_001}).status_code == 400
    assert client.post("/api/v1/handoffs", json={"title": "t", "text": "x"}).status_code == 404
    long = client.post("/api/v1/handoffs", json={**BRIEF, "text": "x" * 9000}).json()
    new = client.get("/api/v1/handoffs/new", params={"session": "s1", **WHERE}).json()["handoffs"][0]
    assert new["cut"] and len(new["text"]) == 6000 and new["size"] == 9000
    assert len(client.get(f"/api/v1/handoffs/{long['id']}").json()["text"]) == 9000


def test_handoff_page(client, data_dir: Path):
    client.post("/api/v1/handoffs", json=BRIEF)
    page = client.get("/handoffs").text
    assert "Sprite brief: forest biome" in page and "from design to code" in page
    assert "Trees need three growth stages." in page
    project = one(data_dir, "SELECT id FROM projects")["id"]
    # The person leaves a text for a lane.
    client.post("/handoffs", data={"project": project, "title": "Please rebase", "text": "main moved",
                                   "to_lane": "Code"}, follow_redirects=False)
    row = one(data_dir, "SELECT * FROM handoffs WHERE id = 2")
    assert (row["from_lane"], row["to_lane"], row["status"]) == ("robin", "code", "open")
    client.get("/api/v1/handoffs/new", params={"session": "s1", **WHERE})
    client.post("/handoffs/1/done", follow_redirects=False)
    assert "Sprite brief" not in client.get("/handoffs").text
    assert "Sprite brief" in client.get("/handoffs", params={"show": "all"}).text
    # Opened again, it is delivered again, also to a session that saw it.
    client.post("/handoffs/1/reopen", follow_redirects=False)
    again = client.get("/api/v1/handoffs/new", params={"session": "s1", **WHERE}).json()["handoffs"]
    assert [h["id"] for h in again] == [1]
    client.post("/handoffs/2/delete", follow_redirects=False)
    assert one(data_dir, "SELECT COUNT(*) FROM handoffs")[0] == 1
    assert client.post("/handoffs", data={"project": 999, "title": "t", "text": "x"}).status_code == 400

    conn = db.connect(data_dir)
    conn.execute("UPDATE projects SET private = 1")
    conn.commit()
    hidden = client.get("/handoffs", params={"hide": 1}).text
    assert "Sprite brief" not in hidden and "Trees need" not in hidden and "Project 1" in hidden


@pytest.fixture
def cfg(live_server, tmp_path: Path) -> Config:
    return Config(live_server, TOKEN, [Account("default", tmp_path / ".claude")])


@pytest.fixture
def orbis(tmp_path: Path, monkeypatch) -> str:
    """A folder that the collector takes for the Orbis repository, without calling Git."""
    folder = str(tmp_path / "orbis")
    found = {"cwd": folder, "remote": REMOTE, "repo_root": folder}
    monkeypatch.setattr(handoff, "where", lambda path: found)
    monkeypatch.setattr(briefing, "where", lambda path: found)
    return folder


def test_prompt_hook_delivers_once_and_survives_an_absent_hub(cfg, orbis, tmp_path, capsys, monkeypatch):
    cache = tmp_path / "cache"
    assert handoff.send(cfg, orbis, "Sprite brief", "Three growth stages.", "code", "design")[0]
    items = handoff.new_for_prompt(cfg, cache, orbis, "session-1")
    text = handoff.render_new(items, "HUB")
    assert 'Handoff #1: "Sprite brief" (from the lane "design", for the lane "code")' in text
    assert "HUB handoff show 1 --take --by YOUR_LANE" in text and "Three growth stages." in text
    assert 'HUB handoff done 1 "RESULT' in text
    assert handoff.new_for_prompt(cfg, cache, orbis, "session-1") == []
    assert handoff.render_new([], "HUB") == ""

    # The hub is away: the hook gives up, and the next prompts do not wait for it.
    away = Config("http://127.0.0.1:9", TOKEN, cfg.accounts)
    assert handoff.new_for_prompt(away, cache, orbis, "session-2") == []
    monkeypatch.setattr(handoff, "Client", None)  # any request would now fail loudly
    assert handoff.new_for_prompt(cfg, cache, orbis, "session-2") == []


def test_prompt_hook_command(cfg, orbis, tmp_path, capsys, monkeypatch):
    config = tmp_path / "collector.toml"
    config.write_text(f'server_url = "{cfg.server_url}"\ntoken = "{TOKEN}"\ncowork = false\n')
    handoff.send(cfg, orbis, "Sprite brief", "Three growth stages.")
    event = json.dumps({"session_id": "abc", "cwd": orbis, "prompt": "weiter"})
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    assert cli.main(["--config", str(config), "hook", "prompt"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[Claude hub]") and "for any lane" in out and "--config" in out
    # No session, broken input, or no configuration: silent, and never an error.
    for raw, conf in (("{}", config), ("not json", config), (event, tmp_path / "missing.toml")):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert cli.main(["--config", str(conf), "hook", "prompt"]) == 0
        assert capsys.readouterr().out == ""


def test_handoff_commands(cfg, orbis, tmp_path, capsys, monkeypatch):
    config = tmp_path / "collector.toml"
    config.write_text(f'server_url = "{cfg.server_url}"\ntoken = "{TOKEN}"\ncowork = false\n')
    run = lambda *args: cli.main(["--config", str(config), "handoff", *args])  # noqa: E731
    brief = tmp_path / "brief.md"
    brief.write_text("# Lore\n\nÄlteste Wälder.", encoding="utf-8")
    assert run("send", "Lore of the forest", "--file", str(brief), "--to", "code", "--from", "design") == 0
    assert 'Handoff 1 is filed in the project orbis for the lane "code"' in capsys.readouterr().out
    assert run("list") == 0
    assert '#1 [open] "Lore of the forest"' in capsys.readouterr().out
    assert run("show", "1", "--take", "--by", "code") == 0
    out = capsys.readouterr().out
    assert "It is yours now." in out and "Älteste Wälder." in out
    assert run("show", "1", "--take", "--by", "Code") == 0
    assert "Your lane has it already." in capsys.readouterr().out
    assert run("show", "1", "--take", "--by", "art") == 0
    assert "You did not get it: it was already taken by code." in capsys.readouterr().out
    assert run("done", "1", "Written into docs/lore.md") == 0
    assert run("list") == 0
    assert "No handoffs with the status open in the project orbis." in capsys.readouterr().out
    assert run("show", "9") == 1
    assert "No handoff 9" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("piped text"))
    assert run("send", "From a pipe") == 0


def test_hooks_install_adds_both_and_removes_both(tmp_path: Path):
    folder = tmp_path / ".claude"
    folder.mkdir()
    (folder / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "other-tool"}]}]}}))
    assert "installed" in briefing.install_hook(folder, None)
    hooks = json.loads((folder / "settings.json").read_text())["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"].endswith("hook session-start")
    assert [h["hooks"][0]["command"].split()[-1] for h in hooks["UserPromptSubmit"]] == ["other-tool", "prompt"]
    assert hooks["UserPromptSubmit"][1]["hooks"][0]["timeout"] == 10
    assert "updated" in briefing.install_hook(folder, None)
    assert len(json.loads((folder / "settings.json").read_text())["hooks"]["UserPromptSubmit"]) == 2
    assert "removed" in briefing.install_hook(folder, None, remove=True)
    assert json.loads((folder / "settings.json").read_text())["hooks"] == {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}
    assert "no hub hook" in briefing.install_hook(folder, None, remove=True)


# --- wake ---------------------------------------------------------


def test_magic_packet():
    packet = wake.magic_packet("AA-BB-CC-00-11-22")
    assert packet[:6] == b"\xff" * 6 and len(packet) == 102
    assert packet[6:12] == bytes.fromhex("aabbcc001122") and packet[-6:] == packet[6:12]
    assert wake.normalize("aa:bb:cc:00:11:22") == "aa:bb:cc:00:11:22"
    assert wake.normalize("aabbcc001122") == "" and wake.normalize("") == ""
    with pytest.raises(ValueError):
        wake.magic_packet("nope")


GETMAC = (
    '"Ethernet","Realtek Gaming 2.5GbE Family Controller","AA-BB-CC-00-11-22","\\Device\\Tcpip_{1}"\r\n'
    '"WLAN","Intel(R) Wi-Fi 6E AX211","AA-BB-CC-00-11-33","Medien getrennt"\r\n'
    '"vEthernet (WSL)","Hyper-V Virtual Ethernet Adapter","00-15-5D-00-11-44","\\Device\\Tcpip_{2}"\r\n'
    '"Bluetooth-Netzwerkverbindung","Bluetooth Device (Personal Area Network)","AA-BB-CC-00-11-55","Medien getrennt"\r\n'
    '\r\n"broken line"\r\n')


def test_adapters_from_windows_output():
    found = netinfo.parse_getmac(GETMAC)
    assert [(a["mac"], a["connected"], a["wired"]) for a in found] == [
        ("aa:bb:cc:00:11:22", True, True), ("aa:bb:cc:00:11:33", False, False),
        ("00:15:5d:00:11:44", True, False), ("aa:bb:cc:00:11:55", False, False)]
    assert found[0]["name"] == "Ethernet (Realtek Gaming 2.5GbE Family Controller)"
    # One connected cable adapter: that is the one to wake, without a choice by the person.
    assert wake.likely_adapter(found) == "aa:bb:cc:00:11:22"
    assert wake.likely_adapter(found + [{**found[0], "mac": "aa:bb:cc:00:11:66"}]) == ""
    assert wake.likely_adapter([found[1]]) == ""


def test_adapters_from_linux(tmp_path: Path):
    for name, mac, state, real, wireless in (("eth0", "AA:BB:CC:00:11:22", "up", True, False),
                                            ("wlan0", "aa:bb:cc:00:11:33", "down", True, True),
                                            ("lo", "00:00:00:00:00:00", "unknown", False, False),
                                            ("docker0", "02:42:00:00:00:01", "up", False, False)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "address").write_text(mac + "\n")
        (folder / "operstate").write_text(state + "\n")
        if real:
            (folder / "device").mkdir()
        if wireless:
            (folder / "wireless").mkdir()
    assert netinfo.linux_adapters(tmp_path) == [
        {"name": "eth0", "mac": "aa:bb:cc:00:11:22", "connected": True, "wired": True},
        {"name": "wlan0", "mac": "aa:bb:cc:00:11:33", "connected": False, "wired": False}]


def test_wake_through_the_hub(client, app, data_dir: Path):
    sent: list[tuple[str, str]] = []
    app.state.wake_sender = lambda mac, broadcast: sent.append((mac, broadcast))
    # Nothing is known about the machine yet.
    assert client.post("/api/v1/machines/desktop/wake").status_code == 409
    assert "has not reported its adapters" in client.get("/settings/devices").text
    client.post("/api/v1/hello", json={"platform": "windows", "adapters": netinfo.parse_getmac(GETMAC)})
    page = client.get("/settings/devices").text
    assert "Automatic: the one cable adapter" in page and "aa:bb:cc:00:11:33" in page
    woke = client.post("/api/v1/machines/Desktop/wake")
    assert woke.status_code == 200 and woke.json()["mac"] == "aa:bb:cc:00:11:22"
    assert sent == [("aa:bb:cc:00:11:22", "255.255.255.255")]

    # The person picks another adapter. That choice wins.
    client.post("/settings/devices/1/adapter", data={"mac": "AA:BB:CC:00:11:33"}, follow_redirects=False)
    done = client.post("/settings/devices/1/wake", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/settings/devices?woke=desktop"
    assert sent[-1][0] == "aa:bb:cc:00:11:33"
    assert "The wake signal for desktop is sent" in client.get("/settings/devices?woke=desktop").text
    assert client.post("/settings/devices/1/adapter", data={"other": "12345"}).status_code == 400
    assert client.post("/api/v1/machines/laptop/wake").status_code == 404
    # A hello without adapters, from an older collector, keeps what is known.
    client.post("/api/v1/hello", json={"platform": "windows"})
    assert one(data_dir, "SELECT adapters FROM machines")["adapters"]

    def broken(mac, broadcast):
        raise OSError("Network is unreachable")

    app.state.wake_sender = broken
    assert client.post("/api/v1/machines/desktop/wake").status_code == 502


def test_wake_command(cfg, app, tmp_path: Path, capsys):
    sent: list[str] = []
    app.state.wake_sender = lambda mac, broadcast: sent.append(mac)
    config = tmp_path / "collector.toml"
    config.write_text(f'server_url = "{cfg.server_url}"\ntoken = "{TOKEN}"\ncowork = false\n')
    assert cli.main(["--config", str(config), "wake", "desktop"]) == 1
    assert "Settings > Devices" in capsys.readouterr().err
    from claude_hub.collector.client import Client
    Client(cfg.server_url, TOKEN).request("POST", "/api/v1/hello", {
        "adapters": [{"name": "eth0", "mac": "aa:bb:cc:00:11:22", "connected": True, "wired": True}]})
    assert cli.main(["--config", str(config), "wake", "desktop"]) == 0
    assert "sent to aa:bb:cc:00:11:22" in capsys.readouterr().out and sent == ["aa:bb:cc:00:11:22"]


# --- connector tools ---------------------------------------------------------


def rpc(cfg: Config, tmp_path: Path, name: str, arguments: dict) -> tuple[str, bool]:
    stdin = io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}}).encode() + b"\n")
    stdout = io.BytesIO()
    mcp_server.serve(cfg, tmp_path / "cache", stdin, stdout)
    result = json.loads(stdout.getvalue())["result"]
    return result["content"][0]["text"], result["isError"]


def test_connector_tools_for_the_hub(cfg, app, orbis, tmp_path: Path):
    sent: list[str] = []
    app.state.wake_sender = lambda mac, broadcast: sent.append(mac)
    names = {tool["name"] for tool in mcp_server.TOOLS}
    assert {"hub_status", "handoff_send", "handoff_take", "routine_draft", "machine_wake"} <= names
    assert "handoff_send" in mcp_server.INSTRUCTIONS

    text, failed = rpc(cfg, tmp_path, "handoff_send", {
        "folder": orbis, "title": "Sprite brief", "text": "Three growth stages.", "to": "code", "from": "design"})
    assert not failed and "Handoff 1 is filed in the project orbis" in text
    assert '#1 [open] "Sprite brief"' in rpc(cfg, tmp_path, "handoff_list", {"folder": orbis})[0]
    # A chat that asks for the rules of the project learns about the handoff as well.
    rules, _ = rpc(cfg, tmp_path, "hub_rules", {"folder": orbis})
    assert "Handoffs waiting in this project" in rules and "Sprite brief" in rules
    taken, failed = rpc(cfg, tmp_path, "handoff_take", {"id": 1, "by": "code"})
    assert not failed and "It is yours now." in taken and "Three growth stages." in taken
    assert "Handoffs waiting" not in rpc(cfg, tmp_path, "hub_rules", {"folder": orbis})[0]
    assert "marked as done" in rpc(cfg, tmp_path, "handoff_done", {"id": 1, "result": "In."})[0]
    assert rpc(cfg, tmp_path, "handoff_take", {"id": 5})[1]

    drafted, failed = rpc(cfg, tmp_path, "routine_draft", {
        "name": "Dentist", "every": 12, "unit": "months", "next_due": "2027-04-06", "source": "chat"})
    assert not failed and "turned off until Robin turns it on" in drafted
    assert "#1 Dentist: every 12 months, next due 2027-04-06 (off)" in rpc(cfg, tmp_path, "routine_list", {})[0]
    refused, failed = rpc(cfg, tmp_path, "routine_draft", {"name": "x", "every": 1, "unit": "months", "next_due": "x"})
    assert failed and "due day" in refused

    status, failed = rpc(cfg, tmp_path, "hub_status", {})
    assert not failed and "asked from the machine desktop" in status
    assert "No Google account is connected." in status and "Machine desktop" in status
    assert "Open handoffs: 0. Routines due: 0." in status

    refused, failed = rpc(cfg, tmp_path, "machine_wake", {"name": "desktop"})
    assert failed and "Settings > Devices" in refused
    from claude_hub.collector.client import Client
    Client(cfg.server_url, TOKEN).request("POST", "/api/v1/hello", {
        "adapters": [{"name": "eth0", "mac": "aa:bb:cc:00:11:22", "connected": True, "wired": True}]})
    woke, failed = rpc(cfg, tmp_path, "machine_wake", {"name": "desktop"})
    assert not failed and "wake signal for desktop is sent" in woke and sent == ["aa:bb:cc:00:11:22"]


def test_status_endpoint(client):
    client.post("/api/v1/handoffs", json=BRIEF)
    answer = client.get("/api/v1/status").json()
    assert answer["schema_version"] == len(db.MIGRATIONS) and answer["open_handoffs"] == 1
    assert answer["machines"][0]["name"] == "desktop" and answer["google_accounts"] == []
    assert answer["feed"]["open_handoffs"] == 1


def test_new_pages_are_linked(client):
    home = client.get("/").text
    assert 'href="/routines"' in home and 'href="/handoffs"' in home
    assert 'href="/settings/devices"' in client.get("/settings").text


# --- routines that send their mail -------------------------------------------------


class Mailbox:
    """Stands in for the Google access of the hub."""

    def __init__(self):
        self.sent: list[tuple[str, str, str, str]] = []
        self.fail = ""

    def send_mail(self, label, to, subject, text):
        from claude_hub.server.google import GoogleError
        if self.fail:
            raise GoogleError(403, self.fail)
        self.sent.append((label, to, subject, text))
        return {"id": f"m{len(self.sent)}"}


SENDING = {**REZEPT, "mail_mode": "send", "mail_account": "private"}


@pytest.fixture
def mailbox(app) -> Mailbox:
    box = Mailbox()
    box.store = app.state.google.store
    box.time_zone = app.state.google.time_zone
    app.state.google = box
    return box


def test_send_on_a_press(client, mailbox, data_dir: Path):
    client.post("/routines", data=SENDING)
    page = client.get("/routines").text
    assert "Send the mail to praxis@example.com from private" in page and "sends on a press" in page
    assert client.post("/routines/1/send", follow_redirects=False).status_code == 303
    assert mailbox.sent == [("private", "praxis@example.com", "Folgerezept", "Guten Tag")]
    row = one(data_dir, "SELECT * FROM routines")
    # The round is closed: the next one counts from the due day, twelve weeks on.
    assert (row["next_due"], row["last_done"], row["mail_error"]) == ("2026-12-24", "2026-10-05", None)
    assert one(data_dir, "SELECT action FROM routine_log")[0] == "sent"
    log = one(data_dir, "SELECT * FROM google_log")
    assert log["action"] == "mail sent" and "praxis@example.com" in log["detail"] and log["account"] == "private"
    # A second press, from a page that is still open, sends nothing.
    assert client.post("/routines/1/send").status_code == 400 and len(mailbox.sent) == 1
    assert "mail sent" in client.get("/routines").text


def test_a_failed_send_shows_and_keeps_the_round_open(client, mailbox, data_dir: Path):
    client.post("/routines", data=SENDING)
    mailbox.fail = "The account 'private' may not send mail."
    assert client.post("/routines/1/send", follow_redirects=False).status_code == 303
    row = one(data_dir, "SELECT * FROM routines")
    assert row["next_due"] == "2026-10-01" and "may not send" in row["mail_error"]
    assert "The mail was not sent: The account" in client.get("/routines").text
    mailbox.fail = ""
    client.post("/routines/1/send", follow_redirects=False)
    assert len(mailbox.sent) == 1 and one(data_dir, "SELECT mail_error FROM routines")[0] is None


def test_a_link_routine_never_sends(client, mailbox):
    client.post("/routines", data=REZEPT)
    assert client.post("/routines/1/send").status_code == 400 and mailbox.sent == []
    assert "Send the mail to" not in client.get("/routines").text


def at(hour: int, day: int = 5):
    from datetime import datetime, timezone
    return datetime(2026, 10, day, hour, 30, tzinfo=timezone.utc)


def test_automatic_sending(client, mailbox, data_dir: Path):
    client.post("/routines", data={**SENDING, "mail_mode": "auto"})
    client.post("/routines", data={**SENDING, "name": "On a press"})
    client.post("/routines", data={**SENDING, "name": "Later", "mail_mode": "auto", "next_due": "2027-01-01"})
    off = {key: value for key, value in SENDING.items() if key != "enabled"}
    client.post("/routines", data={**off, "name": "Off", "mail_mode": "auto"})
    assert "sends by itself" in client.get("/routines").text
    conn = db.connect(data_dir)
    # Not at night.
    assert routines.tick(conn, mailbox, at(3)) == [] and mailbox.sent == []
    assert routines.tick(conn, mailbox, at(10)) == ["sent: Folgerezept"]
    assert [mail[2] for mail in mailbox.sent] == ["Folgerezept"]
    # The round is closed, so the next looks send nothing.
    assert routines.tick(conn, mailbox, at(11)) == [] and routines.tick(conn, mailbox, at(10, day=6)) == []
    assert len(mailbox.sent) == 1


def test_automatic_sending_never_repeats_by_itself(client, mailbox, data_dir: Path):
    client.post("/routines", data={**SENDING, "mail_mode": "auto"})
    conn = db.connect(data_dir)
    mailbox.fail = "Google answered 500"
    assert routines.tick(conn, mailbox, at(10))[0].startswith("failed: Folgerezept")
    mailbox.fail = ""
    # After a failed attempt the person decides. It could have gone out after all.
    assert routines.tick(conn, mailbox, at(11)) == [] and routines.tick(conn, mailbox, at(10, day=7)) == []
    assert mailbox.sent == []
    client.post("/routines/1/send", follow_redirects=False)
    assert len(mailbox.sent) == 1

    # Even a routine that is somehow due again right away waits most of a day.
    conn.execute("UPDATE routines SET next_due = '2026-10-01', last_sent = '2026-10-05T10:30:00.000Z'")
    conn.commit()
    assert routines.tick(conn, mailbox, at(12)) == [] and len(mailbox.sent) == 1
    assert routines.tick(conn, mailbox, at(12, day=6)) == ["sent: Folgerezept"]


def test_a_session_cannot_draft_a_sending_routine(client, data_dir: Path):
    client.post("/api/v1/routines", json={
        "name": "Folgerezept", "every": 12, "unit": "weeks", "next_due": "2026-10-01",
        "mail_to": "praxis@example.com", "mail_subject": "s", "mail_text": "t",
        "mail_mode": "auto", "mail_account": "private"})
    row = one(data_dir, "SELECT * FROM routines")
    assert (row["mail_mode"], row["mail_account"], row["enabled"]) == ("link", "", 0)


def test_the_server_clock_sends_due_mail(data_dir: Path, monkeypatch):
    """The whole path: the thread that starts with the server finds the routine and sends."""
    import time
    from datetime import datetime
    from fastapi.testclient import TestClient
    from claude_hub.server.app import create_app
    from claude_hub.server.google import zone

    monkeypatch.undo()  # this test runs on the real clock
    monkeypatch.setenv("HUB_TICK_SECONDS", "0.1")
    monkeypatch.setattr(routines, "SEND_HOURS", range(0, 24))
    app = create_app(data_dir)
    box = Mailbox()
    box.store, box.time_zone = app.state.google.store, app.state.google.time_zone
    app.state.google = box
    today = datetime.now(zone(box.time_zone)).date().isoformat()
    with TestClient(app) as client:
        client.post("/routines", data={**SENDING, "mail_mode": "auto", "next_due": today})
        for _ in range(50):
            if box.sent:
                break
            time.sleep(0.1)
    assert [mail[1] for mail in box.sent] == ["praxis@example.com"]
    assert one(data_dir, "SELECT action FROM routine_log")[0] == "sent"
