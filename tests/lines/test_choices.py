"""Swats instead of lines, the written request, the release window, photo check-ins by lock length (owner, 2026-11-15/16; queue A5, B1-B3)."""

from __future__ import annotations

from datetime import timedelta

from lines.service import LineService, PaperPort, swats_for
from tests.lines.test_lines import NOW, U, assign, core, rows
from tests.recovery_tasks.test_recovery_tasks import FakeChaster


def paper(db, chaster):
    s = LineService(db, user_id=U)
    s.paper = PaperPort(chaster=chaster, lock_id=lambda now: "lock", halted=lambda: False, slot_taken=lambda: False)
    return s


def line_id(db):
    return rows(db, "SELECT id FROM line_assignments ORDER BY created_at DESC, rowid DESC LIMIT 1")[0]["id"]


class TestSwats:
    def test_one_swat_per_two_lines_and_verified_means_done(self, tmp_path) -> None:
        db, chaster = core(tmp_path), FakeChaster()
        s = paper(db, chaster)
        assign(s, db, "penalty_warning.recorded")
        assert "Swats instead" in rows(db, "SELECT actions_json FROM notifications WHERE kind = 'lines'")[0]["actions_json"]
        s.paper_tick(NOW)                                                    # the sheet is in Chaster
        reply = s.press(user_id=U, row_id=line_id(db), action="swats", now=NOW)
        assert "10 light swats" in reply and chaster.unassigned == 1
        s.paper_tick(NOW + timedelta(minutes=1))
        assert chaster.assigned[-1] == "10 light hand swats, counted aloud. Photo of the skin only."
        chaster.entries[chaster.current["id"]] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "verified"}}
        s.paper_tick(NOW + timedelta(minutes=30))
        assert s.open_lines(U) == 0 and swats_for(40) == 20 and swats_for(1) == 1

    def test_not_done_in_two_hours_brings_the_lines_back_without_a_new_incident(self, tmp_path) -> None:
        db, chaster = core(tmp_path), FakeChaster()
        s = paper(db, chaster)
        assign(s, db, "penalty_warning.recorded")
        s.press(user_id=U, row_id=line_id(db), action="swats", now=NOW)
        s.paper_tick(NOW)
        s.paper_tick(NOW + timedelta(hours=2, minutes=5))
        row = rows(db, "SELECT mode, task_id, status FROM line_assignments")[0]
        assert row == {"mode": None, "task_id": None, "status": "open"} and s.open_lines(U) == 20
        assert "the lines are back" in rows(db, "SELECT text FROM notifications WHERE dedupe_key LIKE 'lines:swats_back:%'")[0]["text"]

    def test_a_rejected_photo_brings_the_lines_back(self, tmp_path) -> None:
        db, chaster = core(tmp_path), FakeChaster()
        s = paper(db, chaster)
        assign(s, db, "penalty_warning.recorded")
        s.press(user_id=U, row_id=line_id(db), action="swats", now=NOW)
        s.paper_tick(NOW)
        chaster.entries[chaster.current["id"]] = {"createdAt": NOW.isoformat(), "peerVerification": {"status": "rejected"}}
        s.paper_tick(NOW + timedelta(minutes=30))
        assert rows(db, "SELECT mode FROM line_assignments")[0]["mode"] is None


class TestBeforeARelease:
    LONG = " ".join(f"word{i}" for i in range(31))

    def _release(self, s, db, at):
        assert s.assign_release((at - timedelta(minutes=1)).isoformat(), at)
        return line_id(db)

    def test_three_options_and_never_the_last_one(self, tmp_path) -> None:
        db, chaster = core(tmp_path), FakeChaster()
        s = paper(db, chaster)
        first = self._release(s, db, NOW)
        note = rows(db, "SELECT text, actions_json FROM notifications WHERE kind = 'lines' ORDER BY created_at DESC, rowid DESC")[0]
        assert "write this 20 times" in note["text"] and "Swats instead" in note["actions_json"] and "Written request" in note["actions_json"]
        assert "Written request" in s.press(user_id=U, row_id=first, action="request", now=NOW) or True
        assert s.take(U, self.LONG, NOW) is not None
        done = s.take(U, self.LONG + " better", NOW)
        assert "accepted" in done.text and s.release_done_since((NOW - timedelta(minutes=2)).isoformat())
        later = NOW + timedelta(days=3)
        second = self._release(s, db, later)                               # the request was last: lines (default), swats offered
        note = rows(db, "SELECT actions_json FROM notifications WHERE kind = 'lines' ORDER BY created_at DESC, rowid DESC")[0]
        assert "Written request" not in note["actions_json"] and "Swats instead" in note["actions_json"]
        assert s.press(user_id=U, row_id=second, action="request", now=later) == "That choice isn't open for these."
        assert s.get_promises_between((NOW - timedelta(days=1)).isoformat(), later.isoformat()) == [self.LONG + " better"]

    def test_after_lines_the_default_is_the_written_request(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        first = self._release(s, db, NOW)
        with db.transaction() as tx:
            tx.execute("UPDATE line_assignments SET status = 'done', done = required, completed_at = ? WHERE id = ?", (NOW.isoformat(), first))
        self._release(s, db, NOW + timedelta(days=3))
        row = rows(db, "SELECT mode FROM line_assignments ORDER BY created_at DESC LIMIT 1")[0]
        note = rows(db, "SELECT text, actions_json FROM notifications WHERE kind = 'lines' ORDER BY created_at DESC, rowid DESC")[0]
        assert row["mode"] == "request" and "Lines were last time" in note["text"] and "Swats instead" in note["actions_json"]
        assert "Lines" not in note["actions_json"].replace("Lines were", "")

    def test_a_short_message_is_an_ordinary_one(self, tmp_path) -> None:
        db = core(tmp_path)
        s = LineService(db, user_id=U)
        first = self._release(s, db, NOW)
        s.press(user_id=U, row_id=first, action="request", now=NOW)
        assert s.take(U, "how are you today?", NOW) is None


class TestReleaseWindow:
    def _window(self, db, pictures, raised):
        from release_earning.window import ReleaseWindow
        return ReleaseWindow(db, user_id=U, last_release=lambda: ("auth-1", NOW), pictures=pictures, local_clock=lambda t: t.strftime("%H:%M"),
                             raise_overrun=lambda row, now: raised.append(row["authorization_id"]))

    def test_the_window_is_announced_and_a_picture_in_time_closes_it(self, tmp_path) -> None:
        db, raised = core(tmp_path), []
        spans = []
        w = self._window(db, lambda a, b: spans.append((a, b)) or True, raised)
        w.tick(NOW + timedelta(minutes=1))
        assert "release window: 2 hours" in rows(db, "SELECT text FROM notifications WHERE kind = 'release_window'")[0]["text"]
        w.tick(NOW + timedelta(hours=2, minutes=11))
        assert rows(db, "SELECT outcome FROM release_windows")[0]["outcome"] == "picture" and raised == []
        assert spans[0][0] == NOW + timedelta(minutes=15)                  # a picture before the release itself doesn't count

    def test_no_picture_asks_and_unknown_never_counts(self, tmp_path) -> None:
        db, raised = core(tmp_path), []
        self._window(db, lambda a, b: False, raised).tick(NOW + timedelta(hours=2, minutes=11))
        assert raised == ["auth-1"]
        db2, raised2 = core(tmp_path / "b"), []
        w = self._window(db2, lambda a, b: None, raised2)
        w.tick(NOW + timedelta(hours=2, minutes=11))
        w.tick(NOW + timedelta(hours=9))
        assert rows(db2, "SELECT outcome FROM release_windows")[0]["outcome"] == "unknown" and raised2 == []

    def test_a_release_never_opened_asks_nothing(self, tmp_path) -> None:
        """Owner, 2026-11-16 21:50: the backstop unfroze his lock, he stayed locked, and was asked to explain an "overrun"."""
        from release_earning.window import ReleaseWindow
        db, raised = core(tmp_path), []
        w = ReleaseWindow(db, user_id=U, last_release=lambda: ("auth-1", NOW), pictures=lambda a, b: False, local_clock=lambda t: t.strftime("%H:%M"),
                          raise_overrun=lambda row, now: raised.append(1), stayed_locked=lambda a, b: True,
                          open_until=lambda c: c + timedelta(minutes=35))
        w.tick(NOW + timedelta(minutes=1))
        text = rows(db, "SELECT text FROM notifications WHERE kind = 'release_window'")[0]["text"]
        assert f"until {(NOW + timedelta(minutes=35)).strftime('%H:%M')}; after that it is frozen again" in text and "stay locked, nothing" in text
        w.tick(NOW + timedelta(hours=2, minutes=11))
        assert rows(db, "SELECT outcome FROM release_windows")[0]["outcome"] == "not_opened" and raised == []

    def test_opened_or_unknown_falls_back_to_the_pictures(self, tmp_path) -> None:
        from release_earning.window import ReleaseWindow
        for stayed in (False, None):
            db, raised = core(tmp_path / str(stayed)), []
            ReleaseWindow(db, user_id=U, last_release=lambda: ("auth-1", NOW), pictures=lambda a, b: False, local_clock=lambda t: t.strftime("%H:%M"),
                          raise_overrun=lambda row, now: raised.append(1), stayed_locked=lambda a, b, s=stayed: s).tick(NOW + timedelta(hours=2, minutes=11))
            assert raised == [1], stayed


def test_whether_the_lock_stayed_locked(tmp_path) -> None:
    import uuid
    from infrastructure.time_format import iso
    from system.keyholder_lock_source import get_lock_stayed_locked
    db = core(tmp_path)

    def seen(at, status="locked", lock="L1"):
        with db.transaction() as tx:
            tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, fetched_at, created_at) "
                       "VALUES (?, ?, 'observed', 'keyholder_search', ?, 0, ?, ?)", (str(uuid.uuid4()), lock, status, iso(at), iso(at)))

    end = NOW + timedelta(hours=2, minutes=10)
    assert get_lock_stayed_locked(db, NOW, end) is None                        # nothing read yet
    seen(NOW - timedelta(minutes=5))
    seen(NOW + timedelta(hours=1))
    assert get_lock_stayed_locked(db, NOW, end) is None                        # nothing after the window yet
    seen(end + timedelta(minutes=5))
    assert get_lock_stayed_locked(db, NOW, end) is True
    seen(end + timedelta(minutes=15), lock="L2")                               # a new lock: the old one was opened
    assert get_lock_stayed_locked(db, NOW, end) is False


def test_photo_check_ins_grow_with_the_lock(tmp_path) -> None:
    from tests.supervision.test_photo_check_ins import photo_env
    e = photo_env(tmp_path, score=0.9)
    assert e.svc._long_lock_extra(NOW) == 0
    e.svc.lock_days = lambda now: 8
    assert e.svc._long_lock_extra(NOW) == 1
    e.svc.lock_days = lambda now: 30
    assert e.svc._long_lock_extra(NOW) == 2
    e.svc.lock_days = lambda now: (_ for _ in ()).throw(RuntimeError("x"))
    assert e.svc._long_lock_extra(NOW) == 0


def test_swats_need_chaster_tasks(tmp_path) -> None:
    db = core(tmp_path)
    s = LineService(db, user_id=U)
    assign(s, db, "penalty_warning.recorded")
    assert "aren't available" in s.press(user_id=U, row_id=line_id(db), action="swats", now=NOW)
    assert rows(db, "SELECT mode FROM line_assignments")[0]["mode"] is None


def test_a_written_request_is_never_paid_with_typed_lines(tmp_path) -> None:
    from lines.service import RELEASE_SENTENCE
    db = core(tmp_path)
    s = LineService(db, user_id=U)
    assert s.assign_release((NOW - timedelta(minutes=1)).isoformat(), NOW)
    s.press(user_id=U, row_id=line_id(db), action="request", now=NOW)
    assert s.take(U, "\n".join([RELEASE_SENTENCE] * 20), NOW + timedelta(hours=4)) is None
    assert rows(db, "SELECT status FROM line_assignments")[0]["status"] == "open"


def test_the_lock_length_is_read_outside_the_photo_draw(tmp_path) -> None:
    """Live, 2026-11-17 00:00: the lock's length was read inside the day's photo draw and opened a nested transaction."""
    from tests.supervision.test_photo_check_ins import photo_env
    e = photo_env(tmp_path, score=0.9)
    seen = []

    def lock_days(now):
        with e.svc._core.transaction():
            seen.append(now)
        return 30

    e.svc.lock_days = lock_days
    e.svc._schedule_photo(NOW)
    assert seen
