#!/usr/bin/env python3
"""
scripts/dev_chaster_schema_probe.py

DEVELOPMENT-ONLY, MANUAL, LOCAL DIAGNOSTIC. Never imported by any
production code path (`bot/discord_bot.py`, `application/`,
`chaster/emergency_unlock*.py`, `chaster/callback_service.py`), never
run automatically, and never exposed through Discord, the emergency
listener, or Cloudflare.

Purpose: make read-only Chaster API calls -- GET /auth/profile and
GET /locks?status=active -- using a Chaster developer token you
provide interactively, so the real response shapes can be inspected
to resolve this project's schema blockers (historical: the identity
resolver and, since 2026-11-03, the LockForWearer fields are both
confirmed -- see `chaster/emergency_unlock_provider.py`).
This script does NOT modify either of those -- it only prints what
the real API actually returns, for a human to read and decide from.

Primary path reuses the existing, already-tested production helpers
exactly as instructed -- no parallel HTTP client for the happy path:
- chaster.oauth_client.ChasterOAuthClient.fetch_raw_profile()
- chaster.lock_client.ChasterLockClient.list_active_locks()

DIAGNOSTIC FALLBACK: both production helpers deliberately discard the
response body on a non-200 status (they only report the status code,
by design), which makes root-causing a real API error impossible
from their exception message alone. When a primary call fails, this
script makes its OWN, additional, still strictly read-only GET
request to the SAME confirmed endpoint/method to capture and print
the full (redacted) response for diagnosis -- never a write endpoint.

CURL-EQUIVALENT DIAGNOSTIC (added this turn): a real Windows
`curl.exe -H "Authorization: Bearer <token>" "https://api.chaster.app/locks?status=active"`
succeeded (HTTP 200) where our Python `requests`-based call returned
HTTP 400. Reconstructing the exact `requests.PreparedRequest` this
project's own production client sends (via `requests.Session.
prepare_request()`, without making a real call) shows Python's
`requests` library adds several headers by default that curl's own
minimal request does not send: `User-Agent: python-requests/<version>`
(vs. curl's own `User-Agent: curl/<version>`), `Accept-Encoding: gzip,
deflate`, and `Connection: keep-alive`. This is a CONFIRMED, real
difference (verified by direct inspection of the actual prepared
headers, not guessed) -- but NOT yet a confirmed root cause. This
script's `_print_diagnostic_response()` gained a `curl_equivalent`
mode that reproduces curl's exact minimal header set (`requests`
lets a header be fully removed by passing `None` as its value) for
`/locks?status=active` specifically, alongside the current default
variant, so the next real run settles this empirically. `/auth/profile`
is intentionally left untouched by this specific diagnostic pass, per
instruction: `/locks` is the current priority.

SECURITY, BY DESIGN:
- The token is read via `getpass.getpass()` -- input is not echoed to
  the terminal, and interactive `getpass` input is never touched by
  shell command history (unlike a command-line argument or an
  environment variable set inline on the same command line).
- The token is never written to a file, never logged, never included
  in any printed output, and never referenced in any exception or
  diagnostic message -- the diagnostic fallback below never prints
  the Authorization header's own value, only response-side data.
- Only GET requests are ever made -- this script contains no
  POST/PUT/PATCH/DELETE call of any kind, in particular never
  `/locks/{lockId}/unlock` or `/locks/{lockId}/emergency-unlock`.
- Response headers and bodies are redacted before printing: any JSON
  key whose name contains a sensitive-looking substring (token,
  secret, password, auth, email, key, credential -- case-insensitive,
  at any nesting depth) has its value replaced with `<redacted>`.
  This is a generic filter over whatever the API actually returns --
  it does not assume or guess Chaster's schema. A response body that
  is not valid JSON is never printed raw (it could, in principle,
  echo back something unexpected) -- only its length is reported.

USAGE (from the repository root, with your virtualenv active):

    python3 scripts/dev_chaster_schema_probe.py

You will be prompted for your developer token; nothing else is
required. The token exists only in local memory for the duration of
this one process and is discarded when it exits.
"""

from __future__ import annotations

import getpass
import hashlib
import http.client
import json
import socket
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from chaster.lock_client import LOCKS_URL, ChasterLockApiError, ChasterLockClient  # noqa: E402
from chaster.oauth_client import ChasterOAuthClient, ChasterTokenExchangeError  # noqa: E402
from core.config import Config  # noqa: E402

SENSITIVE_KEY_SUBSTRINGS = ("token", "secret", "password", "auth", "email", "key", "credential")
PROFILE_URL = "https://api.chaster.app/auth/profile"
_DIAGNOSTIC_TIMEOUT = (5.0, 15.0)


def redact(value: object) -> object:
    """Recursively redacts any dict value whose key name contains a
    sensitive-looking substring. Never inspects or assumes Chaster's
    schema -- applies the same generic rule to whatever real JSON
    structure is actually returned."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if any(s in k.lower() for s in SENSITIVE_KEY_SUBSTRINGS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _build_headers(access_token: str, *, style: str, client_id: str | None = None) -> dict[str, str | None]:
    """`style`:
    - "default": current production behavior -- only Authorization is
      explicitly set; `requests` adds its own defaults (User-Agent:
      python-requests/..., Accept-Encoding, Accept: */*, Connection).
    - "content_type": default plus an explicit Content-type header,
      matching Chaster's own official example page -- already tested
      empirically and found NOT to change the outcome (kept here only
      so a rerun can reconfirm this next to the new curl-equivalent
      variant, not because it's still considered a live hypothesis).
    - "curl_equivalent": reproduces curl's own minimal header set
      exactly (User-Agent: curl/8.14.1, Accept: */*, Authorization
      only) -- `requests` removes a default header entirely when its
      value is explicitly set to None.
    - "accept_json": IDENTICAL to "curl_equivalent" in every other
      respect (User-Agent, Accept-Encoding, Connection, Authorization
      mechanism all unchanged) -- isolates exactly one variable, per
      a Chaster developer's own suggestion in their #developers
      channel: `Accept: */*` -> `Accept: application/json`. Derived
      from the curl_equivalent header set itself (never duplicated)
      so the two variants can never silently drift apart.
    - "client_id": IDENTICAL to "curl_equivalent" except it ALSO adds
      `X-Chaster-Client-Id: <client_id>` -- per a Chaster developer's
      own confirmed working Go example (their #developers channel),
      which sends exactly `X-Chaster-Client-Id` alongside
      `Authorization: Bearer <token>` for developer-token requests.
      Isolates exactly one variable versus "curl_equivalent": the
      presence of this one header. Requires `client_id` to be passed;
      raises if it is missing, since a silent no-op variant here would
      be misleading, not a genuine test of the hypothesis.
    - "client_id_accept_json": "client_id" plus `Accept:
      application/json` together -- a supplementary combined variant
      only, never the primary evidence for either hypothesis on its
      own (each hypothesis is only actually isolated by "client_id"
      and "accept_json" individually)."""
    if style in ("curl_equivalent", "accept_json", "client_id", "client_id_accept_json"):
        headers: dict[str, str | None] = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "curl/8.14.1",
            "Accept": "*/*",
            "Accept-Encoding": None,
            "Connection": None,
        }
        if style in ("accept_json", "client_id_accept_json"):
            headers["Accept"] = "application/json"
        if style in ("client_id", "client_id_accept_json"):
            if not client_id:
                raise ValueError(f"style={style!r} requires a client_id (CHASTER_CLIENT_ID) to be configured.")
            headers["X-Chaster-Client-Id"] = client_id
        return headers
    headers = {"Authorization": f"Bearer {access_token}"}
    if style == "content_type":
        headers["Content-type"] = "application/json"
    return headers


def _print_diagnostic_response(label: str, url: str, *, params: dict | None, access_token: str, header_style: str, client_id: str | None = None) -> None:
    """Strictly read-only (GET only). Prints status, a redacted
    subset of response headers, and a redacted JSON body (or a safe
    "(not JSON, length=N)" note if the body cannot be parsed as JSON
    -- the raw text is never printed, since it cannot be safely
    key-redacted). Never prints the Authorization header's own value,
    and never prints the client_id value either (CHASTER_CLIENT_ID is
    not a secret like a token, but there is no need to print it)."""
    headers = _build_headers(access_token, style=header_style, client_id=client_id)
    print(f"  [diagnostic: {label}]")
    try:
        response = requests.get(url, params=params, headers=headers, timeout=_DIAGNOSTIC_TIMEOUT)
    except requests.RequestException as exc:
        print(f"    network error: {exc}")
        return
    print(f"    HTTP status: {response.status_code}")
    print(f"    Content-Type: {response.headers.get('Content-Type', '<none>')}")
    safe_headers = {
        k: v for k, v in response.headers.items()
        if not any(s in k.lower() for s in SENSITIVE_KEY_SUBSTRINGS)
    }
    print(f"    response headers (redacted): {safe_headers}")
    try:
        body = response.json()
        print(f"    response body (redacted): {json.dumps(redact(body), indent=2, ensure_ascii=False)}")
    except ValueError:
        print(f"    response body is not valid JSON (length={len(response.content)} bytes) -- not printed for safety.")


def _print_self_identity() -> None:
    """Prints this script's own SHA-256 (of its source file on disk)
    and line count at startup. Exists specifically to make it
    trivially verifiable, from the printed output itself, whether the
    file actually being executed matches a known-good version --
    added after a real discrepancy where a Windows run's output did
    not match the current source, and there was no way to tell from
    the output alone which version had actually run. Reads only this
    script's own file; touches nothing else."""
    self_path = Path(__file__).resolve()
    content = self_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    line_count = content.count(b"\n") + (0 if content.endswith(b"\n") else 1)
    print(f"Script identity: {self_path.name}  sha256={digest}  lines={line_count}")


def _test_bare_httpclient_transport(label: str, *, host: str, path: str, access_token: str) -> None:
    """Isolation experiment: makes the exact same GET request using
    Python's stdlib `http.client.HTTPSConnection` DIRECTLY --
    completely bypassing `requests` and `urllib3`. Confirmed by direct
    inspection of CPython 3.12's own `http.client._create_https_context()`:
    like `urllib3.util.ssl_.create_urllib3_context()`, it explicitly
    calls `context.set_alpn_protocols(['http/1.1'])` on top of
    `ssl._create_default_https_context()` -- so this is a genuinely
    different code path (no urllib3/requests involved at all) but a
    similarly-shaped TLS context, not a wildly different one. If this
    ALSO returns HTTP 400, the cause is general to Python/OpenSSL, not
    specific to `requests`/`urllib3`'s own configuration. If it
    returns 200, that specifically implicates something `requests`/
    `urllib3` does differently. Strictly GET-only; never used for any
    write endpoint. Never logs the access token."""
    print(f"  [diagnostic: {label}]")
    conn = http.client.HTTPSConnection(host, timeout=15.0)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {access_token}"})
        response = conn.getresponse()
        body = response.read()
        print(f"    HTTP status: {response.status}")
        safe_headers = {
            k: v for k, v in response.getheaders()
            if not any(s in k.lower() for s in SENSITIVE_KEY_SUBSTRINGS)
        }
        print(f"    response headers (redacted): {safe_headers}")
        try:
            parsed = json.loads(body)
            print(f"    response body (redacted): {json.dumps(redact(parsed), indent=2, ensure_ascii=False)}")
        except ValueError:
            print(f"    response body is not valid JSON (length={len(body)} bytes) -- not printed for safety.")
    except OSError as exc:
        print(f"    network error: {exc}")
    finally:
        conn.close()


def _print_tls_handshake_metadata(host: str, *, port: int = 443) -> None:
    """DEV-ONLY, TLS-HANDSHAKE-ONLY diagnostic -- completes a TLS
    handshake to `host:port` with SNI=`host`, using the exact same
    context construction `http.client`'s own `_create_https_context()`
    uses (`ssl.create_default_context()` + `set_alpn_protocols(['http/1.1'])`
    -- confirmed identical to what this project's own bare-http.client
    isolation test already uses). Sends NO HTTP request at all -- not
    one byte is written to the socket beyond the TLS handshake itself
    -- so this needs NO access token and touches no application-layer
    data whatsoever. Prints only safe, non-secret metadata: negotiated
    TLS version, negotiated cipher, negotiated ALPN protocol, and the
    peer certificate's decoded subject/issuer/validity fields (never
    the raw certificate blob, never any private key material -- none
    is ever accessed, since no client certificate is used)."""
    print(f"  [TLS handshake only -- no HTTP request sent, no token needed: {host}:{port}]")
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    try:
        with socket.create_connection((host, port), timeout=10.0) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                print(f"    Negotiated TLS version: {tls_sock.version()}")
                print(f"    Negotiated cipher: {tls_sock.cipher()}")
                print(f"    Negotiated ALPN protocol: {tls_sock.selected_alpn_protocol()}")
                cert = tls_sock.getpeercert()
                if cert:
                    print(f"    Peer certificate subject: {cert.get('subject')}")
                    print(f"    Peer certificate issuer: {cert.get('issuer')}")
                    print(f"    Peer certificate notAfter: {cert.get('notAfter')}")
                else:
                    print("    Peer certificate: not available (cert verification may be configured differently).")
    except OSError as exc:
        print(f"    TLS handshake failed: {exc}")


def main() -> None:
    print("=" * 78)
    print("DEVELOPMENT-ONLY Chaster schema probe.")
    _print_self_identity()
    print("Makes only real, read-only GET requests to:")
    print("  GET https://api.chaster.app/auth/profile")
    print("  GET https://api.chaster.app/locks?status=active")
    print("Never makes any unlock/write request of any kind.")
    print("Your token is never echoed, logged, written to a file, or printed.")
    print("=" * 78)
    print("\nStep 1 -- TLS handshake only (no HTTP request, no token needed):")
    _print_tls_handshake_metadata("api.chaster.app")
    print("\nStep 2 -- authenticated GET requests (token required from here on):")
    token = getpass.getpass("\nEnter your Chaster developer token (input hidden, not stored): ").strip()
    if not token:
        print("No token entered -- aborting. No request was made.")
        return

    # CHASTER_CLIENT_ID is read from the same local .env-backed Config
    # this project already uses -- never printed (not a secret like
    # the token, but no need to display it either).
    client_id = Config.load().chaster_client_id

    # fetch_raw_profile() uses no instance state at all -- these
    # placeholder values are never read; see chaster/oauth_client.py.
    oauth_client = ChasterOAuthClient(client_id="dev-probe", client_secret="dev-probe", redirect_uri="https://example.invalid/unused")
    lock_client = ChasterLockClient()

    print("\n--- GET /auth/profile (raw response, sensitive keys redacted) ---")
    try:
        profile = oauth_client.fetch_raw_profile(access_token=token)
        print(json.dumps(redact(profile), indent=2, default=str, ensure_ascii=False))
    except ChasterTokenExchangeError as exc:
        print(f"Profile request failed: {exc}")
        print("(Not running further /auth/profile diagnostics this pass -- /locks is the current priority.)")

    print("\n--- GET /locks?status=active (raw response, sensitive keys redacted) ---")
    try:
        locks = lock_client.list_active_locks(access_token=token)
        print(json.dumps(redact(locks), indent=2, default=str, ensure_ascii=False))
    except ChasterLockApiError as exc:
        print(f"Locks request failed: {exc}")
        print("Running additional read-only diagnostics for this failure...")
        _print_diagnostic_response("current headers (default python-requests)", LOCKS_URL, params={"status": "active"}, access_token=token, header_style="default")
        _print_diagnostic_response("curl-equivalent headers (matches the known-working curl.exe request)", LOCKS_URL, params={"status": "active"}, access_token=token, header_style="curl_equivalent")
        if client_id:
            _print_diagnostic_response("curl-equivalent + X-Chaster-Client-Id (per Chaster developer's confirmed working example)", LOCKS_URL, params={"status": "active"}, access_token=token, header_style="client_id", client_id=client_id)
        else:
            print("  [diagnostic: curl-equivalent + X-Chaster-Client-Id] SKIPPED -- CHASTER_CLIENT_ID is not configured in .env.")
        _print_diagnostic_response("curl-equivalent + Accept: application/json (per Chaster developer suggestion)", LOCKS_URL, params={"status": "active"}, access_token=token, header_style="accept_json")
        if client_id:
            _print_diagnostic_response("curl-equivalent + X-Chaster-Client-Id + Accept: application/json (supplementary combined variant)", LOCKS_URL, params={"status": "active"}, access_token=token, header_style="client_id_accept_json", client_id=client_id)
        _test_bare_httpclient_transport(
            "bare stdlib http.client (bypasses requests/urllib3 entirely)",
            host="api.chaster.app", path="/locks?status=active", access_token=token,
        )

    print("\nDone. No unlock request was made. The token above is not stored anywhere.")


if __name__ == "__main__":
    main()
