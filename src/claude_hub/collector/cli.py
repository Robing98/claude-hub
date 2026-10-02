"""Command line for the collector."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config as config_module
from .client import Client, HubError
from .gitscan import build_inventory
from .sessions import find_transcripts, sync_account


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def run_once(cfg: config_module.Config, sessions: bool = True, inventory: bool = True) -> None:
    client = Client(cfg.server_url, cfg.token)
    _, hello = client.request("POST", "/api/v1/hello", {"platform": platform.system().lower()})
    repos_seen: set[str] = set()

    if sessions:
        _, answer = client.request("GET", "/api/v1/sessions/state")
        state = answer["sessions"]
        git_cache: dict[str, dict] = {}
        for account in cfg.accounts:
            if not account.config_dir.is_dir():
                log(f"account '{account.label}': {account.config_dir} does not exist, skipped")
                continue
            files, total = sync_account(client, account, state, cfg.chunk_bytes, git_cache,
                                        repos_seen, log)
            log(f"account '{account.label}': uploaded {files} sessions, {total / 1e6:.1f} MB")

    if inventory:
        repos = build_inventory(cfg.scan_roots, cfg.scan_depth, repos_seen)
        _, answer = client.request("PUT", "/api/v1/inventory",
                                   {"platform": platform.system().lower(), "repos": repos})
        log(f"inventory: {answer['repos']} repositories, {answer['worktrees']} worktrees")
    log(f"done, reported as machine '{hello['machine']}'")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    while True:
        try:
            run_once(cfg, sessions=not args.no_sessions, inventory=not args.no_inventory)
        except HubError as exc:
            log(f"error: {exc}")
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        time.sleep(args.interval)


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    try:
        _, hello = Client(cfg.server_url, cfg.token, timeout=15).request(
            "POST", "/api/v1/hello", {"platform": platform.system().lower()})
    except HubError as exc:
        print(f"Failed: {exc}")
        return 1
    print(f"Connected to {cfg.server_url} as machine '{hello['machine']}' "
          f"(server {hello['server_version']}).")
    for account in cfg.accounts:
        count = sum(1 for _ in find_transcripts(account.config_dir))
        print(f"Account '{account.label}': {count} transcripts in {account.config_dir}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Print the inventory without sending anything."""
    cfg = config_module.load(args.config)
    print(json.dumps(build_inventory(cfg.scan_roots, cfg.scan_depth, set()), indent=2))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.config
    if path.exists():
        print(f"{path} already exists. It was not changed.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_module.EXAMPLE, encoding="utf-8")
    print(f"Wrote {path}. Set server_url and token, then run `claude-hub-collector check`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-hub-collector")
    parser.add_argument("--config", type=Path, default=config_module.default_path(),
                        help="Path to collector.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Upload sessions and the repository inventory")
    run.add_argument("--interval", type=int, default=0,
                     help="Repeat every N seconds. Without it, run once and exit.")
    run.add_argument("--no-sessions", action="store_true")
    run.add_argument("--no-inventory", action="store_true")
    run.set_defaults(func=cmd_run)

    sub.add_parser("check", help="Test the connection and the configuration").set_defaults(func=cmd_check)
    sub.add_parser("scan", help="Print the repository inventory without sending it").set_defaults(func=cmd_scan)
    sub.add_parser("init", help="Write an example configuration").set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
