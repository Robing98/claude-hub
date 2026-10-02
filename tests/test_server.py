import base64
import gzip

from claude_hub.server import db, store

from conftest import make_lines, to_jsonl


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
    assert client.get("/api/v1/sessions/state").json() == {"sessions": {}}


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
                 "/sessions?q=caves", "/settings", "/projects/1", "/sessions/1", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, path

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
