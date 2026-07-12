"""SidebarApp（Textual 壳）冒烟：挂载、渲染、滚动容器持焦、q 退出。

agent_ids=set() 让 scan_sessions 跳过两个 agent 的真实数据扫描，
app 渲染空态——测试不依赖本机 ~/.claude / ~/.codex 内容。
"""

from textual.containers import VerticalScroll
from textual.widgets import Static

from token_tracker.ui.sidebar_app import SidebarApp, _jump_argvs


def test_jump_argvs_tmux_takes_priority():
    argvs = _jump_argvs({"tmux": "%5", "iterm": "w0t1p0:AAA"})
    assert argvs == [["tmux", "select-window", "-t", "%5"], ["tmux", "select-pane", "-t", "%5"]]


def test_jump_argvs_iterm_extracts_uuid():
    argvs = _jump_argvs({"iterm": "w0t3p0:1A2B-3C4D"})
    assert len(argvs) == 1 and argvs[0][0] == "osascript"
    assert 'if id of s is "1A2B-3C4D"' in argvs[0][2]


def test_jump_argvs_none_without_target():
    assert _jump_argvs({}) is None


async def test_sidebar_app_boots_renders_and_quits():
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None
        scroll = app.query_one(VerticalScroll)
        assert scroll.has_focus  # 容器持焦点，方向键可滚
        assert app.theme in ("ansi-dark", "ansi-light")  # chrome 继承终端 ANSI 调色板
        # 滚动条已隐藏（主人定，滚轮/方向键仍可滚）；Screen 层禁滚防 resize 冒第二条
        assert scroll.styles.scrollbar_size_vertical == 0
        assert str(app.screen.styles.overflow_y) == "hidden"
        assert not app.screen.show_vertical_scrollbar
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
