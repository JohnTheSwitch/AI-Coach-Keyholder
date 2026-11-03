"""scripts/make_start_script.py and the committed scripts/start_bot.bat (logging to logs\\bot.log and the restart loop)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.make_start_script import MAX_RESTARTS, RESTART_DELAY_SECONDS, TARGET, main, render_start_script

ROOT = Path(__file__).parent.parent.parent


def test_render_uses_absolute_paths_logs_and_the_restart_loop() -> None:
    text = render_start_script(r"C:\Repo Dir", r"C:\Py\python.exe")
    lines = text.split("\r\n")
    assert lines[0] == "@echo off" and 'cd /d "C:\\Repo Dir" || exit /b 1' in lines
    assert '"C:\\Py\\python.exe" -m bot.discord_bot >> "logs\\bot.log" 2>&1' in lines
    assert f"if %RESTARTS% GEQ {MAX_RESTARTS} goto giveup" in lines and f"ping -n {RESTART_DELAY_SECONDS + 1} 127.0.0.1 >nul" in lines
    assert '"%CODE%"=="0" goto done' in text and text.endswith("endlocal & exit /b %CODE%\r\n")
    echoes = [l for l in lines if " echo " in l or l.startswith("echo")]
    assert len(echoes) == 4 and all(l.startswith(r'>> "logs\bot.log" echo [%date% %time%]') for l in echoes)   # never "3>>" (handle 3)
    assert "\n" not in text.replace("\r\n", "") and (MAX_RESTARTS, RESTART_DELAY_SECONDS) == (3, 10)


@pytest.mark.parametrize("bad", ['C:\\a"b', "C:\\a&b", "C:\\a%PATH%", "C:\\a|b"])
def test_unsafe_paths_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        render_start_script(bad, r"C:\Py\python.exe")


def test_a_missing_interpreter_is_refused(tmp_path, capsys) -> None:
    assert main(["--python", str(tmp_path / "nope.exe")]) == 2 and "Refused" in capsys.readouterr().out


def test_the_committed_launcher_is_the_generator_output_for_this_repository() -> None:
    text = TARGET.read_bytes().decode("ascii")
    assert f'cd /d "{ROOT.resolve()}"' in text and '-m bot.discord_bot >> "logs\\bot.log" 2>&1' in text
    assert text.startswith("@echo off\r\n") and "goto giveup" in text


def _fake_repo(tmp_path: Path, exit_code: int) -> Path:
    repo = tmp_path / "repo"
    (repo / "bot").mkdir(parents=True)
    (repo / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "bot" / "discord_bot.py").write_text(f"print('fake bot run')\nraise SystemExit({exit_code})\n", encoding="utf-8")
    (repo / "start.bat").write_bytes(render_start_script(repo, sys.executable, max_restarts=2, restart_delay_seconds=0).encode("ascii"))
    return repo


@pytest.mark.skipif(sys.platform != "win32", reason="runs cmd.exe")
def test_on_windows_a_crashing_bot_is_restarted_then_given_up(tmp_path) -> None:
    repo = _fake_repo(tmp_path, exit_code=3)
    done = subprocess.run(["cmd", "/c", str(repo / "start.bat")], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    log = (repo / "logs" / "bot.log").read_text(encoding="utf-8", errors="replace")
    assert done.returncode == 3
    assert log.count("fake bot run") == 3 and log.count("the bot exited with code 3") == 3
    assert "restarts so far 0" in log and "restarts so far 2" in log
    assert "restart 1 of 2" in log and "restart 2 of 2" in log and "giving up after 2 restarts" in log


@pytest.mark.skipif(sys.platform != "win32", reason="runs cmd.exe")
def test_on_windows_a_clean_exit_is_not_restarted(tmp_path) -> None:
    repo = _fake_repo(tmp_path, exit_code=0)
    done = subprocess.run(["cmd", "/c", str(repo / "start.bat")], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    log = (repo / "logs" / "bot.log").read_text(encoding="utf-8", errors="replace")
    assert done.returncode == 0 and log.count("fake bot run") == 1 and "restarting" not in log
