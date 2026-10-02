---
title: Orbis: layout and dependency direction
description: Where code lives under src/, which project may reference which.
load: on-demand
when: Before you add a file, a folder, or a project reference.
projects: github.com/robing98/orbis
order: 50
---
## 2. Layout

```
src/Core/        Engine-agnostic primitives: hashing, coordinate types, serialization
src/Net/         Transport abstraction, packet definitions and codec, session handling
src/Server/      Authoritative simulation
  World/         World instances, chunk lifecycle, tick loop
  Storage/       Chunk and world persistence, applied-overlay list per chunk
  Universe/      Strategic layer: universe state, claims, signature, detection, scouts,
                 faction instancing, diplomacy, event scheduler, overlays (ADR-0020 to 0022)
  Sim/           Flat-array simulation, no nodes:
    Network/       TileNetwork plus one solver per domain (ADR-0017)
    Machines/      Machine store with ContainerId, recipes, sleeping
    Multiblock/    Pattern matcher, shell matcher, interior scoring hooks
    Contraption/   Moving tile groups and actors (ADR-0018)
    Storage/       Storage index and terminals
    Autocraft/     Planner and scheduler
    Spawning/      Rule-based creature spawning, loot
    Survival/      Hazards and exposure, rooms, hunger, farming, genomes, settler jobs (ADR-0023)
src/Worldgen/    Deterministic generation. Pure functions of a seed.
src/Content/     Runtime registries: materials, forms, recipes, tiles, machines, species, factions, crops, locale
src/Mods/        Virtual filesystem, mod loader, JSON patch format, Lua host
src/ServerHost/  Dedicated server. A console executable, not a Godot export.
src/Client/      Rendering, prediction, UI. Owns no game state. Godot lives here.
src/App/         Composition root. The only layer that may name both sides.
data/base/       The base game, shipped as a mod, including events, factions, species, lore, locale
assets/          Palette, grayscale form templates, generated sprites
tools/           Python generators and validators. Not shipped.
tests/           Unit tests, two-client integration tests, determinism vectors
docs/            Architecture, protocol, modding, materials, simulation, mechanics, setting,
                 factions, detection, survival, map, contracts, signals, defense, grades,
                 loot, crafting, archive, dungeons, combat, uniqueness, pacing, chemistry,
                 escalation, worldgen, statuses, knowledge, building, machines, physics,
                 dialogue, trade, art, time, complexity, ui, localization, handoff, ADRs
```

Everything under `src/` except `Client/` is a plain .NET class library that does
not reference `GodotSharp`. That is a project reference which does not exist, not a
convention someone has to remember.

`Client/` is the only project that references the engine, because Godot resolves
scripts by file path inside its own project and a `Node` subclass cannot live in a
referenced library. It reaches the rest through `App/`, so the client never
constructs a server or a transport itself. See ADR-0012.

Dependency direction is strictly one way:

```
Core                     <-  Net
Core                     <-  Content  <-  Mods
Core, Content            <-  Worldgen
Net, Mods, Worldgen      <-  Server
Net, Mods, Server        <-  App
Core, Net, Content, App  <-  Client
Server                   <-  ServerHost
```

Read `A <- B` as "B references A". The project references in the `.csproj` files
are the authority; this diagram only restates them.

`Server` never references `Client`. `Client` never references `Server`. Anything
both need lives in `Core` or `Content`. `App` is the single deliberate exception:
it exists so rule 1.1 can be satisfied inside one process, and it is the only place
that names a concrete `ITransport`. `Client` does not reference `Server` at all, so
that is a build error rather than a review comment.
