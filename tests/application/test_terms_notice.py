"""The simplified consent: an informational notice before locking, with a link to the repository. No acceptance is asked for."""

from __future__ import annotations

from pathlib import Path

from application.service import ApplicationService
from application.terms_notice import REPOSITORY_URL, TERMS_NOTICE
from chaster_execution.review_consent import NOTICE_TEXT
from chaster_execution.review_consent import REPOSITORY_URL as CONSENT_REPOSITORY_URL
from tests.application.test_lock_and_task_commands import FIXED_TIME, _incoming, core  # noqa: F401

ROOT = Path(__file__).parent.parent.parent


def test_the_notice_says_proceeding_implies_agreement_and_links_the_repository() -> None:
    assert "by proceeding you agree" in TERMS_NOTICE and REPOSITORY_URL in TERMS_NOTICE
    assert REPOSITORY_URL == CONSENT_REPOSITORY_URL == "https://github.com/JohnTheSwitch/AI-Coach-Keyholder" and REPOSITORY_URL in NOTICE_TEXT


def test_the_way_out_is_part_of_the_notice() -> None:
    assert "stop community review" in TERMS_NOTICE


def test_it_is_shown_when_onboarding_ends(core) -> None:  # noqa: F811
    service = ApplicationService(core.db_path, core=core)
    last = None
    for i, text in enumerate(("anything", "english", "neutral", "alex")):
        last = service.handle_message(_incoming(text, now=FIXED_TIME, external_message_id=f"ob{i}"))
    assert last.text.endswith(TERMS_NOTICE) and last.text.startswith("All set!")


def test_it_is_part_of_the_chaster_connect_reply() -> None:
    text = (ROOT / "application" / "service.py").read_text(encoding="utf-8")
    body = text[text.index("def _handle_chaster_connect"):text.index("def _chaster_chat_context")]
    assert "{TERMS_NOTICE}" in body
