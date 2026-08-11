# Quartermaster and Avrae Integration Plan

Status: planning only. No implementation is authorized by this document.

Updated: 2026-08-11

## Purpose

Make Quartermaster the table's single player-facing front door while keeping Avrae authoritative for D&D mechanics. The goal is one coherent play experience, not two competing combat databases.

The plan deliberately separates three ambitions:

1. One place to discover and start every table action.
2. One place to coordinate session, combat, and continuity workflows.
3. One-click execution of Avrae mechanics from Quartermaster controls.

The first two are Quartermaster work. The third requires an Avrae-side integration surface and must not be assumed to exist in the hosted Avrae bot.

## Research findings

Research performed against official Avrae documentation and the public Avrae repository on 2026-08-11.

### Avrae is extensible, but the important API is internal

- The Avrae repository describes a fully featured modding API for writing custom commands.
- Avrae loads a fixed set of Python Cogs during startup. A Quartermaster-specific Cog would therefore require a self-hosted Avrae build, a maintained fork, or an upstream-supported extension mechanism.
- The current Avrae combat implementation uses `disnake` command contexts and an internal `Combat` model. Combat state is persisted by channel and includes the summary message, combat DM, combatants, round, turn, current combatant, options, and metadata.
- The combat model exposes internal operations such as loading combat by channel, committing changes, advancing turns, changing combatants, and ending combat. These are useful implementation seams inside a self-hosted Avrae process; they are not a public HTTP API for another Discord bot.
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

### Fallback shape if Avrae remains hosted

If we keep using the official hosted Avrae bot, Quartermaster can still provide:

- a unified launcher;
- combat setup and closeout workflows;
- channel and command guidance;
- links or copyable command cards;
- continuity capture;
- loot and treasury handoff.

This improves the user experience but does not provide true one-click execution of Avrae mechanics. We should describe it as a guided wrapper, not a full integration.

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

## Immediate next work, still without application code

1. Confirm whether self-hosting a maintained Avrae fork is acceptable.
2. Decide whether the desired single front door should be the current Quartermaster bot or a Quartermaster Cog within a self-hosted Avrae process.
3. Inventory the table's actual Avrae commands and combat habits.
4. Build a small integration decision record from the four gates above.
5. Only after those decisions, authorize an Avrae extension spike and update the Quartermaster product specification to include the chosen boundary.

Until then, the current Quartermaster implementation remains the continuity core and should not gain speculative combat tables or mirrored mechanics state.
