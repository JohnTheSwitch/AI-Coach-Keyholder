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

Phase E6: `archive.py` moves a removed tenant's database to `tenants/archive/` (read-only, after a 5-minute grace; platform migration 004), plus isolation
hardening and an end-to-end isolation audit test. Operator runbook: `docs/operations/multi_wearer_runbook.md`. See the proposal, section 16.


## Feature parity for additional wearers (owner, 2026-11-13; one slice at a time)

1. **Trust domains and the daily evaluation (done).** An additional wearer's database had no Trust domain, so their daily evaluation, check-in
   level and supervision never ran. `TenantFactory.build` now creates the domains the tenant configuration names
   (`trust_manager.domain_admin.ensure_domains_with_consent`), each with its consent record first, quoting the invitation the wearer accepted
   (which names their trust records). Only an ACTIVE tenant is ever built; a rebuild creates nothing twice.
2. **The wearer's own time zone and public holidays (done).** The wearer sends `timezone <IANA name>` (e.g. `timezone Europe/Berlin`) and
   `holidays <country code|none>` in their DM; bare `timezone` / `holidays` shows the current value. `system/wearer_locale.py` validates them
   (a region IANA name or `UTC`; a country the `holidays` package supports) and stores them in the tenant's own database (migration 061,
   `wearer_locale`). `tenant_config` applies them over the owner's APP_TIMEZONE / HOLIDAY_COUNTRY, so quiet hours, the workday, check-in slots,
   the evaluated day and the release windows follow the wearer's clock; a change drops the runtime and the next reconcile rebuilds it. A setting
   never made falls back to the owner's. The welcome text after `accept` asks the wearer to set both first.
3. **The notification queue (done 2026-11-14).** Each additional wearer gets the owner's outbound queue over their OWN database
   (`TenantFactory._notifications`): retries, the wearer's own quiet hours (an unreadable layout holds routine notices), and delivery that
   reaches only that wearer's Discord DM (any other user id is undeliverable). The tenant loop delivers once per cycle, so it inherits the
   stagger and stops the moment the tenant is no longer managed. Through it the wearer now gets what they never got before: penalty notices
   (warning, window started / extended / resumed / ended), check-in warnings and release notices (immediate). Built first because the next
   slices (exemptions, detector questions, photo check-ins) all speak through it.
4. **Buttons for additional wearers (done 2026-11-14).** The gateway now carries a reply's buttons and follow-up cards (before, with
   multi-wearer on, every button was dropped, the owner's too), and a DM button pressed by an additional wearer goes to the facade of the
   same scope in that wearer's runtime (`system/tenant_dm_actions.py`, `TenantRuntime.dm_actions`), never to the owner's.
5. **Exemptions and hygiene openings (done 2026-11-16, work queue Q5).** `TenantFactory._exemptions`: the owner's ExemptionService over
   the wearer's database, decided by the same judge (no keyholder button), hygiene openings by the wearer's own hygiene Trust. An opening acts
   only on the tenant's CONFIRMED paired lock (`Tenant.lock_ready`), held by the platform keyholder, and only after the owner cleared THIS
   tenant's execution halt (`system.composition.build_tenant_exemption_opening`; the halt is seeded halted and fails closed). The relock
   picture, lines and recovery tasks stay owner-only. Exemptions are ticked by the tenant's periodic cycle.
6. **The detectors and the judge (done 2026-11-16, Q6).** `TenantFactory._detectors`: hygiene misuse, lock claims and emergency reviews
   through the wearer's OWN Chaster connection (the watches list the locks the wearer wears), the same question-and-judge path, incidents in
   the tenant's own Penalty Engine. Without the wearer's connection nothing is watched. Run once per tenant cycle; the infraction and
   hygiene buttons route to them; a late exemption relock is raised through them.
7. **Photo check-ins (done 2026-11-16, Q7).** Wired through the wearer's own connection; off until the wearer sends `photo check-ins on`
   (the consent fails closed); none during their wear exemption.
8. **Confessions (done 2026-11-16, Q8).** A hygiene confession takes the owner's deterministic path (offered as a self-report with the same
   buttons).
9. **The coach and the spot checks (done 2026-11-16, roadmap slice 8 / Q9; migration 085).** The invitation now discloses the coach and the
   spot checks (`COACH_DISCLOSURE`); the tenant gets the Honesty domain at acceptance, the coach (behind ENABLE_COACH, like the owner's) and
   spot checks on its confirmed paired lock, on by default; `proof checks off` / `proof checks on` switch them immediately
   (`proof_check_switch`). Spot checks need the detector questions, so the wearer's Chaster connection.
10. **Feature switches and the 24-hour switch-off wait (done 2026-11-16, owner: multi-wearer first).** `TenantFactory._switches`: the
    wearer's own `features` and G3's opt-out wait over their database; the opt-out question is ticked by the tenant cycle.
11. **Lines (done 2026-11-16).** `TenantFactory._lines`: lines for every confirmed incident in the wearer's database, typed in the DM
    (paper lines stay owner-only), swats or a written request instead; owed lines hold the next hygiene opening and a release; the
    wearer's `lines` switch applies. The tenant cycle drains them as an extra outbox consumer.
12. **Obedience tasks (done 2026-11-16).** `TenantFactory._obedience`: the daily task on the wearer's confirmed paired lock through their
    spot checks, misses through their own detector questions. Their limits and gear are not known yet, so `erotic_tasks` and `workouts`
    start OFF (seeded once, never over the wearer's own choice) and the gear list starts EMPTY (`gear add`). The training plan is the
    owner's programme, so they never get it: `workouts on` waits for a plan of their own, written by the owner and approved by them, and
    `temptations on` for a temptation of their own approved the same way (2026-11-17; `custom_tasks/`, `TenantFactory._temptations`). Their erotic
    tasks are only their own, written by the owner from their limits and approved by them (2026-11-17, `custom_tasks/`).
12b. **Everything else, if they choose (done 2026-11-17).** `TenantFactory._recovery_paper_review` and `_obedience`: recovery tasks
    (`recovery_tasks`, seeded off), lines on paper, the weekly review (with the coach), health uses counted (G2), the task board
    (`GENERIC_BOARD`, `task_board` seeded off) and the read-only Chaster tool over their own connection. Extra passes run from
    `TenantRuntime.extra_ticks` in the tenant cycle.
13. **Earning the release, the release window and the relock picture (done 2026-11-16).** `TenantFactory._release_and_relock`: a token
    waits for the day's verified training session or the release lines; with the wearer's own Chaster connection, the 2-hour release
    window (behind their `release relock` switch, ticked by the tenant cycle) and the relock picture after a planned hygiene opening
    while their hygiene Trust is below 0.70. Without the connection neither runs (unknown never counts against them).
