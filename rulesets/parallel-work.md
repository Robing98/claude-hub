---
title: Parallel work
description: Worktrees, branches, and what several sessions share.
all: true
order: 4
---
Several agent sessions often work on one repository at the same time.

- One Git worktree per agent session. Never work in a checkout that another session uses: two sessions in one checkout share `HEAD` and the index, and commits land on the wrong branch.
- The main branch is never edited in place. Work on a branch and land it through the project's own flow.
- Never force-push. If the remote branch moved, merge or rebase onto it, then check that each of your commits survived.
- The stash is shared between all worktrees of a repository. Use a work-in-progress commit instead of `git stash`.
- Before you delete a folder that Git does not track completely, run `git status --ignored` on it.
- A number that is claimed by counting up collides between branches: a schema version, a cache version, a section number. Check every branch before you take the next one, not only the main branch.
- A resource that a person holds and several sessions want counts as occupied until you can show that it is free: a test checkout, a device, a running server. When the state is unclear, ask.
- Before you split work across several agents, check that the parts touch different files.
- What another session needs to know goes into the repository or the hub, not into the memory of one agent.
