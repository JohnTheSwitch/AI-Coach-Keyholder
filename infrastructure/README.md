# infrastructure

Shared, cross-cutting components used by every domain module. Never a
domain module itself — this package owns no business state and makes
no business decisions (`implementation_conventions.md` Section 2: every
stateful table has exactly one *domain* owner; this package is where
the plumbing those domains share actually lives).

## Why `Clock` exists

Across the architecture baseline, several documents independently
arrived at the same requirement: no timeout, deadline, or expiry in
this system may be tracked by an in-memory timer or read from an
uncoordinated, directly-called system clock.

- `penalty_window_technical_design.md` 4.5/2.8 (I24/RESTART-1): "No
  important timeout may exist solely as an in-memory timer... all are
  stored as absolute UTC timestamps."
- `activity_authorization_technical_design.md` 16.7 establishes the
  `Clock` protocol itself, plus a future `MonotonicGuardedClock` that
  will guard against the system clock jumping backward after a
  restart.
- `implementation_conventions.md` Section 8 (Naming Conventions) and
  Section 9 (Crash/Restart Recovery Conventions) both assume every
  `recover_<module>_state(db, now)` function receives its `now` from
  an injected `Clock`, never from calling `datetime.now()` itself.

The reason this is infrastructure, not a detail left to each module: if
every module called `datetime.now(timezone.utc)` on its own, testing
any restart/expiry scenario deterministically would require either
real elapsed wall-clock time in tests (slow, flaky) or mocking a global
function per test (easy to do inconsistently across modules). A single
injected `Clock`, used everywhere, means every module's tests — and,
later, a system-wide `MonotonicGuardedClock` — are written once, the
same way, here.

**Important clarification of scope** (per architectural review): `Clock`
is not a mechanism for computing "time until unlock" — this system does
not decide unlocking by counting down a Chaster-side timer. `Clock` is
the uniform source of time for this system's own internal events:
Incident creation, Penalty Window start/end, cooldowns, authorization
validity windows, history/audit timestamps, the moment a request or
decision is made, and event ordering. The decision to unlock is made
over domain state; Chaster (or any future physical-lock integration) is
a downstream technical integration layer, not the source of that
decision.

### `Clock`, `SystemClock`, `FrozenClock`

- `Clock` — the `Protocol` every module depends on.
- `SystemClock` — the production implementation (wraps
  `datetime.now(timezone.utc)`). Does NOT guarantee monotonicity — see
  its docstring; that guarantee is the explicit job of a future
  `MonotonicGuardedClock`, not this class.
- `FrozenClock` — the test implementation: starts at an explicit time,
  and only changes when a test calls `advance()` (relative) or `set()`
  (absolute jump, in either direction — deliberate, see its docstring).

A repository-wide guard test
(`test_no_direct_datetime_now_calls_outside_clock_module`,
`tests/infrastructure/test_clock.py`) enforces "production code never
calls `datetime.now()`/`datetime.utcnow()` directly" mechanically, not
just as a written rule. As of Phase 1.2, this guard has **zero**
production exceptions — `KNOWN_PRE_CLOCK_VIOLATIONS` is empty (see
"What changed in Phase 0" below).

## Why `infrastructure.database` exists

`Database`/`Transaction`/`apply_transition()` (`infrastructure/database.py`)
are the single, shared way every module opens an atomic unit of work
against SQLite — `implementation_conventions.md` Section 4 (`_apply_transition`)
and Section 2 (single-writer ownership) made concrete in actual code,
not just as a documented pattern each module was expected to reinvent.

**Who owns the transactional boundary:** `infrastructure.database.Database`
alone. It owns connection creation, pragma configuration
(`foreign_keys`, WAL journal mode, `busy_timeout` — set deliberately,
not left to SQLite's defaults), and the single `transaction()` context
manager that commits on success and rolls back on any exception. No
other class in this codebase opens a `sqlite3.connect()` call directly.

**Why repository methods never call `commit()`/`rollback()` themselves:**
`Transaction` (the object yielded by `transaction()`) exposes only
`execute()`/`executemany()`/`fetch_one()`/`fetch_all()` — deliberately
narrow and technical, per architectural review: it must never grow
domain-shaped methods like `insert_incident()` or `record_trust_history()`,
which belong in a repository (`database/database.py`), not in the
transactional primitive itself. Committing is a decision `Database.transaction()`
makes exactly once, when its `with` block exits without an exception —
letting an individual repository method commit independently would
make it impossible to compose two repository operations into one
atomic unit, which is the entire reason this class exists.

**How the next phase (transactional outbox) will slot in without
changing existing call sites:** `apply_transition()`'s `events=`
parameter is already present and already unused by every current call
site. When `domain_events` is introduced, a call site that needs to
also write an event passes `events=some_callable`; every call site that
doesn't need one is completely unaffected. No existing signature or
call site changes when that phase begins.

## Why `infrastructure.outbox` exists (Phase 1.4)

`DomainEvent`/`write_event`/`claim_pending_events`/`mark_published`/
`has_been_processed`/`mark_processed`/`consume_event`
(`infrastructure/outbox.py`) are the shared transactional outbox
`implementation_conventions.md` Section 5 describes, and the first
real consumer of the `events=` slot `apply_transition()` has carried
since Phase 1.2.

**Schema versus behavior, the same split as `infrastructure/database.py`:**
the `domain_events`/`domain_event_consumers` tables are defined in
`database/migrations/002_domain_events.sql` — a project migration, not
infrastructure code — while every behavior that reads or writes them
lives here, domain-agnostic. `infrastructure/outbox.py` never imports
from `database/`; test-only setup convenience (a fully migrated schema)
is the only place the two meet, in `tests/infrastructure/test_outbox.py`.

**Write side:** `write_event(tx, event)` is called from inside an
already-open `Transaction`, exactly the shape `events=` expects — the
event and the state change that caused it commit or roll back
together, never independently. A fresh event id is generated on every
call; there is no write-side idempotency concern, since a rolled-back
transaction attempt writes nothing to roll back.

**Publish side:** `claim_pending_events()`/`mark_published()` are the
claim/publish half of the outbox — a publisher claims a batch of
unclaimed-or-lease-expired rows (one atomic `BEGIN IMMEDIATE`
transaction, so two concurrent publisher processes can never claim the
same row), delivers them, then marks them published. At-least-once
delivery is the explicit contract, not an oversight — see
`consume_event` below for what makes that safe.

**Consume side:** `has_been_processed()`/`mark_processed()` are the
consumer-side dedup guard (`domain_event_consumers`,
`UNIQUE(event_id, consumer_name)` via its primary key). `consume_event()`
is the consumption counterpart to `apply_transition()` — built directly
on top of it (dedup-check via `load`, the consumer's own reaction plus
`mark_processed()` via `write`), not a parallel reimplementation of
transaction handling. Returns `True` if the handler actually ran,
`False` if the delivery was a harmless, silently-absorbed redelivery.

**No real domain module exists yet to wire this into** (Trust Manager,
Penalty Engine, etc. — `docs/architecture/domain_events_catalog.md` —
are still architecture, not code). The one real, honestly-labeled
demonstration is `database/database.py`'s
`record_rule_change_with_consent()`, which now also emits a
`consent_log.rule_change_recorded` event via the `events=` slot — this
is Phase 0 demo wiring, not a catalog event, and is documented as such
at its call site. The outbox itself is fully implemented and tested
(`tests/infrastructure/test_outbox.py`) independent of that one
example, ready for the first real domain module's events when it
arrives.

**Finding 6 (`docs/architecture/domain_events_catalog.md` Section 9),
resolved:** every event type, without exception, goes through this one
outbox — no per-event-type carve-out for "this one doesn't need
delivery machinery." Simplicity (one mechanism, no special cases) won
over minimalism (skip the outbox for today's zero-consumer events).

**Why migrations use `raw_connection()`, not `transaction()`:**
`sqlite3.Connection.executescript()` (used to apply a `.sql` migration
file) implicitly commits any pending transaction before running and
does not participate in manual `BEGIN`/`COMMIT`/`ROLLBACK` control the
same way `execute()` does. Wrapping it in `transaction()`'s `BEGIN
IMMEDIATE` would misrepresent the atomicity actually available — this
is a genuine SQLite/Python constraint, not a limitation introduced by
this wrapper. `raw_connection()` is a deliberately separate, clearly
documented escape hatch for exactly this case; domain writes must never
use it. Confirmed by a real test
(`tests/infrastructure/test_database.py::TestRawConnection::test_raw_connection_contends_with_an_open_transaction_on_the_same_file`):
using it concurrently with an open `transaction()` on the same database
file raises `sqlite3.OperationalError` once `busy_timeout` elapses,
rather than silently succeeding.

**Nested transactions are explicitly forbidden**, not given SAVEPOINT-based
semantics: calling `transaction()` while one is already open on the same
`Database` instance raises `NestedTransactionError`. A domain operation
needing several steps to be atomic together must be one
`transaction()`/`apply_transition()` call, not two nested ones — nesting
would silently open a second, independent SQLite connection while the
first is still open and uncommitted.

**`:memory:` is explicitly rejected** in `Database.__init__` — this
wrapper opens a new connection per `transaction()` call, and SQLite's
`:memory:` database is unique per connection unless shared-cache mode
is enabled (which this wrapper does not do). Tests use a real temporary
file (pytest's `tmp_path` fixture), never `:memory:`.

**Thread-safety is explicitly out of scope for now**: one `Database`
instance is not safe for concurrent `transaction()` calls from multiple
threads (the open-transaction guard is a plain instance attribute, not
a lock). This system's current scope (a single-process Discord bot)
does not need cross-thread sharing of one instance. Cross-*process*
concurrency is a separate, already-handled concern: `BEGIN IMMEDIATE`
correctly serializes concurrent writers across OS processes at the
SQLite engine level regardless of this class's thread-safety scope.

## What changed in Phase 0 (Phase 1.2)

Per architectural review, `field(default_factory=utc_now)` was not
merely "calling a global function" — the deeper problem was that a
*model* was deciding the time of its own creation. Fixed by removing
`utc_now()` entirely (`database/models.py`) and making `created_at` a
required, keyword-only constructor parameter on every affected
dataclass (`ContextSnapshot`, `CoachAssessment`, `KeyholderAssessment`,
`DecisionResult`, `ObservationRecord`, `Rule`, `ConsentRecord`,
`ConversationMessage`). There is now exactly one path by which any of
these objects can receive a timestamp: the caller passes
`created_at=clock.now()` explicitly, having obtained `clock` via
dependency injection. Neither the model, the repository
(`database/database.py`), nor the transactional core
(`infrastructure/database.py`) generates a timestamp on its own.

`database/backup.py`'s two `datetime.now()` call sites were migrated
the same way — every time-dependent function (`create_backup()`,
`has_backup_today()`, `ensure_daily_backup()`) now takes `now: datetime`
explicitly. `database/database.py`'s `record_trust_history()` and
`mark_observation_reviewed()` (which called `utc_now()` indirectly, not
`datetime.now()` directly — the AST guard did not previously catch
these, since it was watching for the wrong call, not the wrong
*pattern*) now take `now: datetime` explicitly as well.

`bot/discord_bot.py` now constructs and owns a `SystemClock`, passed to
`build_bot()`/`CoachKeyholderBot`, and supplies `created_at=self.clock.now()`
at both `ConversationMessage(...)` construction sites.

`KNOWN_PRE_CLOCK_VIOLATIONS` (`tests/infrastructure/test_clock.py`) is
now empty — kept as a named, empty placeholder (not deleted) so the
pattern stays visible for any future, deliberately-documented exception.

## What this phase deliberately does NOT deliver

- ~~**The transactional outbox (`domain_events`)**~~ — **delivered in
  Phase 1.4** (`infrastructure/outbox.py`).
- ~~**A standalone consumer framework/registry**~~ — **delivered in
  Phase 2.4** (`infrastructure/consumer_registry.py`:
  `ConsumerRegistry`/`process_pending_events()`).
- ~~**The startup orchestrator**~~ — **delivered in Phase 2.4**
  (`system/startup.py`'s `on_system_startup()`; the restart-safe lease
  itself is `infrastructure/startup_lease.py`). See `system/README.md`
  for a real architectural finding this integration surfaced
  (cross-module `NestedTransactionError` and its resolution).
- **`MonotonicGuardedClock`** (`activity_authorization_technical_design.md`
  16.7) — now that the database wrapper exists, this is unblocked, but
  still not implemented here. It will implement the same `Clock`
  protocol, so introducing it means changing what gets injected at the
  composition root — no call site anywhere else needs to change.
- **Verification/Chaster integration policy** (`VerificationRequirement`/
  `VerificationContext`/`VerificationGateway`) — a separate, later
  architectural concern for the `integrations/` layer. Nothing in this
  phase's `Clock`/`Database`/`Transaction`/`Outbox` design assumes
  anything about it, so no conflict exists either way.

## Why `infrastructure.plugin_sdk`/`plugin_fault_boundary`/`plugin_models` exist (Phase: Plugin Infrastructure Step 1)

Canonical: `docs/architecture/plugin_architecture_proposal.md` v1.1.
**This is Step 1 of that document's own recommended implementation
order (Section 27): `plugin_sdk.py` + `plugin_fault_boundary.py`,
tested in isolation, with zero real plugins yet.** `PluginRegistry`
(discovery, manifest validation, wiring into the *existing*
`ConsumerRegistry`/`CommandRouter`) is Step 2, not built here.

- **`plugin_models.py`** — `PluginManifest`, deliberately with no
  `depends_on_plugins`/`optional_plugins`/`conflicts_with`/`load_after`
  field (PLUG-9) — checked directly by
  `tests/infrastructure/test_plugin_models.py::TestPluginManifestDefaults::test_has_no_dependency_fields`,
  not merely asserted in prose. `__post_init__` enforces PLUG-2 (a
  plugin's declared `publishes_event_types` must all be namespaced
  under its own name) and Decision 8 (`trust_tier` must be
  `'first_party'` in the MVP) at construction time — the earliest
  possible point.
- **`plugin_sdk.py`** — `PluginSDK`/`build_plugin_sdk()`. PLUG-5's
  actual mechanism: a read method for a domain module (e.g. `get_goal`)
  is only ever bound onto an SDK instance if the plugin's manifest
  declared the corresponding capability (`'goal_management.read'`, ...)
  — an undeclared capability means `hasattr(sdk, 'get_goal')` is
  `False`, not merely "raises if you try." Read methods delegate
  directly to each domain module's own already-public methods (nothing
  re-implemented, nothing narrower or broader than what every other
  consumer in this system already gets). `publish_event()` enforces
  PLUG-2 a second time, at actual call time — a manifest passing
  `__post_init__`'s check says nothing about what a plugin's code might
  construct as an `event_type` string at runtime.
- **`plugin_fault_boundary.py`** — `PluginFaultBoundary`. PLUG-6: every
  call is wrapped in a `try/except` that never propagates. PLUG-7
  (**renamed in v1.2, after review caught that v1.1's naming
  overstated what it does**): an **execution budget**, not a timeout —
  every call is measured, and one exceeding
  `execution_budget_seconds` is logged and counted toward the failure
  threshold once it returns, but the handler is never interrupted. A
  genuinely hung (infinite-loop) synchronous handler will hang the
  call, and its caller, indefinitely — a real, current, and explicitly
  documented limitation, not something the old `timeout_seconds`/
  `exceeded_timeout` naming should have implied was solved. **A real
  implementation-time finding, not assumed away:** a
  thread-with-join-timeout approach (which would have provided a true
  hard timeout) was considered and rejected, because
  `infrastructure/database.py` opens its `sqlite3` connection without
  `check_same_thread=False` — a handler touching the shared
  `core.transaction()` from a different thread than the one that
  opened the connection would raise `sqlite3.ProgrammingError`, a real
  bug given a plugin's whole point is to be able to touch the database
  (PLUG-5). `plugin_architecture_proposal.md` Section 26's Open
  Question 4 (a genuine hard timeout) remains open, not resolved by
  this module — it would need a fully asynchronous handler contract
  with cooperative cancellation, a separate process, or another truly
  preemptible execution boundary. A per-plugin rolling failure-count
  circuit breaker (Decision 5) auto-disables a plugin after repeated
  failures within a time window — verified independent per plugin
  (`TestDecision5CircuitBreaker::test_a_failure_in_one_plugin_never_affects_another`).

**A second real finding, surfaced while building Step 1 (already
documented in the design doc's own Section 1, confirmed here by
reading the actual code, not assumed):** `ConsumerRegistry.dispatch()`
gave every consumer its own transaction, but had no exception boundary
at all — an unhandled exception from any handler still propagated
straight through `dispatch()` and `process_pending_events()`. **Fixed
in Step 2, for every consumer, not only plugins** — see below.

## Why `infrastructure.plugin_registry` exists (Phase: Plugin Infrastructure Step 2)

Canonical: `docs/architecture/plugin_architecture_proposal.md` v1.3
Section 27. `PluginRegistry` discovers, validates, and loads
first-party plugins, wiring their handlers into the *same*
`ConsumerRegistry`/`CommandRouter` Step 1 already reused rather than
replaced. Deliberately out of scope, per its own design document and
explicit review guidance before this Step began: no startup
integration, no plugin migrations, no real plugin (`goal_celebration`
is a later, separate step) — proven entirely against synthetic,
`tmp_path`-constructed plugin directories
(`tests/infrastructure/test_plugin_registry.py`).

- **Discovery (PLUG-9)** — `discover()` lists every subdirectory of
  `plugins/` containing a `manifest.py`, sorted alphabetically. No
  dependency resolution, matching Decision 9's MVP scope exactly.
- **Manifest loading, then compatibility validation, before the
  plugin's own implementation is ever imported (Decision 6)** —
  `load_manifest()` imports only `<plugin>/manifest.py`;
  `validate_compatibility()` checks `trust_tier == 'first_party'`
  (Decision 8) and the manifest's declared `min_core_version`/
  `max_core_version` against a running `CORE_VERSION` constant (itself
  `BOOTSTRAP_DEFAULT`-tagged — how this should actually be maintained
  long-term is undecided). Only a *compatible* manifest's
  `<plugin>/handlers.py` is ever imported — an incompatible or
  malformed plugin's implementation code never runs at all.
- **`load()` builds the per-plugin `PluginSDK`
  (`build_plugin_sdk()`, Step 1) and one `PluginFaultBoundary` per
  plugin**, then wires whatever the manifest declared into the
  existing registries. `load_all()` never lets one plugin's failure,
  at any stage, prevent another's — every failure becomes a
  `PluginLoadFailure` in a returned list, never a raised exception.

**Two real, structural findings from this Step, both resolved
consistently with prior findings rather than patched around:**

1. **`PluginSDK` originally stored `core` as `self._core`** —
   trivially reachable as `sdk._core` despite the underscore, silently
   defeating PLUG-1/PLUG-5 for any plugin regardless of what its
   manifest declared. Fixed: `publish_event` is now bound as a closure
   over `core` in `build_plugin_sdk()`, never a stored attribute —
   `core` lives only in that closure's own cell. Verified directly
   (`TestNoRawDatabaseAccess`), not merely asserted. Documented
   honestly in `PluginSDK`'s own class docstring that this raises the
   bar sharply but is not literally unbreakable (Python still permits
   `sdk.publish_event.__closure__` introspection) — first-party trust
   (code review + PLUG-4's automated import-boundary test) is what
   actually covers that gap; genuine tamper-proof isolation stays
   third-party's deferred problem, now with an explicit line in
   `plugin_architecture_proposal.md` Section 18 recording why:
   **Python language-level encapsulation is not considered a security
   boundary.**
2. **`ConsumerRegistry.dispatch()`'s own loop had no exception boundary
   at all** (Step 1's own flagged finding, resolved here). Fixed with a
   small, additive `try`/`except` per registration — benefiting every
   consumer, first-party included, not only plugins, since there was
   never a real reason for one consumer's bug to abort every other
   registration for the same event. Correctness was the delicate part:
   a plugin's registered handler still needs its *own* transaction
   (opened by `consume_event()`) to roll back correctly on failure, so
   `PluginRegistry`'s event-consumer wrapper calls `PluginFaultBoundary`
   for its tracking/circuit-breaker effect but then **re-raises** on
   failure — letting the real exception reach the transaction boundary
   (rollback happens, `mark_processed()` never runs) before
   `dispatch()`'s new `except` catches it one level up. A plugin's
   *command* handler wrapper, with no such enclosing transaction to
   protect, safely swallows a failure outright instead. Both paths are
   tested directly:
   `test_a_failing_consumers_partial_write_still_rolls_back` (rollback
   correctness) and
   `test_a_failing_event_consumer_does_not_crash_load_all_or_other_plugins`
   (isolation).

## Why `plugins/goal_celebration` exists (Phase: Plugin Infrastructure Step 3)

Canonical: `docs/architecture/plugin_architecture_proposal.md` v1.5
Section 20/27. The first real plugin — proves the whole Step 1/2
design holds together against a genuine, already-published event
(`goal.completed`, `goal_management`), not synthetic test fixtures.
Read-only via `sdk` (`goal_management.read`), owns exactly one table
(`goal_celebration_log`, for idempotency), publishes its own namespaced
event (`plugin_goal_celebration.sent`).

**Introduced `infrastructure/plugin_migrations.py`** —
`apply_plugin_migrations()`, the plugin-scoped equivalent of
`database.database.Database.migrate()`, tracked in a new core table
(`plugin_schema_versions`, migration 012) instead of core's own
`schema_version`. Mirrors that function's logic deliberately closely
(same `executescript()`-per-file approach, same reliance on each
migration's own seed `INSERT`) — resolves
`plugin_architecture_proposal.md`'s own former Open Question 2.

**Introduced the "table-owning plugin" trust boundary** —
`manifest.owns_tables=True` plugins get a `<plugin>/repository.py`
exposing `build_repository(core) -> Any`, given `core` directly
(unlike `PluginSDK`, which never is). Documented honestly in
`infrastructure/plugin_registry.py`'s own docstring ("Table ownership
and the trust boundary this implies") as a real, deliberate exception:
nothing stops a careless or malicious `owns_tables=True` plugin's own
`repository.py` from touching another module's tables too — PLUG-1's
enforcement for this specific path relies entirely on first-party
trust (code review), same as it always has for any domain module's own
use of `core`.

**Three real `NestedTransactionError` findings, all the same bug
class, found by writing and running this plugin's own tests — not
theorized:**

1. A first draft's `has_been_celebrated()` (a read) opened its own
   transaction from inside the event consumer handler, which already
   runs inside `consume_event()`'s transaction. Fixed:
   `has_been_celebrated_in_transaction(tx, ...)`, taking the handler's
   own already-open `tx` — no read-side transaction of its own at all
   (the whole class is now stateless; see its own docstring).
2. The same bug, on the write/publish side: an early draft called
   `sdk.publish_event()` (which opens its own transaction) from inside
   the same handler. Fixed by using the already-existing
   `sdk.publish_event_in_transaction(tx, ...)` (Step 2) instead — this
   plugin is the first real caller of that method, not only a
   synthetic test.
3. **A third, more general instance, not fully resolved in this Step:**
   `sdk.get_goal(goal_group_id)` — any `PluginSDK` read capability —
   delegates directly to a domain module's own public getter, which
   *also* always opens its own transaction. Every SDK read method has
   this same problem when called from inside an event consumer
   handler, not only `get_goal()`. `goal_celebration`'s own handler
   avoids it by not needing a domain read at all (`event.payload`
   already carries what it needs) — see its own docstring. **Decided
   direction for the real fix** (v1.5, not yet built): explicit
   `_in_transaction`-suffixed read variants mirroring
   `publish_event`/`publish_event_in_transaction`, each domain module's
   public getter split into a private tx-only implementation both
   variants delegate to (so SQL/row-mapping is never duplicated) —
   `plugin_architecture_proposal.md` Section 26 Open Question 6,
   Section 27 step 5. Its own, separate infrastructure step; not a
   blocker for anything already shipped.

**PLUG-2 tightened (v1.4, found via a direct review question):**
`publish_event()`/`publish_event_in_transaction()` originally checked
only an event's namespace prefix (`plugin_<name>.*`), not membership in
the plugin's own declared `manifest.publishes_event_types` — meaning a
plugin could publish anything under its own namespace, not only what it
declared upfront. Fixed: `publishes_event_types` is now a binding
allowlist, the same "declare it or you can't reach it" discipline
PLUG-5 already applies to read capabilities. Verified directly
(`TestPublishEventAllowlist::test_publishing_an_undeclared_event_type_is_rejected_even_with_a_correct_namespace`).

## Usage

```python
from infrastructure.clock import Clock, SystemClock, FrozenClock
from infrastructure.database import Database, apply_transition

# Production composition root:
clock: Clock = SystemClock()
core_db = Database("data/coach_keyholder.db")

# A repository method:
def save_something(core_db: Database, thing: Something) -> str:
    def write(tx, _state):
        tx.execute("INSERT INTO things (id, created_at) VALUES (?, ?)", (thing.id, iso(thing.created_at)))
    apply_transition(core_db, write=write)
    return thing.id

# Tests:
from datetime import datetime, timedelta, timezone

def test_something_expires_after_thirty_minutes() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    grant_expires_at = clock.advance(timedelta(minutes=30))
    # ... construct the thing under test using clock.now() / grant_expires_at ...
    clock.advance(timedelta(minutes=1))  # now past expiry
    # ... assert the expected expiry behavior, deterministically, with no real waiting ...
```

Any function or class that needs the current time takes a `Clock` (or
an already-computed `datetime` obtained from one) as a parameter — never
imports `datetime` and calls `datetime.now()`/`datetime.utcnow()`
itself. Any function that changes state takes a `Database`/`Transaction`
and goes through `transaction()`/`apply_transition()` — never opens its
own `sqlite3.connect()`.

## `decision_log.py` (owner, 2026-11-10)

`logs/decisions.log` and `logs/release.log`, installed by `bot/discord_bot.py` at start. `consumer_registry.process_pending_events` writes each
event after it was published, so a rolled-back event never appears. `decision()` and `release_decision()` add lines with the reasoning from the
modules that decide (the release engine and tick, the detector judge). A key containing "score" is written as `[hidden]`. A failing log never
stops a decision.

## The version in every log (owner, 2026-11-12)

At start the bot writes one "System started." line to bot.log, decisions.log, release.log and model.log: the git commit (12 characters), its date
and subject, `dirty` (true when files differ from that commit, so the commit alone does not describe the running code) and the model name. Every
line after it came from that code. `system_version` only runs read-only `git` queries and never raises; outside a repository it says "unknown".
