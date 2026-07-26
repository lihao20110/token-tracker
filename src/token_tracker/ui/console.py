"""全局 Rich console 持有者。

所有渲染都通过 get_console() 取当前 console，而不是各模块各自 import 一个全局名，
这样交互式 dashboard 截屏时用 capture_console() 临时换成定宽缓冲 console，
对拆分到多个 ui 子模块的渲染函数同时生效（避免猴补丁只改一个模块全局的老问题）。
"""

import contextlib
import os
import sys
from collections.abc import Iterator
from io import StringIO

from rich.console import Console


def _platform_name() -> str:
    return os.name


def _configure_windows_stdout_utf8() -> None:
    """Prevent Rich Unicode glyphs from failing on a legacy Windows code page."""
    if _platform_name() != "nt":
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass


_console = Console()


def get_console() -> Console:
    return _console


@contextlib.contextmanager
def capture_console(width: int) -> Iterator[StringIO]:
    """临时把全局 console 换成定宽、写入缓冲区的 console，yield 该缓冲区。"""
    global _console
    prev = _console
    buf = StringIO()
    _console = Console(file=buf, width=width, force_terminal=True)
    try:
        yield buf
    finally:
        _console = prev


# Claude Code 渲染 `!` 命令输出时整体左缩进约这么多列（gutter），按真实终端宽度渲染
# 会让最右列被挤到下一行，故从探测到的宽度里扣掉它，留出安全余量、保证不折行。
_CC_GUTTER = 4


def _forced_width() -> int | None:
    """`!` 非 tty 场景下尽力探测真实终端宽度，供 force_terminal 的 Console 用。

    仅在 Rich 自身拿不到时才补救：设了**有效正整数** COLUMNS、或真 tty 能自测宽度，都返回
    None 交回 Rich（尊重用户意图与自动探测）。注意 Claude Code 的 `!` 子进程会把 COLUMNS
    设成占位的 "0"，这不是有效宽度，必须忽略、继续探测——否则会被它短路、白白回落 80。
    探测途径：该子进程脱离了控制终端，/dev/tty 与 fd 0/1/2 都失败，改从 shell 留在环境里的
    真实设备路径（powerlevel10k 的 _P9K_TTY、SSH 的 SSH_TTY）直接 ioctl 取宽度，再扣掉
    Claude Code 显示区的左缩进 gutter；都拿不到则回落 None（Rich 自行回落 80）。
    """
    cols_env = os.environ.get("COLUMNS")
    if cols_env and cols_env.isdigit() and int(cols_env) > 0:
        return None
    try:
        os.get_terminal_size(1)
        return None
    except OSError:
        pass
    try:
        import fcntl
        import struct
        import termios
    except ImportError:  # 非 Unix（Windows）
        return None
    for var in ("_P9K_TTY", "_P9K_SSH_TTY", "SSH_TTY"):
        path = os.environ.get(var)
        if not path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                _, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))  # type: ignore[attr-defined]
            finally:
                os.close(fd)
        except OSError:
            continue
        if cols > 0:
            return cols - _CC_GUTTER
    return None


@contextlib.contextmanager
def forced_color_console() -> Iterator[None]:
    """临时把全局 console 换成强制 24-bit 彩色、输出到 stdout 的 console。

    用于 `!tt daily` 这种「非 tty 但要彩色」的场景：Claude Code 的 bash 模式会渲染
    命令输出的 ANSI，但 Rich 默认在非 tty 下关色，故需 force_terminal + truecolor。
    复用的 header/summary 渲染都走 get_console()，因此一并彩色。尊重 NO_COLOR。
    宽度经 _forced_width() 探测真实终端，窄屏才能按真实宽度裁周、不折行。
    """
    global _console
    prev = _console
    _console = Console(
        force_terminal=True,
        color_system="truecolor",
        width=_forced_width(),
        no_color=bool(os.environ.get("NO_COLOR")),
    )
    try:
        yield
    finally:
        _console = prev
