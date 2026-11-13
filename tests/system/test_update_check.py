"""Owner, 2026-11-13: check for updates and tell the owner; never install anything. No network: git and GitHub are faked."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from system.update_check import UpdateInfo, check_for_updates, github_repo, update_check_loop, update_notice

RUNNING, NEWEST = "a" * 40, "b" * 40


def fake_git(head=RUNNING, origin="https://github.com/JohnTheSwitch/AI-Coach-Keyholder.git", remote=NEWEST):
    calls = []

    def git(repo, *args):
        calls.append(args)
        if args[:1] == ("rev-parse",):
            return head
        if args[:2] == ("remote", "get-url"):
            return origin
        if args[:1] == ("ls-remote",):
            return f"{remote}\trefs/heads/main" if remote else ""
        raise AssertionError(f"unexpected git call {args}")
    git.calls = calls
    return git


def compare(status="ahead", titles=("feat: one", "fix: two")):
    return lambda url: {"status": status, "ahead_by": len(titles), "commits": [{"commit": {"message": t + "\n\nbody"}} for t in titles]}


class TestTheCheck:
    def test_github_ahead_is_an_update_with_titles(self) -> None:
        info = check_for_updates(Path("."), git=fake_git(), github=compare())
        assert info == UpdateInfo(running=RUNNING, newest=NEWEST, behind_by=2, titles=("feat: one", "fix: two"))

    def test_up_to_date_is_none(self) -> None:
        assert check_for_updates(Path("."), git=fake_git(remote=RUNNING), github=compare()) is None

    @pytest.mark.parametrize("status", ["identical", "behind", "diverged"])
    def test_local_commits_github_lacks_are_not_an_update(self, status) -> None:
        assert check_for_updates(Path("."), git=fake_git(), github=compare(status=status)) is None

    def test_without_details_it_still_reports(self) -> None:
        def broken(_url):
            raise OSError("offline")
        info = check_for_updates(Path("."), git=fake_git(), github=broken)
        assert info is not None and info.behind_by is None and info.titles == ()

    def test_not_a_github_repository(self) -> None:
        assert check_for_updates(Path("."), git=fake_git(origin="https://example.com/x.git"), github=compare()) is None

    def test_it_only_reads(self) -> None:
        git = fake_git()
        check_for_updates(Path("."), git=git, github=compare())
        assert {c[0] for c in git.calls} == {"rev-parse", "remote", "ls-remote"}
        source = Path("system/update_check.py").read_text(encoding="utf-8")
        for word in ('"fetch"', '"pull"', '"checkout"', '"reset"', '"merge"', "verify=False", "CERT_NONE"):
            assert word not in source

    def test_github_repo(self) -> None:
        assert github_repo("git@github.com:o/r.git") == ("o", "r") and github_repo("https://github.com/o/r") == ("o", "r")


class TestTheNotice:
    def test_it_lists_newest_first_and_says_nothing_was_installed(self) -> None:
        text = update_notice(UpdateInfo(running=RUNNING, newest=NEWEST, behind_by=2, titles=("feat: one", "fix: two")))
        assert text.startswith("2 updates are available") and "Nothing was installed." in text
        assert text.index("fix: two") < text.index("feat: one")

    def test_once_per_new_commit(self) -> None:
        sent = []

        async def run():
            task = asyncio.create_task(update_check_loop(Path("."), lambda key, text: sent.append(key), interval_seconds=0.01,
                                                         initial_delay_seconds=0, check=lambda _r: UpdateInfo(RUNNING, NEWEST, 1, ())))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        assert sent and set(sent) == {f"update_check:{NEWEST}"}       # the queue's dedupe key makes it one notice
