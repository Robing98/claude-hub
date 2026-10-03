---
title: Orbis: sessions, claims, worktrees
description: The current phase, how a session starts and ends, claiming a round, one worktree per round.
load: always
projects: github.com/robing98/orbis
order: 23
---
## 6. Current phase

See `docs/roadmap.md`. Work top down and do not skip ahead: every milestone below
milestone 5 is architecture that cannot be retrofitted, and milestone 5 onward is
ordinary game work that can be reordered freely.

Milestone 0 is the current one unless `docs/roadmap.md` says otherwise, and it
says otherwise: milestone 1 is done and milestone 3 is being taken before
milestone 2. That is a deliberate reorder, not skipping ahead. Seven items of
milestone 1 stopped against the same wall, the registry is the only thing that
moves it, and milestone 2 adds nothing to it. The roadmap carries the reason.

Design rounds that are written into data and docs but not yet implemented
wait in `docs/handoff.md`, in order. Take the next entry there when the
current task is done, and delete it when its commit lands. The data and
docs an entry names are committed with the entry's code, in the same
commit or the one before it; the design side commits its own deploys as
they land, so the tree is never carrying weeks of uncommitted design.

**Sessions.** One hand-off entry per session, and the session starts with
these rules, the entry, and the docs the entry names, nothing else. A session
that has used two thirds of its context finishes the commit it is on,
writes its round report (what landed with commit ids, what is open, what
comes next), and stops; the next session starts fresh from the report and
the queue. Compaction is for a debugging trail with no commit boundary to
stop at, never a way to keep going. A design question found while
implementing goes back to the design chat, never into a workaround. A bug
found while implementing, in the entry's own code or in anything else the
tree already carries, is always in scope: fix it in the same session,
commit it (its own commit, separate from the entry's), and say so in the
round report. It does not wait for a queue entry of its own and does not
need Robin's go-ahead first.

**Claim a round before starting it.** Sessions run side by side, so two can
pick up the same subject from the same report. Before a round's first real
commit, add one line to `docs/board.md` under In hand, `- YYYY-MM-DD  <lane>
<what>`, and commit that line alone; delete it when the round lands. A claim is
good for the day it was made and the day after, and `tools/check_claims.py`
refuses an older one: release it or renew the date. It is a courtesy with a
check, not a lock, and the thing that actually prevents a bad merge is still
that main is only ever fast-forwarded.

**Every round works in its own worktree.** A round cuts a branch and a
worktree from main, commits there, runs the gate there, and main is only ever
fast-forwarded, never edited in place. `docs/worktrees.md` has the layout and
the commands. That holds for design and Cowork rounds as much as code ones. A
round that edits main directly parks its changes in the one checkout every
other round has to merge through, and then every merge collides with
whichever of them the incoming branch happens to touch: on 2026-09-14 that
cost three blocked merges, a stash pop that left conflict markers across the
holder line, and a paragraph of `CLAUDE.md` that went missing between a commit
and a merge. Cutting from main also gives the gate its meaning back, because
a red `check.bat` on a branch is then that branch's own fault rather than
someone else's unfinished round.
