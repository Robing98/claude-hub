"""SQLite schema and connection handling."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    platform TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    UNIQUE (user_id, label)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE (workspace_id, name)
);

-- key is "git:host/owner/name", "local:machine:path" (repository without a
-- remote), or "dir:machine:path" (plain folder).
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    assigned_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 0,
    field TEXT NOT NULL,
    pattern TEXT NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    class_id INTEGER REFERENCES classes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    pk INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    config_dir TEXT,
    source_path TEXT,
    cwd TEXT,
    git_branch TEXT,
    title TEXT,
    first_prompt TEXT,
    last_prompt TEXT,
    started_at TEXT,
    ended_at TEXT,
    user_prompts INTEGER NOT NULL DEFAULT 0,
    assistant_messages INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    entrypoint TEXT,
    cc_version TEXT,
    models TEXT,
    cost_usd REAL,
    last_event TEXT,
    last_tool TEXT,
    raw_bytes INTEGER NOT NULL DEFAULT 0,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    head_sha TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (machine_id, session_id)
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id, ended_at);
CREATE INDEX IF NOT EXISTS sessions_ended ON sessions(ended_at);

CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    default_branch TEXT,
    seen_at TEXT NOT NULL,
    UNIQUE (machine_id, path)
);

CREATE TABLE IF NOT EXISTS worktrees (
    id INTEGER PRIMARY KEY,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    is_main INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    head TEXT,
    last_commit_at TEXT,
    last_commit_subject TEXT,
    dirty_files INTEGER NOT NULL DEFAULT 0,
    upstream TEXT,
    ahead INTEGER,
    behind INTEGER,
    merged INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    prunable INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repo_id, path)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    # One request uses its connection from several worker threads in turn:
    # FastAPI runs the dependency, the endpoint, and the cleanup separately.
    # They never overlap, so the thread check is turned off.
    conn = sqlite3.connect(data_dir / "hub.sqlite3", timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the web view read while a collector upload writes.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Schema changes are appended here as new entries. Entries that have shipped
# are never edited, because existing databases have already applied them.
RULES_SCHEMA = """
CREATE TABLE rule_files (
    id INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    base_path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    applies_to TEXT,
    content TEXT NOT NULL,
    sha TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    modified_at TEXT,
    UNIQUE (machine_id, base_path, rel_path)
);
CREATE INDEX rule_files_name ON rule_files(kind, name);

CREATE TABLE rule_items (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES rule_files(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    level INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    sha TEXT NOT NULL
);
CREATE INDEX rule_items_sha ON rule_items(sha);
"""

WORK_DIRS_SCHEMA = """
ALTER TABLE sessions ADD COLUMN work_dirs TEXT;
ALTER TABLE sessions ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 0;
"""

# Whether a project may carry AI traces in Git: attribution in commits, Claude files.
AI_SWITCH_SCHEMA = """
ALTER TABLE projects ADD COLUMN ai_ok INTEGER NOT NULL DEFAULT 0;
"""

# Whether sessions of a project get their rules from the hub. Off by default,
# so a project keeps its own rule files until it is moved on purpose.
HUB_RULES_SCHEMA = """
ALTER TABLE projects ADD COLUMN hub_rules INTEGER NOT NULL DEFAULT 0;
"""

# Token counts per session, UTC day, and model. Costs are not stored: they are
# computed from the price file when a page is shown, so a price change applies
# to the past as well. A subagent transcript is a session with a parent.
USAGE_SCHEMA = """
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT;
CREATE INDEX sessions_parent ON sessions(machine_id, parent_session_id);
CREATE TABLE usage_daily (
    session_pk INTEGER NOT NULL REFERENCES sessions(pk) ON DELETE CASCADE,
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_pk, day, model)
);
CREATE INDEX usage_daily_day ON usage_daily(day);
"""

MIGRATIONS = [SCHEMA, RULES_SCHEMA, WORK_DIRS_SCHEMA, AI_SWITCH_SCHEMA, HUB_RULES_SCHEMA,
              USAGE_SCHEMA]


def init(data_dir: Path) -> None:
    conn = connect(data_dir)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            conn.executescript(MIGRATIONS[index])
            conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()
    finally:
        conn.close()
