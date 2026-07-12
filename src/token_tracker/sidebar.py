"""tt sidebar 数据层：扫描活跃会话、提取提示词历史、推断会话状态。

数据源与调研结论见本地 sidebar-research/README.md（不入 git）：
- CC transcript jsonl（`~/.claude/projects/`）——提示词唯一来源；格式官方不承诺稳定，
  全程防御式解析：缺字段/类型不对一律跳过不崩。
- Codex rollout jsonl（`~/.codex/sessions/`）——`user_message` 事件干净可靠，
  `task_started` / `task_complete` 供状态判定。
- 心跳 `config.STATUS_FILE`（CC statusline 每帧落盘）——`session_id` + `_received_at`
  判「正在跑」，白拿、零新增开销。
hooks 事件流（PermissionRequest 等授权的精确信号）留 v2 接入；当前状态为启发式推断。
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .adapters import claude as claude_adapter
from .adapters import codex as codex_adapter
from .adapters.util import iter_jsonl_dicts, project_from_cwd

# 会话状态（启发式，见 _infer_state；ATTENTION 无法区分「等授权」和「长工具在跑」，v2 接 hooks 后才能）
RUNNING = "running"      # 正在生成 / 写盘
ATTENTION = "attention"  # 有未完成的工具调用且不再写盘——大概率在等授权
WAITING = "waiting"      # 轮次已结束，等下一条输入
IDLE = "idle"            # 长时间无动静

RUNNING_WINDOW_S = 30       # transcript 多久内有写盘算「正在跑」
HEARTBEAT_FRESH_S = 15      # 心跳多新算「正在跑」
IDLE_AFTER_S = 30 * 60      # 多久无动静降级为 idle

DEFAULT_HOURS_BACK = 12     # 只看窗口期内有动静的会话
DEFAULT_MAX_PROMPTS = 5     # 每会话保留最近 N 条提示词

# CC 里非「人敲的提示词」的内容前缀（slash command 记录 / 本地命令回显 / 中断标记 /
# 后台任务通知 / harness 注入的 system-reminder）——按文本片段级过滤，见 _claude_prompt_text
_CLAUDE_SKIP_PREFIXES = ("<command-", "<local-command-", "[Request interrupted",
                         "<task-notification", "<system-reminder")
# Codex 里包装成 user_message 的注入内容（用户指令模板 / 环境上下文等）
_CODEX_SKIP_PREFIXES = ("<user_instructions", "<environment_context", "<ide_", "<permissions", "<turn_")

_CACHE_MAX = 512  # 解析缓存上限（常驻进程防无限增长，超了整体重建）


@dataclass
class Prompt:
    text: str
    timestamp: datetime | None


@dataclass
class LiveSession:
    agent_id: str
    session_id: str
    project: str
    last_activity: datetime                       # transcript mtime（UTC aware）
    state: str
    prompts: list[Prompt] = field(default_factory=list)  # 时间正序，最后一条最新
    model: str = ""


@dataclass
class _Parsed:
    """单个 transcript 的解析结果（与「现在几点」无关的部分，可按 mtime+size 缓存）。"""
    session_id: str
    project: str
    prompts: list[Prompt]
    pending_tool: bool  # 末个工具调用尚无结果（CC）/ task 未 complete（Codex）
    model: str = ""


# 按 (mtime, size) 缓存解析结果：常驻刷新时只重解析有变化的文件
_parse_cache: dict[str, tuple[float, int, _Parsed]] = {}


def scan_sessions(hours_back: int = DEFAULT_HOURS_BACK,
                  max_prompts: int = DEFAULT_MAX_PROMPTS,
                  agent_ids: set[str] | None = None) -> list[LiveSession]:
    """窗口期内有动静的会话，按最近活动倒序。agent_ids=None 表示不过滤。"""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours_back)
    heartbeat = _read_heartbeat()
    sessions: list[LiveSession] = []
    if agent_ids is None or "claude-code" in agent_ids:
        sessions.extend(_scan_claude_sessions(cutoff, now, heartbeat, max_prompts))
    if agent_ids is None or "codex" in agent_ids:
        sessions.extend(_scan_codex_sessions(cutoff, now, heartbeat, max_prompts))
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions


def _infer_state(now: datetime, last_activity: datetime,
                 pending_tool: bool, heartbeat_fresh: bool) -> str:
    age = (now - last_activity).total_seconds()
    if heartbeat_fresh or age < RUNNING_WINDOW_S:
        return RUNNING
    if pending_tool:
        return ATTENTION
    if age > IDLE_AFTER_S:
        return IDLE
    return WAITING


def _read_heartbeat() -> tuple[str, datetime] | None:
    """CC statusline 心跳：(session_id, 最近一帧时间)。文件只反映最近渲染的那一个会话。"""
    try:
        with open(config.STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        sid = data.get("session_id") or ""
        ts = datetime.fromisoformat(data.get("_received_at", ""))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not sid:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return sid, ts


def _heartbeat_fresh(heartbeat: tuple[str, datetime] | None, session_id: str, now: datetime) -> bool:
    return (heartbeat is not None and heartbeat[0] == session_id
            and (now - heartbeat[1]).total_seconds() < HEARTBEAT_FRESH_S)


def _cache_get(path: Path) -> tuple[float, _Parsed | None]:
    """返回 (mtime, 缓存命中的解析结果)；未命中返回 (mtime, None)。文件消失返回 (0, None)。"""
    try:
        st = path.stat()
    except OSError:
        return 0.0, None
    hit = _parse_cache.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return st.st_mtime, hit[2]
    return st.st_mtime, None


def _cache_put(path: Path, parsed: _Parsed) -> None:
    if len(_parse_cache) > _CACHE_MAX:
        _parse_cache.clear()
    try:
        st = path.stat()
    except OSError:
        return
    _parse_cache[str(path)] = (st.st_mtime, st.st_size, parsed)


# --- Claude Code ---

def _scan_claude_sessions(cutoff: datetime, now: datetime,
                          heartbeat: tuple[str, datetime] | None,
                          max_prompts: int,
                          dirs: list[str] | None = None) -> list[LiveSession]:
    """dirs 供测试注入；默认复用 claude adapter 的目录解析（CLAUDE_CONFIG_DIR 等）。"""
    sessions: list[LiveSession] = []
    seen: set[str] = set()
    for base_dir in (dirs if dirs is not None else claude_adapter._get_claude_dirs()):
        base = Path(base_dir)
        if not base.is_dir():
            continue
        for path in base.rglob("*.jsonl"):
            mtime, parsed = _cache_get(path)
            if mtime <= 0:
                continue
            last_activity = datetime.fromtimestamp(mtime, UTC)
            if last_activity < cutoff:
                continue
            if parsed is None:
                fallback = claude_adapter._extract_project_from_dir(path, base)
                parsed = _parse_claude(path, fallback, max_prompts)
                if parsed is None:
                    continue
                _cache_put(path, parsed)
            if not parsed.prompts or parsed.session_id in seen:
                continue
            seen.add(parsed.session_id)
            sessions.append(LiveSession(
                agent_id="claude-code",
                session_id=parsed.session_id,
                project=parsed.project,
                last_activity=last_activity,
                state=_infer_state(now, last_activity, parsed.pending_tool,
                                   _heartbeat_fresh(heartbeat, parsed.session_id, now)),
                prompts=parsed.prompts,
                model=parsed.model,
            ))
    return sessions


def _parse_claude(path: Path, fallback_project: str, max_prompts: int) -> _Parsed | None:
    session_id = path.stem
    project = fallback_project
    prompts: list[Prompt] = []
    pending_tool = False
    model = ""
    for data in iter_jsonl_dicts(path):
        if data.get("isSidechain"):  # 子代理 sidechain 的消息不是主人敲的提示词
            continue
        dtype = data.get("type")
        if dtype == "user":
            sid = data.get("sessionId")
            if sid:
                session_id = sid
            cwd = data.get("cwd")
            if cwd:
                project = project_from_cwd(cwd)
            if data.get("isMeta"):
                continue
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if _is_tool_result(content):
                pending_tool = False
                continue
            text = _claude_prompt_text(content)
            if text is None:
                continue
            prompts.append(Prompt(text=text, timestamp=_parse_ts(data.get("timestamp"))))
            pending_tool = False
        elif dtype == "assistant":
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            model = message.get("model") or model
            content = message.get("content")
            if isinstance(content, list):
                pending_tool = any(isinstance(i, dict) and i.get("type") == "tool_use" for i in content)
            elif isinstance(content, str):
                pending_tool = False
    if not prompts:
        return None
    return _Parsed(session_id, project, prompts[-max_prompts:], pending_tool, model)


def _is_tool_result(content: object) -> bool:
    return (isinstance(content, list)
            and any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content))


def _claude_prompt_text(content: object) -> str | None:
    """user 行 content → 提示词文本；不是人敲的（命令记录 / 注入通知 / 空内容）返回 None。

    过滤按**文本片段级**做：注入内容（task-notification / system-reminder）可能与
    真提示词同处一条消息的不同 text 块，逐片段判前缀、只丢噪音片段，避免误杀真提示词。
    """
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [i.get("text", "") for i in content
                 if isinstance(i, dict) and i.get("type") == "text"]
    else:
        return None
    kept = [p.strip() for p in parts
            if p.strip() and not p.strip().startswith(_CLAUDE_SKIP_PREFIXES)]
    if not kept:
        return None
    return "\n".join(kept)


# --- Codex ---

def _scan_codex_sessions(cutoff: datetime, now: datetime,
                         heartbeat: tuple[str, datetime] | None,
                         max_prompts: int,
                         sessions_dir: str | None = None) -> list[LiveSession]:
    base = Path(sessions_dir if sessions_dir is not None else codex_adapter.SESSIONS_DIR)
    if not base.is_dir():
        return []
    models = codex_adapter._load_thread_models()
    sessions: list[LiveSession] = []
    seen: set[str] = set()
    for path in base.rglob("*.jsonl"):
        mtime, parsed = _cache_get(path)
        if mtime <= 0:
            continue
        last_activity = datetime.fromtimestamp(mtime, UTC)
        if last_activity < cutoff:
            continue
        if parsed is None:
            parsed = _parse_codex(path, max_prompts)
            if parsed is None:
                continue
            _cache_put(path, parsed)
        if not parsed.prompts or parsed.session_id in seen:
            continue
        seen.add(parsed.session_id)
        sessions.append(LiveSession(
            agent_id="codex",
            session_id=parsed.session_id,
            project=parsed.project,
            last_activity=last_activity,
            state=_infer_state(now, last_activity, parsed.pending_tool,
                               _heartbeat_fresh(heartbeat, parsed.session_id, now)),
            prompts=parsed.prompts,
            model=parsed.model or models.get(parsed.session_id, ""),
        ))
    return sessions


def _parse_codex(path: Path, max_prompts: int) -> _Parsed | None:
    session_id = ""
    project = "unknown"
    prompts: list[Prompt] = []
    pending_task = False
    for data in iter_jsonl_dicts(path):
        payload = data.get("payload")
        if not isinstance(payload, dict):
            continue
        dtype = data.get("type")
        if dtype == "session_meta":
            session_id = payload.get("id", "") or session_id
            cwd = payload.get("cwd", "")
            if cwd:
                project = project_from_cwd(cwd)
        elif dtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message":
                text = (payload.get("message") or "").strip()
                if text and not text.startswith(_CODEX_SKIP_PREFIXES):
                    prompts.append(Prompt(text=text, timestamp=_parse_ts(data.get("timestamp"))))
            elif ptype == "task_started":
                pending_task = True
            elif ptype in ("task_complete", "turn_aborted"):
                pending_task = False
    if not prompts:
        return None
    return _Parsed(session_id or path.stem, project, prompts[-max_prompts:], pending_task)


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
