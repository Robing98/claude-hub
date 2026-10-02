---
title: Orbis: worldgen and persistence
description: Rules 1.4 and 1.5: deterministic integer-only generation, chunks saved whole.
load: on-demand
when: Before you edit src/Worldgen, seeds, chunk generation, or chunk and world storage.
projects: github.com/robing98/orbis
order: 50
---
### 1.4 Worldgen is deterministic and integer-only

One `universe_seed` is persisted. Everything else is derived:

```
planetSeed = SeedHash.Derive(universeSeed, systemX, systemY, orbitIndex)
biomeSeed  = SeedHash.Derive(planetSeed, "biome")
chunkSeed  = SeedHash.Derive(planetSeed, chunkX, chunkY)
```

Use `SeedHash` (`src/Core/SeedHash.cs`), never `Random`, `Guid`, or `GetHashCode()`.
Runtime string hashing is randomised per process and changes between versions;
`SeedHash` will not.

Any value that decides tile placement must come from integer or fixed-point math.
No floats in the generation path. Floats vary across platforms and compiler flags.

There is no physics engine either. Materials carry real properties and each
phenomenon has one deterministic integer solver: bodies in fixed point
(ADR-0038), gravity per world, friction, buoyancy, projectiles, granular
tiles, a liquid automaton, heat per room, electricity, kinetics, light.
What is not on that list is not simulated. See ADR-0039 and `docs/physics.md`.

What the function produces is `docs/worldgen.md`: depth measured below the
local surface in six bands, biome bands by longitude, and every feature on a
power-of-two lattice so no chunk reads a neighbor (ADR-0037). The world type
is the base of a planet (substrate, one primary biome), a surface biome owns
a root of its own rock under its band, underground biomes are pockets, and
landmarks are shapes from a kind and a seed on their own lattice, never
prefabs (ADR-0043). The numbers are data in `data/base/worldgen.json`.

### 1.5 Chunks are persisted whole, never as diffs against the generator

Once a chunk has been generated and observed, save the full chunk. Never save
"generator output plus a diff". A worldgen change would then silently corrupt
every existing save. Each saved world carries a `worldgen_version`.
