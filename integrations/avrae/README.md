# Quartermaster Avrae adapter (opt-in)

This directory contains the first, read-only integration slice for a
self-hosted or forked Avrae deployment. It is not loaded by the hosted
Quartermaster bot and it does not open, read, or write Quartermaster's SQLite
database.

The boundary is:

1. Quartermaster's authenticated Activity request is signed with
   `X-Quartermaster-Timestamp`, `X-Quartermaster-Nonce`, and
   `X-Quartermaster-Signature`.
2. The Avrae-side handler verifies the HMAC, clock skew, nonce replay, protocol,
   operation (`status` only), and configured guild.
3. A provider supplied by the Avrae Cog reads native status inside Avrae and
   returns a provider result. Quartermaster displays it as provider state and
   never stores HP, initiative, conditions, resources, or combatants.

`quartermaster_adapter.py` is dependency-light and owns the signed request
contract. Loading `quartermaster_cog.py` starts an `aiohttp` listener at
`/quartermaster/v1/status` and reads the native combat model through
`Combat.from_id`. The synthetic context is used only for model deserialization;
it is never passed to a mutating command handler. The signed actor must still
be a member of the configured guild, and the channel must belong to that guild.

## Quartermaster configuration

Configure the full POST endpoint and the same secret in Quartermaster:

```text
QM_AVRAE_ADAPTER_URL=https://avrae-host.example/quartermaster/v1/status
QM_AVRAE_ADAPTER_SECRET=<long-random-shared-secret>
QM_AVRAE_ADAPTER_TIMEOUT_SECONDS=2.5

# Avrae-side listener defaults; bind loopback when using a local reverse proxy.
QM_AVRAE_ADAPTER_HOST=127.0.0.1
QM_AVRAE_ADAPTER_PORT=8787
```

When these values are absent, the existing hosted-Avrae handoff continues to
work and the Activity reports the adapter as not configured. The initial
status route is `GET /api/combat/avrae`; it only calls Avrae while a
Quartermaster combat is open.

For a remote bind, set `QM_AVRAE_ADAPTER_ALLOW_REMOTE=1` only when a TLS
reverse proxy terminates HTTPS in front of the listener. The listener itself
is plain HTTP; HMAC authenticates the request but does not encrypt it.

## What is not implemented yet

- the TLS/reverse-proxy deployment and live self-hosted acceptance;
- authorization semantics beyond guild membership for future mutations;
- any state-changing provider operation;
- durable provider receipts for status reads or automatic retries.

The disposable local spike now proves one real native status read through the
listener with Avrae's MongoDB and Redis dependencies. A real Discord login,
guild, TLS deployment, and harmless state change remain before this should be
called live. Combat actions remain out of scope until actor authorization,
idempotency, and timeout recovery are demonstrated in the Avrae process.

## Disposable local validation

The local Windows validation checkout is kept outside this repository. The
repeatable setup is:

```powershell
git clone --depth 1 --branch nightly https://github.com/avrae/avrae.git avrae-local
cd avrae-local
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Set `PYTHONPATH` to include the Quartermaster checkout, the Quartermaster
`src` directory, and the Avrae checkout, then load
`integrations.avrae.quartermaster_cog` from the Avrae virtual environment.
For a real dependency smoke test, start only Avrae's `mongo` and `redis`
services under a disposable compose project, insert a minimal native combat
document into MongoDB, and run the signed HTTP round trip. This exercises the
real `Combat.from_id` model-loading path and verifies MongoDB/Redis reachability
without connecting to Discord. Remove the disposable containers and volumes
afterward; do not start Avrae's bot service without its credentials and secrets.

The 2026-08-23 run completed with MongoDB ping `1`, Redis `PONG`, and a
`COMMITTED` response containing `active: true` and the native summary message
ID. The earlier fake-Mongo smoke remains useful as a fast test of the same
Python boundary.

On the 2026-08-23 nightly checkout (`12b146f`), importing the initiative Cog
also required local-only lazy-import workarounds for upstream circular imports.
Those edits are confined to the disposable Avrae checkout and are not claimed
as Quartermaster or upstream Avrae fixes.
