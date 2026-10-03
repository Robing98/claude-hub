# Claude hub: plan

Status: 2026-10-02. Phase 1 is built. The other phases are planned.

## Goal

One central place for all Claude-assisted work across machines and accounts, so that more projects can run in parallel without losing the overview.

- Collect every session from every machine and account.
- Sort it into a fixed hierarchy.
- Show a summary, open to-dos, and the next step per session and per project.
- Make the project context available to any agent on any machine.
- Track in-flight work (worktrees, branches) and run routine jobs automatically.

## Design principle

The goal is more finished work per day with less exhaustion in the evening. Parallel agents produce more output, and each one costs attention: switching, holding state in your head, and reviewing results. The tool therefore optimizes for fewer switches and fewer decisions per day, not for more parallel sessions.

- The start page is a short list of what needs you now, not the full hierarchy.
- Rules sort projects, so the inbox stays small.
- Questions from agents are collected and answered in batches (decision queue).
- Later: a limit on parallel work per day and an end-of-day summary.

## Hierarchy

1. Workspace: MIT Marburg, GolleIT, university, private.
2. Project class: a grouping inside a workspace.
3. Project: one repository or one topic. Sessions without a project go to "loose chats".

Unmatched items go to an inbox and are assigned by hand once. The assignment becomes a rule.

## Architecture

| Component | Runs on | Purpose |
| --- | --- | --- |
| Server | Proxmox host, own LXC container, plain service without Docker | API, database, web view, job queue |
| Collector | Every machine | Sends sessions and the repository and worktree inventory to the server |
| Runner | Machines that can do the work (desktop first) | Executes queued jobs with Claude Code, holds the machine awake while jobs run |
| MCP server | Part of the server | Gives agents the project context and takes handoffs |

- Stack: Python, FastAPI, SQLite.
- Network: the existing VPN. No public endpoint. Consequence: Claude Code and the desktop app reach the MCP server, chat on the web or phone does not.
- Each collector and runner authenticates with its own token.
- Backup: regular backup of the container to a second disk or machine. After Claude Code deletes local transcripts, the server holds the only copy.

## Sources

| Source | Access | Phase |
| --- | --- | --- |
| Claude Code CLI transcripts | JSONL files in `~/.claude/projects/` (or `CLAUDE_CONFIG_DIR`). Hooks `SessionStart` and `SessionEnd` can send an HTTP request. | 1 |
| Git repositories and worktrees | Local Git commands | 1 |
| Desktop app sessions (Cowork, Code tab) | Stored locally, format undocumented | 4 |
| claude.ai chats, projects, artifacts, design systems | No API on Pro or Max. Import from the data export, or manual links. | 4 |
| Claude Code cloud sessions | No documented list API | Open |

Transcript fields observed in a real session file on 2026-10-02: `sessionId`, `cwd`, `gitBranch`, `timestamp`, `type`, `aiTitle`, `isSidechain`, `version`. The format is internal and can change, so the parser ignores unknown fields and keeps the raw line.

Retention: Claude Code deletes transcripts after 30 days by default (`cleanupPeriodDays`). The collector copies them before that.

## Project identity and Git hosts

- The collector scans configured root folders, for example `D:\dev`, and finds every repository and worktree.
- The project identity is the normalized remote URL: host, owner, and name. The same repository on two machines maps to the same project without setup.
- Any Git host works: GitHub, GitLab, and self-hosted instances. The inventory uses the local Git credentials, so the server needs no tokens for it.
- Workspace rules match on host and owner, for example a self-hosted GitLab host maps to one workplace.
- Optional per-host API token for pull request and merge request status.

## Accounts and users

- Data model: user > account > machine > session.
- Claude Code separates accounts on one machine through `CLAUDE_CONFIG_DIR`. The collector has one entry per config directory, with an account label.
- Two accounts that work on the same repository appear under the same project.
- Session history, claude.ai projects, and memory stay per account. Context moves between accounts through the hub and through files in the repository.
- Open: whether a local process can read the account ID reliably. Until then the label is set in the collector configuration.

## Worktree inventory

| State | Condition | Action |
| --- | --- | --- |
| Active | Commit in the last 7 days | None |
| Dirty | Uncommitted changes | Commit or discard |
| Unpushed | Commits that are not on the remote | Push |
| Merged | Branch is contained in the main branch, or its pull request is merged | Candidate for deletion |
| Stale | No commit for 30 days, not merged | Decide: continue or drop |

- Squash merges need the pull request state, because the commit is never contained in the main branch.
- Each worktree links to its sessions through the working directory.
- Git is the store for in-flight work. Large files outside Git go to a Syncthing folder later.

## Automation

1. Housekeeping without an agent: nightly fetch, classify, push unpushed branches, list merged worktrees. Nothing is deleted without confirmation.
2. Agent reads only: per stale worktree, a headless run writes what is done, what is missing, and the next step.
3. Agent continues the work: a queue with a limit on parallel runs. The result is always a pull request or a report, never a merge.

Job types for level 3:

- Resolve an issue: a labeled issue starts a run in a fresh worktree. The result is a pull request that references the issue.
- Shopware compatibility check: a new Shopware release starts one run per plugin. The run raises the version constraint in a worktree, runs static analysis and tests against the new version, reads the upgrade notes, and writes a report or a merge request.
- Continue a stale worktree.

Conditions:

- Level 3 needs an automated check per repository (build and tests). Without one, the result needs manual verification.
- Runs use the rate limits of the account that executes them.
- Jobs run on the desktop, not on the Proxmox host. The host is too small for builds.

## Decision queue

1. An agent hits a question. Instead of waiting in its terminal, it files the question in the hub through the MCP server, with 2 to 4 options and a recommended one.
2. The agent continues with work that the question does not block, or ends its run.
3. One condensed list shows all open questions, each with one line of context. You select an option per question.
4. The hub hands the answer back: the runner resumes that session, or the next session on that project receives it at start.

- Each entry has a "needs discussion" option that opens the session instead.
- Irreversible steps never run from the queue without a pull request.
- Cowork cannot be controlled by the hub. Cowork and Claude Code share the queue through the MCP server: one files a task or handoff, the other picks it up.

## Review loop

A switch per project or topic lets two agents pass results back and forth, for example Claude Code with the repository and Cowork with Jira and design tools.

- The hub counts the rounds per topic and accepts no more than two or three. After that, the agent continues with its best judgment, or the open point goes into the decision queue.
- Claude Code side: a `Stop` hook asks the hub for a reply and feeds it in as the next instruction.
- Cowork side: Cowork fetches and files messages through the MCP server. It needs a trigger, because nothing can write into a Cowork chat from outside.
- Open: direct messaging between Claude sessions exists as a tool. It is untested for this purpose.

## Rule sets

One place for all instructions, grouped by purpose and delivered per project, role, or task. The aim is that each session loads few and relevant rules, not all of them.

- Import, read-only: the collector reports the instruction sources per machine, account, and repository: `CLAUDE.md` files, `.claude/rules/`, skills, and subagents. The hub lists them and marks copies that have drifted apart.
- Group: rule sets by purpose, assigned to a workspace, class, or project. An assignment applies to everything below it.
- Deliver through the mechanisms that Claude Code already has:
  - By project: files in `.claude/rules/`, optionally limited to matching files with `paths:`.
  - By role: subagent definitions that preload the skills of that role.
  - By task: skills. Claude loads a skill when its description matches the task, so no own classifier is needed.
- Distribute: a private Git repository as a plugin marketplace. The same plugin installs in Claude Code and in Cowork.
- The hub writes only into folders that it owns and never edits hand-written files.

Built so far:

- Rule sets are Markdown files in `rulesets/` of the hub repository. The hub is their only source.
- Each project has two switches, both off by default. "Rules from the hub" decides whether a session gets hub rules at all, so that a project is moved on purpose and never loads two sets of rules. "AI traces in Git" decides which sets with an `ai` field apply.
- No project gets tool attribution in commit messages. That rule is its own set and does not depend on a switch.
- A `SessionStart` hook fetches the rules of the project from the hub and keeps a local copy for the time when the hub is not reachable.
- Sets marked "on-demand" appear as one index line. The agent loads a set with a command when the work needs it.
- Consolidated so far: five shared sets, the hub's own set, and Orbis split into 14 sets without a change of wording.
- The shared sets carry the lessons of the "Sixty Worktrees, One Slot" notes: shared counters, held resources, detector precision, preconditions.

Two questions that are independent of each other:

- Where the rules live. Target: in the hub, for every project. A `CLAUDE.md` in Git is then no longer a source.
- Whether Git may show that AI tools were used: author names, docs that name the tools, Claude files. That is the AI switch. Orbis is "allowed", the synchrotron repository is "not allowed".

Decided on 2026-10-03: the hub generates the rule file for the lanes that cannot run the hook, and rule files move out of Git.

Next, rules outside Git:

- An export command writes the rules of a project into its folder as a generated file. The file is hidden from Git through `.git/info/exclude`, which stays local and leaves no trace in the repository. This is a write into a working tree, so it is its own command and never part of a collector run.
- Claude Code skips the generated file through `claudeMdExcludes` and takes the hook briefing, which is smaller.
- Cowork does not load a `CLAUDE.md` from a connected folder by itself. It needs a pointer: one line in the global instructions, or a skill, that says to read the generated file first. Target: the hub as a local MCP server in the desktop app, so that a Cowork chat fetches rules and loads sets on demand as Claude Code does.
- Orbis: stop tracking `CLAUDE.md` once the export works. Until then that file wins over the hub sets, because the design lane still edits it.

Open:

- How a lane changes a rule when the rules live in the hub. Today the Orbis design lane edits `CLAUDE.md` itself. Candidates: a rule proposal in the decision queue, or rule sets that are edited in the hub with their own history and no deploy.
- Rule proposals from session outputs: find the corrections that repeat across sessions, and propose a rule for each. Needs the summary step of phase 3.
- The synchrotron and GolleIT rules are not consolidated yet.

## Usage and cost

Built: token counts per session, day, and model from the transcripts, subagents included. The usage page shows them per project, workspace, model, and day with an API-equivalent cost from `pricing.toml`.

Open:

- Cowork, chat, and Claude Design are not counted. They use the same subscription limits.
- A view against the limits of the plan: how much of the week is used, and by which project.
- Cost per result: per merged pull request, per roadmap item, per content unit.

## Ideas from 2026-10-03

- The hub as the main way to use Claude: see, decide, and adjust everything there. This needs the decision queue and the runner, in that order.
- Project overview with pipelines: the stages of a project and what is in each stage.
- Progress meters for content. This needs a framework first: what a unit of content is, how its size is estimated, and when it counts as done. The Orbis content map and progression map are the first test case.
- An end-of-day page in the shape of the "Schichtende" page: done, next in order with an effort estimate, waiting for others.

## Roadmap per project

Projects that are marked as deep get a roadmap. Loose chats and small projects stay without one.

- A roadmap has milestones and items. Each item has acceptance criteria, dependencies, and a status.
- Agents take work through the MCP server. An agent claims the next free item, so two agents never take the same one. The result is a pull request that links back to the item.
- Where a tracker exists, such as Jira or GitHub issues, the item links to the ticket. The hub does not replace the tracker.
- Open: whether the roadmap lives in the hub database or as a file in the repository that the hub reads.

## Phases

1. Built: server, collector for Claude Code sessions, worktree inventory, "Now" page, hierarchy view, rules, inbox.
   Also built: the read-only import of instruction files with the "Rules" page.
2. MCP server with the decision queue, handoffs, and the review loop, and a `SessionStart` hook that loads the project briefing.
3. Summaries, to-dos, and next steps per session and project. End-of-day summary.
4. Rule sets: grouping by purpose, assignment, and distribution.
5. Roadmap per project, served to agents through the MCP server.
6. More sources: desktop app sessions, claude.ai export, artifacts.
7. Runner and automation levels 1 to 3, with a limit on parallel work.

Rule sets are chosen as the next step. The order of the other phases is open.

## Decisions

- Hub runs on the existing Proxmox host as a plain service in its own container. The host has an i5-6600K, 8 GB of memory, and one 120 GB SSD, so Docker and agent sessions do not go there. No hardware purchase for the hub.
- No API usage besides the subscription. Robin uses one Max 20x plan. Everything that needs a model, summaries included, runs as a Claude Code session on that plan, started by the runner on the desktop. The i5 host has no GPU, so a local model there is not an option.
- No own file sync. Git for code, Syncthing for the rest.
- No local model for code search for now. A local embedding index as an MCP tool is the better option if token use for exploring becomes a problem.
- Heavy work stays on the desktop. A larger home server is a later option for availability around the clock.

## Open points

- Summaries draw from the same plan limits as the work itself. Decide how much of the limit they may use, and whether a smaller model is enough.
- A view against the plan limits needs the limit state. The transcripts hold token counts, not the share of the five-hour or weekly limit that is used. Find out whether Claude Code exposes that state.
- Whether the Orbis repository has an automated check that level 3 can rely on.
- Terms for a second account held by the same person. The consumer terms forbid sharing an account. A statement on one person with two accounts was not found.
- How headless runs are billed. On 2026-10-02 the support article says they draw from the subscription limits, and that an announced change to a separate monthly credit is paused.
