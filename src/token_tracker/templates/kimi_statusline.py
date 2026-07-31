#!/usr/bin/env python3
"""token-tracker Kimi Code statusline（tui.toml [status_line].command）：渲染一行会话状态。
[项目](分支) │ Model: <模型> │ Ctx <bar> <%> │ ⬆<会话累计 token> $<累计成本>
数据：model/cwd/gitBranch/contextTokens/maxContextTokens/sessionId 取 stdin JSON 快照；
token/成本增量解析本会话 wire.jsonl（state 文件缓存 offset，避免每帧全量扫，300ms 上限内零网络）；
终端映射写 tt-terminal-map.json（与 Codex 同文件同 schema），供 tt sidebar 点击跳转。
被 Kimi 以 1s 节流反复调用：任何解析失败都 fail-open 输出一行，绝不 traceback 到 stdout。
由 `tt setup` 生成，勿手改。"""
__version__ = "__KIMI_STATUSLINE_HOOK_VERSION__"
import glob
import json
import os
import sys
import tempfile

STATE_FILE = os.path.join(os.path.expanduser("~/.config/token-tracker"), "tt-kimi-statusline.json")
TERMINAL_MAP_FILE = os.path.join(os.path.expanduser("~/.config/token-tracker"), "tt-terminal-map.json")
MAX_SESSIONS = 20
MAX_TERMINAL_MAPPINGS = 20

# 配色由 tt setup / update_hook / tt theme set 烘焙时注入（跟随当前主题，与 CC/Codex statusline 同源）。
# Kimi TUI 支持 24-bit truecolor，只注入 truecolor 一套（同 Codex 伪 statusline）。
C = __STATUSLINE_TRUECOLOR__
RST = C["reset"]
BOLD = "\033[1m"

# 定价由烘焙时从 analyzer.cost._fallback_pricing() 注入（kimi-k3 / kimi-k2.7-code / kimi-k2.6
# 三档 dict 原样 repr）。状态栏不联网、查不到价的模型按 $0 计（不 warn，避免污染状态栏）。
P = __KIMI_PRICING__


def _kimi_home():
    """Kimi Code 配置/数据根目录：KIMI_CODE_HOME 优先，否则 ~/.kimi-code（内联实现，零依赖）。"""
    env = os.environ.get("KIMI_CODE_HOME", "").strip()
    return env if env else os.path.expanduser("~/.kimi-code")


def _color(pct):
    return C["bar_ok"] if pct < 50 else C["bar_warn"] if pct < 80 else C["bar_danger"]


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _bar(pct, width=8):
    """进度条（同 CC/Codex statusline）：█ 填充档位色 + ░ 空槽（>0 也染档位色），尾接 % 档位色。"""
    pct = max(0.0, min(100.0, float(pct)))
    filled = round(pct / 100 * width)
    empty = width - filled
    color = _color(pct)
    empty_s = f"{color}{'░' * empty}{RST}" if pct > 0 and empty else "░" * empty
    return f"{color}{'█' * filled}{RST}{empty_s} {color}{pct:.0f}%{RST}"


def _pricing_for(model):
    """wire 的模型 id 路由到注入的三档定价：kimi-code/k3 → kimi-k3；
    kimi-code/kimi-for-coding* → kimi-k2.7-code；其余 kimi* → kimi-k2.6；查不到 → None（按 $0 计）。"""
    m = (model or "").lower()
    if m in P:
        return P[m]
    if m.startswith("kimi-code/k3"):
        return P.get("kimi-k3")
    if m.startswith("kimi-code/kimi-for-coding"):
        return P.get("kimi-k2.7-code")
    if m.startswith("kimi"):
        return P.get("kimi-k2.6")
    return None


def _cost(model, i, o, cr, cc):
    """input*input + output*output + cache_read*cache_read + cache_creation*(cache_creation 或 input*1.25)。"""
    info = _pricing_for(model)
    if not info:
        return 0.0
    ic = info.get("input_cost_per_token", 0) or 0
    oc = info.get("output_cost_per_token", 0) or 0
    crc = info.get("cache_read_input_token_cost", 0) or 0
    ccc = info.get("cache_creation_input_token_cost") or ic * 1.25
    return i * ic + o * oc + cr * crc + cc * ccc


def _update_usage(session_id):
    """增量解析本会话 wire.jsonl 的 usage.record，返回 (会话累计总 token, 累计成本 USD)。

    state：{sessionId: {"wire": path, "offset": int, "models": {model: {"i","o","cr","cc"}}}}，
    flock 串行合并 + 原子替换 + LRU 20（文件操作骨架同 codex statusline 的 _record_terminal_map）。
    state 里的 wire 路径失效时回退 glob <kimi_home>/sessions/*/<sessionId>/agents/main/wire.jsonl；
    offset > 文件大小说明 wire 被截断 → 从头重读、旧累计作废（否则重复计数）。
    """
    if not session_id:
        return 0, 0.0
    tmp = None
    lock = None
    try:
        parent = os.path.dirname(STATE_FILE)
        os.makedirs(parent, exist_ok=True)
        lock = open(STATE_FILE + ".lock", "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        entry = state.get(session_id)
        if not isinstance(entry, dict):
            entry = {}
        wire = entry.get("wire")
        if not (isinstance(wire, str) and os.path.exists(wire)):
            matches = glob.glob(os.path.join(
                _kimi_home(), "sessions", "*", session_id, "agents", "main", "wire.jsonl"))
            wire = matches[0] if matches else None
        models = entry.get("models")
        models = models if isinstance(models, dict) else {}
        offset = entry.get("offset")
        offset = offset if isinstance(offset, int) else 0
        if wire:
            try:
                if offset > os.path.getsize(wire):  # 文件被截断 → 重置
                    offset, models = 0, {}
                with open(wire, encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                for line in chunk.splitlines():
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(data, dict) or data.get("type") != "usage.record":
                        continue
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    model = data.get("model") or ""
                    bucket = models.get(model)
                    if not isinstance(bucket, dict):
                        bucket = {}
                        models[model] = bucket
                    for short, field in (("i", "inputOther"), ("o", "output"),
                                         ("cr", "inputCacheRead"), ("cc", "inputCacheCreation")):
                        val = usage.get(field)
                        if isinstance(val, (int, float)):
                            bucket[short] = bucket.get(short, 0) + int(val)
            except OSError:
                pass
        state.pop(session_id, None)
        state[session_id] = {"wire": wire, "offset": offset, "models": models}
        for key in list(state)[:-MAX_SESSIONS]:
            del state[key]
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
        tmp = None
        total = 0
        cost = 0.0
        for model, bucket in models.items():
            if not isinstance(bucket, dict):
                continue
            i = int(bucket.get("i", 0))
            o = int(bucket.get("o", 0))
            cr = int(bucket.get("cr", 0))
            cc = int(bucket.get("cc", 0))
            total += i + o + cr + cc
            cost += _cost(model, i, o, cr, cc)
        return total, cost
    except OSError:
        return 0, 0.0
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if lock:
            lock.close()


def _record_terminal_map(session_id):
    """记录当前 Kimi 会话所在窗格，供普通 `tt sidebar` 点击项目名跳转。

    与 Codex 共用一个文件一个 schema（sidebar 读取时对 agent 无差别合并）；多实例并发
    read-modify-write 用 flock 串行（Windows 无 iTerm/tmux，缺 fcntl 时仍保留原子替换兜底）。
    """
    term = {}
    if os.environ.get("ITERM_SESSION_ID"):
        term["iterm"] = os.environ["ITERM_SESSION_ID"]
    if os.environ.get("TMUX_PANE"):
        term["tmux"] = os.environ["TMUX_PANE"]
    if not session_id or not term:
        return

    tmp = None
    lock = None
    try:
        parent = os.path.dirname(TERMINAL_MAP_FILE)
        os.makedirs(parent, exist_ok=True)
        lock = open(TERMINAL_MAP_FILE + ".lock", "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            with open(TERMINAL_MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        term_map = data.get("_terminal_map") if isinstance(data, dict) else None
        if not isinstance(term_map, dict):
            term_map = {}
        term_map.pop(session_id, None)
        term_map[session_id] = term
        for key in list(term_map)[:-MAX_TERMINAL_MAPPINGS]:
            del term_map[key]
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"_terminal_map": term_map}, f)
        os.replace(tmp, TERMINAL_MAP_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if lock:
            lock.close()


def _ctx_pct(payload):
    """当前 context 占用 % = contextTokens ÷ maxContextTokens（stdin 快照自带，无需解析 wire）。"""
    used = payload.get("contextTokens")
    max_tokens = payload.get("maxContextTokens")
    if isinstance(used, (int, float)) and isinstance(max_tokens, (int, float)) and max_tokens > 0:
        return used / max_tokens * 100
    return None


def _render_project(cwd, branch):
    """[项目](分支)；git 信息直接用 payload 的 gitBranch（不调 git 子进程，300ms 预算内）。"""
    if not cwd:
        return ""
    name = os.path.basename(cwd.rstrip("/")) or cwd
    if not branch:
        return f"{BOLD}{C['project']}[{name}]{RST}"
    return f"{BOLD}{C['project']}[{name}]{RST}({C['branch']}{branch}{RST})"


def _render(payload):
    session_id = payload.get("sessionId")
    session_id = session_id if isinstance(session_id, str) else ""
    _record_terminal_map(session_id)
    total, cost = _update_usage(session_id)

    segments = []
    cwd = payload.get("cwd")
    branch = payload.get("gitBranch")
    proj = _render_project(cwd if isinstance(cwd, str) else "",
                           branch if isinstance(branch, str) else "")
    if proj:
        segments.append(proj)
    model = payload.get("model")
    if isinstance(model, str) and model:
        segments.append(f"{C['model']}Model: {model}{RST}")
    ctx = _ctx_pct(payload)
    if ctx is not None:
        segments.append(f"{C['label']}Ctx {RST}{_bar(ctx)}")
    if total:
        segments.append(f"{C['tokens']}⬆{fmt_tokens(total)} ${cost:.2f}{RST}")
    return " │ ".join(segments)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        line = _render(payload)
    except Exception:
        line = ""
    print(line)  # Kimi 只取 stdout 第一行：单条 print，fail-open 也保证有一行
    sys.stdout.flush()


if __name__ == "__main__":
    main()
