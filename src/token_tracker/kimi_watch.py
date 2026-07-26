"""`tt kimi-watch`：Kimi Code 常驻实时窗格（Rich Live，三行内容）。

① 活跃会话（wire.jsonl mtime<30s 或 Stop 心跳新鲜 → 「运行中」）：项目名 + 模型 + 最近 output tok/s；
② 5h / 7d 额度进度条（重置倒计时）+ Extra ¥余额：颜色按**剩余**比例分档（>50% 绿 / 20-50% 黄 / <20% 红）；
③ 今日累计 in/out/cache token + 估算成本（¥）+ 当前会话累计。

本地数据每 2 秒重扫（mtime 预筛，只解析今天动过的文件）；额度 API 每 60 秒轮询，
失败保留上次值并灰色标注 (stale)。`--once` 或非 tty 输出单帧即退。
宽度不足先隐藏倒计时、再把进度条退化为纯百分比（仿 codex_statusline 降级思路）。
"""

import json
import os
import re
import sys
import time
from datetime import UTC, datetime

from rich.console import Group
from rich.live import Live
from rich.text import Text

from . import config
from .adapters import kimi
from .adapters.types import RateLimits, UsageEntry
from .analyzer.cost import _CNY_PER_USD, calculate_cost
from .tz import system_tz
from .ui.console import _configure_windows_stdout_utf8, forced_color_console, get_console
from .ui.format import _fmt_tokens
from .ui.tables import _bar_text
from .ui.theme import _S

_LOCAL_REFRESH_SECONDS = 2.0
_QUOTA_REFRESH_SECONDS = 60.0
_ACTIVE_MTIME_SECONDS = 30  # wire.jsonl mtime 在此窗口内判「运行中」
_RATE_WINDOW_SECONDS = 60  # tok/s 统计窗口
_BAR_WIDTH = 12
# 进度条在宽度不足时的两档降级阈值
_WIDTH_HIDE_COUNTDOWN = 16
_WIDTH_HIDE_BAR = 40


# --- 心跳（tt kimi-heartbeat 写、kimi-watch 读） ---


def _stdin_session_id() -> str:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("session_id", "sessionId"):
        if isinstance(data.get(key), str):
            return data[key]
    session = data.get("session")
    return session.get("id", "") if isinstance(session, dict) else ""


def write_heartbeat() -> None:
    session_id = _stdin_session_id()
    previous = _read_heartbeat() or {}
    sessions = previous.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    now = time.time()
    sessions = {str(key): value for key, value in sessions.items()
                if isinstance(value, (int, float)) and now - value < _ACTIVE_MTIME_SECONDS}
    if session_id:
        sessions[session_id] = now
    payload = {"sessions": sessions, "session_id": session_id, "ts": now}
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    temp = f"{config.KIMI_HEARTBEAT_FILE}.tmp-{os.getpid()}"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(temp, config.KIMI_HEARTBEAT_FILE)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass


def _read_heartbeat() -> dict | None:
    try:
        with open(config.KIMI_HEARTBEAT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

# --- 本地扫描 ---


def _scan_active_sessions(now: float) -> list[dict]:
    """扫 wire.jsonl：mtime<30s 判运行中；统计最近 60s 的 output tok/s。心跳新鲜时也把
    对应 session 标为运行中（Stop hook 触发说明会话活着但模型此刻未必在写 wire）。"""
    heartbeat = _read_heartbeat() or {}
    heartbeat_sessions = heartbeat.get("sessions")
    if not isinstance(heartbeat_sessions, dict):
        heartbeat_sessions = {}
    fresh_sessions = {str(session_id) for session_id, ts in heartbeat_sessions.items()
                      if isinstance(ts, (int, float)) and now - ts < _ACTIVE_MTIME_SECONDS}

    sessions: dict[str, dict] = {}
    sessions_dir = kimi.SESSIONS_DIR
    if os.path.isdir(sessions_dir):
        workspaces = kimi._load_workspaces()
        for session_name, _agent, wire_path in kimi._iter_wire_files(sessions_dir):
            try:
                mtime = wire_path.stat().st_mtime
            except OSError:
                continue
            wire_running = now - mtime < _ACTIVE_MTIME_SECONDS
            # 只细看活跃文件；非活跃文件也要登记（心跳可能点亮它），但不逐行解析
            info = sessions.setdefault(session_name, {
                "running": False, "output_tokens": 0, "model": "", "mtime": 0.0,
                "project": kimi._project_for(wire_path.parent.parent.parent.parent.name, workspaces),
            })
            info["running"] = info["running"] or wire_running
            info["mtime"] = max(info["mtime"], mtime)
            if not wire_running and now - mtime > _RATE_WINDOW_SECONDS:
                continue
            for data in kimi.iter_jsonl_dicts(wire_path):
                if data.get("type") != "usage.record":
                    continue
                time_ms = data.get("time")
                if not isinstance(time_ms, int | float) or now - time_ms / 1000 > _RATE_WINDOW_SECONDS:
                    continue
                usage = data.get("usage")
                if isinstance(usage, dict):
                    info["output_tokens"] += int(usage.get("output") or 0)
                raw_model = data.get("model")
                if isinstance(raw_model, str) and raw_model:
                    info["model"] = kimi._strip_model_prefix(raw_model)

    result = []
    for session_name, info in sessions.items():
        running = info["running"] or any(session_id in session_name for session_id in fresh_sessions)
        if running or now - info["mtime"] < _RATE_WINDOW_SECONDS:
            result.append({
                "session": session_name,
                "project": info["project"],
                "model": info["model"],
                "running": running,
                "tok_per_s": info["output_tokens"] / _RATE_WINDOW_SECONDS,
            })
    result.sort(key=lambda s: (not s["running"], s["project"]))
    return result


def _today_totals(session_id: str = "") -> dict:
    """Aggregate today from individual usage records, not session start time."""
    tz = system_tz()
    today = datetime.now(tz).date()
    hb_session = session_id
    total = {"input": 0, "output": 0, "cache": 0, "cost": 0.0}
    current = {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "found": False}
    workspaces = kimi._load_workspaces()
    for session_name, agent_name, wire_path in kimi._iter_wire_files(kimi.SESSIONS_DIR):
        project = kimi._project_for(wire_path.parent.parent.parent.parent.name, workspaces)
        for data in kimi.iter_jsonl_dicts(wire_path):
            if data.get("type") != "usage.record" or not isinstance(data.get("usage"), dict):
                continue
            time_ms = data.get("time")
            if not isinstance(time_ms, (int, float)):
                continue
            timestamp = datetime.fromtimestamp(time_ms / 1000, tz=UTC)
            if timestamp.astimezone(tz).date() != today:
                continue
            usage = data["usage"]
            entry = UsageEntry(timestamp=timestamp, session_id=f"{session_name}:{agent_name}",
                message_id=f"{session_name}:{agent_name}:{time_ms}", request_id="",
                model=kimi._strip_model_prefix(str(data.get("model") or "unknown")),
                input_tokens=int(usage.get("inputOther") or 0), output_tokens=int(usage.get("output") or 0),
                cache_creation_tokens=int(usage.get("inputCacheCreation") or 0),
                cache_read_tokens=int(usage.get("inputCacheRead") or 0), cost_usd=None,
                project=project, agent_id="kimi-code")
            cost = calculate_cost(entry)
            total["input"] += entry.input_tokens
            total["output"] += entry.output_tokens
            total["cache"] += entry.cache_read_tokens + entry.cache_creation_tokens
            total["cost"] += cost
            if hb_session and hb_session in session_name:
                current["found"] = True
                current["input"] += entry.input_tokens
                current["output"] += entry.output_tokens
                current["cache"] += entry.cache_read_tokens + entry.cache_creation_tokens
                current["cost"] += cost
    for data in (total, current):
        data["cost"] *= _CNY_PER_USD
    return {"today": total, "current": current}


# --- 渲染 ---


def _remaining_style(pct_used: float) -> str:
    """按剩余比例分档：>50% 绿 / 20-50% 黄 / <20% 红（剩余越少越红）。"""
    remaining = 100.0 - pct_used
    if remaining > 50:
        return _S.bar_low
    if remaining > 20:
        return _S.bar_mid
    return _S.bar_high


def _fmt_countdown(resets_at: int | None, now: float) -> str:
    if not resets_at or resets_at < now:
        return ""
    seconds = int(resets_at - now)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _render_quota_segment(label: str, pct: float | None, resets_at: int | None,
                          now: float, width: int, stale: bool) -> Text:
    """单个额度段：窄屏先隐藏倒计时、再退化为纯百分比。"""
    seg = Text(f" {label} ")
    dim = stale or pct is None
    if pct is None:
        seg.append("--", style=_S.dim)
        return seg
    style = "dim" if dim else _remaining_style(pct)
    if width >= _WIDTH_HIDE_BAR:
        seg.append_text(_bar_text(pct / 100, style, width=_BAR_WIDTH))
        seg.append(f" {pct:.0f}%", style=style)
    else:
        seg.append(f"{pct:.0f}%", style=style)
    if width >= _WIDTH_HIDE_COUNTDOWN:
        countdown = _fmt_countdown(resets_at, now)
        if countdown:
            seg.append(f" ⏳{countdown}", style=_S.dim)
    return seg


def render_frame(active: list[dict], quota: RateLimits | None, quota_stale: bool,
                 extra_yuan: float | None, totals: dict) -> Group:
    width = get_console().width
    now = time.time()

    # ① 活跃会话行
    line1 = Text()
    if not active:
        line1.append(" ● 无活跃会话", style=_S.dim)
    else:
        for i, s in enumerate(active[:3]):
            if i:
                line1.append("   ")
            dot_style = _S.good if s["running"] else _S.dim
            line1.append("● ", style=dot_style)
            line1.append(s["project"], style=f"bold {_S.good}" if s["running"] else "")
            if s["model"]:
                line1.append(f" {s['model']}", style=_S.dim)
            line1.append(f" {s['tok_per_s']:.1f} tok/s", style=_S.peach)
            if s["running"]:
                line1.append(" 运行中", style=_S.good)
        if len(active) > 3:
            line1.append(f"   +{len(active) - 3}", style=_S.dim)

    # ② 额度行
    quota_5h = Text()
    quota_7d = Text()
    if quota is None:
        quota_5h.append(" {message}", style=_S.dim)
    else:
        quota_5h.append_text(_render_quota_segment("5h", quota.five_hour_pct, quota.five_hour_resets_at,
                                                    now, width, quota_stale))
        quota_7d.append_text(_render_quota_segment("7d", quota.seven_day_pct, quota.seven_day_resets_at,
                                                    now, width, quota_stale))
        if extra_yuan is not None:
            quota_7d.append(" |", style=_S.dim)
            quota_7d.append(f" Extra ¥{extra_yuan:.2f}", style="" if quota_stale else _S.good)
        if quota_stale:
            quota_7d.append(" (stale)", style=_S.dim)

    # ③ 今日累计 + 当前会话行
    today = totals["today"]
    current = totals["current"]
    today_in = Text()
    today_in.append(" 今日 in ", style=_S.dim)
    today_in.append(_fmt_tokens(today["input"]), style=_S.token)

    today_out = Text()
    today_out.append(" 今日 out ", style=_S.dim)
    today_out.append(_fmt_tokens(today["output"]), style=_S.token)

    today_cache = Text()
    today_cache.append(" 今日 cache ", style=_S.dim)
    today_cache.append(_fmt_tokens(today["cache"]), style=_S.token)
    today_cache.append(f"  ≈¥{today['cost']:.2f}", style=_S.cost)
    if current["found"]:
        today_cache.append("  |  本会话 ", style=_S.dim)
        today_cache.append(f"out {_fmt_tokens(current['output'])}", style=_S.token)
        today_cache.append(f"  ≈¥{current['cost']:.2f}", style=_S.cost)

    return Group(line1, quota_5h, quota_7d, today_in, today_out, today_cache)


# --- 命令入口 ---


def _extra_from_plan_type(plan_type: str) -> float | None:
    """load_rate_limits 把 Extra 余额拼进 plan_type（"… · Extra ¥12.34"），从文本取回数值。"""
    match = re.search(r"Extra ¥([0-9]+(?:\.[0-9]+)?)", plan_type or "")
    return float(match.group(1)) if match else None


def _collect(now: float, quota_state: dict, session_id: str = "") -> tuple:
    """扫一帧数据。额度每 60s 轮询一次，失败保留上次值并标 stale。"""
    active = _scan_active_sessions(now)
    if now - quota_state["fetched_at"] >= _QUOTA_REFRESH_SECONDS:
        quota_state["fetched_at"] = now
        fresh = kimi.load_rate_limits()
        if fresh is not None:
            quota_state["quota"] = fresh
            quota_state["extra"] = _extra_from_plan_type(fresh.plan_type)
            quota_state["stale"] = False
        else:
            quota_state["stale"] = quota_state["quota"] is not None
    totals = _today_totals(session_id)
    return active, quota_state["quota"], quota_state["stale"], quota_state.get("extra"), totals


def cmd_kimi_watch(args: list[str]) -> None:
    _configure_windows_stdout_utf8()
    session_id = ""
    if "--session-id" in args:
        index = args.index("--session-id")
        if index + 1 < len(args):
            session_id = args[index + 1]
    quota_state = {"quota": None, "extra": None, "stale": False, "fetched_at": 0.0}

    if "--once" in args or not sys.stdout.isatty():
        with forced_color_console():
            frame = render_frame(*_collect(time.time(), quota_state, session_id))
            get_console().print(frame)
        return

    console = get_console()
    with Live(console=console, refresh_per_second=1) as live:
        try:
            while True:
                live.update(render_frame(*_collect(time.time(), quota_state, session_id)))
                time.sleep(_LOCAL_REFRESH_SECONDS)
        except KeyboardInterrupt:
            pass
