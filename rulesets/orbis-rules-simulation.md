---
title: Orbis: simulation
description: Rules 1.8, 1.11 to 1.13, 1.15, 1.16: machines as data, one solve per network, sleeping, no allocation in the tick, containers.
load: on-demand
when: Before you edit src/Server/Sim: machines, networks, ticking, multiblocks, or contraptions.
projects: github.com/robing98/orbis
order: 50
---
### 1.8 Machines and tiles are data, not scene nodes

A world holds tens of thousands of machines. They are stored in flat arrays
(struct-of-arrays) and ticked by systems in `src/Server/Sim/`. They are not
`Node`s and they are not in the scene tree.

Nodes exist only for what the local client currently renders. Rendering is a
projection of simulation state, never its owner.

This is enforced by the compiler, not by review: `Orbis.Server` does not reference
`GodotSharp`, so a `Node` in the simulation does not build.

A machine still takes up space: it occupies foreground cells with its own tile
id and its footprint offset in the state byte, so the chunk renders and
collides with it and the record is found from the origin cell. A multiblock
is casing tiles plus hatches plus one controller. One cell holds one
foreground thing. See ADR-0036 and `docs/simulation.md`.

Corollary: terrain is never replicated via `MultiplayerSynchronizer`. Tiles use
the custom chunk protocol in `docs/protocol.md`.

### 1.11 A network is solved once per network, never per tile

Power, fluids, and items are connected components. Each component is solved once
per tick by one solver that sees the whole component: sum supply, sum demand,
distribute, apply losses. A tile never runs logic on behalf of the network it
belongs to, and a network is rebuilt incrementally when a conductor changes, never
wholesale every tick.

Rationale: per-tile network logic is why heavily modded Minecraft collapses. It
scales with the wrong quantity, and no amount of faster hardware fixes that.

### 1.12 Only observed chunks tick, and idle machines sleep

A chunk ticks only while it is in the world's simulation set: subscribed by a
client, held by a chunk loader, or carrying an active contraption (ADR-0019).
There is no offline catch-up. A machine with no valid recipe, no input
change, and no power change is asleep and skipped entirely. It wakes on exactly
three triggers: an adjacent inventory changed, its power state changed, or a player
interacted with it.

Rationale: ten thousand placed machines are normal. Ten thousand *awake* machines
are not. If everything a player ever places ticks forever, the budget in
`docs/simulation.md` is fiction.

### 1.13 The tick allocates nothing

Simulation state is struct-of-arrays. Hot paths use `Span<T>` and `ArrayPool<T>`.
No LINQ, no closures, no iteration through an interface, no strings built per tick.
Server GC is on in `Directory.Build.props`.

Rationale: the budget is 50 ms at 20 Hz. A gen 2 collection can cost more than the
whole tick, and a dropped tick in a server-authoritative game is a visible stutter
for every player at once.

### 1.15 One TileNetwork for every conduit domain

Kinetic, electric, fluid, item, and storage conduits all use the one component
tracker in `src/Server/Sim/Network/`. A domain is a solver plus data, never a
second implementation of connected components. A conduit is an exposed
foreground tile or a record buried behind a background wall; both are cells
of the same component, and a machine's port sees the buried ones behind it
(ADR-0036). See ADR-0017 and `docs/mechanics.md`.

### 1.16 Machines are addressed by container

Every machine record carries a `ContainerId` (0 for the world, otherwise a
contraption) and a position local to that container. Never key a machine by
world position alone; contraptions in milestone 12 depend on it. See ADR-0018.
