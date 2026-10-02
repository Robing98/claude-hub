# Claude hub

A self-hosted hub that collects Claude Code sessions and Git worktrees from several machines and accounts, and shows what needs you now.

Status: phase 1. See [the plan](docs/plan.md) for the goal, the design, and the next phases.

## What phase 1 does

- **Now page:** sessions that wait for your reply, sessions that run, sessions that stopped mid-turn, and worktrees with unsaved or unpushed work.
- **Hierarchy:** workspace > class > project. Rules assign projects by Git host and owner, or by path. Unmatched projects go to the inbox.
- **Sessions:** every Claude Code transcript, stored in full and readable in the browser.
- **Worktrees:** one state per worktree: `dirty`, `unpushed`, `missing`, `stale`, `merged`, `idle`, `active`, or `main`.
- **Rules:** every instruction file that Claude Code loads, from all machines: `CLAUDE.md`, `.claude/rules/`, skills, subagents, commands, and output styles. `CLAUDE.md` and rule files are split into single rules by heading. Copies that differ are marked.
- **Several machines and accounts:** one collector per machine, one entry per Claude Code configuration directory.

## How it works

| Part | Runs on | Purpose |
| --- | --- | --- |
| Server | Home server | API, SQLite database, web view |
| Collector | Every machine | Uploads transcripts and reports repositories and worktrees |

- The collector needs Python 3.10 or later and Git. On Python 3.11 or later it has no other dependencies.
- The collector is read-only. It never fetches, commits, or pushes, and it takes no Git locks.
- From the Claude Code configuration directory, the collector reads only transcripts and Markdown instruction files. It never opens settings or credential files.
- A project is identified by its normalized remote URL, for example `github.com/owner/name`. The same repository on two machines is one project. Credentials in a remote URL are removed before anything is sent.
- Uploads are incremental. The collector asks the server what it holds and sends only new lines.

## Set up the server

**On Proxmox (recommended):** see [Deploy on Proxmox](docs/deploy-proxmox.md). The hub runs as a plain service in its own container, and one command from Windows installs or updates it.

**On any machine with Python:**

```bash
python -m pip install ".[server]"
claude-hub token add MACHINE --user USER
claude-hub serve --host 0.0.0.0
```

Replace `MACHINE` with a name for the machine that runs a collector, for example `desktop`, and `USER` with your name. The token is shown only once. Then open `http://SERVER:8787/settings`, where `SERVER` is the address of the server, and create your workspaces, classes, and rules.

**With Docker:** `docker compose up -d --build`, then `docker compose exec hub claude-hub token add MACHINE --user USER`. The image build is not verified yet.

## Set up a collector

1. Install the package from a clone of this repository:

   ```bash
   python -m pip install .
   ```

2. Write the example configuration:

   ```bash
   claude-hub-collector init
   ```

   The command prints the path of the file.

3. Edit the file. Set `server_url` and `token`. Add one `[[accounts]]` entry per Claude Code configuration directory. Optional: add folders that contain repositories to `scan_roots`.
4. Test the connection:

   ```bash
   claude-hub-collector check
   ```

5. Run the collector every five minutes:

   ```bash
   claude-hub-collector run --interval 300
   ```

To start the collector at logon on Windows without a console window, create a scheduled task:

```powershell
schtasks /Create /TN "Claude hub collector" /SC ONLOGON /TR "pythonw -m claude_hub.collector.cli run --interval 300"
```

This command has not been verified on Windows yet. Repositories inside WSL need their own collector inside WSL, with its own token.

## Worktree states

| State | Meaning |
| --- | --- |
| `dirty` | Changed or untracked files. Linked worktrees inside the folder do not count. |
| `unpushed` | Commits that are not on the remote, or a branch without an upstream. |
| `missing` | Git knows the worktree, but the folder is gone. |
| `stale` | No commit for 30 days, and not merged. |
| `merged` | The branch is contained in the default branch. It is safe to remove. |
| `idle` | Last commit 8 to 30 days ago. |
| `active` | Last commit in the last 7 days. |
| `main` | The default branch, clean and pushed. |

Squash merges are not detected yet, because that needs the pull request state from the Git host.

## Session states

| State | Meaning |
| --- | --- |
| `waiting` | The agent finished its turn or asked a question, in the last 3 days. |
| `running` | Activity in the last 10 minutes. This is based on the last upload, not on a live process check. |
| `paused` | The turn stopped in the middle: interrupted, crashed, or held by a permission prompt. |
| `idle` | Everything older than 3 days. |

## Security

- Transcripts contain code and can contain secrets. Keep the server inside your VPN, and set `HUB_UI_PASSWORD` if other people share that network.
- Each collector token identifies one machine. The server stores only a hash of it.
- The forms of the web view have no CSRF protection. Do not expose the server to the internet.
- Back up the `hub-data` volume. After Claude Code deletes old transcripts (30 days by default), the server holds the only copy.

## Limits

- The transcript format is internal to Claude Code and can change. The parser skips what it does not know. After a parser update, run `claude-hub reparse` to rebuild the session fields from the stored transcripts.
- Sessions of the desktop app, claude.ai chats, and artifacts are not collected yet.
- Subagent transcripts are not uploaded yet.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
.venv/bin/pytest
.venv/bin/claude-hub serve
```

On Windows, the commands are in `.venv\Scripts`.
