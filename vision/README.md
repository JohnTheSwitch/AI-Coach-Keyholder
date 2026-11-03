# vision

**Discontinued (2026-11-03): dormant code.** Photo validation is handled natively by Chaster Peer Verification; the bot only reads the resulting status.
Nothing outside this package, `scripts/eval_vision.py` and `tests/vision/` imports it, and it stays off while `VISION_OLLAMA_MODEL` is empty. The decision and
the probe results behind it: [`docs/architecture/phase_h_proposal.md`](../docs/architecture/phase_h_proposal.md), section 12.

v0.39 Phase H1, the core of the Zero-Trace Visual Proof Engine. Nothing here polls Chaster or changes any state; H2 to H4 will not be built.

| Module | Role |
|---|---|
| `fetch.py` | `fetch_image(url)`: downloads one image into a capped `bytearray` (8 MB). HTTPS only, TLS verification untouched, no redirects, the cap checked on the declared length and on every chunk, magic-byte check (JPEG, PNG, WebP). `wipe(buffer)` overwrites a buffer in place. |
| `adapter.py` | `VisionAdapter.evaluate(image, verification_type=, expected_code=)`: one call to the LOCAL Ollama `/api/chat` with the image in `images`, `format` = the schema, temperature 0. A non-local `OLLAMA_HOST` is refused. `build_vision_adapter(config)` returns None when `VISION_OLLAMA_MODEL` is empty (the feature is off). |
| `prompts.py` | fixed templates per verification type (`device_check`, `post_opening_check`, `task_proof`) and `RESPONSE_SCHEMA`: `verdict`, `reason_code` (enum), `code_seen`. The model is never shown the expected code and is never asked to describe anything. |
| `parser.py` | the strict parser: anything off-schema becomes `unclear` / `invalid_output`; `verified` stands only with reason `matches` AND a transcribed code that equals the Chaster code, otherwise it becomes `unclear` (`code_mismatch`, `no_expected_code`). |
| `models.py` | `Verdict`, `VerificationType`, `ReasonCode`, `VisionResult` (enums, a boolean, a latency), `VisionError` (a code only). |

**Zero-trace (tested).** No module here opens a file, uses `tempfile`, `shutil`, a database or `print`, saves an image or logs anything but literal text. The
tests check this statically (AST) and at runtime: every file and database opener raises while a full fetch and evaluation run. Copies that cannot be
wiped (the base64 bytes, the transport's buffers) are only dropped; see the specification, section 4.

**Errors decide nothing.** A `VisionError` (transport, redirect, size, not an image, model unavailable, bad response) leaves the item pending.

**Trying a model on your own photos:** `python -m scripts.eval_vision --type device_check --code ABC123 --expect verified photo1.jpg photo2.jpg`. The
script reads each photo into memory, prints one line per photo (verdict, reason code, code match, latency) and a summary, and writes nothing anywhere.
