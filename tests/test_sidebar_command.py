from __future__ import annotations

import io
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from token_tracker import sidebar_command


def test_current_session_id_prefers_explicit_then_codex(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex")
    assert sidebar_command._current_session_id() == "codex"
    monkeypatch.setenv("TT_SIDEBAR_SESSION_ID", "explicit")
    assert sidebar_command._current_session_id() == "explicit"


def test_sidebar_command_uses_installed_module_not_project_path(monkeypatch):
    monkeypatch.setattr(sidebar_command.sys, "executable", "/opt/tt env/bin/python")
    command = sidebar_command._sidebar_command("thread-1")
    assert "token_tracker.sidebar_command current thread-1" in command
    assert "token-tracker/.agents" not in command
    assert "/opt/tt env/bin/python" in command


def test_current_sessions_scans_only_codex_and_keeps_all_prompts(monkeypatch):
    from token_tracker import sidebar

    target = SimpleNamespace(session_id="target")
    other = SimpleNamespace(session_id="other")
    scan = MagicMock(return_value=[target, other])
    monkeypatch.setattr(sidebar, "scan_sessions", scan)

    assert sidebar_command.current_sessions("target") == [target]
    assert scan.call_args.kwargs == {
        "agent_ids": {"codex"}, "max_sessions": 1000, "max_prompts": None,
    }


def test_open_tmux_requests_right_one_third(monkeypatch, tmp_path):
    succeeded = subprocess.CompletedProcess(["tmux"], 0, "", "")
    run = MagicMock(return_value=succeeded)
    monkeypatch.setattr(sidebar_command, "_run", run)
    monkeypatch.chdir(tmp_path)

    ok, _message = sidebar_command._open_tmux("%7", "thread")

    assert ok
    argv = run.call_args.args[0]
    assert argv[:6] == ["tmux", "split-window", "-h", "-p", "33", "-t"]
    assert argv[6:9] == ["%7", "-c", str(tmp_path)]


def test_open_iterm_runs_packaged_worker_with_same_python(monkeypatch):
    succeeded = subprocess.CompletedProcess(["iterm"], 0, "tt_sidebar_split_ok", "")
    run = MagicMock(return_value=succeeded)
    monkeypatch.setattr(sidebar_command, "_run", run)
    monkeypatch.setattr(sidebar_command.sys, "executable", "/venv/bin/python")

    ok, _message = sidebar_command._open_iterm("w0t0:session-uuid", "thread")

    assert ok
    argv = run.call_args.args[0]
    assert argv[:5] == ["/venv/bin/python", "-B", "-m", "token_tracker.iterm_split", "--iterm-session-id"]
    assert argv[5] == "session-uuid"


def test_run_current_stays_open_before_first_prompt(tmp_path, monkeypatch):
    from token_tracker import config
    from token_tracker.ui import sidebar_app

    app = MagicMock()
    monkeypatch.setattr(sidebar_command, "current_sessions", lambda _session_id: [])
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    constructor = MagicMock(return_value=app)
    monkeypatch.setattr(sidebar_app, "SidebarApp", constructor)

    assert sidebar_command.run_current("new-session") == 0
    constructor.assert_called_once_with(
        variant="split",
        initial_sessions=[],
        prompt_session_id="new-session",
        prompt_channel_dir=str(tmp_path),
    )
    app.run.assert_called_once_with()


def test_prompt_hook_pushes_codex_event_to_config_channel(tmp_path, monkeypatch):
    from token_tracker import config, sidebar_events

    payload = (
        '{"hook_event_name":"UserPromptSubmit","session_id":"s1",'
        '"prompt":"hello","cwd":"/tmp/p","model":"gpt-5","turn_id":"t1"}'
    )
    send = MagicMock()
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sidebar_events, "send_prompt_event", send)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    assert sidebar_command.run_prompt_hook("codex") == 0
    event = send.call_args.args[0]
    assert (event.session_id, event.prompt, event.turn_id) == ("s1", "hello", "t1")
    assert send.call_args.kwargs == {"channel_dir": str(tmp_path)}


def test_open_split_rejects_unsupported_terminal(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    assert sidebar_command.open_split() == 1
    assert "仅支持 tmux 或 iTerm2" in capsys.readouterr().err


def test_main_routes_actions(monkeypatch):
    monkeypatch.setattr(sidebar_command, "open_split", lambda: 7)
    monkeypatch.setattr(sidebar_command, "run_current", lambda sid, once=False: 8 if (sid, once) == ("s", True) else 0)
    monkeypatch.setattr(sidebar_command, "run_prompt_hook", lambda agent: 9 if agent == "codex" else 0)

    assert sidebar_command.main(["split"]) == 7
    assert sidebar_command.main(["current", "s", "--once"]) == 8
    assert sidebar_command.main(["prompt-hook", "--agent", "codex"]) == 9
