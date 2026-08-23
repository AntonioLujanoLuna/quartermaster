# Dice, Character Sheets, and Explainable Mechanics

Status: Stage 1 Dice slice implemented and live-accepted in the Activity on 2026-08-23;
second-client propagation remains open with the broader Activity release gate.
Stages 2-5 remain proposed. This document does not authorize a local D&D mechanics engine.

This is a post-v2.6 extension plan for the Activity and the existing Avrae boundary. It
does not change SQLite's role as Quartermaster's canonical store for continuity, inventory,
receipts, projections, and session workflow. It also does not make Quartermaster authoritative
for Avrae-owned D&D mechanics unless a later decision explicitly reopens that boundary.

## The problem to solve

The current Combat surface can open and close a Quartermaster encounter and show a read-only
Avrae status result. It does not yet explain or execute an attack, spell, save, damage result,
armor calculation, or enchantment.

The desired experience is not merely a button labelled **Attack**. A player should be able to
see what will be used, what was rolled, what modifiers contributed, what defense was tested,
and what remains uncertain if the provider is unavailable.

For a D&D/Avrae table, the vocabulary should be explicit:

- AC rather than generic “armor points”;
- hit points and temporary hit points;
- attack bonus and damage bonus;
- ability modifier and proficiency bonus;
- saving throw and save DC;
- spell attack modifier, spell slots, concentration, and duration;
- resistance, vulnerability, immunity, and conditions.

If the table uses another rules system, these are presentation terms to replace, not hidden
assumptions to bake into the generic inventory model.

## Decisions that must stay visible

### 1. Source of truth

The current integration plan says Avrae is authoritative for D&D mechanics. That remains the
default path:

- Quartermaster owns the Activity workflow, labels, continuity, receipts, and audit trail.
- Avrae owns attacks, spells, saves, HP, initiative, conditions, resources, and their rules.
- Quartermaster displays structured results and links them to the current session and actor.
- An unavailable provider is shown as unavailable or unresolved, never as an invented inactive
  combat or a locally guessed result.

A local Quartermaster rules engine is a separate product decision. It must not appear gradually
because a convenient helper started calculating a bonus. If that decision is ever made, it
needs its own authority, migration, rules-version, and compatibility plan.

### 2. Dice ownership

The general-purpose Dice view can be Quartermaster-owned. It is useful for table rolls that do
not belong to Avrae and is a small, testable foundation for later explanations.

Native D&D rolls remain provider-owned unless the source-of-truth decision changes. A future
Avrae result should contain its roll trace; Quartermaster should render that trace rather than
recalculate it from a partial character snapshot.

### 3. Inventory shape

Ordinary campaign loot remains quantity stacks. A rules-bearing equipped object may eventually
need an identity for enhancement, attunement, charges, or per-object notes, but that is not a
reason to turn every potion and rope into a unique instance now.

The first character-sheet slice should use imported equipment references or a read-only
snapshot. It should not silently redesign the existing inventory tables.

## Proposed stages

### Stage 0 — Close the current Activity release gates

Before calling the mechanics surface ready for a real table, finish the already-open checks:

- two-client live propagation;
- reconnect after bot/API interruption;
- token renewal after expiry;
- mobile layout and backgrounding;
- non-owner DM-route refusal;
- one full evening from the Activity.

These are not prerequisites for local Dice development, but they are prerequisites for claiming
that a mechanics surface is usable during play.

### Stage 1 — Dice view

Build the smallest useful screen and API:

- `POST /api/dice/roll` with the authenticated actor from the session token;
- a bounded grammar such as `d20`, `2d6+3`, `1d20-1`;
- explicit advantage and disadvantage rather than arbitrary expression syntax;
- server-side cryptographically secure randomness;
- a label, visibility (`PUBLIC` or `DM_ONLY`), and optional purpose;
- individual die values, modifiers, total, and natural-one/natural-twenty markers;
- a durable event or roll record when the table wants the result in continuity;
- idempotency for a retried request, so a browser retry cannot produce two rolls.

The parser must never use `eval`. It must bound the number of dice, sides, and modifier, and
return a readable refusal for malformed or excessive input.

The Activity should present common actions as controls—**d20**, **Advantage**, **Disadvantage**,
and **Custom roll**—so a player does not need to remember the grammar. The current first slice
provides the bounded expression form, explicit mode selector, and safe d20, Advantage, and
Disadvantage form presets. The presets fill the expression and mode without silently submitting a
public roll. The result should remain
usable when the live feed is unavailable; the header should say whether the roll was recorded
and whether the table feed is currently live.

Exit criteria:

- deterministic tests can inject a random source;
- two identical idempotency keys produce one result;
- a client cannot choose the result or actor;
- public and DM-only visibility are enforced on reads;
- the Activity displays a readable breakdown on desktop and a narrow viewport;
- one live roll is visible in the session history if the table chooses to record rolls.

### Stage 2 — Character dossier, read-only first

Add a character view that explains the numbers an action would use without pretending
Quartermaster owns the sheet.

The initial snapshot contract should carry:

- character name and provider/source reference;
- system and rules version;
- level and proficiency bonus;
- ability scores and modifiers;
- AC, HP, temporary HP, and initiative;
- saving throws;
- spell attack modifier, save DC, and visible spell resources;
- equipped weapon, armor, and shield references;
- observed-at time and source freshness.

The Activity should show a stale or unavailable indicator. A cached sheet is useful for
orientation, but it must not look current if the provider has not confirmed it.

The first version should support one explicitly documented import/source path. It should not
accept arbitrary client JSON as a character sheet, and it should not let a stale snapshot
authorize a mechanic.

Automatic bonus detection is deliberately not enabled by the roster or by a local helper. A
character name is not a character sheet. Until a verified snapshot or provider result supplies
the value, the player enters a bonus explicitly in the Dice expression (for example, `d20+5`).
This keeps the displayed number explainable and prevents a second rules engine from quietly
appearing in the Activity.

Exit criteria:

- the player can understand their own attack bonus, AC, HP, and save DC;
- each displayed number has a source or “not supplied” explanation;
- a provider outage produces a clear stale/unavailable state;
- the snapshot is versioned so a rules update cannot silently reinterpret old results.

### Stage 3 — Explainable action cards

Before one-click execution, make the action understandable. An action card should be a
structured rendering of a provider request or result, not a second rules engine.

For an attack, the card should be able to show:

```text
Longsword attack
d20: 14
Strength: +4
Proficiency: +3
Weapon: +1
Total: 22 vs AC 17 → hit

Damage: 1d8 = 6 + 4 Strength + 1 weapon = 11 slashing
```

For armor:

```text
AC 18
base 10 + Dexterity 2 + armor 4 + shield 2
```

For an effect:

```text
Bless: +1d4 to attack rolls and saving throws
Remaining: 7 rounds · concentration · source: Cleric
```

The result protocol should support a breakdown tree with named contributions, dice, target
values, outcome, provider correlation ID, rules version, and warnings. The renderer should
not need to know how Avrae calculated the result in order to explain what Avrae returned.

Exit criteria:

- a provider result can be rendered in Discord, Activity, and session history without three
  different interpretations;
- a failed or timed-out provider request is visibly unresolved;
- the correlation ID and provider/rules version are retained for support and audit;
- no action card claims a result that the provider did not confirm.

### Stage 4 — Limited provider-backed mechanics

This stage is blocked on a supported execution path. The current Avrae plan explicitly parks
self-hosting and one-click mechanics after Gate 1 was answered “no, for now”. Do not implement
this stage by smuggling network calls into the domain layer.

If the authority decision is reopened, pilot only:

1. one combat start/end path;
2. one actor and one target association;
3. one attack;
4. one spell or saving throw;
5. one combat status read;
6. one combat-to-loot handoff.

Provider calls must use the existing durable operation boundary. A timeout becomes `UNKNOWN`,
not an automatic retry of an attack or spell. A duplicate button press must not execute twice.

### Stage 5 — Effects and enchantments

Only after the source of truth and action protocol are proven should the surface grow beyond
simple bonuses.

The portable effect vocabulary should cover:

- source and target;
- affected stat or rule hook;
- operation and value, including dice;
- duration and round/turn expiry;
- charges or uses;
- concentration or other exclusivity;
- stacking policy;
- resistance, vulnerability, immunity, and conditions;
- provider reference and source text.

For the Avrae-authoritative path, Quartermaster stores and renders observed effect summaries,
not an independent effect engine. A local effect engine is only part of the separate decision
to make Quartermaster authoritative.

## Suggested API/result vocabulary

The exact JSON belongs in an implementation slice, but the boundaries should be stable:

```text
RollResult
  actor_id, visibility, purpose, expression
  dice: [{ sides, values }]
  modifiers: [{ label, value, source }]
  total, natural, recorded_at, source

ActionResult
  actor_id, target_id, action_kind, provider
  rules_version, correlation_id, status
  attack_roll, defense, damage, effects, warnings

EffectSummary
  effect_id, name, source, target, contributions
  duration, remaining, concentration, provider_reference
```

These are explanation/result shapes, not permission to add HP, AC, or initiative columns to
`combat_encounters` under the current authority decision.

## Testing and live acceptance

Every stage needs both domain/API tests and a real Activity check. The important cases are:

- malformed and oversized dice expressions;
- deterministic dice and idempotent retries;
- public versus DM-only rolls;
- stale character snapshots;
- provider timeouts and unresolved results;
- natural 1 and natural 20 display;
- advantage/disadvantage explanation;
- resistance and effect contribution display;
- a second client receiving a recorded result;
- mobile readability of the full breakdown.

The first implementation slice is now locally complete at Stage 1. Stage 2 can follow once the Dice view has
been observed in real play rather than only in this acceptance run. Stage 4 remains parked unless the group
explicitly accepts the Avrae operational burden or a supported execution surface becomes available.

The Stage 1 live acceptance on 2026-08-23 used the configured Discord Activity after restarting
the managed bot onto the current Dice route wiring. A public `d20+5` rendered as `d20: [17]`,
modifier `+5`, total `22`, and natural `17`; a public advantage `d20` rendered both attempts
and identified the counted one; a DM-only `d20` rendered its result but stated that it was not
added to the session log. The two public results appeared in the Session 3 Discord history
thread. This accepts the first Dice slice; it does not select a character-sheet contract.

## Immediate next implementation work

1. Finish the current Activity release-gate checks that do not require new domain code.
2. Use the accepted Stage 1 Dice slice in a real play session and observe which character
   values the table actually asks for.
3. Only after that observation, design the read-only character snapshot/import contract. Do
   not build a complete character-sheet editor from assumptions.

## Explicitly not queued

- a full D&D rules engine;
- local HP/initiative/condition authority alongside Avrae;
- arbitrary effect scripting;
- automatic retries for attacks, spells, saves, or turn advances;
- converting every existing quantity stack into a unique item instance;
- mobile polish before a first usable desktop Dice slice;
- self-hosted Avrae deployment without a new explicit decision.
