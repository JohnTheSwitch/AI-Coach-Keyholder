"""
system/update_check.py

The update check (owner, 2026-11-13: "check for updates"; the owner chose check and notify, never an automatic update).

Once a day it compares the running commit with the newest commit of the repository's `main` on GitHub and, when GitHub is ahead, sends the
owner ONE notice per new GitHub commit: how many updates and their titles. Updating stays a manual step: nothing is fetched, pulled or run.

Read-only on both sides:
- locally: `git rev-parse HEAD` and `git remote get-url origin` (no fetch: the repository is never written);
- remotely: `git ls-remote <origin> refs/heads/main` for the newest commit, and the public GitHub compare API
  (`GET https://api.github.com/repos/<owner>/<repo>/compare/<running>...<newest>`) for the count and titles, best effort.

A local commit GitHub does not have yet (not pushed) is not an update: the notice is only for `ahead` (GitHub has commits the running version
lacks). Any failure only means no notice this time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from infrastructure.decision_log import decision

logger = logging.getLogger("ai_coach_keyholder.system.update_check")

__all__ = ["UpdateInfo", "check_for_updates", "github_repo", "update_notice", "update_check_loop", "CHECK_EVERY_SECONDS", "BRANCH"]

CHECK_EVERY_SECONDS = 24 * 3600
BRANCH = "main"
_GITHUB = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_TITLES = 8


@dataclass(frozen=True)
class UpdateInfo:
    running: str
    newest: str
    behind_by: int | None              # None: GitHub's count was not available
    titles: tuple[str, ...]


def github_repo(url: str | None) -> tuple[str, str] | None:
    m = _GITHUB.search((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def _github_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-coach-keyholder-update-check"})
    with urllib.request.urlopen(request, timeout=30) as response:          # https only; certificate checks stay on
        return json.loads(response.read().decode("utf-8"))


def check_for_updates(repo: Path, *, git: Callable[..., str] = _git, github: Callable[[str], dict] = _github_json) -> UpdateInfo | None:
    """None: up to date, not a GitHub repository, or the check could not be made."""
    running = git(repo, "rev-parse", "HEAD")
    origin = git(repo, "remote", "get-url", "origin")
    names = github_repo(origin)
    if not _SHA.match(running) or names is None:
        return None
    line = git(repo, "ls-remote", origin, f"refs/heads/{BRANCH}")
    newest = line.split()[0] if line else ""
    if not _SHA.match(newest) or newest == running:
        return None
    try:
        body = github(f"https://api.github.com/repos/{names[0]}/{names[1]}/compare/{running}...{newest}")
    except Exception:
        logger.info("The GitHub compare could not be read; reporting the update without details.", exc_info=True)
        return UpdateInfo(running=running, newest=newest, behind_by=None, titles=())
    if body.get("status") != "ahead":
        return None                                     # identical, or the running version has commits GitHub lacks: not an update
    titles = tuple(str(((c.get("commit") or {}).get("message") or "").splitlines()[0])[:120] for c in body.get("commits") or [] if isinstance(c, dict))
    return UpdateInfo(running=running, newest=newest, behind_by=int(body.get("ahead_by") or len(titles)), titles=titles[-_MAX_TITLES:])


def update_notice(info: UpdateInfo) -> str:
    count = "An update is" if info.behind_by in (None, 1) else f"{info.behind_by} updates are"
    lines = [f"{count} available on GitHub (running {info.running[:7]}, newest {info.newest[:7]}). Nothing was installed."]
    lines += [f"- {title}" for title in reversed(info.titles)]
    if info.behind_by and info.behind_by > len(info.titles):
        lines.append(f"- and {info.behind_by - len(info.titles)} more")
    return "\n".join(lines)


async def update_check_loop(repo: Path, notify: Callable[[str, str], None], *, interval_seconds: float = CHECK_EVERY_SECONDS,
                            initial_delay_seconds: float = 600.0, check: Callable[[Path], UpdateInfo | None] = check_for_updates) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            info = await asyncio.to_thread(check, repo)
            if info is not None:
                decision("system", "An update is available on GitHub.", running=info.running[:12], newest=info.newest[:12], behind_by=info.behind_by)
                notify(f"update_check:{info.newest}", update_notice(info))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("The update check failed (%s); will try again tomorrow.", type(exc).__name__)
        await asyncio.sleep(interval_seconds)
