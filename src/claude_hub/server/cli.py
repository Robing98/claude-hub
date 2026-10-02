"""Command line for the server: run it, manage collector tokens, re-parse."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from . import db
from ..transcript import PARSER_VERSION
from .ingest import hash_token, refresh_session
from .projects import reassign_loose_sessions


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir or os.environ.get("HUB_DATA_DIR", "./data")).resolve()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(_data_dir(args)), host=args.host, port=args.port)
    return 0


def cmd_token_add(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    db.init(data_dir)
    conn = db.connect(data_dir)
    try:
        conn.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (args.user,))
        user_id = conn.execute("SELECT id FROM users WHERE name = ?", (args.user,)).fetchone()["id"]
        token = secrets.token_urlsafe(32)
        existing = conn.execute(
            "SELECT id FROM machines WHERE user_id = ? AND name = ?", (user_id, args.machine)
        ).fetchone()
        if existing:
            # A second "add" for the same machine rotates its token and keeps its data.
            conn.execute("UPDATE machines SET token_hash = ? WHERE id = ?",
                         (hash_token(token), existing["id"]))
        else:
            conn.execute(
                "INSERT INTO machines (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, args.machine, hash_token(token), db.now_iso()),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"Token for machine '{args.machine}' (user '{args.user}'). It is shown only once:")
    print(token)
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    db.init(data_dir)
    conn = db.connect(data_dir)
    try:
        rows = conn.execute(
            "SELECT m.name, u.name AS user, m.platform, m.last_seen FROM machines m "
            "JOIN users u ON u.id = m.user_id ORDER BY m.name"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        print(f"{row['name']}\tuser={row['user']}\tplatform={row['platform'] or '-'}"
              f"\tlast_seen={row['last_seen'] or 'never'}")
    return 0


def cmd_reparse(args: argparse.Namespace) -> int:
    """Re-derive session fields from the stored transcripts.

    Needed after a parser change. Each session is committed on its own, so
    that a running server is blocked only briefly.
    """
    data_dir = _data_dir(args)
    db.init(data_dir)
    conn = db.connect(data_dir)
    try:
        query = "SELECT pk FROM sessions"
        if args.outdated:
            query += f" WHERE parser_version < {PARSER_VERSION}"
        keys = [row["pk"] for row in conn.execute(query).fetchall()]
        for pk in keys:
            refresh_session(conn, data_dir, pk)
            conn.commit()
        moved = 0
        if keys:
            for machine in conn.execute("SELECT id FROM machines").fetchall():
                moved += reassign_loose_sessions(conn, machine["id"])
            conn.commit()
    finally:
        conn.close()
    print(f"Re-parsed {len(keys)} sessions, moved {moved} to another project.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-hub")
    parser.add_argument("--data-dir", help="Data directory (default: $HUB_DATA_DIR or ./data)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.set_defaults(func=cmd_serve)

    token = sub.add_parser("token", help="Manage collector tokens")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    add = token_sub.add_parser("add", help="Create or rotate the token of a machine")
    add.add_argument("machine")
    add.add_argument("--user", default="me")
    add.set_defaults(func=cmd_token_add)
    token_sub.add_parser("list", help="List machines").set_defaults(func=cmd_token_list)

    reparse = sub.add_parser("reparse", help="Re-derive session fields from stored transcripts")
    reparse.add_argument("--outdated", action="store_true",
                         help="Only sessions that an older parser version read")
    reparse.set_defaults(func=cmd_reparse)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
