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
import re
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

DEFAULT_HOURS_BACK = 5      # 只看窗口期内有动静的会话
DEFAULT_MAX_PROMPTS = 5     # 每会话保留最近 N 条提示词
DEFAULT_MAX_SESSIONS = 10   # 最多显示 N 个会话（按最近活动取前 N，窗口内不足 N 就全显）

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
    branch: str = ""  # git 分支（CC=transcript 自带 gitBranch；codex=session_meta.git.branch）
    terminal: dict = field(default_factory=dict)  # 终端定位 {"iterm": ..., "tmux": ...}（statusline 采集），空=不可跳转
    next_hint: str = ""  # 「下一步」提示：末条 assistant 回复的最后一行（通常是建议/追问），无则空


@dataclass
class _Parsed:
    """单个 transcript 的解析结果（与「现在几点」无关的部分，可按 mtime+size 缓存）。"""
    session_id: str
    project: str
    prompts: list[Prompt]
    pending_tool: bool  # 末个工具调用尚无结果（CC）/ task 未 complete（Codex）
    model: str = ""
    branch: str = ""
    next_hint: str = ""
    # 最后一条有效事件（真实提示词/工具结果/AI 回复）的时间戳——「最近活动」的权威来源。
    # 不能用文件 mtime：CC 常驻进程会对闲置会话的 transcript 做不改内容的周期性触碰
    # （实测 design-agent 会话 5.5h 没动、mtime 却常新），mtime 只配当缓存键与窗口初筛。
    last_event: datetime | None = None


# 按 (mtime, size) 缓存解析结果：常驻刷新时只重解析有变化的文件
_parse_cache: dict[str, tuple[float, int, _Parsed]] = {}


def scan_sessions(hours_back: int = DEFAULT_HOURS_BACK,
                  max_prompts: int = DEFAULT_MAX_PROMPTS,
                  agent_ids: set[str] | None = None,
                  max_sessions: int = DEFAULT_MAX_SESSIONS) -> list[LiveSession]:
    """窗口期内有动静的会话，按最近活动倒序、最多取前 max_sessions 个。
    agent_ids=None 表示不过滤。"""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours_back)
    heartbeat, term_map = _read_status()
    sessions: list[LiveSession] = []
    if agent_ids is None or "claude-code" in agent_ids:
        sessions.extend(_scan_claude_sessions(cutoff, now, heartbeat, max_prompts, term_map=term_map))
    if agent_ids is None or "codex" in agent_ids:
        sessions.extend(_scan_codex_sessions(cutoff, now, heartbeat, max_prompts, term_map=term_map))
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions[:max_sessions]


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


def _read_status() -> tuple[tuple[str, datetime] | None, dict[str, dict]]:
    """读 CC statusline 落盘文件一次，返回 (心跳, 终端定位 map)。

    心跳 = (session_id, 最近一帧时间)，只反映最近渲染的那一个会话；
    终端定位 = `_terminal_map`（statusline ≥2.0 采集的 ITERM_SESSION_ID/TMUX_PANE，
    按 session_id 隔离），旧版脚本没有该字段时返回空 dict、点击跳转优雅降级。
    """
    try:
        with open(config.STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, {}
    term_map = data.get("_terminal_map")
    if not isinstance(term_map, dict):
        term_map = {}
    sid = data.get("session_id") or ""
    try:
        ts = datetime.fromisoformat(data.get("_received_at", ""))
    except ValueError:
        return None, term_map
    if not sid:
        return None, term_map
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (sid, ts), term_map


def terminal_info(session_id: str) -> dict:
    """某会话的终端定位（点击跳转用），无记录返回 {}。每次现读文件，保证拿最新。"""
    _, term_map = _read_status()
    info = term_map.get(session_id)
    return info if isinstance(info, dict) else {}


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
                          dirs: list[str] | None = None,
                          term_map: dict[str, dict] | None = None) -> list[LiveSession]:
    """dirs 供测试注入；默认复用 claude adapter 的目录解析（CLAUDE_CONFIG_DIR 等）。"""
    term_map = term_map or {}
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
            mtime_dt = datetime.fromtimestamp(mtime, UTC)
            if mtime_dt < cutoff:  # 初筛：内容事件时间必然 ≤ mtime，mtime 过老可安全跳过
                continue
            if parsed is None:
                fallback = claude_adapter._extract_project_from_dir(path, base)
                parsed = _parse_claude(path, fallback, max_prompts)
                if parsed is None:
                    continue
                _cache_put(path, parsed)
            if not parsed.prompts or parsed.session_id in seen:
                continue
            # 权威活动时间 = 内容里最后一条有效事件；CC 会对闲置会话做不改内容的 mtime 触碰
            last_activity = parsed.last_event or mtime_dt
            if last_activity < cutoff:
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
                branch=parsed.branch,
                terminal=term_map.get(parsed.session_id) or {},
                next_hint=parsed.next_hint,
            ))
    return sessions


def _parse_claude(path: Path, fallback_project: str, max_prompts: int) -> _Parsed | None:
    session_id = path.stem
    project = fallback_project
    prompts: list[Prompt] = []
    pending_tool = False
    model = ""
    last_reply = ""
    pending_question = ""  # 待回答的 AskUserQuestion（任何后续用户动作即视为不再待决）
    last_event: datetime | None = None
    branch = ""
    for data in iter_jsonl_dicts(path):
        if data.get("isSidechain"):  # 子代理 sidechain 的消息不是主人敲的提示词
            continue
        branch = data.get("gitBranch") or branch
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
            pending_question = ""  # 回答/新提示/打断，提问已被消费
            content = message.get("content")
            if _is_tool_result(content):
                pending_tool = False
                last_event = _parse_ts(data.get("timestamp")) or last_event
                continue
            text = _claude_prompt_text(content)
            if text is None:  # 注入通知/命令记录等：不算「主人动过」，不计活动时间
                continue
            ts = _parse_ts(data.get("timestamp"))
            prompts.append(Prompt(text=text, timestamp=ts))
            last_event = ts or last_event
            pending_tool = False
        elif dtype == "assistant":
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            last_event = _parse_ts(data.get("timestamp")) or last_event
            model = message.get("model") or model
            content = message.get("content")
            reply = ""
            if isinstance(content, list):
                pending_tool = any(isinstance(i, dict) and i.get("type") == "tool_use" for i in content)
                for item in content:
                    if (isinstance(item, dict) and item.get("type") == "tool_use"
                            and item.get("name") == "AskUserQuestion"):
                        q = _format_question(item.get("input") or {})
                        pending_question = q or pending_question
                parts = [i.get("text", "") for i in content
                         if isinstance(i, dict) and i.get("type") == "text"]
                reply = "\n".join(p for p in parts if p)
            elif isinstance(content, str):
                pending_tool = False
                reply = content
            if reply.strip():
                last_reply = reply
    if not prompts:
        return None
    # 「下一步」优先级链：结构化提问（待回答，零猜测）> 句子打分精简 > 末行兜底
    return _Parsed(session_id, project, prompts[-max_prompts:], pending_tool, model,
                   branch=branch, next_hint=pending_question or _hint_text(last_reply),
                   last_event=last_event)


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
                         sessions_dir: str | None = None,
                         term_map: dict[str, dict] | None = None) -> list[LiveSession]:
    term_map = term_map or {}  # Codex 伪 statusline 暂未采集终端定位，通常为空、点击不可用
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
        mtime_dt = datetime.fromtimestamp(mtime, UTC)
        if mtime_dt < cutoff:  # 初筛：内容事件时间必然 ≤ mtime
            continue
        if parsed is None:
            parsed = _parse_codex(path, max_prompts)
            if parsed is None:
                continue
            _cache_put(path, parsed)
        if not parsed.prompts or parsed.session_id in seen:
            continue
        last_activity = parsed.last_event or mtime_dt
        if last_activity < cutoff:
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
            branch=parsed.branch,
            terminal=term_map.get(parsed.session_id) or {},
            next_hint=parsed.next_hint,
        ))
    return sessions


def _parse_codex(path: Path, max_prompts: int) -> _Parsed | None:
    session_id = ""
    project = "unknown"
    prompts: list[Prompt] = []
    pending_task = False
    last_reply = ""
    last_event: datetime | None = None
    branch = ""
    for data in iter_jsonl_dicts(path):
        payload = data.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = _parse_ts(data.get("timestamp"))
        if ts and (last_event is None or ts > last_event):
            last_event = ts
        dtype = data.get("type")
        if dtype == "session_meta":
            session_id = payload.get("id", "") or session_id
            cwd = payload.get("cwd", "")
            if cwd:
                project = project_from_cwd(cwd)
            git = payload.get("git")
            if isinstance(git, dict) and git.get("branch"):
                branch = git["branch"]
        elif dtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message":
                text = (payload.get("message") or "").strip()
                if text and not text.startswith(_CODEX_SKIP_PREFIXES):
                    prompts.append(Prompt(text=text, timestamp=_parse_ts(data.get("timestamp"))))
            elif ptype == "agent_message":
                reply = payload.get("message") or ""
                if reply.strip():
                    last_reply = reply
            elif ptype == "task_started":
                pending_task = True
            elif ptype in ("task_complete", "turn_aborted"):
                pending_task = False
    if not prompts:
        return None
    return _Parsed(session_id or path.stem, project, prompts[-max_prompts:], pending_task,
                   branch=branch, next_hint=_hint_text(last_reply), last_event=last_event)


_HINT_MAX_LINES = 5   # 「下一步」显示上限（AskUserQuestion 格式化路径用；打分路径上限 3）
_HINT_PICK_LINES = 3  # 句子打分路径最多保留 N 个正分句

# 句子打分词表（确定性规则，随用随补）：命中行动/征询词加分，纯完成陈述减分
_ACTION_WORDS = ("要我", "需要我", "是否", "说一声", "确认", "建议", "重启", "验证", "跑一下",
                 "你来定", "等你", "接下来", "下一步", "要不要", "可以选", "告诉我", "试试",
                 "生效", "开工", "动手", "选一个", "定一个", "待你", "看看")
_DONE_WORDS = ("已提交", "已完成", "完成了", "全绿", "修好", "已合并", "已更新", "已修复", "通过", "落地")
_OPTION_RE = re.compile(r"^(\d+[.、)]|[-•·]|[A-Da-d][.、)])\s*")
_SENT_SPLIT_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")


def _clean_reply_lines(reply: str, line_limit: int = 160) -> list[str]:
    """回复正文降噪：剔代码块与围栏、分隔线、表格行、空行，剥标题井号与粗体星号，每行限长。"""
    kept: list[str] = []
    in_code = False
    for raw in reply.splitlines():
        ln = raw.strip()
        if ln.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or not ln:
            continue
        if ln.startswith("|") or re.fullmatch(r"[-*_=~]{3,}", ln):
            continue
        ln = re.sub(r"^#{1,6}\s+", "", ln).replace("**", "")
        kept.append(ln[:line_limit])
    return kept


def _score(sent: str) -> int:
    """「下一步」信号分：问句 +3、行动/征询词 +2、纯完成陈述 -2。纯规则，无模型。"""
    s = 0
    body = sent.rstrip("。；;.")
    if body.endswith(("？", "?", "吗", "么", "呢")):
        s += 3
    if any(w in sent for w in _ACTION_WORDS):
        s += 2
    if any(w in sent for w in _DONE_WORDS) and "？" not in sent and "?" not in sent:
        s -= 2
    return s


def _hint_text(reply: str) -> str:
    """句子打分精简：切句后取正分句（问句/建议/选项行）按原顺序保留末尾至多
    _HINT_PICK_LINES 个；全无信号（纯汇报）回退最后一个有效行。"""
    cleaned = _clean_reply_lines(reply)
    if not cleaned:
        return ""
    picked: list[str] = []
    for ln in cleaned:
        if _OPTION_RE.match(ln):  # 选项/列表行保持原样不切句；完成陈述类列表被减分排除
            if _score(ln) >= 0:
                picked.append(ln)
            continue
        for match in _SENT_SPLIT_RE.findall(ln):
            sent = match.strip()
            if sent and _score(sent) > 0:
                picked.append(sent)
    if picked:
        return "\n".join(picked[-_HINT_PICK_LINES:])
    return cleaned[-1]


def _format_question(tool_input: dict) -> str:
    """AskUserQuestion 的结构化提问 → 「下一步」文本：问题一行 + 选项一行（· A / B / C）。"""
    lines: list[str] = []
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return ""
    for q in questions[:2]:
        if not isinstance(q, dict):
            continue
        text = (q.get("question") or "").strip()
        if text:
            lines.append(text[:160])
        opts = " / ".join((o.get("label") or "").strip()
                          for o in (q.get("options") or []) if isinstance(o, dict))
        if opts:
            lines.append(("· " + opts)[:160])
    return "\n".join(lines[:_HINT_MAX_LINES])


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
