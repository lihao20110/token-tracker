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


def _u(content, ts: str = "2026-07-12T02:00:00.000Z", **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content},
            "timestamp": ts, **extra}


def _a(content, model: str = "claude-fable-5") -> dict:
    return {"type": "assistant", "timestamp": "2026-07-12T02:00:05.000Z",
            "message": {"role": "assistant", "model": model, "content": content}}


# --- CC transcript 解析 ---

def test_claude_prompt_extraction_filters_noise(tmp_path):
    # 只保留人敲的提示词：slash command 记录 / isMeta / tool_result / 子代理 sidechain 全过滤
    rows = [
        {"type": "summary", "summary": "历史摘要行"},
        _u("<command-name>/clear</command-name>"),
        _u("<local-command-caveat>Caveat: ...</local-command-caveat>", isMeta=True),
        _u("真提示词一", sessionId="s-abc"),
        _u([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        _u([{"type": "image", "source": {}}, {"type": "text", "text": "带图提示词"}]),
        _u("子代理里的任务描述", isSidechain=True),
        _u("[Image: source: /tmp/x.png]", isMeta=True),
        _u("[Request interrupted by user for tool use]"),
    ]
    parsed = _parse_claude(_write_jsonl(tmp_path / "s-abc.jsonl", rows), "fallback", 5)
    assert parsed is not None
    assert [p.text for p in parsed.prompts] == ["真提示词一", "带图提示词"]
    assert parsed.session_id == "s-abc"
    assert parsed.prompts[0].timestamp is not None


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
         "payload": {"id": "cx-1", "timestamp": "2026-07-12T02:00:00.000Z", "cwd": "/tmp/nope/beta"}},
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
    old = _write_jsonl(d / "old.jsonl", [_u("久远会话")])
    ancient_ts = (now - timedelta(hours=24)).timestamp()
    os.utime(old, (ancient_ts, ancient_ts))
    mid = _write_jsonl(d / "mid.jsonl", [_u("两分钟前的")])
    mid_ts = (now - timedelta(seconds=120)).timestamp()
    os.utime(mid, (mid_ts, mid_ts))
    fresh = _write_jsonl(d / "fresh.jsonl", [_u("刚刚的")])
    fresh_ts = (now - timedelta(seconds=3)).timestamp()
    os.utime(fresh, (fresh_ts, fresh_ts))

    got = _scan_claude_sessions(now - timedelta(hours=12), now, None, 3, dirs=[str(base)])
    got.sort(key=lambda s: s.last_activity, reverse=True)
    assert [s.session_id for s in got] == ["fresh", "mid"]  # 24h 前的被窗口过滤
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


def test_render_sidebar_empty():
    console = Console(record=True, width=60)
    console.print(render_sidebar([]))
    assert "tt sidebar" in console.export_text()
