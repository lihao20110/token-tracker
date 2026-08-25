from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from token_tracker import ghostty_split


def test_osascript_argv_passes_dynamic_values_as_arguments():
    command = '/bin/sh \'/tmp/tt launch.sh\''
    cwd = '/Users/test/项目 work'
    cols_file = "/tmp/tt-ghostty-cols-1-abc.txt"

    argv = ghostty_split._osascript_argv(command, cwd, cols_file)

    assert argv[:3] == ["/usr/bin/osascript", "-e", ghostty_split._APPLESCRIPT]
    assert argv[-4:] == ["--", command, cwd, cols_file]
    assert command not in ghostty_split._APPLESCRIPT
    assert cwd not in ghostty_split._APPLESCRIPT
    assert cols_file not in ghostty_split._APPLESCRIPT


def test_applescript_keeps_split_versioned_and_convergent():
    script = ghostty_split._APPLESCRIPT

    assert "on run argv" in script
    assert "versionBelow" in script
    assert "tt_sidebar_ghostty_version" in script
    assert "tt_sidebar_source_mismatch" in script
    assert "split src direction right with configuration cfg" in script
    assert "set targetColumns to (2 * c0) div 3" in script
    assert "resize_split:" in script
    assert "tt_sidebar_resize_timeout" in script
    assert "focus src" in script
    assert "close newTerm" in script


def test_wrapper_script_writes_cols_gate_and_self_cleans():
    script = ghostty_split._wrapper_script("exec /opt/tt/bin/python -m token_tracker.sidebar_command current s1", "/tmp/c f.txt")

    assert "stty -f /dev/tty size > '/tmp/c f.txt'" in script
    assert "_tt_limit=$(( $2 * 2 / 3 + 1 ))" in script
    assert 'rm -f "$0" \'/tmp/c f.txt\'' in script
    assert script.rstrip().endswith("exec /opt/tt/bin/python -m token_tracker.sidebar_command current s1")


def test_run_split_uses_system_osascript_and_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(ghostty_split.tempfile, "gettempdir", lambda: str(tmp_path))
    completed = subprocess.CompletedProcess(["osascript"], 0, "tt_sidebar_split_ok\n", "")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert (ok, message) == (True, "tt_sidebar_split_ok")
    argv = run.call_args.args[0]
    assert argv[0] == "/usr/bin/osascript"
    assert argv[-4] == "--"
    launch_command, cwd, cols_file = argv[-3:]
    assert cwd == "/cwd"
    assert launch_command.startswith("/bin/sh ")
    assert cols_file.startswith(str(tmp_path))
    assert run.call_args.kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": ghostty_split._PROCESS_TIMEOUT,
    }
    # 启动脚本写入 tmp，osascript 返回后兜底删除
    assert not (tmp_path / launch_command.removeprefix("/bin/sh ")).exists()


def test_run_split_reports_raw_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(ghostty_split.tempfile, "gettempdir", lambda: str(tmp_path))
    completed = subprocess.CompletedProcess(["osascript"], 0, "tt_sidebar_split_ok_raw\n", "")
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", MagicMock(return_value=completed))

    assert ghostty_split._run_split("exec sidebar", "/cwd") == (True, "tt_sidebar_split_ok_raw")


def test_run_split_explains_version_requirement(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: tt_sidebar_ghostty_version (-2700)\n",
    )
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", MagicMock(return_value=completed))

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert not ok
    assert "1.3.0" in message
    assert "升级" in message


def test_run_split_explains_automation_denial(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: Not authorized to send Apple events to Ghostty. (-1743)\n",
    )
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", MagicMock(return_value=completed))

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert not ok
    assert "系统设置" in message
    assert "自动化" in message
    assert "Ghostty" in message


def test_run_split_probe_explains_codex_sandbox(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: Expected end of line but found identifier. (-2741)\n",
    )
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert not ok
    assert "Codex 沙箱" in message
    assert "require_escalated" in message
    assert run.call_count == 1  # 探针即失败，不再跑整段脚本


def test_run_split_probe_reports_precise_version(monkeypatch):
    completed = subprocess.CompletedProcess(["osascript"], 0, "1.2.9\n", "")
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    run = MagicMock(return_value=completed)
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert not ok
    assert "1.2.9" in message
    assert "1.3.0" in message
    assert "升级" in message
    assert run.call_count == 1


def test_run_split_probe_passes_current_version(monkeypatch, tmp_path):
    monkeypatch.setattr(ghostty_split.tempfile, "gettempdir", lambda: str(tmp_path))
    probe = subprocess.CompletedProcess(["osascript"], 0, "1.3.1\n", "")
    split = subprocess.CompletedProcess(["osascript"], 0, "tt_sidebar_split_ok\n", "")
    run = MagicMock(side_effect=[probe, split])
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    assert ghostty_split._run_split("exec sidebar", "/cwd") == (True, "tt_sidebar_split_ok")
    assert run.call_count == 2


def test_probe_failure_does_not_block_split(monkeypatch, tmp_path):
    monkeypatch.setattr(ghostty_split.tempfile, "gettempdir", lambda: str(tmp_path))
    split = subprocess.CompletedProcess(["osascript"], 0, "tt_sidebar_split_ok\n", "")
    run = MagicMock(side_effect=[subprocess.TimeoutExpired(["osascript"], 5.0), split])
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    assert ghostty_split._run_split("exec sidebar", "/cwd") == (True, "tt_sidebar_split_ok")


def test_version_tuple_parses_dotted_versions():
    assert ghostty_split._version_tuple("1.3.1\n") == (1, 3, 1)
    assert ghostty_split._version_tuple("1.3.1") > ghostty_split._version_tuple("1.3")
    assert ghostty_split._version_tuple("1.2.9") < ghostty_split._version_tuple("1.3")
    assert ghostty_split._version_tuple("tt_sidebar_split_ok") is None


def test_failure_message_fallback_covers_runtime_dictionary_denial():
    message = ghostty_split._failure_message("execution error: Expected end of line but found identifier. (-2741)\n")

    assert "沙箱外" in message
    assert "1.3.0" in message


def test_run_split_reports_source_mismatch_resize_timeout_and_process_timeout(monkeypatch):
    mismatch = subprocess.CompletedProcess(
        ["osascript"],
        1,
        "",
        "execution error: tt_sidebar_source_mismatch (-2700)\n",
    )
    monkeypatch.setattr(ghostty_split.sys, "platform", "darwin")
    run = MagicMock(return_value=mismatch)
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")
    assert not ok
    assert "焦点窗格已切换" in message

    assert "未能收敛" in ghostty_split._failure_message("tt_sidebar_resize_timeout")

    run.side_effect = subprocess.TimeoutExpired(["osascript"], ghostty_split._PROCESS_TIMEOUT)
    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")
    assert not ok
    assert f"启动器超时（{ghostty_split._PROCESS_TIMEOUT:g}s）" in message


def test_run_split_rejects_non_macos(monkeypatch):
    monkeypatch.setattr(ghostty_split.sys, "platform", "linux")
    run = MagicMock()
    monkeypatch.setattr(ghostty_split.subprocess, "run", run)

    ok, message = ghostty_split._run_split("exec sidebar", "/cwd")

    assert not ok
    assert "仅支持 macOS" in message
    run.assert_not_called()
