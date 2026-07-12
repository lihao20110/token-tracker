"""tt sidebar 的 Textual 壳：备用屏常驻 + 滚动 + 定时刷新。

数据与渲染完全复用 `sidebar.scan_sessions` + `ui.sidebar.render_sidebar`——Rich renderable
直接塞进 Static 整帧更新，滚动位置由 VerticalScroll 容器保持（滚轮 / 方向键 / PgUp/PgDn）。
配色继承终端：`ansi_color=True` + `ansi-dark/light` 主题让 app chrome（背景/滚动条/Footer）
走终端 ANSI 调色板、不糊 Textual 自己的深色底；正文内容色仍由 tt 主题（`_S` 运行时代理）给，
`tt sidebar --theme <名>` 可临时切。明暗跟随 tt 主题的 is_light。
本模块 import textual，cli 只在 live 模式延迟 import（照 questionary 先例，日常 tt 启动不加载）。
点击跳转到对应终端窗格（需 statusline 携带 ITERM_SESSION_ID / TMUX_PANE 映射）留下一迭代。
"""

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from .. import config
from ..sidebar import scan_sessions
from .sidebar import render_sidebar
from .themes import get_theme

REFRESH_SECONDS = 5.0


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
    VerticalScroll { scrollbar-size: 1 1; }
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
        theme = get_theme(config.resolve_theme())
        self.theme = "ansi-light" if theme.get("is_light") else "ansi-dark"
        scroll = self.query_one(VerticalScroll)
        # 滚动条去存在感：拇指用 tt 主题 dim 灰、轨道透明融入终端背景，hover/拖动才提亮
        base = theme["base"]
        scroll.styles.scrollbar_color = base["overlay0"]
        scroll.styles.scrollbar_color_hover = base["blue"]
        scroll.styles.scrollbar_color_active = base["blue"]
        scroll.styles.scrollbar_background = "transparent"
        scroll.styles.scrollbar_background_hover = "transparent"
        scroll.styles.scrollbar_background_active = "transparent"
        scroll.focus()  # 容器持焦点，方向键/PgUp/PgDn 直接滚
        self.set_interval(REFRESH_SECONDS, self._refresh)

    def _refresh(self) -> None:
        self.query_one("#sidebar-body", Static).update(
            render_sidebar(scan_sessions(agent_ids=self._agent_ids)))
