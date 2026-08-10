# Quartermaster Implementation Plan

This plan implements the frozen v2.6 specification in the smallest useful increments. The workspace is currently empty, so the first phase establishes the application and operational foundation before adding shared-state mutations.

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

Use a small TypeScript/Node service with a Discord adapter and SQLite persistence. Lock the exact Node, Discord library, SQLite driver, and component-support versions during the implementation spike; do not let library conventions leak into domain logic.

Suggested boundaries:

```text
src/
  domain/          inventory, treasury, sessions, invariants, domain events
  application/     workflows, authorization, receipts, handles, response policy
  infrastructure/  SQLite connection, migrations, repositories, clock, IDs
  discord/         commands, components, modals, response state machine, rendering
  projections/     state scheduler, event outbox, Discord delivery
  operations/      recovery, maintenance, backup, restore, export, health
  instrumentation/ local aggregates and redacted logging
tests/
  unit/            pure domain and policy tests
  integration/     SQLite transaction and recovery tests
  acceptance/      core failure gate and Discord adapter fakes
docs/
```

The domain and application layers must be runnable with fake transport and clock implementations. Discord calls are adapters and never run inside a SQLite write transaction.

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
- items/stacks and unique item instances;
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

### 9. Evidence-gated expansion

After real table use, evaluate each candidate independently using its zero-code baseline, observed friction, smallest experiment, success signal, and removal criterion. Do not create a combined "full feature" milestone.

## Test strategy

Every mutation should have tests at three levels:

1. Pure domain tests for invariants, quantity semantics, lifecycle, read sets, and event construction.
2. SQLite integration tests for transaction atomicity, constraints, migrations, receipts, handles, outbox insertion, recovery, and backup/restore.
3. Adapter/acceptance tests with a fake Discord transport for response timing, follow-ups, edits, pins, retries, permissions, rate limits, and projection scheduling.

The release gate is the specification's core failure gate plus export/restore equivalence, projection recreation, startup recovery, privacy-path checks, and measured acknowledgement latency inside the configured budget.

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
- State projections converge and event projections preserve order.
- Export remains readable during outage.
- The core failure gate passes on the target host.
- Optional domains remain disabled until evidence promotes them.
