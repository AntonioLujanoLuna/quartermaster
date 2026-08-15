# Quartermaster and Avrae Integration Plan

Status: the hosted-Avrae path is the committed one. Gate 1 has been answered "no, for now"
(see [Gate 1 decision](#gate-1-decision-2026-08-14)), so the self-hosted extension work is
parked rather than pending, and the hosted fallback is being built out as the destination
instead of a waiting room. The provider boundary in `integration.py` remains a contract with
no live caller.

Updated: 2026-08-14

## Purpose

Make Quartermaster the table's single player-facing front door while keeping Avrae authoritative for D&D mechanics. The goal is one coherent play experience, not two competing combat databases.

The plan deliberately separates three ambitions:

1. One place to discover and start every table action.
2. One place to coordinate session, combat, and continuity workflows.
3. One-click execution of Avrae mechanics from Quartermaster controls.

The first two are Quartermaster work. The third requires an Avrae-side integration surface and must not be assumed to exist in the hosted Avrae bot.

## Implementation slice 1: durable provider boundary

Quartermaster now has a schema-9 `provider_operations` record linked by operation and interaction IDs to the existing receipt protocol. It records the authenticated actor, guild, channel, session, operation kind, provider reference, correlation ID, integration/provider versions, request payload, and one of `REQUESTED`, `COMMITTED`, `FAILED`, or `UNKNOWN`.

Provider request reservation is atomic with the DEFERRED receipt, and provider finalization is atomic with the receipt outcome. Startup recovery marks an interrupted provider request `UNKNOWN`; it does not authorize an automatic retry of an attack, spell, save, or turn advance. The implementation exposes an `AvraeGateway` protocol, but it does not pretend that Quartermaster can invoke the hosted Avrae bot or calculate mechanics.

## Implementation slice 2: hosted-Avrae handoff

Quartermaster now exposes guild-scoped `/combat` with choices for starting, joining, advancing, attacking, casting, checking, saving, ending, and viewing combat status. It requires an active Quartermaster session, identifies the current Discord channel, and renders the native Avrae command card for that action. The handoff is deliberately read-only: it creates no provider receipt because Quartermaster has not executed a provider call.

The current cards use the documented native forms such as `!i begin`, `!i join`, `!i next`, `!attack`, `!cast`, `!check`, `!save`, and `!i end`. This is a genuine hosted-Avrae wrapper for discovery and context, but not yet one-click execution; that still requires the Avrae-side extension gate.

## Implementation slice 3: self-hosted Avrae context probe

`integrations/avrae/quartermaster_cog.py` is a disposable Avrae-side extension scaffold. Its `/qm-combat-probe` command receives a real Avrae interaction, preserves the actor/guild/channel identity, loads native combat with `Combat.from_ctx(inter)`, and records a Quartermaster provider receipt and correlation ID. It reports native combat presence only; it does not copy HP, initiative, conditions, resources, or combatants into Quartermaster.

The scaffold is not loaded by the current hosted Quartermaster process and has never been run inside an Avrae deployment. It is now parked behind the Gate 1 decision below rather than queued as the next task. Nothing in the running bot has ever exercised it, and its assumptions about Avrae's internals — that `CombatNotFound` is importable from `cogs5e.initiative`, that `Combat.from_ctx` accepts a slash interaction, that two OS processes may share one SQLite file — are unverified. Treat the file as a record of the intended shape, not as working code.

## Implementation slice 4: the Quartermaster combat record

Schema 12 adds `combat_encounters`: session, channel, open/closed status, who opened and closed it, timestamps, a close reason, and an optional DM outcome note. It has no column for HP, initiative, conditions, resources, or combatants, and a test asserts the exact column set so it cannot quietly gain one.

`/combat` now does three things beyond printing a card:

- **Start combat** opens the encounter and then renders `!i begin`. DM-only, because it writes canonical state.
- **End combat** closes the encounter, takes an optional outcome note, renders `!i end`, reports any Loot Drops still outstanding in the session, and attaches the Party Stash and Loot Drop controls. DM-only.
- **Combat status** answers from Quartermaster's own record — which session, which channel, how long the fight has been running, what loot is outstanding, and what the previous combat resolved to — and names Avrae as the owner of everything mechanical.

The other six actions remain pure handoff cards open to any player. Closing a session closes any encounter still open, with reason `SESSION_CLOSED`, the same way it closes outstanding Loot Drops.

This is the closeout the fallback was missing: combat ending is the moment loot exists, and it used to be the moment Quartermaster stopped talking. None of it requires an Avrae API, and none of it makes Quartermaster authoritative for a mechanic.

## Research findings

Research performed against official Avrae documentation and the public Avrae repository on 2026-08-11.

### Avrae is extensible, but the important API is internal

- The Avrae repository describes a fully featured modding API for writing custom commands.
- Avrae loads a fixed set of Python Cogs during startup. A Quartermaster-specific Cog would therefore require a self-hosted Avrae build, a maintained fork, or an upstream-supported extension mechanism.
- The current Avrae combat implementation uses `disnake` command contexts and an internal `Combat` model. Combat state is persisted by channel and includes the summary message, combat DM, combatants, round, turn, current combatant, options, and metadata.
- The combat model exposes internal operations such as loading combat by channel, committing changes, advancing turns, changing combatants, and ending combat. `Combat.from_ctx(inter)` and `Combat.commit(ctx)` are therefore useful implementation seams inside a self-hosted Avrae process; they are not a public HTTP API for another Discord bot.
- Avrae supports both prefix command groups and some slash commands. The combat surface should not assume that every Avrae operation is available as a slash command.

Sources:

- [Avrae repository and self-hosting requirements](https://github.com/avrae/avrae)
- [Avrae initiative Cog](https://github.com/avrae/avrae/blob/nightly/cogs5e/initiative/cog.py)
- [Avrae Combat model](https://github.com/avrae/avrae/blob/nightly/cogs5e/initiative/combat.py)
- [Avrae combat commands](https://avrae.readthedocs.io/en/stable/cheatsheets/dm_combat.html)

### The documented alias surface is useful but insufficient by itself

- Avrae aliases can access the active combat context and initiative metadata.
- Avrae provides signed alias-invocation data and a verification endpoint. That can help authenticate an observed alias event, but it is not a general-purpose endpoint for Quartermaster to execute attacks, spells, or initiative operations.
- The official documentation reviewed does not describe a supported external command-execution API. This remains a verification item, not an assumption that no future or undocumented option exists.

Sources:

- [Avrae aliasing API](https://avrae.readthedocs.io/en/stable/aliasing/api.html)
- [Avrae aliasing basics](https://avrae.readthedocs.io/en/stable/aliasing/aliasing.html)

### Discord does not provide ordinary bot-to-bot impersonation

Discord sends an interaction to the application whose command or component the user invoked. Quartermaster cannot safely invoke Avrae's command as though it were the user merely by possessing its own bot token.

Therefore, these are not acceptable integration foundations:

- a Quartermaster bot pretending to be a player;
- a user token or self-bot;
- screen automation;
- scraping Avrae embeds as the canonical combat API;
- writing directly into Avrae's MongoDB schema without an Avrae-owned adapter.

Source: [Discord interactions and application commands](https://docs.discord.com/developers/platform/interactions)

## Recommended target architecture

### Product boundary

Quartermaster is the table control plane and continuity system.

Avrae is the mechanics provider.

Quartermaster owns:

- the session record;
- the Quartermaster combat record and Avrae combat reference;
- the player-facing launcher;
- actor, guild, channel, and session authorization;
- shared inventory, treasury, loot, and provenance;
- continuity notes and the end-of-combat handoff;
- integration receipts, correlation IDs, health, and unresolved provider outcomes.

Avrae owns:

- character sheets and active characters;
- initiative order and turns;
- HP, AC, conditions, effects, resistances, and resources;
- dice, attacks, spells, checks, saves, and mechanical results;
- the detailed combatant state.

Quartermaster may display an Avrae-backed status projection, but it must be labelled as provider state and must never become a second authoritative copy.

### Preferred technical shape for true centralization

If one-click mechanics are a non-negotiable goal, the preferred path is:

1. Self-host or fork Avrae.
2. Add a Quartermaster integration Cog inside the Avrae process, or create an Avrae-owned RPC boundary that calls the same internal combat services.
3. Keep Quartermaster's SQLite service as the authority for continuity and shared campaign state.
4. Route the player-facing launcher through one Discord application surface.
5. Let the Avrae-side component execute mechanics using the authenticated Discord actor and the native Avrae combat context.

This is materially larger than adding a wrapper around the current Quartermaster bot. The Avrae repository's self-hosting documentation calls for MongoDB, Redis, Python, and other operational dependencies, with Windows described as compatible but untested. That deployment cost is a first-class decision.

### Committed shape: hosted Avrae

Gate 1 chose this one. With the official hosted Avrae bot, Quartermaster provides:

- a unified launcher;
- a combat record of its own — session, channel, duration, outcome — with no Avrae state in it;
- combat setup and closeout workflows, including the spoils handoff at the moment combat ends;
- channel and command guidance;
- copyable native command cards;
- continuity capture;
- loot and treasury handoff.

This is a guided wrapper around a combat workflow Quartermaster genuinely owns half of, not one-click execution of Avrae mechanics. Describing it as a full integration would be wrong, and the surfaces say so in the text a player reads.

## Integration contract to design before implementation

Every provider-backed action needs:

- a Quartermaster operation ID;
- the Discord actor ID, guild ID, channel ID, and session ID;
- the Avrae combat reference, normally the channel-bound combat identity;
- the requested operation class, such as `start`, `join`, `next`, `attack`, `cast`, `check`, `save`, or `end`;
- provider version and integration version;
- a durable status: `REQUESTED`, `COMMITTED`, `FAILED`, or `UNKNOWN`;
- a correlation reference that lets the user or DM find the Avrae result;
- explicit handling for timeout and partial completion.

The contract must specify which operations are safe to retry. In particular, an attack, spell, save, or turn advance must not be retried automatically unless the Avrae-side integration provides idempotency. A network timeout must produce `UNKNOWN` and a recovery workflow, not an unverified second mechanic.

Quartermaster's existing FAST/DEFERRED rules still apply:

- persist a DEFERRED receipt before an external provider call;
- never make an external call while holding a SQLite write transaction;
- acknowledge Discord promptly;
- record the provider outcome after the call;
- do not claim success merely because the request was sent.

## Proposed player experience

The `/quartermaster` launcher becomes the table's front door:

### Session

- Start or resume session.
- Show the current session and active combat reference.
- End session with continuity and loot handoff.

### Combat

- Start combat.
- Add or join combatants.
- Show whose turn it is.
- Select an action.
- Resolve attack, spell, check, save, or other action through Avrae.
- Record combat end and unresolved consequences.

### Shared state

- Grant, claim, and close loot.
- View and mutate treasury through existing Quartermaster workflows.
- Resolve character belongings after lifecycle changes.

The combat buttons should not ask Quartermaster to calculate mechanics. They should dispatch to the Avrae provider or, during the hosted-Avrae fallback phase, produce a guided handoff.

## Research and decision gates

### Gate 1 decision, 2026-08-14

**Answered: no, for now.** The table is not taking on a self-hosted Avrae fork.

The price is MongoDB, Redis, a forked Avrae, and a fork-sync obligation that does not end.
The integration seams the spike would use — `Combat.from_ctx`, `Combat.commit` — are Avrae
internals, not a supported API, so every upstream pull is a chance for the Cog to break. For
one home table, that is an operational burden that outlives the benefit.

The consequence is that one-click execution of Avrae mechanics is off the table, and should
be described that way rather than as work that is coming. Everything downstream of this gate
— slices beyond 3, gates 2 through 5 — is parked, not scheduled.

Revisit only if someone at the table actively wants to operate that infrastructure. Nothing
in the current build depends on the answer changing.

One direction remains genuinely unexplored and does not need a fork: Avrae aliases can emit
signed invocation data, which would let combat events flow Avrae → Quartermaster rather than
the reverse. The catch is that Draconic aliases appear to have no outbound network access, so
the signature would land as channel text and Quartermaster would need message-content intent
to read it. That is worth half an hour against current Avrae documentation before the door is
considered shut. It is an observation channel, not an execution one, and it would not change
the authority boundary.

### Gate 1: deployment choice

Decide whether the table accepts operating a self-hosted Avrae fork. Record:

- host and operating system;
- MongoDB and Redis placement;
- Avrae update and fork-sync policy;
- bot ownership and Discord permissions;
- backup and restore expectations;
- whether Quartermaster and Avrae share one bot surface or remain separate.

No true one-click mechanics work should be promised before this gate passes.

### Gate 2: Avrae extension spike

In a disposable test guild, prove the smallest Avrae-side extension:

- load a custom Cog or equivalent extension;
- receive a user-authenticated command/component;
- locate the active combat by channel;
- read combat metadata;
- perform one harmless state-changing operation;
- commit through Avrae's own model path;
- return a correlation ID and result to Quartermaster.

The spike must also determine whether the integration belongs inside Avrae, behind an authenticated local RPC endpoint, or in another supported extension boundary.

### Gate 3: actor and idempotency semantics

Prove that the integration can preserve:

- the real Discord actor;
- Avrae's normal DM/GM and turn permissions;
- one execution for one requested action;
- safe handling of timeouts and duplicate deliveries;
- no privilege gained from a Quartermaster handle alone.

If this gate fails, use guided handoff rather than automated mechanics.

### Gate 4: limited combat pilot

Pilot only:

- combat start and end;
- join combat;
- initiative advance;
- one attack;
- one spell or save;
- combat status display;
- combat-to-loot handoff.

Do not begin with every Avrae command. Measure latency, failures, ambiguity, and whether the unified launcher actually reduces friction.

### Gate 5: promotion

Promote additional actions only when each action has:

- a verified Avrae execution path;
- actor authorization;
- idempotency or explicit non-retry semantics;
- a useful failure/recovery message;
- test coverage at the provider boundary;
- a clear owner for every resulting fact.

## Acceptance criteria

The integration is not ready for table use until all of the following are true:

- Players can begin a session and reach the combat workflow from Quartermaster.
- The real Discord actor, channel, and Avrae combat are correctly associated.
- Quartermaster never becomes authoritative for HP, initiative, attacks, spells, saves, conditions, or resources.
- A committed mechanic cannot be silently executed twice by redelivery or a button double-click.
- Provider timeouts become visible unresolved states rather than duplicated actions.
- Avrae output and Quartermaster continuity remain usable if the other service is temporarily unavailable.
- The DM can close combat and hand resulting loot into the existing Party Stash workflow.
- A restored Quartermaster database retains continuity and integration references without pretending to contain Avrae mechanics.
- The group can still play manually if Quartermaster is offline.

## Immediate next implementation work

Gate 1 is answered, so the queue is no longer "prove the spike". It is "make the fallback
worth having", which is work the hosted bot can do on its own:

1. Play a real session with the combat record and see whether the closeout actually gets
   used. If the DM never presses **Record spoils**, the button is in the wrong place.
2. Decide whether the encounter deserves a name or a short label. Right now two fights in one
   session are distinguishable only by timestamp, which is fine for the status card and thin
   for the export.
3. Consider surfacing the open encounter on the session projection, so the channel shows a
   fight in progress without anyone running a command.
4. Spend the half hour on the Avrae alias signature question above, and write the answer down
   either way so it stops being an open loop.

Explicitly not queued: loading the Cog, self-hosting Avrae, provider gateway implementations,
combat reference projections, and any surface that would hold an Avrae-owned number.

The provider boundary in `integration.py` stays as a contract with no live caller. It is
cheap to carry, its recovery semantics are the hard part and are already correct, and
removing it would cost a migration to destroy work we would have to redo. It should not be
read as evidence that an integration is running — the health check that watches it cannot
fail on the current build, and says so in a comment.
