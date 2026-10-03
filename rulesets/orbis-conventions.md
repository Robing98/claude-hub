---
title: Orbis: conventions
description: Language, naming, units, comments, commits, errors, prose, schemas, snapshots, deferrals.
load: always
projects: github.com/robing98/orbis
order: 21
---
## 3. Conventions

**Language.** C# 12 on .NET 8. Godot is used from `src/Client/` and `src/App/`
only. Move a subsystem to a native library only when a benchmark in `tests/` shows
it misses its tick budget. With C# that is not expected, and a benchmark remains
the only acceptable trigger.

**Architecture.** 64-bit only. Godot ships a 32-bit .NET build beside x86_64 and
arm64, so neither C# nor .NET 8 decides this: the export preset does, and it is
one line in a file nobody reads twice. A heavily modded session is exactly the
case that runs out of a 32-bit address space, and `ServerGarbageCollection` in
`Orbis.csproj` wants a 64-bit heap. `tools/check_export_arch.py` refuses a preset
whose `binary_format/architecture` is not `x86_64`, `arm64`, or `universal`, and
passes while there is no `export_presets.cfg` at all.

**Naming.** `PascalCase` for C# files, types, and members. `camelCase` for locals
and parameters, `_camelCase` for private fields. `snake_case` stays for data files
and for the identifiers inside them, which are always namespaced:
`base:copper_plate`, `somemod:tungsten_wire`. Never use a bare identifier in a
data file.

**Units.** Tiles are 32px; item icons are 64px, drawn at the UI's scale, and a
form that places as a tile keeps a 32px face whose icon is the face at 2x
(ADR-0050). Chunks are 32x32 tiles. Planets wrap horizontally and
have a fixed height. Server tick is 20 Hz. Power is in integer energy units per
tick; never floats. The world renders at 1x by default (a client setting with
0.5x as the overview and 2x), 60 by 34 tiles on a 1080p screen; the UI has its own scale.

**Commit messages.** Imperative mood, present tense. What a message must not
carry is in the shared set "Commit and pull request texts".

**Errors.** A malformed mod file must produce a readable message naming the file
and the field, and must not crash the server. Assume every mod file is written by
a human at 2am. A packet decoder must never throw on hostile input; it returns
false and a reason.

**Rules and their enforcement.** A rule ships with its check. The rule is in
the shared set "Verification".

**Prose.** `tools/check_prose.py` refuses em-dashes. The rule is in the shared
set "Working with Robin".

**Generated schemas.** `docs/schema/` is written by
`python tools/gen_schemas.py` from the loaders, so a schema cannot describe a
field no loader reads. A commit that adds a key to a data file, removes one, or
changes its type runs the generator in the same commit; the gate's `mod_surface`
check refuses it otherwise. A schema is the loaders' view and the Python
validator enforces more, so it is a floor, and it never sets
`additionalProperties: false` while that is true.

**Snapshots.** A document that describes the tree at a moment (the roadmap,
a design brief, a patch note) carries `verified-as-of: YYYY-MM-DD` in its
first lines, the date its claims were last checked against the tree, or
`superseded-by: docs/<path>` when it is kept for the record. Whoever checks
it bumps the date. An ADR is not a snapshot: once accepted it is not
rewritten, a change of mind is a new ADR that supersedes it, and a
clarification is a dated amendment line under Consequences. Questions with
no decision yet live in `docs/open-questions/`; what has cost time lives in
`docs/traps.md`. `tools/check_docs.py` enforces the dates and keeps
`docs/decisions/README.md` generated.

**Deferrals.** Whatever a round puts off is filed in `docs/open-questions/`
the day it is put off, with the date it is picked up again, and it ends as
exactly one of three: promoted (solved, the file names the doc and goes),
closed (won't fix, the file says why and stays), or open with a new date.
`tools/check_docs.py` refuses an open question without a revisit date or
with one that has passed; `docs/board.md` lists them all under Deferred.
