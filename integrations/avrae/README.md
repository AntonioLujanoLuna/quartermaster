# Quartermaster Avrae spike

This directory is a disposable self-hosted Avrae extension spike. It is not
loaded by the current Quartermaster bot and does not change the hosted-Avrae
fallback.

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
