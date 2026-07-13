"""tt sidebar 的 Textual 壳：备用屏常驻 + 滚动 + 鼠标选择自动复制 + 定时刷新。

数据复用 `sidebar.scan_sessions`；默认总览走 `ui.sidebar.render_sidebar`，自动 1/3 分屏走
`render_split_sidebar`。Rich renderable 直接塞进 Static 整帧更新，滚动位置由
VerticalScroll 容器保持（滚轮 / 方向键 / PgUp/PgDn）。
Rich Group 由 _SidebarBody 补 Textual 选择偏移与文本提取；拖拽时暂停整帧更新、松手自动复制。
本地分屏运行器可传 `initial_sessions` 复用预扫描首帧，compose 不再重复冷扫描。
配色继承终端：`ansi_color=True` + `ansi-dark/light` 主题让 app chrome（背景/滚动条/Footer）
走终端 ANSI 调色板、不糊 Textual 自己的深色底；正文内容色仍由 tt 主题（`_S` 运行时代理）给，
`tt sidebar --theme <名>` 可临时切。明暗跟随 tt 主题的 is_light。
本模块 import textual，cli 只在 live 模式延迟 import（照 questionary 先例，日常 tt 启动不加载）。
点击跳转依赖 statusline 携带 ITERM_SESSION_ID / TMUX_PANE 映射；tmux 与 iTerm2 已支持。
"""

import subprocess
from typing import Literal

from rich.console import Group
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Footer, Static

from .. import config
from ..i18n import t
from ..sidebar import LiveSession, registry_update_hint, scan_sessions, terminal_info
from .sidebar import SPLIT_MAX_PROMPTS, render_sidebar, render_split_sidebar
from .themes import get_theme

REFRESH_SECONDS = 5.0
SPINNER_SECONDS = 0.5  # 动画/时钟帧间隔——只用缓存会话重绘，不触发磁盘扫描
_TARGET_GONE_MARKER = "tt_jump_target_gone"  # AppleScript 未匹配到目标窗格的信号（tab 已关闭）


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
        # 找不到目标 session 时必须以 error 结束——否则 osascript 静默 rc=0、
        # 用户看到的是「点了没反应」（目标 tab 已关闭是常见场景）
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
        end tell
        error "{_TARGET_GONE_MARKER}"'''
        return [["osascript", "-e", script]]
    return None


class _SidebarBody(Static):
    """会话面板：链接（可跳转的会话头行）统一蓝色下划线语义。

    链接视觉定版（与主人多轮收敛）：静止=项目名蓝字蓝下划线（渲染层写死）；
    hover=整行蓝字+蓝下划线——Rich/Textual 均无独立下划线色通道（SGR 58 零支持，
    已核实源码），下划线颜色物理绑定字色，「整行统一蓝下划线」唯一实现就是整行变蓝。
    悬停高亮的连续性依赖渲染层 _click_style 的 link_id 跨帧稳定（曾因漂移断续）。
    """

    @property
    def link_style(self) -> RichStyle:
        return RichStyle()

    @property
    def link_style_hover(self) -> RichStyle:
        return RichStyle(color=get_theme(config.resolve_theme())["base"]["blue"], underline=True)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """从当前已渲染行提取选区，保证复制结果与窄窗格折行完全一致。"""
        if self._dirty_regions:
            self._render_content()
        text = "\n".join(line.text.rstrip() for line in self._render_cache.lines)
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        """给 Rich Group 补选择偏移，并在选区上叠加 Textual 高亮样式。

        Textual 自带 Strip.apply_offsets() 会给每个 Segment 新建 offset Style，并把
        新 Style 放在合并右侧；Rich 因此用 offset 的 link_id 覆盖整行共用的点击
        link_id，hover 只能命中鼠标所在的一段。这里反向合并，让点击 link_id 保持
        统一，同时仍给每段写入准确的选择坐标。
        """
        line = super().render_line(y)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            start, end = span
            line_text = Text()
            for segment in line:
                if not segment.control:
                    line_text.append(segment.text, segment.style)
            line_text.stylize(
                self.screen.get_component_rich_style("screen--selection"),
                start,
                None if end == -1 else end,
            )
            line = Strip(line_text.render(self.app.console), line.cell_length)
        offset_x = 0
        segments: list[Segment] = []
        for segment in line:
            offset_style = RichStyle.from_meta({"offset": (offset_x, y)})
            style = offset_style + segment.style if segment.style else offset_style
            segments.append(Segment(segment.text, style, segment.control))
            offset_x += len(segment.text)
        return Strip(segments, line.cell_length)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        app = self.app
        if event.button == 1 and isinstance(app, SidebarApp):
            app._text_dragging = True


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

    def __init__(self, agent_ids: set[str] | None = None,
                 variant: Literal["default", "split"] = "default",
                 initial_sessions: list[LiveSession] | None = None) -> None:
        # ansi_color=True：不把 ANSI 色转成 Textual 主题色，背景用终端自身默认色
        super().__init__(ansi_color=True)
        self._agent_ids = agent_ids
        self._variant = variant
        self._sessions = list(initial_sessions) if initial_sessions is not None else []
        self._needs_initial_scan = initial_sessions is None
        self._update_hint = variant == "default" and registry_update_hint(self._sessions)
        self._frame = 0
        self._text_dragging = False
        self._body_update_pending = False

    def _render_body(self) -> Group:
        if self._variant == "split":
            return render_split_sidebar(self._sessions)
        return render_sidebar(self._sessions, self._frame, update_hint=self._update_hint)

    def compose(self) -> ComposeResult:
        if self._needs_initial_scan:
            self._scan()
            self._needs_initial_scan = False
        with VerticalScroll():
            yield _SidebarBody(self._render_body(), id="sidebar-body")
        if self._variant == "default":
            yield Footer()

    def on_mount(self) -> None:
        # chrome 配色映射到终端 ANSI 调色板；明暗跟随 tt 主题
        self.theme = "ansi-light" if get_theme(config.resolve_theme()).get("is_light") else "ansi-dark"
        self.query_one(VerticalScroll).focus()  # 容器持焦点，方向键/PgUp/PgDn 直接滚
        self.set_interval(REFRESH_SECONDS, self._refresh)
        self.set_interval(SPINNER_SECONDS, self._tick_spinner)

    def _scan(self) -> None:
        if self._variant == "split":
            self._sessions = scan_sessions(agent_ids=self._agent_ids, max_prompts=SPLIT_MAX_PROMPTS)
        else:
            self._sessions = scan_sessions(agent_ids=self._agent_ids)
        self._update_hint = self._variant == "default" and registry_update_hint(self._sessions)

    def _update_body(self) -> None:
        if self._text_dragging:
            self._body_update_pending = True
            return
        self.query_one("#sidebar-body", Static).update(self._render_body())
        self._body_update_pending = False

    def _refresh(self) -> None:
        self._scan()
        self._update_body()

    def _tick_spinner(self) -> None:
        """动画/时钟帧：0.5s 重绘一次（纯内存渲染，5s 的磁盘扫描节奏不变）——
        运行中星形轮转 + 头部三时区时钟的秒针都靠它走。"""
        self._frame += 1
        self._update_body()

    def on_text_selected(self, event: events.TextSelected) -> None:
        """松开鼠标即复制；拖拽期间跳过的最新一帧在复制后补上。"""
        self._text_dragging = False
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
        if self._body_update_pending:
            self._update_body()

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
                stderr = (proc.stderr or "").strip()
                if _TARGET_GONE_MARKER in stderr:  # 映射还在、窗格已关（换成友好话术）
                    self.call_from_thread(self.notify, t("sidebar_jump_gone"),
                                          severity="warning", timeout=4)
                    return
                err = stderr[:80] or f"exit {proc.returncode}"
                self.call_from_thread(self.notify, t("sidebar_jump_failed", err=err),
                                      severity="error", timeout=4)
                return
