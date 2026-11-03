"""
vision/adapter.py

VisionAdapter (v0.39 Phase H1): sends ONE in-memory image to the LOCAL Ollama (`/api/chat`, the image in `images`, the answer constrained by
`format` = RESPONSE_SCHEMA, temperature 0, no streaming) and returns a VisionResult made of enums only. It is separate from the conversation engine: the
image never passes through ModelMessage, the prompt builder or Working Memory.

Zero-trace (docs/architecture/phase_h_proposal.md section 4): the request body is assembled in a `bytearray` around the base64 text and overwritten after the
call; the caller's image buffer is not modified here (the caller wipes it). Immutable copies made on the way (the base64 bytes, the transport's own
buffers) can only be dropped, not wiped. Nothing is logged except codes and numbers; a non-local OLLAMA_HOST is refused when the adapter is built.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Callable
from urllib.parse import urlparse

from vision.models import VerificationType, VisionError, VisionResult
from vision.parser import parse_verdict
from vision.prompts import RESPONSE_SCHEMA, SYSTEM_PROMPT, user_prompt

__all__ = ["VisionAdapter", "is_local_host", "build_vision_adapter", "MAX_RESPONSE_BYTES"]

MAX_RESPONSE_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0


def is_local_host(url: str) -> bool:
    """True when a model URL points at this machine (an image must never leave it)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _requests_post(url: str, *, body: bytearray, timeout: tuple[float, float]) -> tuple[int, bytes]:
    import requests

    # requests needs `bytes` here (a bytearray would be treated as an iterable stream); this copy can only be dropped, not wiped.
    response = requests.post(url, data=bytes(body), headers={"Content-Type": "application/json"}, timeout=timeout, stream=True,
                             allow_redirects=False)
    try:
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise VisionError("response_too_large")
        out = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            out.extend(chunk or b"")
            if len(out) > MAX_RESPONSE_BYTES:
                raise VisionError("response_too_large")
        return response.status_code, bytes(out)
    finally:
        response.close()


class VisionAdapter:
    def __init__(self, *, host: str, model: str, read_timeout_seconds: float = 180.0, keep_alive: str | None = None,
                 post: Callable[..., tuple[int, bytes]] = _requests_post, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not is_local_host(host):
            raise VisionError("host_not_local")
        if not model:
            raise VisionError("no_model")
        self._url = host.rstrip("/") + "/api/chat"
        self._model = model
        self._timeout = (CONNECT_TIMEOUT_SECONDS, float(read_timeout_seconds))
        self._keep_alive = keep_alive
        self._post = post
        self._monotonic = monotonic

    @property
    def model(self) -> str:
        return self._model

    def evaluate(self, image: bytearray | bytes, *, verification_type: VerificationType, expected_code: str | None,
                 task_text: str | None = None, require_code: bool = True) -> VisionResult:
        """One verdict. Raises VisionError on any system error (nothing is decided then); a usable answer always becomes a VisionResult."""
        body = self._body(image, user_prompt(verification_type, task_text=task_text))
        started = self._monotonic()
        try:
            try:
                status, payload = self._post(self._url, body=body, timeout=self._timeout)
            except VisionError:
                raise
            except Exception:
                raise VisionError("model_unavailable") from None
        finally:
            body[:] = bytes(len(body))                      # the request copy of the image, overwritten
        latency_ms = max(0, int((self._monotonic() - started) * 1000))
        if status != 200:
            raise VisionError("model_http_error")
        try:
            content = json.loads(payload)["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise VisionError("bad_response") from None
        verdict, reason, matched = parse_verdict(content, expected_code=expected_code, require_code=require_code)
        return VisionResult(verdict=verdict, reason_code=reason, code_matched=matched, latency_ms=latency_ms)

    def _body(self, image: bytearray | bytes, prompt: str) -> bytearray:
        """The JSON body with the base64 image spliced in once (no str copy of the image)."""
        payload = {
            "model": self._model, "stream": False, "format": RESPONSE_SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt, "images": ["IMAGE_SLOT"]}],
        }
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        head, tail = json.dumps(payload).encode("ascii").split(b'"IMAGE_SLOT"')
        body = bytearray(head)
        body += b'"'
        body += base64.b64encode(image)
        body += b'"'
        body += tail
        return body


def build_vision_adapter(config, *, read_timeout_seconds: float | None = None) -> VisionAdapter | None:
    """None when VISION_OLLAMA_MODEL is empty or unset (the feature is off). Raises VisionError("host_not_local") for a non-local OLLAMA_HOST."""
    model = (getattr(config, "vision_ollama_model", None) or "").strip()
    if not model:
        return None
    return VisionAdapter(host=config.ollama_host, model=model,
                         read_timeout_seconds=read_timeout_seconds or getattr(config, "vision_read_timeout_seconds", 180.0),
                         keep_alive=getattr(config, "ollama_keep_alive", None))
