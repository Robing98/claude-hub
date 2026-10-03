# Rule sets

Each file in this folder is one rule set. The hub hands the sets to sessions: to Claude Code through the session hook, to Cowork through the local connector, and to other tools through an exported folder.

A set changes in one of two ways:

- **In the hub:** Edit the set on its page, or accept a rule proposal. The change applies from the next session start. Then run `hub rules-pull` here and commit, so that the repository holds the same text.
- **In this folder:** Edit the file and deploy. Run `hub rules-pull` first. A version that was changed in the hub wins over the file until the repository holds the same text.

## File format

```markdown
---
title: Shown as the heading
description: One line for the index of on-demand sets
load: always
when: Before you edit the frontend
all: true
workspaces: GolleIT, MIT Marburg
projects: github.com/owner/name
ai: no
order: 10
---
The rules, as Markdown.
```

Headings inside a set can start at any level. In the session text they are moved below the title of the set.

| Field | Meaning |
| --- | --- |
| `load` | `always` puts the set into every session start. `on-demand` lists it as one index line, and the agent loads it when the work needs it. The default is `always`. |
| `when` | For on-demand sets: when the agent has to load the set. |
| `all` | `true` applies the set to every project and to sessions outside a project. |
| `workspaces` | Workspace names, separated by commas. |
| `projects` | Projects, separated by commas: `host/owner/name` of the remote, or the project name. |
| `ai` | `no` limits the set to projects where AI traces in Git are not allowed, `yes` to the others. Without the field, the switch does not matter. |
| `order` | Position in the session text. Lower comes first. The default is 100. |

## Switches per project

Each project page in the hub has two switches:

- **Sessions get their rules from the hub:** Off by default. While it is off, a session in that project gets nothing from the hub and keeps the rule files of the project. Turn it on when the rules of the project are in this folder. A session outside any project always gets the sets with `all: true`.
- **AI traces in Git are allowed:** Off by default. It decides which sets with an `ai` field apply. It does not change the commit rule: no project gets tool attribution in commit messages.

## When a set applies

A set applies when `all` is true, or its workspaces contain the project's workspace, or its projects contain the project. The `ai` field then narrows that.

The file name without `.md` is the name that an agent uses to load the set.
