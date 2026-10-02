---
title: Orbis: mods and Lua
description: Rules 1.6, 1.7, 1.14: the base game is a mod, Lua logic runs on the server, hooks are event-driven and metered.
load: on-demand
when: Before you edit src/Mods, the mod API, Lua hooks, or how data/base is loaded.
projects: github.com/robing98/orbis
order: 50
---
### 1.6 The base game is a mod

`data/base/` is loaded through the same mod pipeline as any third-party mod, with
no special casing. If the base game needs an engine feature that mods cannot
reach, mods will need it too. Add it to the mod API instead of hardcoding.

### 1.7 Lua game logic runs on the server only

Server-side Lua drives machines, entities, recipes, and world events.
Client-side Lua may touch UI and cosmetics only.

If the same logic script runs on both sides and diverges by one branch, you get a
desync that costs a weekend to find. The server owns the mod list; a client whose
mod set does not match is rejected at handshake with a readable message.

All mod Lua runs in a sandboxed state: no `io`, no `os`, no `require`, no
`loadstring`, with an instruction budget per call.

### 1.14 Mod hooks are event-driven, and metered

Lua sees events: `on_recipe_complete`, `on_power_lost`, and so on. A `tick` hook
fires only while its machine is awake, and every call runs under an instruction
budget. A per-mod profiler that players can open is part of the mod API, not a
debugging feature to add later.

Rationale: the mod API is where rule 1.12 leaks back out. A mod that can ask to be
ticked unconditionally reintroduces exactly the problem the rest of this
architecture exists to avoid, and without a profiler nobody can tell which mod
did it.
