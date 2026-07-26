"""Kimi Code 数据适配器。

本地 token：`~/.kimi-code/sessions/wd_*/session_*/agents/<agent>/wire.jsonl` 里的
`{"type":"usage.record","model":"kimi-code/k3","usage":{...},"usageScope":"turn","time":<epoch ms>}`。

usage.record 汇总口径（本机实测结论，2026-07）：**增量**，不是累计快照——同一文件相邻记录的
inputOther 会变小（如 6276→3072→488），若把每条当快照取最后一条会严重少算；每条是一次
turn 的独立计量，全部求和。usageScope 实测只有 "turn" 一种，无 session 级记录可交叉验证。

额度 API：`~/.kimi-code/credentials/kimi-code.json`（access_token/refresh_token/expires_at）→
GET {api_base}/coding/v1/usages（Bearer）。响应数字全是字符串；5 小时窗口在 limits[] 里按
window.duration==300 + timeUnit=="TIME_UNIT_MINUTE" 定位（与 kimi-quota-tray 同口径），
周额度在顶层 usage；Extra 余额在 boosterWallet.balance.amountLeft（1e-8 元整数）。
token 过期则 POST {oauth_host}/api/oauth/token（404 退回 /v1/oauth/token）刷新并原子写回，
与 CLI / kimi-quota-tray 行为一致；任何网络/凭证异常都返回 None 降级，绝不抛出影响其他 agent。
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .types import AgentInfo, RateLimits, UsageEntry, normalize_pct
from .util import file_may_have_events_since, iter_jsonl_dicts, kimi_home, project_from_cwd

KIMI_DIR = kimi_home()
SESSIONS_DIR = os.path.join(KIMI_DIR, "sessions")
CREDENTIALS_PATH = os.path.join(KIMI_DIR, "credentials", "kimi-code.json")
WORKSPACES_PATH = os.path.join(KIMI_DIR, "workspaces.json")

# CLI 公开二进制中的 OAuth 公共 client_id（公共客户端无法保密，社区工具通行做法，同 kimi-quota-tray）
_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
# 刷新端点：main 分支源码是 /api/oauth/token，本机 CLI v0.27.0 是 /v1/oauth/token；前者优先，404 换后者
_OAUTH_TOKEN_PATHS = ("/api/oauth/token", "/v1/oauth/token")
_HTTP_TIMEOUT = 10
_EXPIRY_SKEW_SECONDS = 60  # 提前 60 秒视为过期，避免请求路上过期（同 kimi-quota-tray）


def _oauth_host() -> str:
    env = os.environ.get("KIMI_CODE_OAUTH_HOST") or os.environ.get("KIMI_OAUTH_HOST") or ""
    return env.strip().rstrip("/") or "https://auth.kimi.com"


def _api_base() -> str:
    env = os.environ.get("KIMI_CODE_BASE_URL") or ""
    return env.strip().rstrip("/") or "https://api.kimi.com"


def detect() -> AgentInfo | None:
    # 以 ~/.kimi-code 目录判断是否安装（与 codex.detect 同口径）
    if Path(KIMI_DIR).is_dir():
        return AgentInfo(id="kimi-code", name="Kimi Code")
    return None


# --- 本地 token（wire.jsonl） ---


def _iter_wire_files(sessions_dir: str):
    """yield (session_dir_name, agent_name, wire_path)；目录在扫描中被删则静默跳过。"""
    for wire in sorted(Path(sessions_dir).glob("wd_*/session_*/agents/*/wire.jsonl")):
        try:
            agent_name = wire.parent.name
            session_name = wire.parent.parent.parent.name
        except (IndexError, ValueError):
            continue
        yield session_name, agent_name, wire


def _load_workspaces() -> dict:
    """workspaces.json：wd 目录名 → {"root": ...}。缺失/损坏返回空 dict。"""
    try:
        with open(WORKSPACES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        ws = data.get("workspaces")
        return ws if isinstance(ws, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _project_for(wd_name: str, workspaces: dict) -> str:
    """项目名：优先 workspaces.json 的 root 走 project_from_cwd（能命中 git 仓库根）；
    映射缺失回退 session 目录名 `wd_<目录名>_<hash>` 的中段。"""
    info = workspaces.get(wd_name)
    root = info.get("root") if isinstance(info, dict) else None
    if isinstance(root, str) and root:
        return project_from_cwd(root)
    parts = wd_name.split("_")
    return parts[1] if len(parts) >= 3 and parts[1] else wd_name


def _read_state(session_dir: Path) -> dict:
    try:
        with open(session_dir / "state.json", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_entries(hours_back: int = 0) -> list[UsageEntry]:
    entries: list[UsageEntry] = []
    seen: set[str] = set()
    cutoff = None
    if hours_back > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    sessions_path = Path(SESSIONS_DIR)
    if not sessions_path.is_dir():
        return entries

    workspaces = _load_workspaces()
    for session_name, agent_name, wire_path in _iter_wire_files(SESSIONS_DIR):
        if not file_may_have_events_since(wire_path, cutoff):
            continue
        entry = _parse_wire(session_name, agent_name, wire_path, workspaces, cutoff)
        if entry is None or entry.dedup_key in seen:
            continue
        seen.add(entry.dedup_key)
        entries.append(entry)

    entries.sort(key=lambda e: e.timestamp)
    return entries


def _parse_wire(
    session_name: str,
    agent_name: str,
    wire_path: Path,
    workspaces: dict,
    cutoff: datetime | None,
) -> UsageEntry | None:
    """一个 agent 的 wire.jsonl 聚合成一条 UsageEntry（subagent 各自单独一条，全部计入）。"""
    state = _read_state(wire_path.parent.parent.parent)
    wd_name = wire_path.parent.parent.parent.parent.name
    project = _project_for(wd_name, workspaces)

    input_tokens = output_tokens = cache_read = cache_creation = msg_count = 0
    model = "unknown"
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for data in iter_jsonl_dicts(wire_path):
        if data.get("type") != "usage.record":
            continue
        usage = data.get("usage")
        if not isinstance(usage, dict):
            continue
        time_ms = data.get("time")
        ts = datetime.fromtimestamp(time_ms / 1000, tz=UTC) if isinstance(time_ms, int | float) else None
        if cutoff and ts and ts < cutoff:
            continue
        input_tokens += int(usage.get("inputOther") or 0)
        output_tokens += int(usage.get("output") or 0)
        cache_read += int(usage.get("inputCacheRead") or 0)
        cache_creation += int(usage.get("inputCacheCreation") or 0)
        msg_count += 1
        raw_model = data.get("model")
        if isinstance(raw_model, str) and raw_model:
            model = _strip_model_prefix(raw_model)
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    if msg_count == 0 or (input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_creation == 0):
        return None

    # 会话开始时间优先 state.json createdAt（比第一条 usage 更早、更接近真实开始）
    start_ts = _parse_iso(state.get("createdAt")) or first_ts
    if start_ts is None:
        return None
    if cutoff and last_ts and last_ts < cutoff:
        return None

    # 同一 session 的 main/sub agent 各一条 entry：session_id 带 agent 后缀保证 dedup_key 唯一
    session_id = f"{session_name}:{agent_name}"
    return UsageEntry(
        timestamp=start_ts,
        session_id=session_id,
        message_id=session_id,
        request_id="",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost_usd=None,
        project=project,
        agent_id="kimi-code",
        message_count=msg_count,
        session_end=last_ts,
    )


def _strip_model_prefix(model: str) -> str:
    """wire 里的 model 带路由前缀（kimi-code/k3）→ 去掉前缀，报表按真实模型名归行。"""
    return model.split("/", 1)[1] if "/" in model else model


# --- 额度 API ---


def load_rate_limits() -> RateLimits | None:
    """读凭证（过期先刷新写回）→ GET /coding/v1/usages → 映射 RateLimits。
    纯查询不耗模型额度；任何一步失败都返回 None（降级不显示，绝不抛出影响其他 agent）。"""
    try:
        return _load_rate_limits_inner()
    except Exception:  # noqa: BLE001 —— 刻意兜底：网络/凭证/字段漂移一律降级，不影响其他 agent
        return None


def _load_rate_limits_inner() -> RateLimits | None:
    cred = _read_credentials()
    if cred is None:
        return None
    if _token_expired(cred):
        cred = _refresh_token(cred)
        if cred is None:
            return None
    data = _get_usages(cred["access_token"])
    if not isinstance(data, dict):
        return None
    return _map_rate_limits(data)


def _read_credentials() -> dict | None:
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            cred = json.load(f)
        if isinstance(cred, dict) and cred.get("access_token"):
            return cred
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _token_expired(cred: dict) -> bool:
    try:
        return float(cred.get("expires_at") or 0) <= datetime.now(UTC).timestamp() + _EXPIRY_SKEW_SECONDS
    except (TypeError, ValueError):
        return True  # expires_at 缺失/非法按过期处理，先刷一次


def _refresh_token(cred: dict) -> dict | None:
    refresh = cred.get("refresh_token")
    if not refresh:
        return None
    body = urllib.parse.urlencode({
        "client_id": _OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode()
    for path in _OAUTH_TOKEN_PATHS:
        req = urllib.request.Request(
            _oauth_host() + path,
            data=body,
            headers={"Accept": "application/json", "User-Agent": "token-tracker/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                tr = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # 此路径不存在，试下一个
            return None  # 401/403/invalid_grant/5xx 等：降级，不重试
        if not isinstance(tr, dict) or not tr.get("access_token"):
            return None
        # 更新内存凭证（保留未建模字段），refresh_token 会轮换、必须写回
        cred["access_token"] = tr["access_token"]
        if tr.get("refresh_token"):
            cred["refresh_token"] = tr["refresh_token"]
        if tr.get("expires_in"):
            try:
                cred["expires_in"] = int(tr["expires_in"])
                cred["expires_at"] = int(datetime.now(UTC).timestamp()) + int(tr["expires_in"])
            except (TypeError, ValueError):
                pass
        for key in ("token_type", "scope"):
            if tr.get(key):
                cred[key] = tr[key]
        _write_credentials_atomic(cred)
        return cred
    return None


def _write_credentials_atomic(cred: dict) -> None:
    """原子写回凭证（临时文件 + rename，与 CLI 一致）；写失败不影响本轮请求（内存凭证仍有效）。"""
    temp = f"{CREDENTIALS_PATH}.tmp-{os.getpid()}"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(cred, f, indent=2, ensure_ascii=False)
        os.replace(temp, CREDENTIALS_PATH)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass


def _get_usages(access_token: str) -> dict | None:
    req = urllib.request.Request(
        _api_base() + "/coding/v1/usages",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "token-tracker/0.1"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _to_int(value: object) -> int | None:
    """响应数字全是字符串（64 位），也可能直接是数字；两种都接。"""
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _quota_used_pct(detail: object) -> tuple[float | None, int | None]:
    """QuotaDetail → (已用 %, resetTime epoch 秒)。limit<=0 / 字段缺失 → (None, None)。"""
    if not isinstance(detail, dict):
        return None, None
    limit = _to_int(detail.get("limit"))
    used = _to_int(detail.get("used"))
    pct = None
    if limit and used is not None:
        pct = max(0.0, min(100.0, used * 100.0 / limit))
    resets = None
    reset_time = detail.get("resetTime")
    if isinstance(reset_time, str):
        dt = _parse_iso(reset_time)
        if dt:
            resets = int(dt.timestamp())
    return pct, resets


def _map_rate_limits(data: dict) -> RateLimits | None:
    now_ts = datetime.now(UTC).timestamp()
    five_pct = five_reset = None

    # 5 小时窗口：limits[] 里按 duration=300 + TIME_UNIT_MINUTE 找，不硬编码下标（同 kimi-quota-tray）
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            window = item.get("window")
            if not isinstance(window, dict):
                continue
            if _to_int(window.get("duration")) == 300 and window.get("timeUnit") == "TIME_UNIT_MINUTE":
                five_pct, five_reset = _quota_used_pct(item.get("detail"))
                break

    # 周额度在顶层 usage
    seven_pct, seven_reset = _quota_used_pct(data.get("usage"))

    five_pct = normalize_pct(five_pct, five_reset, now_ts)
    seven_pct = normalize_pct(seven_pct, seven_reset, now_ts)
    if five_pct is None and seven_pct is None:
        return None

    # plan_type 拼会员等级 + Extra 余额（amountLeft 是 1e-8 元整数，同 kimi-quota-tray History 采样口径）
    plan_parts: list[str] = []
    user = data.get("user")
    if isinstance(user, dict):
        membership = user.get("membership")
        if isinstance(membership, dict) and membership.get("level"):
            plan_parts.append(str(membership["level"]))
    extra = extra_balance_yuan(data)
    if extra is not None:
        plan_parts.append(f"Extra ¥{extra:.2f}")

    return RateLimits(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_reset,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_reset,
        plan_type=" · ".join(plan_parts),
    )


def extra_balance_yuan(data: dict) -> float | None:
    wallet = data.get("boosterWallet")
    if not isinstance(wallet, dict):
        return None
    balance = wallet.get("balance")
    if not isinstance(balance, dict):
        return None
    raw = _to_int(balance.get("amountLeft"))
    return raw / 1e8 if raw is not None else None
