"""scripts/make_start_script.py and the committed scripts/start_bot.bat."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.make_start_script import TARGET, main, render_start_script

ROOT = Path(__file__).parent.parent.parent


def test_render_uses_absolute_paths_and_switches_directory() -> None:
    text = render_start_script(r"C:\Repo Dir", r"C:\Py\python.exe")
    lines = text.split("\r\n")
    assert lines[0] == "@echo off" and 'cd /d "C:\\Repo Dir"' in lines and '"C:\\Py\\python.exe" -m bot.discord_bot' in lines
    assert text.endswith("exit /b %ERRORLEVEL%\r\n") and "\n" not in text.replace("\r\n", "")


@pytest.mark.parametrize("bad", ['C:\\a"b', "C:\\a&b", "C:\\a%PATH%", "C:\\a|b"])
def test_unsafe_paths_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        render_start_script(bad, r"C:\Py\python.exe")


def test_a_missing_interpreter_is_refused(tmp_path, capsys) -> None:
    assert main(["--python", str(tmp_path / "nope.exe")]) == 2 and "Refused" in capsys.readouterr().out


def test_the_committed_launcher_is_this_generator_output_for_this_repository() -> None:
    text = TARGET.read_bytes().decode("ascii")
    assert f'cd /d "{ROOT.resolve()}"' in text and "-m bot.discord_bot" in text and text.startswith("@echo off\r\n")
