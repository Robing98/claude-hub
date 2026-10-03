import base64
import gzip

from datetime import datetime, timezone
from pathlib import Path

from claude_hub.server import db, store, usage
from claude_hub.server.pricing import load_prices

from conftest import USAGE, make_lines, to_jsonl


def chunk(raw: bytes) -> str:
    return base64.b64encode(gzip.compress(raw)).decode()


def upload(client, session_id, raw, offset=0, **extra):
    body = {"offset": offset, "data": chunk(raw), "head_sha": "h1", "account": "max-1", **extra}
    return client.post(f"/api/v1/sessions/{session_id}/append", json=body)


def one(data_dir, query, *params):
    conn = db.connect(data_dir)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def test_api_requires_a_known_token(client):
    assert client.get("/api/v1/sessions/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/sessions/state", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/v1/sessions/state").json() == {"sessions": {}, "probe_dirs": []}


def test_upload_then_append(client, data_dir):
    lines = make_lines("s1", "/work/app", ["First prompt", "Second prompt"])
    first, second = to_jsonl(lines[:4]), to_jsonl(lines[4:])

    response = upload(client, "s1", first, remote="github.com/robing98/demo")
    assert response.status_code == 200 and response.json() == {"raw_bytes": len(first)}
    response = upload(client, "s1", second, offset=len(first))
    assert response.json() == {"raw_bytes": len(first) + len(second)}

    row = one(data_dir, "SELECT * FROM sessions WHERE session_id = 's1'")
    assert row["title"] == "Title of s1"
    assert row["user_prompts"] == 2
    assert row["last_event"] == "reply"
    stored = b"".join(store.read_lines(store.transcript_path(data_dir, row["machine_id"], "s1")))
    assert stored == first + second
    assert one(data_dir, "SELECT key FROM projects WHERE id = ?", row["project_id"])["key"] == \
        "git:github.com/robing98/demo"
    assert one(data_dir, "SELECT label FROM accounts WHERE id = ?", row["account_id"])["label"] == "max-1"
    assert client.get("/api/v1/sessions/state").json()["sessions"]["s1"]["raw_bytes"] == len(stored)


def test_wrong_offset_is_rejected_without_writing(client, data_dir):
    raw = to_jsonl(make_lines("s1", "/w", ["Hello"]))
    upload(client, "s1", raw)
    response = upload(client, "s1", b'{"type":"x"}\n', offset=5)
    assert response.status_code == 409 and response.json()["expected_offset"] == len(raw)
    assert one(data_dir, "SELECT raw_bytes FROM sessions")["raw_bytes"] == len(raw)


def test_rewritten_file_restarts_from_zero(client, data_dir):
    raw = to_jsonl(make_lines("s1", "/w", ["Hello"]))
    upload(client, "s1", raw)
    response = upload(client, "s1", b"x\n", offset=len(raw), head_sha="other")
    assert response.status_code == 409 and response.json()["expected_offset"] == 0

    replacement = to_jsonl(make_lines("s1", "/w", ["Rewritten"]))
    assert upload(client, "s1", replacement, head_sha="other", truncate=True).status_code == 200
    row = one(data_dir, "SELECT * FROM sessions")
    assert (row["first_prompt"], row["raw_bytes"], row["head_sha"]) == ("Rewritten", len(replacement), "other")


def test_invalid_input_is_rejected(client):
    assert upload(client, "..%2Fescape", b"x\n").status_code in (400, 404)
    assert upload(client, "bad id", b"x\n").status_code == 400
    assert client.post("/api/v1/sessions/s1/append", json={"offset": 0, "data": "not-base64!"}).status_code == 400


def test_sessions_are_separate_per_machine(client, data_dir):
    conn = db.connect(data_dir)
    from claude_hub.server.ingest import hash_token
    conn.execute("INSERT INTO machines (user_id, name, token_hash, created_at) VALUES (1, 'laptop', ?, 'x')",
                 (hash_token("laptop-token"),))
    conn.commit()
    conn.close()
    raw = to_jsonl(make_lines("s1", "/w", ["Hello"]))
    upload(client, "s1", raw)
    client.post("/api/v1/sessions/s1/append", headers={"Authorization": "Bearer laptop-token"},
                json={"offset": 0, "data": chunk(raw)})
    assert one(data_dir, "SELECT COUNT(*) AS n FROM sessions")["n"] == 2
    laptop_state = client.get("/api/v1/sessions/state", headers={"Authorization": "Bearer laptop-token"})
    assert list(laptop_state.json()["sessions"]) == ["s1"]


INVENTORY = {
    "platform": "windows",
    "repos": [{
        "path": "D:\\dev\\orbis", "remote": "github.com/robing98/orbis", "default_branch": "main",
        "worktrees": [
            {"path": "D:\\dev\\orbis", "is_main": True, "branch": "main", "upstream": "origin/main",
             "ahead": 0, "behind": 0, "last_commit_at": "2026-10-01T09:00:00Z", "merged": True},
            {"path": "D:\\dev\\orbis\\.claude\\worktrees\\caves", "branch": "caves", "dirty_files": 4,
             "last_commit_at": "2026-09-30T09:00:00Z"},
            {"path": "D:\\dev\\orbis\\.claude\\worktrees\\done", "branch": "done", "merged": True,
             "last_commit_at": "2026-08-01T09:00:00Z"},
        ],
    }],
}


def test_inventory_and_session_in_deleted_worktree(client, data_dir):
    # The worktree folder is gone, so the collector sends no remote for this session.
    raw = to_jsonl(make_lines("s1", "D:\\dev\\orbis\\.claude\\worktrees\\gone", ["Old work"]))
    upload(client, "s1", raw)
    assert one(data_dir, "SELECT kind FROM projects")["kind"] == "dir"

    assert client.put("/api/v1/inventory", json=INVENTORY).json() == {"repos": 1, "worktrees": 3}
    project = one(data_dir, "SELECT p.* FROM sessions s JOIN projects p ON p.id = s.project_id")
    assert project["key"] == "git:github.com/robing98/orbis"
    assert one(data_dir, "SELECT COUNT(*) AS n FROM projects")["n"] == 1

    # A second report replaces the first instead of adding to it.
    client.put("/api/v1/inventory", json=INVENTORY)
    assert one(data_dir, "SELECT COUNT(*) AS n FROM worktrees")["n"] == 3


def test_rules_sort_projects_and_manual_choice_wins(client, data_dir):
    client.post("/settings/workspaces", data={"name": "Private"})
    client.post("/settings/workspaces", data={"name": "Work"})
    client.post("/settings/classes", data={"workspace_id": 1, "name": "Games"})
    client.post("/settings/rules", data={"field": "remote", "pattern": "github.com/Robing98/*", "target": "1:1"})
    client.post("/settings/rules", data={"field": "path", "pattern": "D:/uni/*", "target": "2"})

    client.put("/api/v1/inventory", json=INVENTORY)
    upload(client, "s2", to_jsonl(make_lines("s2", "D:\\Uni\\thesis", ["Outline"])))
    upload(client, "s3", to_jsonl(make_lines("s3", "C:\\Users\\robin", ["Random question"])))

    def placement(key):
        row = one(data_dir, "SELECT workspace_id, class_id, assigned_by FROM projects WHERE key = ?", key)
        return tuple(row)

    assert placement("git:github.com/robing98/orbis") == (1, 1, "rule")
    assert placement("dir:desktop:d:/uni/thesis") == (2, None, "rule")
    assert placement("dir:desktop:c:/users/robin") == (None, None, None)

    orbis = one(data_dir, "SELECT id FROM projects WHERE key LIKE 'git:%'")["id"]
    client.post(f"/projects/{orbis}/assign", data={"target": "2"})
    client.post("/settings/rules/apply")
    assert placement("git:github.com/robing98/orbis") == (2, None, "manual")


def test_owner_rule_from_inbox(client, data_dir):
    client.post("/settings/workspaces", data={"name": "Private"})
    client.put("/api/v1/inventory", json=INVENTORY)
    other = {"repos": [{"path": "D:\\dev\\stats", "remote": "github.com/robing98/stats", "worktrees": []}]}
    client.post(f"/projects/1/assign", data={"target": "1", "owner_rule": "1"})
    client.put("/api/v1/inventory", json={"repos": INVENTORY["repos"] + other["repos"]})
    row = one(data_dir, "SELECT workspace_id, assigned_by FROM projects WHERE key = 'git:github.com/robing98/stats'")
    assert tuple(row) == (1, "rule")
    assert one(data_dir, "SELECT pattern FROM rules")["pattern"] == "github.com/robing98/*"


def test_pages_render(client, data_dir):
    client.post("/settings/workspaces", data={"name": "Private"})
    client.put("/api/v1/inventory", json=INVENTORY)
    cwd = "D:\\dev\\orbis\\.claude\\worktrees\\caves"
    upload(client, "s1", to_jsonl(make_lines("s1", cwd, ["Dig <b>caves</b> & tunnels"], branch="caves")))
    client.post("/projects/1/assign", data={"target": "1"})

    for path in ("/", "/overview", "/inbox", "/worktrees", "/worktrees?state=dirty", "/sessions",
                 "/sessions?q=caves", "/settings", "/projects/1", "/sessions/1", "/rules", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "built-in method" not in response.text and "bound method" not in response.text, path

    project = client.get("/projects/1").text
    assert "caves" in project and "dirty" in project and "Title of s1" in project
    session = client.get("/sessions/1").text
    # Transcript text is user-controlled, so it must arrive escaped.
    assert "Dig &lt;b&gt;caves&lt;/b&gt; &amp; tunnels" in session and "<b>caves</b>" not in session
    assert client.get("/projects/999").status_code == 404
    assert client.get("/sessions/999").status_code == 404


def test_now_page_groups_sessions(client, monkeypatch):
    from datetime import datetime, timezone

    from claude_hub.server import views

    monkeypatch.setattr(views, "utcnow", lambda: datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc))
    upload(client, "done", to_jsonl(make_lines("done", "/w/a", ["Finished turn"])))
    upload(client, "busy", to_jsonl(make_lines("busy", "/w/b", ["Stuck turn"], finish_turn=False)))
    page = client.get("/").text
    waiting, _, rest = page.partition("Stopped mid-turn")
    assert "Title of done" in waiting and "Title of busy" not in waiting
    assert "Title of busy" in rest

    status = client.get("/status.json").json()
    assert (status["waiting"], status["paused"], status["running"], status["active"]) == (1, 1, 0, 2)
    assert status["headline"] == "1 waiting for you, 1 stopped mid-turn"
    assert status["detail"] == "a: Title of done · b: Title of busy"
    assert [s["status"] for s in status["sessions"]] == ["waiting", "paused"]
    # Three hours later, the old reply no longer counts as open in the feed.
    monkeypatch.setattr(views, "utcnow", lambda: datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc))
    assert client.get("/status.json").json()["headline"] == "Nothing open"


def test_ui_password(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from claude_hub.server.app import create_app

    monkeypatch.setenv("HUB_UI_PASSWORD", "secret")
    with TestClient(create_app(data_dir)) as protected:
        assert protected.get("/").status_code == 401
        assert protected.get("/", auth=("hub", "wrong")).status_code == 401
        assert protected.get("/", auth=("hub", "secret")).status_code == 200
        assert protected.get("/healthz").status_code == 200
        # Collectors use their token, not the page password.
        assert protected.get("/api/v1/sessions/state", headers={"Authorization": "Bearer test-token"}).status_code == 200


def test_leftover_bytes_from_a_failed_commit_are_cut_off(client, data_dir):
    lines = make_lines("s1", "/w", ["One", "Two"])
    first, second = to_jsonl(lines[:4]), to_jsonl(lines[4:])
    upload(client, "s1", first)
    # Simulate an upload that reached the file but not the database.
    path = store.transcript_path(data_dir, 1, "s1")
    with open(path, "ab") as handle:
        handle.write(gzip.compress(b'{"type": "ghost"}\n'))
    upload(client, "s1", second, offset=len(first))
    assert b"".join(store.read_lines(path)) == first + second


def test_migrations_run_once(tmp_path):
    db.init(tmp_path)
    db.init(tmp_path)
    conn = db.connect(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()


RULES = {"sources": [
    {"scope": "user", "account": "max-1", "base_path": "C:\\Users\\robin\\.claude", "files": [
        {"rel_path": "CLAUDE.md", "kind": "claude_md", "content": "# Commits\nNo attribution.\n# Style\nShort."},
        {"rel_path": "skills/qa-run/SKILL.md", "kind": "skill",
         "content": "---\nname: qa-run\ndescription: Manual QA rounds\n---\nPlan, test, <b>report</b>."},
    ]},
    {"scope": "project", "base_path": "D:\\dev\\orbis", "remote": "github.com/robing98/orbis", "files": [
        {"rel_path": "CLAUDE.md", "kind": "claude_md", "content": "# Commits\nNo  attribution.\n# Godot\nUse C#."},
        {"rel_path": ".claude/skills/qa-run/SKILL.md", "kind": "skill",
         "content": "---\nname: qa-run\n---\nPlan and test only."},
    ]},
]}


def test_rules_import_and_pages(client, data_dir):
    assert client.put("/api/v1/rules", json=RULES).json() == {"files": 4, "items": 6}
    # A second report replaces the first.
    assert client.put("/api/v1/rules", json=RULES).json() == {"files": 4, "items": 6}
    assert one(data_dir, "SELECT COUNT(*) AS n FROM rule_files")["n"] == 4
    skill = one(data_dir, "SELECT * FROM rule_files WHERE kind = 'skill' AND scope = 'user'")
    assert (skill["name"], skill["description"]) == ("qa-run", "Manual QA rounds")
    project = one(data_dir, "SELECT p.key FROM rule_files f JOIN projects p ON p.id = f.project_id LIMIT 1")
    assert project["key"] == "git:github.com/robing98/orbis"

    listing = client.get("/rules").text
    assert "4 files with 6 single rules" in listing and "1 differ" in listing
    # A template that reads a dict key named like a dict method prints the method.
    assert "built-in method" not in listing and '<td class="num">2</td>' in listing
    assert "1 differ" not in client.get("/rules?kind=claude_md").text
    assert "Godot" not in client.get("/rules?q=attribution").text  # search lists files, not text

    page = client.get(f"/rules/{skill['id']}").text
    assert "differs" in page and "&lt;b&gt;report&lt;/b&gt;" in page
    user_md = one(data_dir, "SELECT id FROM rule_files WHERE kind = 'claude_md' AND scope = 'user'")["id"]
    page = client.get(f"/rules/{user_md}").text
    # "Commits" has the same text in the project file, apart from spacing.
    assert page.count("Same text in:") == 1 and "orbis / CLAUDE.md" in page
    assert client.get("/rules/999").status_code == 404
    assert client.put("/api/v1/rules", json={"sources": [{"scope": "odd", "base_path": "x"}]}).status_code == 400


def test_existing_database_gets_new_tables(tmp_path):
    # A database created by the first release has only the first migration.
    conn = db.connect(tmp_path)
    conn.executescript(db.MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO users (name) VALUES ('kept')")
    conn.commit()
    conn.close()
    db.init(tmp_path)
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT name FROM users").fetchone()[0] == "kept"
    assert conn.execute("SELECT COUNT(*) FROM rule_files").fetchone()[0] == 0
    conn.close()


def tool_line(session_id, cwd, file_path, index):
    return {"type": "assistant", "sessionId": session_id, "cwd": cwd, "uuid": f"t{index}",
            "timestamp": f"2026-10-01T11:00:0{index}.000Z", "isSidechain": False,
            "message": {"id": f"tm{index}", "model": "claude-test", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": f"tt{index}", "name": "Read",
                                     "input": {"file_path": file_path}}]}}


def project_key(data_dir, session_id):
    return one(data_dir, "SELECT p.key FROM sessions s JOIN projects p ON p.id = s.project_id "
                         "WHERE s.session_id = ?", session_id)["key"]


def test_session_started_in_a_drive_root_goes_to_the_repository_it_worked_in(client, data_dir):
    lines = make_lines("s1", "D:\\", ["Work on orbis"])
    lines += [tool_line("s1", "D:\\", f"D:\\dev\\orbis\\src\\File{i}.cs", i) for i in range(3)]
    idle = make_lines("s2", "D:\\", ["Just a question"])

    # Before any inventory, both sessions can only be filed under the folder.
    upload(client, "s1", to_jsonl(lines))
    upload(client, "s2", to_jsonl(idle))
    assert project_key(data_dir, "s1") == project_key(data_dir, "s2") == "dir:desktop:d:"

    client.put("/api/v1/inventory", json=INVENTORY)
    assert project_key(data_dir, "s1") == "git:github.com/robing98/orbis"
    assert project_key(data_dir, "s2") == "dir:desktop:d:"

    # With the inventory known, a new session is assigned at upload.
    upload(client, "s3", to_jsonl([dict(line, sessionId="s3") for line in lines]))
    assert project_key(data_dir, "s3") == "git:github.com/robing98/orbis"


def test_one_stray_file_does_not_move_a_session(client, data_dir):
    client.put("/api/v1/inventory", json=INVENTORY)
    lines = make_lines("s1", "C:\\Users\\robin", ["Question"])
    lines.append(tool_line("s1", "C:\\Users\\robin", "D:\\dev\\orbis\\README.md", 0))
    upload(client, "s1", to_jsonl(lines))
    assert project_key(data_dir, "s1") == "dir:desktop:c:/users/robin"


def test_removed_worktree_joins_the_folder_of_its_repository(client, data_dir):
    base = "D:\\golleit\\marketing suite"
    upload(client, "s1", to_jsonl(make_lines("s1", base, ["Main work"])))
    upload(client, "s2", to_jsonl(make_lines("s2", base + "\\.claude\\worktrees\\frosty-1", ["Side work"])))
    assert project_key(data_dir, "s1") == project_key(data_dir, "s2") == "dir:desktop:d:/golleit/marketing suite"


def test_reparse_outdated_updates_old_sessions(client, data_dir, capsys):
    from claude_hub.server import cli
    from claude_hub.transcript import PARSER_VERSION

    lines = make_lines("s1", "D:\\", ["Work on orbis"])
    lines += [tool_line("s1", "D:\\", f"D:\\dev\\orbis\\src\\File{i}.cs", i) for i in range(3)]
    upload(client, "s1", to_jsonl(lines))
    client.put("/api/v1/inventory", json={"repos": []})
    # Simulate a session that an older release stored: no work folders yet.
    conn = db.connect(data_dir)
    conn.execute("UPDATE sessions SET work_dirs = NULL, parser_version = 1")
    conn.execute("INSERT INTO repos (machine_id, project_id, path, seen_at) VALUES (1, ?, 'D:\\dev\\orbis', 'x')",
                 (conn.execute("INSERT INTO projects (key, kind, name, created_at) "
                               "VALUES ('git:github.com/robing98/orbis', 'repo', 'orbis', 'x')").lastrowid,))
    conn.commit()
    conn.close()

    assert cli.main(["--data-dir", str(data_dir), "reparse", "--outdated"]) == 0
    assert "Re-parsed 1 sessions, moved 1" in capsys.readouterr().out
    assert project_key(data_dir, "s1") == "git:github.com/robing98/orbis"
    assert one(data_dir, "SELECT parser_version FROM sessions")["parser_version"] == PARSER_VERSION

    assert cli.main(["--data-dir", str(data_dir), "reparse", "--outdated"]) == 0
    assert "Re-parsed 0 sessions" in capsys.readouterr().out


PRICES = """
read_on = "2026-10-03"
plan_usd_per_month = 100
[models."claude-opus-4"]
input = 15.0
output = 75.0
[models."claude-opus-4-5"]
input = 5.0
cache_write_5m = 6.25
cache_write_1h = 10.0
cache_read = 0.5
output = 25.0
"""


def test_prices_match_the_longest_prefix_and_fill_cache_prices(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text(PRICES)
    prices = load_prices(path)
    assert prices.rates("claude-opus-4-5-20251101")["output"] == 25.0
    assert prices.rates("claude-opus-4-20250514") == {
        "input": 15.0, "output": 75.0, "cache_write_5m": 18.75, "cache_write_1h": 30.0, "cache_read": 1.5}
    assert prices.rates("claude-test") is None and prices.cost("claude-test", {"input": 5}) is None
    counts = {"input": 1_000_000, "output": 1_000_000, "cache_write_5m": 0,
              "cache_write_1h": 1_000_000, "cache_read": 2_000_000}
    assert prices.cost("claude-opus-4-5", counts) == 5.0 + 25.0 + 10.0 + 1.0

    assert load_prices(tmp_path / "absent.toml").error
    path.write_text("not = = toml")
    assert load_prices(path).error and load_prices(path).models == {}

    shipped = load_prices(Path(__file__).resolve().parents[1] / "pricing.toml")
    assert not shipped.error and shipped.read_on
    assert shipped.rates("claude-opus-5-5")["output"] == 20.0
    assert shipped.rates("claude-haiku-4-5-20251001")["input"] == 1.0
    for name, rates in shipped.models.items():
        assert set(rates) == {"input", "output", "cache_write_5m", "cache_write_1h", "cache_read"}, name


def test_usage_goes_to_the_project_and_subagents_to_their_parent(client, app, data_dir, tmp_path,
                                                               monkeypatch):
    def as_model(lines, model, sidechain=False):
        for line in lines:
            if line.get("type") == "assistant":
                line["message"]["model"] = model
            line["isSidechain"] = sidechain
        return lines

    remote = "github.com/robing98/demo"
    upload(client, "s1", to_jsonl(as_model(make_lines("s1", "/w", ["One", "Two"]), "claude-opus-4-5")),
           remote=remote)
    sub = to_jsonl(as_model(make_lines("s1", "/w", ["Search"]), "claude-opus-4-5", sidechain=True))
    assert upload(client, "s1.agent-a", sub, parent="s1").status_code == 200
    assert upload(client, "s1.agent-b", sub, parent="../x").status_code == 400
    upload(client, "s2", to_jsonl(as_model(make_lines("s2", "/tmp/x", ["Loose"]), "claude-test")))

    (tmp_path / "pricing.toml").write_text(PRICES)
    app.state.pricing_file = tmp_path / "pricing.toml"
    prices = load_prices(app.state.pricing_file)
    per_reply = prices.cost("claude-opus-4-5", {**{k: 0 for k in ("cache_write_5m",)},
                                                "input": USAGE["input_tokens"],
                                                "output": USAGE["output_tokens"],
                                                "cache_write_1h": 30, "cache_read": 40})

    conn = db.connect(data_dir)
    report = usage.report(conn, prices, 0)
    by_name = {item["name"]: item for item in report["projects"]}
    demo = by_name["demo"]
    # Two replies of the session and one of its subagent.
    assert demo["messages"] == 3 and demo["output"] == 60 and len(demo["sessions"]) == 1
    assert abs(demo["cost"] - 3 * per_reply) < 1e-12 and demo["unpriced"] == 0
    loose = by_name["x"]
    assert loose["cost"] == 0 and loose["unpriced"] == 100
    assert report["total"]["tokens"] == 400 and report["total"]["unpriced"] == 100
    assert abs(sum(item["share"] for item in report["projects"]) - 100) < 1e-9
    assert [item["name"] for item in report["models"]] == ["claude-opus-4-5", "claude-test"]
    assert report["days"][0]["day"] == "2026-10-01" and abs(report["days"][0]["bar"] - 100) < 1e-9

    pk = conn.execute("SELECT pk FROM sessions WHERE session_id = 's1'").fetchone()[0]
    spent = usage.for_session(conn, prices, pk)
    assert spent["messages"] == 3 and spent["subagents"]["messages"] == 1
    # The sessions are from October 1. Seen from October 20, they fall into
    # the last 30 days but not into the last 7.
    monkeypatch.setattr(usage, "utcnow", lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert usage.report(conn, prices, 7)["total"]["tokens"] == 0
    assert usage.report(conn, prices, 30)["total"]["tokens"] == 400
    assert usage.for_project(conn, prices, demo["id"], 7)["tokens"] == 0
    monkeypatch.undo()
    conn.close()

    page = client.get("/usage?days=0")
    assert page.status_code == 200 and "built-in method" not in page.text
    assert "demo" in page.text and "no price" in page.text and "without a price" in page.text
    assert client.get("/usage").status_code == 200 and client.get("/usage?days=5").status_code == 200
    # A subagent transcript is not a session in the lists.
    listing = client.get("/sessions").text
    assert "Title of s1" in listing and "agent-a" not in listing
    assert "Of that, subagents" in client.get(f"/sessions/{pk}").text
    assert "s1.agent-a" in client.get("/api/v1/sessions/state").json()["sessions"]

    # An upload that replaces a transcript replaces its usage as well.
    upload(client, "s1", to_jsonl(as_model(make_lines("s1", "/w", ["One"]), "claude-opus-4-5")),
           remote=remote, truncate=True)
    assert one(data_dir, "SELECT SUM(messages) AS n FROM usage_daily WHERE session_pk = ?", pk)["n"] == 1


def project_id(data_dir, name):
    return one(data_dir, "SELECT id FROM projects WHERE name = ?", name)["id"]


def test_pinned_projects_lead_in_the_given_order(client, data_dir):
    client.post("/settings/workspaces", data={"name": "Private"})
    for index, name in enumerate(("alpha", "beta", "gamma")):
        upload(client, f"s-{name}", to_jsonl(make_lines(f"s-{name}", "/w", ["Hi"], start=f"2026-10-0{index + 1}T10:00:0")),
               remote=f"github.com/robing98/{name}")
        client.post(f"/projects/{project_id(data_dir, name)}/assign", data={"target": "1"})

    def order():
        page = client.get("/overview").text
        return sorted(("alpha", "beta", "gamma"), key=lambda name: page.index(f">{name}</a>"))

    assert order() == ["gamma", "beta", "alpha"]          # by last activity
    client.post(f"/projects/{project_id(data_dir, 'alpha')}/pin", data={"action": "pin"})
    client.post(f"/projects/{project_id(data_dir, 'beta')}/pin", data={"action": "pin"})
    assert order() == ["alpha", "beta", "gamma"]          # pinned first, in pin order
    client.post(f"/projects/{project_id(data_dir, 'beta')}/pin", data={"action": "up"})
    assert order() == ["beta", "alpha", "gamma"]
    client.post(f"/projects/{project_id(data_dir, 'beta')}/pin", data={"action": "up"})   # already first
    client.post(f"/projects/{project_id(data_dir, 'beta')}/pin", data={"action": "down"})
    assert order() == ["alpha", "beta", "gamma"]
    client.post(f"/projects/{project_id(data_dir, 'alpha')}/pin", data={"action": "unpin"})
    assert order() == ["beta", "gamma", "alpha"]
    assert client.post(f"/projects/{project_id(data_dir, 'alpha')}/pin", data={"action": "x"}).status_code == 400


def test_archived_project_leaves_the_lists_and_keeps_its_usage(client, app, data_dir, monkeypatch):
    from claude_hub.server import views

    monkeypatch.setattr(views, "utcnow", lambda: datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc))
    upload(client, "s1", to_jsonl(make_lines("s1", "/w", ["Old work"])), remote="github.com/robing98/old")
    upload(client, "s2", to_jsonl(make_lines("s2", "/w", ["New work"])), remote="github.com/robing98/new")
    old = project_id(data_dir, "old")
    assert client.get("/status.json").json()["waiting"] == 2 and "Title of s1" in client.get("/").text

    client.post(f"/projects/{old}/archive", data={"archived": "1"})
    assert client.get("/status.json").json()["waiting"] == 1
    assert "Title of s1" not in client.get("/").text
    inbox = client.get("/inbox").text
    assert ">new</a>" in inbox and ">old</a>" not in inbox
    assert "/projects/%d\"" % old not in client.get("/rulesets").text
    overview = client.get("/overview").text
    assert "Archived projects" in overview and ">old</a>" in overview
    # The usage of an archived project stays in the totals, as one row.
    conn = db.connect(data_dir)
    report = usage.report(conn, load_prices(None), 0)
    conn.close()
    assert {item["name"] for item in report["projects"]} == {"new", "Archived projects (1)"}
    assert report["total"]["messages"] == 2
    assert "archived" in client.get(f"/projects/{old}").text
    assert client.get("/usage?days=0").status_code == 200

    client.post(f"/projects/{old}/archive", data={})
    assert client.get("/status.json").json()["waiting"] == 2


def test_merged_project_takes_everything_and_later_uploads(client, data_dir):
    upload(client, "s1", to_jsonl(make_lines("s1", "/w", ["One"])), remote="github.com/robing98/scheduler")
    upload(client, "s2", to_jsonl(make_lines("s2", "/w", ["Two"])), remote="github.com/robing98/scheduler-old")
    client.put("/api/v1/inventory", json={"repos": [
        {"path": "/w/old", "remote": "github.com/robing98/scheduler-old",
         "worktrees": [{"path": "/w/old", "is_main": True, "branch": "main"}]}]})
    keep, gone = project_id(data_dir, "scheduler"), project_id(data_dir, "scheduler-old")

    assert client.post(f"/projects/{gone}/merge", data={"target": str(gone)}).status_code == 400
    response = client.post(f"/projects/{gone}/merge", data={"target": str(keep)}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/projects/{keep}"
    assert client.get(f"/projects/{gone}").status_code == 404
    assert one(data_dir, "SELECT COUNT(*) AS n FROM sessions WHERE project_id = ?", keep)["n"] == 2
    assert one(data_dir, "SELECT project_id FROM repos")["project_id"] == keep
    page = client.get(f"/projects/{keep}").text
    assert "Merged in: scheduler-old" in page and "built-in method" not in page

    # A later upload and a later inventory for the old remote land in the target.
    upload(client, "s3", to_jsonl(make_lines("s3", "/w", ["Three"])), remote="github.com/robing98/scheduler-old")
    client.put("/api/v1/inventory", json={"repos": [{"path": "/w/old", "remote": "github.com/robing98/scheduler-old"}]})
    assert one(data_dir, "SELECT COUNT(*) AS n FROM projects")["n"] == 1
    assert one(data_dir, "SELECT project_id FROM sessions WHERE session_id = 's3'")["project_id"] == keep
    # A rule set that names the old project applies to the target.
    from claude_hub.rulesets import applies, parse_ruleset
    from claude_hub.server.ingest import project_view
    conn = db.connect(data_dir)
    assert applies(parse_ruleset("x", "---\nprojects: github.com/robing98/scheduler-old\n---\nRule"),
                   project_view(conn, keep))
    conn.close()

    # Separated again: the next upload creates the old project again.
    client.post(f"/projects/{keep}/aliases/delete", data={"key": "git:github.com/robing98/scheduler-old"})
    upload(client, "s4", to_jsonl(make_lines("s4", "/w", ["Four"])), remote="github.com/robing98/scheduler-old")
    assert one(data_dir, "SELECT COUNT(*) AS n FROM projects")["n"] == 2


def test_session_moved_by_hand_stays_where_it_was_put(client, data_dir):
    lines = make_lines("s1", "/w", ["First", "Second"])
    upload(client, "s1", to_jsonl(lines[:4]), remote="github.com/robing98/auto")
    upload(client, "s2", to_jsonl(make_lines("s2", "/w", ["Other"])), remote="github.com/robing98/chosen")
    auto, chosen = project_id(data_dir, "auto"), project_id(data_dir, "chosen")
    pk = one(data_dir, "SELECT pk FROM sessions WHERE session_id = 's1'")["pk"]

    assert client.post(f"/sessions/{pk}/project", data={"project": "999"}).status_code == 400
    client.post(f"/sessions/{pk}/project", data={"project": str(chosen)})
    assert "set by hand" in client.get(f"/sessions/{pk}").text
    # The session continues with its old remote. The choice made by hand wins.
    first = to_jsonl(lines[:4])
    upload(client, "s1", to_jsonl(lines[4:]), offset=len(first), remote="github.com/robing98/auto")
    assert one(data_dir, "SELECT project_id FROM sessions WHERE pk = ?", pk)["project_id"] == chosen
    # Its usage counts for the chosen project.
    conn = db.connect(data_dir)
    assert usage.for_project(conn, load_prices(None), chosen, 0)["messages"] == 3
    assert usage.for_project(conn, load_prices(None), auto, 0)["messages"] == 0
    conn.close()

    # Back to automatic: the next upload assigns it by its remote again.
    client.post(f"/sessions/{pk}/project", data={})
    upload(client, "s1", to_jsonl(lines), remote="github.com/robing98/auto", truncate=True)
    assert one(data_dir, "SELECT project_id FROM sessions WHERE pk = ?", pk)["project_id"] == auto


HIDDEN_ROW = "(hidden)"


def test_private_projects_are_masked_while_the_switch_is_on(client, data_dir):
    client.post("/settings/workspaces", data={"name": "GolleIT"})
    client.put("/api/v1/inventory", json={"repos": [
        {"path": "D:\\work\\secret-shop", "remote": "git.example.com/client/secret-shop",
         "worktrees": [{"path": "D:\\work\\secret-shop", "is_main": True, "branch": "feature/coupon-engine",
                        "last_commit_subject": "Add the coupon engine", "dirty_files": 2}]},
        {"path": "D:\\dev\\open", "remote": "github.com/robing98/open",
         "worktrees": [{"path": "D:\\dev\\open", "is_main": True, "branch": "main"}]}]})
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    upload(client, "s-secret", to_jsonl(make_lines("s-secret", "D:\\work\\secret-shop", ["Build coupons"], start=now[:-1])),
           remote="git.example.com/client/secret-shop")
    upload(client, "s-open", to_jsonl(make_lines("s-open", "D:\\dev\\open", ["Open work"], start=now[:-1])),
           remote="github.com/robing98/open")
    client.put("/api/v1/rules", json={"sources": [{"scope": "project", "base_path": "D:\\work\\secret-shop",
        "remote": "git.example.com/client/secret-shop",
        "files": [{"rel_path": ".claude/skills/coupon-release/SKILL.md", "kind": "skill",
                   "content": "---\nname: coupon-release\ndescription: Release the coupon engine\n---\nSteps"}]}]})
    secret, visible = project_id(data_dir, "secret-shop"), project_id(data_dir, "open")
    client.post(f"/projects/{secret}/assign", data={"target": "1"})
    client.post("/api/v1/proposals", json={"ruleset": "x", "text": "y"})      # no sets here, ignored

    pages = ("/", "/overview", "/sessions", "/worktrees", "/usage?days=0",
             "/rulesets", "/rules", "/inbox", f"/projects/{visible}", "/sessions/2")
    leaks = ("secret-shop", "coupon", "Title of s-secret", "git.example.com")

    # Nothing is private yet: no switch, real names.
    assert "Hide private projects" not in client.get("/").text
    assert "secret-shop" in client.get("/overview").text

    client.post(f"/projects/{secret}/private", data={"private": "1"})
    # Marked, but the switch is off: still the real names, and the switch appears.
    home = client.get("/overview").text
    assert "secret-shop" in home and "Hide private projects: off" in home

    assert client.post("/privacy", data={"hide": "1"}, follow_redirects=False).status_code == 303
    for path in pages:
        text = client.get(path).text
        assert client.get(path).status_code == 200, path
        for leak in leaks:
            assert leak not in text, (path, leak)
        assert "Hide private projects: on" in text, path
    # A search must not find what is hidden. The page repeats the search word, so
    # the check is that no row comes back.
    found = client.get("/sessions?q=coupons").text
    assert "Title of s-secret" not in found and HIDDEN_ROW not in found and "No sessions." in found
    files = client.get("/rules?q=coupon").text
    assert "coupon-release" not in files and "git.example.com" not in files and HIDDEN_ROW not in files
    assert "Project 1" in client.get("/overview").text and "Project 1" in client.get("/usage?days=0").text
    assert "Project 1" in client.get("/worktrees").text
    assert ">open</a>" in client.get("/inbox").text          # a project that is not private keeps its name
    assert "Title of s-open" in client.get("/sessions").text
    # The page of the project itself keeps the real name.
    own = client.get(f"/projects/{secret}").text
    assert "secret-shop" in own and "private" in own and "No longer private" in own

    feed = client.get("/status.json").json()
    assert "secret" not in str(feed) and "Project 1: (hidden)" in feed["detail"]

    # Switch off: everything is back. The feed can still ask for the masked form.
    client.post("/privacy", data={})
    assert "secret-shop" in client.get("/overview").text and "Title of s-secret" in client.get("/sessions").text
    assert "secret-shop" in client.get("/status.json").json()["detail"]
    assert "secret" not in str(client.get("/status.json?hide=1").json())
    client.post(f"/projects/{secret}/private", data={})
    assert "Hide private projects" not in client.get("/").text


def cowork_lines(session_id, reads=(), cds=()):
    """A Cowork transcript: it starts in its own folder and touches the given paths."""
    cwd = f"C:\\Users\\robin\\AppData\\Roaming\\Claude\\local-agent-mode-sessions\\a1\\o1\\local_{session_id}\\outputs"
    lines = make_lines(session_id, cwd, ["Do the task"])
    for index, path in enumerate(reads):
        lines.append(tool_line(session_id, cwd, path, index))
    for index, command in enumerate(cds):
        lines.append({"type": "assistant", "sessionId": session_id, "cwd": cwd, "uuid": f"c{index}",
                      "timestamp": f"2026-10-01T12:00:0{index}.000Z", "isSidechain": False,
                      "message": {"id": f"cm{index}", "model": "claude-test", "stop_reason": "tool_use",
                                  "content": [{"type": "tool_use", "id": f"ct{index}", "name": "Bash",
                                               "input": {"command": command}}]}})
    return to_jsonl(lines)


def test_cowork_sessions_find_their_repository_or_folder(client, data_dir):
    client.put("/api/v1/inventory", json={"repos": [
        {"path": "D:\\WIP\\swutsch", "remote": "github.com/robing98/swutsch",
         "worktrees": [{"path": "D:\\WIP\\swutsch", "is_main": True, "branch": "main"}]}]})

    # The connected folder is a parent of the repository. Shell commands name it by the sandbox path.
    upload(client, "cw-parent", cowork_lines("cw-parent", reads=["D:\\WIP\\swutsch\\src\\a.php"],
           cds=["cd /sessions/busy-fox/mnt/WIP/swutsch/src && ls", "cd /sessions/busy-fox/mnt/WIP/swutsch/src && git status"]))
    assert project_key(data_dir, "cw-parent") == "git:github.com/robing98/swutsch"

    # A connected folder that is no repository becomes a project of its own.
    upload(client, "cw-uni", cowork_lines("cw-uni",
           reads=["C:\\Users\\robin\\OneDrive\\Uni\\BPP und BA\\notes\\a.md", "C:\\Users\\robin\\OneDrive\\Uni\\BPP und BA\\notes\\b.md"],
           cds=['cd "/sessions/busy-fox/mnt/BPP und BA/notes" && ls']))
    assert project_key(data_dir, "cw-uni") == "dir:desktop:c:/users/robin/onedrive/uni/bpp und ba"
    assert one(data_dir, "SELECT name FROM projects WHERE key LIKE '%bpp und ba'")["name"] == "BPP und BA"

    # Without a sandbox path, the first folder below OneDrive counts.
    upload(client, "cw-vault", cowork_lines("cw-vault", reads=[
        f"C:\\Users\\robin\\OneDrive\\Obisidian Vault\\Daily\\{day}.md" for day in ("mon", "tue", "wed")]))
    assert project_key(data_dir, "cw-vault") == "dir:desktop:c:/users/robin/onedrive/obisidian vault"

    # A session that touched only its own folder and application data stays in the shared project.
    upload(client, "cw-none", cowork_lines("cw-none", reads=[
        "C:\\Users\\robin\\AppData\\Roaming\\Claude\\local-agent-mode-sessions\\a1\\o1\\local_cw-none\\outputs\\report.md",
        "C:\\Users\\robin\\AppData\\Local\\Temp\\x\\a.txt", "C:\\Users\\robin\\AppData\\Local\\Temp\\x\\b.txt",
        "C:\\Users\\robin\\AppData\\Local\\Temp\\x\\c.txt"], cds=["cd /sessions/busy-fox/mnt/outputs && ls"]))
    assert project_key(data_dir, "cw-none").endswith("/local-agent-mode-sessions")

    # A repository that the hub does not know yet: first the folder, and the hub asks the collector about it.
    upload(client, "cw-repo", cowork_lines("cw-repo", reads=[
        f"D:\\GolleIT\\FgitsTickets\\src\\{name}.php" for name in ("a", "b", "c")]))
    assert project_key(data_dir, "cw-repo") == "dir:desktop:d:/golleit/fgitstickets"
    probes = client.get("/api/v1/sessions/state").json()["probe_dirs"]
    assert "D:/GolleIT/FgitsTickets/src" in probes
    assert not any("swutsch" in path or "local-agent-mode-sessions" in path for path in probes)
    # The collector found the repository and reports it. The session moves, and the folder project goes.
    client.put("/api/v1/inventory", json={"repos": [
        {"path": "D:\\GolleIT\\FgitsTickets", "remote": "git.example.com/golle/fgitstickets",
         "worktrees": [{"path": "D:\\GolleIT\\FgitsTickets", "is_main": True, "branch": "main"}]},
        {"path": "D:\\WIP\\swutsch", "remote": "github.com/robing98/swutsch",
         "worktrees": [{"path": "D:\\WIP\\swutsch", "is_main": True, "branch": "main"}]}]})
    assert project_key(data_dir, "cw-repo") == "git:git.example.com/golle/fgitstickets"
    assert one(data_dir, "SELECT COUNT(*) AS n FROM projects WHERE key = 'dir:desktop:d:/golleit/fgitstickets'")["n"] == 0
    # The sessions that belong to folders stay where they are.
    assert project_key(data_dir, "cw-uni").endswith("bpp und ba")
    assert project_key(data_dir, "cw-parent") == "git:github.com/robing98/swutsch"

    # One stray sandbox path is not enough to move a session into a repository.
    upload(client, "cw-stray", cowork_lines("cw-stray", cds=["cd /sessions/busy-fox/mnt/swutsch && ls"]))
    assert project_key(data_dir, "cw-stray").endswith("/local-agent-mode-sessions")
