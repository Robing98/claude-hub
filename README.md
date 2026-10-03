# Claude hub

A self-hosted hub that collects Claude Code sessions and Git worktrees from several machines and accounts, and shows what needs you now.

Status: phase 1. See [the plan](docs/plan.md) for the goal, the design, and the next phases.

## What phase 1 does

- **Now page:** sessions that wait for your reply, sessions that run, sessions that stopped mid-turn, and worktrees with unsaved or unpushed work.
- **Hierarchy:** workspace > class > project. Rules assign projects by Git host and owner, or by path. Unmatched projects go to the inbox.
- **Private projects:** mark projects as private and hide their names with one switch, for screenshots and screen shares.
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

### Cowork sessions

The collector also uploads the Cowork sessions that ran on the computer. The Claude desktop app keeps their transcripts in its data folder, under `local-agent-mode-sessions`. They appear with the account label `cowork`.

- A Cowork session that worked in a repository is assigned to the project of that repository. The hub asks the collector about folders it does not know, and the collector checks whether they are repositories.
- A Cowork session that worked in a folder without Git gets a project for that folder: the connected folder, or the first folder below a container such as OneDrive or Documents. Assign such a project to a workspace in the inbox, or add a path rule under **Settings**.
- The remaining sessions share one project named "Cowork".
- The collector reads only the transcripts in those folders. It reads no audit log and no instruction file there, and it installs no hook there.
- To leave Cowork out, set `cowork = false` in the collector configuration. If the app keeps the sessions somewhere else, set `cowork_dir`.

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
| `hub rules-export [DIR]` | Write the rules of a project into its folder, hidden from Git |
| `hub rules-pull` | Bring rule sets that were changed in the hub into `rulesets/` |
| `hub mcp-install` | Give Cowork the hub tools through the Claude desktop app |
| `hub handoffs` | Show the handoffs of the project of this folder |
| `hub wake NAME` | Let the hub wake the machine `NAME` over the network |
| `hub unlock [DIR]` | Remove Git lock files that were left behind |
| `hub google-add LABEL` | Sign a Google account in for the hub, in the browser |
| `hub google-list` | Show the Google accounts of the hub |

The server address and the container ID are set at the top of `hub.bat`.

## Rules from the hub

1. Write or change the rule sets in `rulesets/` and deploy.
2. On each machine, install the session hook once: `hub hooks-install`. Inside WSL: `hub wsl-hooks-install`. The command adds one entry to the Claude Code `settings.json` and keeps a backup beside it. `hub hooks-remove` undoes it.
3. On the project page in the hub, turn on **Sessions get their rules from the hub**. Until then, a session in that project gets nothing from the hub, so a project with its own rule files does not load two sets of rules.
4. Start a new Claude Code session. Its first context contains the rules of the project.
5. To see what a session receives in a folder, run `hub rules` there. `hub rules NAME` shows one set.

The **Rules** page lists all sets with their size and shows, per project, how much a session carries at its start.

Old `CLAUDE.md` files can stay on disk. To stop Claude Code from loading them as well, add them to `claudeMdExcludes` in the Claude Code settings.

### Change a rule

- **Edit in the hub:** Open a set on the **Rules** page and change the file text under **Edit**. The change applies from the next session start, without a deployment.
- **Proposals:** A session cannot change a rule. It proposes the change, and you accept or reject it under **Rules** > **Rule proposals**. You can change the wording before you accept. A Claude Code session proposes with `rules propose`, a Cowork session with the tool `hub_propose_rule`.
- **Back into the repository:** A set that was changed in the hub is marked "changed in the hub". Run `hub rules-pull`, review the files in `rulesets/`, and ship. After that deployment the mark is gone.

### Rules for Cowork

Cowork cannot run the session hook. It gets the rules through a local connector that the Claude desktop app starts.

1. Run `hub mcp-install`. The command adds the entry `claude-hub` to `claude_desktop_config.json` of the desktop app and keeps a backup beside it. `hub mcp-remove` undoes it.
2. Quit the Claude desktop app completely and start it again.
3. In a Cowork chat, the tools `hub_rules`, `hub_ruleset`, and `hub_propose_rule` are available. The connector tells the chat to call `hub_rules` with the project folder before it works there.

The connector runs on your computer, so it reaches the hub only while that computer reaches it.

The hub shows these steps, the tools, and what to check when something fails under **Settings** > **Cowork connector (MCP)**. That page also shows when the connector of each machine last fetched rules.

### Rules as files, outside Git

For a tool that has neither the hook nor the connector, export the rules into the project folder:

1. Turn on **Sessions get their rules from the hub** for the project.
2. Run `hub rules-export DIR`, where `DIR` is the project folder. Without `DIR`, the command uses the current folder.

The command writes `.claude-hub/RULES.md` and one file per on-demand set under `.claude-hub/rules/`. It adds `/.claude-hub/` to `.git/info/exclude`, which is local to your clone, so the repository shows no trace of the export. Every collector run refreshes an export that exists. A linked worktree needs its own export.

## Sort by hand

- **Assign:** On a project page or in the inbox, choose a workspace or class. A choice made by hand wins over the rules.
- **Pin and order:** In the overview, select **Pin**. Pinned projects lead their group. **Up** and **Down** change their order.
- **Archive:** On a project page, select **Archive**. The project leaves the overview, the inbox, the Now page, and the rule table. Its usage stays in the totals. The overview lists archived projects at the end, with **Restore**.
- **Merge:** On the page of the duplicate, choose the project to keep and select **Merge**. Sessions, worktrees, and instruction files move over. Later uploads for the duplicate land in the kept project. **Separate again** on the kept project ends that.
- **Move a session:** On a session page, choose a project and select **Move**. The session stays there, whatever the collector reports later. **Assign automatically** returns it to the rules.

## Hide private projects

For a screenshot or a screen share, some project names must not show.

1. On the page of such a project, select **Mark as private**.
2. In the top bar, select **Hide private projects**. The switch appears as soon as one project is private, and it applies to your browser only.

While the switch is on, every list shows a private project as "Project n": the overview, usage, Now, sessions, worktrees, the rule tables, and the inbox. Its remote, paths, branch names, commit subjects, and session titles are hidden as well, and a search does not find its sessions. The number stays the same from page to page. The page of the project itself, and the page of one of its sessions, show everything.

The status feed has no browser. Add `?hide=1` to its address to get the masked form.

## Usage and cost

The **Usage** page shows the tokens that sessions used, per project, workspace, model, and day. Subagents count for the session that started them.

The cost is an API-equivalent: what the same tokens cost on the API. A subscription is a flat fee, so the number compares projects and days. It is not a bill.

- Prices are in `pricing.toml`, in USD per million tokens. Edit the file when prices change, then deploy. The hub applies the current file to all stored usage.
- A model without an entry in `pricing.toml` is listed with "no price", and its tokens are missing from the cost.
- To compare the last 30 days with what you pay, set `plan_usd_per_month` in `pricing.toml`.
- Days are UTC days.

## Calendar and mail

The Google connection of a Claude app holds one Google account. The hub holds as many as you sign in, so that every session reaches the calendars of a private and a work account through one place.

- The **Calendar** page lists the events of every account in order of time, and the events that sessions propose.
- Sessions use the connector tools: `calendar_list`, `calendar_agenda`, `calendar_propose_event`, `calendar_create_event`, `calendar_update_event`, `mail_search`, and `mail_read`.
- A proposed event reaches a calendar only when you accept it on the **Calendar** page. You can change it first. A session writes an event at once only when you confirmed that event in the chat.
- The hub creates and changes events. It deletes none. It reads mail. It sends mail only for a routine that you set to send. No session and no connector tool can send mail.
- **Settings** > **Google accounts** holds the setup steps, the state of each account, and the last activity.

To connect an account:

1. Create a Google Cloud project with the Calendar API, the Gmail API, and a client of the type **Desktop app**. The settings page lists the steps.
2. Run `hub google-add private --client PATH_TO_CLIENT_JSON`. Replace `private` with a label for the account and `PATH_TO_CLIENT_JSON` with the downloaded client file. The file is needed once per computer.
3. Sign in at Google in the browser window that opens. The command hands the sign-in to the hub and prints the address of the account.

The sign-ins are in `google.json` in the data folder, readable by the service user only. Times without a time zone count as `Europe/Berlin`. Set `HUB_TIME_ZONE` on the server to change that.

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

## Handoffs

A handoff is a text that one session leaves for another session of the same project, or that you leave for a session. Example: the design lane of a project runs in Cowork and writes a brief. The code lane runs in Claude Code and receives the brief with its next prompt. Nobody copies text between two apps.

- A lane is a free name for a line of work in a project, such as `design` or `code`. A handoff can name the lane it is meant for.
- Claude Code: a hook delivers open handoffs of the project with the next prompt, once per session. Run `hub hooks-install` again after this update, because the hook is new. The session takes a handoff with `handoff show ID --take` and finishes it with `handoff done ID "RESULT"`. It sends one with `handoff send "TITLE" --file FILE --to LANE`.
- Cowork: the connector tools `handoff_send`, `handoff_list`, `handoff_take`, and `handoff_done`. `hub_rules` also lists what waits in the project.
- You: the **Handoffs** page shows every handoff and has a form to leave one.
- A handoff is `open`, then `taken` by one session, then `done`. A second session that tries to take it is told that it came too late.
- The prompt hook asks the hub before every prompt. It waits at most three seconds. If the hub does not answer, it stops asking for five minutes.

## Routines

A routine is something that comes back on a schedule: a checkup every six months, an appointment once a year, a new prescription every twelve weeks.

- A routine has a next due day and a lead time. From the lead time on, it shows on the **Routines** page, on **Now**, and in the status feed as `routines_due`. The feed carries only the number, never a name.
- **Done** sets the next due day. For an appointment, the interval counts from the day it was done. For a supply that runs out, it counts from the due day, no matter when you acted.
- A routine can carry a prepared mail. Who sends it is a choice per routine, which only you make on the page:
  - You do: the page offers a link that opens the mail in your mail program. This is the default.
  - The hub, when you press **Send**.
  - The hub, by itself, from the first day of the lead time.
- The hub sends only from a Google account where you ticked **Send email on your behalf** at sign-in, only to the one address of the routine, and once per round. By itself it sends between 8:00 and 19:00. After a failed attempt it waits for you. **Settings** > **Google accounts** lists every sent mail.
- A session can draft a routine with the connector tool `routine_draft`. A draft is turned off and never sends, until you change it.
- The **Routines** page shows no names while "Hide private projects" is on.

## Wake a computer

The hub server is always on, so it can wake a computer that sleeps or is shut down (Wake-on-LAN).

- The collector reports the network adapters of its machine. **Settings** > **Devices** shows them, picks the one connected cable adapter by default, and has a **Wake** button.
- From another machine: `hub wake NAME`. From a session: the connector tool `machine_wake`.
- The computer must allow waking in its firmware and in Windows. The **Devices** page lists the steps.
- The signal reaches only computers in the same network as the hub server.

## Git locks

Git creates `index.lock` while it writes and removes it when it is done. A Git command that is killed, or that runs where files cannot be deleted, leaves the file behind. Every later Git command in that repository then fails with "index.lock: File exists". The shell that Cowork uses on your computer is such a place.

- Each collector run looks for lock files in every repository and worktree that it knows. It removes a lock that is older than five minutes, if the lock is empty or no Git process runs on the machine.
- To remove stale locks at once, run `hub unlock`, or `hub unlock DIR` for one repository. The limit is 15 seconds there. Add `--force` to remove a lock with content while a Git process runs.
- A Cowork chat removes its own leftovers with the connector tool `hub_unlock`.
- The **Worktrees** page lists the locks of the last 30 days, per repository and machine. A lock that could not be removed shows on the **Now** page and in the status feed as `stuck_locks`.
- To only report locks, set `clean_locks = false` in the collector configuration. `lock_stale_minutes` sets the age limit.

The collector itself never takes a Git lock: it runs Git with `GIT_OPTIONAL_LOCKS=0`.

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
- The web view has no sign-in unless `HUB_UI_PASSWORD` is set. It refuses forms that another website sends, and nothing more. Do not expose the server to the internet.
- The data folder holds the sign-ins of the Google accounts. Whoever reads `google.json` can read that mail and write to those calendars. Keep backups of the data folder as safe as the server.
- Everyone who reaches the web view can set up a routine that sends mail from an account that may send. If other people share the network, set `HUB_UI_PASSWORD`, or leave the box for sending unticked.
- A handoff is an instruction to a session. Every machine with a token can file one, and so can everyone who reaches the web view.
- The API for Google needs the token of a machine. A session on any machine with a token can read the mail of every connected account.
- Back up the `hub-data` volume. After Claude Code deletes old transcripts (30 days by default), the server holds the only copy.

## Limits

- The transcript format is internal to Claude Code and can change. The parser skips what it does not know. After a parser update, run `claude-hub reparse` to rebuild the session fields from the stored transcripts.
- Sessions of the desktop app, claude.ai chats, and artifacts are not collected yet.
- Subagent transcripts are stored for their token usage. They cannot be read in the browser yet.
- Usage covers Claude Code and the Cowork sessions that ran on your computers. Cowork sessions that ran in the cloud, chat, and Claude Design use the same subscription limits and are not counted.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
.venv/bin/pytest
.venv/bin/claude-hub serve
```

On Windows, the commands are in `.venv\Scripts`.
