"""Rules that live in the hub: edits, proposals, exports, and the local connector."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from claude_hub.collector import cli, export, mcp_server
from claude_hub.collector.cli import run_once
from claude_hub.collector.config import Account, Config
from claude_hub.rulesets import build_briefing, load_rulesets, split_file, with_addition, with_body
from claude_hub.server import db
from claude_hub.server.app import create_app

from conftest import TOKEN

CORE = """---
title: Demo core
projects: github.com/example/demo
---
- Run the gate before a push.
"""
NET = """---
title: Demo networking
description: Transport rules.
load: on-demand
when: Before you edit the network code.
projects: github.com/example/demo
---
## Transport

All networking goes through one interface.
"""
REMOTE = "github.com/example/demo"


@pytest.fixture
def rules_dir(tmp_path: Path, app) -> Path:
    folder = tmp_path / "rulesets"
    folder.mkdir()
    (folder / "demo-core.md").write_text(CORE)
    (folder / "demo-net.md").write_text(NET)
    (folder / "README.md").write_text("# Not a rule set\n")
    app.state.rulesets_dir = folder
    return folder


@pytest.fixture
def demo(client, data_dir, rules_dir) -> int:
    """The demo project, with rules from the hub turned on."""
    client.get("/api/v1/briefing", params={"remote": REMOTE})
    conn = db.connect(data_dir)
    conn.execute("UPDATE projects SET hub_rules = 1")
    conn.commit()
    project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
    conn.close()
    return project_id


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A Git repository whose remote is the demo project."""
    folder = tmp_path / "dev" / "demo"
    folder.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/Example/Demo.git"],
                   cwd=folder, check=True, capture_output=True)
    return folder


def test_file_helpers_keep_the_frontmatter():
    head, body = split_file(CORE)
    assert head.endswith("---\n") and body == "- Run the gate before a push.\n"
    assert with_addition(CORE, "- New rule.\n\n") == CORE + "- New rule.\n"
    assert with_body(CORE, "Only this.") == head + "Only this.\n"
    assert split_file("No frontmatter") == ("", "No frontmatter")


def test_briefing_wording_follows_the_channel(rules_dir):
    sets = load_rulesets(rules_dir)
    project = {"key": f"git:{REMOTE}", "name": "demo", "workspace": None, "ai_ok": 1}
    plain = build_briefing(sets, project)["text"]
    # A collector that predates proposals gets no command it cannot fill in.
    assert "{{RULES_COMMAND}} NAME" in plain and "{{HUB_COMMAND}}" not in plain
    hook = build_briefing(sets, project, propose=True)["text"]
    assert '{{HUB_COMMAND}} rules propose SET "TEXT" --reason "WHY"' in hook
    mcp = build_briefing(sets, project, channel="mcp", propose=True)["text"]
    assert "hub_ruleset" in mcp and "hub_propose_rule" in mcp and "{{" not in mcp
    file = build_briefing(sets, project, channel="file", propose=True)["text"]
    assert ".claude-hub/rules/NAME.md" in file and "{{" not in file


def test_proposal_changes_nothing_until_it_is_accepted(client, app, data_dir, demo):
    params = {"remote": REMOTE}
    bad = client.post("/api/v1/proposals", json={"ruleset": "nope", "text": "x"})
    assert bad.status_code == 404 and "demo-core" in bad.json()["detail"]
    assert client.post("/api/v1/proposals", json={"ruleset": "demo-core", "text": " "}).status_code == 400
    assert client.post("/api/v1/proposals", json={"ruleset": "demo-core", "text": "x", "mode": "y"}).status_code == 400

    filed = client.post("/api/v1/proposals", json={
        "ruleset": "demo-core", "text": "- Never rebase a shared branch.", "remote": REMOTE,
        "reason": "A rebase lost two commits.", "source": "Demo design lane"})
    assert filed.status_code == 201 and filed.json()["status"] == "pending"
    assert "Never rebase" not in client.get("/api/v1/briefing", params=params).json()["text"]
    assert client.get("/status.json").json()["proposals"] == 1
    page = client.get("/proposals").text
    assert "Never rebase a shared branch." in page and "A rebase lost two commits." in page
    assert "Demo design lane" in page and "built-in method" not in page
    assert "1 rule proposal waits" in client.get("/rulesets").text

    # Accept with a changed wording: the changed text is what applies.
    proposal_id = filed.json()["id"]
    client.post(f"/proposals/{proposal_id}/accept", data={"text": "- Never rebase a branch that others use."})
    text = client.get("/api/v1/briefing", params=params).json()["text"]
    assert "- Run the gate before a push.\n- Never rebase a branch that others use." in text
    assert client.get("/status.json").json()["proposals"] == 0
    assert client.post(f"/proposals/{proposal_id}/accept", data={}).status_code == 404   # decided
    assert "changed in the hub" in client.get("/rulesets").text
    edits = client.get("/api/v1/ruleset-edits").json()["sets"]
    assert list(edits) == ["demo-core"] and edits["demo-core"].startswith("---\ntitle: Demo core")

    # A rejected proposal leaves the set alone.
    second = client.post("/api/v1/proposals", json={
        "ruleset": "demo-net", "mode": "replace", "text": "Use the new transport."}).json()["id"]
    assert "-All networking goes through one interface." in client.get("/proposals").text   # the diff
    client.post(f"/proposals/{second}/reject")
    assert "one interface" in client.get("/api/v1/rulesets/demo-net").json()["body"]
    decided = client.get("/proposals").text
    assert "accepted" in decided and "rejected" in decided


def test_edit_in_the_hub_wins_until_the_repository_has_the_same_text(client, app, data_dir, demo, rules_dir):
    assert client.post("/rulesets/nope/edit", data={"text": "x"}).status_code == 404
    edited = CORE.replace("before a push", "before every push")
    client.post("/rulesets/demo-core/edit", data={"text": edited.replace("\n", "\r\n")})
    assert "before every push" in client.get("/api/v1/briefing", params={"remote": REMOTE}).json()["text"]
    live = app.state.live_rulesets_dir / "demo-core.md"
    assert live.read_bytes() == edited.encode()                 # stored with plain line ends
    page = client.get("/rulesets/demo-core").text
    assert "differs from the file in the repository" in page and "Discard the change" in page

    client.post("/rulesets/demo-core/reset")
    assert not live.exists()
    assert "before a push" in client.get("/api/v1/briefing", params={"remote": REMOTE}).json()["text"]

    # After a deployment that brings the same text, the edit is no longer needed.
    client.post("/rulesets/demo-core/edit", data={"text": edited})
    (rules_dir / "demo-core.md").write_text(edited)
    assert client.get("/api/v1/ruleset-edits").json()["sets"] == {}
    from claude_hub.server.app import drop_shipped_edits
    drop_shipped_edits(rules_dir, app.state.live_rulesets_dir)
    assert not live.exists()
    assert create_app(data_dir).state.live_rulesets_dir == data_dir / "rulesets"


def config_file(tmp_path: Path, server: str) -> Path:
    path = tmp_path / "cfg" / "collector.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'server_url = "{server}"\ntoken = "{TOKEN}"\n')
    return path


def test_export_writes_the_rules_and_hides_them_from_git(tmp_path, live_server, client, data_dir, rules_dir,
                                                         checkout, capsys, monkeypatch):
    config = config_file(tmp_path, live_server)
    # Rules from the hub are off for a new project: nothing is written.
    assert cli.main(["--config", str(config), "rules", "export", str(checkout)]) == 1
    assert "serves no rules" in capsys.readouterr().out and not (checkout / ".claude-hub").exists()

    conn = db.connect(data_dir)
    conn.execute("UPDATE projects SET hub_rules = 1")
    conn.commit()
    conn.close()
    assert cli.main(["--config", str(config), "rules", "export", str(checkout)]) == 0
    assert "hidden from Git" in capsys.readouterr().out
    rules = (checkout / ".claude-hub" / "RULES.md").read_text()
    assert "Run the gate before a push." in rules and "Do not edit" in rules
    assert "`.claude-hub/rules/NAME.md`" in rules and "demo-net" in rules and "{{" not in rules
    assert "All networking goes through one interface." in (checkout / ".claude-hub/rules/demo-net.md").read_text()
    status = subprocess.run(["git", "status", "--porcelain", "--ignored"], cwd=checkout,
                            capture_output=True, text=True).stdout
    assert status.strip() == "!! .claude-hub/"                  # ignored, and nothing else changed
    assert not (checkout / ".gitignore").exists()

    # A second export changes nothing and does not repeat the exclude line.
    cli.main(["--config", str(config), "rules", "export", str(checkout)])
    assert "0 changed, already hidden" in capsys.readouterr().out
    assert (checkout / ".git/info/exclude").read_text().count("/.claude-hub/") == 1

    # A collector run refreshes an existing export, and only an existing one.
    other = checkout.parent / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True, capture_output=True)
    (rules_dir / "demo-core.md").write_text(CORE.replace("before a push", "before every push"))
    (rules_dir / "demo-net.md").unlink()
    cfg = Config(server_url=live_server, token=TOKEN, accounts=[Account("a", tmp_path / "none")],
                 scan_roots=[checkout.parent])
    run_once(cfg, sessions=False)
    assert "before every push" in (checkout / ".claude-hub" / "RULES.md").read_text()
    assert not (checkout / ".claude-hub/rules/demo-net.md").exists()   # the set is gone
    assert not (other / ".claude-hub").exists()
    assert export.hide_from_git(tmp_path) == "not a Git repository, nothing to hide"


def test_propose_and_pull_from_the_command_line(tmp_path, live_server, client, demo, checkout, capsys, monkeypatch):
    config = config_file(tmp_path, live_server)
    monkeypatch.chdir(checkout)
    assert cli.main(["--config", str(config), "rules", "propose", "demo-core", "- Tag every release.",
                     "--reason", "Releases were hard to find."]) == 0
    assert "changes nothing until Robin accepts" in capsys.readouterr().out
    assert cli.main(["--config", str(config), "rules", "propose", "nope", "x"]) == 1
    assert "refused" in capsys.readouterr().err

    # The session hook tells a session how to propose, with a command that runs here.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(checkout)})))
    cli.main(["--config", str(config), "hook", "session-start"])
    out = capsys.readouterr().out
    assert "## Changing a rule" in out and "{{" not in out
    assert 'claude_hub.collector.cli --config' in out and 'rules propose SET "TEXT"' in out

    proposal = client.get("/proposals").text
    assert "Tag every release." in proposal and "Claude Code" in proposal and "demo" in proposal
    client.post("/proposals/1/accept", data={})
    target = tmp_path / "hub" / "rulesets"
    assert cli.main(["--config", str(config), "rules", "pull", str(target)]) == 1   # not the hub repository
    capsys.readouterr()
    target.mkdir(parents=True)
    (target / "README.md").write_text("# Rule sets\n")
    assert cli.main(["--config", str(config), "rules", "pull", str(target)]) == 0
    assert "Updated demo-core.md" in capsys.readouterr().out
    assert (target / "demo-core.md").read_text() == CORE + "- Tag every release.\n"


def talk(cfg: Config, cache: Path, *messages) -> list[dict]:
    """Send messages to the local connector and return its answers."""
    stdin = io.BytesIO(b"".join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in messages))
    stdout = io.BytesIO()
    mcp_server.serve(cfg, cache, stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_local_connector_serves_rules_and_files_proposals(tmp_path, live_server, client, demo, checkout,
                                                          monkeypatch):
    cfg = Config(server_url=live_server, token=TOKEN, accounts=[])

    def call(msg_id, tool, **arguments):
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}

    answers = talk(
        cfg, tmp_path / "cache",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},        # no answer
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "hub_rules", folder=str(checkout)),
        call(4, "hub_ruleset", name="demo-net"),
        call(5, "hub_propose_rule", ruleset="demo-core", text="- Keep art at native size.",
             reason="Scaled art looked blurry.", folder=str(checkout), source="Demo design lane"),
        call(6, "hub_propose_rule", ruleset="nope", text="x", reason="y"),
        call(7, "hub_ruleset", name="nope"),
        call(8, "unknown_tool"),
        {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        b"not json",
    )
    by_id = {answer["id"]: answer for answer in answers}
    assert len(answers) == 11 and set(by_id) == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, None}
    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"] == {"tools": {}}
    assert init["serverInfo"]["name"] == "claude-hub" and "hub_rules" in init["instructions"]
    tools = {tool["name"]: tool for tool in by_id[2]["result"]["tools"]}
    assert set(tools) == {"hub_rules", "hub_ruleset", "hub_propose_rule"}
    assert all(tool["inputSchema"]["type"] == "object" and tool["description"] for tool in tools.values())

    rules = by_id[3]["result"]
    assert rules["isError"] is False and "Run the gate before a push." in rules["content"][0]["text"]
    assert "Call the tool `hub_ruleset`" in rules["content"][0]["text"] and "{{" not in rules["content"][0]["text"]
    assert "All networking goes through one interface." in by_id[4]["result"]["content"][0]["text"]
    assert by_id[5]["result"]["isError"] is False and "Proposal 1 is filed" in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["result"]["isError"] is True and by_id[7]["result"]["isError"] is True
    assert by_id[8]["result"]["isError"] is True
    assert by_id[9]["error"]["code"] == -32601 and by_id[10]["result"] == {}
    assert by_id[None]["error"]["code"] == -32700
    page = client.get("/proposals").text
    assert "Keep art at native size." in page and "Demo design lane" in page

    # A project that still keeps its own rule files says so instead of returning rules.
    conn = db.connect(client.app.state.data_dir)
    conn.execute("UPDATE projects SET hub_rules = 0")
    conn.commit()
    conn.close()
    off = talk(cfg, tmp_path / "cache", call(1, "hub_rules", folder=str(checkout)))[0]["result"]
    assert off["isError"] is False and "keeps its rules in its own files" in off["content"][0]["text"]
    down = Config(server_url="http://127.0.0.1:9", token=TOKEN, accounts=[])
    assert talk(down, tmp_path / "cache", call(1, "hub_rules", folder=str(checkout)))[0]["result"]["isError"] is True


def test_connector_install_keeps_other_servers(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    assert "added" in mcp_server.install(path, None)
    assert json.loads(path.read_text())["mcpServers"]["claude-hub"]["args"][-1] == "mcp"

    path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    message = mcp_server.install(path, tmp_path / "c.toml")
    settings = json.loads(path.read_text())
    assert "added" in message and settings["theme"] == "dark" and "other" in settings["mcpServers"]
    entry = settings["mcpServers"]["claude-hub"]
    assert entry["args"] == ["-m", "claude_hub.collector.cli", "--config", str(tmp_path / "c.toml"), "mcp"]
    assert "updated" in mcp_server.install(path, None)
    assert "removed" in mcp_server.install(path, None, remove=True)
    assert json.loads(path.read_text()) == {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    assert "no hub entry" in mcp_server.install(path, None, remove=True)
    assert path.with_name("claude_desktop_config.json.claude-hub.bak").exists()

    path.write_text("{broken")
    assert "not valid JSON" in mcp_server.install(path, None) and path.read_text() == "{broken"
