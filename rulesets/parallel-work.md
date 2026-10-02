---
title: Parallel work
description: Worktrees, branches, and what several sessions share.
all: true
order: 3
---
Several agent sessions often work on one repository at the same time.

- One Git worktree per agent session. Never work in a checkout that another session uses: two sessions in one checkout share `HEAD` and the index, and commits land on the wrong branch.
- The main branch is never edited in place. Work on a branch and land it through the project's own flow.
- Never force-push. If the remote branch moved, merge or rebase onto it, then check that each of your commits survived.
- The stash is shared between all worktrees of a repository. Use a work-in-progress commit instead of `git stash`.
- Before you delete a folder that Git does not track completely, run `git status --ignored` on it.
