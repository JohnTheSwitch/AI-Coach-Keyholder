# Phase H: the Zero-Trace Visual Proof Engine

Status: H0 (specification, binding) and H1 (the vision core, `vision/`, section 11) built. Nothing polls Chaster yet. Sections 2 and 3 record what the Chaster API actually allows, verified against the live OpenAPI
document (fetched 2026-11-03 from `api.chaster.app/api-json`, Chaster API version 0.35.0, 181 paths). Live behaviour still to be confirmed with a read-only
probe once the keyholder token has the `keyholder` scope is listed in section 9.

## 1. Owner decisions (2026-11-03)

1. "Confirm, but never convict": a `verified` verdict may act automatically; `not_verified` only reports an **unconfirmed** Incident for the owner's
   review and never penalises by itself.
2. No automatic inflation bump and no token deduction for vision tasks. Incident rules apply only after the owner confirms the Incident.
3. Wearer messages after an AI verdict are neutral. A harsher register belongs to the Incident flow after the owner confirmed it (still bound by guardrail
   rule 6 "never cruel" and philosophy 2.19, never while the wearer is struggling); that flow is not part of Phase H.
4. No perceptual hash or any other derived image data is stored; the Chaster verification code is the anti-reuse check.
5. `VISION_OLLAMA_MODEL=qwen3-vl:8b-instruct`; the feature is off when the variable is empty or unset.

## 2. What the Chaster API offers (verified)

| Operation | Scope | What it does |
|---|---|---|
| `GET /locks/{lockId}/verification-pictures` (`VerificationPictureController_getVerificationPictures`) | `locks` | The HISTORY of verification pictures: per entry `imageKey`, `verificationCode` (nullable), `submittedAt`, and optionally `peerVerification` (`status`: `ongoing` / `verified` / `rejected`, vote counts, assigned reviewers). |
| `POST /extensions/verification-picture/{lockId}/request` | `locks` | Requests a picture (201; 400 when one is already pending; 404 when the extension is not on the lock). |
| `POST /extensions/verification-picture/{lockId}/submit` | `locks` | The wearer's upload (only after a request). |
| `GET /extensions/tasks/{lockId}/history` | `locks` | Task history: `status` in `verified`, `pending_verification`, `abandoned`, `rejected`, `completed`, `removed`; optional `peerVerification` whose `TaskCheck` carries the task text and the `code` expected in the photo. |
| `POST /extensions/tasks/{lockId}/complete-task` | `locks` | `{isCompleted, verificationPictureToken}`: the wearer completing a task with a picture. |
| `POST /extensions/temporary-opening/{lockId}/request-verification-picture` | `locks` | A picture after a temporary (hygiene) opening. |
| `GET /files/{fileKey}` (`StorageController_getFileFromKey`) | any OAuth | Returns `{url}` for an `imageKey`: the image itself is fetched from that URL in a second request. |
| `POST /locks/{lockId}/extensions/{extensionId}/action` | any OAuth | A generic extension action `{action, payload}`; the action names are NOT documented ("refer to the extension documentation"). |
| Partner Extensions API (`/api/extensions/sessions/{sessionId}/...`: `tasks/complete` with `actor` and `isCompleted`, `peer-verifications`, `verification-picture/request`, `files/{fileId}` with `url`, `size`, `expiresAt`) | `developer` | Only for a registered Chaster extension installed on the lock. |

## 3. Consequences for the blueprint

- **Polling is possible**: the picture history and the task history are readable with the `locks` scope; pending items are history entries without a
  final peer-verification status, or tasks in `pending_verification`.
- **The verification code is available** (`verificationCode` on a picture, `TaskCheck.code` on a task): the anti-reuse check of decision 4 is feasible.
- **There is NO public keyholder "approve" or "reject" call** for verification pictures or tasks. Decisions on Chaster are made by peer verification
  (votes in the Chaster app); there is no vote endpoint for peer verifications in the public API.
- The only documented programmatic decision is the Partner Extensions API (`tasks/complete` with `actor: "keyholder"` or `"extension"`), which needs a
  registered Chaster developer extension and the `developer` scope. The generic `extensions/{id}/action` endpoint has undocumented action names; by the
  project rule (nothing is inferred) it is not used.

**Design decision for Phase H: verify, then act inside our own engine.** The bot reads pictures, evaluates them, records the outcome in its own database
and Trust/goal pipeline, and informs the owner. It does not write a verdict to Chaster. A Chaster write-back is a separate later phase (a Partner
Extension), not part of H.

## 4. Zero-trace: what is promised

Promised and tested: no file, database row, log line or Discord message ever contains the image, its base64 form, its download URL or the model's free-text
reason. The image is streamed into a size-capped `bytearray`, sent to the local Ollama, then overwritten and dropped.

Not promised (stated to the wearer and the owner): Chaster stores the picture on its servers, and its visibility depends on the wearer's Chaster settings
and peer verification; CPython cannot wipe immutable copies (`bytes`, the base64 `str`, the JSON body) from memory, it only frees them; the OS page file,
hibernation file and crash dumps of `python.exe` or `ollama.exe` may hold process memory (runbook: disable crash dumps for both, keep `OLLAMA_DEBUG`
off).

Guards: an AST test bans `open` in write modes, `Path.write_*`, `os.open`, `tempfile`, `shutil`, image `.save(` and logging of buffer variables in the vision
package; a runtime test makes every file and database opener raise while an evaluation runs; error paths carry codes only.

## 5. Pipeline

1. **Poller** (its own background task, like `research_export_loop`; never the periodic cycle, never the reply path). Per paired lock with an opted-in
   wearer: one history call per poll interval (default 5 minutes, staggered across tenants) on the `ApiBudget` background lane; on HTTP 429 the
   `Retry-After` is honoured with exponential backoff and one warning per outage. At most one picture per tenant per pass.
2. **Selection**: a picture is evaluated once (UNIQUE Chaster image key per tenant database), only if it is pending, at most N hours old, and of a known
   verification type (section 6). Older or unknown ones go to the owner as "manual review" without evaluation.
3. **Fetch**: `GET /files/{imageKey}` (budget), then the returned URL: HTTPS only, TLS verification on, no redirects (to be confirmed live), streamed with a
   hard cap (8 MB), magic-byte check (JPEG, PNG, WebP). The URL is never logged or stored.
4. **Evaluate** (`VisionAdapter`, separate from the conversation engine): local Ollama only (non-local `OLLAMA_HOST` refused), `/api/chat` with the image
   in `images`, `format` = the JSON schema, `temperature: 0`, `stream: false`, read timeout about 180 s, bounded response. Waits while a chat reply ran in
   the last minute (G2 buffer) so live replies keep priority.
5. **Parse** strictly: `{"verdict": "verified" | "not_verified" | "unclear", "reason_code": <enum>, "code_seen": <string or null>}`. Anything else is
   `unclear`. `verified` additionally requires `code_seen` to match the Chaster verification code (normalised); otherwise it becomes `unclear`.
6. **Act** (section 7), **record** (verdict, reason code, model, latency, never the reason text), **clear** the buffer.

On any system error (Chaster, Ollama, timeout, parse) nothing is decided: the picture stays pending and is retried later; after N attempts it goes to the
owner for manual review.

## 6. Verification types (philosophy 2.10)

| Type | Source | Kind | Prompt asks |
|---|---|---|---|
| `device_check` | verification-picture history | rule compliance | the chastity device is worn and the code is visible |
| `post_opening_check` | temporary-opening picture | rule compliance | the device is back on after a hygiene opening, code visible |
| `task_proof` | task in `pending_verification` | goal / task evidence | the task text (from `TaskCheck.task`) is shown done, code visible |

The prompt is built from a fixed template per type; task text from Chaster is inserted as quoted data. A `task_proof` failure is a goal outcome, never an
Incident (2.10). Only rule-compliance types can lead to an Incident.

## 7. Consequences inside the engine

| Verdict | `task_proof` | `device_check` / `post_opening_check` |
|---|---|---|
| `verified` | task evidence `met` (feeds the daily pool through the normal goal path; no direct tokens) | positive verification evidence (Trust evidence through the normal path) |
| `not_verified` | task evidence `not met`, owner informed | an UNCONFIRMED Incident report for the owner's review (never confirmed by the system, 2.12) |
| `unclear` | nothing; owner informed | nothing; owner informed |

Never: an automatic penalty, a Progressive Conditioning change, a token debit, a Chaster write, or a mocking message. Wearer messages are neutral
("Your photo was checked and passed." / "The photo could not be confirmed; your keyholder will review it."). The owner gets a DM with type, verdict, reason
code and lock, never the image or the URL.

## 8. Consent and isolation

- New processing purpose (2.5): AI analysis of the wearer's verification photos. Disclosed in the invitation (tenants are covered only after accepting the
  disclosing text) and an explicit wearer opt-in `vision on|off|status`, default off; `vision off` stops evaluation at once. `forget all` also erases the
  stored verdict rows.
- Multi-tenant: pictures only from the tenant's confirmed paired lock (Phase E4); verdict rows only in that tenant's database; the platform database holds
  nothing.

## 9. To confirm live (read-only probe, after the keyholder token has the `keyholder` scope)

1. The keyholder token can read `GET /locks/{lockId}/verification-pictures` and `GET /extensions/tasks/{lockId}/history` for a managed lock.
2. How a pending picture looks (no `peerVerification`, or `status: ongoing`), and whether `verificationCode` is filled.
3. The host of the `GET /files/{fileKey}` URL, whether it needs authentication, redirects, expiry and content type.
The probe prints field names, statuses and the URL host only, never the URL or the image, and downloads nothing.

## 10. Steps

| Step | Content | Live effect |
|---|---|---|
| H0 | this specification | none |
| H1 | vision core: in-memory fetcher, `VisionAdapter`, strict parser with the code check, disk-ban guards (static and runtime), local-host rule, `VISION_OLLAMA_MODEL` (off when empty); an evaluation script that reads owner-provided sample images into memory only | none |
| H2 | the read-only probe of section 9, then the poller in its own task with `ApiBudget`, the verdict table (migration 042), the wearer opt-in and consent text; shadow mode: verdicts recorded and sent to the owner only | reads only |
| H3 | consequences of section 7 inside the engine, neutral wearer messages | engine only |
| H4 | hardening: injection images, numeric telemetry (verdict and latency only), cross-tenant audit, runbook (crash dumps, `OLLAMA_DEBUG`, Chaster visibility settings) | none |
| later | Chaster write-back through a registered Partner Extension (separate decision) | live |

## 11. H1 as built (2026-11-03)

- `vision/fetch.py`, `vision/adapter.py`, `vision/prompts.py`, `vision/parser.py`, `vision/models.py`; see `vision/README.md`.
- The schema has no free-text field at all: `verdict`, `reason_code` (enum), `code_seen` (at most 32 characters). The expected code is never given to the
  model; `verified` needs reason `matches` and a transcribed code equal to the Chaster code, otherwise it becomes `unclear`.
- The request body is one `bytearray` with the base64 image spliced in (no `str` copy of the image) and is overwritten after the call. `requests` needs a
  `bytes` copy of it, which, like the base64 bytes, can only be dropped.
- Guards: an AST ban (no `open`, `print`, logging, `tempfile`, `shutil`, databases, image `.save`, directory creation, nor imports of the chat path in
  `vision/`), and a runtime test that runs a full fetch and evaluation while every file and database opener raises. Nothing outside `vision/`, the
  evaluation script and tests imports the package yet.
- `VISION_OLLAMA_MODEL` (empty = off) and `VISION_READ_TIMEOUT_SECONDS` (180). A non-local `OLLAMA_HOST` is refused.
- `python -m scripts.eval_vision`: the owner's offline accuracy check on local sample photos (memory only, nothing written).
- Live smoke test on this machine with a synthetic gray image: `qwen3-vl:8b-instruct` accepted the image and the schema and answered `unclear` /
  `image_unclear` for both a device check and a task proof (about 80 s including loading the model, about 8 s warm).

