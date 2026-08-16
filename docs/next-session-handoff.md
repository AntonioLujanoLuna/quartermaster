# Next-session handoff

Updated: 2026-08-16 · Schema 12

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

Canonical state is SQLite at schema 12. Discord messages are disposable projections.

**Runtime and durability.** Configuration is validated at startup. FAST interactions run
their mutation and receipt in one transaction; DEFERRED interactions persist `PROCESSING`
before acknowledgement and finalize to `COMMITTED`/`FAILED`. Startup recovery fails any
receipt interrupted mid-flight, marks the matching provider operations `UNKNOWN`, and
releases any projection claim left behind by the previous run. Mutations are addressed by
opaque single-use handles carrying the read set they were rendered against.

**Domain.** Party Stash grant/browse/take/give, Loot Drops (create, claim, manual close,
session close, absolute expiry), sessions, integer-only treasury with adjust, give, and a
split that previews its recipients and their shares and commits on a second press,
character registration and lifecycle, and explicit belongings resolution for non-active
characters. Taking from the stash and claiming a drop both transfer ownership to the
actor's registered active character and both require one. **My Items** is the way back: a
player returns what they hold to the Party Stash or hands it to another active character, and
every path that credits a holder shares one merge rule in `credit_stack`. A give driven by a
button carries a handle, so "Give all" cannot mean a number the giver never saw; a give that
names its own quantity does not need one. Coin moves both ways on the same terms —
**Treasury → My coin…** sends a character's own currency back to the treasury or on to
another active character — and every balance read and write goes through `read_balance` and
`write_balance`. Items can also leave the campaign: **My Items → Use…** spends what a
character carries and **DM Tools → Correct stash…** removes from the Party Stash, both
through `consume_interaction`, which follows possession — you may use up what you hold, only
a DM may remove what the party shares — and writes an `ITEM_CONSUMED` ledger line. Neither
is relative and neither carries a handle: the quantity is always named by the person
removing it.

**Projection.** State targets are scheduled by normalized lateness; events deliver FIFO
per destination and bind to durable per-session threads. Undeliverable events dead-letter
after eight hard failures instead of blocking their destination. A target's claim is leased,
so a claim that outlives its delivery is taken back rather than hiding the target forever.
The runner survives a failed iteration, and every database call it makes — including the
transport's session-thread binding — runs in a worker thread rather than on the event loop.
A state target that keeps failing backs off to a five-minute ceiling and is reported as
stuck rather than as a backlog; it is never dead-lettered, because one success renders the
whole current state.

**Rendering.** Every Discord message is rendered within the platform's 2000-character
limit. List surfaces drop whole lines from the end and say how many they dropped; the send
boundary clamps anything else. One view carries twenty-five controls — two of them Refresh and
the way back — so take and claim listings mint handles against that budget and name the
entries they have no control for. `rendering.py` holds all three bounds.

**Surface.** One guild-scoped command. `/quartermaster` opens an ephemeral home panel and
every action past it is a button, a select menu, or a modal. A panel renders only the controls
its caller may press, and every DM control checks again when it is pressed, because a view
outlives the render that built it. Nothing is identified by hand: characters, players, held
items, and open drops all come from select menus built out of canonical state. Navigation
replaces the panel in place; a result is a separate ephemeral message, so reporting a
committed mutation never depends on a re-render succeeding.

**Operations.** `health`, `maintenance`, `backup`, `restore`, and `requeue-events` on the
CLI; validated timestamped backups on a schedule with retention; **DM Tools → Maintenance**
in Discord. Only `run` and `restore` may create a database file; every other command
refuses a path with no database at it, and `--db` defaults to `QM_DATABASE_PATH`. The
export is the full record every truncated surface points at: items with the character
holding them named, open Loot Drops with what is still unclaimed, the roster, and the
played session's history as sentences rather than payloads. Every interaction logs what
its acknowledgement cost, and one past `internal_hard_deadline_seconds` logs a warning
naming the control. See [the runbook](runbook.md).

**Avrae.** The Combat panel has two halves. Start, End, and Status read and write
Quartermaster's own `combat_encounters` record — session, channel, duration, outcome, and
nothing Avrae owns; Start and End render only for a DM and check again when pressed. The other
six controls render read-only handoff cards pointing at native Avrae commands. Ending combat
reports outstanding Loot Drops and offers the spoils controls. The provider operation boundary is
durable but has no live caller, and its health check cannot fail on this build. The
extension scaffold at `integrations/avrae/quartermaster_cog.py` is parked: Gate 1 was
answered "no, for now" on 2026-08-14, and it has never been loaded in an Avrae deployment.

**Item shape.** Every item is a quantity stack. Unique item instances — specification 30.2
and the unique half of 31 — are deliberately not built and are no longer listed as pending
work in the implementation plan; the reasoning and the cost of reversing it are recorded in
section 2 of that plan.

**Checks.** 258 tests pass under `uv run pytest -q`; `ruff check` is clean. Both run in CI
on every pull request.

## Ninth pass on 2026-08-16

One missing confirmation, the last of the three read-set gaps, covered by tests that fail
against the old behaviour.

- **The split was the one relative operation with nothing in front of it.** `Take all` and
  `Give all` both mean a number the actor was looking at, so both are minted against the
  render and both ask again when it moves. A split is the same shape one level up: it means
  "a share each", and the share is a function of how many characters are alive. The
  machinery for it existed — `split_relative_interaction` compares the roster and treasury
  version the handle was minted against — and nothing minted the handle, so the modal
  committed against whatever the roster happened to be at submit time. A character dying
  between the DM reading the roster and pressing Split changed everyone's share silently,
  and the first anyone knew of it was the receipt.

  Submitting the modal now prepares rather than pays. `prepare_split` mints the handle and
  returns what it would do; the panel names the recipients, the share each of them gets,
  what will not divide, and says plainly that nothing has moved. The button on that preview
  is what commits. If the roster or the treasury changed in between, the split refuses,
  recomputes against the party as it stands, and asks again with the *new* shares — the
  second question has to carry the second answer, or it is just the first question repeated.

  This is a confirmation in front of a DM control, which the surface had avoided until now:
  every other DM action commits on submit. It is here because a split is the one DM action
  whose meaning is set by state the DM cannot see from inside the modal, and because there
  is no way back — putting 81 gp back after a wrong split is a debit per character, one at
  a time, through a panel that only the holder can drive.

  The unguarded entry point is gone rather than kept beside the guarded one:
  `split_treasury_interaction` had no callers left once the modal moved, and leaving a
  second way to split that skips the roster check is how the check stops being true.
  `_describe_split` is now the one piece of arithmetic the preview and the commit share, so
  a preview cannot promise a share the commit would not pay.

## Eighth pass on 2026-08-16

Three defects, each silent, each ending with someone believing something the
database does not say. All covered by tests that fail against the old behaviour.

- **The export printed the ledger as JSON, and "recent" meant the last ten rows
  in the campaign.** Every truncated surface tells the reader the export holds
  the full record, and the session log has read events as sentences since the
  fifth pass — but the history section printed the stored payload, so the
  document a DM downloads during an outage was the one place internal UUIDs and
  Discord user IDs actually got read out. Ten rows is also a few minutes of a
  busy evening, and says nothing about which evening. `narrative.py` is now the
  one renderer table both surfaces go through, for the same reason
  `credit_stack` is one merge rule, and the window is the session the table is
  playing — the active one, or the most recent closed one for as long as the
  next has not started.

  `render_event` is total. Renderers quote payload keys, payloads are written by
  whatever build appended them, and the export renders every ledger row a
  campaign has accumulated, so a key that moved between versions has to degrade
  to the raw payload rather than raise. That matters more on the delivery side
  than in the export: a renderer that raised would fail its event identically
  every attempt, burning eight retries and a dead letter while the
  per-destination FIFO gate held every later event in that thread behind it.
  The JSON fallback existed for event types nobody had written a line for; it
  did not cover a known type whose payload had moved.

- **The CLI would invent a database and then report on it.** Opening SQLite
  creates and migrates whatever path it is given. The runbook passes
  `--db $env:QM_DATABASE_PATH`, and this document already records that a new
  PowerShell process may not have imported that value yet — so the ordinary
  failure was `health` answering `DEGRADED` about an empty database it had just
  created in the working directory, `export` printing it, and `backup` writing a
  *valid* empty snapshot into the same directory the real ones live in: one
  genuine backup pruned to make room, and a restore candidate that passes
  validation and holds nothing. `run` and `restore` may create a file because
  that is what they are for; every other command now refuses a path with no
  database at it and names the path. `--db` defaults to `QM_DATABASE_PATH`
  rather than to a name in the working directory, `run` refuses a `--db` that
  disagrees with the configured value instead of silently ignoring it, and a
  command-line backup lands in `QM_BACKUP_DIRECTORY` with the configured
  retention — the CLI and the scheduler rotate one set of files, and health
  reports on whichever wrote last.

- **The one number the release gate asks for was computed and thrown away.**
  `execute_fast` recorded an acknowledgement latency only on the path that had
  already deferred, never on the common path — which is the path that can still
  lose Discord's three-second window and strand a committed mutation with no
  reply — and nothing read it either way, while
  `internal_hard_deadline_seconds` was validated at startup by a process that
  had no consumer for it. Every interaction now logs what its acknowledgement
  cost, and one past the hard deadline logs a warning naming the control that
  was slow. The label comes from the custom ID this package authored, so a
  latency line quotes nothing a person typed or chose, and no line names the
  actor: latency is a property of the host, not of who pressed the button.

  This does not reinstate the metric histograms removed on 2026-08-14. That
  decision stands — at one table's volume percentiles cannot carry meaning — and
  `local_metric_buckets` still has no reader and no writer. A log line per
  interaction is what the observability section of the specification asks for
  and what makes "measured acknowledgement latency inside the configured budget"
  answerable from a real evening rather than an estimate.

## Seventh pass on 2026-08-16

One missing capability, the third with the same shape as the two before it, covered by tests
that fail against the old behaviour.

- **Items could enter the campaign and never leave.** Every item path was a mint or a
  transfer: a grant mints into the Party Stash, a Loot Drop mints into the drop and returns
  what nobody wanted, and take, claim, give and belongings resolution move what already
  exists between owners. A stack row was only ever deleted because its quantity reached zero
  on the way somewhere else, so the campaign's item total was monotonic.

  Two ordinary things had nowhere to go. A potion drunk, a rope burned, twenty arrows fired:
  the stash kept saying the party had them, and it drifted fastest for exactly the items that
  get used most — on a permanent public surface that is also bounded, so dead entries push
  live ones off the end and the table never gets the room back. And a mistyped grant, fifty
  potions where the DM meant five, was permanent: granting again cannot subtract, and taking
  only moves the mistake onto a character. The treasury has had signed **Adjust…** since the
  beginning. Coin had this exit all along and items did not, which is the same asymmetry the
  fifth and sixth passes closed for movement, one level down.

  `consume_interaction` is the debit. **My Items → an item → Use…** spends what the caller's
  active character is carrying; **DM Tools → Correct stash…** removes a quantity from the
  Party Stash. Authorization follows possession, which is the rule the give paths already
  hold: you may use up what you carry, and only a DM may remove what the party shares. It is
  enforced in the transaction rather than only at the control, because a panel outlives the
  render that built it and this is the one operation with no way back.

  Nothing on this path is relative and nothing carries a handle. Every other quantity control
  on the give panel is minted against a render because a give can be undone by giving it
  back, and a stale one is worth a confirmation prompt; a removal cannot be undone by
  anything, so the number is typed by the person removing it rather than fixed by a render
  that has since gone stale. `ITEM_CONSUMED` is what pays for allowing it at all — the
  session log says who removed what, how many, and why, reading as play at the character end
  and as a correction at the party end.

  Deliberately not done: a DM cannot remove from a *character's* holdings. An active
  character's items are the player's to use up, and a non-active character's are what
  belongings resolution is for. Adding a third path would be a second way to reach into
  someone's pack.

## Sixth pass on 2026-08-15

One defect, with the same shape as the possession gap the fifth pass closed and covered by
tests that fail against the old behaviour.

- **Currency only moved towards a living character, and a player could not see their own.**
  A split credits every active character, **Give to…** credits one, and the only debit in
  the product — belongings resolution — refuses an active character on purpose. So an
  active character's balance could only ever rise. What made that worse than the equivalent
  item mistake is the repair: a DM reaching for **Adjust…** to put back 81 gp they meant to
  keep does not return it, because adjust only touches the party row. The character keeps
  the coin and the campaign ends the evening 81 gp richer than it started — the same
  inflation `/grant` would have caused for items, which is exactly why **My Items** exists.
  Compounding it, a character's balance was rendered in one place only, the DM's export, so
  the money a split handed a player was invisible to the person it belonged to.

  **Treasury → My coin…** is the coin counterpart of My Items: the giver's active character
  sends coin back to the treasury or hands it to another active character, refusing a
  non-active recipient exactly as the item path does. The Treasury panel names what the
  caller is carrying and the home panel carries a `Your coin` line, both said only when
  there is coin to say it about. There are no handles on this path and there should not be:
  a held stack has a quantity on screen that another character can move underneath the
  giver, so `Give all` has to be minted against what was rendered, but coin is typed into a
  modal at the moment it is given and has nothing on screen to go stale.

  `read_balance` and `write_balance` in `currency.py` are now the one pair every balance
  read and write shares — the split, both gives, and belongings resolution — for the same
  reason `credit_stack` is one merge rule: four hand-written copies of the same upsert are
  four chances to disagree about what an absent row means.

## Surface pass on 2026-08-15

Quartermaster had eighteen commands. Using it meant knowing which one existed, what it was
called, and what order its arguments went in — at the table, mid-session, with four other
people waiting. Three of them wanted a thirty-six character UUID pasted out of another
command's output. That is now one command and a panel.

- **`/quartermaster` is the whole surface.** It opens a home panel that states session, Party
  Stash, open loot, treasury, and who the caller is playing, and every action past it is
  something already on screen to press. `discord_panels` holds the panels and the navigation
  between them; `discord_views` holds the controls that act and the modals they open, so a
  panel can hand a control the way back to itself without the control knowing what a panel is.

- **Nothing is identified by hand.** A character comes from a select menu of the roster, a
  player from Discord's own user picker, a held item from what you are actually carrying, a
  Loot Drop from what is actually open — each built out of canonical state at render time.
  The previous pass recorded typed IDs as "the obvious next ergonomic step" and left it for
  a session of real use; collapsing the command surface made it the same work as the rest.

- **A panel renders only what its caller may press, and every DM control checks again.** A
  player never sees DM Tools, which is what makes the refusals rare rather than routine. The
  check stays at the control because a view outlives the render that built it: a panel left
  open across a role change would otherwise be a mutation with no gate in front of it.

- **A give driven by a button carries a read set.** This was recorded as an observation last
  pass — "if a component path for giving ever exists, it needs a handle" — and the panel is
  that path. `Give all` means the quantity the giver was looking at, so another player handing
  them more of the same item between rendering and pressing produces the same confirmation
  prompt `Take all` produces, not a silent transfer of a different number. `create_give_handles`
  mints it; `_move_from_character` is the one body both the typed and the pressed give share.

Two things were deliberately not done. Navigation replaces the panel in place, but a *result*
is still a separate ephemeral message: re-rendering a list after a take would mean the report
of a committed mutation depends on a second read succeeding, and "nothing was changed unless
you were told otherwise" is the one sentence that must never be wrong. And the take and claim
panels now spend two of their twenty-five controls on Refresh and the way back, which costs
one listed stack, because a panel of consumed handles with no way to renew them is a dead end.

## Fifth pass on 2026-08-15

One missing capability and three defects, each covered by a test that fails against the
old behaviour.

- **Possession only moved one way.** A take or a claim transfers ownership to the actor's
  active character, and nothing moved it back: `/character-resolve` refuses active
  characters by design, and `/grant` mints a new item, so using it to undo a mistaken
  `Take all` inflates the campaign's inventory instead of correcting it. A misread button
  was therefore permanent, for the player and for the DM — which is also what quietly made
  the take-all confirmation prompt the only thing standing between the table and an
  unfixable stash. `/item-give` closes the door: the giver's active character hands a
  quantity back to the Party Stash or on to another active character, refusing a
  non-active recipient per specification 32.1. `credit_stack` is now the single merge rule
  every crediting path shares, so the Loot Drop return, the claim, the take, and the give
  cannot disagree about when two stacks are the same stack.

- **Four event types were being read out at the table as raw JSON.** The outbox renderer
  falls back to the payload for any event type it does not know, which is the right
  behaviour — an unrenderable event must not block its destination — but it also means
  nothing fails when a new event ships without a line. `CHARACTER_CREATED`,
  `CHARACTER_LIFECYCLE_CHANGED`, `COMBAT_OPENED`, and `COMBAT_CLOSED` had all arrived that
  way, so the session log printed internal UUIDs and a player's Discord user ID. The
  renderers are now a table, and a test reads the event types the package actually appends
  and fails if any of them is missing from it.

- **A known event type could still be refused by Discord.** Only the JSON fallback was
  clamped to 2000 characters. Every renderer quotes canonical state, and `/grant` puts no
  bound on an item name, so a long enough name produced an event Discord rejects
  identically every attempt: eight retries, a dead letter, and the per-destination FIFO
  gate holding every later event in that thread behind it. `_content_for_event` clamps
  whatever it returns.

- **The export lost the combat record at `/session-end`.** Encounters were read against
  the active session only, and ending a session closes its open combat, so the section
  emptied at exactly the moment the DM writes up what happened. It now reads the session
  the table is playing — the active one, or the most recent closed one — and names it.

Also corrected:

- Component callbacks now answer when something unforeseen breaks. A slash command routes
  an unexpected exception to `bot.tree.error`; a button had no equivalent, so anything a
  callback did not name reached discord.py's default handler and the player was left with
  Discord's bare "This interaction failed", unable to tell whether their take committed.
  Every view inherits `QuartermasterView.on_error`, and the grant modal has the same
  handler. This was the "shape decision" the previous pass recorded and did not take.

## Fourth correction pass on 2026-08-15

Three defects, each fixed and covered by a test that fails against the old behaviour.

- **A pin the bot was not allowed to make cost the Party Stash its convergence.** Pinning
  needs Manage Messages, and a channel holds fifty pins. The pin failure was raised out of
  `upsert_state`, so the delivery failed *after* the message was posted and the message id
  was never recorded — which meant the next attempt sent another Party Stash rather than
  editing the one already there. One duplicate per retry, forever, and a projection that
  never converged: exactly the shape the previous pass fixed for stuck state targets,
  reached through a permission nobody thinks to grant. Delivery now succeeds and the
  failure is logged with what to repair, on every attempt, so it is not silent and the
  surface re-pins itself the moment the permission comes back. The superseded test asserted
  the raise; it now asserts one message and two warnings.

- **Browse listed stacks it had no button for.** A stack above one offers Take 1 and Take
  all, so twenty-five listed stacks want fifty controls and one view holds twenty-five.
  Everything past the twelfth was rendered with nothing to press and nothing said, and the
  Loot Drop listing had already been fixed for exactly this. Handles are now minted against
  the control budget and only for a leading run — so the controls line up with the top of
  the list rather than skipping a stack in the middle — and the message names the entries
  that have none. `rendering.py` holds the component limit, because the code that mints
  controls and the code that renders them have to agree.

- **The export was not the record every surface says it is.** Each truncated list tells the
  reader the export holds the full record. It did not: an open Loot Drop's items live
  nowhere but `loot_drop_items` until it closes, so loot the party could still claim left no
  trace in the document; ownership moves to a character on every take and every claim, and
  the holder was rendered as a bare UUID; the roster appeared only for characters that
  happened to hold currency. The export now names holders, lists open drops with what is
  unclaimed, and carries every registered character.

## Third correction pass on 2026-08-14

A review of what Discord will actually accept found two defects. Both end with the bot
online and answering commands while a surface stops updating, and both are fixed and
covered by tests that fail against the old behaviour.

- **Nothing bounded a message to Discord's 2000-character limit.** Discord rejects
  over-long content outright and rejects it identically every time, so this was not a
  rendering blemish that arrives gradually — it was a cliff. The Party Stash is permanent
  and only grows: at roughly a hundred stacks the pinned projection would start failing on
  every delivery and never render again. `/stash`, `/characters`, `/loot`, and the raw-JSON
  fallback for events with no renderer had the same cliff, where the player sees only that
  Quartermaster did not respond. `rendering.py` now holds the bound: list surfaces drop
  whole lines from the end and name the count they dropped, and `_send_error` and
  `_send_execution` clamp whatever reaches them. Open Loot Drops render before the stash
  body, so the entries that expire are the ones kept when the tail has to go.

- **A permanently failing state projection retried once a second forever.** The event
  outbox already backed off and dead-lettered; the state scheduler passed a fixed one-second
  delay for every failure, so a deleted channel or a revoked permission meant one Discord
  call per second for the life of the process — and `health` reported the same `DEGRADED` it
  reports for a surface that is one second behind. Hard failures now back off exponentially
  to the same five-minute ceiling the outbox uses, rate limits wait exactly as long as
  Discord asks and do not count, and eight consecutive failures make `state_projections`
  `FAILED` with the target and error named. There is deliberately no dead letter: a state
  target blocks nothing and one successful delivery renders current state, so the count
  clears itself.

Also corrected:

- The Loot Drop listing rendered items it had no claim control for. One component view
  carries a bounded number of buttons, and beyond that an item was listed with nothing to
  press and nothing said. Browse had the same gap: its snapshot is capped at 25 stacks and
  read as the whole stash. Both now say what they are showing and out of how much.
- Two migration tests derived "the previous version" from `SCHEMA_VERSION - 1`, so adding
  migration 11 pointed them at migration 10 instead of the one they were written for. They
  name the version they mean now, through one `_schema_version` helper.

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
  The Split control calls `split_treasury_interaction` directly and skips that check, so a
  character who died between the DM reading the roster and pressing Split silently changes
  everyone's share. This is the same shape as the take-all gap, and as the give gap the
  surface pass closed, but the fix is a product decision — it puts a confirmation step in
  front of a DM control — so it is left for the table to choose.
- **`local_metric_buckets` (migration 8) has no reader and no writer.** The
  histograms it was built for were removed deliberately on 2026-08-14: at one
  table's interaction volume percentiles cannot carry meaning. The table stays
  because inert storage costs nothing and fails nothing, unlike a validated
  configuration knob with no consumer — which is why
  `internal_hard_deadline_seconds` and `ack_latency_ms` were wired up instead of
  left sitting there. Latency now comes from the log, one line per interaction.
## Not yet verified live

Nothing in any correction pass has been exercised against the guild, and the surface pass
replaced every way in. The command list changed, so **the first thing to confirm is that the
guild's old commands are gone and `/quartermaster` is there** — a stale tree is the one
failure that makes everything below untestable.

Panel surface:

1. `/quartermaster` as the server owner and as a player, and confirm the player's panel has no
   **DM Tools** and the DM's does.
2. Press through every panel and confirm each one replaces the message in place rather than
   leaving a column of ephemeral replies, and that the way back always works.
3. **Party Stash → Take something…**, and confirm both `Take 1` and `Take all` appear, that
   Refresh renews the controls after a take, and that a take reports what it moved.
4. `Take all` on a stack the DM grows in between, and confirm the confirmation prompt reads
   acceptably at the table and takes the current quantity.
5. **My Items → an item → Give all** back to the party, and the same to another character
   through the destination select. Confirm the pinned Party Stash reflects the return and the
   session log line reads acceptably.
6. **My Items → Give some…** with a quantity larger than the holding, and confirm the refusal
   names what is actually held.
7. Have a second player hand the first more of the same item while the first has the give
   panel open, then press `Give all`, and confirm the confirmation prompt appears and reads
   acceptably. This is the one path with no live evidence at all behind it.
7a. **My Items → an item → Use…** for part of a stack, and confirm the reply and the session
    log both read as a player using something rather than as a transfer, and that nothing
    appeared in the Party Stash. Then use the rest and confirm the stack disappears from
    My Items.
7b. **DM Tools → Correct stash…** on a deliberately over-granted stack, and confirm the panel
    lists the stash, that the removal reports what remains, and that the pinned Party Stash
    converges to the corrected number. This is the repair for the most likely DM mistake at
    the table, so it is worth making the mistake once on purpose.
8. **Characters → Register…**, pick a player from the user select, and confirm the reply names
   them by mention. Repeat for a player who already has an active character and confirm the
   refusal names the existing one.
9. **Characters → Lifecycle…** and **Resolve estate…**, and confirm the staged line reads
   correctly before Apply and that Apply reports both ends of the move.
10. **DM Tools → Loot Drops**, open a drop, claim from it as a player, then close it from the
    select and confirm the remainder returns to the stash.
11. **Treasury → Give to…**, and confirm the recipient select holds only active characters.
11a. **Treasury** as a player who has been given coin, and confirm the panel names what they
    are carrying, that home carries the `Your coin` line, and that both are absent for a
    player carrying nothing.
11b. **Treasury → My coin… → Give coin…** back to the treasury, and confirm the treasury
    rises by exactly what the player's balance fell by. Then the same to another character
    through the destination select. This is the path that makes a mistyped **Give to…**
    repairable, so it is worth running the mistake deliberately once.
12. **Combat**, and confirm the handoff cards are open to players while Start and End are not
    even rendered for them.

Runtime, unchanged by this pass and still unverified:

13. Restart across the schema-10 migration and confirm health, the unique index, and the
    normalized stack names on the live database.
14. `health` after a deliberately broken session thread, and `requeue-events` once it is
    recreated.
15. Kill the bot mid-delivery (stop it while a grant is still propagating), restart, and
    confirm the startup log reports a released projection claim and the Party Stash converges
    without a manual database edit.
16. Run `maintenance` from the CLI while the bot is up and a grant is in flight, and confirm
    the runner logs at most a failed iteration and keeps delivering.
17. Grant enough distinct items to push the pinned Party Stash past 2000 characters, and
    confirm it keeps updating and that the truncation line reads acceptably at the table.
18. Revoke the bot's Send Messages permission on `#party-inventory`, watch the retry interval
    grow in the log, and confirm `health` reports `state_projections: FAILED` with the target
    named. Restore the permission and confirm it clears without operator action.
19. Revoke Manage Messages on `#party-inventory` and grant an item. Confirm the pinned
    projection keeps updating in place — one message, not a new one per delivery — and that
    the log names the missing permission. Restore it and confirm the pin returns.
20. Fill the take controls with two-item stacks, and confirm the buttons line up with the top
    of the list and the closing line about entries with no control reads acceptably.
21. Export mid-session with an open Loot Drop and an item a player has taken, and confirm the
    drop, the holder's name, and the roster all read correctly.
21a. Export after a full evening and read the history section aloud. It should be the story of
    that session in sentences, headed by the session number, with nothing before the session
    in it and no JSON anywhere. This is the section a DM reads during an outage, so whether it
    is the right length is a judgement only real play can make.
21b. From a fresh PowerShell that has *not* imported the user-level variables, run
    `uv run python -m quartermaster health`. It must refuse and name the path it looked at,
    rather than reporting on a database it created in the working directory. Then run
    `backup` with the variables imported and confirm the snapshot lands beside the scheduled
    ones rather than in a second `backups` directory.
21c. Watch the log through an ordinary evening for the acknowledgement lines, and see whether
    anything crosses the 2.5 s internal hard deadline on the live host. This is the
    measurement the release gate wants and the only way to find out whether the current
    budgets are the right ones.
22. Register a character and change a lifecycle with the session log in view, and confirm
    those lines read as sentences rather than JSON.

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
2. Play one session on the panel and watch where the DM hesitates. The surface pass replaced
   a command list with a shape, and a shape is only right if the thing you want next is
   already on screen — how many presses a real grant, a real take, and a real end-of-session
   actually cost is worth more than any further feature.
3. Play a session with the combat record and see whether the closeout gets used. If the DM
   never presses **Record spoils** after a fight, the control is in the wrong place. That
   observation is worth more than any further combat feature.
4. Choose evidence-based latency and freshness budgets from observed play if the current
   estimates prove wrong.

The Avrae extension spike is no longer a priority: Gate 1 was answered "no, for now", so
self-hosting, the Cog, provider gateway implementations, and combat reference projections
are all parked. See [the integration plan](avrae-integration-plan.md) for the reasoning and
for the one hosted-path question still worth answering.

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
