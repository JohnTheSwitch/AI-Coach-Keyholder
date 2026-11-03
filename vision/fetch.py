"""
vision/fetch.py

The in-memory image fetcher (v0.39 Phase H1). An image URL (from Chaster's `GET /files/{fileKey}`) is downloaded straight into a size-capped `bytearray`:
HTTPS only, TLS verification left at the library default (never disabled), no redirects, a hard byte cap checked on the declared length AND on every chunk,
and a magic-byte check (JPEG, PNG, WebP). Nothing is written to disk and the URL is never logged; errors carry a code only.

`wipe(buffer)` overwrites a buffer in place once it is no longer needed (bytes objects made from it elsewhere are immutable and can only be dropped).
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from vision.models import VisionError

__all__ = ["fetch_image", "check_image", "wipe", "MAX_IMAGE_BYTES"]

MAX_IMAGE_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0
_CHUNK = 64 * 1024


def check_image(buffer: bytearray | bytes) -> str:
    """The image kind from its first bytes ("jpeg", "png", "webp"); VisionError("not_an_image") otherwise."""
    head = bytes(buffer[:12])
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    raise VisionError("not_an_image")


def wipe(buffer: bytearray | None) -> None:
    if buffer is not None:
        buffer[:] = bytes(len(buffer))


def _requests_stream(url: str, *, timeout: tuple[float, float]):
    import requests

    # TLS verification is the library default and is never turned off here.
    return requests.get(url, stream=True, timeout=timeout, allow_redirects=False)


def fetch_image(url: str, *, max_bytes: int = MAX_IMAGE_BYTES, get: Callable = _requests_stream) -> bytearray:
    """Downloads one image into memory. Raises VisionError with a code on any problem (the caller leaves the item pending)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        raise VisionError("bad_url") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise VisionError("not_https")
    try:
        response = get(url, timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
    except Exception:
        raise VisionError("transport") from None
    buffer = bytearray()
    try:
        if 300 <= response.status_code < 400:
            raise VisionError("redirect")
        if response.status_code != 200:
            raise VisionError("http_error")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise VisionError("too_large")
            except ValueError:
                raise VisionError("bad_length") from None
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                if len(buffer) + len(chunk) > max_bytes:
                    raise VisionError("too_large")
                buffer.extend(chunk)
        except VisionError:
            raise
        except Exception:
            raise VisionError("transport") from None
        if not buffer:
            raise VisionError("empty")
        check_image(buffer)
        return buffer
    except VisionError:
        wipe(buffer)
        raise
    finally:
        try:
            response.close()
        except Exception:
            pass
