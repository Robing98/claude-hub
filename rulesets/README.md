# Rule sets

Each file in this folder is one rule set. The hub hands the sets to Claude Code sessions through the session hook. A change takes effect with the next deployment.

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

| Field | Meaning |
| --- | --- |
| `load` | `always` puts the set into every session start. `on-demand` lists it as one index line, and the agent loads it when the work needs it. The default is `always`. |
| `when` | For on-demand sets: when the agent has to load the set. |
| `all` | `true` applies the set to every project and to sessions outside a project. |
| `workspaces` | Workspace names, separated by commas. |
| `projects` | Projects, separated by commas: `host/owner/name` of the remote, or the project name. |
| `ai` | `no` limits the set to projects where AI traces in Git are not allowed, `yes` to the others. Without the field, the switch does not matter. |
| `order` | Position in the session text. Lower comes first. The default is 100. |

A set applies when `all` is true, or its workspaces contain the project's workspace, or its projects contain the project. The `ai` field then narrows that.

The file name without `.md` is the name that an agent uses to load the set.
