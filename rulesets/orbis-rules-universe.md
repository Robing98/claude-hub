---
title: Orbis: strategic layer, detection, knowledge
description: Rules 1.17, 1.18, 1.21: the strategic layer and overlays, signature and factions as data, what the player knows.
load: on-demand
when: Before you edit src/Server/Universe, factions, signature, detection, diplomacy, time, dialogue, the archive, or the knowledge system.
projects: github.com/robing98/orbis
order: 50
---
### 1.17 The strategic layer always ticks; consequences are overlays

Universe state (claims, anchors, signature, interest, fix, event schedule,
faction instances, relations, population, Keystone decay) lives in
`src/Server/Universe/`, ticks on its own slow clock for every world regardless
of what is loaded, and schedules events deterministically from seed and tick.
What happens to an unattended world is recorded as an overlay and applied once
when its chunks load; every chunk records the overlays it has applied, which is
a field in the chunk format from the first save. See ADR-0020.

Each world also carries an **escalation stage** (0 to 3) set by the Bloom's
fix on it and floored by Keystone decay. A stage changes what spawns, how
fast bloomed ground grows, and how often surges come; it never changes a
creature's stats and never reads the player's depth, disciplines, archive,
play time, or kills. Every wildlife creature has one warped form with its
own verb. See ADR-0035 and `docs/escalation.md`.

Events obey four data-enforced rules: telegraphed, only where the player acted
(a claim, a gate they opened, an enclave they met), failure costs territory not
the ship or the base, frequency follows signature. See `docs/setting.md`.

Time is the same clock: a world's day, year, seasons, moons, and eclipses are
pure functions of the universe tick and the orbit numbers rolled with the
world (`data/base/orbits.json`), never stored, never caught up, never a
timer. Insolation from the star and the orbit is what solar and crops get,
and the orbit band and the season add at most a tier to the hazard vector.
See `docs/time.md`.

### 1.18 Signature, detection, archetypes, species, diplomacy are data

Attention is a per-world signature vector over four bands (thermal, optical,
radio, exotic) fed by data-defined sources and masked in permille by region,
world type, and depth; the exotic band passes through matter. Signature raises
per-faction interest through a listening profile over bands; interest sends
scouts; scouts raise fix; events require a fix level. The player sees their own
signature exactly and a faction's fix only through evidence, and sees the
galaxy only where their own sensors reach. Transmissions are strategic objects
with a cipher; intercepting, tracing (two receivers for a fix), and cracking
(material over time, never a mini-game) are data, and locks on doors and loot
use the same cipher families (ADR-0028). Ship combat is abstract resolution
plus boarding (ADR-0029). Factions are archetypes in data and
Lua, instanced per universe from the seed. A species is a selection from the
fixed trait vocabulary in `data/base/species/_traits.json`, never new code.
Diplomacy is a state machine over data-defined treaty types with per-archetype
Lua rules on the strategic tick. See ADR-0021, ADR-0022, `docs/factions.md`,
`docs/detection.md`, `docs/map.md`.

### 1.21 The player knows only what the save has learned

The character is printed from a template with a corrupted imprint: hands
remember, the head does not. Every world-knowledge term is an entry in
`data/base/terms/` with an unknown and a known display, learned by named
sources; UI, codex, and dialogue show the unknown display until then. Object
terms (materials, forms, machines) are retained. There is no story quest log
and no tutorial; the codex keeps a board of open questions instead.
Numbers follow the same rule: every fact of every thing names the sources
that reveal it in `data/base/knowledge.json` (assay, the lab, eating,
sightings, mining, the archive), every tooltip shows the known facts and
the hint for each unknown one, and a recipe appears only when its machine
is known and its inputs have been held. See `docs/knowledge.md`. Deals
with people are contracts on a second board, and their rewards are physical:
picked up, couriered, or gated, never granted (ADR-0027). Talking is data in
`data/base/dialogue/` and obeys the same parity: a line that names a gated
term has an unknown variant, nobody states a goal, every choice says what it
is, and effects are the game's own verbs (`docs/dialogue.md`). There is no
money: every stack has a derived value and trade is a two-way contract
(`docs/trade.md`). See ADR-0026, `docs/setting.md`, and `docs/contracts.md`.

The imprint has two layers. The head is terms and questions. The hands are
**disciplines**: use-based levels 0 to 5 with effects capped at 25 percent and
no gating of any recipe. Imprint **depth** is the overall level, whose XP is
practice plus discoveries and never a farmable counter; **specializations**
are Concord job imprints from the archive, chosen at depth 3 and 10,
changeable at the printer. Gear modifiers come from a fixed vocabulary;
crafting never rolls one; loot, the Graft, and the Rewriter do, and the
Rewriter's seeded roll can destroy the gear or turn it into an enemy built
from its own assembly (ADR-0033). Structures are prefab pieces stitched by
worldgen from the seed. The **archive** is the research tree and belongs to
the vault: nothing above tier 0 is buildable without its entry, entries cost
fragments and notes, and they also print settler jobs. A reprint restores the
last imprint written at a pod. See ADR-0032 and `docs/archive.md`.
