"""Codex ``$tt-sidebar`` 的分屏启动器与提示词事件入口。

该模块由随包安装的用户级 Skill / Hook 通过当前 Python 解释器直接调用，避免依赖
项目源码路径、项目虚拟环境或 ``tt`` 是否恰好在 Hook 子进程的 ``PATH`` 中。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

_SPLIT_OK = "tt_sidebar_split_ok"
_PROCESS_TIMEOUT = 25


def _current_session_id() -> str:
    return (
        os.environ.get("TT_SIDEBAR_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or _kimi_session_id_for_cwd()
    ).strip()


def _kimi_session_id_for_cwd() -> str:
    """委托 adapters.kimi 的共享实现（workDir==cwd 且 updatedAt 最新；不限新鲜度——
    用户刚提交提示词触发 Skill，该会话必是最新）。"""
    from .adapters.kimi import current_session_id_for_cwd

    return current_session_id_for_cwd()


def _module_argv(action: str, *args: str) -> list[str]:
    return [sys.executable or "python3", "-B", "-m", "token_tracker.sidebar_command", action, *args]


def _sidebar_command(session_id: str) -> str:
    return f"exec {shlex.join(_module_argv('current', session_id))}"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=_PROCESS_TIMEOUT)


def _open_tmux(pane: str, tracked_session_id: str) -> tuple[bool, str]:
    result = _run(
        [
            "tmux",
            "split-window",
            "-h",
            "-p",
            "33",
            "-t",
            pane,
            "-c",
            os.getcwd(),
            _sidebar_command(tracked_session_id),
        ]
    )
    if result.returncode == 0:
        return True, "已在当前 tmux 会话右侧打开 1/3 宽度的 tt sidebar。"
    return False, (result.stderr or result.stdout or "tmux 分屏失败").strip()


def _open_iterm(iterm_session_id: str, tracked_session_id: str) -> tuple[bool, str]:
    result = _run(
        [
            sys.executable or "python3",
            "-B",
            "-m",
            "token_tracker.iterm_split",
            "--iterm-session-id",
            iterm_session_id.rpartition(":")[2],
            "--command",
            _sidebar_command(tracked_session_id),
        ]
    )
    if result.returncode == 0 and _SPLIT_OK in result.stdout:
        return True, "已在当前 iTerm2 会话右侧打开 1/3 宽度的 tt sidebar。"
    detail = (result.stderr or result.stdout or "iTerm2 分屏失败").strip()
    return False, detail


def open_split() -> int:
    """在发起 Skill 的终端窗格右侧打开当前会话专属 sidebar。"""
    try:
        tracked_session_id = _current_session_id()
        if not tracked_session_id:
            print("无法识别当前 Claude Code / Codex / Kimi Code 会话 ID。", file=sys.stderr)
            return 1
        if pane := os.environ.get("TMUX_PANE"):
            ok, message = _open_tmux(pane, tracked_session_id)
        elif iterm_session_id := os.environ.get("ITERM_SESSION_ID"):
            ok, message = _open_iterm(iterm_session_id, tracked_session_id)
        else:
            ok, message = False, "当前终端不受支持；tt-sidebar 仅支持 tmux 或 iTerm2。"
    except subprocess.TimeoutExpired:
        ok, message = False, f"tt-sidebar 分屏失败：启动器超时（{_PROCESS_TIMEOUT}s）"
    except (OSError, subprocess.SubprocessError) as exc:
        ok, message = False, f"tt-sidebar 分屏失败：{exc}"

    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def current_sessions(session_id: str):
    """按会话 ID 精确过滤，并保留目标会话的全部提示词。不限 agent——
    CC / Codex / Kimi 的 split 都走这里，按 session_id 过滤后天然只剩目标会话。"""
    from .sidebar import scan_sessions
    from .ui.sidebar import SPLIT_PROMPT_LIMIT

    return [
        session
        for session in scan_sessions(
            agent_ids=None, max_sessions=1000, max_prompts=SPLIT_PROMPT_LIMIT
        )
        if session.session_id == session_id
    ]


def run_current(session_id: str, once: bool = False) -> int:
    sessions = current_sessions(session_id)
    if once:
        if not sessions:
            print("未找到当前活跃会话，无法渲染单会话 sidebar 快照", file=sys.stderr)
            return 2
        from .ui.console import forced_color_console, get_console
        from .ui.sidebar import render_split_sidebar

        with forced_color_console():
            get_console().print(render_split_sidebar(sessions))
        return 0

    from . import config
    from .ui.sidebar_app import SidebarApp

    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    # 新会话可能先执行 $tt-sidebar，此时首扫没有普通提示词是正常状态。保持空态常驻，
    # 后续首条 UserPromptSubmit 事件会直接挂载目标会话。
    SidebarApp(
        variant="split",
        initial_sessions=sessions,
        prompt_session_id=session_id,
        prompt_channel_dir=config.CONFIG_DIR,
    ).run()
    return 0


def run_prompt_hook(agent_id: str) -> int:
    """把 Codex / Claude Code / Kimi 的 UserPromptSubmit stdin 尽力推送到当前 split。"""
    from . import config
    from .sidebar_events import prompt_event_from_hook, send_prompt_event

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    event = prompt_event_from_hook(data, agent_id)
    if event is not None:
        send_prompt_event(event, channel_dir=config.CONFIG_DIR)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("split", add_help=False)
    current = subparsers.add_parser("current")
    current.add_argument("session_id")
    current.add_argument("--once", action="store_true")
    prompt_hook = subparsers.add_parser("prompt-hook")
    prompt_hook.add_argument("--agent", choices=("claude-code", "codex", "kimi"), required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.action == "split":
        return open_split()
    if args.action == "current":
        return run_current(args.session_id, once=args.once)
    return run_prompt_hook(args.agent)


if __name__ == "__main__":
    raise SystemExit(main())
