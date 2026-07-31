import http.client
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import URLError

from ..adapters.types import UsageEntry

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
# 缓存放用户可写目录：包根目录在 site-packages 下是只读的，写失败会导致每次都联网
CACHE_DIR = Path(os.path.expanduser("~/.cache/token-tracker"))
CACHE_PATH = CACHE_DIR / "pricing_cache.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 定价表 7 天过期，过期后尝试刷新（失败仍用旧缓存兜底）
_INSECURE_TLS_ENV = "TT_PRICING_INSECURE_TLS"

_pricing: dict | None = None
_warned_insecure = False

# 未知的新模型按系列退回最新已知价（这些 key 由 _fallback_pricing 保证存在）。
# codex- 兜底覆盖 Codex 内部虚拟 model（如 codex-auto-review，stop-time auto-review gate 用）
_FAMILY_FALLBACK = (
    ("claude-opus", "claude-opus-4-8"),
    ("claude-sonnet", "claude-sonnet-5"),
    ("claude-haiku", "claude-haiku-4-5-20251001"),
    ("claude-fable", "claude-fable-5"),
    # GPT-5.6 三档并列（sol/terra/luna 各自有内置价，dated/variant 靠前缀命中）；系列内
    # 未知新档（如假想 gpt-5.6-nova）退回旗舰 sol——新档价未知时退回最贵档，宁可高估不低估
    ("gpt-5.6", "gpt-5.6-sol"),
    ("codex-", "gpt-5.6-sol"),
    # 国产模型系列兜底：出新版本（如 GLM-4.8、Kimi K3）litellm 未收录时退回该系列最新已知价
    ("kimi", "kimi-k2.6"),
    ("moonshot-v", "moonshot-v1-128k"),
    ("glm-4", "glm-4.6"),
    ("qwen3-coder", "qwen3-coder-plus"),
    ("qwen3-max", "qwen-max"),
    ("doubao-seed", "doubao-seed-1-6"),
    ("doubao-1-5-pro", "doubao-1-5-pro-256k"),
    ("deepseek", "deepseek-v4-flash"),
    ("minimax", "MiniMax-M2"),
    ("mimo", "mimo-v2.5"),
    # Grok：退役 slug 按官方路由兜底（grok-code-* → build-0.1；grok-4-fast/4.1-fast/grok-3 等 → grok-4.3）
    ("grok-code", "grok-build-0.1"),
    ("grok-4", "grok-4.3"),
    ("grok", "grok-4.3"),
)

# 解析不到定价的模型只提示一次，避免聚合时每条 entry 刷屏
_warned_unknown_models: set[str] = set()

# model → 解析出的定价 key。非精确命中要线性扫全表（litellm 数千 key），逐 entry 调用必须记忆化。
# 命中后还校验 key 仍在当前 pricing 里（测试会整表替换 _pricing），失效则重算。
_model_key_cache: dict[str, str | None] = {}


def get_pricing() -> dict:
    global _pricing
    if _pricing is not None:
        return _pricing
    # 以 litellm 数据为准，_fallback_pricing 作为已知价底座补 litellm 尚未收录的新模型（如最新 Opus）
    _pricing = {**_fallback_pricing(), **_load_pricing()}
    return _pricing


def calculate_cost(entry: UsageEntry) -> float:
    if entry.cost_usd is not None:
        return entry.cost_usd

    pricing = get_pricing()
    model_key = _resolve_model_key(entry.model, pricing)
    if model_key is None:
        _warn_unknown_model_once(entry.model)
        return 0.0

    info = pricing[model_key]
    input_cost = info.get("input_cost_per_token", 0)
    output_cost = info.get("output_cost_per_token", 0)
    cache_creation_cost = info.get("cache_creation_input_token_cost", input_cost * 1.25)
    cache_read_cost = info.get("cache_read_input_token_cost", input_cost * 0.1)

    return (
        entry.input_tokens * input_cost
        + entry.output_tokens * output_cost
        + entry.cache_creation_tokens * cache_creation_cost
        + entry.cache_read_tokens * cache_read_cost
    )


def _resolve_model_key(model: str, pricing: dict) -> str | None:
    if not model:
        return None
    if model in _model_key_cache:
        cached = _model_key_cache[model]
        if cached is None or cached in pricing:
            return cached
    key = _resolve_model_key_uncached(model, pricing)
    _model_key_cache[model] = key
    return key


def _resolve_model_key_uncached(model: str, pricing: dict) -> str | None:
    if model in pricing:
        return model

    ml = model.lower()
    # model 以 key + "-" 为前缀：处理 dated/variant 后缀（gpt-5-codex-2025-12-01 → gpt-5-codex）
    # "-" 锚点避免 gpt-5 误吞 gpt-5.6-* 这类点号新版本；取最长匹配，避免 gpt-5 误吞 gpt-5-codex-*
    prefix_keys = [
        k for k in pricing
        if (kl := k.lower()) != ml and ml.startswith(kl) and ml[len(kl)] == "-"
    ]
    if prefix_keys:
        return max(prefix_keys, key=len)
    # 反向兜底：key 以 model + "-" 开头（gpt-5 命中 gpt-5-2025-08-07）
    # 加 "-" 锚点避免 gpt-5 撞上 gpt-5-mini
    suffix_keys = [k for k in pricing if k.lower().startswith(ml + "-")]
    if suffix_keys:
        return min(suffix_keys, key=len)

    # 同系列兜底：未知的新 Claude 模型（litellm 收录滞后）退回同系列最新已知价，避免成本静默归零
    for prefix, fallback_key in _FAMILY_FALLBACK:
        if ml.startswith(prefix) and fallback_key in pricing:
            return fallback_key
    return None


def _warn_unknown_model_once(model: str) -> None:
    # 全新系列（非 opus/sonnet/haiku/fable）连系列兜底都接不住，成本会按 $0 计；显形以免静默少算
    if model and model not in _warned_unknown_models:
        _warned_unknown_models.add(model)
        print(
            f"token-tracker: 未知模型 {model!r} 缺少定价，本次成本按 $0 计；"
            "litellm 收录后自动恢复，或在 cost.py 的 _fallback_pricing 补内置价",
            file=sys.stderr,
        )


def _load_pricing() -> dict:
    cached = _read_cache()
    if cached is not None and not _cache_stale():
        return cached

    # 缓存缺失或过期 → 尝试联网刷新；失败时优先用旧缓存（哪怕过期），最后才用内置兜底。
    # 异常集要覆盖整条抓取链：URLError/TimeoutError/ssl.SSLError/OSError（网络与 socket），
    # http.client.HTTPException（IncompleteRead 等截断响应），ValueError（JSON 解析 + decode 失败）。
    # 仍不用裸 except，避免吞掉 AttributeError/KeyError 这类真 bug。
    try:
        return _fetch_and_cache()
    except (URLError, TimeoutError, ssl.SSLError, OSError, http.client.HTTPException, ValueError):
        if cached is not None:
            return cached
        return _fallback_pricing()


def _read_cache() -> dict | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _cache_stale() -> bool:
    try:
        age = time.time() - CACHE_PATH.stat().st_mtime
    except OSError:
        return True
    return age > CACHE_TTL_SECONDS


def _fetch_and_cache() -> dict:
    try:
        data = _fetch(verify=True)
    except ssl.SSLCertVerificationError:
        # 默认不静默降级 TLS：仅当用户显式 TT_PRICING_INSECURE_TLS=1 时才放行（抓的是公开定价表）
        if os.environ.get(_INSECURE_TLS_ENV) != "1":
            raise
        _warn_insecure_once()
        data = _fetch(verify=False)

    _write_cache(data)
    return data


def _fetch(verify: bool) -> dict:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "token-tracker/0.1"})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        return json.loads(resp.read().decode())


def _write_cache(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _warn_insecure_once() -> None:
    global _warned_insecure
    if not _warned_insecure:
        print(
            f"token-tracker: 已按 {_INSECURE_TLS_ENV}=1 关闭 TLS 证书校验（仅用于抓取公开定价表）",
            file=sys.stderr,
        )
        _warned_insecure = True


# Anthropic 官方价（claude.com/pricing 2026-06 核对）。opus 4.5/4.6/4.7/4.8 同价。
_OPUS_PRICING = {
    "input_cost_per_token": 5e-6,
    "output_cost_per_token": 25e-6,
    "cache_creation_input_token_cost": 6.25e-6,
    "cache_read_input_token_cost": 0.5e-6,
}

# Fable 5 / Mythos 5 同价，是 Opus 档的 2 倍（$10/$50 每百万 token）
_FABLE_PRICING = {
    "input_cost_per_token": 10e-6,
    "output_cost_per_token": 50e-6,
    "cache_creation_input_token_cost": 12.5e-6,
    "cache_read_input_token_cost": 1.0e-6,
}

# 国产模型多以人民币计价，统一折 USD 入表，与 CC/Codex 同口径（2026-06 近似汇率）
_CNY_PER_USD = 7.1


def _cny(input_m: float, output_m: float, cache_read_m: float | None = None) -> dict:
    """人民币「元 / 百万 tokens」→ USD per token（÷汇率 ÷1e6）。国产模型按中国站人民币价折算。"""
    info = {
        "input_cost_per_token": input_m / _CNY_PER_USD * 1e-6,
        "output_cost_per_token": output_m / _CNY_PER_USD * 1e-6,
    }
    if cache_read_m is not None:
        info["cache_read_input_token_cost"] = cache_read_m / _CNY_PER_USD * 1e-6
    return info


def _usd(input_m: float, output_m: float, cache_read_m: float | None = None) -> dict:
    """美元「$ / 百万 tokens」→ USD per token（÷1e6）。用于只有官方国际站 USD 价的模型。"""
    info = {
        "input_cost_per_token": input_m * 1e-6,
        "output_cost_per_token": output_m * 1e-6,
    }
    if cache_read_m is not None:
        info["cache_read_input_token_cost"] = cache_read_m * 1e-6
    return info


def _fallback_pricing() -> dict:
    return {
        "claude-fable-5": _FABLE_PRICING,
        "claude-opus-4-8": _OPUS_PRICING,
        "claude-opus-4-7": _OPUS_PRICING,
        "claude-opus-4-6": _OPUS_PRICING,
        "claude-opus-4-5": _OPUS_PRICING,
        # Sonnet 5 当前为导入价（$2 / $10 / $2.50 cache-write / $0.20 cache-read），截止 2026-08-31；
        # 9-1 起标准价 $3 / $15（与 Sonnet 4.6 一致）。到期后需切价（注意：litellm 若已收录、以在线价为准）。
        "claude-sonnet-5": {
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 10e-6,
            "cache_creation_input_token_cost": 2.5e-6,
            "cache_read_input_token_cost": 0.2e-6,
        },
        "claude-sonnet-4-6": {
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 15e-6,
            "cache_creation_input_token_cost": 3.75e-6,
            "cache_read_input_token_cost": 0.3e-6,
        },
        "claude-haiku-4-5-20251001": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 5e-6,
            "cache_creation_input_token_cost": 1.25e-6,
            "cache_read_input_token_cost": 0.1e-6,
        },
        "gpt-5": {
            "input_cost_per_token": 1.25e-6,
            "output_cost_per_token": 10e-6,
            "cache_read_input_token_cost": 0.125e-6,
        },
        "gpt-5.5": {
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 30e-6,
            "cache_read_input_token_cost": 0.5e-6,
        },
        # GPT-5.6 系列（2026-07-09 GA，litellm 收录前必须有专属内置价——gpt-5.6-* 会被
        # gpt-5 前缀吞掉错价）。取短上下文基础档；7-30 官方调价后：Terra 降 20%、Luna 降 80%。
        #  cached input 均为 input 的 10%；长上下文表（Sol $10/$45 等）本表不跟踪。
        "gpt-5.6-sol": {
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 30e-6,
            "cache_read_input_token_cost": 0.5e-6,
        },
        "gpt-5.6-terra": {
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 12e-6,
            "cache_read_input_token_cost": 0.2e-6,
        },
        "gpt-5.6-luna": {
            "input_cost_per_token": 0.2e-6,
            "output_cost_per_token": 1.2e-6,
            "cache_read_input_token_cost": 0.02e-6,
        },
        "gpt-5-codex": {
            "input_cost_per_token": 1.25e-6,
            "output_cost_per_token": 10e-6,
            "cache_read_input_token_cost": 0.125e-6,
        },
        "gpt-5-mini": {
            "input_cost_per_token": 0.25e-6,
            "output_cost_per_token": 2e-6,
            "cache_read_input_token_cost": 0.025e-6,
        },
        "gpt-5-nano": {
            "input_cost_per_token": 0.05e-6,
            "output_cost_per_token": 0.4e-6,
            "cache_read_input_token_cost": 0.005e-6,
        },
        "gpt-5-pro": {
            "input_cost_per_token": 15e-6,
            "output_cost_per_token": 120e-6,
        },
        "codex-mini-latest": {
            "input_cost_per_token": 1.5e-6,
            "output_cost_per_token": 6e-6,
            "cache_read_input_token_cost": 0.375e-6,
        },
        # ---- 国产模型（2026-06 官方核实）。除 GLM 用 z.ai 国际站 USD 外，其余按各家中国站
        # 人民币价 ÷7.1 折算；阶梯定价模型（Qwen3-Coder / Doubao）统一取 0-32K 基础档。----
        # Kimi / Moonshot（platform.kimi.com 官方人民币价；老 kimi-k2-instruct 已 EOL，靠系列兜底）
        "kimi-k2.7-code": _cny(6.5, 27, 1.3),
        "kimi-k2.6": _cny(6.5, 27, 1.1),
        "kimi-k2.5": _cny(4, 21, 0.7),
        "moonshot-v1-8k": _cny(2, 10),
        "moonshot-v1-32k": _cny(5, 20),
        "moonshot-v1-128k": _cny(10, 30),
        # 智谱 GLM（z.ai 国际站官方 USD；中国站按量完整价含缓存无法从官方 SPA 取得，国内口径可能偏高）
        "glm-4.6": _usd(0.6, 2.2, 0.11),
        "glm-4.5": _usd(0.6, 2.2, 0.11),
        "glm-4.5-air": _usd(0.2, 1.1, 0.03),
        "glm-4.7": _usd(0.6, 2.2, 0.11),
        "glm-5": _usd(1.0, 3.2, 0.2),
        # 阿里 Qwen（中国站百炼人民币价，0-32K 基础档）
        "qwen3-coder-plus": _cny(4, 16, 0.4),
        "qwen-max": _cny(2.5, 10),
        "qwen-plus": _cny(0.8, 2),
        # 火山方舟 Doubao（中国站人民币价，0-32K 基础档）
        "doubao-seed-1-6": _cny(0.8, 8),
        "doubao-seed-code": _cny(1.2, 8),
        "doubao-1-5-pro-32k": _cny(0.8, 2, 0.16),
        "doubao-1-5-pro-256k": _cny(5, 9),
        # DeepSeek（官方中国站人民币价；chat/reasoner 现映射 V4-Flash，2026-07-24 弃用旧名）
        "deepseek-v4-flash": _cny(1, 2, 0.02),
        "deepseek-v4-pro": _cny(3, 6, 0.025),
        "deepseek-chat": _cny(1, 2, 0.02),
        "deepseek-reasoner": _cny(1, 2, 0.02),
        # MiniMax（官方 USD，与中国站÷7 自洽；M2/M2.1/M2.5 同价 legacy）
        "MiniMax-M2": _usd(0.3, 1.2, 0.03),
        "MiniMax-M2.1": _usd(0.3, 1.2, 0.03),
        "MiniMax-M2.5": _usd(0.3, 1.2, 0.03),
        "MiniMax-M2.7": _usd(0.3, 1.2, 0.06),
        "MiniMax-M3": _usd(0.3, 1.2, 0.06),
        # 小米 MiMo（mimo.mi.com 官方中国站人民币价；与 DeepSeek 同价，V2.5-Pro 主攻 agentic 编程）
        "mimo-v2.5-pro": _cny(3, 6, 0.025),
        "mimo-v2.5": _cny(1, 2, 0.02),
        # xAI Grok（docs.x.ai 官方 USD）。2026-05-15 退役潮：grok-4-fast/4.1-fast/grok-3 路由到 grok-4.3，
        # grok-code-fast-1 退役为 grok-build-0.1 别名（退役 slug 靠 _FAMILY_FALLBACK 接住）。grok-4.3 取 ≤200K 默认档。
        "grok-4.3": _usd(1.25, 2.5, 0.2),
        "grok-build-0.1": _usd(1.0, 2.0, 0.2),
        "grok-code-fast-1": _usd(1.0, 2.0, 0.2),
    }
