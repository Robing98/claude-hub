"""Command line for the collector."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from . import briefing
from . import config as config_module
from .client import Client, HubError
from .gitscan import build_inventory
from .rulescan import scan_repo, scan_user
from .sessions import find_transcripts, sync_account


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def run_once(cfg: config_module.Config, sessions: bool = True, inventory: bool = True,
             rules: bool = True) -> None:
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

        if rules:
            sources = [
                {"scope": "user", "account": account.label, "base_path": str(account.config_dir),
                 "files": scan_user(account.config_dir)}
                for account in cfg.accounts if account.config_dir.is_dir()
            ]
            sources += [
                {"scope": "project", "base_path": repo["path"], "remote": repo["remote"],
                 "files": scan_repo(Path(repo["path"]))}
                for repo in repos
            ]
            _, answer = client.request("PUT", "/api/v1/rules", {"sources": sources})
            log(f"instruction files: {answer['files']} files, {answer['items']} rules")
    log(f"done, reported as machine '{hello['machine']}'")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    while True:
        try:
            run_once(cfg, sessions=not args.no_sessions, inventory=not args.no_inventory,
                     rules=not args.no_rules)
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


def _cache_dir(args: argparse.Namespace) -> Path:
    return args.config.parent / "cache"


def _custom_config(args: argparse.Namespace) -> Path | None:
    """The config path, if it is not the default one. Hook commands must repeat it."""
    return None if args.config == config_module.default_path() else args.config


def cmd_hook(args: argparse.Namespace) -> int:
    """Print the session rules. Never fails, so that a session always starts."""
    try:
        cwd = os.getcwd()
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                cwd = json.loads(raw).get("cwd") or cwd
        cfg = config_module.load(args.config)
        text, cached = briefing.fetch_briefing(cfg, _cache_dir(args), cwd)
        if text:
            text = text.replace(briefing.PLACEHOLDER,
                                briefing.rules_command(_custom_config(args)) + " rules show")
            if cached:
                text += "\n(The hub was not reachable. These rules are the last saved copy.)\n"
            briefing.emit(text)
    except Exception:  # noqa: BLE001 - a broken hook must not block the session
        pass
    return 0


def cmd_rules_show(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    text, state = briefing.fetch_ruleset(cfg, _cache_dir(args), args.name)
    if text is None:
        reason = ("The hub is not reachable and there is no saved copy of"
                  if state == "offline" else "The hub has no")
        print(f"{reason} rule set '{args.name}'.", file=sys.stderr)
        return 1
    briefing.emit(text)
    if state == "cached":
        briefing.emit("\n(The hub was not reachable. This is the last saved copy.)\n")
    return 0


def cmd_rules_list(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    text, cached = briefing.fetch_briefing(cfg, _cache_dir(args), os.getcwd())
    if not text:
        print("No rule set applies here, or the hub is not reachable.")
        return 0
    briefing.emit(text.replace(briefing.PLACEHOLDER,
                               briefing.rules_command(_custom_config(args)) + " rules show"))
    return 0


def cmd_hooks_install(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    for account in cfg.accounts:
        print(briefing.install_hook(account.config_dir, _custom_config(args), remove=args.remove))
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
    run.add_argument("--no-inventory", action="store_true",
                     help="Skip repositories, worktrees, and instruction files")
    run.add_argument("--no-rules", action="store_true", help="Skip instruction files")
    run.set_defaults(func=cmd_run)

    sub.add_parser("check", help="Test the connection and the configuration").set_defaults(func=cmd_check)
    sub.add_parser("scan", help="Print the repository inventory without sending it").set_defaults(func=cmd_scan)
    sub.add_parser("init", help="Write an example configuration").set_defaults(func=cmd_init)

    hook = sub.add_parser("hook", help="Called by Claude Code hooks")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("session-start", help="Print the rules for the session").set_defaults(
        func=cmd_hook)

    rules = sub.add_parser("rules", help="Rule sets from the hub")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    show = rules_sub.add_parser("show", help="Print one rule set")
    show.add_argument("name")
    show.set_defaults(func=cmd_rules_show)
    rules_sub.add_parser("list", help="Print what a session in this folder receives").set_defaults(
        func=cmd_rules_list)

    hooks = sub.add_parser("hooks", help="Manage the Claude Code hook")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    install = hooks_sub.add_parser("install", help="Add the session hook to Claude Code")
    install.add_argument("--remove", action="store_true", help="Remove the hook instead")
    install.set_defaults(func=cmd_hooks_install)

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
