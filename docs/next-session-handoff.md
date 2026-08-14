# Next-session handoff

Updated: 2026-08-14 · Schema 10

## How to use this document

This describes what is true now and what is not yet proven. It is not a changelog —
`git log` already is one, and keeping a second narrative copy here is how the previous
version of this file drifted into asserting correctness that a review then falsified.

Two rules keep it useful:

- **State current behaviour, not past activity.** If a line would still read the same
  after being rewritten from scratch against the code, keep it. If it only records that
  something once happened, delete it.
- **Anything under "Not yet verified live" stays there until someone actually runs it
  against the guild.** Passing tests move a line out of "unimplemented", not out of
  "unverified".

## Current state

Canonical state is SQLite at schema 10. Discord messages are disposable projections.

**Runtime and durability.** Configuration is validated at startup. FAST interactions run
their mutation and receipt in one transaction; DEFERRED interactions persist `PROCESSING`
before acknowledgement and finalize to `COMMITTED`/`FAILED`. Startup recovery fails any
receipt interrupted mid-flight, marks the matching provider operations `UNKNOWN`, and
releases any projection claim left behind by the previous run. Mutations are addressed by
opaque single-use handles carrying the read set they were rendered against.

**Domain.** Party Stash grant/browse/take, Loot Drops (create, claim, manual close,
session close, absolute expiry), sessions, integer-only treasury with adjust/split/give,
character registration and lifecycle, and explicit belongings resolution for non-active
characters. Taking from the stash and claiming a drop both transfer ownership to the
actor's registered active character and both require one.

**Projection.** State targets are scheduled by normalized lateness; events deliver FIFO
per destination and bind to durable per-session threads. Undeliverable events dead-letter
after eight hard failures instead of blocking their destination. A target's claim is leased,
so a claim that outlives its delivery is taken back rather than hiding the target forever.
The runner survives a failed iteration, and every database call it makes — including the
transport's session-thread binding — runs in a worker thread rather than on the event loop.

**Operations.** `health`, `maintenance`, `backup`, `restore`, and `requeue-events` on the
CLI; validated timestamped backups on a schedule with retention; `/quartermaster` as the
DM launcher. See [the runbook](runbook.md).

**Avrae.** Guild-scoped `/combat` renders read-only handoff cards pointing at native Avrae
commands. The provider operation boundary is durable but has no live gateway behind it. No
mechanics are mirrored. The extension scaffold at `integrations/avrae/quartermaster_cog.py`
has never been loaded in an Avrae deployment.

**Checks.** 132 tests pass under `uv run pytest -q`; `ruff check` is clean. Both run in CI
on every pull request.

## Second correction pass on 2026-08-14

A review of the delivery runtime found three defects. Each is silent, each ends with the
bot online and answering commands while Discord quietly stops reflecting canonical state,
and each is fixed and covered by a test that fails against the old behaviour.

- **A projection claim survived the process that took it.** `_claim_next_target` sets
  `in_flight`, only the same process clears it, and the scheduler skips claimed targets.
  A crash during the Discord round-trip therefore retired that surface permanently:
  nothing on the startup path cleared the flag, so restarting did not help either, and
  health reported an ordinary `DEGRADED` backlog that never drained. Startup now releases
  every claim, and a claim is leased for five minutes so the same stall cannot outlive one
  delivery while the process stays up — which is what happens if recording the outcome is
  itself what failed.

- **One transient error ended all delivery for the life of the process.** Maintenance,
  backup, and the surface check were each guarded inside the runner loop; the two delivery
  calls were not. A `sqlite3.OperationalError` from the claim step — reachable whenever an
  operator runs a CLI command against the live database, which the runbook tells them to do
  — ended the task. Nothing was logged until shutdown re-raised it. Each step is guarded and
  logged now, so a bad iteration costs one second.

- **The transport did its database work on the event loop.** `_ensure_session_thread` and
  `_fetch_channel` read and wrote the store directly from the loop that runs discord.py's
  heartbeat, while the store serializes every caller onto one connection. An ordinary
  interaction's write could stall the gateway; measured, a held 0.5s write cost the loop
  roughly 80% of its ticks. Those calls now go through `asyncio.to_thread`, as every other
  database call in the runner already did.

## Correction pass on 2026-08-14

A review found four defects, three of them silent. All are fixed and covered by tests that
fail against the old behaviour.

- **State projections dropped work committed during delivery.** `_record_success` re-read
  `desired_revision` *after* the Discord round-trip, so any mutation that landed while the
  call was open was credited to the payload already in flight and `dirty_since` was cleared
  with it. That change was then never rendered, and health reported a clean projection.
  Success now retires only the revision captured when the target was claimed.

- **One Discord user could hold several active characters.** Nothing enforced the rule, so
  a player with two live characters drew two shares of every treasury split, and
  `active_claimant` picked between them with an unordered `fetchone()`. Schema 10 adds a
  unique partial index, both the create and the reactivate paths check it with a message
  naming the character in the way, and claimant resolution is ordered.

- **One undeliverable event blocked its destination forever.** Nothing ever set
  `event_outbox.status = 'FAILED'` and `attempt_count` was written but never read, so a
  poisoned event retried once a second indefinitely while the per-destination FIFO gate
  held every later event behind it. Hard failures now back off exponentially and
  dead-letter after eight; rate limits are retried at Discord's delay and do not count.
  `requeue-events` is the operator path back.

- **The acknowledgement fell back to deferral in the wrong direction.** `execute_fast`
  skipped the deferral whenever any write transaction was open — exactly the slow case, and
  the one most likely to overrun Discord's three-second window and strand a committed
  mutation with no reply. The write-state input is gone; the decision now runs through
  `ResponseController.should_fallback_to_deferred`, which was previously dead code with the
  live logic duplicated inline beside it.

Also corrected:

- Migration 2 backfilled `normalized_name` with `lower(trim(...))`, which is ASCII-only and
  keeps internal whitespace, so stacks written under it could disagree with the runtime rule
  and split in two. Migration 10 recomputes the rule in Python and merges the collisions.
  Normalization now lives in `naming.py` so migrations and services share one definition.
- Migrations apply statement-by-statement inside one transaction rather than through
  `executescript`, so a Python data fix can share the schema change's atomicity.
- `expire_due_drops` checks with a read before taking the write lock. The projection runner
  calls it every second, so an idle bot was contending with live interactions for nothing.
- Take-all was unreachable. The relative handle that carries the on-screen quantity had no
  producer in the Discord layer, so the staleness confirmation and `TakeConfirmationView` —
  both implemented and tested — could never fire. Browse now offers "Take all" for stacks
  above one, and the confirmation path is live. **Reversible judgement call:** the
  alternative was deleting the domain capability instead of wiring it up.
- The restore test simulated an old backup by hand-undoing the newest migration's artifacts,
  which needed an edit per migration and silently stopped simulating anything once it fell
  behind. It now builds a genuine previous-version database.

## Observed but not acted on

Neither of these is a defect; both are capability that exists without a caller, recorded so
the next session decides deliberately rather than rediscovering them.

- **The relative treasury split has no producer in the Discord layer.**
  `create_relative_split_handle` and `split_relative_interaction` implement a split that
  notices the active recipient set changed since the DM looked and asks for confirmation.
  `/treasury-split` calls `split_treasury_interaction` directly and skips that check, so a
  character who died between `/characters` and the split silently changes everyone's share.
  This is the same shape as the take-all gap fixed in the previous pass, but the fix is a
  product decision — it puts a confirmation step in front of a DM command — so it is left
  for the table to choose.
- **`local_metric_buckets` (migration 8) has no reader and no writer**, and
  `internal_hard_deadline_seconds` and `ack_latency_ms` are likewise computed or validated
  and never consumed. Latency budgets stay estimates until something records them.

## Not yet verified live

Nothing in either correction pass has been exercised against the guild. Before the next
session:

1. `/stash` → `Browse`, and confirm both `Take 1` and the new `Take all` appear.
2. `Take all` on a stack the DM grows in between, and confirm the confirmation prompt reads
   acceptably at the table and takes the current quantity.
3. `/character-add` for a player who already has an active character, and confirm the
   refusal names the existing one.
4. Restart across the schema-10 migration and confirm health, the unique index, and the
   normalized stack names on the live database.
5. `health` after a deliberately broken session thread, and `requeue-events` once it is
   recreated.
6. Kill the bot mid-delivery (stop it while a grant is still propagating), restart, and
   confirm the startup log reports a released projection claim and the Party Stash converges
   without a manual database edit.
7. Run `maintenance` from the CLI while the bot is up and a grant is in flight, and confirm
   the runner logs at most a failed iteration and keeps delivering.

## Live Discord setup

- Server `Quartermaster Test`, guild `1536121899388506222`
- Party inventory `#party-inventory` (`1536122527892635809`)
- Session log `#session-log` (`1536122560322863224`)
- Application/bot `1536120052871602256`
- `QM_DM_ROLE_IDS` is unset; the server owner is accepted as DM administrator.
- Guild, channel, and database configuration are user-level environment variables. The
  token lives only in `QM_DISCORD_TOKEN` and is not stored in Git, this document, or chat.
- The bot runs under the managed Windows supervisor wrapper.
- Backups are local-only by policy: leave `QM_BACKUP_OFF_DEVICE_DIRECTORY` unset.

## Deliberate test state

Party Stash holds `Smoke-Test Potion x2` and `Live Loot Token x2` from earlier acceptance
runs. These are fixtures retained for cleanup and audit, not campaign data.

## Next priorities

1. Work through "Not yet verified live" above.
2. The Avrae extension spike, which is the next real gate: decide whether self-hosting or
   forking Avrae is acceptable, load the cog in a disposable guild, and prove one
   authenticated harmless native state change through the provider boundary. Do not add
   launcher combat controls or combat reference projections before that.
3. Choose evidence-based latency and freshness budgets from observed play if the current
   estimates prove wrong.

Your Pack, Journal, Parking Lot, Downtime, faction clocks, rich continuity, and Undo remain
evidence-gated. Do not start them.

## Restart command

The IDs and database path are persisted, but a new PowerShell process may need to import the
user-level values before starting:

```powershell
$env:QM_GUILD_ID = [Environment]::GetEnvironmentVariable('QM_GUILD_ID', 'User')
$env:QM_PARTY_INVENTORY_CHANNEL_ID = [Environment]::GetEnvironmentVariable('QM_PARTY_INVENTORY_CHANNEL_ID', 'User')
$env:QM_SESSION_LOG_CHANNEL_ID = [Environment]::GetEnvironmentVariable('QM_SESSION_LOG_CHANNEL_ID', 'User')
$env:QM_DATABASE_PATH = [Environment]::GetEnvironmentVariable('QM_DATABASE_PATH', 'User')
$env:QM_DISCORD_TOKEN = [Environment]::GetEnvironmentVariable('QM_DISCORD_TOKEN', 'User')
uv run python -m quartermaster --db .\quartermaster.sqlite run
```
