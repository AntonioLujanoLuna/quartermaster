# Quartermaster Implementation Plan

This plan implements the frozen v2.6 specification in the smallest useful increments. The initial application and operational foundation now exists as a Python 3.11+ service using `discord.py`, SQLite, and `uv`; the architectural boundaries below remain the governing design.

## Delivery boundary

The initial release includes:

- one configured Discord guild and table;
- Party Stash shared inventory;
- minimal Session lifecycle and `where_ended` continuity;
- append-only ledger and domain events;
- SQLite canonical state;
- durable interaction receipts for FAST and DEFERRED work;
- opaque handles with atomic single-use consumption;
- state projections and FIFO event outbox;
- startup recovery, maintenance, backup/restore, export, health, and local aggregate instrumentation.

Your Pack, Journal, Parking Lot, Downtime, faction clocks, rich continuity, and Undo remain outside the initial build until their evidence gates are met.

## Initial technical shape

Use a small Python service with a Discord adapter and SQLite persistence. Lock the exact Python, `discord.py`, SQLite, and component-support versions during the implementation spike; do not let library conventions leak into domain logic.

The boundaries below are what the code actually holds. They were planned as a directory per
layer and built as one flat package, `src/quartermaster/`, because at this size a package per
boundary buys nothing an import does not: what the plan is asserting is which module may call
which, and that is enforced by review and by the import graph rather than by the tree.

```text
src/quartermaster/
  domain            inventory, loot, currency, characters, sessions, combat, events, naming
  application       receipts, handles, response, integration, recovery, avrae_handoff
  infrastructure    db (connection, pragmas, migrations, transactions), clock, config
  discord           discord_commands, discord_panels, discord_views, discord_common,
                    discord_adapter, discord_projection, rendering, narrative
  projections       projections (state scheduler, event outbox), discord_projection
  operations        operations (maintenance, backup, restore, health), export, __main__
tests/
  test_core         domain, policy, SQLite transaction, migration, and recovery tests
  test_discord_surface  the panel surface driven the way the table drives it
  test_integration, test_combat, test_avrae_handoff
docs/
```

Two rules are load-bearing regardless of the tree: the domain and application modules must be
runnable with fake transport and clock implementations, and Discord calls are adapters that
never run inside a SQLite write transaction. `discord_*` may import the rest; nothing else may
import `discord_*`.

## Work sequence

### 0. Product and platform spike

Before building workflows:

1. Confirm the target Discord library's slash-command, button, select, modal, ephemeral-response, follow-up, edit, pin, and rate-limit APIs.
2. Measure acknowledgement latency on the intended host with a minimal bot.
3. Confirm SQLite WAL, busy-timeout, online-backup, and migration behaviour on the target runtime.
4. Decide channel IDs, DM/admin role IDs, guild ID, retention settings, freshness budgets, and backup destination policy.
5. Record the choices in an implementation decision record; only change the v2.6 architecture when the spike or an acceptance test proves a contradiction.

Exit: a checked-in skeleton can receive a test interaction, acknowledge it safely, and report measured timing.

### 1. Repository and runtime foundation

Create the project, formatting/linting/type-checking, test runner, configuration validation, structured logging, graceful shutdown, and dependency-locking setup. Add a fake Discord transport and a controllable clock for deterministic tests.

Add configuration for the single guild, channel/role IDs, database path, retention periods, deadlines, projection budgets, backup settings, and log privacy mode. Fail startup on missing or invalid required configuration.

### 2. SQLite schema and transaction primitives

Implement ordered migrations and a single database access module that applies pragmas, opens WAL mode, sets a bounded busy timeout, and exposes explicit read and write transaction helpers.

Initial tables should cover:

- schema metadata and migrations;
- items/stacks (see the note below on unique item instances);
- characters and lifecycle state;
- sessions;
- ledger and append-only domain events;
- interaction receipts;
- opaque interaction handles;
- confirmations/modal context where needed;
- state projection targets and dirty/version state;
- event outbox;
- backup/maintenance outcomes and local aggregate counters.

Encode uniqueness, non-negative quantities, one active session, and active ownership constraints in the database wherever practical, then enforce them again in application/domain code.

On unique item instances, specification 30.2 and the unique half of 31: not built, deliberately, and this plan no longer asks for them. Every item in the campaign is a quantity stack merged by `credit_stack` under the identity in specification 30.3. The only thing 30.2 carries that a stack does not is per-object state — attunement and per-instance notes — and nothing on the surface reads either; the table tracks attunement on character sheets, which is where the players already look for it. A second item shape would have to be threaded through every path items travel, and each of those paths is where the quantity arithmetic that the core failure gate covers actually lives.

The cost of the decision is that reversing it is a migration, not a feature: unique objects that were recorded as stacks have no per-object identity to recover. If attunement, per-object notes, or one-of-a-kind provenance ever need to be canonical rather than remembered at the table, that is the evidence this gate wants, and the work belongs in section 9 rather than here.

Exit: migrations create a fresh database, a transaction rollback leaves no partial mutation, and a consistent database export can be produced.

### 3. Application protocol and recovery substrate

Implement the interaction execution model before domain commands:

- `UNACKNOWLEDGED -> RESPONDED | DEFERRED` atomic response state machine;
- FAST receipt protocol with absent -> COMMITTED only;
- DEFERRED receipt protocol with PROCESSING -> COMMITTED/FAILED;
- modal-submit idempotency keyed by the submit interaction ID;
- deadline-aware FAST-to-DEFERRED fallback;
- authorization and guild validation;
- opaque handle creation, schema validation, expiry, authorization re-check, and atomic single-use consumption;
- startup recovery for interrupted DEFERRED receipts;
- periodic cleanup of transient state.

Keep the logical response serializable and transport-independent so a redelivery can replay the committed result.

Exit: receipt, handle, deadline, crash-window, malformed-input, expiry, replay, and modal-submit tests pass without Discord network access.

### 4. Projection and delivery infrastructure

Implement:

- permanent state projection targets with message lookup/recreation;
- per-target freshness budgets and fair scheduling;
- coalesced state renders that never regress;
- FIFO event outbox delivery per destination;
- retry/backoff based on Discord-provided rate-limit information;
- pin/edit permission checks;
- delivery errors that affect health but never roll back canonical state.

Place permanent Party Stash state in the low-traffic inventory channel and session events in the active session thread. Keep all external calls after transaction commit.

Exit: projection failures retry, deleted projections are recreated, state traffic and event traffic cannot starve one another, and outbox order survives retries.

### 5. Minimal Session and read-only Party Stash

Add DM launchers and the smallest useful read-only experience:

- Start Session with explicit stale-active-session resolution;
- End Session with at most one required narrative input;
- active session and previous endpoint display;
- Party Stash browse/read view;
- automatic permanent projection rendering;
- human-readable export containing stash, treasury if enabled, session continuity, recent relevant history, timestamp, and schema version.

Exit: restart reconstructs projections, missing Party Stash messages are recreated, stale sessions are never silently closed, and export is useful while the bot is unavailable.

On "previous endpoint display": for most of this build that meant one line on a pinned state
projection and a paragraph in the export, which is the DM's document. The product is named for
continuity and the surface the table opens an evening on said only whether a session was
running. Specification 29 is now built where it belongs — the home panel names where the last
session stopped while no session is running, and a **Last time** panel reads that endpoint back
with the tail of what happened, through the same renderer the session log uses. Nothing on it
is authored twice: the endpoint is the one sentence End Session asks for, and the recap is
derived from the ledger, so a recap cannot describe an evening differently from the log the
table watched it in. The full record stays the export's job, and the panel says so when it is
showing only part.

### 6. Shared inventory mutation

Implement Grant, Take, and Loot Drop workflows in this order:

1. item/stack creation and provenance;
2. absolute quantity requests with sufficiency checks;
3. relative requests such as Take all with confirmation on semantic staleness;
4. single-use Take/Give/confirm handles;
5. Loot Drop creation, claim, manual close, session-close close, and absolute expiry;
6. ledger/domain events and session-thread event projections;
7. useful expired/consumed/conflict responses that redirect users to current state.

Use read sets to guard only semantically relevant state. Preserve valid independent actions and reserve confirmation for changed meaning or failed preconditions.

Exit: the complete core failure gate in the specification passes, including final-item races, independent-item races, redelivery, same-handle double-click, independent handles, crash-after-commit, closed drops, and stale sessions.

### 7. Currency and transfers

Add integer-only treasury and character currency only after the shared inventory gate is green:

- treasury grants and corrections;
- absolute and relative split semantics;
- Give/transfer with atomic ownership changes;
- recipient-set and read-set validation;
- ledger/event/projection integration;
- export and backup coverage.

Keep character lifecycle changes separate from possession movement; lifecycle transitions must never silently move inventory.

### 8. Operations and resilience hardening

Complete the maintainer surface:

- SQLite Online Backup API or `VACUUM INTO` snapshot flow;
- snapshot validation, compression/copy policy, retention, and failure health state;
- restore procedure and equivalent-export verification;
- health states for database, schema, Discord surfaces, outbox, projections, and backups;
- migration interruption and unsupported-schema handling;
- degraded-operation runbook and one practiced fallback exercise;
- observability with redacted privacy-class paths.

Run the extended resilience suite as each corresponding feature lands. Pause Scene is not enabled by default; if enabled later, verify that actor identity is absent from every durable path, including receipts, events, outbox, projections, logs, and analytics.

### 8a. Surface lifetime

The plan was written for a command surface and the product is now one command and a panel,
so the constraint that governs the surface is one the sequence above never names: **a
component view has a lifetime, and a control that has outlived it is a dead end.** Past its
timeout a view stops listening, discord.py never dispatches the press, nothing acknowledges
the interaction, and Discord tells the player "This interaction failed" — the same sentence
a crash produces, on the one surface where the difference between "refused" and "unknown"
is the whole point.

Every view therefore retires itself: on timeout it replaces its own controls with the
reason they are gone and, where the view knows a way back, one control that renders the
panel again out of current state. Three constraints hold it together:

- A view is bound to the interaction that rendered it, because an ephemeral message can be
  edited only through the interaction or webhook that produced it.
- Navigation replaces a panel in place, so several views share one message across an
  evening. Only the view currently on screen may retire itself; an earlier one timing out
  later must leave the message alone.
- Reopening mints new state. A take panel's controls are single-use handles, so its way
  back is a fresh render, never a restored view.

Deliberately not adopted: specification 22's 90-second confirmation TTL. A confirmation's
real deadline is the handle it carries, which lives five minutes, and expiring the view
first would take away a confirmation the table could still have answered.

Still a dead end: a panel that outlives the process. Nothing in memory survives a restart,
so an old ephemeral message keeps controls that answer to nobody, and the interaction that
would edit it is gone. Reopening from `/quartermaster` is the only route back.

### 9. Evidence-gated expansion

After real table use, evaluate each candidate independently using its zero-code baseline, observed friction, smallest experiment, success signal, and removal criterion. Do not create a combined "full feature" milestone.

## Test strategy

Every mutation should have tests at three levels:

1. Pure domain tests for invariants, quantity semantics, lifecycle, read sets, and event construction.
2. SQLite integration tests for transaction atomicity, constraints, migrations, receipts, handles, outbox insertion, recovery, and backup/restore.
3. Adapter/acceptance tests with a fake Discord transport for response timing, follow-ups, edits, pins, retries, permissions, rate limits, and projection scheduling.

The release gate is the specification's core failure gate plus export/restore equivalence, projection recreation, startup recovery, privacy-path checks, measured acknowledgement latency inside the configured budget, and one session played against the live guild.

On the latency item: the local metric histograms built for it were removed in August 2026, because at one table's interaction volume percentiles cannot carry meaning. The measurement is a log line per interaction carrying what its acknowledgement cost, and a warning whenever one crosses the configured internal hard deadline. That is enough to answer the gate from an evening of real play, which is the only place the answer exists; it is not enough to draw a distribution, and the plan no longer asks for one.

On the played session: it is a gate item rather than a nice-to-have because every other item on this list is answerable from a test suite, and none of them can tell you that a control is in the wrong place, that a refusal reads as an accusation, or that the DM never presses the thing the design assumed they would. A first-launch Activity smoke has now exercised the configured guild, real HTTP, and the live service path, but the full build has not been exercised through a complete evening. The checklist that gate runs against lives in the next-session handoff, under "Not yet verified live"; a passing suite moves an item out of "unimplemented", never out of "unverified", and the plan should not be read as claiming otherwise. Until the complete evening is run, treat the gate as open regardless of what CI says.

## First implementation slice

The first coding slice should be deliberately narrow:

1. Create the project skeleton and configuration validator.
2. Add the SQLite connection, migration runner, schema metadata, and one health check.
3. Add the fake Discord transport and response state machine.
4. Implement FAST and DEFERRED receipt repositories with tests.
5. Add a minimal startup/recovery command and human-readable database export.

This slice establishes the correctness and operational substrate before any inventory mutation is exposed to the table.

## Definition of done for the initial release

- A clean deployment can restore from repository, configuration, and a valid backup.
- The table can play without Quartermaster.
- Canonical state survives Discord delivery failure and restart.
- Replayed interactions and UI intents cannot duplicate mutations.
- No control answers with silence: an unexpected failure, a spent or expired handle, and an
  expired view each say what happened and where to go next.
- The table can pick up where it stopped: the endpoint End Session recorded, and what had
  happened by then, are readable from the surface rather than only from the export.
- One session has been played against the live guild.
- State projections converge and event projections preserve order.
- Export remains readable during outage.
- The core failure gate passes on the target host.
- Optional domains remain disabled until evidence promotes them.
