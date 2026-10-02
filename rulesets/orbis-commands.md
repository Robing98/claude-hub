---
title: Orbis: commands and the skipped gate
description: Commands to run, test, generate, and validate, and what a commit owes when it skips the gate.
load: on-demand
when: When you need a command, and before you commit with --no-verify.
projects: github.com/robing98/orbis
order: 50
---
## 5. Commands

```powershell
# Run the game
godot --path .
play.bat --menu

# Skip the menu and start a local game straight away
godot --path . -- --singleplayer
play.bat

# Dedicated server. A console executable, no engine in the process.
dotnet run --project src/ServerHost -- --port 7777

# All tests, unit and integration
dotnet test

# The milestone 0 acceptance test on its own
dotnet test tests/Orbis.IntegrationTests

# Measure a frame instead of watching it: one row per frame, what it spent.
# --probe-shaft cuts a shaft so a fall can be measured. See docs/simulation.md.
godot --path . -- --singleplayer --probe-frames run.csv --probe-shaft 1760

# Take one picture and quit: where, when, how wide, and a manifest beside it.
# The same command twice gives the same bytes. shots/ is ignored.
play.bat --screenshot shots/wood.png --window 3440x1440 --at base:broadleaf_wood --time dusk --seed 17
# --at names the biome, so the seed has to be one whose world actually rolled it.
# This number rots: 3 until the home world moved (entry 163), 20 until biome wave 1
# took the broadleaf wood out of that universe, 17 today. Entry 201 makes --at find
# its own seed, and then this line loses the number for good.

# Regenerate sprites after a palette or material change
python tools/gen_tiles.py
python tools/gen_item_sprites.py

# Placeholders for everything without art yet, and the art backlog
python tools/gen_placeholders.py

# Check content data and locale files before committing
python tools/validate_content.py
python tools/validate_locale.py

# Walk the ladder: what each tier is the first to give you, and what breaks it
python tools/validate_progression.py

# The local gate, everything above plus the prose and docs checks, the seed
# vectors, and dotnet test; --fast skips the last two. The hooks run it.
check.bat
check.bat --fast
git config core.hooksPath .githooks

# Regenerate the ADR index after adding a decision
python tools/gen_adr_index.py

# The mod surface: a mod that already loads, one command that says what is wrong
# with it, and the schemas generated from the loaders (docs/modding.md)
orbis.bat new-mod <id>
python tools/validate_content.py --mod data/<id> --json
python tools/gen_schemas.py
```

**A skipped gate is written down.** `check.bat` needs `dotnet test`, and not every
session has it: the design lane has no dotnet at all, and the art step takes minutes. So a
commit sometimes goes in with `--no-verify`. Three of those turned main red in two days,
each one a determinism vector the lane that moved it could not repin. When a commit skips
the hook:

1. Say so in the commit message, in the same sentence that says why.
2. Write a row in the Gate owed block of `docs/board.md`, `- YYYY-MM-DD  <commit>  <why>`,
   in the round's release commit, once the hash exists. A rebase before landing rewrites
   that hash, so fix the row to the commit that landed: `tools/check_claims.py` refuses a
   row naming a commit that is not in the branch's history, which is what a stale hash
   looks like. The old object still resolves out of the reflog, so asking whether it exists
   proves nothing.
3. The next code round runs `check.bat` on main as its first act, clears the row, and
   reports what it found.

`tools/check_claims.py` refuses a row older than the day after it was written, on the same
clock the In hand block runs on. That row is the enforced half: no hook can check the
commit message, because the hook is the thing that was skipped.
`tools/check_vectors.py` holds the other end, the table of which data each vector in
`tests/vectors/` is recorded from. The pre-commit hook prints it for the design lane, and
the gate refuses a vector that the table does not name at all.
