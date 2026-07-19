from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

from token_tracker import iterm_split


def test_osascript_argv_passes_dynamic_values_as_arguments():
    session_id = 'session "quoted"'
    command = "exec '/opt/tt env/bin/python' -m token_tracker.sidebar_command current 中文"

    argv = iterm_split._osascript_argv(session_id, command)

    assert argv[:3] == ["/usr/bin/osascript", "-e", iterm_split._APPLESCRIPT]
    assert argv[-3:] == ["--", session_id, command]
    assert session_id not in iterm_split._APPLESCRIPT
    assert command not in iterm_split._APPLESCRIPT


def test_applescript_keeps_command_gated_and_layout_transactional():
    script = iterm_split._APPLESCRIPT

    assert "on run argv" in script
    assert "set targetId to item 1 of argv" in script
    assert "set launchCommand to item 2 of argv" in script
    assert "quoted form of gatedCommand" in script
    assert "COLUMNS <= _tt_limit" in script
    assert "split vertically with same profile command wrappedCommand" in script
    assert "set targetColumns to splitSourceColumns div 2" in script
    assert "set bounds of sourceWindow to originalBounds" in script
    assert "select sourceSession" in script
    assert "close sidebarSession" in script


def test_project_has_no_iterm2_python_dependency():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert not any(dependency.partition(";")[0].strip().lower().startswith("iterm2") for dependency in dependencies)


def test_run_split_uses_system_osascript_and_timeout(monkeypatch):
    completed = subprocess.CompletedProcess(["osascript"], 0, "tt_sidebar_split_ok\n", "")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(iterm_split.sys, "platform", "darwin")
    monkeypatch.setattr(iterm_split.subprocess, "run", run)

    ok, message = iterm_split._run_split("session-uuid", "exec sidebar")

    assert (ok, message) == (True, "tt_sidebar_split_ok")
    assert run.call_args.args[0][-3:] == ["--", "session-uuid", "exec sidebar"]
    assert run.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": iterm_split._PROCESS_TIMEOUT,
    }


def test_run_split_explains_automation_denial(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        'execution error: Not authorized to send Apple events to iTerm2. (-1743)\n',
    )
    monkeypatch.setattr(iterm_split.sys, "platform", "darwin")
    monkeypatch.setattr(iterm_split.subprocess, "run", MagicMock(return_value=completed))

    ok, message = iterm_split._run_split("session-uuid", "exec sidebar")

    assert not ok
    assert "系统设置" in message
    assert "自动化" in message
    assert "iTerm2" in message


def test_run_split_explains_missing_source_and_rolls_back_marker(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: tt_sidebar_source_not_found (-2700)\n",
    )
    monkeypatch.setattr(iterm_split.sys, "platform", "darwin")
    monkeypatch.setattr(iterm_split.subprocess, "run", MagicMock(return_value=completed))

    ok, message = iterm_split._run_split("gone", "exec sidebar")

    assert not ok
    assert "找不到发起命令的 iTerm2 会话窗格" in message
    assert "原窗格重新执行 $tt-sidebar" in message


def test_run_split_reports_layout_timeout_and_process_timeout(monkeypatch):
    layout_failed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: tt_sidebar_layout_timeout (-2700)\n",
    )
    monkeypatch.setattr(iterm_split.sys, "platform", "darwin")
    run = MagicMock(return_value=layout_failed)
    monkeypatch.setattr(iterm_split.subprocess, "run", run)

    assert iterm_split._run_split("session", "exec sidebar") == (
        False,
        "iTerm2 分屏失败：右侧 1/3 布局未能收敛，已关闭本次新窗格并恢复原窗口。",
    )
    assert "请先退出全屏" in iterm_split._failure_message("tt_sidebar_resize_timeout")

    run.side_effect = subprocess.TimeoutExpired(["osascript"], iterm_split._PROCESS_TIMEOUT)
    ok, message = iterm_split._run_split("session", "exec sidebar")
    assert not ok
    assert f"启动器超时（{iterm_split._PROCESS_TIMEOUT:g}s）" in message


def test_run_split_rejects_non_macos(monkeypatch):
    monkeypatch.setattr(iterm_split.sys, "platform", "linux")
    run = MagicMock()
    monkeypatch.setattr(iterm_split.subprocess, "run", run)

    assert iterm_split._run_split("session", "exec sidebar") == (
        False,
        "iTerm2 自动分屏仅支持 macOS。",
    )
    run.assert_not_called()
