---
title: No tooling traces
description: Commits, pull requests, and shipped files carry no sign of AI tooling.
all: true
ai: no
order: 2
---
This project must not show that AI tools were used.

- Commit messages contain only the change description. No AI attribution, no co-author trailers, no tool signatures, no generated-by footers.
- Pull request and merge request texts say what changed and why, in plain professional prose. No tooling self-references and no AI markers.
- Code, docs, changelogs, and shipped packages carry no hint of the tools used to write them.
- Claude files stay out of Git: `CLAUDE.md`, `CLAUDE.local.md`, and the `.claude` folder are never staged or committed. If one appears in `git status`, stop and say so.
- Before a commit or a pull request, read it as an outside reviewer would. If they could tell that an AI tool was involved, fix it first.
