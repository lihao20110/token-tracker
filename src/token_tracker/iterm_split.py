"""用 iTerm2 Python API 创建右侧 1/3 分屏并直接启动当前会话 sidebar。"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import signal
import sys
from collections.abc import Awaitable
from typing import TypeVar

import iterm2

_SPLIT_OK = "tt_sidebar_split_ok"
_result: tuple[bool, str] = (False, "iTerm2 Python API 未执行")
_SPLIT_TIMEOUT = 8.0
_LAYOUT_TIMEOUT = 3.0
_REFRESH_TIMEOUT = 3.0
_TOTAL_TIMEOUT = 20
_T = TypeVar("_T")


def _frame_snapshot(frame: iterm2.util.Frame) -> tuple[int, int, int, int]:
    """复制不可变几何值；iTerm2 会原地更新旧 Frame 对象，不能直接留引用比较。"""
    return (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)


def _frame_from_snapshot(snapshot: tuple[int, int, int, int]) -> iterm2.util.Frame:
    x, y, width, height = snapshot
    return iterm2.util.Frame(
        origin=iterm2.util.Point(x, y),
        size=iterm2.util.Size(width, height),
    )


def _command_profile(command: str, target_columns: int) -> iterm2.LocalWriteOnlyProfile:
    """静默等 pane 收敛到目标宽度后再启动 sidebar，避免半宽首帧与临时 shell。"""
    column_limit = max(1, target_columns + 2)
    gated_command = (
        f"typeset -i _tt_limit={column_limit}; "
        "for _tt_i in {1..200}; do "
        "(( COLUMNS <= _tt_limit )) && break; "
        "sleep 0.01; "
        "done; "
        f"{command}"
    )
    profile = iterm2.LocalWriteOnlyProfile()
    profile.set_use_custom_command("Yes")
    profile.set_command(f"/bin/zsh -lc {shlex.quote(gated_command)}")
    return profile


async def _await_stage(label: str, awaitable: Awaitable[_T], timeout: float) -> _T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError(f"{label}超时（{timeout:g}s）") from exc


async def _restore_geometry(
    window: iterm2.Window,
    frame: tuple[int, int, int, int],
    fullscreen: bool,
) -> None:
    current_fullscreen = await window.async_get_fullscreen()
    if current_fullscreen != fullscreen:
        await window.async_set_fullscreen(fullscreen)
    if not fullscreen and _frame_snapshot(await window.async_get_frame()) != frame:
        await window.async_set_frame(_frame_from_snapshot(frame))


async def _resize_one_third(source: iterm2.Session, sidebar: iterm2.Session) -> None:
    """把同一水平 split 调整为 2:1。"""
    tab = source.tab
    if tab is None:
        raise RuntimeError("找不到分屏所属的 iTerm2 tab")
    pane_width = source.grid_size.width + sidebar.grid_size.width
    sidebar_width = max(1, pane_width // 3)
    source.preferred_size = iterm2.util.Size(
        width=max(1, pane_width - sidebar_width),
        height=source.grid_size.height,
    )
    sidebar.preferred_size = iterm2.util.Size(
        width=sidebar_width,
        height=sidebar.grid_size.height,
    )
    await tab.async_update_layout()


async def _layout_ok(
    source: iterm2.Session,
    sidebar: iterm2.Session,
    original_frame: tuple[int, int, int, int],
    original_fullscreen: bool,
) -> bool:
    if source.window is None:
        return False
    pane_width = source.grid_size.width + sidebar.grid_size.width
    ratio_ok = pane_width > 0 and abs(sidebar.grid_size.width / pane_width - 1 / 3) <= 0.03
    frame_ok = original_fullscreen or _frame_snapshot(await source.window.async_get_frame()) == original_frame
    fullscreen_ok = await source.window.async_get_fullscreen() == original_fullscreen
    return ratio_ok and frame_ok and fullscreen_ok


async def _finalize_layout(
    connection: iterm2.Connection,
    source: iterm2.Session,
    sidebar: iterm2.Session,
    original_frame: tuple[int, int, int, int],
    original_fullscreen: bool,
) -> None:
    """在短事务里一次提交比例、窗口状态与焦点；pane 必须已在事务外创建。"""
    if source.window is None:
        raise RuntimeError("找不到分屏所属的 iTerm2 窗口")
    async with iterm2.Transaction(connection):
        await _await_stage("调整 1/3 布局", _resize_one_third(source, sidebar), _LAYOUT_TIMEOUT)
        await _await_stage(
            "恢复窗口状态",
            _restore_geometry(source.window, original_frame, original_fullscreen),
            _LAYOUT_TIMEOUT,
        )
        await _await_stage(
            "恢复原窗格焦点",
            source.async_activate(select_tab=True, order_window_front=False),
            _LAYOUT_TIMEOUT,
        )


async def _rollback_sidebar(sidebar: iterm2.Session | None) -> None:
    if sidebar is None:
        return
    try:
        await asyncio.wait_for(sidebar.async_close(force=True), timeout=2)
    except Exception:
        pass


async def _split(connection: iterm2.Connection) -> None:
    global _result

    args = _parse_args()
    sidebar: iterm2.Session | None = None
    try:
        app = await _await_stage("连接 iTerm2", iterm2.async_get_app(connection), _REFRESH_TIMEOUT)
        if app is None:
            raise RuntimeError("无法读取 iTerm2 应用状态")
        source = app.get_session_by_id(args.iterm_session_id)
        if source is None or source.window is None or source.tab is None:
            raise RuntimeError("找不到发起命令的 iTerm2 会话窗格")

        window = source.window
        original_frame = _frame_snapshot(
            await _await_stage("读取窗口尺寸", window.async_get_frame(), _REFRESH_TIMEOUT)
        )
        original_fullscreen = await _await_stage(
            "读取全屏状态", window.async_get_fullscreen(), _REFRESH_TIMEOUT
        )
        profile = await _await_stage("读取 iTerm2 profile", source.async_get_profile(), _REFRESH_TIMEOUT)

        # split completion 依赖 iTerm2 主循环，放进 Transaction 会永久等待。在事务外创建
        # pane，随后立刻用短事务提交最终布局；custom command 在列数收敛前静默等待。
        target_columns = max(1, source.grid_size.width // 3)
        sidebar = await _await_stage(
            "创建右侧分屏",
            source.async_split_pane(
                vertical=True,
                before=False,
                profile=profile.name,
                profile_customizations=_command_profile(args.command, target_columns),
            ),
            _SPLIT_TIMEOUT,
        )
        sidebar_id = sidebar.session_id

        for _ in range(2):
            await _finalize_layout(
                connection,
                source,
                sidebar,
                original_frame,
                original_fullscreen,
            )
            await _await_stage("刷新分屏状态", app.async_refresh(), _REFRESH_TIMEOUT)
            source = app.get_session_by_id(args.iterm_session_id)
            sidebar = app.get_session_by_id(sidebar_id)
            if source is None or sidebar is None:
                raise RuntimeError("新建分屏后无法刷新 iTerm2 session")
            if await _await_stage(
                "验证分屏布局",
                _layout_ok(source, sidebar, original_frame, original_fullscreen),
                _REFRESH_TIMEOUT,
            ):
                break
        else:
            raise RuntimeError("iTerm2 无法同时保持窗口状态与右侧 1/3 布局")

        _result = (True, _SPLIT_OK)
    except Exception as exc:
        await _rollback_sidebar(sidebar)
        _result = (False, f"iTerm2 分屏失败：{exc}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterm-session-id", required=True)
    parser.add_argument("--command", required=True)
    return parser.parse_args()


def main() -> int:
    def timeout_handler(_signum, _frame) -> None:
        raise TimeoutError(f"iTerm2 启动器超时（{_TOTAL_TIMEOUT}s）")

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(_TOTAL_TIMEOUT)
    try:
        iterm2.run_until_complete(_split, retry=False)
    except SystemExit as exc:
        return int(exc.code or 1)
    except TimeoutError as exc:
        global _result
        _result = (False, f"iTerm2 分屏失败：{exc}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
    ok, message = _result
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
