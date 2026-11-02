# tenancy

Multi-wearer support (v0.39 Phase E). Architecture (approved): one tenant per managed person, each with its OWN isolated SQLite database, all in one process; the
core domain modules are untouched. See `docs/architecture/dual_role_and_messaging_proposal.md` sections 10-11.

**Phase E1 (this package): the shared platform layer.** Off unless `ENABLE_MULTI_WEARER=true`; with the flag off nothing here is built and the running system is unchanged.

- `platform_db.py` + `migrations/001_platform.sql`: a small separate SQLite file (`PLATFORM_DB_PATH`, default `platform.sqlite` next to the main database). It holds the tenant
  registry, the global halt and a lifecycle audit, nothing else (no goals, Trust, lock data, tokens or message text).
- `registry.py` `TenantRegistry`: `bootstrap_tenant_zero` (registers the existing system as `tenant_0`, idempotent, never touches its database), `invite` (keyholder),
  `accept` (the wearer, in their own Discord DM), `suspend` / `resume` (keyholder pause), `remove` (keyholder, unilateral) and `leave` (wearer, unilateral), `pair_lock`.
  Statuses: `invited -> active <-> suspended`, `removed` (terminal). Only an ACTIVE tenant is "managed": a suspended or removed tenant's management and polling stop at
  once. Tenant zero can be neither removed nor left. The cap (`TENANT_CAP`, default 5) counts every non-removed tenant, tenant zero included. Every change is a
  compare-and-set transition with an audit row in `tenant_events` in the same transaction.
- `router.py` `TenantRouter`: a pure lookup from a Discord id (or Chaster account id) to `active | invited | suspended | unknown`. A removed tenant looks like a stranger.
  The Discord wiring that uses it comes with E3.
- `halt.py`: `PlatformHalt` (one switch for every tenant) and `CombinedHalt` (halted when the platform OR the tenant's own halt is set). Both fail closed.
- `admin.py` `TenantAdmin` + `python -m cli tenants list|invite|suspend|resume|remove|halt|unhalt`: owner operations. Every mutation needs `--owner-id` equal to
  `OWNER_USER_ID` (constant-time compare; unset refuses everybody), refusals are audited; `remove` needs `--yes`; `list` is read-only.
- `services.py` `open_platform_services`: opens the platform database, registers tenant zero (reading only the owner's Discord id from the main database) and holds the
  registry, router, halt and the SHARED keyholder token manager. The keyholder (bot) Chaster connection stays in tenant zero's database: Chaster refresh tokens rotate, so
  exactly one place may own that connection, and every tenant will use it through this one manager.

The package imports no domain module (AST-guarded); only `bot/`, `cli/`, `system/` and tests may import it.

Phase E2 (in `system/`, not here, because the factory must import the domain modules): `tenant_factory.py` builds an isolated runtime per ACTIVE tenant, `tenant_loops.py` runs one staggered
periodic loop per tenant and stops it the moment the tenant stops being managed; a new tenant's execution starts halted (`python -m cli tenants execution <id> clear ...`). See the proposal, section 12.

Phase E3 (`system/tenant_gateway.py`): Discord routing by author id, the invitation DM and `accept` / `decline`, the wearer commands `halt`, `resume` and
`leave` / `leave confirm`, per-tenant message logging (platform migration 002). See the proposal, section 13.

Phase E4 (`system/managed_lock_source.py`, `system/lock_pairing.py`): each tenant's lock is observed through the keyholder search and only after the pairing check
(active tenant, confirmed pairing, the paired lock and wearer, the bot as keyholder, `locked`); the owner pairs with `tenants pair`, the wearer confirms with `confirm lock`
(platform migration 003, main migration 035). See the proposal, section 14.

Phase E5: per-tenant Chaster chat (each tenant's own chat stack over its own database, identity = the confirmed pairing) and the shared keyholder
request budget (`chaster/api_budget.py`, `CHASTER_API_BUDGET_PER_MINUTE`). See the proposal, section 15.

Not built yet: E6 (isolation hardening, removal/archival flow, runbook).
