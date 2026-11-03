# vision

v0.39 Phase H1, the core of the Zero-Trace Visual Proof Engine. Specification: [`docs/architecture/phase_h_proposal.md`](../docs/architecture/phase_h_proposal.md).
Nothing here polls Chaster or changes any state yet; that is H2 and H3.

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
