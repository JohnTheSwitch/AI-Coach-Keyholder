"""The tamper watch (owner, 2026-11-16): code or stored history changed outside the bot is asked about."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tamper_watch.service import TamperWatch
from tests.lines.test_lines import core

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class Git:
    def __init__(self) -> None:
        self.head, self.status, self.log = "a" * 40, "", ""

    def __call__(self, *args):
        if args[0] == "rev-parse":
            return self.head + "\n"
        if args[0] == "status":
            return self.status
        if args[0] == "log":
            return self.log
        return None


def world(tmp_path):
    db, git, raised = core(tmp_path), Git(), []
    return db, git, raised, TamperWatch(db, repo=tmp_path, raise_finding=lambda f, now: raised.append(f), git=git)


def _event(db, n: int) -> None:
    with db.transaction() as tx:
        tx.execute("INSERT INTO domain_events (id, event_type, source_module, payload_json, occurred_at, created_at) VALUES (?, 'x.y', 'x', '{}', ?, ?)",
                   (f"e{n}", NOW.isoformat(), NOW.isoformat()))


def test_a_clean_start_raises_nothing_and_records_the_commit(tmp_path) -> None:
    db, git, raised, watch = world(tmp_path)
    assert watch.check_at_start(NOW) == [] and raised == []
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT last_commit FROM tamper_watch_state")["last_commit"] == "a" * 40


def test_a_dirty_start_and_a_commit_not_by_claude_are_asked_about(tmp_path) -> None:
    db, git, raised, watch = world(tmp_path)
    watch.check_at_start(NOW)
    git.head, git.status = "b" * 40, " M lines/service.py\n"
    git.log = ("b" * 40 + "\x1fmake tokens easier\x1fmake tokens easier\n\x1e" +
               "c" * 40 + "\x1ffeat: x\x1ffeat: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n\x1e")
    found = watch.check_at_start(NOW + timedelta(hours=1))
    kinds = sorted(f.subject_id.split(":")[0] for f in found)
    assert kinds == ["commit", "dirty"] and found == raised
    assert any("make tokens easier" in f.wearer_text for f in found)


def test_history_edited_while_the_bot_was_off_is_noticed_but_new_rows_are_not(tmp_path) -> None:
    db, git, raised, watch = world(tmp_path)
    _event(db, 1)
    watch.check_at_start(NOW)
    _event(db, 2)                                             # the bot's own later writes never count
    assert watch.check_at_start(NOW + timedelta(hours=1)) == []
    with db.transaction() as tx:
        tx.execute("DELETE FROM domain_events WHERE id = 'e1'")
    found = watch.check_at_start(NOW + timedelta(hours=2))
    assert [f.subject_id.split(":")[0] for f in found] == ["history"]


def test_a_broken_git_raises_nothing(tmp_path) -> None:
    db = core(tmp_path)
    raised = []
    watch = TamperWatch(db, repo=tmp_path, raise_finding=lambda f, now: raised.append(f), git=lambda *a: None)
    assert watch.check_at_start(NOW) == [] and raised == []


def test_changes_to_tests_and_docs_are_not_a_dirty_start(tmp_path) -> None:
    db, git, raised, watch = world(tmp_path)
    git.status = " M tests/system/test_x.py\n M docs/roadmap.md\n M lines/README.md\n"
    assert watch.check_at_start(NOW) == []


def test_dirtiness_is_read_when_the_bot_starts_not_a_minute_later(tmp_path) -> None:
    """Bug fix 2026-11-16: the check ran 60 s after start and saw Claude's next, uncommitted edits."""
    import asyncio

    class Clock:
        def now(self):
            return NOW

    db, git, raised, watch = world(tmp_path)

    async def run():
        task = asyncio.create_task(watch.loop(Clock(), initial_delay_seconds=0.2))
        await asyncio.sleep(0.05)
        git.status = " M lines/service.py\n"                    # edited after the start
        await asyncio.sleep(0.4)
        task.cancel()

    asyncio.run(run())
    assert raised == []


def test_commits_from_the_trusted_upstream_of_someone_elses_copy_are_not_asked_about(tmp_path) -> None:
    """Owner, 2026-11-16: someone running their own copy pulls his commits; only their own changes are asked about."""
    class UpstreamGit(Git):
        def __call__(self, *args):
            if args[0] == "merge-base":
                return "" if args[2] == "b" * 40 and args[3] == "upstream/main" else None
            return super().__call__(*args)

    db, git, raised = core(tmp_path), UpstreamGit(), []
    watch = TamperWatch(db, repo=tmp_path, raise_finding=lambda f, now: raised.append(f), git=git, trusted_ref="upstream/main")
    watch.check_at_start(NOW)
    git.head = "c" * 40
    git.log = "b" * 40 + "\x1fowner's change\x1fowner's change\n\x1e" + "c" * 40 + "\x1fmy own change\x1fmy own change\n\x1e"
    found = watch.check_at_start(NOW + timedelta(hours=1))
    assert [f.subject_id for f in found] == ["commit:" + "c" * 40]


def test_without_a_trusted_upstream_every_commit_not_by_claude_is_asked_about(tmp_path) -> None:
    db, git, raised, watch = world(tmp_path)
    watch.check_at_start(NOW)
    git.head, git.log = "b" * 40, "b" * 40 + "\x1fpushed then pulled\x1fpushed then pulled\n\x1e"
    assert len(watch.check_at_start(NOW + timedelta(hours=1))) == 1
