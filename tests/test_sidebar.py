import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from token_tracker import sidebar
from token_tracker.sidebar import (
    ATTENTION,
    IDLE,
    RUNNING,
    WAITING,
    LiveSession,
    Prompt,
    _infer_state,
    _parse_claude,
    _parse_codex,
    _scan_claude_sessions,
)
from token_tracker.ui.sidebar import render_sidebar


@pytest.fixture(autouse=True)
def _clear_parse_cache():
    # 模块级解析缓存按 (mtime, size) 命中，tmp_path 各测试独立但防跨测试串味
    sidebar._parse_cache.clear()


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _iso(seconds_ago: float = 60) -> str:
    """相对当前时间的 ISO 时间戳——last_activity 以内容事件时间为准，fixture 不能用死日期。"""
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _u(content, ts: str | None = None, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content},
            "timestamp": ts or _iso(), **extra}


def _a(content, model: str = "claude-fable-5", ts: str | None = None) -> dict:
    return {"type": "assistant", "timestamp": ts or _iso(55),
            "message": {"role": "assistant", "model": model, "content": content}}


# --- CC transcript 解析 ---

def test_claude_prompt_extraction_filters_noise(tmp_path):
    # 只保留人敲的提示词：slash command 记录 / isMeta / tool_result / 子代理 sidechain 全过滤
    rows = [
        {"type": "summary", "summary": "历史摘要行"},
        _u("<command-name>/clear</command-name>"),
        _u("<local-command-caveat>Caveat: ...</local-command-caveat>", isMeta=True),
        _u("真提示词一", sessionId="s-abc", gitBranch="feature/x"),
        _u([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        _u([{"type": "image", "source": {}}, {"type": "text", "text": "带图提示词"}]),
        _u("子代理里的任务描述", isSidechain=True),
        _u("[Image: source: /tmp/x.png]", isMeta=True),
        _u("[Request interrupted by user for tool use]"),
        _u("<task-notification> <task-id>a36fa19b</task-id> 后台任务完成通知"),
        # 注入片段与真提示词同消息：只丢噪音片段、保留真提示词
        _u([{"type": "text", "text": "<system-reminder>召回的记忆背景</system-reminder>"},
            {"type": "text", "text": "混合消息里的真提示词"}]),
    ]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s-abc.jsonl", rows), "fallback", 5)
    assert parsed is not None
    assert [p.text for p in parsed.prompts] == ["真提示词一", "带图提示词", "混合消息里的真提示词"]
    assert parsed.session_id == "s-abc"
    assert parsed.prompts[0].timestamp is not None
    assert parsed.branch == "feature/x"  # transcript 自带 gitBranch，白拿


def test_claude_max_prompts_keeps_latest(tmp_path):
    rows = [_u(f"提示词{i}") for i in range(5)]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s.jsonl", rows), "p", 3)
    assert [p.text for p in parsed.prompts] == ["提示词2", "提示词3", "提示词4"]


def test_claude_pending_tool_tracking(tmp_path):
    tool_use = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
    tool_result = [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]
    # 末条是无结果的 tool_use → pending（等授权/工具中）
    parsed = _parse_claude(_write_jsonl(tmp_path / "a.jsonl", [_u("跑一下"), _a(tool_use)]), "p", 3)
    assert parsed.pending_tool is True
    # tool_result 回来 → 不再 pending
    parsed = _parse_claude(
        _write_jsonl(tmp_path / "b.jsonl", [_u("跑一下"), _a(tool_use), _u(tool_result)]), "p", 3)
    assert parsed.pending_tool is False
    # 纯文本回复收尾 → 不 pending
    parsed = _parse_claude(_write_jsonl(tmp_path / "c.jsonl", [_u("你好"), _a("好的")]), "p", 3)
    assert parsed.pending_tool is False
    assert parsed.model == "claude-fable-5"


def test_claude_no_prompts_returns_none(tmp_path):
    rows = [_u("<command-name>/clear</command-name>")]
    assert _parse_claude(_write_jsonl(tmp_path / "s.jsonl", rows), "p", 3) is None


def test_claude_filters_bare_slash_command_records(tmp_path):
    # CC 对 slash 命令实测记两条 user 消息：<command-name> 包裹（既有过滤）+ 裸文本（本用例）
    rows = [
        _u("/compact"),
        _u("/cd ~/project/token-tracker"),
        _u("/plugin:cmd-name 带参数"),
        _u("/Users/stormzhang/x.md 这个文件帮我看下"),  # 路径开头的真提示词不误伤
    ]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s.jsonl", rows), "p", 5)
    assert [p.text for p in parsed.prompts] == ["/Users/stormzhang/x.md 这个文件帮我看下"]


def test_claude_skips_compact_summary_injection(tmp_path):
    # /compact 后注入的巨型摘要带 isCompactSummary/isVisibleInTranscriptOnly 标记：
    # 不进提示词、不消费待回答的 AskUserQuestion（压缩≠回答了提问）、不计活动时间
    ask = [{"type": "tool_use", "id": "t1", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "合并到 main 吗？", "options": [{"label": "合并"}]}]}}]
    rows = [
        _u("真提示词", ts=_iso(300)),
        _a(ask, ts=_iso(240)),
        _u("This session is being continued from a previous conversation...", ts=_iso(10),
           isCompactSummary=True, isVisibleInTranscriptOnly=True),
    ]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s.jsonl", rows), "p", 5)
    assert [p.text for p in parsed.prompts] == ["真提示词"]
    assert "合并到 main 吗？" in parsed.next_hint
    # 活动时间停在压缩前最后一条真实事件（240s 前），不被摘要（10s 前）顶新
    assert (datetime.now(UTC) - parsed.last_event).total_seconds() > 120


# --- 状态推断 ---

def test_infer_state_matrix():
    now = datetime.now(UTC)
    fresh = now - timedelta(seconds=5)
    stale = now - timedelta(minutes=5)
    ancient = now - timedelta(hours=2)
    assert _infer_state(now, fresh, False, False) == RUNNING
    assert _infer_state(now, stale, False, True) == RUNNING   # 心跳新鲜也算在跑
    assert _infer_state(now, stale, True, False) == ATTENTION
    assert _infer_state(now, stale, False, False) == WAITING
    assert _infer_state(now, ancient, False, False) == IDLE
    assert _infer_state(now, ancient, True, False) == ATTENTION  # pending 优先于 idle


# --- Codex rollout 解析 ---

def test_codex_parse(tmp_path):
    rows = [
        {"timestamp": "2026-07-12T02:00:00.000Z", "type": "session_meta",
         "payload": {"id": "cx-1", "timestamp": "2026-07-12T02:00:00.000Z", "cwd": "/tmp/nope/beta",
                     "git": {"commit_hash": "abc", "branch": "main"}}},
        {"timestamp": "2026-07-12T02:00:01.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "<user_instructions>注入的模板</user_instructions>"}},
        {"timestamp": "2026-07-12T02:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "介绍下这个项目"}},
        {"timestamp": "2026-07-12T02:00:03.000Z", "type": "event_msg",
         "payload": {"type": "task_started"}},
    ]
    parsed = _parse_codex(_write_jsonl(tmp_path / "rollout.jsonl", rows), 3)
    assert parsed is not None
    assert parsed.session_id == "cx-1"
    assert parsed.project == "beta"  # cwd 不存在 .git → 落最后一段
    assert [p.text for p in parsed.prompts] == ["介绍下这个项目"]
    assert parsed.branch == "main"  # session_meta.git.branch
    assert parsed.pending_tool is True  # task_started 无 complete

    rows.append({"timestamp": "2026-07-12T02:01:00.000Z", "type": "event_msg",
                 "payload": {"type": "task_complete"}})
    sidebar._parse_cache.clear()
    parsed = _parse_codex(_write_jsonl(tmp_path / "rollout.jsonl", rows), 3)
    assert parsed.pending_tool is False


# --- 扫描：窗口过滤 + 排序 + 缓存 ---

def _make_claude_base(tmp_path: Path) -> Path:
    base = tmp_path / "projects"
    (base / "-Users-x-project-alpha").mkdir(parents=True)
    return base


def test_scan_claude_window_filter_and_sort(tmp_path):
    base = _make_claude_base(tmp_path)
    d = base / "-Users-x-project-alpha"
    now = datetime.now(UTC)
    old = _write_jsonl(d / "old.jsonl", [_u("久远会话", ts=_iso(24 * 3600))])
    ancient_ts = (now - timedelta(hours=24)).timestamp()
    os.utime(old, (ancient_ts, ancient_ts))
    mid = _write_jsonl(d / "mid.jsonl", [_u("两分钟前的", ts=_iso(120))])
    mid_ts = (now - timedelta(seconds=120)).timestamp()
    os.utime(mid, (mid_ts, mid_ts))
    fresh = _write_jsonl(d / "fresh.jsonl", [_u("刚刚的", ts=_iso(3))])
    fresh_ts = (now - timedelta(seconds=3)).timestamp()
    os.utime(fresh, (fresh_ts, fresh_ts))
    # 回归（design-agent 实测）：内容 24h 没动、但文件 mtime 被 CC 常驻进程触碰得很新
    # ——活动时间以内容事件为准，这种会话必须被窗口过滤，不能靠 mtime 冒到第一位
    _write_jsonl(d / "touched.jsonl", [_u("很久以前的内容", ts=_iso(24 * 3600))])

    got = _scan_claude_sessions(now - timedelta(hours=12), now, None, 3, dirs=[str(base)])
    got.sort(key=lambda s: s.last_activity, reverse=True)
    assert [s.session_id for s in got] == ["fresh", "mid"]  # 24h 前的与「假触碰」的都被过滤
    assert got[0].state == RUNNING
    assert got[1].state == WAITING
    assert got[0].project == "alpha"  # 无 cwd 时从目录名解码
    assert got[0].agent_id == "claude-code"


def test_scan_claude_uses_cache_for_unchanged_file(tmp_path, monkeypatch):
    base = _make_claude_base(tmp_path)
    d = base / "-Users-x-project-alpha"
    _write_jsonl(d / "s.jsonl", [_u("你好")])
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=12)
    assert len(_scan_claude_sessions(cutoff, now, None, 3, dirs=[str(base)])) == 1
    # 文件未变 → 第二次扫描不再触发解析（mtime+size 缓存命中）
    monkeypatch.setattr(sidebar, "_parse_claude",
                        lambda *a, **k: pytest.fail("cache miss: reparsed unchanged file"))
    assert len(_scan_claude_sessions(cutoff, now, None, 3, dirs=[str(base)])) == 1


def test_scan_sessions_caps_at_max_sessions(monkeypatch):
    # 窗口内超过 max_sessions 时按最近活动取前 N；不足 N 全显（切片天然满足）
    now = datetime.now(UTC)
    fake = [LiveSession(agent_id="claude-code", session_id=f"s{i}", project=f"p{i}",
                        last_activity=now - timedelta(minutes=i), state=WAITING,
                        prompts=[Prompt("x", now)])
            for i in range(12)]
    monkeypatch.setattr(sidebar, "_scan_claude_sessions", lambda *a, **k: fake)
    monkeypatch.setattr(sidebar, "_scan_codex_sessions", lambda *a, **k: [])
    got = sidebar.scan_sessions()
    assert len(got) == 10
    assert [s.session_id for s in got] == [f"s{i}" for i in range(10)]  # 最新的 10 个


def test_read_status_returns_terminal_map(tmp_path, monkeypatch):
    status = tmp_path / "tt-status.json"
    status.write_text(json.dumps({
        "session_id": "s-live", "_received_at": datetime.now(UTC).isoformat(),
        "_terminal_map": {"s-live": {"iterm": "w0t1p0:AAA-BBB", "tmux": "%3"}},
    }), encoding="utf-8")
    monkeypatch.setattr(sidebar.config, "STATUS_FILE", str(status))
    hb, term_map = sidebar._read_status()
    assert hb is not None and hb[0] == "s-live"
    assert term_map["s-live"]["iterm"] == "w0t1p0:AAA-BBB"
    assert sidebar.terminal_info("s-live")["tmux"] == "%3"
    assert sidebar.terminal_info("unknown") == {}


def test_scan_claude_attaches_terminal(tmp_path):
    base = _make_claude_base(tmp_path)
    _write_jsonl(base / "-Users-x-project-alpha" / "s1.jsonl", [_u("你好")])
    now = datetime.now(UTC)
    term_map = {"s1": {"iterm": "w0t2p0:CCC"}}
    got = _scan_claude_sessions(now - timedelta(hours=12), now, None, 3,
                                dirs=[str(base)], term_map=term_map)
    assert got[0].terminal == {"iterm": "w0t2p0:CCC"}


def test_render_head_click_meta_and_link_style():
    # 链接语义统一：有终端定位=可点（meta 带 app. 前缀防派发到 Static 静默失败）
    # 且项目名带蓝色下划线标记；无定位=无 meta 无链接样式，所见即所得
    from rich.text import Text

    def _collect(session):
        actions, underlined = [], False
        for r in render_sidebar([session]).renderables:
            if isinstance(r, Text):
                for span in r.spans:
                    meta = getattr(span.style, "meta", None)
                    if meta and "@click" in meta:
                        actions.append(meta["@click"])
                    if isinstance(span.style, str) and "underline" in span.style:
                        underlined = True
        return actions, underlined

    now = datetime.now(UTC)
    mapped = LiveSession(agent_id="claude-code", session_id="sX", project="p",
                         last_activity=now, state=WAITING,
                         prompts=[Prompt("x", now)], terminal={"iterm": "w0t0p0:X"})
    actions, underlined = _collect(mapped)
    assert actions == ["app.jump_to('sX')"]
    assert underlined
    unmapped = LiveSession(agent_id="claude-code", session_id="sY", project="p",
                           last_activity=now, state=WAITING,
                           prompts=[Prompt("x", now)], terminal={})
    actions, underlined = _collect(unmapped)
    assert actions == [] and not underlined


def test_click_link_id_stable_across_renders():
    # 回归：Rich 每次 from_meta 生成随机 link_id，0.5s 整帧重绘若每帧新建样式，
    # Textual 按 link_id 定位的 hover 高亮半秒即失联（下划线时有时无）
    from rich.text import Text
    now = datetime.now(UTC)
    session = LiveSession(agent_id="claude-code", session_id="sZ", project="p",
                          last_activity=now, state=WAITING,
                          prompts=[Prompt("x", now)], terminal={"iterm": "w0t0p0:X"})

    def _link_ids(group):
        ids = set()
        for r in group.renderables:
            if isinstance(r, Text):
                for span in r.spans:
                    st = span.style
                    if getattr(st, "_meta", None):
                        ids.add(st.link_id)
        return ids

    first, second = _link_ids(render_sidebar([session])), _link_ids(render_sidebar([session]))
    assert len(first) == 1
    assert first == second  # 跨帧稳定


def test_heartbeat_marks_running(tmp_path):
    base = _make_claude_base(tmp_path)
    d = base / "-Users-x-project-alpha"
    p = _write_jsonl(d / "hb.jsonl", [_u("在等心跳")])
    now = datetime.now(UTC)
    stale_ts = (now - timedelta(minutes=5)).timestamp()
    os.utime(p, (stale_ts, stale_ts))
    hb = ("hb", now - timedelta(seconds=3))
    got = _scan_claude_sessions(now - timedelta(hours=12), now, hb, 3, dirs=[str(base)])
    assert got[0].state == RUNNING  # 文件虽停写，statusline 心跳仍新鲜


# --- 渲染 ---

def test_render_sidebar_smoke():
    now = datetime.now(UTC)
    sessions = [
        LiveSession(agent_id="claude-code", session_id="s1", project="fuxi",
                    last_activity=now - timedelta(seconds=10), state=RUNNING,
                    prompts=[Prompt("全面看下这个项目", now), Prompt("全去做", now)],
                    model="claude-fable-5"),
        LiveSession(agent_id="codex", session_id="s2", project="wx-clawbot",
                    last_activity=now - timedelta(minutes=9), state=WAITING,
                    prompts=[Prompt("介绍下这个项目", now)]),
    ]
    console = Console(record=True, width=60, force_terminal=True)
    console.print(render_sidebar(sessions))
    text = console.export_text()
    assert "fuxi" in text
    assert "全去做" in text
    assert "wx-clawbot" in text
    assert "Codex" in text


def test_render_prompt_wraps_two_lines_with_ellipsis():
    # 长提示词最多折 2 行、末行省略号；正文右侧留 2 格（行宽 ≤ 终端宽 - 2）
    now = datetime.now(UTC)
    long_prompt = "这是一条非常长的提示词内容" * 12
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=WAITING,
                            prompts=[Prompt(long_prompt, now)])]
    console = Console(record=True, width=40, force_terminal=True)
    console.print(render_sidebar(sessions))
    out_lines = console.export_text().splitlines()
    body_lines = [ln for ln in out_lines if "提示词" in ln or "非常长" in ln]
    assert len(body_lines) == 2
    assert body_lines[-1].rstrip().endswith("…")
    assert all(len(ln.rstrip()) <= 40 - 2 for ln in body_lines)  # 右侧留白 2 格


def test_render_tree_glyphs_count_prompts():
    # 树状语义：每条提示词一个分支符（├，末条 └）——数分支即数提示词；
    # 非末条折行的续行用 │ 延续轨道，与新条目肉眼可分
    now = datetime.now(UTC)
    long_prompt = "很长的提示词内容" * 15
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=WAITING,
                            prompts=[Prompt(long_prompt, now), Prompt("第二条", now),
                                     Prompt("第三条", now)])]
    console = Console(record=True, width=40, force_terminal=True)
    console.print(render_sidebar(sessions))
    out = console.export_text().splitlines()
    assert sum(1 for ln in out if ln.startswith("├")) == 2
    assert sum(1 for ln in out if ln.startswith("└")) == 1
    assert any(ln.startswith("│") for ln in out)  # 首条折行的续行


def test_render_prompt_short_stays_single_line():
    now = datetime.now(UTC)
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=WAITING,
                            prompts=[Prompt("短提示词", now)])]
    console = Console(record=True, width=40, force_terminal=True)
    console.print(render_sidebar(sessions))
    body_lines = [ln for ln in console.export_text().splitlines() if "短提示词" in ln]
    assert len(body_lines) == 1
    assert "…" not in body_lines[0]


def test_next_hint_scores_action_over_narrative(tmp_path):
    # 「下一步」= 打分精简：完成陈述被滤掉，行动/征询句保留；后续新回复覆盖旧的
    rows = [
        _u("改一下"),
        _a("改好了。\n\n要我继续做 B 么"),
        _u("继续"),
        _a([{"type": "text", "text": "B 也完成。\n验证后我们再发版"}]),
    ]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s.jsonl", rows), "p", 5)
    assert parsed.next_hint == "验证后我们再发版"


def test_hint_text_scoring_and_noise_filter():
    from token_tracker.sidebar import _hint_text
    reply = "\n".join([
        "## 结论",
        "细节修改已提交（abc123）。",     # 完成陈述 → 滤掉
        "```python",                     # 代码块整段剔除
        "print('hi')",
        "```",
        "---",                           # 分隔线剔除
        "| a | b |",                     # 表格行剔除
        "**加粗的建议**",                 # 粗体星号剥掉 + 「建议」行动词保留
        "重启侧边栏生效。要不要我继续做 B？",  # 切句：两句都是正分
    ])
    got = _hint_text(reply)
    assert got.splitlines() == ["加粗的建议", "重启侧边栏生效。", "要不要我继续做 B？"]
    assert "已提交" not in got and "print" not in got and "##" not in got and "**" not in got


def test_hint_text_falls_back_to_last_line():
    from token_tracker.sidebar import _hint_text
    # 纯汇报无任何行动信号 → 回退最后一个有效行（单行，精简）
    assert _hint_text("统计口径核对完毕。\n数字与后台一致。") == "数字与后台一致。"


def test_hint_text_only_scans_last_paragraph():
    from token_tracker.sidebar import _hint_text
    # 只看最后一段（主人定）：中段的大纲列表、前段的问句都不再进「下一步」
    reply = "\n".join([
        "大纲如下：",
        "1. 开头要不要放钩子？",
        "2. 中间论证",
        "",
        "先看第 1 节，看完告诉我。",
    ])
    assert _hint_text(reply) == "先看第 1 节，看完告诉我。"


def test_hint_text_trailing_code_block_falls_back():
    from token_tracker.sidebar import _hint_text
    # 结尾是内含空行的代码块（段落切分的坑）：代码不漏进提示、回退到代码块前的段落
    reply = "\n".join([
        "跑一下这个验证：",
        "",
        "```python",
        "a = 1",
        "",
        "b = 2",
        "```",
    ])
    assert _hint_text(reply) == "跑一下这个验证："


def test_ask_user_question_takes_priority(tmp_path):
    # 待回答的 AskUserQuestion（结构化字段，零猜测）优先于文本打分
    ask = [{"type": "tool_use", "id": "t1", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "范围选哪个？", "header": "范围",
                                     "options": [{"label": "只做方向一"}, {"label": "一二连做"}],
                                     "multiSelect": False}]}}]
    rows = [_u("开工"), _a("我先确认范围。建议尽快定。"), _a(ask)]
    parsed = _parse_claude(_write_jsonl(tmp_path / "a.jsonl", rows), "p", 5)
    assert parsed.next_hint == "范围选哪个？\n· 只做方向一 / 一二连做"
    # 用户回答（tool_result）后提问被消费 → 回落文本打分
    rows += [_u([{"type": "tool_result", "tool_use_id": "t1", "content": "只做方向一"}]),
             _a("好，方向一开工。做完说一声。")]
    parsed = _parse_claude(_write_jsonl(tmp_path / "b.jsonl", rows), "p", 5)
    assert "范围选哪个" not in parsed.next_hint
    assert parsed.next_hint.splitlines()[-1] == "做完说一声。"  # 「开工」也是行动词，前一句保留属合理


def test_codex_next_hint_from_agent_message(tmp_path):
    rows = [
        {"timestamp": "2026-07-12T02:00:00.000Z", "type": "session_meta",
         "payload": {"id": "cx", "timestamp": "2026-07-12T02:00:00.000Z", "cwd": "/tmp/x"}},
        {"timestamp": "2026-07-12T02:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "做个功能"}},
        {"timestamp": "2026-07-12T02:00:09.000Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "完成了。\n需要我补测试吗"}},
    ]
    parsed = _parse_codex(_write_jsonl(tmp_path / "r.jsonl", rows), 5)
    assert parsed.next_hint == "需要我补测试吗"


def test_render_next_hint_and_spinner_frame():
    now = datetime.now(UTC)
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=RUNNING,
                            prompts=[Prompt("干活", now)], next_hint="验证后我们再发版")]
    console = Console(record=True, width=60, force_terminal=True)
    console.print(render_sidebar(sessions, spinner_frame=0))
    console.print(render_sidebar(sessions, spinner_frame=1))
    out = console.export_text()
    assert "验证后我们再发版" in out
    assert "✢ proj" in out and "✳ proj" in out  # 帧不同 → 会话头星形符号轮转（标题自带 ✳，不数全局）


def test_render_hint_wraps_to_three_lines_then_ellipsis():
    # 「下一步」一行原文正常折行（主人定）：最多 3 行、仍放不下才在第三行末省略；
    # 折行/续行等宽空格悬挂对齐正文列，右侧留 2 格
    now = datetime.now(UTC)
    long_line = "这是特别长的建议内容" * 8  # 160 格，远超 3 行容量
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=WAITING,
                            prompts=[Prompt("短", now)],
                            next_hint=f"第一行建议\n{long_line}\n1. 选项甲")]
    console = Console(record=True, width=40, force_terminal=True)
    console.print(render_sidebar(sessions))
    out = console.export_text().splitlines()
    arrow_idx = next(i for i, ln in enumerate(out) if "↳" in ln)
    hint_lines = out[arrow_idx:]
    assert len(hint_lines) == 5  # 1 + 超长行折满 3 行 + 1
    assert "第一行建议" in hint_lines[0]
    assert not hint_lines[1].rstrip().endswith("…")  # 前两折行正常显示
    assert hint_lines[3].rstrip().endswith("…")      # 3 行仍放不下 → 末行省略
    assert "选项甲" in hint_lines[4]
    first_text_col = hint_lines[0].index(":") + 2  # 正文起始列
    assert all(ln[:first_text_col].strip() == "" for ln in hint_lines[1:])  # 悬挂对齐
    assert all(len(ln.rstrip()) <= 40 - 2 for ln in hint_lines)  # 右侧留白 2 格


def test_fold_cjk_fills_and_ascii_word_atomic():
    from rich.cells import cell_len

    from token_tracker.ui.sidebar import _fold
    # CJK 逐字可折、填满换行
    assert _fold("中" * 10, 8) == ["中中中中", "中中中中", "中中"]
    # 英文单词是原子 token：放不下整个挪下一行，不拦腰切
    got = _fold("中中中中 provider 之后", 10)
    assert any(ln == "provider" or ln.startswith("provider") for ln in got)
    assert all("provide" not in ln or "provider" in ln for ln in got)  # 没有被切成 provide/r
    # 单 token 超过整行宽（长 URL）：按格硬切兜底
    got = _fold("https://example.com/very/long/path", 10)
    assert len(got) > 1 and all(cell_len(ln) <= 10 for ln in got)


def test_render_mixed_cjk_ascii_fills_width():
    # 回归：Rich 词折行把空格后的整段中文当一个词挪下行，省略号/换行
    # 出现在断词点、右侧大片留白；硬折后每行应填满可用宽度再折行
    now = datetime.now(UTC)
    sessions = [LiveSession(agent_id="claude-code", session_id="s1", project="proj",
                            last_activity=now, state=WAITING,
                            prompts=[Prompt("短", now)],
                            next_hint="取最后一条 AI 回复的尾部继续更多内容这一行非常长必须折行")]
    console = Console(record=True, width=40, force_terminal=True)
    console.print(render_sidebar(sessions))
    from rich.cells import cell_len
    hint_line = next(ln for ln in console.export_text().splitlines() if "↳" in ln)
    # 按终端格宽（CJK 一字两格）首行应填满到右留白附近，不在断词点提前留白换行
    assert cell_len(hint_line.rstrip()) >= 40 - 2 - 2


def test_render_header_count_and_dual_clocks():
    import re as _re
    from zoneinfo import ZoneInfo
    now = datetime.now(UTC)
    sessions = [LiveSession(agent_id="claude-code", session_id=f"s{i}", project="p",
                            last_activity=now, state=WAITING, prompts=[Prompt("x", now)])
                for i in range(3)]
    console = Console(record=True, width=60, force_terminal=True)
    console.print(render_sidebar(sessions))
    out = console.export_text().splitlines()
    assert "3" in out[0]  # 活跃会话计数在标题行
    clock_lines = [ln for ln in out if _re.search(r"\d{2}:\d{2}:\d{2}", ln)]
    assert len(clock_lines) == 3  # 北京 + 洛杉矶 + 伦敦三行时钟
    for i, tz_name in enumerate(("Asia/Shanghai", "America/Los_Angeles", "Europe/London")):
        local = datetime.now(ZoneInfo(tz_name))
        assert local.strftime("%m-%d") in clock_lines[i]
        assert local.strftime("%H:%M") in clock_lines[i]  # 秒可能跨帧，比对到分钟


def test_render_branch_and_session_separator():
    # 项目名后接 (分支)（statusline L1 同款）；会话块之间一条分割线、首块前是空行
    now = datetime.now(UTC)
    mk = lambda i: LiveSession(agent_id="claude-code", session_id=f"s{i}", project=f"p{i}",  # noqa: E731
                               last_activity=now, state=WAITING, branch="main",
                               prompts=[Prompt("x", now)])
    console = Console(record=True, width=50, force_terminal=True)
    console.print(render_sidebar([mk(1), mk(2), mk(3)]))
    out = console.export_text().splitlines()
    assert sum(1 for ln in out if "p1(main)" in ln or "p2(main)" in ln or "p3(main)" in ln) == 3
    rules = [ln for ln in out if set(ln.strip()) == {"─"}]
    assert len(rules) == 2  # 3 个会话块之间 2 条分割线


def test_render_sidebar_empty():
    console = Console(record=True, width=60)
    console.print(render_sidebar([]))
    assert "tt sidebar" in console.export_text()
