# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A personal, local AI coach and accountability system. It runs as a Discord DM bot, uses a local Ollama model, stores data in SQLite and integrates with the Chaster lock platform. `philosophy.md` holds the principles every change must satisfy, and section 6, "What the System Never Does", is a list of hard limits.

## Commands

There is no `.venv` here. Development uses the machine's Python 3.14, which has pytest installed. On Windows, `python` and `py` can resolve to a Store stub; see `windows/README.md` if they misbehave. `py` works on this machine.

```bash
py -m pytest                                                    # whole suite
py -m pytest tests/penalty_engine                               # one module
py -m pytest tests/infrastructure/test_clock.py                 # one file
py -m pytest tests/path/test_x.py::TestClass::test_name         # one test
py -m compileall ai bot core database integrations observations # all that CI runs
```

The whole suite (5,033 tests) takes about 15 minutes, so run it in the background and use targeted runs while iterating. `pytest.ini` sets `--strict-markers`, so an unregistered marker is an error. There is no linter or formatter configured. CI (`.github/workflows/python-ci.yml`) only does the `compileall` above and doesn't run the tests, so a passing CI says nothing about behaviour. The README's "No CI currently exists" is out of date.

The receiver has its own suite and dependencies, and runs from the sibling `hive-venv`, not the bot's environment. The bot's suite doesn't need FastAPI; it checks only isolation and the contract, in `tests/hive_mind_receiver/`.

```bash
../hive-venv/Scripts/python -m pip install -r hive_mind_receiver/requirements-dev.txt
../hive-venv/Scripts/python -m pytest hive_mind_receiver/tests
```

Running things:

```bash
py -m bot.discord_bot                  # the bot
.\windows\run_bot.ps1                  # the bot, finding a real interpreter itself
py -m cli --help                       # owner/operator CLI
py -m scripts.execution_control status # the runtime halt: status | halt "why" | clear "why"
py -m scripts.seed_development_tasks   # idempotent dev task templates
```

**Treat all of these as live.** They use `.env` and the real database `data/coach_keyholder.db`, and only one bot instance may run at a time, because two would both answer DMs. Don't start the bot or run a mutating CLI command unless asked. CLI commands marked `LIVE` in `--help`, such as `freeze-lock`, act on real Chaster locks. Also leave the halt alone unless asked: clearing it is what allows real lock actions.

Don't read or print `.env`, whose keys are documented in `.env.example`. `data/` and `logs/` are gitignored and hold real personal data.

## Architecture

**Event-driven modular monolith.** Each top-level package is a domain module that owns its own tables. Modules communicate through a transactional outbox (`infrastructure/outbox.py`, `consumer_registry.py`), and `system/` is the composition layer that wires them together without holding domain state. The core lifecycle:

```
Incident (trust_manager) -> Trust Score -> Penalty Window (penalty_engine) -> Recovery Plan -> Recovery Credit (back to penalty_engine)
```

**Message path.** `bot/discord_bot.py` converts a DM to `IncomingMessage` and calls `ApplicationService.handle_message()` in `application/service.py`, which never raises. The order there is onboarding, then `CommandRouter` (exact matches only), then the deterministic Chaster status and read-only natural-query matchers, and only then `conversation_engine` (Ollama). `application/` must not import `discord`. `bot/` handles messages only through `ApplicationService`, but it is also the composition root, so it does import and construct many modules. The README's claim that it imports no domain modules is out of date.

**Background work.** Startup runs `on_system_startup()` in `system/startup.py`: per-module `recover_*_state()`, then the outbox publisher. `setup_hook()` then starts the asyncio loops. `system/composition.py` builds the rules, execution, release and supervision machinery. Features are behind `ENABLE_*` flags, on by default since 2026-11-10 (owner: easy setup) except the research export and multi-wearer; each one reports "NOT active" in the log when its settings are missing, and nothing touches a lock until the owner clears the runtime halt.

**Planes to keep distinct:**

- **Rules Engine** (`rules_engine/`): decides, but executes nothing; it only persists dry-run decisions.
- **Execution** (`chaster_execution/`): `ChasterExecutor` is the only caller of the transport, and it consumes persisted decisions only.
- **Emergency unlock** (`chaster/emergency_unlock_server.py`): a separate standalone local process, never imported by the bot.
- **Hive receiver** (`hive_mind_receiver/`): standalone, shares no code with the bot, and treats senders as untrusted. A test enforces both the isolation and the contract.

**Multi-wearer** (`tenancy/`, `system/tenant_*.py`, flag off): each tenant gets its own SQLite database within one process, plus a small shared platform database. `tenancy/` imports no domain modules, and domain modules have no notion of a tenant.

## Hard invariants

Several of these are enforced by repository-wide tests that scan the source, so breaking one fails in a test far from your change.

**Safety of the live system:**

- The system never changes a lock's duration, up or down. No time method exists on the transport or executor, and none may be added.
- The Chaster `/unlock` endpoint and lock destruction are banned from the execution plane. Only freeze and unfreeze execute.
- Execution is doubly gated: `ENABLE_CHASTER_EXECUTION` plus a runtime halt that is seeded halted and fails closed if its row is missing or unreadable. New gates must also fail closed.
- An ambiguous outcome (timeout, 5xx, unexpected status) ends as `needs_review` and is never retried.
- Models can't change state. Known commands never reach the model, conversation context providers are read-only, and the coach only proposes from the closed catalogue in `coach/actions.py`, which applies only on the user's own button. Don't add a path where model output writes state or acts on a lock. The one exception (owner, 2026-11-10, because there is no second person to decide): `infractions/judge.py` judges the wearer's explanation of a detector finding, and an unexplained finding is confirmed by the system. Health and emergencies are decided in code before any model, and a model failure is never a verdict.
- Provider data is labelled provider-reported, never "verified". Unknown stays unknown, and unconfirmed Chaster fields are read only when present with the expected type.

**Code conventions** (`docs/architecture/implementation_conventions.md`; read section 15 before adding a module):

- Never call `datetime.now()` or `utcnow()` outside `infrastructure/clock.py`. Inject a `Clock`, and use `FrozenClock` in tests.
- Each table has exactly one writing module. Other modules read only through its narrow API, named `get_<noun>()` or `determine_<noun>()`, never `fetch_`, `load_` or `query_`.
- A state change and its domain event are written in one transaction. A state change without an event is a defect.
- Events are named `<source_module>.<verb_past_tense>`.
- A consumer handler must never call another module's transaction-opening method, which raises `NestedTransactionError`. It uses only the event payload, so payloads must carry what consumers need. Inside a handler, use the `*_in_transaction` variants.
- Migrations in `database/migrations/` are additive, numbered, and never lose data. Don't edit an applied one. Use a nullable column rather than a fabricated default when no truthful value exists for old rows. Plugins and `tenancy/` have their own migration tracks.
- Provisional constants use the structured tag `# BOOTSTRAP_DEFAULT(owner=undecided, mechanism=code):`.
- The bot's output is English only (since v0.37). The input matchers still recognise Czech deliberately, so don't remove that.

## Docs

- Module READMEs are the authoritative record of what is built, what is deliberately deferred and why. Read the one for the module you're changing, and update it in the same change.
- Approval is per slice. Most design docs in `docs/architecture/` are drafts with only specific slices approved, so a design existing doesn't authorise building the rest of it. Don't extend scope past a slice without the owner's decision.
- `docs/architecture/` is canonical. The design docs and `domain_glossary.md` at the repo root have the same names but different contents, and the README links only to `docs/architecture/`.
- The top-level `README.md` is largely a historical record. For current state use its "Current status" paragraph, and don't trust its "Structure" tree, which lists files that don't exist. Check the code.
