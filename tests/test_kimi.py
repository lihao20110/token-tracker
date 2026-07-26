import json
import os
import time
from pathlib import Path

import pytest

from token_tracker import cli, config, hooks, kimi_watch
from token_tracker.adapters import kimi
from token_tracker.adapters.types import RateLimits, UsageEntry
from token_tracker.analyzer import cost


def _write_session(kimi_dir: Path, wd: str, session: str, agents: dict[str, list[dict]],
                   state: dict | None = None) -> Path:
    """在 tmp kimi home 里造一个 session：state.json + 每个 agent 的 wire.jsonl。"""
    session_dir = kimi_dir / "sessions" / wd / session
    session_dir.mkdir(parents=True)
    with open(session_dir / "state.json", "w", encoding="utf-8") as f:
        json.dump(state or {"createdAt": "2026-07-26T02:00:00.000Z", "title": "t"}, f)
    for agent, events in agents.items():
        agent_dir = session_dir / "agents" / agent
        agent_dir.mkdir(parents=True)
        with open(agent_dir / "wire.jsonl", "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    return session_dir


def _usage_record(input_other=100, output=50, cache_read=200, cache_creation=0,
                  model="kimi-code/k3", time_ms=1785000000000) -> dict:
    return {
        "type": "usage.record",
        "model": model,
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": "turn",
        "time": time_ms,
    }


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    """把 kimi 适配器的目录常量指到 tmp_path。"""
    home = tmp_path / ".kimi-code"
    home.mkdir()
    monkeypatch.setattr(kimi, "KIMI_DIR", str(home))
    monkeypatch.setattr(kimi, "SESSIONS_DIR", str(home / "sessions"))
    monkeypatch.setattr(kimi, "CREDENTIALS_PATH", str(home / "credentials" / "kimi-code.json"))
    monkeypatch.setattr(kimi, "WORKSPACES_PATH", str(home / "workspaces.json"))
    return home


# --- detect ---


def test_detect(kimi_home):
    assert kimi.detect() is not None
    assert kimi.detect().id == "kimi-code"


def test_detect_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(kimi, "KIMI_DIR", str(tmp_path / "nope"))
    assert kimi.detect() is None


# --- load_entries ---


def test_load_entries_fields_and_agent_aggregation(kimi_home):
    _write_session(
        kimi_home, "wd_haoproject_19dfa4623a46", "session_abc",
        {
            "main": [
                _usage_record(input_other=100, output=50, cache_read=200, time_ms=1785000000000),
                _usage_record(input_other=60, output=30, cache_read=100, time_ms=1785000060000),
                {"type": "user.message", "time": 1785000000000},  # 非 usage.record 忽略
            ],
            "agent-0": [
                _usage_record(input_other=10, output=5, cache_read=20, cache_creation=7,
                              model="kimi-code/k3-256k", time_ms=1785000030000),
            ],
        },
    )
    entries = kimi.load_entries()

    # main / sub agent 各聚合成一条，subagent 也计入
    assert len(entries) == 2
    main = next(e for e in entries if e.session_id == "session_abc:main")
    sub = next(e for e in entries if e.session_id == "session_abc:agent-0")

    # 增量口径：同一 agent 的 usage.record 全部求和
    assert main.input_tokens == 160
    assert main.output_tokens == 80
    assert main.cache_read_tokens == 300
    assert main.cache_creation_tokens == 0
    assert main.message_count == 2
    assert main.model == "k3"  # 去 kimi-code/ 前缀
    assert main.agent_id == "kimi-code"
    assert main.project == "haoproject"  # workspaces.json 缺失时回退 wd 目录名中段
    assert main.timestamp.isoformat() == "2026-07-26T02:00:00+00:00"  # state.json createdAt
    assert main.session_end.isoformat() == "2026-07-25T17:21:00+00:00"  # 最后一条记录 time

    assert sub.model == "k3-256k"
    assert sub.cache_creation_tokens == 7
    # dedup_key 唯一（同 session 不同 agent 不互相吞掉）
    assert len({e.dedup_key for e in entries}) == 2


def test_load_entries_project_from_workspaces(kimi_home):
    (kimi_home / "workspaces.json").write_text(json.dumps({
        "version": 1,
        "workspaces": {"wd_token_tracker_aa11bb22cc33": {"root": "C:/haoproject/studyItem/token-tracker"}},
    }), encoding="utf-8")
    _write_session(kimi_home, "wd_token_tracker_aa11bb22cc33", "session_x",
                   {"main": [_usage_record()]})
    entries = kimi.load_entries()
    # root 在真实文件系统里不存在 .git 时，project_from_cwd 回退到最后一段
    assert entries[0].project == "token-tracker"


def test_load_entries_hours_back_filters(kimi_home):
    old_ms = int((time.time() - 48 * 3600) * 1000)
    new_ms = int(time.time() * 1000)
    _write_session(kimi_home, "wd_p_aa11bb22cc33", "session_old",
                   {"main": [_usage_record(time_ms=old_ms)]},
                   state={"createdAt": "2020-01-01T00:00:00.000Z"})
    _write_session(kimi_home, "wd_p_aa11bb22cc33", "session_new",
                   {"main": [_usage_record(time_ms=new_ms)]})
    # mtime 预筛要求文件也是新的：把新文件 mtime 刷成现在（写文件已是现在），旧文件刷旧
    old_wire = kimi_home / "sessions" / "wd_p_aa11bb22cc33" / "session_old" / "agents" / "main" / "wire.jsonl"
    old = time.time() - 48 * 3600
    os.utime(old_wire, (old, old))

    entries = kimi.load_entries(hours_back=24)
    assert [e.session_id for e in entries] == ["session_new:main"]


def test_load_entries_zero_usage_skipped(kimi_home):
    _write_session(kimi_home, "wd_p_aa11bb22cc33", "session_z",
                   {"main": [_usage_record(input_other=0, output=0, cache_read=0)]})
    assert kimi.load_entries() == []


# --- load_rate_limits ---


def _write_credentials(kimi_home: Path, expires_at: int) -> Path:
    cred_path = kimi_home / "credentials" / "kimi-code.json"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(json.dumps({
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": expires_at,
    }), encoding="utf-8")
    return cred_path


def _usages_response() -> dict:
    return {
        "usage": {"limit": "1000", "used": "600", "remaining": "400",
                  "resetTime": "2999-01-02T00:00:00Z"},
        "user": {"userId": "u1", "membership": {"level": "LEVEL_ADVANCED"}},
        "limits": [
            {"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
             "detail": {"limit": "100", "used": "25", "remaining": "75",
                        "resetTime": "2999-01-01T05:00:00Z"}},
        ],
        "boosterWallet": {"balance": {"amount": "500000000", "amountLeft": "1234000000"}},
    }


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_urlopen(monkeypatch, handler):
    monkeypatch.setattr(kimi.urllib.request, "urlopen", handler)


def test_load_rate_limits_bucketing(kimi_home, monkeypatch):
    _write_credentials(kimi_home, expires_at=int(time.time()) + 3600)
    _mock_urlopen(monkeypatch, lambda req, **kw: _FakeResponse(_usages_response()))

    rl = kimi.load_rate_limits()

    assert rl is not None
    assert rl.five_hour_pct == pytest.approx(25.0)
    assert rl.five_hour_resets_at is not None
    assert rl.seven_day_pct == pytest.approx(60.0)
    # plan_type 拼会员等级 + Extra 余额（amountLeft 是 1e-8 元整数 → ¥12.34）
    assert "LEVEL_ADVANCED" in rl.plan_type
    assert "Extra ¥12.34" in rl.plan_type


def test_load_rate_limits_expired_reset_zeroed(kimi_home, monkeypatch):
    _write_credentials(kimi_home, expires_at=int(time.time()) + 3600)
    data = _usages_response()
    data["usage"]["resetTime"] = "2000-01-01T00:00:00Z"  # 已过重置时间 → 归零
    _mock_urlopen(monkeypatch, lambda req, **kw: _FakeResponse(data))

    rl = kimi.load_rate_limits()

    assert rl is not None
    assert rl.seven_day_pct == 0.0
    assert rl.five_hour_pct == pytest.approx(25.0)


def test_load_rate_limits_refreshes_expired_token(kimi_home, monkeypatch):
    cred_path = _write_credentials(kimi_home, expires_at=int(time.time()) - 10)  # 已过期
    calls = []

    def handler(req, **kw):
        calls.append(req.full_url)
        if "oauth/token" in req.full_url:
            return _FakeResponse({"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer at-2"
        return _FakeResponse(_usages_response())

    _mock_urlopen(monkeypatch, handler)

    rl = kimi.load_rate_limits()

    assert rl is not None
    assert any("oauth/token" in url for url in calls)
    # 刷新后凭证原子写回（含轮换的 refresh_token）
    saved = json.loads(cred_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "at-2"
    assert saved["refresh_token"] == "rt-2"
    assert saved["expires_at"] > time.time()


def test_load_rate_limits_degrades_to_none(kimi_home, monkeypatch):
    # 无凭证文件 → None
    assert kimi.load_rate_limits() is None

    # 网络异常 → None（绝不抛出）
    _write_credentials(kimi_home, expires_at=int(time.time()) + 3600)

    def boom(req, **kw):
        raise OSError("network down")

    _mock_urlopen(monkeypatch, boom)
    assert kimi.load_rate_limits() is None

    # 凭证损坏 → None
    Path(kimi.CREDENTIALS_PATH).write_text("{bad json", encoding="utf-8")
    assert kimi.load_rate_limits() is None


# --- k3 定价 ---


def test_k3_pricing_hit(monkeypatch):
    # 固定在内置兜底表上测（不依赖 litellm 在线表 / 本地缓存）
    monkeypatch.setattr(cost, "_pricing", cost._fallback_pricing())
    monkeypatch.setattr(cost, "_model_key_cache", {})
    entry = UsageEntry(
        timestamp=None, session_id="s", message_id="s", request_id="", model="k3",
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_creation_tokens=0, cache_read_tokens=0,
        cost_usd=None, project="p", agent_id="kimi-code",
    )
    # k3 兜底到 kimi-k2.7-code 同价（6.5 / 27 元每百万 ÷ 7.1 汇率）
    expected = 1_000_000 * (6.5 / 7.1 * 1e-6) + 1_000_000 * (27 / 7.1 * 1e-6)
    assert cost.calculate_cost(entry) == pytest.approx(expected)
    assert cost._resolve_model_key("k3", cost.get_pricing()) is not None


# --- kimi-heartbeat ---


def test_heartbeat_written(kimi_home, monkeypatch, capsys, tmp_path):
    hb_file = tmp_path / "cfg" / "kimi-heartbeat.json"
    monkeypatch.setattr(config, "KIMI_HEARTBEAT_FILE", str(hb_file))
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(kimi_watch.config, "KIMI_HEARTBEAT_FILE", str(hb_file))
    monkeypatch.setattr(kimi_watch.config, "CONFIG_DIR", str(tmp_path / "cfg"))

    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "session_abc"})))
    kimi_watch.write_heartbeat()

    data = json.loads(hb_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "session_abc"
    assert data["ts"] > 0
    # stdout 必须为空（防污染模型上下文）
    assert capsys.readouterr().out == ""


def test_heartbeat_bad_stdin_still_writes(kimi_home, monkeypatch, capsys, tmp_path):
    hb_file = tmp_path / "cfg" / "kimi-heartbeat.json"
    monkeypatch.setattr(kimi_watch.config, "KIMI_HEARTBEAT_FILE", str(hb_file))
    monkeypatch.setattr(kimi_watch.config, "CONFIG_DIR", str(tmp_path / "cfg"))

    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    kimi_watch.write_heartbeat()

    data = json.loads(hb_file.read_text(encoding="utf-8"))
    assert data["session_id"] == ""
    assert capsys.readouterr().out == ""


# --- setup hook 生成/移除幂等 ---


@pytest.fixture
def kimi_hooks_env(tmp_path, monkeypatch):
    kimi_dir = tmp_path / ".kimi-code"
    kimi_dir.mkdir()
    monkeypatch.setattr(hooks, "_KIMI", str(kimi_dir))
    monkeypatch.setattr(hooks, "KIMI_CONFIG", str(kimi_dir / "config.toml"))
    return kimi_dir


def test_install_kimi_hooks_idempotent(kimi_hooks_env):
    config_path = kimi_hooks_env / "config.toml"
    config_path.write_text('default_model = "kimi-code/k3"\n', encoding="utf-8")

    assert hooks.install_kimi_hooks() is True
    first = config_path.read_text(encoding="utf-8")
    # 用户原有配置保留
    assert 'default_model = "kimi-code/k3"' in first
    assert 'event = "Stop"' in first
    assert "kimi-heartbeat" in first

    # 重复安装：内容不变、返回 False（幂等）
    assert hooks.install_kimi_hooks() is False
    assert config_path.read_text(encoding="utf-8") == first


def test_uninstall_kimi_hooks(kimi_hooks_env):
    config_path = kimi_hooks_env / "config.toml"
    config_path.write_text('default_model = "kimi-code/k3"\n', encoding="utf-8")
    hooks.install_kimi_hooks()

    assert hooks.uninstall_kimi_hooks() is True
    content = config_path.read_text(encoding="utf-8")
    assert "token-tracker kimi hooks" not in content
    assert "kimi-heartbeat" not in content
    assert 'default_model = "kimi-code/k3"' in content  # 用户配置不动

    # 重复卸载幂等
    assert hooks.uninstall_kimi_hooks() is False


def test_install_kimi_hooks_creates_config(kimi_hooks_env):
    assert hooks.install_kimi_hooks() is True
    assert (kimi_hooks_env / "config.toml").exists()


def test_heartbeat_keeps_multiple_sessions(kimi_home, monkeypatch, tmp_path):
    hb_file = tmp_path / "cfg" / "kimi-heartbeat.json"
    monkeypatch.setattr(kimi_watch.config, "KIMI_HEARTBEAT_FILE", str(hb_file))
    monkeypatch.setattr(kimi_watch.config, "CONFIG_DIR", str(tmp_path / "cfg"))
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "session_a"})))
    kimi_watch.write_heartbeat()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "session_b"})))
    kimi_watch.write_heartbeat()
    data = json.loads(hb_file.read_text(encoding="utf-8"))
    assert {"session_a", "session_b"} <= set(data["sessions"])


def test_today_totals_uses_usage_timestamp_not_session_start(kimi_home, monkeypatch, tmp_path):
    now_ms = int(time.time() * 1000)
    _write_session(kimi_home, "wd_p_aa11bb22cc33", "session_old",
                   {"main": [_usage_record(input_other=7, output=11, time_ms=now_ms)]},
                   state={"createdAt": "2020-01-01T00:00:00.000Z"})
    hb_file = tmp_path / "cfg" / "kimi-heartbeat.json"
    monkeypatch.setattr(kimi_watch.config, "KIMI_HEARTBEAT_FILE", str(hb_file))
    monkeypatch.setattr(kimi_watch.config, "CONFIG_DIR", str(tmp_path / "cfg"))
    totals = kimi_watch._today_totals("session_old")
    assert totals["today"]["input"] == 7
    assert totals["today"]["output"] == 11
    assert totals["current"]["found"] is True


def test_kimi_hook_requires_explicit_setup(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.config, "resolve_lang", lambda: "en")
    monkeypatch.setattr(cli.config, "load_config", lambda: {"theme": "mocha"})
    monkeypatch.setattr(cli, "setup", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(cli, "get_console", lambda: type("C", (), {"print": lambda *_: None})())
    cli._auto_setup()
    assert calls == [{"auto": True, "install_kimi": False}]
    monkeypatch.setattr(cli, "_run_setup_flow", lambda **kwargs: calls.append(kwargs))
    assert cli._handle_non_data_command("setup", [])
    assert calls[-1] == {"install_kimi": True}

def test_setup_kimi_installs_windows_terminal_action(monkeypatch):
    messages = []
    monkeypatch.setattr(hooks, "_platform_name", lambda: "nt")
    monkeypatch.setattr(hooks, "install_kimi_hooks", lambda: False)
    monkeypatch.setattr(hooks, "install_kimi_watch_action", lambda: True)
    monkeypatch.setattr(hooks, "get_console", lambda: type("Console", (), {"print": messages.append})())

    hooks._setup_kimi()

    assert len(messages) == 1
    assert "kimi_watch_action_synced" not in messages[0]



def test_render_frame_splits_today_usage_into_three_lines():
    totals = {
        "today": {"input": 760_000, "output": 117_800, "cache": 20_100_000, "cost": 34.19},
        "current": {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "found": False},
    }

    frame = kimi_watch.render_frame([], None, False, None, totals)

    lines = [renderable.plain for renderable in frame.renderables]
    assert lines[-3:] == [" 今日 in 760.0K", " 今日 out 117.8K", " 今日 cache 20.1M  ≈¥34.19"]


def test_render_frame_splits_quota_into_two_lines():
    quota = RateLimits(five_hour_pct=10, seven_day_pct=20)
    totals = {
        "today": {"input": 0, "output": 0, "cache": 0, "cost": 0.0},
        "current": {"input": 0, "output": 0, "cache": 0, "cost": 0.0, "found": False},
    }

    frame = kimi_watch.render_frame([], quota, False, None, totals)

    lines = [renderable.plain for renderable in frame.renderables]
    assert lines[1].startswith(" 5h")
    assert lines[2].startswith(" 7d")


def test_quota_countdown_is_visible_in_very_narrow_right_pane():
    segment = kimi_watch._render_quota_segment("5h", 42, int(time.time()) + 3600, time.time(), 16, False)

    assert chr(0x23F3) in segment.plain
