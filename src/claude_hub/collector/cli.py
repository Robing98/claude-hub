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

from . import briefing, export, mcp_server
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
        cowork = [0, 0, 0]      # folders, sessions, bytes
        for account in cfg.accounts:
            if not account.config_dir.is_dir():
                log(f"account '{account.label}': {account.config_dir} does not exist, skipped")
                continue
            files, total = sync_account(client, account, state, cfg.chunk_bytes, git_cache,
                                        repos_seen, log)
            if account.kind == config_module.COWORK:
                # Every Cowork session has a folder of its own. One line covers them all.
                cowork = [cowork[0] + 1, cowork[1] + files, cowork[2] + total]
            else:
                log(f"account '{account.label}': uploaded {files} sessions, {total / 1e6:.1f} MB")
        if cowork[0]:
            log(f"Cowork on this computer: {cowork[0]} session folders, uploaded {cowork[1]} "
                f"sessions, {cowork[2] / 1e6:.1f} MB")

    if inventory:
        repos = build_inventory(cfg.scan_roots, cfg.scan_depth, repos_seen)
        _, answer = client.request("PUT", "/api/v1/inventory",
                                   {"platform": platform.system().lower(), "repos": repos})
        log(f"inventory: {answer['repos']} repositories, {answer['worktrees']} worktrees")

        if rules:
            sources = [
                {"scope": "user", "account": account.label, "base_path": str(account.config_dir),
                 "files": scan_user(account.config_dir)}
                for account in cfg.accounts
                if account.kind != config_module.COWORK and account.config_dir.is_dir()
            ]
            sources += [
                {"scope": "project", "base_path": repo["path"], "remote": repo["remote"],
                 "files": scan_repo(Path(repo["path"]))}
                for repo in repos
            ]
            _, answer = client.request("PUT", "/api/v1/rules", {"sources": sources})
            log(f"instruction files: {answer['files']} files, {answer['items']} rules")

        # Keep rule exports current. Only folders that "rules export" created
        # are written, so a collector run never adds files to a repository.
        for repo in repos:
            folder = Path(repo["path"])
            if export.is_exported(folder):
                changed, message = export.export_rules(cfg, folder)
                if changed:
                    log(f"rule export refreshed: {message}")
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
    folders = transcripts = 0
    for account in cfg.accounts:
        count = sum(1 for _ in find_transcripts(account.config_dir))
        if account.kind == config_module.COWORK:
            folders, transcripts = folders + 1, transcripts + count
        else:
            print(f"Account '{account.label}': {count} transcripts in {account.config_dir}")
    if folders:
        print(f"Cowork on this computer: {transcripts} transcripts in {folders} session folders")
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
            text = briefing.fill_commands(text, _custom_config(args))
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
    briefing.emit(briefing.fill_commands(text, _custom_config(args)))
    return 0


def cmd_rules_propose(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    filed, message = briefing.propose(cfg, os.getcwd(), args.ruleset, args.text, args.reason,
                                      "replace" if args.replace else "add", args.source)
    print(message, file=sys.stdout if filed else sys.stderr)
    return 0 if filed else 1


def cmd_rules_export(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    folder = Path(args.folder or os.getcwd()).resolve()
    if not folder.is_dir():
        print(f"{folder} is not a folder.", file=sys.stderr)
        return 1
    changed, message = export.export_rules(cfg, folder)
    print(message)
    return 0 if export.is_exported(folder) else 1


def cmd_rules_pull(args: argparse.Namespace) -> int:
    """Bring rule sets that were changed in the hub into the repository folder."""
    cfg = config_module.load(args.config)
    folder = Path(args.folder).resolve()
    if not (folder / "README.md").is_file():
        print(f"{folder} does not look like the rulesets folder of the hub repository.",
              file=sys.stderr)
        return 1
    try:
        _, answer = Client(cfg.server_url, cfg.token, timeout=30).request(
            "GET", "/api/v1/ruleset-edits")
    except HubError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    for name, text in sorted(answer["sets"].items()):
        if "/" in name or "\\" in name or ".." in name:
            continue
        (folder / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")
        print(f"Updated {name}.md")
    if not answer["sets"]:
        print("The repository already holds every rule set as the hub delivers it.")
    else:
        print("Review the changes, then commit and deploy.")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    mcp_server.serve(cfg, _cache_dir(args))
    return 0


def cmd_mcp_install(args: argparse.Namespace) -> int:
    path = args.desktop_config or mcp_server.desktop_config_path()
    print(mcp_server.install(path, _custom_config(args), remove=args.remove))
    return 0


def cmd_hooks_install(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    for account in cfg.accounts:
        # The hook belongs to Claude Code. A Cowork session folder is left alone.
        if account.kind != config_module.COWORK:
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
    propose = rules_sub.add_parser("propose", help="Propose a rule change to the hub")
    propose.add_argument("ruleset", help="Name of the rule set")
    propose.add_argument("text", help="The rule, as it should appear in the set")
    propose.add_argument("--reason", default="", help="Why the change is needed")
    propose.add_argument("--replace", action="store_true",
                         help="The text replaces the whole set instead of adding to it")
    propose.add_argument("--source", default="Claude Code", help="Who proposes")
    propose.set_defaults(func=cmd_rules_propose)
    export_cmd = rules_sub.add_parser(
        "export", help="Write the rules of a project into its folder, hidden from Git")
    export_cmd.add_argument("folder", nargs="?", help="Project folder. Default: this folder.")
    export_cmd.set_defaults(func=cmd_rules_export)
    pull = rules_sub.add_parser(
        "pull", help="Write rule sets that were changed in the hub into the repository")
    pull.add_argument("folder", nargs="?", default="rulesets",
                      help="The rulesets folder of the hub repository. Default: ./rulesets")
    pull.set_defaults(func=cmd_rules_pull)

    mcp = sub.add_parser("mcp", help="Run as a local MCP server for the Claude desktop app")
    mcp.set_defaults(func=cmd_mcp)
    mcp_sub = mcp.add_subparsers(dest="mcp_command")
    mcp_install = mcp_sub.add_parser("install", help="Add this server to the Claude desktop app")
    mcp_install.add_argument("--remove", action="store_true", help="Remove the entry instead")
    mcp_install.add_argument("--desktop-config", type=Path, default=None,
                             help="Path to claude_desktop_config.json, if it is not the default")
    mcp_install.set_defaults(func=cmd_mcp_install)

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
