# hive_mind_receiver

The standalone receiver for the bot's opt-in research telemetry (Phase G5; contract `hive-telemetry/1`, see
[`docs/architecture/phase_g_proposal.md`](../docs/architecture/phase_g_proposal.md), G4). It is a small FastAPI + SQLite service that shares **no code**
with the bot: it imports only the standard library, FastAPI/Pydantic/Starlette/Uvicorn and itself. A test in the bot's suite enforces this, and checks that
this contract matches exactly what the bot sends.

It treats every sender as untrusted, because other community instances may send to it.

## What it accepts

All requests need `Authorization: Bearer <token>`, `Content-Type: application/json` and a `Content-Length`.

| Endpoint | Body | Answer |
|---|---|---|
| `POST /v1/records` | `{"schema": "hive-telemetry/1", "instance": <64 hex>, "records": [1..200 records]}`; a record is `{"id": <32 hex>, "subject": <64 hex>, "event_type", "occurred_hour": "YYYY-MM-DDTHH:00:00Z", "metrics": {...}}` | `200 {"stored", "duplicates"}` |
| `POST /v1/erase` | `{"schema", "instance", "erase": {"id": <32 hex>, "subject": <64 hex>}}` | `200 {"erased", "already_done"}` |
| `GET /healthz` | (no token) | `200 {"status": "ok"}` |

`metrics` must have exactly the keys of its event type in [`contract.py`](contract.py): enum strings or null, finite numbers (up to 1e12) or booleans
or null. Anything else returns 422, and the error never echoes the input. One bad record refuses the whole batch.

**Protections:**
- **Before the body is read:**
  - an unknown or missing token gets 401
  - too many failed logins get 429 (one shared budget, `RECEIVER_AUTH_FAILURES_PER_MINUTE`, default 30)
  - each token has its own request budget (`RECEIVER_REQUESTS_PER_MINUTE`, default 120), with 429 and `Retry-After`
  - no `Content-Length` gets 411; more than `RECEIVER_MAX_BODY_BYTES` (default 512 KB, declared or actually sent) gets 413
  - a content type other than JSON gets 415
- **Idempotency:** records are keyed by (sender, id), so a retransmitted record is ignored and never overwritten.
- **Erase:** deletes every record of that subject sent with the same token; another sender can neither erase nor overwrite someone else's data. The erase
  id is remembered, so a retry answers `already_done`. Records sent later under the same subject, after a new opt-in, are accepted again.
- **Storage hygiene:** a sender is stored only as a 16-character SHA-256 prefix of its token. Records and erase receipts older than
  `RECEIVER_RETENTION_DAYS` (default 365) are deleted, checked at most once an hour.
- **Small surface:** no `/docs`, `/redoc` or `/openapi.json`, no `Server` header, and logs carry counts only.

The rate limits are in memory, so they apply per process. Run one process; that is plenty for this traffic.

## Run it

From the repository root, in its own virtual environment. It is not installed with the bot. Create the environment OUTSIDE the repository folder: the bot's repository-wide guard tests read every `.py` file under it. The examples use `~/hive-venv` (on Linux or macOS use `bin/python` instead of `Scripts/python`).

```bash
python -m venv ~/hive-venv
```

```bash
~/hive-venv/Scripts/python -m pip install -r hive_mind_receiver/requirements.txt
```

Settings are environment variables:

| Variable | Meaning |
|---|---|
| `TELEMETRY_TOKENS` | comma-separated bearer tokens, one per sending instance (remove one to revoke that sender) |
| `TELEMETRY_TOKEN` | a single token (added to the list; handy for your own instance) |
| `RECEIVER_DB_PATH` | the SQLite file (default `hive_mind.sqlite` in the working directory; better a path outside the repository, for example `~/hive/hive_mind.sqlite`) |
| `RECEIVER_HOST` / `RECEIVER_PORT` | default `127.0.0.1` / `8787` (local only: the tunnel connects locally) |
| `RECEIVER_MAX_BODY_BYTES`, `RECEIVER_REQUESTS_PER_MINUTE`, `RECEIVER_AUTH_FAILURES_PER_MINUTE`, `RECEIVER_RETENTION_DAYS` | limits (see above) |

Every token needs at least 24 characters. Create one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Start it (the tokens and the database path must be set in the environment):

```bash
~/hive-venv/Scripts/python -m hive_mind_receiver
```

or with uvicorn directly:

```bash
~/hive-venv/Scripts/python -m uvicorn --factory hive_mind_receiver.app:create_app_from_env --host 127.0.0.1 --port 8787
```

## Expose it with a free Cloudflare tunnel

The bot only sends to `https://` URLs, and Cloudflare provides the TLS. Install `cloudflared` (Cloudflare's tunnel client) on the receiver machine.

**Quick test** (no account; the random `*.trycloudflare.com` URL changes every time it starts):

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

**Stable setup** (free Cloudflare account and a domain on Cloudflare): `cloudflared tunnel login`, then `cloudflared tunnel create hive`, then
`cloudflared tunnel route dns hive hive.example.org`, then `cloudflared tunnel run --url http://127.0.0.1:8787 hive`. Nothing listens publicly on your
machine: the tunnel connects outwards.

On each sending bot, in its `.env`: `TELEMETRY_ENDPOINT=https://hive.example.org` (no path: the bot appends `/v1/records` and `/v1/erase`),
`TELEMETRY_TOKEN=<the token you issued for that instance>`, its own secret `TELEMETRY_HMAC_KEY`, and `ENABLE_RESEARCH_TELEMETRY=true`. Each wearer still
has to say `research on`.

## Tests

The tests use FastAPI's `TestClient`, which runs the app in process, so no network is used:

```bash
~/hive-venv/Scripts/python -m pip install -r hive_mind_receiver/requirements-dev.txt
```

```bash
~/hive-venv/Scripts/python -m pytest hive_mind_receiver/tests
```

The bot's own suite (`pytest` at the root) does not need FastAPI. It checks the isolation and the contract in `tests/hive_mind_receiver/`.
