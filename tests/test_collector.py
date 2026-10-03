import json
import subprocess
from pathlib import Path

import pytest

from claude_hub.collector import gitscan
from claude_hub.collector.cli import run_once
from claude_hub.collector.config import Account, Config, ConfigError, load
from claude_hub.server import db, store, usage
from claude_hub.server.pricing import load_prices

from conftest import TOKEN, make_lines, to_jsonl


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def commit(cwd: Path, name: str) -> None:
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", f"Add {name}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a remote and four worktrees in different states."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    repo = tmp_path / "dev" / "demo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    commit(repo, "README.md")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-u", "origin", "main")

    trees = repo / ".claude" / "worktrees"
    git(repo, "worktree", "add", "-b", "dirty", str(trees / "dirty"))
    (trees / "dirty" / "scratch.txt").write_text("unsaved")

    git(repo, "worktree", "add", "-b", "unpushed", str(trees / "unpushed"))
    commit(trees / "unpushed", "local-only.txt")

    git(repo, "worktree", "add", "-b", "pushed", str(trees / "pushed"))
    commit(trees / "pushed", "shared.txt")
    git(trees / "pushed", "push", "-u", "origin", "pushed")

    git(repo, "worktree", "add", "-b", "merged", str(trees / "merged"))

    # The identity must come from the URL, so point origin at a hosted address
    # after the refs exist.
    git(repo, "remote", "set-url", "origin", "https://token@github.com/Example/Demo.git")
    return repo


def test_find_repos_skips_linked_worktrees_and_noise(repo: Path, tmp_path: Path):
    (tmp_path / "dev" / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    found = list(gitscan.find_repos([tmp_path / "dev"], depth=4))
    assert found == [repo]
    assert list(gitscan.find_repos([tmp_path], depth=1)) == []


def test_describe_repo(repo: Path):
    described = gitscan.describe_repo(repo)
    assert described["remote"] == "github.com/example/demo"
    assert described["default_branch"] == "main"
    by_branch = {wt["branch"]: wt for wt in described["worktrees"]}
    assert set(by_branch) == {"main", "dirty", "unpushed", "pushed", "merged"}

    assert by_branch["main"]["is_main"] and by_branch["main"]["upstream"] == "origin/main"
    assert by_branch["dirty"]["dirty_files"] == 1
    assert by_branch["unpushed"]["upstream"] is None and by_branch["unpushed"]["merged"] is False
    assert (by_branch["pushed"]["ahead"], by_branch["pushed"]["behind"]) == (0, 0)
    assert by_branch["pushed"]["merged"] is False
    assert by_branch["merged"]["merged"] is True and by_branch["merged"]["dirty_files"] == 0
    assert by_branch["pushed"]["last_commit_subject"] == "Add shared.txt"
    assert by_branch["pushed"]["last_commit_at"].endswith("Z")


def test_deleted_worktree_folder_is_reported_as_missing(repo: Path):
    import shutil

    shutil.rmtree(repo / ".claude" / "worktrees" / "merged")
    by_branch = {wt["branch"]: wt for wt in gitscan.describe_repo(repo)["worktrees"]}
    assert by_branch["merged"]["prunable"] is True


def test_resolve_dir(repo: Path, tmp_path: Path):
    inside = gitscan.resolve_dir(str(repo / ".claude" / "worktrees" / "dirty"))
    assert inside["remote"] == "github.com/example/demo"
    assert Path(inside["repo_root"]) == repo
    assert gitscan.resolve_dir(str(tmp_path))["remote"] is None
    assert gitscan.resolve_dir(str(tmp_path / "missing"))["repo_root"] is None


def test_scanning_takes_no_locks_and_changes_nothing(repo: Path):
    before = git(repo, "status", "--porcelain")
    gitscan.describe_repo(repo)
    assert git(repo, "status", "--porcelain") == before
    assert not (repo / ".git" / "index.lock").exists()


def write_transcript(config_dir: Path, session_id: str, lines: list[dict]) -> Path:
    folder = config_dir / "projects" / "-some-project"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_bytes(to_jsonl(lines))
    return path


def stored_lines(data_dir: Path, session_id: str) -> list[bytes]:
    return list(store.read_lines(store.transcript_path(data_dir, 1, session_id)))


def test_end_to_end(repo: Path, tmp_path: Path, live_server: str, data_dir: Path):
    config_dir = tmp_path / "claude-a"
    worktree = repo / ".claude" / "worktrees" / "dirty"
    lines = make_lines("sess-1", str(worktree), ["Start the feature", "Continue"], branch="dirty")
    path = write_transcript(config_dir, "sess-1", lines[:4])
    # Subagent transcripts live one level deeper. They are uploaded for their
    # token usage, under the session that started them.
    sub = path.parent / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_bytes(to_jsonl(
        [{**line, "isSidechain": True} for line in make_lines("sess-1", str(worktree), ["Search"])]))

    (config_dir / "CLAUDE.md").write_text("# Personal\nKeep answers short.\n")
    (repo / "CLAUDE.md").write_text("# Build\nRun the gate.\n# Tests\nUse pytest.\n")

    cfg = Config(server_url=live_server, token=TOKEN,
                 accounts=[Account("max-1", config_dir), Account("max-2", tmp_path / "absent")],
                 chunk_bytes=200)   # Small chunks force several requests per file.
    run_once(cfg)

    conn = db.connect(data_dir)
    rule_files = conn.execute(
        "SELECT f.scope, f.rel_path, p.key, (SELECT COUNT(*) FROM rule_items i WHERE i.file_id = f.id) "
        "FROM rule_files f LEFT JOIN projects p ON p.id = f.project_id ORDER BY f.scope").fetchall()
    conn.close()
    assert [tuple(row) for row in rule_files] == [
        ("project", "CLAUDE.md", "git:github.com/example/demo", 2), ("user", "CLAUDE.md", None, 1)]

    conn = db.connect(data_dir)
    session = conn.execute("SELECT s.*, p.key FROM sessions s JOIN projects p ON p.id = s.project_id").fetchone()
    assert session["key"] == "git:github.com/example/demo"
    assert session["first_prompt"] == "Start the feature"
    assert session["raw_bytes"] == path.stat().st_size
    others = conn.execute("SELECT session_id, parent_session_id, project_id FROM sessions "
                          "WHERE pk != ?", (session["pk"],)).fetchall()
    assert [tuple(row) for row in others] == [("sess-1.agent-1", "sess-1", None)]
    # One reply of the session and one of its subagent: the subagent's tokens
    # count for the project of its parent.
    spent = usage.for_project(conn, load_prices(None), session["project_id"], 0)
    assert (spent["messages"], spent["output"], spent["cache_write_1h"]) == (2, 40, 60)
    # No scan root is configured: the repository was found through the session.
    assert conn.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0] == 5
    assert conn.execute("SELECT platform FROM machines").fetchone()[0]
    conn.close()
    assert len(stored_lines(data_dir, "sess-1")) == 4

    # The session continues, and Claude Code is in the middle of writing a line.
    with open(path, "ab") as handle:
        handle.write(to_jsonl(lines[4:]))
        handle.write(b'{"type": "assistant", "partial')
    run_once(cfg)
    assert len(stored_lines(data_dir, "sess-1")) == len(lines)

    # Nothing new: a third run uploads nothing and duplicates nothing.
    run_once(cfg)
    stored = stored_lines(data_dir, "sess-1")
    assert len(stored) == len(lines)
    assert [json.loads(line) for line in stored] == lines

    conn = db.connect(data_dir)
    row = conn.execute("SELECT title, user_prompts, last_event FROM sessions "
                       "WHERE parent_session_id IS NULL").fetchone()
    assert tuple(row) == ("Title of sess-1", 2, "reply")
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    conn.close()


def test_rewritten_transcript_is_uploaded_again(tmp_path: Path, live_server: str, data_dir: Path):
    config_dir = tmp_path / "claude-a"
    path = write_transcript(config_dir, "sess-2", make_lines("sess-2", str(tmp_path), ["Original"]))
    cfg = Config(server_url=live_server, token=TOKEN, accounts=[Account("a", config_dir)])
    run_once(cfg)
    path.write_bytes(to_jsonl(make_lines("sess-2", str(tmp_path), ["Replaced after compaction"])))
    run_once(cfg)
    conn = db.connect(data_dir)
    row = conn.execute("SELECT first_prompt, raw_bytes FROM sessions").fetchone()
    conn.close()
    assert tuple(row) == ("Replaced after compaction", path.stat().st_size)


def test_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_TOKEN", raising=False)
    path = tmp_path / "collector.toml"
    with pytest.raises(ConfigError):
        load(path)
    path.write_text('server_url = "http://hub:8787/"\ntoken = "PASTE_TOKEN_HERE"\n')
    with pytest.raises(ConfigError):
        load(path)
    path.write_text(
        'server_url = "http://hub:8787/"\ntoken = "abc"\nscan_roots = ["~/dev"]\n'
        '[[accounts]]\nlabel = "one"\nconfig_dir = "~/.claude"\n'
        '[[accounts]]\nlabel = "two"\nconfig_dir = "~/.claude-two"\n')
    cfg = load(path)
    assert cfg.server_url == "http://hub:8787"
    assert [a.label for a in cfg.accounts] == ["one", "two"]
    assert cfg.scan_roots == [Path.home() / "dev"]


def test_linked_worktrees_inside_the_repository_are_not_unsaved_work(repo: Path):
    main = next(wt for wt in gitscan.describe_repo(repo)["worktrees"] if wt["is_main"])
    assert main["dirty_files"] == 0
    # A real untracked file next to the worktrees still counts.
    (repo / ".claude" / "notes.md").write_text("todo")
    main = next(wt for wt in gitscan.describe_repo(repo)["worktrees"] if wt["is_main"])
    assert main["dirty_files"] == 1


def test_worktrees_of_the_other_system_are_left_out(repo: Path, monkeypatch):
    # Seen from Windows, every path in this Linux test repository is foreign.
    monkeypatch.setattr(gitscan.sys, "platform", "win32")
    assert gitscan._foreign("/home/robin/.cache/ci/wt") and not gitscan._foreign("D:/dev/orbis")
    monkeypatch.setattr(gitscan.sys, "platform", "linux")
    assert gitscan._foreign("C:/Users/robin/repo") and not gitscan._foreign("/mnt/c/repo")

    listing = "worktree /srv/repo\nHEAD abc\nbranch refs/heads/main\n\nworktree C:/Users/robin/wt\nHEAD def\nbranch refs/heads/x\nprunable gone\n"
    monkeypatch.setattr(gitscan, "_parse_worktree_list", lambda text: gitscan.__dict__["_real_parse"](listing))
    monkeypatch.setitem(gitscan.__dict__, "_real_parse", _REAL_PARSE)
    branches = [wt["branch"] for wt in gitscan.describe_repo(repo)["worktrees"]]
    assert branches == ["main"]


_REAL_PARSE = gitscan._parse_worktree_list


def test_default_config_path_ignores_empty_variables(monkeypatch, tmp_path):
    from claude_hub.collector import config

    monkeypatch.delenv("CLAUDE_HUB_CONFIG", raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert config.default_path() == tmp_path / ".config" / "claude-hub" / "collector.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.default_path() == tmp_path / "xdg" / "claude-hub" / "collector.toml"


def cowork_session(root: Path, name: str, session_id: str, lines_for) -> Path:
    """Write a Cowork session folder as the Claude desktop app lays it out."""
    session = root / "acc-1" / "org-1" / f"local_{name}"
    cwd = str(session / "outputs")
    folder = session / ".claude" / "projects" / "C--encoded-outputs"
    folder.mkdir(parents=True)
    (folder / f"{session_id}.jsonl").write_bytes(to_jsonl(lines_for(cwd)))
    # The audit log is not a transcript and must stay where it is.
    (session / "audit.jsonl").write_text('{"type": "command_lifecycle", "session_id": "x"}\n')
    return session


def test_cowork_sessions_are_collected_and_sorted(repo: Path, tmp_path: Path, live_server: str, data_dir: Path):
    def read(session_id, cwd, file_path, index):
        return {"type": "assistant", "sessionId": session_id, "cwd": cwd, "uuid": f"t{index}",
                "timestamp": f"2026-10-01T11:00:0{index}.000Z", "isSidechain": False,
                "message": {"id": f"tm{index}", "model": "claude-test", "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "id": f"tt{index}", "name": "Read",
                                         "input": {"file_path": file_path}}]}}

    root = tmp_path / "Claude" / "local-agent-mode-sessions"
    # This session worked in a connected folder, which Cowork names by its sandbox path.
    in_repo = cowork_session(root, "a", "cw-repo", lambda cwd: make_lines("cw-repo", cwd, ["Review the docs"]) + [
        read("cw-repo", cwd, "/sessions/busy-fox/mnt/demo/README.md", 1),
        read("cw-repo", cwd, "/sessions/busy-fox/mnt/demo/docs/plan.md", 2)])
    for name in ("b", "c"):
        cowork_session(root, name, f"cw-{name}", lambda cwd, name=name: make_lines(f"cw-{name}", cwd, ["A question"]))
    (in_repo / ".claude" / "CLAUDE.md").write_text("# Not an instruction source\n")
    (root / "acc-1" / "org-1" / "local_empty" / ".claude").mkdir(parents=True)    # no transcripts yet

    config = tmp_path / "collector.toml"
    code_dir = tmp_path / "claude-code"
    config.write_text(f'server_url = "{live_server}"\ntoken = "{TOKEN}"\n'
                      f'cowork_dir = "{root.as_posix()}"\nscan_roots = ["{repo.parent.as_posix()}"]\n'
                      f'[[accounts]]\nlabel = "max-1"\nconfig_dir = "{code_dir.as_posix()}"\n')
    cfg = load(config)
    assert [(a.label, a.kind) for a in cfg.accounts] == [("max-1", "claude-code")] + [("cowork", "cowork")] * 3
    off = tmp_path / "off.toml"
    off.write_text(config.read_text().replace("cowork_dir", "cowork = false\ncowork_dir"))
    assert [a.kind for a in load(off).accounts] == ["claude-code"]

    run_once(cfg)
    # A second run finds the repository of the first session through the inventory.
    run_once(cfg)
    conn = db.connect(data_dir)
    rows = {row["session_id"]: row for row in conn.execute(
        """SELECT s.session_id, a.label, p.key, p.name FROM sessions s
           JOIN accounts a ON a.id = s.account_id LEFT JOIN projects p ON p.id = s.project_id""")}
    assert set(rows) == {"cw-repo", "cw-b", "cw-c"}                 # no audit log, no empty folder
    assert all(row["label"] == "cowork" for row in rows.values())
    assert rows["cw-repo"]["key"] == "git:github.com/example/demo"
    # The two sessions without a repository share one project instead of two inbox entries.
    assert rows["cw-b"]["key"] == rows["cw-c"]["key"] and rows["cw-b"]["name"] == "Cowork"
    assert rows["cw-b"]["key"].endswith("/local-agent-mode-sessions")
    # A Cowork session folder is no source of instruction files.
    assert conn.execute("SELECT COUNT(*) FROM rule_files WHERE content LIKE '%Not an instruction%'").fetchone()[0] == 0
    report = usage.report(conn, load_prices(None), 0)
    assert [(item["name"], len(item["sessions"])) for item in report["accounts"]] == [("cowork", 3)]
    conn.close()

    # The session hook is for Claude Code. It is never written into a Cowork session folder.
    from claude_hub.collector import cli
    assert cli.main(["--config", str(config), "hooks", "install"]) == 0
    assert (code_dir / "settings.json").exists()
    assert not list(root.rglob("settings.json"))
    assert cli.main(["--config", str(config), "check"]) == 0


def test_probe_reports_repositories_and_remembers_what_is_none(repo: Path, tmp_path: Path, monkeypatch):
    from claude_hub.collector import cli

    plain = tmp_path / "plain"
    plain.mkdir()
    calls: list[str] = []
    real = cli.resolve_dir
    monkeypatch.setattr(cli, "resolve_dir", lambda folder: calls.append(folder) or real(folder))

    seen: set[str] = set()
    cache = tmp_path / "cache"
    folders = [str(repo / ".claude" / "worktrees" / "dirty"), str(plain), str(tmp_path / "gone")]
    cli.probe(folders, cache, seen)
    assert {Path(path).name for path in seen} == {"demo"}           # the main repository of the worktree
    assert len(calls) == 3

    # The two folders that are no repository are not asked about again today.
    cli.probe(folders, cache, seen)
    assert len(calls) == 4 and calls[-1] == folders[0]
    # A broken cache file must not stop a run.
    (cache / "not-repositories.json").write_text("{broken")
    cli.probe(folders, cache, set())
    cli.probe(folders, None, set())
