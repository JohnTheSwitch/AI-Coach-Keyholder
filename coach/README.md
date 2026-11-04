# coach — Phase J3

Canonical: `docs/architecture/phase_j_proposal.md`, section 5. Off by default (`ENABLE_COACH`).

The coach lets the local model propose goals and daily commitments in the Discord DM. The model proposes and the user decides (philosophy 2.5): nothing the
model writes changes anything, and only the user's own button applies a proposal.

```
model reply (JSON) -> the reply text goes to the user
                   -> `proposal` -> validate_action -> the gates -> a pending row -> a card -> the user's button -> GoalManager
```

## Files

- **`actions.py`** (pure): the closed catalogue, `propose_goal`, `adapt_goal` and `propose_commitment`, and `validate_action`, which rejects and never repairs. To
  add an action, add it here with a test. Nothing exists for a lock, time, a release, a token, a penalty, Trust, a rule, consent, a message or a task assignment.
- **`safety.py`** (pure): the content check. Regime words and anything that could hurt the user are refused, English and Czech.
- **`desk.py`**: the gates, the `coach_proposals` / `coach_refusals` / `daily_commitments` ledger (migration 046), the decision on a button press, and the
  context the model sees. It is the only way from a model request to `GoalManager`.

`system/coach_dm.py` holds what touches the channel: the engine's port, the cards, the buttons' door and `build_coach`. `bot/action_buttons.py::CoachButton` is
the persistent button. `conversation_engine/context_providers/coach_provider.py` passes a field whitelist into the prompt.

## The gates

Each refusal stores a reason code in `coach_refusals`, never the text. `python -m cli coach-status` counts them.

| Code | Meaning |
|---|---|
| `unknown_action`, `unknown_parameter`, `missing_parameter`, `invalid_*` | failed validation |
| `not_this_user` | the coach serves one wearer per database |
| `paused` | a vacation is in force |
| `safety_topic` | the user's message mentions pain, injury, distress or an emergency |
| `regime_topic`, `unsafe_content` | `safety.py` |
| `locked` | `adapt_goal` while the Iron Contract is in force; checked again at the button |
| `unknown_goal`, `too_many_goals`, `too_many_commitments` | a goal number must reach an active goal; at most 5 goals, 3 commitments a day |
| `other_kind_pending` | a counter-offer stays the kind it was |
| `daily_limit` | at most 3 proposals a local day |
| `declined_recently` | nothing similar to a declined proposal for 14 days |

## What it never does

- It reads no Trust state and imports no execution, release, penalty, Trust, rules or Iron Contract code (guard test). Locks and vacations arrive as injected callables.
- A commitment and its outcome have no consequence: they are not goal evidence, so no Trust, no tokens, nothing on a lock.
- An untrusted channel never reaches it, and multi-wearer tenants are not wired, because a tenant's invitation does not disclose it.

`forget all` erases proposals, refusals and commitments. Goals stay, as agreed records with an append-only history.

## Turning it on

`ENABLE_COACH=true`, a model that follows a JSON schema (`OLLAMA_MODEL`; `llama3.1` and `qwen3-vl` were tested), `OLLAMA_NUM_CTX=8192` or similar, a Trust domain
to label goals with (`CHECK_IN_TRUST_DOMAIN`), and a user (`CHECK_IN_USER_ID`, `CHASTER_EXECUTION_WEARER_USER_ID` or `OWNER_USER_ID`). The check-in negotiation also
needs `ENABLE_CHECK_INS`.
