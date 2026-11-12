# Memory System

Canonical design: `docs/architecture/memory_system_technical_design.md`
(**still `Draft for review, not approved for implementation` as a
whole document** — this README describes exactly which specific slice
of that draft has been implemented here, and nothing more).

## What is implemented here — the non-persistent Working Memory foundation slice

**Process-lifetime, per-subject working memory.** Precisely:

- Survives multiple messages from the same subject within one process run.
- **Not** separated by Discord channel, guild, or conversation session
  — `subject_key` is the only dimension.
- A process restart wipes it completely.
- It is neither long-term memory nor session-identified memory — no
  session ID or session lifecycle exists, and none was added without
  a demonstrated need.

**`models.py`** — `WorkingMemoryRole` (`USER`/`ASSISTANT`),
`WorkingMemoryTurn` (immutable, runtime-validated in `__post_init__` —
a bare type hint alone would not stop `WorkingMemoryTurn(role="system", ...)`
from constructing), `WorkingMemorySnapshot`, `WorkingMemoryError`,
`WorkingMemoryCapacityError`.

**`working_memory.py`** — `WorkingMemoryReader`/`WorkingMemoryWriter`
(`Protocol`s), `InMemoryWorkingMemory` (the one concrete implementation).

## Privacy contract — non-persistent does not mean non-sensitive

Working Memory stores **only**:
- the raw text of an already-validated user message,
- the raw text of its already-validated assistant response,
- which role each turn belongs to.

It **never** stores: commands, onboarding messages, fallback
responses, the system prompt, prompt instructions, an identity
profile, a consent ID, an audit ID, a Discord message ID, a provider
ID, exception text, or any other internal metadata. Raw content is
**never logged** — this module does not import `logging` at all
(verified structurally, `tests/memory_system/test_system_independence.py`),
not merely "no log statement happens to fire today."

## Atomic commit, capacity, and trimming

- `commit_exchange()` stores a whole exchange (`user_content` +
  `assistant_content`) as a single atomic unit — never two independent
  turns that could end up orphaned.
- If the new exchange **alone** exceeds `max_characters_per_subject`,
  `commit_exchange()` raises `WorkingMemoryCapacityError` **before any
  mutation** — nothing is stored, not even the smaller half. A commit
  never looks like a successful no-op. Exactly `max_characters_per_subject`
  is accepted; one character over is rejected.
- Trimming removes the **oldest whole exchange** first by count
  (`max_exchanges_per_subject`), then by character budget
  (`max_characters_per_subject`) — never a lone turn.
- `read()` returns a **new** `WorkingMemorySnapshot` every call — never
  a reference to the internal mutable list. A snapshot taken before a
  later commit is unaffected by it.
- An unknown `subject_key` returns an empty snapshot, never an error.

## Thread safety — no FIFO guarantee of its own

A short internal `threading.Lock` protects only: reading the internal
map, the atomic append of a whole exchange, and trimming — **never**
the duration of anything else, since this module has no notion of
"generation" at all (it is a pure data structure, nothing more).

Concurrent commits for the **same** subject are mutually exclusive —
the structure is never corrupted, no exchange is lost or duplicated —
but their **relative order is not guaranteed** without an external
queue. This module documents only "the order in which commits actually
acquired the internal lock," never a stronger FIFO promise.
**`SubjectConversationQueue`** (`conversation_engine/subject_queue.py`)
remains the layer responsible for real per-subject ordering, when a
future slice wires the two together.

## Conversation Engine integration — implemented (Slice 3)

Previously listed as undecided here; now resolved and implemented.
See `conversation_engine/README.md`'s own "Slice 3" section for the
full detail. Summary:

- `ConversationEngine` reads/writes through `WorkingMemoryReader`/
  `WorkingMemoryWriter` — separate dependencies, injected, never
  constructed by `ConversationEngine` itself.
- A `read()` failure (expected or unexpected) is logged once by
  `ConversationEngine` and degrades to an empty history — never
  blocks the conversation.
- A `commit_exchange()` failure (expected, capacity, or unexpected)
  is logged once; the already-validated response is still returned to
  the user regardless.
- This module itself still never logs anything — the single logging
  point remains `ConversationEngine`'s own orchestration.

## What is explicitly NOT implemented — still draft, still open

- **Persistent memory of any kind** (Episodic, Semantic, Relationship,
  Decision — the remaining four layers `memory_system_technical_design.md`
  describes) — **blocked on a privacy/consent design that does not yet
  exist.** Confirmed by direct audit: no account-deletion mechanism,
  no data-export mechanism, and no consent model beyond a per-write
  audit-trail string (`created_via_consent_id`/`via_consent_id`, which
  only checks "non-empty," never verifies actual informed consent)
  exist anywhere in this project today. A single `sensitive` boolean
  flag (the draft's own Section 3.12) is not a substitute for a real
  sensitive-category policy (health, sexuality, religion, political
  views, finances, precise location, authentication data, third-party
  data, minors' data — none of these are enumerated or governed
  anywhere yet).
- **No database table, no migration.** This slice is pure in-memory
  Python — no `sqlite3`, no `Database`, no transaction (verified
  structurally, not just behaviorally).
- **No memory item IDs, no confidence, no provenance.** Working Memory
  turns are not addressable facts — they are transient conversation
  turns, not "memories" in the draft's own Episodic/Semantic sense.
- **No sensitive-data taxonomy for persistent storage**, no export, no
  delete-all, no account-deletion integration, no user-facing memory
  commands, no embeddings/vector store, no model-driven extraction.
- **No `ConversationContextProvider` integration** — `InMemoryWorkingMemory`
  is injected directly into `ConversationEngine` via
  `WorkingMemoryReader`/`WorkingMemoryWriter`, never disguised as a
  `ConversationContextProvider` (no demonstrated type-compatibility
  ever existed between the two).
- **No session semantics.** `subject_key` is the only dimension; no
  session ID, no session lifecycle.

## Old chat drops out (owner, 2026-11-12)

`InMemoryWorkingMemory` takes an optional `clock` and `max_age`; exchanges older than that are left out of `read()`. `commit_exchange` takes an
optional `at` (the restored conversation passes each message's own time). The bot uses 36 hours (`WORKING_MEMORY_MAX_AGE` in `bot/discord_bot.py`).
