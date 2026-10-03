from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from claude_hub.collector import cli, locks, mcp_server
from claude_hub.collector.config import Account, Config
from claude_hub.server import db
from tests.conftest import TOKEN


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dev" / "demo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("a")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "Add a")
    git(repo, "worktree", "add", "-b", "lane", str(repo / ".claude" / "worktrees" / "lane"))
    return repo


def lock(path: Path, age: float, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def states(found: list[dict]) -> dict[str, str]:
    return {Path(entry["path"]).name: entry["state"] for entry in found}


def test_sweep_removes_only_what_nothing_can_own(repo: Path):
    old_empty = lock(repo / ".git" / "index.lock", 600)
    young = lock(repo / ".git" / "HEAD.lock", 5)
    in_worktree = lock(repo / ".git" / "worktrees" / "lane" / "index.lock", 900)
    ref = lock(repo / ".git" / "refs" / "heads" / "lane.lock", 900, "abc")
    with_content = lock(repo / ".git" / "config.lock", 900, "[core]")

    # While a Git process runs, a lock with content can belong to it.
    found = locks.sweep([repo], running=lambda: True)
    assert states(found) == {"index.lock": "removed", "HEAD.lock": "young", "lane.lock": "in use",
                             "config.lock": "in use"}
    assert not old_empty.exists() and not in_worktree.exists()
    assert young.exists() and ref.exists() and with_content.exists()
    assert {e["size"] for e in found if e["state"] == "in use"} == {3, 6}

    # No Git process: the rest that is old goes as well.
    assert states(locks.sweep([repo], running=lambda: False)) == {
        "HEAD.lock": "young", "lane.lock": "removed", "config.lock": "removed"}
    # Git itself works afterwards.
    git(repo, "status")
    assert young.exists()


def test_sweep_can_only_report_and_can_be_forced(repo: Path):
    path = lock(repo / ".git" / "index.lock", 600, "x")
    assert states(locks.sweep([repo], remove=False)) == {"index.lock": "kept"} and path.exists()
    assert states(locks.sweep([repo], running=lambda: True)) == {"index.lock": "in use"}
    assert states(locks.sweep([repo], running=lambda: True, force=True)) == {"index.lock": "removed"}
    assert locks.sweep([repo]) == [] and locks.sweep([repo.parent / "missing"]) == []


def test_other_files_are_never_touched(repo: Path):
    keep = [repo / ".git" / "index", repo / ".git" / "gc.pid", repo / "package.lock",
            repo / ".git" / "objects" / "pack" / "x.lock"]
    for path in keep[1:]:
        lock(path, 9000)
    assert locks.sweep([repo], running=lambda: False) == []
    assert all(path.exists() for path in keep)


def test_describe_names_the_reason():
    entry = {"path": "/r/.git/index.lock", "age_seconds": 7300, "state": "in use"}
    assert locks.describe(entry) == ("/r/.git/index.lock (2 h old): left, because a Git process runs. "
                                    "Use --force if you are sure")
    assert "(90 s old): removed" in locks.describe({**entry, "age_seconds": 90, "state": "removed"})


def test_unlock_command(repo: Path, tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr(locks, "git_running", lambda: False)
    lock(repo / ".git" / "index.lock", 60)
    fresh = lock(repo / ".git" / "HEAD.lock", 1)
    # From a folder inside a linked worktree, the locks of the whole repository are found.
    inside = repo / ".claude" / "worktrees" / "lane"
    assert cli.main(["unlock", str(inside)]) == 1
    out = capsys.readouterr().out
    assert "index.lock (60 s old): removed" in out and "HEAD.lock" in out and "may still use it" in out
    fresh.unlink()
    assert cli.main(["unlock", str(repo)]) == 0
    assert "No Git lock files in 1 repository." in capsys.readouterr().out
    assert cli.main(["unlock", str(tmp_path)]) == 1
    assert "not inside a Git repository" in capsys.readouterr().err

    # Without a folder: every repository below the scan roots.
    config = tmp_path / "collector.toml"
    config.write_text(f'server_url = "http://127.0.0.1:1"\ntoken = "t"\ncowork = false\n'
                      f'scan_roots = ["{(tmp_path / "dev").as_posix()}"]\n')
    lock(repo / ".git" / "index.lock", 60)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--config", str(config), "unlock"]) == 0
    assert "removed" in capsys.readouterr().out


def test_git_running_reads_the_process_list(monkeypatch):
    class Done:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    monkeypatch.setattr(locks.sys, "platform", "linux")
    monkeypatch.setattr(locks.subprocess, "run", lambda *a, **k: Done(1))
    assert locks.git_running() is False
    monkeypatch.setattr(locks.subprocess, "run", lambda *a, **k: Done(0, "123"))
    assert locks.git_running() is True
    monkeypatch.setattr(locks.sys, "platform", "win32")
    monkeypatch.setattr(locks.subprocess, "run", lambda *a, **k: Done(0, '"git.exe","412","Console"'))
    assert locks.git_running() is True
    monkeypatch.setattr(locks.subprocess, "run",
                        lambda *a, **k: Done(0, "INFO: No tasks are running which match the criteria."))
    assert locks.git_running() is False

    def broken(*args, **kwargs):
        raise OSError("no such program")

    # When the process list cannot be read, a lock with content stays.
    monkeypatch.setattr(locks.subprocess, "run", broken)
    assert locks.git_running() is True


# --- the hub side ---------------------------------------------------------------


def report(client, *entries: dict):
    base = {"repo": "D:\\dev\\demo", "locked_at": 1000, "size": 0, "state": "removed"}
    return client.put("/api/v1/locks", json={"locks": [{**base, **entry} for entry in entries]})


def test_hub_shows_locks(client, data_dir: Path):
    assert "No lock files were left behind" in client.get("/worktrees").text
    report(client, {"path": "D:\\dev\\demo\\.git\\index.lock"},
           {"path": "D:\\dev\\demo\\.git\\worktrees\\lane\\index.lock", "state": "in use", "size": 40},
           {"path": "D:\\dev\\demo\\.git\\HEAD.lock", "state": "young", "locked_at": 2000})
    page = client.get("/worktrees").text
    assert "1 lock file could not be removed" in page and "hub unlock --force" in page
    assert ".git\\worktrees\\lane\\index.lock" in page and "HEAD.lock" not in page
    assert "1 Git lock file blocks a repository" in client.get("/").text
    status = client.get("/status.json").json()
    assert status["stuck_locks"] == 1 and "1 stuck Git lock" in status["headline"]

    # The next run removed the stuck one. The young one went away by itself.
    report(client, {"path": "D:\\dev\\demo\\.git\\worktrees\\lane\\index.lock", "size": 40})
    rows = {row["path"].split(".git\\")[1]: row["state"]
            for row in db.connect(data_dir).execute("SELECT * FROM git_locks")}
    assert rows == {"index.lock": "removed", "worktrees\\lane\\index.lock": "removed",
                    "HEAD.lock": "gone"}
    assert client.get("/status.json").json()["stuck_locks"] == 0
    assert "could not be removed" not in client.get("/worktrees").text
    # A new lock on the same file is a new row: the history counts how often it happens.
    report(client, {"path": "D:\\dev\\demo\\.git\\index.lock", "locked_at": 3000})
    assert db.connect(data_dir).execute("SELECT COUNT(*) FROM git_locks").fetchone()[0] == 4
    assert "D:\\dev\\demo" not in client.get("/worktrees", params={"hide": 1}).text


def test_collector_run_cleans_and_reports(live_server, repo: Path, tmp_path: Path, data_dir: Path,
                                          monkeypatch):
    monkeypatch.setattr(locks, "git_running", lambda: False)
    stale = lock(repo / ".git" / "index.lock", 600)
    cfg = Config(live_server, TOKEN, [Account("default", tmp_path / ".claude")],
                 scan_roots=[tmp_path / "dev"])
    cli.run_once(cfg, sessions=False, rules=False)
    assert not stale.exists()
    row = db.connect(data_dir).execute("SELECT * FROM git_locks").fetchone()
    assert row["state"] == "removed" and row["path"].endswith("index.lock")

    # With cleaning turned off, the lock is reported and stays.
    stale = lock(repo / ".git" / "index.lock", 600)
    cli.run_once(Config(live_server, TOKEN, cfg.accounts, scan_roots=cfg.scan_roots, clean_locks=False),
                 sessions=False, rules=False)
    assert stale.exists()
    assert db.connect(data_dir).execute(
        "SELECT state FROM git_locks ORDER BY id DESC").fetchone()[0] == "kept"


def test_connector_tool_unlocks(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(locks, "git_running", lambda: False)
    cfg = Config("http://127.0.0.1:1", "t", [Account("default", tmp_path / ".claude")])

    def call(folder: str) -> dict:
        stdin = io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "hub_unlock", "arguments": {"folder": folder}}}).encode() + b"\n")
        stdout = io.BytesIO()
        mcp_server.serve(cfg, tmp_path / "cache", stdin, stdout)
        return json.loads(stdout.getvalue())["result"]

    stale = lock(repo / ".git" / "index.lock", 60)
    answer = call(str(repo))
    assert not answer["isError"] and "removed" in answer["content"][0]["text"] and not stale.exists()
    assert "No Git lock files" in call(str(repo))["content"][0]["text"]
    assert call(str(tmp_path))["isError"]
    assert "hub_unlock" in [tool["name"] for tool in mcp_server.TOOLS]
