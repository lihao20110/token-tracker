"""tt sidebar 的 Textual 壳：备用屏常驻 + 滚动 + 定时刷新。

数据与渲染完全复用 `sidebar.scan_sessions` + `ui.sidebar.render_sidebar`——Rich renderable
直接塞进 Static 整帧更新，滚动位置由 VerticalScroll 容器保持（滚轮 / 方向键 / PgUp/PgDn）。
本模块 import textual，cli 只在 live 模式延迟 import（照 questionary 先例，日常 tt 启动不加载）。
点击跳转到对应终端窗格（需 statusline 携带 ITERM_SESSION_ID / TMUX_PANE 映射）留下一迭代。
"""

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..sidebar import scan_sessions
from .sidebar import render_sidebar

REFRESH_SECONDS = 5.0


class SidebarApp(App[None]):
    BINDINGS = [
        ("q", "quit", "退出"),
        # Textual 部分版本默认把 ctrl+c 让给剪贴板，显式绑回退出、与 Rich Live 时代习惯一致
        ("ctrl+c", "quit", "退出"),
    ]

    CSS = """
    VerticalScroll { scrollbar-size: 1 1; }
    Static { width: 1fr; }
    """

    def __init__(self, agent_ids: set[str] | None = None) -> None:
        super().__init__()
        self._agent_ids = agent_ids

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(render_sidebar(scan_sessions(agent_ids=self._agent_ids)))

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()  # 容器持焦点，方向键/PgUp/PgDn 直接滚
        self.set_interval(REFRESH_SECONDS, self._refresh)

    def _refresh(self) -> None:
        self.query_one(Static).update(render_sidebar(scan_sessions(agent_ids=self._agent_ids)))
