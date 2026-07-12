"""tt sidebar 的 Textual 壳：备用屏常驻 + 滚动 + 定时刷新。

数据与渲染完全复用 `sidebar.scan_sessions` + `ui.sidebar.render_sidebar`——Rich renderable
直接塞进 Static 整帧更新，滚动位置由 VerticalScroll 容器保持（滚轮 / 方向键 / PgUp/PgDn）。
配色继承终端：`ansi_color=True` + `ansi-dark/light` 主题让 app chrome（背景/滚动条/Footer）
走终端 ANSI 调色板、不糊 Textual 自己的深色底；正文内容色仍由 tt 主题（`_S` 运行时代理）给，
`tt sidebar --theme <名>` 可临时切。明暗跟随 tt 主题的 is_light。
本模块 import textual，cli 只在 live 模式延迟 import（照 questionary 先例，日常 tt 启动不加载）。
点击跳转到对应终端窗格（需 statusline 携带 ITERM_SESSION_ID / TMUX_PANE 映射）留下一迭代。
"""

import subprocess

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from .. import config
from ..i18n import t
from ..sidebar import scan_sessions, terminal_info
from .sidebar import render_sidebar
from .themes import get_theme

REFRESH_SECONDS = 5.0


def _jump_argvs(info: dict) -> list[list[str]] | None:
    """终端定位 → 跳转命令序列；无可用定位返回 None。

    tmux 优先（零授权、pane 级精确）：select-window 接受 pane 目标、会选中其所在 window。
    iTerm2 走 AppleScript：ITERM_SESSION_ID 形如 "w0t3p0:<UUID>"，session 的 AppleScript
    `id` 即冒号后的 UUID；逐层匹配后选中 tab/session 并把窗口带到最前（首次触发 macOS
    自动化授权弹窗属预期）。
    """
    if info.get("tmux"):
        pane = info["tmux"]
        return [["tmux", "select-window", "-t", pane], ["tmux", "select-pane", "-t", pane]]
    if info.get("iterm"):
        uuid = info["iterm"].rpartition(":")[2]
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with tb in tabs of w
                    repeat with s in sessions of tb
                        if id of s is "{uuid}" then
                            tell w to select tb
                            select s
                            set index of w to 1
                            activate
                            return
                        end if
                    end repeat
                end repeat
            end repeat
        end tell'''
        return [["osascript", "-e", script]]
    return None


class SidebarApp(App[None]):
    BINDINGS = [
        ("q", "quit", "退出"),
        ("escape", "quit", "退出"),
        # Textual 部分版本默认把 ctrl+c 让给剪贴板，显式绑回退出、与终端习惯一致
        ("ctrl+c", "quit", "退出"),
    ]

    CSS = """
    /* 滚动只发生在 VerticalScroll 一层；Screen 禁滚，杜绝 resize 瞬间冒出第二条
       （Screen 默认滚动条宽 2 格）叠在容器滚动条旁边 */
    Screen { overflow-x: hidden; overflow-y: hidden; }
    /* 滚动条隐藏（主人定）：滚轮/方向键/PgUp 照常滚，只是不画位置指示条 */
    VerticalScroll { scrollbar-size: 0 0; }
    #sidebar-body { width: 1fr; }
    """

    def __init__(self, agent_ids: set[str] | None = None) -> None:
        # ansi_color=True：不把 ANSI 色转成 Textual 主题色，背景用终端自身默认色
        super().__init__(ansi_color=True)
        self._agent_ids = agent_ids

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(render_sidebar(scan_sessions(agent_ids=self._agent_ids)), id="sidebar-body")
        yield Footer()

    def on_mount(self) -> None:
        # chrome 配色映射到终端 ANSI 调色板；明暗跟随 tt 主题
        self.theme = "ansi-light" if get_theme(config.resolve_theme()).get("is_light") else "ansi-dark"
        self.query_one(VerticalScroll).focus()  # 容器持焦点，方向键/PgUp/PgDn 直接滚
        self.set_interval(REFRESH_SECONDS, self._refresh)

    def _refresh(self) -> None:
        self.query_one("#sidebar-body", Static).update(
            render_sidebar(scan_sessions(agent_ids=self._agent_ids)))

    def action_jump_to(self, session_id: str) -> None:
        """点击会话头行触发：跳转焦点到该会话所在的终端窗格。

        定位现读 STATUS_FILE（比渲染时快照新）；命令跑 worker 线程——osascript 首次
        触发 macOS 自动化授权弹窗时会阻塞，不能卡住 UI 事件循环。
        """
        info = terminal_info(session_id)
        argvs = _jump_argvs(info)
        if argvs is None:
            self.notify(t("sidebar_jump_no_target"), severity="warning", timeout=4)
            return
        self.run_worker(lambda: self._run_jump(argvs), thread=True)

    def _run_jump(self, argvs: list[list[str]]) -> None:
        for argv in argvs:
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as e:
                self.call_from_thread(self.notify, t("sidebar_jump_failed", err=str(e)[:80]),
                                      severity="error", timeout=4)
                return
            if proc.returncode != 0:
                err = (proc.stderr or "").strip()[:80] or f"exit {proc.returncode}"
                self.call_from_thread(self.notify, t("sidebar_jump_failed", err=err),
                                      severity="error", timeout=4)
                return
