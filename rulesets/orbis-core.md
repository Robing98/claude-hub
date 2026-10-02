---
title: Orbis: core rules
description: What Orbis is and the 26 rules that cannot be retrofitted, by title.
load: always
projects: github.com/robing98/orbis
order: 20
---
# orbis

> Codename. Rename before the first public commit. With C# the name is also in
> namespaces, assembly names and the solution, so the longer you wait the more it costs.

A 2D side-scrolling sandbox survival game: procedurally generated planets
reached by an upgradeable crew ship and, rarely, by gates that stay open only
for a while; mining and building; deep industrial progression; and a galaxy
that has already ended once. Starbound is the reference for world structure
and feel, GregTech and Create for progression depth, and `docs/setting.md` is
the story the mechanics serve.

**These rules contain decisions that cannot be retrofitted. The titles below are binding
as they stand. Load the rule set named behind a rule before the first edit in its area.**

## 1. Non-negotiable rules

Breaking any of these is not a bug to fix later. It is a rewrite. If a task
appears to require breaking one, stop and ask instead of working around it.

- 1.1 The server is always authoritative, even in singleplayer (`orbis-rules-net`)
- 1.2 The client never decides anything that matters (`orbis-rules-net`)
- 1.3 The transport layer is abstracted (`orbis-rules-net`)
- 1.4 Worldgen is deterministic and integer-only (`orbis-rules-worldgen`)
- 1.5 Chunks are persisted whole, never as diffs against the generator (`orbis-rules-worldgen`)
- 1.6 The base game is a mod (`orbis-rules-mods`)
- 1.7 Lua game logic runs on the server only (`orbis-rules-mods`)
- 1.8 Machines and tiles are data, not scene nodes (`orbis-rules-simulation`)
- 1.9 Content is defined by the material and form matrix (`orbis-rules-content`)
- 1.10 No assets, names, IDs, text, or code from any reference work (`orbis-rules-content`)
- 1.11 A network is solved once per network, never per tile (`orbis-rules-simulation`)
- 1.12 Only observed chunks tick, and idle machines sleep (`orbis-rules-simulation`)
- 1.13 The tick allocates nothing (`orbis-rules-simulation`)
- 1.14 Mod hooks are event-driven, and metered (`orbis-rules-mods`)
- 1.15 One TileNetwork for every conduit domain (`orbis-rules-simulation`)
- 1.16 Machines are addressed by container (`orbis-rules-simulation`)
- 1.17 The strategic layer always ticks; consequences are overlays (`orbis-rules-universe`)
- 1.18 Signature, detection, archetypes, species, diplomacy are data (`orbis-rules-universe`)
- 1.19 Every player-facing string is a locale key (`orbis-rules-content`)
- 1.20 One hazard vocabulary; protection and crops derive from data (`orbis-rules-content`)
- 1.21 The player knows only what the save has learned (`orbis-rules-universe`)
- 1.22 Reuse before adding: the concept ledger (`orbis-rules-content`)
- 1.23 Nothing is a recolor (`orbis-rules-content`)
- 1.24 No system gates another system's basic use (`orbis-rules-content`)
- 1.25 Reactions conserve elements (`orbis-rules-content`)
- 1.26 The window is the canvas; art is never scaled to fit it (`orbis-rules-client`)
