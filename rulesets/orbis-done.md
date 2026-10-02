---
title: Orbis: definition of done
description: When a task counts as finished.
load: always
projects: github.com/robing98/orbis
order: 22
---
## 4. Definition of done

A task is finished only when all of these hold:

1. It runs headless: `dotnet test`.
2. Unit tests cover the new logic, including at least one failure case.
3. Anything touching worldgen has a determinism test: same seed, same output,
   asserted against committed vectors in `tests/vectors/`.
4. Anything touching netcode passes the two-client integration test.
5. No new `MultiplayerSynchronizer` on anything tile-shaped.
6. Documentation updated when a contract changed: `docs/protocol.md` for packets,
   `docs/modding.md` for the mod API, `docs/materials.md` for content rules.
7. A decision that closes off an alternative gets an ADR in `docs/decisions/`.
8. Every new player-facing string has a key in `data/base/locale/en.json`, and
   `python tools/validate_locale.py` passes. A string that names a gated term has
   an `.unknown` variant.
9. `check.bat` is green. It is the local gate: the content, locale, and
   progression validators, the prose and docs checks, the seed vectors, and
   `dotnet test`. The pre-commit hook runs its fast half and the pre-push hook
   all of it, so a red gate never becomes a commit by accident; GitHub CI runs
   the same steps later. Install the hooks once per clone with
   `git config core.hooksPath .githooks`.
10. New content brings its sounds or owes them on purpose (ADR-0103, `docs/sound.md`): a new
   machine, creature, tile, status, biome, event, or weapon adds sound events, and the gate's
   `sound_inventory` step is red until each has a sound or `python tools/sound_inventory.py --owe`
   lists it in `data/base/sounds_owed.json`. Commit the regenerated `docs/sound-inventory.md` with it.
