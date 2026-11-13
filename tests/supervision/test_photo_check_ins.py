"""Photo check-ins (owner, 2026-11-13): some check-ins ask for a verification picture in Chaster. The bot only learns WHEN pictures were submitted.
No network."""

from __future__ import annotations

from datetime import timedelta

import pytest

from chaster.peer_reviews import parse_picture_times
from supervision.photo_check_ins import PHOTO_RECEIVED, PHOTO_RESPONSE_TIMEOUT, PHOTO_SLOT, PHOTO_UNKNOWN_LIMIT, PhotoCheckInSwitch
from tests.supervision.test_checkins import MON, PRAGUE, U, Env, local


def photo_env(tmp_path, *, consent=True, score=0.9, fraction=0.0):
    e = Env(tmp_path, score=score, fraction=fraction)       # 0.9: level 4, no text check-ins, one photo a week
    e.pictures = []
    e.svc.photo_times = lambda now: e.pictures
    if consent:
        e.svc.photo_consent.set(U, True, now=local(MON, 8))
    return e


def photos(e):
    return [r for r in e.rows() if r["kind"] == "photo"]


def due(e):
    from infrastructure.time_format import parse_iso
    return parse_iso(photos(e)[0]["due_at"])


class TestWhenOneIsAsked:
    def test_never_without_the_wearers_consent(self, tmp_path) -> None:
        e = photo_env(tmp_path, consent=False)
        e.svc.tick(local(MON, 9))
        assert photos(e) == []

    def test_never_without_a_way_to_read_the_pictures(self, tmp_path) -> None:
        e = photo_env(tmp_path)
        e.svc.photo_times = None
        e.svc.tick(local(MON, 9))
        assert photos(e) == []

    def test_one_is_drawn_in_free_time_with_the_whole_answer_time_free_too(self, tmp_path) -> None:
        from schedule_windows.windows import WindowKind, classify
        from tests.supervision.test_checkins import CFG
        e = photo_env(tmp_path)
        e.svc.tick(local(MON, 9))
        [row] = photos(e)
        assert row["slot_index"] == PHOTO_SLOT and row["status"] == "scheduled"
        assert classify(CFG, due(e)) is WindowKind.DISCRETIONARY and classify(CFG, due(e) + PHOTO_RESPONSE_TIMEOUT) is WindowKind.DISCRETIONARY

    def test_a_day_is_drawn_once_even_across_a_restart(self, tmp_path) -> None:
        from supervision.checkins import CheckInService
        from tests.supervision.test_checkins import CFG
        e = photo_env(tmp_path, fraction=0.99)          # the draw says no
        e.svc.tick(local(MON, 9))
        assert photos(e) == []
        again = CheckInService(e.core.db_path, core=e.core, window_config=CFG, user_id=U, trust_domain="d", get_trust_score=lambda: 0.9,
                               sender=e.sender, evaluate=lambda p, r: None, rand_fraction=lambda: 0.0)
        again.photo_times = lambda now: []
        again.tick(local(MON, 9, 5))
        assert photos(e) == []

    @staticmethod
    def _week(e, hour):
        """Each day: the morning draw, then, if one was drawn, it is sent and answered with a picture."""
        from infrastructure.time_format import parse_iso
        for day in range(7):
            e.svc.tick(local(MON + timedelta(days=day), hour))
            for row in photos(e):
                if row["status"] == "scheduled":
                    at = parse_iso(row["due_at"])
                    e.svc.tick(at)
                    e.pictures.append(at + timedelta(minutes=10))
                    e.svc.tick(at + timedelta(minutes=11))
        return [r["status"] for r in photos(e)]

    def test_the_weeks_number_follows_trust(self, tmp_path) -> None:
        assert self._week(photo_env(tmp_path, score=0.9), 9) == ["answered"]            # level 4: one a week

    def test_lower_trust_asks_more_often(self, tmp_path) -> None:
        assert self._week(photo_env(tmp_path, score=0.3), 6).count("answered") == 4     # level 1: four a week

    def test_a_skipped_one_comes_back_on_a_later_day(self, tmp_path) -> None:
        e = photo_env(tmp_path, score=0.9)
        e.svc.tick(local(MON, 9))
        e.svc.tick(local(MON + timedelta(days=1), 9))       # the PC was off at Monday's time: skipped, and Tuesday draws again
        assert [r["status"] for r in photos(e)] == ["skipped", "scheduled"]


class TestTheAnswer:
    def _sent(self, tmp_path):
        e = photo_env(tmp_path)
        e.svc.tick(local(MON, 9))
        e.svc.tick(due(e))
        return e, due(e)

    def test_the_prompt_says_where_and_until_when(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        text = e.sender.sent[-1][1]
        assert "verification picture in Chaster" in text and (at + PHOTO_RESPONSE_TIMEOUT).astimezone(PRAGUE).strftime("%H:%M") in text
        assert photos(e)[0]["status"] == "sent"

    def test_a_picture_in_time_answers_it(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        e.pictures = [at + timedelta(minutes=40)]
        e.svc.tick(at + timedelta(minutes=41))
        assert photos(e)[0]["status"] == "answered" and e.sender.sent[-1][1] == PHOTO_RECEIVED
        assert e.events("check_in.missed") == []

    def test_an_older_picture_does_not(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        e.pictures = [at - timedelta(hours=3)]
        e.svc.tick(at + PHOTO_RESPONSE_TIMEOUT + timedelta(minutes=1))
        assert photos(e)[0]["status"] == "missed" and len(e.events("check_in.missed")) == 1

    def test_a_text_reply_does_not_answer_it(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        assert e.svc.on_user_message(U, "here you go", at + timedelta(minutes=5)) is False
        assert photos(e)[0]["status"] == "sent"

    def test_unreadable_pictures_are_never_a_miss(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        e.svc.photo_times = lambda now: None
        e.svc.tick(at + PHOTO_RESPONSE_TIMEOUT + timedelta(minutes=1))
        assert photos(e)[0]["status"] == "sent"
        e.svc.tick(at + PHOTO_RESPONSE_TIMEOUT + PHOTO_UNKNOWN_LIMIT + timedelta(minutes=2))
        assert photos(e)[0]["status"] == "skipped" and e.events("check_in.missed") == []

    def test_a_failing_reader_is_unknown_too(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)

        def boom(now):
            raise RuntimeError("down")
        e.svc.photo_times = boom
        e.svc.tick(at + PHOTO_RESPONSE_TIMEOUT + timedelta(minutes=1))
        assert photos(e)[0]["status"] == "sent"

    def test_revoking_closes_it_without_a_miss(self, tmp_path) -> None:
        e, at = self._sent(tmp_path)
        assert e.svc.photo_consent.set(U, False, now=at + timedelta(minutes=5)) is True
        e.svc.tick(at + PHOTO_RESPONSE_TIMEOUT + timedelta(minutes=1))
        assert photos(e)[0]["status"] == "skipped" and e.events("check_in.missed") == []
        assert len(e.events("supervision.photo_check_ins_revoked")) == 1


class TestPictureTimes:
    def test_only_well_formed_submission_times_are_kept(self) -> None:
        times = parse_picture_times([{"submittedAt": "2026-11-13T10:00:00.000Z", "imageKey": "never-kept"}, {"submittedAt": "garbage"},
                                     {"other": 1}, "x"])
        assert len(times) == 1 and times[0].tzinfo is not None


class TestTheWearersCommands:
    def _service(self, tmp_path, user_id=U):
        from application.service import ApplicationService
        e = photo_env(tmp_path, consent=False)
        service = ApplicationService.__new__(ApplicationService)
        service.photo_check_ins = PhotoCheckInSwitch(consent=e.svc.photo_consent, user_id=user_id)
        return service, e

    def _ctx(self, user_id=U):
        from types import SimpleNamespace
        return SimpleNamespace(user=SimpleNamespace(id=user_id), now=local(MON, 9))

    def test_on_and_off(self, tmp_path) -> None:
        service, e = self._service(tmp_path)
        assert "Photo check-ins are on" in service._handle_photo_on(self._ctx()).text and e.svc.photo_consent.is_on(U)
        assert "never counts as missed" in service._handle_photo_off(self._ctx()).text and not e.svc.photo_consent.is_on(U)

    def test_someone_else_cannot_switch_them(self, tmp_path) -> None:
        service, e = self._service(tmp_path)
        assert "not available" in service._handle_photo_on(self._ctx("someone-else")).text and not e.svc.photo_consent.is_on(U)

    @pytest.mark.parametrize("command", ["photo check-ins", "photo check-ins on", "photo check-ins off"])
    def test_the_commands_are_registered(self, command) -> None:
        from pathlib import Path
        assert f'self.router.register("{command}",' in Path("application/service.py").read_text(encoding="utf-8")
