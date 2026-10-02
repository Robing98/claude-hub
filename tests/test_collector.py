import json
import subprocess
from pathlib import Path

import pytest

from claude_hub.collector import gitscan
from claude_hub.collector.cli import run_once
from claude_hub.collector.config import Account, Config, ConfigError, load
from claude_hub.server import db, store

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
    # Subagent transcripts live one level deeper and are not sessions of their own.
    sub = path.parent / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_bytes(to_jsonl(lines[:2]))

    cfg = Config(server_url=live_server, token=TOKEN,
                 accounts=[Account("max-1", config_dir), Account("max-2", tmp_path / "absent")],
                 chunk_bytes=200)   # Small chunks force several requests per file.
    run_once(cfg)

    conn = db.connect(data_dir)
    session = conn.execute("SELECT s.*, p.key FROM sessions s JOIN projects p ON p.id = s.project_id").fetchone()
    assert session["key"] == "git:github.com/example/demo"
    assert session["first_prompt"] == "Start the feature"
    assert session["raw_bytes"] == path.stat().st_size
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
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
    row = conn.execute("SELECT title, user_prompts, last_event FROM sessions").fetchone()
    assert tuple(row) == ("Title of sess-1", 2, "reply")
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
