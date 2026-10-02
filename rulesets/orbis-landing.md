---
title: Orbis: landing on main
description: The baton, the code claim window, what a rebase owes, and the Cowork design lane.
load: on-demand
when: Before you fast-forward main, rebase a branch, or pass the baton.
projects: github.com/robing98/orbis
order: 50
---
**The baton.** `docs/board.md` opens with `holder: code` or
`holder: design`. It says who Robin is expecting work from next, and who
fast-forwards main. That is all it says now: since every round works in its
own worktree, the pre-commit hook no longer reads the holder line, because a
gate that serialises commits is the thing worktrees exist to stop. Robin
passes the baton with one word in either chat; the holder's last act is to
tick its line with the hash, set the holder line to the other lane, and
commit that, and the receiver's first act is to read the board and the top
open line of its lane. What the hook does enforce is path ownership, which is
fixed either way: queue entries and briefs are written by the design side
only, `src/`, `tests/` and the code side of `docs/traps.md` by the code side
only. A worktree carries its own lane and its own author name, written once
into its git-dir rather than exported into a shell, so `git log` names which
agent produced a commit and a board line's hash resolves to a lane without
prose anyone has to remember to write (`docs/worktrees.md`).

**A code claim closes the window.** A code round costs the better part of an
hour with the gate behind it, and its branch has to fast-forward main to land.
On 2026-09-30 main took 32 landings from the design side against 7 from the
code side, one every 15 to 25 minutes, so the code branch never reached a
fast-forward: entry 139 was rebased three times and every rebase paid for
another full gate run. Main stays fast-forward only, because that is what makes
every point on it a point the gate has seen. The window is therefore made by
hand:

1. A code round writes its claim and fast-forwards that claim commit to main
   **before** it starts working. A claim that never landed is not a claim.
2. While a row under In hand says `code`, the design side keeps committing on
   its branch and holds its fast-forward until the row clears. One exception: a
   claim row may land during a hold, because a lane that cannot say what it is
   taking is worse than a code branch crossing one line of `docs/board.md`.
3. The code round releases the row as its branch lands, so the window lasts no
   longer than the round.

The same clock applies: `tools/check_claims.py` refuses a row older than the day
after it was written, so a forgotten claim cannot hold the design side for more
than a day.

**What a rebase owes.** `dotnet test` reads the engine, the tests, the data they
load, and the assets: `assets/palette.json` and `assets/body/rig.json` by name,
the creature, form and item sprites by name, and whatever else
`ClientContent.Load` walks, because the new-mod test hands it the whole assets
root. So the slow half is `src/`, `tests/`, `data/` and `assets/`, plus the build's
own files, a `.csproj`, the solution and `Directory.Build.props`, since a change
there changes what compiles.

The list is by exclusion, not by enumeration, and that polarity is the point: a
folder under those prefixes is the slow half until someone shows that no test and
no loader reads it, which is what `FAST_ASSETS` in `tools/check_vectors.py` is
for, and the gate refuses an entry there that any test names. The first version of
this rule listed what the tests read and called the rest fast, which would have
told the art lane that a new underground background costs nothing.

`python tools/check_vectors.py --gate-owed-since <the commit the branch was cut
from>` answers it from the paths, so nobody has to judge it by eye. Measured on
2026-09-30: a code branch crossing an afternoon of ship art, 50 changed files,
owed the fast half and nothing more, because `assets/ship/` is the one folder so
far that nothing reads.

So the hold in point 2 bites when the art lane is about to land under `data/` or
an asset a test reads. Lore, ADRs, docs, the art tools and a sprite in a folder
nothing reads land whenever their round is ready.

**Cowork is the design lane.** It writes design docs, briefs and open
questions, the same files a Claude Design round feeds, so it holds no
baton of its own and waits for design's like any design work. It commits
for itself, from its round's own worktree under `wt/`, which carries
`Claude Cowork` as its author name. It is also the art lane: every drawing
goes through the Orbis design system and `docs/orbis-design-workflow.md`,
and lands as a commit with the gate green.
One exception, because it is the case that keeps arising: a correction the
holding lane is already reading may ride in that lane's commit, under the
writing agent's author name.
