"""Owner, 2026-11-13: nobody reads the keyholder account, so new Chaster messages to it are forwarded to the owner's DM, once. Forward only:
nothing is ever sent from the account. A real database; fake Chaster reads; no network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from infrastructure.database import Database as CoreDatabase
from messaging.keyholder_inbox import EXCERPT, KeyholderInboxWatch

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
OWN, OWNER, STRANGER = "kh-account", "owner-account", "stranger"


def conversation(cid, message_id, sender=STRANGER, text="hi, please release me", status="approved"):
    return {"_id": cid, "status": status, "users": [{"_id": OWN, "username": "AI_Coach_Keyholder"}, {"_id": sender, "username": f"name-{sender}"}],
            "lastMessage": {"_id": message_id, "user": sender, "message": text}}


class Tokens:
    def call_with_token(self, user, *, now, operation):
        return operation("token")


class Client:
    def __init__(self):
        self.by_status = {"pending": [], "approved": []}
        self.sent = []
        self.fail = False

    def list_conversations(self, *, access_token, status="approved", limit=50):
        if self.fail:
            raise RuntimeError("down")
        return list(self.by_status[status])

    def send_message(self, **_kw):              # must never be called
        self.sent.append(_kw)


@pytest.fixture
def world(tmp_path):
    core = CoreDatabase(tmp_path / "t.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    client, told = Client(), []
    watch = KeyholderInboxWatch(core, tokens=Tokens(), client=client, keyholder_user_id="kh-user", own_account_id=lambda: OWN, owner_account_id=OWNER,
                                notify=lambda key, text: told.append((key, text)))
    return watch, client, told


class TestTheInbox:
    def test_the_first_pass_is_the_baseline(self, world) -> None:
        watch, client, told = world
        client.by_status["approved"] = [conversation("c1", "m1")]
        assert watch.run(NOW) == 0 and told == []

    def test_an_empty_first_pass_is_still_the_baseline(self, world) -> None:
        watch, client, told = world
        watch.run(NOW)
        client.by_status["pending"] = [conversation("c1", "m1", status="pending")]
        assert watch.run(NOW) == 1 and "(a message request)" in told[0][1] and "name-stranger" in told[0][1]

    def test_a_new_message_is_forwarded_once(self, world) -> None:
        watch, client, told = world
        watch.run(NOW)
        client.by_status["approved"] = [conversation("c1", "m1")]
        watch.run(NOW)
        watch.run(NOW)
        assert len(told) == 1 and told[0][0] == "keyholder_inbox:m1" and "please release me" in told[0][1]
        client.by_status["approved"] = [conversation("c1", "m2", text="still waiting")]
        watch.run(NOW)
        assert len(told) == 2 and "still waiting" in told[1][1]

    @pytest.mark.parametrize("sender", [OWN, OWNER])
    def test_the_account_itself_and_the_owner_are_not_forwarded(self, world, sender) -> None:
        watch, client, told = world
        watch.run(NOW)
        client.by_status["approved"] = [conversation("c1", "m1", sender=sender)]
        assert watch.run(NOW) == 0 and told == []

    def test_a_long_message_is_cut(self, world) -> None:
        watch, client, told = world
        watch.run(NOW)
        client.by_status["approved"] = [conversation("c1", "m1", text="x" * (EXCERPT + 50))]
        watch.run(NOW)
        assert ("x" * EXCERPT + "...") in told[0][1] and ("x" * (EXCERPT + 1)) not in told[0][1]

    def test_nothing_is_ever_sent_and_a_failed_read_raises(self, world) -> None:
        watch, client, told = world
        client.fail = True
        with pytest.raises(RuntimeError):
            watch.run(NOW)
        assert client.sent == []

    def test_the_bot_wires_it_forward_only(self) -> None:
        text = (ROOT / "bot" / "discord_bot.py").read_text(encoding="utf-8")
        assert "KeyholderInboxWatch(" in text and "bot.keyholder_inbox = keyholder_inbox_coro" in text
        assert "send_message" not in (ROOT / "messaging" / "keyholder_inbox.py").read_text(encoding="utf-8")
