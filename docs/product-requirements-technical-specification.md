# Discord D&D Table - Product Requirements & Technical Specification

**Status:** Draft v2.6 - supersedes v2.5  
**Owner:** Antonio  
**Audience:** DM + maintainer

---

## 0. Changelog from v2.5

v2.6 is the final specification pass before implementation.

It does not introduce new product domains. It resolves the remaining defects in interaction idempotency, transient-state lifecycle, Discord execution timing, projection fairness, privacy handling, and deployment topology.

| Area | v2.5 | v2.6 |
|---|---|---|
| FAST idempotency | `PROCESSING` implied | Atomic protocol has only absent → `COMMITTED` |
| DEFERRED idempotency | Same protocol as FAST | Explicit durable `PROCESSING → COMMITTED / FAILED` lifecycle |
| Modal idempotency | Implicit | `MODAL_SUBMIT` interaction id is the mutation idempotency key |
| Double-click | Attributed partly to concurrency semantics | Single-use mutation handles consumed atomically with mutation |
| Mutation handles | `consumed_at` optional concept | Explicit `single_use` semantics |
| Loot Drop lifecycle | Manual/session closure | Manual, session closure, and absolute expiry |
| Session lifecycle | Normal start/end only | Stale active-session reconciliation on Start Session |
| Server topology | Removed in v2.5 | Restored with state/event placement rules |
| Projection fairness | Party Stash-specific freshness | Per-target freshness budgets and starvation prevention |
| Transient-state retention | Receipt cleanup only | Handles, confirmations, receipts, expired drops cleaned explicitly |
| Export | Committed but unscheduled | Phase 2 deliverable |
| ACK fallback | Static FAST/DEFERRED classification | Deadline-aware FAST → DEFERRED fallback |
| Pause Scene privacy | Logs/analytics redacted | Every durable persistence path redacted |
| Rate-limit assumptions | General scheduling model | Runtime follows Discord-provided bucket information |
| Specification status | Architecture frozen | Specification frozen after v2.6 |

### 0.1 Purpose of v2.6

v2.5 established the right architecture but left several runtime states underspecified.

The key corrections are:

1. FAST and DEFERRED interactions have fundamentally different persistence protocols.
2. A double-click is not necessarily a concurrency conflict.
3. UI intent replay is prevented by consuming a single-use handle.
4. A Loot Drop cannot remain actionable forever because the DM forgot to close a session.
5. State-projection freshness applies to every state target, not only the Party Stash.
6. Privacy guarantees apply to every durable path, not only ordinary application logs.
7. Interaction timing is managed by a state machine rather than a blind timer.

### 0.2 Specification freeze

After v2.6, changes to the architectural specification require one of:

- evidence from the Discord component/library spike,
- a failed acceptance/failure test,
- repeated friction observed during actual sessions,
- a concrete implementation contradiction.

Do not continue revising architecture based only on hypothetical edge cases.

---

# 1. Product identity

Quartermaster is a lightweight **table continuity companion** for a Discord-based D&D campaign.

Avrae answers:

> What mechanically happens?

Quartermaster answers:

> What does the group own?  
> What did the group decide?  
> Where did the last session leave off?  
> What needs continuity between sessions?

Bounded domains:

```text id="ujfwn1"
STASH
    Shared inventory, ownership, treasury

SESSION
    Session lifecycle and continuity

JOURNAL
    Decisions, goals, unresolved threads

DOWNTIME
    Between-session player intentions
```

Optional small utilities:

```text id="l6ue7n"
PARKING LOT
PAUSE SCENE
```

Only Stash plus the minimal Session substrate are committed initial product scope.

Everything else remains evidence-gated.

---

# 2. Product goals

| ID | Goal | Success signal |
|---|---|---|
| G1 | Reduce administrative friction | Routine actions require no syntax |
| G2 | Prevent lost shared state | Shared loot and treasury remain known |
| G3 | Keep DM overhead low | Routine administration takes under a minute |
| G4 | Never block play | Campaign continues when Quartermaster is unavailable |
| G5 | Keep shared state ambiently visible | Permanent state projections converge automatically |
| G6 | Preserve trust under failure | Retries and crashes never silently duplicate/corrupt state |
| G7 | Keep correctness invisible | Independent actions do not generate unnecessary warnings |
| G8 | Preserve session continuity | "Where were we?" is quickly answerable |
| G9 | Build only demonstrated value | Optional features pass evidence gates |
| G10 | Make transport behaviour predictable | Discord timing/rate limits are explicitly accounted for |

---

# 3. Non-goals

- Virtual tabletop functionality.
- Maps, tokens, fog of war.
- Combat UI.
- Mirroring Avrae state.
- HP, AC, initiative, attacks, saves, conditions, spell slots.
- Rules adjudication.
- DM automation.
- Character building.
- Bidirectional Avrae synchronization.
- Automatic D&D Beyond synchronization.
- Generic campaign wiki.
- Generic NPC/location database.
- Relationship graph.
- Quest engine.
- External analytics platform.
- Multi-tenancy.
- Cross-guild deployment.
- Public distribution.
- AI recap generation in the critical path.

---

# 4. Constraints

- Single guild.
- One table.
- Approximately 3-5 players plus one DM.
- Mobile first.
- Near-zero recurring cost.
- SQLite intentionally preferred.
- Self-hosted.
- Discord is an external asynchronous API with hard interaction deadlines.
- Avrae remains the authoritative mechanical system.
- Quartermaster must not be required to play the game.

---

# 5. Users and roles

| Role | Primary needs |
|---|---|
| DM | Administer shared state and continuity |
| Player | Act without learning syntax |
| Maintainer | Deploy, diagnose, backup, restore |

Player-facing vocabulary describes table intentions.

Infrastructure terminology stays in maintainer/admin surfaces.

---

# 6. Build vs. buy

## 6.1 Existing tools

| Capability | Tool |
|---|---|
| Dice | Avrae |
| Sheets | Avrae |
| Initiative | Avrae |
| SRD lookup | Avrae |
| Class resources | Avrae counters |
| Rechargeable charges | Avrae counters where appropriate |
| Scheduling | Sesh |
| IC posting | Tupperbox, optional |
| Voice recording | Craig, optional |

## 6.2 Initial Quartermaster scope

Committed:

- Party Stash.
- Shared loot.
- Shared treasury.
- Ownership.
- Provenance.
- Minimal Session lifecycle.
- Ledger.
- Export.
- Backup/restore.
- Projection infrastructure.

Evidence-gated:

- Your Pack.
- Journal.
- Parking Lot.
- Downtime.
- Faction clocks.
- Rich session continuity.

Preference-gated:

- Pause Scene.

---

# 7. Server topology

Recommended topology:

```text id="u12r5p"
#table-talk
#rolls
#party-inventory
#session-log
#character-sheets
#rules-questions

DM-only channel
```

Optional later:

```text id="dq43c1"
#campaign-journal
```

Do not create a new channel merely because a database table exists.

## 7.1 Channel roles

### `#party-inventory`

Low-traffic state channel.

Contains:

- Party Stash permanent state projection.
- optionally compact Loot Drop cards while active.

Human chatter should be discouraged or disabled.

### `#session-log`

Contains one thread per session.

Event projections go to the active session thread:

```text id="bs7co9"
Edrin took the Silvered Dagger.
DM added 80 gp.
The party converted 1 pp into 10 gp.
```

### DM-only channel

Contains the DM state surface and administrative launchers.

## 7.2 Placement invariant

> **Permanent state projections live in low-traffic surfaces. High-frequency event projections live in session/event surfaces.**

This is primarily a UX rule and secondarily reduces unnecessary API contention.

Do not assume specific Discord REST routes share a particular bucket. Runtime scheduling responds to actual Discord rate-limit information.

---

# 8. Authority boundary

## 8.1 Avrae authority

Avrae owns:

- character mechanics,
- combat resolution,
- initiative,
- HP,
- conditions and effects,
- spell slots,
- class resources,
- rechargeable counters where configured.

## 8.2 Quartermaster authority

Quartermaster initially owns:

- shared inventory,
- shared treasury,
- ownership,
- provenance,
- session records,
- the combat encounter record,
- Quartermaster ledger.

The combat encounter record is the one place these two lists come close, so the split is
stated exactly. Quartermaster owns *that* a fight is happening: which session it belongs to,
which channel it runs in, when it opened and closed, and the DM's note on how it resolved.
Avrae owns everything *inside* the fight. Quartermaster opening or closing its record does
not start or end an Avrae combat, and the two can disagree without either being wrong — a DM
who forgets `/combat end` has stale Quartermaster bookkeeping, not a corrupted mechanic.

A column for HP, initiative, conditions, resources, or combatants on the Quartermaster
encounter would violate 8.3. A test asserts the table's exact column set to keep that from
happening by accident.

Potentially later:

- personal inventory,
- personal currency,
- journal continuity,
- downtime intentions.

## 8.3 Hard invariant

> A fact or quantity has exactly one authoritative owner.

Valid:

```text id="fvw3mi"
Quartermaster:
    Wand x1

Avrae:
    Charges 5/7
```

Invalid:

```text id="p6tws1"
Quartermaster:
    Healing Potion x3

Avrae:
    Healing Potion counter = 3
```

---

# 9. Core architecture

```text id="eoqo8c"
Discord interaction
        |
        v
Quartermaster application layer
        |
        v
SQLite canonical state
        |
        +---- state projections
        |
        +---- event projections
        |
        v
Discord
```

Discord never becomes authoritative state.

## 9.1 Mutation outline

```text id="mhn2q9"
receive
authenticate
resolve handle
deduplicate
classify execution mode
evaluate read set
validate domain
mutate
append events
dirty state projections
enqueue event projections
persist logical response
commit
respond
dispatch projections
```

The exact ordering differs by execution class, defined next.

---

# 10. Discord interaction execution model

Every interaction belongs to one execution class.

## 10.1 Response state machine

Every inbound interaction starts:

```text id="v6oknj"
UNACKNOWLEDGED
```

and transitions exactly once to:

```text id="grva0m"
DEFERRED
```

or:

```text id="m8drl9"
RESPONDED
```

A response state transition must be atomic within the application runtime.

Never attempt both a defer and a normal initial response.

---

## 10.2 FAST

Used when execution before acknowledgement is predictably local and bounded.

Examples:

- Take item.
- Give item.
- Spend currency.
- Record lightweight state.
- Resolve a simple handle.

Expected flow:

```text id="p4nhiv"
receive
resolve idempotency
perform bounded transaction
commit
send initial response
```

No:

- external network work,
- backup,
- large export,
- unbounded query,
- expensive rendering,
- remote integrations.

---

## 10.3 DEFERRED

Used when work may exceed the safe acknowledgement budget.

Examples:

- export,
- backup,
- large audit,
- reconciliation,
- future external service calls.

Flow:

```text id="419erp"
persist PROCESSING receipt
commit receipt
defer interaction
perform work
persist result
mark COMMITTED / FAILED
send follow-up
```

---

## 10.4 IMMEDIATE_UI

Used when the initial callback must open UI such as a modal.

Flow:

```text id="pwgidn"
interaction A
    -> open modal
    -> no mutation

interaction B = MODAL_SUBMIT
    -> validate
    -> mutate
```

**The modal submission is a new interaction.**

Its `interaction_id`, not the interaction that opened the modal, is the idempotency key for any mutation performed on submission.

---

# 11. Interaction latency and deadline policy

Targets:

```text id="1jndrf"
normal initial ACK target: < 1.0 s
soft application deadline: ~1.2 s
internal hard deadline:    2.5 s
Discord deadline:          ~3 s
```

Exact platform values must be verified at implementation time.

Quartermaster does not intentionally operate near the external deadline.

---

# 12. Deadline-aware fallback

A FAST operation may be converted to DEFERRED before acknowledgement if latency is unexpectedly high.

Possible causes:

- cold start,
- temporary SQLite lock contention,
- WAL checkpoint behaviour,
- CPU scheduling delay.

## 12.1 Rule

A watchdog does **not** independently send HTTP responses underneath arbitrary application code.

Instead, it signals deadline pressure to the response state machine.

Conceptually:

```text id="tw42ql"
if state == UNACKNOWLEDGED
and elapsed >= soft_deadline
and operation.can_defer
and no write transaction is active:

    atomically transition to DEFERRED
    send defer
    finish operation afterwards
```

## 12.2 Transaction acquisition

Before entering a FAST write transaction:

```text id="f4kgdg"
if insufficient acknowledgement budget remains:
    defer first
    execute mutation afterwards
```

If obtaining the SQLite write lock exceeds the FAST allowance:

```text id="gfy7g4"
abort FAST attempt before mutation
defer
retry mutation through deferred path
```

## 12.3 IMMEDIATE_UI

Cannot use generic deferred fallback when the required initial response is a modal.

Keep IMMEDIATE_UI handlers deliberately tiny.

---

# 13. No external I/O in transactions

Hard invariant:

> **No Discord or other external network call occurs while a SQLite write transaction is held.**

Transactions contain only:

- database reads required by the mutation,
- validation,
- domain mutation,
- ledger writes,
- receipt updates,
- handle consumption,
- projection state updates/outbox insertion.

Then commit.

External delivery happens afterwards.

---

# 14. Interaction idempotency

Idempotency behaviour depends on execution class.

## 14.1 Identifiers

```text id="8uhzve"
interaction_id
    external Discord idempotency key

operation_id
    internal correlation identifier
```

---

# 15. FAST idempotency protocol

FAST mutations execute atomically with their receipt.

There is no durable `PROCESSING` state.

Persistent reachable states:

```text id="79jhgu"
receipt absent
receipt COMMITTED
```

Conceptual protocol:

```text id="0jj975"
BEGIN IMMEDIATE

lookup interaction receipt

if COMMITTED:
    load stored logical response
    COMMIT/exit
else:
    perform mutation
    append domain events
    mark projections
    persist logical response
    insert COMMITTED receipt

COMMIT
```

## 15.1 Crash before commit

Neither the mutation nor the committed receipt exists.

The same interaction may be retried safely.

## 15.2 Crash after commit before Discord response

Both mutation and receipt exist.

A redelivery returns the committed logical result and does not re-execute.

---

# 16. DEFERRED idempotency protocol

DEFERRED work spans transactions and therefore has a genuine lifecycle.

Receipt states:

```text id="y4t1yr"
PROCESSING
COMMITTED
FAILED
```

## 16.1 Initial transaction

```text id="e0u6j1"
BEGIN IMMEDIATE

if receipt COMMITTED:
    load stored result
elif receipt PROCESSING:
    handle according to operation policy
else:
    create PROCESSING receipt

COMMIT
```

Then defer the Discord interaction.

## 16.2 Work completion

```text id="wpzah8"
perform work

BEGIN

persist logical result
mark receipt COMMITTED

COMMIT
```

Failure:

```text id="yr93jr"
mark receipt FAILED
store actionable failure result
```

## 16.3 Restart policy

Default:

> **Interrupted DEFERRED operations are marked FAILED during startup recovery unless the operation explicitly declares itself resumable.**

Initial deferred operations such as:

- export,
- backup,
- administrative audit,

are **not resumable**.

The user invokes them again.

Do not build general-purpose job resumption unless a future operation actually needs it.

---

# 17. Interaction receipt schema

Conceptually:

```text id="d5flq9"
interaction_receipt

interaction_id      UNIQUE
operation_id
actor_id nullable
execution_class
status
response_kind
logical_response
serialized_response nullable
created_at
committed_at nullable
failed_at nullable
```

## 17.1 Retention

Receipts are not permanent audit history.

Retain for:

- retry safety,
- short-term diagnosis.

Periodic cleanup removes receipts older than a configured retention period once they are terminal.

The ledger owns long-term history.

---

# 18. Component identifiers and handles

## 18.1 Stateless controls

Stable actions may use short identifiers:

```text id="7k4674"
qm:take
qm:browse
qm:session
```

No business state is encoded.

## 18.2 Stateful controls

Stateful actions use opaque handles:

```text id="2hbjpg"
qm:h:7Rkf3Ap
```

No quantity, item name, version, authorization, or read-set payload is encoded into Discord's identifier.

---

# 19. Handle model

Conceptual record:

```text id="0ziuc9"
interaction_handle

id
schema_version
workflow_type
action
actor_id nullable
payload
read_set_snapshot
single_use
created_at
expires_at nullable
consumed_at nullable
```

## 19.1 Authorization

Possessing a handle grants no authority.

Authorization is reevaluated during execution.

## 19.2 Single-use mutation handles

Mutation controls are single-use by default.

Examples:

- Take 1.
- Give item.
- Confirm.
- Spend 5 gp.
- Submit corrective action.

Read-only navigation handles may be reusable until expiry.

## 19.3 Atomic consumption

For single-use mutation handles:

> **Handle consumption and domain mutation occur atomically in the same write transaction.**

Conceptually:

```text id="x6s3k4"
BEGIN IMMEDIATE

load handle

if consumed:
    return replay/expired result

validate
perform mutation
set consumed_at

COMMIT
```

This is the UI-intent deduplication mechanism.

---

# 20. Double-click semantics

A double-click may produce two distinct Discord interaction ids.

Interaction receipts therefore cannot deduplicate it.

Read-set semantics also may not prevent both mutations.

Example:

```text id="pyyaee"
Potion x3

Take 1
Take 1
```

Two independent `Take 1` actions are valid if enough remains.

The distinction is the handle.

## 20.1 Same mutation handle clicked twice

Represents replay of one UI intent.

Result:

```text id="55ap7q"
at most one mutation
```

because the handle is single-use.

## 20.2 Two independently generated Take handles

Represent two independent user intents.

Both may execute if both remain valid.

This is correct.

---

# 21. Handle lifecycle and cleanup

Handles are transient UI infrastructure.

They are not historical data.

Periodic cleanup removes:

### Expired handles

```text id="vz7aq0"
expires_at < now - expiry_retention_margin
```

### Consumed handles

```text id="3u6ccq"
consumed_at IS NOT NULL
and consumed_at < now - replay_retention_margin
```

Typical retention may keep recently consumed handles briefly for debugging.

Exact retention is configurable.

---

# 22. Workflow TTLs

Initial defaults:

| Workflow | TTL |
|---|---:|
| Confirmation | 90 s |
| Take / Give | 5 min |
| Your Pack | 10 min |
| DM workflow | 10 min |
| Modal context | 10 min |

Expired:

```text id="9hchrp"
This view has expired.

[ Open again ]
```

Discord token lifetime is transport state, not application workflow state.

---

# 23. Concurrency model

## 23.1 Read-set principle

Every mutation declares the state its meaning depends upon.

Only changes in that state matter.

## 23.2 Outcomes

| Situation | Result |
|---|---|
| Relevant state unchanged | Execute |
| Unrelated state changed | Execute |
| Relevant state changed but request can be reinterpreted safely only with user input | Confirm |
| Required precondition no longer holds | Reject |

Aggregate party revision never blocks an entity-scoped action merely because unrelated shared state changed.

---

# 24. Absolute and relative quantity semantics

## 24.1 Absolute

Examples:

```text id="ef59s4"
Take 1
Spend 5 gp
Drop 2
```

Meaning depends on sufficiency.

If enough remains, execute.

## 24.2 Relative

Examples:

```text id="652gab"
Take all
Give the rest
Split everything
```

Meaning depends on observed quantity.

If the defining quantity changes, confirm against current state.

Example:

```text id="7jhrgh"
There are 12 arrows now, not 20.

Take all 12?

[ Take all 12 ] [ Cancel ]
```

---

# 25. Party Stash UX

Permanent public state surface.

```text id="0a2t4g"
PARTY STASH

1 pp · 137 gp · 8 sp

Unclaimed · 6
Potion of Healing x3
Silvered Dagger
Strange brass key
+3 more

[ Take something ] [ Browse ]
```

No Refresh button.

## 25.1 Display priority

Show:

1. recently acquired items,
2. unclaimed significant items,
3. relevant consumables,
4. compact overflow count.

Browse shows full state.

---

# 26. Take flow

```text id="s93kvh"
TAKE FROM THE PARTY STASH

[ Select item... ]
```

Then:

```text id="3wff5s"
POTION OF HEALING
3 available

[ Take 1 ] [ Choose amount ] [ Take all ]
[ Back ]
```

Success:

```text id="y9k9ru"
You took 1 Potion of Healing.
2 remain.

[ Take another ] [ Done ]
```

Every mutating button is backed by a single-use handle.

---

# 27. Loot Drop lifecycle

Loot Drops are transient event cards.

```text id="zeeq4z"
NEW LOOT

Potion of Healing x3
Silvered Dagger
80 gp

[ Take something ]
```

States:

```text id="9aplcg"
OPEN
CLOSED
```

## 27.1 Closure conditions

A Loot Drop closes on the first applicable condition:

1. DM closes it manually.
2. Associated session closes.
3. `expires_at` is reached.

Initial default absolute expiry:

```text id="nizzt2"
72 hours
```

Configurable.

The expiry exists so correctness does not depend on DM session-closing discipline.

## 27.2 Closed state

```text id="8zl281"
LOOT DROP · CLOSED

This drop is no longer active.
Its items remain in the Party Stash.

[ Open Party Stash ]
```

All mutation handles belonging to the drop are invalidated or expire.

---

# 28. Session lifecycle

States:

```text id="h1dk0y"
ACTIVE
CLOSED
```

At most one Session is ACTIVE.

## 28.1 Start Session

When no active session exists:

```text id="txsc8q"
create session
bind/create log thread
set active
route events
```

## 28.2 Stale active session

If Start Session is invoked while another session remains ACTIVE:

```text id="qr4pc7"
Session 7 is still marked active.

Close Session 7 and start Session 8?

[ Close & start ] [ Cancel ]
```

Do not silently auto-close.

## 28.3 Closing a session

Session closure:

- records `ended_at`,
- optionally records `where_ended`,
- closes associated OPEN Loot Drops,
- finalizes the active log routing,
- preserves all state/history.

---

# 29. Minimal Session continuity

At End Session the only required human narrative input is:

```text id="1uhbkv"
WHERE DID YOU END?

Outside the Observatory after opening the lower gate.
```

Everything else is derived when possible.

Possible next-session surface:

```text id="xdmbpl"
LAST TIME

You ended:
Outside the Observatory after opening the lower gate.

Since then:
• Edrin took the Silvered Dagger
• Party spent 50 gp
• 2 healing potions remain

[ Session log ]
```

If Journal is later enabled, decisions/open threads may appear too.

---

# 30. Inventory model

## 30.1 Item stack

Fungible.

```text id="m9q1f3"
id
owner
normalized_name
display_name
variant_metadata
quantity
notes
version
timestamps
```

Quantity must remain positive.

## 30.2 Item instance

Unique object.

```text id="hhs60d"
id
owner
name
notes
attuned
version
timestamps
```

Quantity = 1 implicitly.

## 30.3 Stack identity

```text id="m37wvj"
owner
normalized_name
variant_metadata
```

Normalization limited to:

- case,
- whitespace.

## 30.4 Merge

Equivalent stacks may merge.

Provenance remains in event history.

---

# 31. Grant semantics

## Unique

Create a new instance.

No stale-client quantity dependency.

## Fungible

Atomic server-side upsert:

```text id="wjfo96"
existing quantity += granted quantity
```

Never:

```text id="i5u4nx"
client read quantity
client computes replacement
server writes replacement
```

---

# 32. Character lifecycle

States:

```text id="90i4hs"
ACTIVE
DEAD
RETIRED
DEPARTED
```

Allowed:

```text id="332k0g"
ACTIVE -> DEAD / RETIRED / DEPARTED

DEAD / RETIRED / DEPARTED -> ACTIVE
```

## 32.1 Lifecycle invariant

> Lifecycle transitions never move belongings.

Non-active characters:

- cannot claim,
- cannot spend,
- cannot ordinarily receive transfers,
- retain visible holdings.

## 32.2 Resolve belongings

Explicit DM mutation.

Only allowed for non-active characters.

May transfer holdings to:

- Party Stash,
- named active characters.

Reactivation never reverses a previous belongings resolution automatically.

---

# 33. Currency

Balances:

```text id="iwns8q"
cp
sp
ep
gp
pp
version
```

All integers.

No implicit conversion.

Electrum exists in schema and is hidden/disabled by default.

## 33.1 Split

Each denomination split independently.

Remainder remains with source.

Recipient set = currently active characters.

---

# 34. State projections

Examples:

- Party Stash.
- Session surface.
- DM surface.

Each target stores:

```text id="kulj4u"
target_id
discord_message_id
desired_revision
delivered_revision
dirty
dirty_since
freshness_budget
priority
next_attempt_at
in_flight
```

## 34.1 Rendering rules

1. Single-flight per target.
2. Read current DB state at dispatch.
3. Intermediate states may be skipped.
4. Delivered revision never regresses.
5. Dirty state remains until latest desired revision is delivered.

---

# 35. State-projection freshness

Freshness is configured per target.

Initial example:

| Target | Freshness budget | Priority |
|---|---:|---|
| Party Stash | 2 s | Player-visible high |
| Session surface | 5 s | Player-visible medium |
| DM surface | 10 s | Administrative |

These are initial application targets, not protocol constants.

## 35.1 Scheduling

State targets past their freshness budget are eligible for promotion ahead of ordinary event draining.

When multiple targets are overdue, scheduler considers:

```text id="5ihgjx"
normalized_lateness =
    dirty_age / freshness_budget
```

plus priority.

Conceptually:

```text id="57jfnz"
choose most overdue/high-priority state projection
render latest state
return to event draining
```

No state projection may starve indefinitely.

---

# 36. Event projections

Examples:

```text id="zvd6r3"
Edrin took the Silvered Dagger.
DM added 80 gp.
Session 7 started.
```

Properties:

- durable,
- FIFO per destination,
- non-coalescible,
- monotonic sequence number,
- retry with backoff.

No event is silently dropped because a newer event exists.

---

# 37. Projection scheduling principle

> **Event projections are lossless. State projections have bounded freshness. Neither class may starve the other indefinitely.**

Priority order:

1. Interaction acknowledgement.
2. State projection already beyond freshness bound, according to scheduler.
3. Event projection FIFO.
4. Non-overdue state refresh.

This is conceptual scheduling, not a hard assumption about Discord request buckets.

Runtime must honor actual rate-limit responses.

---

# 38. Discord rate limits

Quartermaster:

- consumes Discord rate-limit headers,
- respects retry windows,
- does not assume fixed request quotas,
- does not assume two routes share or do not share a bucket,
- backs off when instructed,
- preserves event ordering during retries,
- coalesces stale state renders.

Channel separation reduces UX interference and may reduce avoidable contention, but correctness does not depend on a guessed bucket topology.

---

# 39. Event outbox

Event projections use a transactional outbox.

Conceptual record:

```text id="cab21j"
event_outbox

id
operation_id
event_sequence
destination
event_type
payload
status
attempt_count
next_attempt_at
last_error
delivered_at
```

Inserted in the same transaction as the mutation.

FIFO preserved per destination.

---

# 40. Startup recovery

Before accepting ordinary mutations:

1. Open database.
2. Apply SQLite pragmas.
3. Validate schema.
4. Apply supported migrations.
5. Validate guild/channel configuration.
6. Validate required permissions.
7. Recover DEFERRED `PROCESSING` receipts.
8. Mark non-resumable interrupted operations `FAILED`.
9. Run transient-state maintenance.
10. Resume event outbox delivery.
11. Locate permanent state projection messages.
12. Recreate missing ones.
13. Ensure required pins.
14. Mark/render all permanent state projections from current DB state.
15. Resolve stale active-session condition only when the DM next invokes Start Session.
16. Begin accepting ordinary interactions.

Do not silently close stale sessions during startup.

---

# 41. Transient-state maintenance

A small periodic maintenance job handles:

- expired handles,
- old consumed handles,
- expired confirmations,
- terminal interaction receipts beyond retention,
- Loot Drops past `expires_at`,
- stale temporary workflow state.

This job must be:

- idempotent,
- safe to run repeatedly,
- lightweight.

No external scheduler platform is required.

---

# 42. Backup

Supported mechanisms:

1. SQLite Online Backup API, preferred.
2. `VACUUM INTO` to a fresh destination.

Unsupported:

> raw filesystem copy of a live canonical database.

## 42.1 Backup process

```text id="p39uo1"
create consistent snapshot
validate snapshot
timestamp
compress optionally
copy off-device
apply retention
record outcome
```

Backup failure degrades health.

## 42.2 Restore

Before Quartermaster becomes important:

```text id="qz2y48"
clean deployment
+ repository
+ configuration/secrets
+ latest valid backup
=
equivalent logical state
```

Permanent Discord projections are reconstructed.

---

# 43. Export

Export is committed core functionality.

Human-readable output includes:

```text id="zk017t"
Party Stash
Treasury
Quartermaster-owned character holdings, if enabled
Active session
Previous session endpoint
Recent relevant history
Export timestamp
Schema version
```

Export must remain useful when Quartermaster is down.

---

# 44. Pause Scene privacy class

If the group enables Pause Scene, it receives a dedicated privacy classification:

```text id="hw729f"
PRIVACY_REDACTED
```

The triggering actor identity may exist transiently in process memory only as required to handle the Discord interaction.

It must not be persisted in:

- ledger,
- domain events,
- event outbox,
- state-projection payload,
- application logs,
- analytics counters tied to an actor,
- interaction receipt actor field,
- workflow handle payload beyond transient routing necessity,
- error context.

## 44.1 Interaction receipt

Transport idempotency still applies.

Receipt may contain:

```text id="19ej8w"
interaction_id
operation_id
status
logical_response
actor_id = NULL
```

## 44.2 Public output

```text id="g04ql9"
Scene paused.

The table can pause, change direction,
or skip ahead before continuing.
```

No actor.

## 44.3 Production logging

Raw Discord interaction payload logging must not be enabled if it would defeat the privacy policy.

---

# 45. DM surface

Launcher, not dashboard.

```text id="mnos2p"
QUARTERMASTER

Party stash · 14 entries
Session 7 active

1 unresolved character estate

[ Grant loot ] [ Session ]
[ More... ]
```

More:

```text id="xhi1f0"
Split treasury
Correct state
Characters
Resolve belongings
Export
Audit
Health
Re-render
```

Candidate domains appear only after promotion.

---

# 46. Behavioural requirements

- **BR1.** Shared mutations are auditable.
- **BR2.** Quantities never become negative.
- **BR3.** Authorization is server-side.
- **BR4.** Currency is integer-only.
- **BR5.** FAST interactions execute atomically with committed receipts.
- **BR6.** DEFERRED interactions expose durable `PROCESSING`, `COMMITTED`, or `FAILED` state.
- **BR7.** Modal submission uses its own interaction id for mutation idempotency.
- **BR8.** A committed Discord interaction never executes its logical mutation twice.
- **BR9.** Single-use mutation handles are consumed atomically with mutation.
- **BR10.** Replaying one consumed mutation handle cannot cause another mutation.
- **BR11.** Two independently issued valid absolute requests may both succeed.
- **BR12.** Unrelated state changes never interrupt entity-scoped actions.
- **BR13.** Relative requests whose defining value changes require confirmation.
- **BR14.** Discord messages hold no unique business state.
- **BR15.** Active item ownership is unique.
- **BR16.** Transfers are atomic.
- **BR17.** History is append-only.
- **BR18.** Discord delivery failure cannot roll back committed state.
- **BR19.** Avrae and Quartermaster never share authority for one fact or quantity.
- **BR20.** Lifecycle changes never move inventory.
- **BR21.** State projections never regress.
- **BR22.** Every state projection has a bounded freshness policy.
- **BR23.** Event projections preserve FIFO ordering per destination.
- **BR24.** Loot Drops cannot remain actionable indefinitely.
- **BR25.** At most one Session is active.
- **BR26.** Stale active sessions are resolved explicitly, not silently.
- **BR27.** Healthy infrastructure remains invisible.
- **BR28.** Optional domains require evidence.
- **BR29.** End Session requires at most one mandatory human narrative input.
- **BR30.** No external I/O occurs while a SQLite write transaction is held.
- **BR31.** Privacy-class operations cannot leak actor identity through durable infrastructure.

---

# 47. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR1 | Initial acknowledgement remains within Discord's required window |
| NFR2 | Target acknowledgement latency under 1 s |
| NFR3 | FAST fallback prevents avoidable interaction timeout |
| NFR4 | Ordinary player actions feel immediate |
| NFR5 | Full state export available in human-readable form |
| NFR6 | Nightly consistent backups copied off-device |
| NFR7 | Restore tested |
| NFR8 | Player errors are actionable |
| NFR9 | Full session playable without Quartermaster |
| NFR10 | Zero mandatory recurring software cost |
| NFR11 | Restarts require no manual component registration |
| NFR12 | Permanent projections reconstruct automatically |
| NFR13 | Domain logic independent from Discord handlers |
| NFR14 | Routine UX works one-handed on mobile |
| NFR15 | Non-conflicting concurrent actions require no extra prompts |
| NFR16 | Event projections survive transient backpressure without loss |
| NFR17 | All state projections remain within configured ordinary freshness bounds |
| NFR18 | No state projection may starve indefinitely behind event traffic |
| NFR19 | No event queue may starve indefinitely behind state rendering |
| NFR20 | Privacy-class actions leave no durable actor attribution |

---

# 48. Degraded operation

If Quartermaster fails:

- Avrae remains usable.
- Dice work.
- Sheets work.
- Combat works.
- Loot goes into manual notes.
- Decisions go into Discord.
- Latest export remains readable.
- State is reconciled later.

The fallback must be practiced once.

---

# 49. Local product instrumentation

Local aggregate signals only.

No external analytics service.

Examples:

| Question | Signal |
|---|---|
| Is compact Stash enough? | Browse opens / Stash views |
| Is Take flow good? | started vs completed |
| Are prompts intrusive? | confirmations / successful Takes |
| Is freshness good? | dirty duration p50/p95/max by target |
| Is interaction latency safe? | ACK p50/p95/max |
| Does Session continuity work? | `where_ended` completion rate |
| Is Your Pack needed? | observed personal-inventory friction |
| Is Downtime helpful? | completion + reminder count |

Do not store actor-level product analytics unless a concrete product need justifies it.

Pause Scene is excluded from actor-level instrumentation entirely.

---

# 50. Feature promotion protocol

Candidate feature lifecycle:

```text id="9z0lxt"
CANDIDATE
    -> OBSERVED NEED
        -> SMALL EXPERIMENT
            -> ENABLED
```

Every candidate specifies:

1. zero-code baseline,
2. repeated failure mode,
3. evidence,
4. minimal implementation,
5. removal criterion.

Detailed candidate design is not implementation commitment.

---

# 51. Candidate: Your Pack

Zero-code baseline:

> Character sheet / D&D Beyond inventory.

Build only if repeated personal-inventory friction exists despite that baseline.

---

# 52. Candidate: Journal

Zero-code baseline:

> DM notes or one pinned/session-thread message.

Build only if:

- decisions are forgotten,
- goals are repeatedly unclear,
- Discord archaeology becomes routine.

---

# 53. Candidate: Parking Lot

Zero-code baseline:

> DM keeps one list of questions to revisit.

Build only if:

- rules questions repeatedly interrupt play,
- and deferred questions are repeatedly lost.

---

# 54. Candidate: Downtime

Zero-code baseline:

> One Discord thread between sessions.

Build only if:

- DM repeatedly chases players,
- intentions are lost,
- structured collection would reduce work.

---

# 55. Optional future Undo

Evidence gate:

> Accidental mobile actions create enough corrective work to matter.

If implemented:

- actor-bound,
- short-lived,
- guarded,
- compensating transaction,
- original history retained.

---

# 56. Rollout plan

## Phase 0 - Table works without Quartermaster

Deliver:

- server topology,
- Avrae,
- character sheets,
- session-zero rules,
- authority decisions,
- optional Pause Scene decision.

Exit:

> The entire table can play without Quartermaster.

---

## Phase 1 - Observe manually

Sessions 1-3.

Use:

- manual inventory,
- ordinary session log,
- one-line `where_ended`,
- zero-code baselines.

Collect actual friction.

Exit:

> Written observed-friction log.

---

## Phase 2 - Runtime and recovery substrate

Build:

- bot skeleton,
- SQLite schema,
- migrations,
- FAST receipts,
- DEFERRED receipts,
- deadline-aware response state machine,
- opaque handles,
- handle consumption,
- handle cleanup,
- confirmation cleanup,
- state projection scheduler,
- event outbox,
- startup reconciliation,
- backup,
- restore,
- **human-readable export**,
- read-only Party Stash,
- minimal Session record,
- local aggregate instrumentation.

Exit:

```text id="jcjf6w"
DB -> export works

DB -> Party Stash works

DB backup -> restored DB -> equivalent export works

restart -> projections reconstructed

missing Party Stash -> recreated

ACK latency -> safely inside budget
```

---

## Phase 3 - Shared inventory mutation

Build:

- Grant.
- Take.
- absolute/relative semantics.
- Loot Drops.
- Loot Drop expiry.
- event logging.
- single-use mutation handles.
- conflict UX.

Exit:

- two sessions without manual shared-inventory fallback,
- core failure gate passes,
- no projection regression,
- interaction completion rate acceptable.

---

## Phase 4 - Currency and transfers

Only if observed need exists.

Possible:

- treasury,
- split,
- Give,
- corrections,
- character lifecycle,
- resolve belongings.

---

## Phase 5+ - Evidence-gated expansion

Promote independently:

- Your Pack,
- Journal,
- Parking Lot,
- Downtime,
- richer Session continuity,
- faction clocks.

No "full build" umbrella exists.

---

# 57. Core failure gate

Required before optional feature expansion.

### 1. Final-item race

Two users attempt final copy.

Expected:

- exactly one succeeds,
- loser receives useful explanation.

### 2. Independent item race

Two users Take different items.

Expected:

- both succeed,
- no confirmation.

### 3. Absolute quantity change

User requests Take 1.

Observed stack quantity changes but remains sufficient.

Expected:

- action succeeds,
- no confirmation.

### 4. Relative quantity change

User requests Take all.

Quantity changes.

Expected:

- fresh quantity shown,
- explicit confirmation required.

### 5. Discord interaction redelivery

Same interaction id received twice.

Expected:

- mutation happens once,
- committed logical response recoverable.

### 6. Double-click same mutation handle

Two different Discord interactions reference the same single-use handle.

Expected:

- one mutation maximum,
- second sees consumed/replayed state,
- protection comes from atomic handle consumption.

### 7. Two independent Take handles

Two independently generated Take-1 handles used against sufficient quantity.

Expected:

- both may succeed.

This distinguishes valid repeated intent from UI replay.

### 8. Crash after FAST commit before response

Expected:

- mutation persists,
- receipt persists,
- duplicate redelivery does not re-execute,
- projections reconcile.

### 9. Interrupted DEFERRED operation

Expected:

- PROCESSING receipt recovered,
- non-resumable operation marked FAILED,
- user may retry explicitly.

### 10. Transient state-projection failure

Expected:

- state remains correct,
- projection retries,
- latest state eventually appears.

### 11. Burst mutations

Expected:

- state edits coalesce,
- event projections remain FIFO,
- no enabled state target exceeds ordinary freshness indefinitely.

### 12. Deleted Party Stash

Delete message and restart.

Expected:

- recreated automatically.

### 13. Unauthorized privileged action

Expected:

- rejected server-side.

### 14. Closed Loot Drop

Use an old handle after:

- session closure, or
- absolute expiry.

Expected:

- no mutation,
- user redirected to current Party Stash.

### 15. Stale active session

Invoke Start Session while another Session is ACTIVE.

Expected:

- explicit Close & Start / Cancel prompt,
- no silent closure.

---

# 58. Extended resilience suite

Add as corresponding features exist.

Includes:

- confirmation actor mismatch,
- confirmation replay,
- confirmation expiry,
- malformed handles,
- expired handles,
- old-schema handles,
- handle consumption race,
- modal-submit idempotency,
- modal context expiry,
- write-lock delay triggers FAST fallback,
- FAST fallback response race,
- state scheduler fairness across multiple overdue targets,
- event outbox ordering after retry,
- repeated Discord rate limiting,
- pin permission loss,
- edit permission loss,
- stack-upsert race,
- treasury recipient-set change,
- lifecycle races,
- belongings resolution race,
- backup failure,
- migration interruption,
- stale restore,
- Pause Scene receipt privacy,
- Pause Scene event-outbox privacy,
- raw logging disabled in privacy mode.

---

# 59. Observability

Ordinary operation log:

```text id="j226rv"
operation_id
interaction_id
actor_id
execution_class
domain
action
result
duration_ms
ack_latency_ms
```

Where relevant:

```text id="6g1cck"
handle_id
read_set
expected_state
actual_state
outcome_class
```

Error taxonomy:

```text id="j4gg5y"
DOMAIN_ERROR
AUTHORIZATION_ERROR
SEMANTIC_STALENESS
HARD_CONFLICT
INTERACTION_EXPIRED
HANDLE_INVALID
HANDLE_CONSUMED
DEFERRED_OPERATION_FAILED
DISCORD_API_ERROR
DATABASE_ERROR
PROJECTION_ERROR
BACKUP_ERROR
MIGRATION_ERROR
```

Privacy-class operations use redacted logging rules instead.

---

# 60. Health

## Healthy

- DB writable.
- schema valid.
- required Discord configuration valid.
- permanent state surfaces reachable.
- event backlog healthy.
- projection freshness within target.
- backup current.

## Degraded

Examples:

- state projection overdue,
- event queue delayed,
- pin permission missing,
- backup overdue,
- sustained Discord rate limiting.

## Unhealthy

Examples:

- DB unavailable,
- corruption detected,
- unsupported schema,
- migration failure,
- canonical state cannot be safely written.

Healthy infrastructure remains invisible in ordinary play.

---

# 61. Security model

Trusted:

- authenticated Discord interaction identity,
- configured guild,
- configured role ids,
- SQLite canonical state.

Untrusted:

- `custom_id`,
- opaque handle until resolved,
- modal values,
- display names,
- client entity references,
- button visibility,
- stale client state.

Every mutation validates:

```text id="t0hff5"
guild
actor
authorization
handle
handle consumption
entity existence
ownership
read set
domain invariants
```

---

# 62. Migration policy

- Ordered migrations.
- Stored in Git.
- Backup before destructive migration.
- Startup halts on migration failure.
- Newer unsupported schema refuses startup.
- Old handles fail gracefully.
- UI state is reconstructed from current code.

Old Discord messages are disposable.

Canonical data is not.

---

# 63. Open questions

## Table

- 2014 or 2024 5e?
- Encumbrance?
- Ammunition?
- Currency pooling?
- Play-by-post?

## Discord implementation

- Chosen Discord library?
- Components V2 support quality?
- Actual ACK latency on target host?
- Appropriate freshness budgets after testing?
- Handle TTLs after mobile testing?

## Operations

- Host?
- Physical access?
- Backup retention?
- Receipt retention?
- Handle replay/debug retention?
- Log retention?

## Product

- Is Your Pack justified?
- Does Journal pass its gate?
- Does Parking Lot pass its gate?
- Does Downtime pass its gate?
- Does the group want Pause Scene?
- Is one-line `where_ended` sufficient?

---

# 64. Governing principles

1. **The campaign must not depend on Quartermaster.**
2. **SQLite is authoritative; Discord is disposable projection.**
3. **A fact or quantity has one authoritative owner.**
4. **Avrae owns mechanics; Quartermaster owns continuity.**
5. **Discord platform physics are part of the architecture.**
6. **FAST and DEFERRED work have different persistence semantics.**
7. **A committed interaction executes at most once.**
8. **A UI intent replay is prevented by single-use handle consumption, not by wishful concurrency semantics.**
9. **Opaque handles contain references, not authority.**
10. **Guard only the state an operation semantically depends on.**
11. **Changed does not mean conflicting.**
12. **Absolute requests test sufficiency; relative requests preserve observed meaning.**
13. **Permanent surfaces show state; ephemeral surfaces perform tasks.**
14. **Events are lossless; state has bounded freshness.**
15. **Neither state nor events may starve indefinitely.**
16. **Lifecycle describes characters; it never moves possessions.**
17. **Healthy infrastructure stays invisible.**
18. **Privacy guarantees extend through every durable persistence and logging path.**
19. **The DM should not acquire homework because tooling exists.**
20. **Every optional feature begins with a zero-code baseline.**
21. **Observed friction outranks hypothetical requirements.**
22. **Local evidence guides product expansion.**
23. **A detailed design is not a commitment to implement it.**
24. **A feature being easy to build is not evidence that it belongs.**
25. **After v2.6, implementation and real play outrank further speculative specification.**

Quartermaster succeeds when the table remembers the campaign and forgets that the tooling is doing work.