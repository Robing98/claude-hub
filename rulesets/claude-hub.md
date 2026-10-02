---
title: claude-hub
description: Layout, rules, and commands of the hub repository.
projects: github.com/robing98/claude-hub
order: 20
---
A self-hosted hub that collects Claude Code sessions, worktrees, and instruction files from several machines, and serves rule sets to sessions. Goal and phases: `docs/plan.md`. Setup: `README.md` and `docs/deploy-proxmox.md`.

## Layout

- `src/claude_hub/server/`: FastAPI app, SQLite, server-rendered pages.
- `src/claude_hub/collector/`: runs on every machine.
- `src/claude_hub/*.py`: shared code for both sides.
- `rulesets/`: the rule sets that sessions receive. One Markdown file per set.
- `deploy/`: installation on the Proxmox container. `hub.bat`: shortcuts on Windows.

## Rules

- The collector and the shared modules use the standard library only. The one exception is `tomli` on Python 3.10.
- The collector is read-only towards repositories: it never fetches, commits, or pushes, and it takes no Git locks. From a Claude Code configuration directory it reads only transcripts and Markdown instruction files, never settings or credentials.
- The transcript format is internal to Claude Code. Parse defensively and skip what is unknown. A change to what `parse_meta` derives raises `PARSER_VERSION`.
- A schema change is a new entry in `MIGRATIONS` in `db.py`. A shipped entry is never edited.
- An endpoint that writes calls `conn.commit()` before it returns.
- Text from transcripts and rule files is user-controlled. Templates keep autoescaping and never use `|safe`.
- In templates, do not read a dict key that is named like a dict method (`items`, `keys`, `values`) with dot syntax.
- Every change comes with a test, and the suite is green before a commit.
- Docs and interface text are in English and follow the Google developer documentation style: second person, present tense, sentence-case headings, short sentences.
- Batch files have CRLF line endings, everything else LF. `.gitattributes` enforces it.
- No tokens, passwords, or collector configurations in the repository.

## Commands

- Tests: `.venv\Scripts\pytest`
- Local server: `.venv\Scripts\claude-hub serve`
- Commit, push, and install on the server: `hub ship "MESSAGE"`
- Install the last commit only: `hub deploy`
