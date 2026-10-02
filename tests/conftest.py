from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from claude_hub.server import db
from claude_hub.server.app import create_app
from claude_hub.server.ingest import hash_token

TOKEN = "test-token"


def make_lines(session_id: str, cwd: str, prompts: list[str], *, branch: str = "main",
               start: str = "2026-10-01T10:00:0", finish_turn: bool = True) -> list[dict]:
    """Build a small transcript in the shape Claude Code writes."""
    lines: list[dict] = []
    for index, prompt in enumerate(prompts):
        stamp = f"{start}{index}.000Z"
        base = {"sessionId": session_id, "cwd": cwd, "gitBranch": branch, "version": "2.1.0",
                "entrypoint": "cli", "isSidechain": False, "timestamp": stamp}
        lines.append({**base, "type": "user", "uuid": f"u{index}",
                      "message": {"role": "user", "content": prompt}})
        lines.append({**base, "type": "assistant", "uuid": f"a{index}t",
                      "message": {"id": f"msg{index}", "role": "assistant", "model": "claude-test",
                                  "stop_reason": "tool_use",
                                  "content": [{"type": "tool_use", "id": f"tool{index}",
                                               "name": "Bash", "input": {"command": "ls"}}]}})
        lines.append({**base, "type": "user", "uuid": f"r{index}",
                      "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": f"tool{index}", "content": "file.txt"}]}})
        if finish_turn or index < len(prompts) - 1:
            lines.append({**base, "type": "assistant", "uuid": f"a{index}",
                          "message": {"id": f"msg{index}b", "role": "assistant", "model": "claude-test",
                                      "stop_reason": "end_turn",
                                      "content": [{"type": "text", "text": f"Answer {index}"}]}})
    lines.append({"type": "ai-title", "aiTitle": f"Title of {session_id}", "sessionId": session_id})
    return lines


def to_jsonl(lines: list[dict]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    db.init(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO users (name) VALUES ('robin')")
    conn.execute(
        "INSERT INTO machines (user_id, name, token_hash, created_at) VALUES (1, 'desktop', ?, ?)",
        (hash_token(TOKEN), db.now_iso()),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def app(data_dir: Path):
    return create_app(data_dir)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield test_client


@pytest.fixture
def live_server(app):
    """Run the app on a real port, because the collector uses urllib."""
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
