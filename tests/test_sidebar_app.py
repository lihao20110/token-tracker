"""SidebarApp（Textual 壳）冒烟：挂载、渲染、滚动容器持焦、q 退出。

agent_ids=set() 让 scan_sessions 跳过两个 agent 的真实数据扫描，
app 渲染空态——测试不依赖本机 ~/.claude / ~/.codex 内容。
"""

from textual.containers import VerticalScroll
from textual.widgets import Static

from token_tracker.ui.sidebar_app import SidebarApp


async def test_sidebar_app_boots_renders_and_quits():
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None
        assert app.query_one(VerticalScroll).has_focus  # 容器持焦点，方向键可滚
        assert app.theme in ("ansi-dark", "ansi-light")  # chrome 继承终端 ANSI 调色板
        await pilot.press("q")
    # run_test 正常退出即通过：q 绑定 quit、app 无异常


async def test_sidebar_app_refresh_updates_content():
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        # 手动触发一次定时刷新逻辑，确认整帧更新不抛异常
        app._refresh()
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None
