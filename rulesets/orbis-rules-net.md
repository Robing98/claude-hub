---
title: Orbis: server authority and transport
description: Rules 1.1 to 1.3: the server decides, clients send intents, networking goes through ITransport.
load: on-demand
when: Before you edit src/Net, packets, or anything a client sends or predicts.
projects: github.com/robing98/orbis
order: 50
---
### 1.1 The server is always authoritative, even in singleplayer

Singleplayer starts a real local server in the same process and connects a real
client to it over the transport layer. There is no privileged client path, no
"if singleplayer then skip the network" branch, ever.

Rationale: it removes the entire class of bugs that only appear in multiplayer.

### 1.2 The client never decides anything that matters

Clients send intents (`PlaceTile`, `BreakTile`, `MoveInput`, `MachineInteract`).
The server validates, applies, and broadcasts the result. The client may predict
**only** its own character movement, and must reconcile when the server disagrees.

Never trust a client for: inventory contents, item stack counts, machine state,
power, position of anything but its own predicted body, mod list, or recipe results.

### 1.3 The transport layer is abstracted

All networking goes through `ITransport` (`src/Net/Transport/ITransport.cs`).
Two implementations:

- `LiteNetTransport`: local development, automated tests, headless CI. Default.
- `SteamTransport`: real sessions, via Steam Networking Sockets.

Game code never references LiteNetLib or Steam types. If you need a Steam-only
feature in game code, that is a signal the abstraction is wrong. A `FakeTransport`
under `tests/` is a test double, not a third implementation.
