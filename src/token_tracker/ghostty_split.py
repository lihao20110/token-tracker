"""用系统 ``osascript`` 在 Ghostty（≥ 1.3.0，macOS）右侧创建 1/3 分屏并启动当前会话 sidebar。

与 iTerm2 后端的差异：Ghostty 的 AppleScript 字典（1.3.0 起）不暴露窗格列宽，
``split`` 只支持默认平分；宽度收敛靠新窗格内的 ``stty -f /dev/tty size`` 后台回写
列数文件 + ``perform action "resize_split:方向,像素"`` 闭环逼近 1/3——平分后新窗格
恰为源窗格一半，目标列数直接取 ``2 * c0 div 3``（c0 为平分列数），启动器无需探测
终端宽度（沙箱 / 管道下 /dev/tty 未必可用）。实测（Ghostty 1.3.1）
``resize_split:X,N`` 是「朝 X 方向扩展」，右栏缩小用 ``right``；脚本仍按回读列数
自适应翻转方向，不依赖该语义保持稳定。新窗格命令带同口径列数门控，宽度收敛前不
exec sidebar，避免 Textual 半宽首帧。
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import uuid

_OSASCRIPT = "/usr/bin/osascript"
_SPLIT_OK = "tt_sidebar_split_ok"
_SPLIT_OK_RAW = "tt_sidebar_split_ok_raw"
_PROCESS_TIMEOUT = 10.0
_PROBE_TIMEOUT = 5.0
_MIN_GHOSTTY_VERSION = "1.3"
# 探针只取 version 属性，仍需编译期加载 Ghostty 术语字典，沙箱下同样报 -2741，
# 因此可在跑整段脚本前精确区分「Codex 沙箱读不到字典」与「版本过低」。
_PROBE_SCRIPT = 'tell application "Ghostty" to get version'

# 动态命令、工作目录与列数文件只通过 argv 进入脚本，不插值到 AppleScript 源码。
# 注意 `before` 是 AppleScript 保留字，循环变量命名须避开。
_APPLESCRIPT = r"""
on run argv
    set launchCommand to item 1 of argv
    set workDir to item 2 of argv
    set colsFile to item 3 of argv

    tell application "Ghostty"
        if my versionBelow(version, "1.3") then error "tt_sidebar_ghostty_version"

        set src to focused terminal of selected tab of front window
        set srcDir to working directory of src
        if srcDir is not "" and srcDir is not workDir then error "tt_sidebar_source_mismatch"

        set cfg to new surface configuration
        set initial working directory of cfg to workDir
        set command of cfg to launchCommand
        set newTerm to missing value

        try
            set newTerm to split src direction right with configuration cfg

            delay 0.4
            set c0 to 0
            repeat 20 times
                set c0 to my readCols(colsFile)
                if c0 > 0 then exit repeat
                delay 0.05
            end repeat

            if c0 is 0 then
                focus src
                return "tt_sidebar_split_ok_raw"
            end if

            set targetColumns to (2 * c0) div 3
            set shrinkDir to "right"
            set prevCols to -1
            set converged to false
            repeat with attempt from 1 to 60
                set curCols to my readCols(colsFile)
                if curCols > 0 and curCols <= targetColumns + 1 then
                    set converged to true
                    exit repeat
                end if
                if curCols > 0 then
                    if prevCols > 0 and curCols > prevCols then
                        if shrinkDir is "right" then
                            set shrinkDir to "left"
                        else
                            set shrinkDir to "right"
                        end if
                        set prevCols to -1
                    else
                        set prevCols to curCols
                        perform action ("resize_split:" & shrinkDir & ",60") on newTerm
                    end if
                end if
                delay 0.06
            end repeat
            if not converged then error "tt_sidebar_resize_timeout"

            focus src
            return "tt_sidebar_split_ok"
        on error errorMessage number errorNumber
            try
                if newTerm is not missing value then close newTerm
            end try
            try
                focus src
            end try
            error errorMessage number errorNumber
        end try
    end tell
end run

on readCols(colsFile)
    try
        set sizeText to read POSIX file colsFile as text
        return (word 2 of sizeText) as integer
    on error
        return 0
    end try
end readCols

on versionBelow(currentVersion, minVersion)
    try
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to "."
        set curParts to text items of currentVersion
        set minParts to text items of minVersion
        set AppleScript's text item delimiters to oldDelims
        repeat with i from 1 to (count of minParts)
            set curNum to 0
            if i <= (count of curParts) then
                try
                    set curNum to (item i of curParts) as integer
                on error
                    set curNum to 0
                end try
            end if
            set minNum to (item i of minParts) as integer
            if curNum > minNum then return false
            if curNum < minNum then return true
        end repeat
        return false
    on error
        return false
    end try
end versionBelow
"""


def _wrapper_script(command: str, cols_file: str) -> str:
    """新窗格启动脚本：后台循环回写 ``行 列`` 尺寸文件供 AppleScript 收敛宽度，
    并按平分半宽推导 1/3 目标门控（宽度收敛前不 exec sidebar），避免 Textual 半宽首帧。

    Ghostty 实际以 ``shell -c "exec -l <command>"`` 启动（实测 1.3.1），``command``
    只能是单条简单命令，复合 shell 语法会在 ``exec -l`` 后语法报错，因此包装逻辑
    必须落盘成脚本文件、``command`` 只传 ``/bin/sh <脚本>``。
    """
    return f"""#!/bin/sh
( i=0; while [ $i -lt 200 ]; do stty -f /dev/tty size > {shlex.quote(cols_file)} 2>/dev/null; i=$((i+1)); sleep 0.05; done ) &
set -- $(stty -f /dev/tty size 2>/dev/null)
if [ -n "$2" ]; then
    _tt_limit=$(( $2 * 2 / 3 + 1 ))
    i=0
    while [ $i -lt 200 ]; do
        set -- $(stty -f /dev/tty size 2>/dev/null)
        [ -n "$2" ] && [ "$2" -le "$_tt_limit" ] && break
        i=$((i+1)); sleep 0.05
    done
fi
rm -f "$0" {shlex.quote(cols_file)}
{command}
"""



def _osascript_argv(command: str, cwd: str, cols_file: str) -> list[str]:
    return [_OSASCRIPT, "-e", _APPLESCRIPT, "--", command, cwd, cols_file]


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """解析 ``1.3.1`` 形式版本号；解析不出返回 ``None``（按未知处理，不阻断）。"""
    try:
        return tuple(int(piece) for piece in version.strip().split()[0].split("."))
    except (ValueError, IndexError):
        return None


def _probe_ghostty() -> tuple[bool, str]:
    """分屏前置探针：跑整段脚本前用最小脚本区分沙箱 / 版本过低 / 未授权。

    Codex 沙箱会让整段脚本在编译期死于 -2741，脚本内的版本检查根本没机会执行，
    只能把「沙箱」与「版本低」混报；探针先把两者拆开，给出可行动的准确文案。
    探针自身失败（osascript 不存在 / 超时）不阻断主流程，交给主脚本报原始错误。
    """
    try:
        result = subprocess.run(
            [_OSASCRIPT, "-e", _PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True, ""
    output = result.stderr or result.stdout
    if result.returncode != 0:
        lowered = output.lower()
        if "-2741" in output or "expected end of line but found identifier" in lowered:
            return False, (
                "Ghostty 分屏失败：当前命令运行在 Codex 沙箱中，osascript 无法读取 Ghostty 的 "
                "AppleScript 术语字典。请以沙箱外权限（require_escalated）重跑 $tt-sidebar。"
            )
        return False, _failure_message(output)
    version = _version_tuple(result.stdout)
    minimum = _version_tuple(_MIN_GHOSTTY_VERSION)
    if version is not None and minimum is not None and version < minimum:
        return False, (
            f"Ghostty 分屏失败：当前版本 {result.stdout.strip()}，AppleScript 分屏需要 Ghostty "
            f"≥ {_MIN_GHOSTTY_VERSION}.0，请升级后重试。"
        )
    return True, ""


def _failure_message(output: str) -> str:
    lowered = output.lower()
    if "tt_sidebar_ghostty_version" in output:
        return f"Ghostty 分屏失败：AppleScript 分屏需要 Ghostty ≥ {_MIN_GHOSTTY_VERSION}.0，请升级后重试。"
    if "tt_sidebar_source_mismatch" in output:
        return "Ghostty 分屏失败：焦点窗格已切换，请在发起会话的窗格中重新执行 $tt-sidebar。"
    if "tt_sidebar_resize_timeout" in output:
        return "Ghostty 分屏失败：右侧 1/3 宽度未能收敛，已关闭本次新窗格。"
    if "-1743" in output or "not authorized" in lowered or "未获授权" in output:
        return (
            "Ghostty 分屏失败：macOS 未授权自动化控制。请在“系统设置 → 隐私与安全性 → 自动化”"
            "中允许运行 tt 的应用控制 Ghostty。"
        )
    if "-2741" in output or "expected end of line but found identifier" in lowered:
        return (
            f"Ghostty 分屏失败：无法读取 Ghostty 的 AppleScript 术语字典（Codex 沙箱限制或 Ghostty "
            f"低于 {_MIN_GHOSTTY_VERSION}.0）。请允许 $tt-sidebar 在沙箱外运行并确认 Ghostty 版本。"
        )
    if "-10814" in output or "application isn’t running" in lowered:
        return "Ghostty 分屏失败：无法连接 Ghostty；请确认 Ghostty 已安装并正在运行。"

    detail = output.strip()
    if "execution error:" in detail:
        detail = detail.rsplit("execution error:", 1)[1].strip()
    return f"Ghostty 分屏失败：{detail or 'osascript 未返回错误详情'}"


def _run_split(command: str, cwd: str) -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "Ghostty 自动分屏仅支持 macOS（Linux 下请在 tmux 中使用 $tt-sidebar）。"
    ok, message = _probe_ghostty()
    if not ok:
        return False, message
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp_dir = tempfile.gettempdir()
    cols_file = os.path.join(tmp_dir, f"tt-ghostty-cols-{token}.txt")
    script_file = os.path.join(tmp_dir, f"tt-ghostty-launch-{token}.sh")
    with open(script_file, "w", encoding="utf-8") as script:
        script.write(_wrapper_script(command, cols_file))
    os.chmod(script_file, 0o700)
    try:
        result = subprocess.run(
            _osascript_argv(f"/bin/sh {shlex.quote(script_file)}", cwd, cols_file),
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"Ghostty 分屏失败：启动器超时（{_PROCESS_TIMEOUT:g}s）。"
    except OSError as exc:
        return False, f"Ghostty 分屏失败：无法执行系统 osascript：{exc}"
    finally:
        # 正常路径下脚本与新窗格命令已在 exec sidebar 前自删；这里兜底清理未启动的残留。
        for leftover in (script_file, cols_file):
            try:
                os.remove(leftover)
            except OSError:
                pass

    if result.returncode == 0 and _SPLIT_OK in result.stdout:
        if _SPLIT_OK_RAW in result.stdout:
            return True, _SPLIT_OK_RAW
        return True, _SPLIT_OK
    return False, _failure_message(result.stderr or result.stdout)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ok, message = _run_split(args.command, args.cwd)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
