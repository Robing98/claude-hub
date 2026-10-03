import io
import json
from pathlib import Path

import pytest

from claude_hub.collector import briefing, cli
from claude_hub.collector.config import Account, Config
from claude_hub.rulesets import applies, build_briefing, load_rulesets, nest_headings, parse_ruleset
from claude_hub.server import db

from conftest import TOKEN

SHARED = """---
title: No tooling traces
description: Commits carry no sign of AI tooling.
all: true
ai: no
order: 10
---
Commit messages contain only the change description.
"""
ORBIS = """---
title: Orbis core
projects: github.com/Robing98/orbis
---
The server is always authoritative.
"""
ORBIS_NET = """---
title: Orbis networking
description: Authority and transport rules.
load: on-demand
when: Before you edit src/Net or src/Server.
projects: github.com/robing98/orbis
---
All networking goes through ITransport.
"""
GOLLE = """---
title: Shopware plugins
workspaces: GolleIT
---
Raise the version in composer.json before a merge request.
"""


@pytest.fixture
def rules_dir(tmp_path: Path, app) -> Path:
    folder = tmp_path / "rulesets"
    folder.mkdir()
    for name, text in (("no-tooling-traces", SHARED), ("orbis-core", ORBIS),
                       ("orbis-net", ORBIS_NET), ("shopware", GOLLE)):
        (folder / f"{name}.md").write_text(text)
    (folder / "README.md").write_text("# Not a rule set\n")
    app.state.rulesets_dir = folder
    return folder


def test_parse_and_scope():
    rs = parse_ruleset("orbis-net", ORBIS_NET)
    assert (rs.title, rs.load, rs.projects) == ("Orbis networking", "on-demand", ["github.com/robing98/orbis"])
    assert rs.when == "Before you edit src/Net or src/Server."
    assert rs.body == "All networking goes through ITransport."
    shared = parse_ruleset("x", SHARED)
    assert shared.all_projects and shared.ai == "no" and shared.order == 10
    assert "all projects" in shared.scope and "not allowed" in shared.scope


def test_applies(rules_dir: Path):
    sets = {rs.name: rs for rs in load_rulesets(rules_dir)}
    assert set(sets) == {"no-tooling-traces", "orbis-core", "orbis-net", "shopware"}
    orbis = {"key": "git:github.com/robing98/orbis", "name": "orbis", "workspace": "Private", "ai_ok": 0}
    hub = {"key": "git:github.com/robing98/claude-hub", "name": "claude-hub", "workspace": "Private", "ai_ok": 1}
    plugin = {"key": "git:git.fgit.de/shopware/x", "name": "x", "workspace": "GolleIT", "ai_ok": 0}

    def names(project):
        return {name for name, rs in sets.items() if applies(rs, project)}

    assert names(orbis) == {"no-tooling-traces", "orbis-core", "orbis-net"}
    assert names(hub) == set()                 # AI traces allowed, and no set of its own
    assert names(plugin) == {"no-tooling-traces", "shopware"}
    assert names(None) == {"no-tooling-traces"}  # outside any project: the strict default


def test_briefing_text(rules_dir: Path):
    orbis = {"key": "git:github.com/robing98/orbis", "name": "orbis", "workspace": "Private", "ai_ok": 0}
    result = build_briefing(load_rulesets(rules_dir), orbis)
    text = result["text"]
    assert result["always"] == ["no-tooling-traces", "orbis-core"] and result["on_demand"] == ["orbis-net"]
    assert "AI traces in Git: not allowed" in text
    assert "The server is always authoritative." in text
    # An on-demand set appears as one index line, not with its body.
    assert "`orbis-net`: Authority and transport rules. Load when: Before you edit" in text
    assert "ITransport" not in text
    assert build_briefing([], orbis)["text"] == ""


def test_briefing_api_and_project_switches(client, data_dir, rules_dir):
    params = {"cwd": "D:\\dev\\orbis", "remote": "github.com/robing98/orbis"}
    # A project keeps its own rule files until hub rules are turned on for it.
    answer = client.get("/api/v1/briefing", params=params).json()
    assert answer["project"] == "orbis" and answer["text"] == "" and answer["hub_rules"] is False

    project_id = db.connect(data_dir).execute("SELECT id FROM projects").fetchone()[0]
    assert "would start" in client.get(f"/projects/{project_id}").text
    client.post(f"/projects/{project_id}/session-rules", data={"hub_rules": "1"})
    answer = client.get("/api/v1/briefing", params=params).json()
    assert answer["always"] == ["no-tooling-traces", "orbis-core"] and answer["hub_rules"] is True

    client.post(f"/projects/{project_id}/session-rules", data={"hub_rules": "1", "ai_ok": "1"})
    assert client.get("/api/v1/briefing", params=params).json()["always"] == ["orbis-core"]
    client.post(f"/projects/{project_id}/session-rules", data={"ai_ok": "1"})
    assert client.get("/api/v1/briefing", params=params).json()["text"] == ""
    client.post(f"/projects/{project_id}/session-rules", data={"hub_rules": "1"})
    assert "no-tooling-traces" in client.get("/api/v1/briefing", params={"remote": "github.com/robing98/orbis"}).json()["always"]

    # A plain folder gets the sets for all projects only.
    loose = client.get("/api/v1/briefing", params={"cwd": "C:\\Users\\robin"}).json()
    assert loose["project"] is None and loose["always"] == ["no-tooling-traces"]

    assert client.get("/api/v1/rulesets/orbis-net").json()["body"] == "All networking goes through ITransport."
    assert client.get("/api/v1/rulesets/nope").status_code == 404
    assert client.get("/api/v1/briefing", headers={"Authorization": "Bearer wrong"}).status_code == 401

    for path in ("/rulesets", "/rulesets/orbis-net", f"/projects/{project_id}", f"/projects/{project_id}/briefing"):
        page = client.get(path)
        assert page.status_code == 200 and "built-in method" not in page.text, path
    assert "Orbis networking" in client.get("/rulesets").text
    assert client.get("/rulesets/nope").status_code == 404


def test_hook_prints_rules_and_falls_back_to_the_saved_copy(tmp_path, live_server, rules_dir, monkeypatch, capsys):
    config = tmp_path / "cfg" / "collector.toml"
    config.parent.mkdir()
    config.write_text(f'server_url = "{live_server}"\ntoken = "{TOKEN}"\n')
    payload = json.dumps({"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "SessionStart"})

    def run_hook():
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert cli.main(["--config", str(config), "hook", "session-start"]) == 0
        return capsys.readouterr().out

    first = run_hook()
    assert "Commit messages contain only the change description." in first
    assert "(The hub was not reachable" not in first

    # The hub goes away: the session still gets its rules.
    config.write_text(f'server_url = "http://127.0.0.1:9"\ntoken = "{TOKEN}"\n')
    second = run_hook()
    assert "Commit messages contain only the change description." in second
    assert "last saved copy" in second

    # A broken configuration must not break the session either.
    config.write_text("not valid toml = = =")
    assert run_hook() == ""


def test_rules_show_and_placeholder(tmp_path, live_server, rules_dir, data_dir, monkeypatch, capsys):
    config = tmp_path / "cfg" / "collector.toml"
    config.parent.mkdir()
    config.write_text(f'server_url = "{live_server}"\ntoken = "{TOKEN}"\n')
    assert cli.main(["--config", str(config), "rules", "show", "orbis-net"]) == 0
    assert "All networking goes through ITransport." in capsys.readouterr().out
    assert cli.main(["--config", str(config), "rules", "show", "nope"]) == 1
    assert "no rule set 'nope'" in capsys.readouterr().err

    cfg = Config(server_url=live_server, token=TOKEN, accounts=[Account("a", tmp_path)])
    # Ask as the Orbis repository: its briefing carries the index with a runnable command.
    monkeypatch.setattr(briefing, "resolve_dir", lambda cwd: {"remote": "github.com/robing98/orbis",
                                                             "repo_root": cwd, "main_repo": None})
    def run_hook():
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
        assert cli.main(["--config", str(config), "hook", "session-start"]) == 0
        return capsys.readouterr().out

    # Hub rules are off for a new project: the hook prints nothing.
    assert run_hook() == ""
    conn = db.connect(data_dir)
    conn.execute("UPDATE projects SET hub_rules = 1")
    conn.commit()
    conn.close()
    out = run_hook()
    assert "{{RULES_COMMAND}}" not in out
    assert "claude_hub.collector.cli" in out and "rules show NAME" in out and str(config).replace("\\", "/") in out
    assert cfg.server_url == live_server


def test_install_hook_keeps_other_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}))

    assert "installed" in briefing.install_hook(tmp_path, None)
    assert "updated" in briefing.install_hook(tmp_path, None)      # running it twice adds nothing
    data = json.loads(settings.read_text())
    starts = data["hooks"]["SessionStart"]
    assert len(starts) == 2 and starts[0]["hooks"][0]["command"] == "echo mine"
    assert starts[1]["hooks"][0]["command"].endswith("hook session-start")
    assert data["model"] == "opus" and "Stop" in data["hooks"]
    assert json.loads((tmp_path / "settings.json.claude-hub.bak").read_text())["model"] == "opus"

    assert "removed" in briefing.install_hook(tmp_path, None, remove=True)
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["SessionStart"]) == 1 and "Stop" in data["hooks"]
    assert "Nothing changed" in briefing.install_hook(tmp_path, None, remove=True)

    settings.write_text("{ broken")
    assert "Nothing changed" in briefing.install_hook(tmp_path, None)
    assert settings.read_text() == "{ broken"

    fresh = tmp_path / "new-config-dir"
    assert "installed" in briefing.install_hook(fresh, tmp_path / "custom.toml")
    command = json.loads((fresh / "settings.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--config" in command and "custom.toml" in command


def test_the_shipped_rule_sets_load():
    folder = Path(__file__).resolve().parents[1] / "rulesets"
    sets = {rs.name: rs for rs in load_rulesets(folder)}
    assert {"working-with-robin", "commit-messages", "no-tooling-traces", "parallel-work",
            "verification", "claude-hub", "orbis-core"} <= set(sets)
    for rs in sets.values():
        assert rs.title and rs.body, rs.name
        assert rs.all_projects or rs.workspaces or rs.projects, f"{rs.name} applies to nothing"
        if rs.load == "on-demand":
            assert rs.description and rs.when, f"{rs.name} needs a description and a 'when'"

    orbis = {"key": "git:github.com/robing98/orbis", "name": "orbis", "workspace": "Private", "ai_ok": 1}
    briefing_ = build_briefing(list(sets.values()), orbis)
    assert "no-tooling-traces" not in briefing_["always"]
    # The commit rule holds in every project, whatever the AI switch says.
    assert briefing_["always"][:4] == ["working-with-robin", "commit-messages", "parallel-work",
                                       "verification"]
    assert "no-tooling-traces" in build_briefing(list(sets.values()), {**orbis, "ai_ok": 0})["always"]
    assert {"orbis-core", "orbis-conventions", "orbis-done", "orbis-session"} <= set(briefing_["always"])
    # Every rule that the core lists points to a set that exists and loads on demand.
    import re
    named = set(re.findall(r"\(`(orbis-rules-[a-z]+)`\)", sets["orbis-core"].body))
    assert named and named <= set(briefing_["on_demand"])
    # No heading inside a set sits on the level of the set titles.
    titles = {f"## {sets[name].title}" for name in briefing_["always"]}
    second_level = {line for line in briefing_["text"].splitlines() if line.startswith("## ")}
    assert second_level - titles == {"## More rule sets"}


def test_headings_inside_a_set_move_below_its_title():
    body = "Intro\n## Layout\n### Detail\n```\n## not a heading\n```\n# Top"
    assert nest_headings(body).splitlines() == [
        "Intro", "#### Layout", "##### Detail", "```", "## not a heading", "```", "### Top"]
    assert nest_headings("### Already below\ntext") == "### Already below\ntext"
    assert nest_headings("No headings") == "No headings"
