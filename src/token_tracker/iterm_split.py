"""用系统 ``osascript`` 创建 iTerm2 右侧 1/3 分屏并启动当前会话 sidebar。"""

from __future__ import annotations

import argparse
import subprocess
import sys

_OSASCRIPT = "/usr/bin/osascript"
_SPLIT_OK = "tt_sidebar_split_ok"
_PROCESS_TIMEOUT = 8.0

# 动态 session UUID 与命令只通过 argv 进入脚本，不能插值到 AppleScript 源码。
# iTerm2 的 `set columns` 会先改变窗口宽度：默认平分后把新 pane 调为源 pane
# 的一半，再恢复原 bounds，等比放大后即得到 2:1（真机 107 → 71:35）。
_APPLESCRIPT = r"""
on run argv
    set targetId to item 1 of argv
    set launchCommand to item 2 of argv

    tell application "iTerm2"
        set sourceSession to missing value
        set sourceTab to missing value
        set sourceWindow to missing value

        repeat with currentWindow in windows
            repeat with currentTab in tabs of currentWindow
                repeat with currentSession in sessions of currentTab
                    if id of currentSession is targetId then
                        set sourceSession to currentSession
                        set sourceTab to currentTab
                        set sourceWindow to currentWindow
                        exit repeat
                    end if
                end repeat
                if sourceSession is not missing value then exit repeat
            end repeat
            if sourceSession is not missing value then exit repeat
        end repeat

        if sourceSession is missing value then error "tt_sidebar_source_not_found"

        set currentBounds to bounds of sourceWindow
        set originalBounds to {item 1 of currentBounds, item 2 of currentBounds, item 3 of currentBounds, item 4 of currentBounds}
        set originalZoomed to zoomed of sourceWindow
        set sidebarSession to missing value

        try
            tell sourceWindow to select sourceTab
            select sourceSession
            set originalColumns to columns of sourceSession
            set columnLimit to (originalColumns div 3) + 2
            if columnLimit < 1 then set columnLimit to 1
            set gatedCommand to "typeset -i _tt_limit=" & (columnLimit as text) & "; for _tt_i in {1..200}; do (( COLUMNS <= _tt_limit )) && break; sleep 0.01; done; " & launchCommand
            set wrappedCommand to "/bin/zsh -lc " & quoted form of gatedCommand

            tell sourceSession
                set sidebarSession to split vertically with same profile command wrappedCommand
            end tell

            set splitReady to false
            repeat with attempt from 1 to 200
                set splitSourceColumns to columns of sourceSession
                set splitSidebarColumns to columns of sidebarSession
                set splitDelta to splitSourceColumns - splitSidebarColumns
                if splitDelta < 0 then set splitDelta to -splitDelta
                if splitSourceColumns > 0 and splitSidebarColumns > 0 and splitDelta <= 2 then
                    set splitReady to true
                    exit repeat
                end if
                delay 0.01
            end repeat
            if splitReady is false then error "tt_sidebar_split_timeout"

            set targetColumns to splitSourceColumns div 2
            if targetColumns < 1 then set targetColumns to 1
            set columns of sidebarSession to targetColumns

            set resizeReady to false
            repeat with attempt from 1 to 200
                set resizedColumns to columns of sidebarSession
                if resizedColumns >= targetColumns - 1 and resizedColumns <= targetColumns + 1 then
                    set resizeReady to true
                    exit repeat
                end if
                delay 0.01
            end repeat
            if resizeReady is false then error "tt_sidebar_resize_timeout"

            set bounds of sourceWindow to originalBounds
            if (zoomed of sourceWindow) is not originalZoomed then set zoomed of sourceWindow to originalZoomed
            tell sourceWindow to select sourceTab
            select sourceSession

            set layoutReady to false
            repeat with attempt from 1 to 200
                set finalBounds to bounds of sourceWindow
                set sourceColumns to columns of sourceSession
                set sidebarColumns to columns of sidebarSession
                set totalColumns to sourceColumns + sidebarColumns
                set boundsReady to item 1 of finalBounds = item 1 of originalBounds and item 2 of finalBounds = item 2 of originalBounds and item 3 of finalBounds = item 3 of originalBounds and item 4 of finalBounds = item 4 of originalBounds
                set zoomReady to (zoomed of sourceWindow) is originalZoomed
                if totalColumns > 0 then
                    set ratioReady to sidebarColumns * 100 >= totalColumns * 30 and sidebarColumns * 100 <= totalColumns * 36
                else
                    set ratioReady to false
                end if
                if boundsReady and zoomReady and ratioReady then
                    set layoutReady to true
                    exit repeat
                end if
                delay 0.01
            end repeat
            if layoutReady is false then error "tt_sidebar_layout_timeout"

            tell sourceWindow to select sourceTab
            select sourceSession
            return "tt_sidebar_split_ok"
        on error errorMessage number errorNumber
            try
                if sidebarSession is not missing value then close sidebarSession
            end try
            try
                set bounds of sourceWindow to originalBounds
                if (zoomed of sourceWindow) is not originalZoomed then set zoomed of sourceWindow to originalZoomed
                tell sourceWindow to select sourceTab
                select sourceSession
            end try
            error errorMessage number errorNumber
        end try
    end tell
end run
"""


def _osascript_argv(session_id: str, command: str) -> list[str]:
    return [_OSASCRIPT, "-e", _APPLESCRIPT, "--", session_id, command]


def _failure_message(output: str) -> str:
    lowered = output.lower()
    if "tt_sidebar_source_not_found" in output:
        return (
            "iTerm2 分屏失败：找不到发起命令的 iTerm2 会话窗格；"
            "请在原窗格重新执行 $tt-sidebar。"
        )
    if "tt_sidebar_split_timeout" in output:
        return "iTerm2 分屏失败：创建右侧窗格超时，未保留未完成的分屏。"
    if "tt_sidebar_resize_timeout" in output:
        return (
            "iTerm2 分屏失败：当前窗口无法调整窗格宽度；macOS 原生全屏下请先退出全屏再重试。"
            "已关闭本次新窗格并恢复原窗口。"
        )
    if "tt_sidebar_layout_timeout" in output:
        return "iTerm2 分屏失败：右侧 1/3 布局未能收敛，已关闭本次新窗格并恢复原窗口。"
    if "-1743" in output or "not authorized" in lowered or "未获授权" in output:
        return (
            "iTerm2 分屏失败：macOS 未授权自动化控制。请在“系统设置 → 隐私与安全性 → 自动化”"
            "中允许运行 tt 的应用控制 iTerm2。"
        )
    if "-10814" in output or "application isn’t running" in lowered:
        return "iTerm2 分屏失败：无法连接 iTerm2；请确认 iTerm2 已安装并正在运行。"

    detail = output.strip()
    if "execution error:" in detail:
        detail = detail.rsplit("execution error:", 1)[1].strip()
    return f"iTerm2 分屏失败：{detail or 'osascript 未返回错误详情'}"


def _run_split(session_id: str, command: str) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "iTerm2 自动分屏仅支持 macOS。"
    try:
        result = subprocess.run(
            _osascript_argv(session_id, command),
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"iTerm2 分屏失败：启动器超时（{_PROCESS_TIMEOUT:g}s）。"
    except OSError as exc:
        return False, f"iTerm2 分屏失败：无法执行系统 osascript：{exc}"

    if result.returncode == 0 and _SPLIT_OK in result.stdout:
        return True, _SPLIT_OK
    return False, _failure_message(result.stderr or result.stdout)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterm-session-id", required=True)
    parser.add_argument("--command", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ok, message = _run_split(args.iterm_session_id, args.command)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
