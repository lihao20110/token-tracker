"""SidebarApp（Textual 壳）冒烟：挂载、渲染、滚动容器持焦、q 退出。

agent_ids=set() 让 scan_sessions 跳过两个 agent 的真实数据扫描，
app 渲染空态——测试不依赖本机 ~/.claude / ~/.codex 内容。
"""

from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import Footer, Static

from token_tracker.ui.sidebar_app import SidebarApp, _jump_argvs


def test_jump_argvs_tmux_takes_priority():
    argvs = _jump_argvs({"tmux": "%5", "iterm": "w0t1p0:AAA"})
    assert argvs == [["tmux", "select-window", "-t", "%5"], ["tmux", "select-pane", "-t", "%5"]]


def test_jump_argvs_iterm_extracts_uuid():
    argvs = _jump_argvs({"iterm": "w0t3p0:1A2B-3C4D"})
    assert len(argvs) == 1 and argvs[0][0] == "osascript"
    assert 'if id of s is "1A2B-3C4D"' in argvs[0][2]
    # 回归：未匹配到目标窗格必须以 error 结束，否则 rc=0 静默、用户看到「点了没反应」
    assert "tt_jump_target_gone" in argvs[0][2]


def test_jump_argvs_none_without_target():
    assert _jump_argvs({}) is None


async def test_sidebar_app_boots_renders_and_quits():
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None
        assert len(app.query(Footer)) == 1  # 普通 sidebar 保留按键提示
        assert app._variant == "default"  # 普通 `tt sidebar` 走原总览渲染器
        scroll = app.query_one(VerticalScroll)
        assert scroll.has_focus  # 容器持焦点，方向键可滚
        assert app.theme in ("ansi-dark", "ansi-light")  # chrome 继承终端 ANSI 调色板
        # 滚动条已隐藏（主人定，滚轮/方向键仍可滚）；Screen 层禁滚防 resize 冒第二条
        assert scroll.styles.scrollbar_size_vertical == 0
        assert str(app.screen.styles.overflow_y) == "hidden"
        assert not app.screen.show_vertical_scrollbar
        # 链接视觉定版：静止=项目名蓝字蓝下划线；hover=整行蓝字+蓝下划线
        # （下划线颜色物理绑定字色，整行统一蓝下划线的唯一实现就是整行变蓝）
        body = app.query_one("#sidebar-body", Static)
        assert not body.link_style
        assert body.link_style_hover.underline
        assert body.link_style_hover.color is not None
        await pilot.press("q")
    # run_test 正常退出即通过：q 绑定 quit、app 无异常


async def test_click_session_head_dispatches_jump(monkeypatch):
    # 端到端派发链路：鼠标点会话头行 → meta @click → App.action_jump_to 收到 session_id。
    # 回归：action 不带 app. 前缀时派发到 Static 找不到方法、静默无反应。
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    fake = [LiveSession(agent_id="claude-code", session_id="s-click", project="proj",
                        last_activity=datetime.now(UTC), state="waiting",
                        prompts=[Prompt("提示词", None)], terminal={"iterm": "w0t0p0:X"})]
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kw: fake)
    app = SidebarApp(agent_ids=set())
    called: list[str] = []
    monkeypatch.setattr(app, "action_jump_to", lambda sid: called.append(sid), raising=False)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        # 内容行序：0=标题、1/2/3=北京/洛杉矶/伦敦时钟、4=空行、5=会话头行
        await pilot.click("#sidebar-body", offset=(4, 5))
        await pilot.pause()
    assert called == ["s-click"]


async def test_session_head_hover_underlines_the_whole_link(monkeypatch):
    # 回归：选择 offset 曾覆盖整行共用的 link_id，hover 只能命中项目名这一段，
    # 分支 / Agent / 活动时间 / 状态的下划线在样式边界全部断开。
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    fake = [LiveSession(agent_id="codex", session_id="s-hover", project="proj", branch="main",
                        last_activity=datetime.now(UTC), state="waiting",
                        prompts=[Prompt("提示词", None)], terminal={"iterm": "w0t0p0:X"})]
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kw: fake)
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.pause()
        body = app.query_one("#sidebar-body", Static)
        await pilot.hover("#sidebar-body", offset=(4, 5))
        await pilot.pause()
        line = body.render_lines(Region(0, 5, body.size.width, 1))[0]
        linked = [segment for segment in line
                  if segment.style and segment.style._meta and "@click" in segment.style.meta]

        assert "".join(segment.text for segment in linked).startswith("● proj(main) · Codex")
        assert {segment.style.link_id for segment in linked} == {body.hover_style.link_id}
        assert all(segment.style.underline for segment in linked)
        assert all(segment.style.color == body.link_style_hover.color for segment in linked)


async def test_drag_selection_auto_copies_and_defers_refresh(monkeypatch):
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    fake = [LiveSession(agent_id="claude-code", session_id="s-copy", project="proj",
                        last_activity=datetime.now(UTC), state="waiting",
                        prompts=[Prompt("copy target", None)])]
    scan_kwargs: list[dict] = []

    def fake_scan(**kwargs):
        scan_kwargs.append(kwargs)
        return fake

    monkeypatch.setattr(app_module, "scan_sessions", fake_scan)
    app = SidebarApp(agent_ids=set(), variant="split")
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: notifications.append(message))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        body = app.query_one("#sidebar-body", Static)
        content_before_drag = body.content

        assert app._variant == "split"
        assert len(app.query(Footer)) == 0  # 分屏只显示提示词
        assert scan_kwargs[0]["max_prompts"] == 10
        # split 第 0 行就是最新提示词；从树前缀后拖到行尾，视觉背景填充不能进入剪贴板。
        await pilot.mouse_down("#sidebar-body", offset=(2, 0))
        assert app._text_dragging
        app._tick_spinner()
        assert app._body_update_pending
        assert body.content is content_before_drag

        await pilot.mouse_up("#sidebar-body", offset=(58, 0))
        await pilot.pause()
        assert app.clipboard == "copy target"
        assert notifications == []
        assert not app._text_dragging
        assert not app._body_update_pending


async def test_sidebar_app_refresh_updates_content():
    app = SidebarApp(agent_ids=set())
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        # 手动触发一次定时刷新逻辑，确认整帧更新不抛异常
        app._refresh()
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None


async def test_sidebar_app_reuses_initial_sessions_on_first_frame(monkeypatch):
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    initial = [LiveSession(agent_id="codex", session_id="fast", project="proj",
                           last_activity=datetime.now(UTC), state="waiting",
                           prompts=[Prompt("ready", None)])]
    scans: list[dict] = []
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kwargs: scans.append(kwargs) or [])
    app = SidebarApp(agent_ids=set(), variant="split", initial_sessions=initial)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#sidebar-body", Static) is not None
        assert app._sessions == initial
        assert scans == []  # 首帧直接复用预扫描结果，不冷启动扫第二遍
