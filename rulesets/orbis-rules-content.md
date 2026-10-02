---
title: Orbis: content and data
description: Rules 1.9, 1.10, 1.19, 1.20, 1.22 to 1.25: the material and form matrix, no copied content, locale keys, hazards and statuses, the concept ledger, uniqueness, pacing, chemistry.
load: on-demand
when: Before you add or change content data: materials, items, recipes, machines, hazards, statuses, creatures, biomes, or player-facing strings.
projects: github.com/robing98/orbis
order: 50
---
### 1.9 Content is defined by the material and form matrix

Do not hand-author individual items. An item is the pair
`(material, form)`, for example `(copper, plate)` or `(titanium, small_dust)`.
Materials and forms are data. Items, their sprites, and their base recipes are
derived. See `docs/materials.md`.

Adding a material must yield its whole item family and processing chain
automatically. If you find yourself writing per-item code, the abstraction leaked.

What varies between two copper plates is the stack's **grade**: purity and
taint as per-stack permille integers, never a graded item type. Stats derive
from properties times purity; taint comes from the world and is nearly
conserved through processing. See ADR-0030 and `docs/grades.md`.

What you build with is the matrix too: `block`, `wall`, `platform`, `casing`,
`ladder`, and `rope` are forms that place as tiles, with the tile id,
sprite, hardness, and collision derived from the material. A multiblock
shell asks for a casing property, never a material. Furniture is a machine
without ports. See `docs/building.md`. Every machine declares
`crafted_from` in matrix items plus, from tier III up, a circuit, with property
requirements instead of material names, and its electric port sits at its
own tier; the validator refuses the rest. See `docs/machines.md`.

Anything above the matrix is an **assembly**: slots that take matrix forms,
stats as formulas over the slotted materials. Tools, weapons, the suit, and
ship modules are assemblies; no variant is hand-authored, and craft quality is
the producing machine's purity delta, narrowed at most by a quarter by a
discipline. See ADR-0031, ADR-0032, and `docs/crafting.md`.

### 1.10 No assets, names, IDs, text, or code from any reference work

Starbound, GregTech, Create, Extreme Reactors, Applied Energistics, Halo,
StarCraft, and Stargate are references for mechanics and structure only. No
copied sprites, item names, faction names, terms, identifiers, recipe values,
or text.

### 1.19 Every player-facing string is a locale key

No hardcoded player-facing text in code, data, or Lua. Strings are keys
resolved through locale files with named placeholders and plural rules; no
sentence is built by concatenation. English is the source language and ships
first; other languages follow as data. See `docs/localization.md`.

### 1.20 One hazard vocabulary; protection and crops derive from data

Worlds, species tolerances, protective gear, sealed rooms, and crop needs all
use the hazard keys and tiers in `data/base/hazards.json`. Exposure is one
integer formula. Plating protects by its material's properties and linings by
insulation; no armor is hand-authored. Crops have genomes from a fixed trait
vocabulary. Ore generation is gated by hazard tier. See ADR-0023 and
`docs/survival.md`.

Exposure effects, hunger stages, food effects, and what creatures, liquids,
and tiles do to a body are **statuses** from one fixed vocabulary in
`data/base/statuses.json`: a timed modifier on a body with a fixed set of
stat hooks, one instance per status, buffs capped at 250 permille per hook.
Adding a status is data; adding a hook needs an ADR. See `docs/statuses.md`.

### 1.22 Reuse before adding: the concept ledger

`docs/complexity.md` lists every concept the game has. A feature names the
concept it reuses. A new concept is allowed at most once per milestone and
only when it absorbs something that previously needed its own explanation.
A feature that cannot name its concept is not ready. See ADR-0031.

### 1.23 Nothing is a recolor

Every biome, creature, event, dungeon theme, faction, and boss declares a
`signature_mechanic` no other entry has; every creature declares a `verb` no
other creature has; a boss is a structure with a mechanic, never a bigger
creature. Palette swaps exist only in the material and form matrix. The
validator refuses duplicates and omissions. See `docs/uniqueness.md`.

### 1.24 No system gates another system's basic use

Research gates machines; nothing gates research. Signature draws attention;
a player with no sensor still sees the meter. Every system past the core loop
is optional, and world settings can turn the pressure off. See `docs/pacing.md`.

### 1.25 Reactions conserve elements

A compound is a material with a `composition` over element materials; a
reaction is a recipe whose inputs and outputs carry the same count of every
element, and `tools/validate_content.py` refuses any other. Gases and
liquids are material states with one item form, the cell, and live
otherwise in the fluid network; a fluid declares the corrosion resistance
its container needs and the hazards it causes when free. Alloys are
mixtures and never react. Air is a composition of a world, never a
material. See ADR-0034 and `docs/chemistry.md`.
