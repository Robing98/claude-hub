---
title: No tooling traces
description: Nothing in Git shows that AI tools were used.
all: true
ai: no
order: 3
---
This project must not show that AI tools were used.

- Code, docs, changelogs, pull request texts, and shipped packages carry no hint of the tools used to write them.
- Claude files stay out of Git: `CLAUDE.md`, `CLAUDE.local.md`, and the `.claude` folder are never staged or committed. If one appears in `git status`, stop and say so.
- Before a commit or a pull request, read it as an outside reviewer would. If they could tell that an AI tool was involved, fix it first.
