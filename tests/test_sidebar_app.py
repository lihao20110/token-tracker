"""SidebarApp（Textual 壳）冒烟：挂载、渲染、滚动容器持焦、q 退出。

agent_ids=set() 让 scan_sessions 跳过两个 agent 的真实数据扫描，
app 渲染空态——测试不依赖本机 ~/.claude / ~/.codex 内容。
"""

import json
import os
from uuid import uuid4

from textual import events
from textual.containers import VerticalScroll
from textual.geometry import Offset, Region
from textual.selection import Selection
from textual.widgets import Footer, Static

from token_tracker.sidebar_events import PromptEvent, prompt_event_from_hook, prompt_fifo_path, send_prompt_event
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


def test_split_sidebar_has_no_animation_repaint():
    app = SidebarApp(agent_ids=set(), variant="split", initial_sessions=[])
    app._tick_spinner()
    assert app._frame == 0


def test_prompt_event_from_hook_validates_and_keeps_agent_filtering():
    data = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "  正常提示词  ",
        "cwd": "/tmp/project",
        "model": "gpt-5",
        "turn_id": "turn-1",
    }
    assert prompt_event_from_hook(data, "codex") == PromptEvent(
        session_id="s1",
        prompt="正常提示词",
        agent_id="codex",
        cwd="/tmp/project",
        model="gpt-5",
        turn_id="turn-1",
    )
    assert prompt_event_from_hook({**data, "prompt": "/tt-sidebar"}, "claude-code") is None
    assert prompt_event_from_hook({**data, "session_id": ""}, "codex") is None
    claude_event = prompt_event_from_hook({
        **data,
        "turn_id": "",
        "prompt_id": "prompt-cc-1",
    }, "claude-code")
    assert claude_event is not None and claude_event.turn_id == "prompt-cc-1"


async def test_sidebar_app_boots_renders_and_quits(monkeypatch):
    app = SidebarApp(agent_ids=set())
    intervals: list[float] = []
    monkeypatch.setattr(app, "set_interval", lambda seconds, *args, **kwargs: intervals.append(seconds))
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
        assert intervals == [5.0, 0.5]  # 普通 `tt sidebar` 继续保留原数据/动画 timer
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
    fake = [LiveSession(agent_id="claude-code", session_id="s-copy", project="proj",
                        last_activity=datetime.now(UTC), state="waiting",
                        prompts=[Prompt("copy target", None)])]
    from token_tracker.ui import sidebar_app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "darwin")
    native_copies: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda argv, *, input, text, timeout, check: native_copies.append((argv, input)),
    )
    app = SidebarApp(
        agent_ids=set(),
        variant="split",
        initial_sessions=fake,
        prompt_session_id="s-copy",
        prompt_channel_dir=os.getcwd(),
    )
    notifications: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: notifications.append(message))
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        body = app.query_one("#sidebar-body", Static)
        content_before_drag = body.content

        assert app._variant == "split"
        assert len(app.query(Footer)) == 0  # 分屏只显示提示词
        # split 第 0 行就是最新提示词；从「树线 + 序号」后拖到行尾，背景填充不能进入剪贴板。
        await pilot.mouse_down("#sidebar-body", offset=(5, 0))
        assert app._text_dragging
        app._accept_prompt_event(PromptEvent("s-copy", "new target", "claude-code", turn_id="turn-2"))
        assert app._body_update_pending
        assert body.content is content_before_drag

        await pilot.mouse_up("#sidebar-body", offset=(58, 0))
        await pilot.pause()
        assert app.clipboard == "copy target"
        assert native_copies == [(["/usr/bin/pbcopy"], "copy target")]
        assert notifications == []
        assert not app._text_dragging
        assert not app._body_update_pending
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["copy target", "new target"]


async def test_split_fifo_event_updates_without_polling(monkeypatch):
    from token_tracker.ui import sidebar_app as app_module

    session_id = f"socket-{uuid4()}"
    scans: list[dict] = []
    intervals: list[float] = []
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kwargs: scans.append(kwargs) or [])
    app = SidebarApp(
        agent_ids=set(),
        variant="split",
        initial_sessions=[],
        prompt_session_id=session_id,
        prompt_channel_dir=os.getcwd(),
    )
    monkeypatch.setattr(app, "set_interval", lambda seconds, *args, **kwargs: intervals.append(seconds))
    path = prompt_fifo_path(session_id, os.getcwd())
    async with app.run_test(size=(50, 10)) as pilot:
        await pilot.pause()
        event = PromptEvent(
            session_id=session_id,
            prompt="hook 立即推送",
            agent_id="codex",
            cwd="/tmp/project",
            model="gpt-5",
            turn_id="turn-1",
        )
        assert send_prompt_event(event, channel_dir=os.getcwd())
        await pilot.pause(0.1)

        assert scans == []
        assert intervals == []
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["hook 立即推送"]
        body = app.query_one("#sidebar-body", Static)
        body._render_content()
        assert "hook 立即推送" in "\n".join(line.text for line in body._render_cache.lines)

        assert send_prompt_event(event, channel_dir=os.getcwd())  # 同一 Codex turn 重发也不能重复追加
        await pilot.pause(0.1)
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["hook 立即推送"]
    assert not os.path.exists(path)


async def test_split_transcript_tail_refreshes_when_codex_hook_was_not_loaded(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    session_id = "old-codex-session"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
    now = datetime.now(UTC)
    initial = [LiveSession(
        agent_id="codex",
        session_id=session_id,
        project="proj",
        last_activity=now,
        state="waiting",
        prompts=[Prompt("旧提示词", now)],
    )]
    refreshed = [LiveSession(
        agent_id="codex",
        session_id=session_id,
        project="proj",
        last_activity=now,
        state="running",
        prompts=[Prompt("旧提示词", now), Prompt("tail 立即补上", now)],
    )]
    distractor = LiveSession(
        agent_id="codex",
        session_id="other-session",
        project="other",
        last_activity=now,
        state="running",
        prompts=[Prompt("其他会话", now)],
    )
    scans: list[dict] = []
    monkeypatch.setattr(
        app_module,
        "find_session_transcript",
        lambda _sid: (_ for _ in ()).throw(AssertionError("精确 transcript hint 不应回退目录查找")),
    )
    def fake_scan(**kwargs):
        scans.append(kwargs)
        return [distractor, *refreshed] if "tail 立即补上" in rollout.read_text(encoding="utf-8") else initial

    monkeypatch.setattr(app_module, "scan_sessions", fake_scan)
    app = SidebarApp(
        variant="split",
        initial_sessions=initial,
        prompt_session_id=session_id,
        prompt_channel_dir=str(tmp_path),
        prompt_transcript_path=str(rollout),
        prompt_agent_id="codex",
    )
    intervals: list[float] = []
    monkeypatch.setattr(app, "set_interval", lambda seconds, *args, **kwargs: intervals.append(seconds))

    async with app.run_test(size=(50, 10)) as pilot:
        await pilot.pause(0.25)  # watcher 已记录启动 offset
        with rollout.open("a", encoding="utf-8") as transcript:
            for row in (
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-tail"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "tail 立即补上"}},
            ):
                transcript.write(json.dumps(row, ensure_ascii=False) + "\n")
        await pilot.pause(0.5)

        assert intervals == []  # 没恢复 Textual 全量轮询 timer
        assert len(scans) == 2  # 首次绑定对账 + 追加提示词后的权威刷新
        assert all(scan["agent_ids"] == {"codex"} for scan in scans)
        assert all(scan["max_sessions"] == 1000 for scan in scans)
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["旧提示词", "tail 立即补上"]

        # transcript 先到、同 turn 的 FIFO 后到，也不能把同一提示词乐观追加第二次。
        app._accept_prompt_event(PromptEvent(
            session_id,
            "tail 立即补上",
            "codex",
            turn_id="turn-tail",
        ))
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["旧提示词", "tail 立即补上"]

        body = app.query_one("#sidebar-body", Static)
        body._render_content()
        assert "tail 立即补上" in "\n".join(line.text for line in body._render_cache.lines)


async def test_split_transcript_initial_reconcile_closes_watcher_bind_race(tmp_path, monkeypatch):
    """提示词先于 watcher 绑定写入时，也不能因首次 offset=EOF 永久漏掉。"""
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    session_id = "bind-race"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "绑定前已写入"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    refreshed = [LiveSession(
        agent_id="codex",
        session_id=session_id,
        project="proj",
        last_activity=now,
        state="running",
        prompts=[Prompt("绑定前已写入", now)],
    )]
    monkeypatch.setattr(
        app_module,
        "find_session_transcript",
        lambda sid: ("codex", rollout) if sid == session_id else None,
    )
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kwargs: refreshed)
    app = SidebarApp(
        variant="split",
        initial_sessions=[],
        prompt_session_id=session_id,
        prompt_channel_dir=str(tmp_path),
    )

    async with app.run_test(size=(50, 10)) as pilot:
        await pilot.pause(0.3)
        assert [prompt.text for prompt in app._sessions[0].prompts] == ["绑定前已写入"]


async def test_split_fifo_first_then_transcript_snapshot_converges_without_duplicate(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    from token_tracker.ui import sidebar_app as app_module

    session_id = "fifo-first"
    now = datetime.now(UTC)
    initial = [LiveSession(
        agent_id="codex",
        session_id=session_id,
        project="proj",
        last_activity=now,
        state="waiting",
        prompts=[Prompt("旧提示词", now)],
    )]
    authoritative = [LiveSession(
        agent_id="codex",
        session_id=session_id,
        project="proj",
        last_activity=now,
        state="running",
        prompts=[Prompt("旧提示词", now), Prompt("同一条", now)],
    )]
    monkeypatch.setattr(app_module, "find_session_transcript", lambda _sid: None)
    monkeypatch.setattr(app_module, "scan_sessions", lambda **kwargs: authoritative)
    app = SidebarApp(
        variant="split",
        initial_sessions=initial,
        prompt_session_id=session_id,
        prompt_channel_dir=str(tmp_path),
    )

    async with app.run_test(size=(50, 10)) as pilot:
        await pilot.pause()
        app._accept_prompt_event(PromptEvent(session_id, "同一条", "codex", turn_id="turn-same"))
        app._refresh_split_from_transcript("codex", ("turn-same",))
        await pilot.pause()

        assert [prompt.text for prompt in app._sessions[0].prompts] == ["旧提示词", "同一条"]


async def test_split_multiline_selection_excludes_tree_number_and_hanging_indent(monkeypatch):
    from datetime import UTC, datetime

    from rich.style import Style

    from token_tracker.sidebar import LiveSession, Prompt

    now = datetime.now(UTC)
    initial = [LiveSession(agent_id="codex", session_id="select", project="proj",
                           last_activity=now, state="waiting",
                           prompts=[Prompt("alpha beta gamma delta", now)])]
    app = SidebarApp(agent_ids=set(), variant="split", initial_sessions=initial)
    async with app.run_test(size=(18, 6)) as pilot:
        await pilot.pause()
        body = app.query_one("#sidebar-body", Static)
        component_style = app.screen.get_component_rich_style
        monkeypatch.setattr(
            app.screen,
            "get_component_rich_style",
            lambda name: Style(bgcolor="red") if name == "screen--selection" else component_style(name),
        )
        selection = Selection.from_offsets(Offset(5, 0), Offset(10, 1))
        app.screen.selections[body] = selection
        body.selection_updated(selection)
        await pilot.pause()

        rendered = [line.text.rstrip() for line in body._render_cache.lines]
        assert rendered[:2] == ["└ 1. alpha beta", "     gamma delta"]
        assert body.get_selection(selection) == ("alpha beta\ngamma", "\n")
        assert body._split_body_span(rendered[1], (0, 10), 5) == (5, 10)
        assert body._split_body_span("│", (0, -1), 5) is None

        selection_style = Style(bgcolor="red")
        second_line = body.render_line(1)
        selected_second_line = "".join(
            segment.text for segment in second_line
            if segment.style and segment.style.bgcolor == selection_style.bgcolor
        )
        assert selected_second_line == "gamma"

        across_prompts = Selection.from_offsets(Offset(5, 0), Offset(8, 2))
        assert body._split_selection(
            across_prompts,
            ["├ 2. first", "│", "└ 1. old"],
            5,
        ) == "first\n\nold"


async def test_split_sidebar_all_prompts_scroll_with_wheel_and_page_down():
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt

    now = datetime.now(UTC)
    initial = [LiveSession(agent_id="codex", session_id="scroll", project="proj",
                           last_activity=now, state="waiting",
                           prompts=[Prompt(f"提示词 {i:02d}", now) for i in range(30)])]
    app = SidebarApp(agent_ids=set(), variant="split", initial_sessions=initial)
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        scroll = app.query_one(VerticalScroll)
        assert scroll.max_scroll_y > 0

        await pilot.press("pagedown")
        await pilot.pause()
        assert scroll.scroll_y > 0

        scroll.scroll_home(animate=False)
        await pilot.pause()
        assert scroll.scroll_y == 0
        await pilot._post_mouse_events(
            [events.MouseScrollDown], "#sidebar-body", offset=(5, 5), times=3,
        )
        assert scroll.scroll_y > 0


async def test_split_prompt_event_keeps_scrolled_history_anchored():
    from datetime import UTC, datetime

    from token_tracker.sidebar import LiveSession, Prompt
    now = datetime.now(UTC)
    prompts = [Prompt(f"提示词 {i:02d}", now) for i in range(30)]

    def session(items):
        return [LiveSession(agent_id="codex", session_id="scroll", project="proj",
                            last_activity=now, state="waiting", prompts=items)]

    app = SidebarApp(
        agent_ids=set(),
        variant="split",
        initial_sessions=session(prompts),
        prompt_session_id="scroll",
        prompt_channel_dir=os.getcwd(),
    )
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        scroll = app.query_one(VerticalScroll)
        await pilot.press("pagedown")
        await pilot.pause()
        old_y = scroll.scroll_y
        old_height = scroll.virtual_size.height

        app._accept_prompt_event(PromptEvent(
            "scroll",
            "新增提示词第一行\n新增提示词第二行",
            "codex",
            turn_id="turn-new",
        ))
        await pilot.pause()
        height_delta = scroll.virtual_size.height - old_height

        assert height_delta > 0
        assert scroll.scroll_y == old_y + height_delta


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
