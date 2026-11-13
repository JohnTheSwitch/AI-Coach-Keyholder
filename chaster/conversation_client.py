"""
chaster/conversation_client.py

ChasterConversationClient (v0.39 dual-role, Phase C): the three Chaster chat operations, confirmed against the OpenAPI document
(api.chaster.app/api-json, fetched 2026-10-10). OAuth scope `messaging` for all of them.

- POST /conversations/{conversationId}   body {message}                      -> 201 MessageForPublic   (send)
- GET  /conversations/{conversationId}/messages?limit&lastId                 -> 200 {results[], hasMore} (poll)
- GET  /conversations/by-user/{userId}                                       -> 200 ConversationForPublic (find)

NOT specified by the document (so nothing is assumed): the ordering of `results`, the maximum message length, and what
`lastId` pages over. The poller therefore never relies on ordering. Never logs or raises with the access token.
"""

from __future__ import annotations

from urllib.parse import quote

import requests

from chaster import CHASTER_USER_AGENT
from chaster._http import send_with_ordered_headers
from chaster.lock_client import DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS, ChasterLockApiError

__all__ = ["ChasterConversationClient", "CONVERSATIONS_URL", "MESSAGING_SCOPE", "MAX_POLL_LIMIT"]

CONVERSATIONS_URL = "https://api.chaster.app/conversations"
MESSAGING_SCOPE = "messaging"
MAX_POLL_LIMIT = 100                       # the documented maximum for `limit`


def _path_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return quote(value.strip(), safe="")


class ChasterConversationClient:
    def send_message(self, *, access_token: str, conversation_id: str, message: str) -> dict:
        """Returns the created MessageForPublic as a raw dict. Raises ChasterLockApiError (a subclass-compatible error that the
        token manager understands: status 401 triggers one refresh) on any failure."""
        self._check_token(access_token)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string.")
        body = self._call("POST", f"{CONVERSATIONS_URL}/{_path_id(conversation_id, 'conversation_id')}",
                          access_token, json={"message": message}, ok=(200, 201))
        if not isinstance(body, dict):
            raise ChasterLockApiError("Chaster's send-message endpoint returned an unexpected response shape.")
        return body

    def list_messages(self, *, access_token: str, conversation_id: str, limit: int = 50) -> list[dict]:
        """The newest page Chaster returns (no `lastId`), as raw dicts. Order is NOT assumed."""
        self._check_token(access_token)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_POLL_LIMIT:
            raise ValueError(f"limit must be an int between 1 and {MAX_POLL_LIMIT}.")
        body = self._call("GET", f"{CONVERSATIONS_URL}/{_path_id(conversation_id, 'conversation_id')}/messages",
                          access_token, params={"limit": limit}, ok=(200,))
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise ChasterLockApiError("Chaster's messages endpoint returned an unexpected response shape.")
        return [item for item in results if isinstance(item, dict)]

    def list_conversations(self, *, access_token: str, status: str = "approved", limit: int = 50) -> list[dict]:
        """Owner, 2026-11-13: the account's conversations, newest first by `lastMessageAt` (`GET /conversations`, params limit 1-100,
        status pending|approved|ignored; GetConversationsRepDto {results, hasMore}). One page only: the inbox watch needs the recent ones."""
        if status not in ("pending", "approved", "ignored"):
            raise ValueError("status must be pending, approved or ignored.")
        body = self._call("GET", CONVERSATIONS_URL, access_token, params={"limit": max(1, min(int(limit), MAX_POLL_LIMIT)), "status": status}, ok=(200,))
        results = body.get("results") if isinstance(body, dict) else None
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    def find_conversation_by_user(self, *, access_token: str, chaster_user_id: str) -> dict | None:
        """The existing conversation with that Chaster user, or None when there is none (404)."""
        self._check_token(access_token)
        try:
            body = self._call("GET", f"{CONVERSATIONS_URL}/by-user/{_path_id(chaster_user_id, 'chaster_user_id')}",
                              access_token, ok=(200,))
        except ChasterLockApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        if body is None:
            return None
        if not isinstance(body, dict) or not isinstance(body.get("_id"), str):
            raise ChasterLockApiError("Chaster's conversation lookup returned an unexpected response shape.")
        return body

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _check_token(access_token: str) -> None:
        if not access_token or not access_token.strip():
            raise ValueError("access_token must be a non-empty string.")

    @staticmethod
    def _call(method: str, url: str, access_token: str, *, ok: tuple[int, ...], params: dict | None = None,
              json: object | None = None):
        try:
            response = send_with_ordered_headers(
                method, url, params=params, json=json,
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": CHASTER_USER_AGENT},
                timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ChasterLockApiError("Network error contacting Chaster's conversations endpoint.") from exc
        if response.status_code not in ok:
            raise ChasterLockApiError(
                f"Chaster's conversations endpoint returned HTTP {response.status_code}.", status_code=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            if response.status_code == 204:
                return None
            raise ChasterLockApiError("Chaster's conversations endpoint returned a response this application could not parse.") from exc
