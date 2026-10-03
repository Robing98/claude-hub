# Claude hub

A self-hosted hub that collects Claude Code sessions and Git worktrees from several machines and accounts, and shows what needs you now.

Status: phase 1. See [the plan](docs/plan.md) for the goal, the design, and the next phases.

## What phase 1 does

- **Now page:** sessions that wait for your reply, sessions that run, sessions that stopped mid-turn, and worktrees with unsaved or unpushed work.
- **Hierarchy:** workspace > class > project. Rules assign projects by Git host and owner, or by path. Unmatched projects go to the inbox.
- **Order by hand:** pin projects to the top of their group and set their order, archive projects that are done, merge duplicates, and move a single session into another project.
- **Sessions:** every Claude Code transcript, stored in full and readable in the browser.
- **Worktrees:** one state per worktree: `dirty`, `unpushed`, `missing`, `stale`, `merged`, `idle`, `active`, or `main`.
- **Rules:** every instruction file that Claude Code loads, from all machines: `CLAUDE.md`, `.claude/rules/`, skills, subagents, commands, and output styles. `CLAUDE.md` and rule files are split into single rules by heading. Copies that differ are marked.
- **Rule sets:** the hub hands each Claude Code session the rules of its project. A hook asks the hub at session start, and a local copy covers the time when the hub is not reachable. Sets marked "on-demand" cost one index line until the work needs them. See [the rule set format](rulesets/README.md).
- **Two switches per project:** whether sessions get their rules from the hub, and whether the project may carry AI traces in Git. Both are off by default.
- **Usage:** tokens per project, workspace, model, and day, with subagents, and what the same tokens cost at API prices.
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

This command has not been verified on Windows yet. Repositories inside WSL need their own collector inside WSL, with its own token. `hub wsl-setup` installs it. Each collector reports only the worktrees of its own system.

## Shortcuts on Windows

`hub.bat` in the repository folder wraps the commands. It uses the right Python on each machine. Run `hub` without arguments for the full list.

| Command | Effect |
| --- | --- |
| `hub collect` | Upload sessions, worktrees, and rules once |
| `hub check` | Test the connection and the configuration |
| `hub loop` | Upload every five minutes |
| `hub config` | Open the collector configuration |
| `hub wsl-setup` | Install or update the collector inside WSL |
| `hub wsl-collect` | Upload from WSL once |
| `hub rules` | Show the rules that a session in this folder receives |
| `hub hooks-install` | Make Claude Code sessions fetch their rules from the hub |
| `hub token NAME` | Create the token for the machine `NAME` |
| `hub ship "TEXT"` | Commit with the message `TEXT`, push, and deploy |
| `hub deploy` | Install the last commit on the server |

The server address and the container ID are set at the top of `hub.bat`.

## Rules from the hub

1. Write or change the rule sets in `rulesets/` and deploy.
2. On each machine, install the session hook once: `hub hooks-install`. Inside WSL: `hub wsl-hooks-install`. The command adds one entry to the Claude Code `settings.json` and keeps a backup beside it. `hub hooks-remove` undoes it.
3. On the project page in the hub, turn on **Sessions get their rules from the hub**. Until then, a session in that project gets nothing from the hub, so a project with its own rule files does not load two sets of rules.
4. Start a new Claude Code session. Its first context contains the rules of the project.
5. To see what a session receives in a folder, run `hub rules` there. `hub rules NAME` shows one set.

The **Rules** page lists all sets with their size and shows, per project, how much a session carries at its start.

Old `CLAUDE.md` files can stay on disk. To stop Claude Code from loading them as well, add them to `claudeMdExcludes` in the Claude Code settings.

## Sort by hand

- **Assign:** On a project page or in the inbox, choose a workspace or class. A choice made by hand wins over the rules.
- **Pin and order:** In the overview, select **Pin**. Pinned projects lead their group. **Up** and **Down** change their order.
- **Archive:** On a project page, select **Archive**. The project leaves the overview, the inbox, the Now page, and the rule table. Its usage stays in the totals. The overview lists archived projects at the end, with **Restore**.
- **Merge:** On the page of the duplicate, choose the project to keep and select **Merge**. Sessions, worktrees, and instruction files move over. Later uploads for the duplicate land in the kept project. **Separate again** on the kept project ends that.
- **Move a session:** On a session page, choose a project and select **Move**. The session stays there, whatever the collector reports later. **Assign automatically** returns it to the rules.

## Usage and cost

The **Usage** page shows the tokens that sessions used, per project, workspace, model, and day. Subagents count for the session that started them.

The cost is an API-equivalent: what the same tokens cost on the API. A subscription is a flat fee, so the number compares projects and days. It is not a bill.

- Prices are in `pricing.toml`, in USD per million tokens. Edit the file when prices change, then deploy. The hub applies the current file to all stored usage.
- A model without an entry in `pricing.toml` is listed with "no price", and its tokens are missing from the cost.
- To compare the last 30 days with what you pay, set `plan_usd_per_month` in `pricing.toml`.
- Days are UTC days.

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
- Subagent transcripts are stored for their token usage. They cannot be read in the browser yet.
- Usage covers Claude Code only. Cowork, chat, and Claude Design use the same subscription limits and are not counted.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
.venv/bin/pytest
.venv/bin/claude-hub serve
```

On Windows, the commands are in `.venv\Scripts`.
