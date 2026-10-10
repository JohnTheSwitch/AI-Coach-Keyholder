"""
chaster/_http.py

Internal helper -- narrow, Chaster-specific, not a general HTTP
framework. Every real Chaster HTTP call in this project must go
through `send_with_ordered_headers()` rather than calling
`requests.get()`/`requests.post()` directly.

WHY THIS EXISTS: `requests` merges its own default headers
(`User-Agent`, `Accept-Encoding`, `Accept`, `Connection` --
`requests.utils.default_headers()`) into a request's final header set
*first*; any caller-supplied header that isn't already one of those
defaults -- `Authorization`, in every Chaster call -- gets appended
*last*, regardless of the order the caller's own `headers=` dict was
written in. This is `requests`' own architecture (confirmed via
`Session.prepare_request()` / `merge_setting()`), not fixable through
the plain `headers=` keyword argument alone.

Chaster's own API/Cloudflare edge rejects requests with this default
ordering (`Authorization` last) -- confirmed by direct, real,
reproducible testing against the live endpoint: an otherwise-identical
request succeeds (HTTP 200) once `Authorization` and `User-Agent` are
moved to the front of the header sequence, and fails (HTTP 400)
otherwise. Every other header-*value* hypothesis (removing
`Accept-Encoding`, removing `Accept`, setting `Connection: close`) was
independently tested and ruled out first -- see
docs/architecture/chaster_integration_technical_design.md for the
full diagnostic history. Order, not presence or value, is the
confirmed differentiator.

This helper reorders rather than strips: every header `requests`
itself needs to add (e.g. `Content-Type`/`Content-Length` for a form
body) is preserved, only moved to come after `Authorization`/
`User-Agent` instead of before.
"""

from __future__ import annotations

import requests
from requests.structures import CaseInsensitiveDict

__all__ = ["send_with_ordered_headers"]


def send_with_ordered_headers(
    method: str, url: str, *,
    params: dict | None = None, data: dict | None = None, json: object | None = None,
    headers: dict | None = None, timeout: tuple[float, float] | float,
) -> requests.Response:
    """Sends a request through `requests`, with the final header set
    reordered so `Authorization` comes first and `User-Agent` comes
    second (when present) -- matching the real, confirmed-working
    order. Every other header `requests` itself prepares (including
    any body-related headers from `data`) is preserved unchanged,
    just moved after those two. Never touches TLS/certificate
    verification, retry behavior, or any setting beyond the header
    order itself."""
    session = requests.Session()
    request = requests.Request(method, url, params=params, data=data, json=json, headers=headers or {})
    prepared = session.prepare_request(request)

    ordered: CaseInsensitiveDict = CaseInsensitiveDict()
    for key in ("Authorization", "User-Agent"):
        if key in prepared.headers:
            ordered[key] = prepared.headers[key]
    for key, value in prepared.headers.items():
        if key not in ordered:
            ordered[key] = value
    prepared.headers = ordered

    return session.send(prepared, timeout=timeout)
