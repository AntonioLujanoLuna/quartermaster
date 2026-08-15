# Quartermaster Avrae spike (parked)

**Status: parked, not queued.** Gate 1 of the
[integration plan](../../docs/avrae-integration-plan.md) was answered "no, for now" on
2026-08-14: the table is not taking on a self-hosted Avrae fork, so nothing here is
scheduled to run. The committed path is the hosted-Avrae workflow in the main bot.

This code has never been executed. It is kept as a record of the shape the integration would
take, and it should be re-verified from scratch before anyone trusts it, because it assumes
things about Avrae's internals that nobody has checked against a running process:

- that `CombatNotFound` is importable from `cogs5e.initiative` — upstream it lives in
  `cogs5e.models.errors`, and whether the package re-exports it is unconfirmed;
- that `Combat.from_ctx` accepts a slash-command interaction with the attributes this Cog
  reads off it;
- that a second OS process may open the live Quartermaster SQLite database. WAL and the
  2.5s busy timeout make it survivable in principle, but it turns a single-writer design
  into a two-writer one and complicates the backup and restore story. That is a decision to
  make deliberately, not to inherit from this file.

Avrae's combat internals are not a stable API. Every upstream pull is a chance for this to
break, which is the substance of the Gate 1 answer.

It is not loaded by the current Quartermaster bot and does not affect the hosted-Avrae
workflow.

The Cog adds `/qm-combat-probe`. When invoked in a guild with an active
Quartermaster session, it:

1. uses the real Avrae interaction actor, guild, and channel;
2. locates the native Avrae combat with `Combat.from_ctx(inter)`;
3. records a Quartermaster provider receipt and correlation ID; and
4. reports whether a native combat exists, without copying HP, initiative,
   conditions, resources, or combatants into Quartermaster.

The extension assumes the self-hosted Avrae process can import the core
Quartermaster package without installing its Discord adapter dependencies:

```powershell
uv pip install --python <avrae-python> --no-deps -e C:\path\to\quartermaster
```

Add `QuartermasterAvraeCog` to the self-hosted Avrae extension loading path,
configure `QM_DATABASE_PATH` and `QM_GUILD_ID` so the Cog reaches the canonical
SQLite database and is pinned to one guild, and run this only in that disposable
guild first. The next spike must prove one native state-changing operation
through an Avrae-owned context before we add attack, cast, save, or turn-advance
dispatch.

None of that is scheduled. Reopening it means reopening Gate 1 first.
