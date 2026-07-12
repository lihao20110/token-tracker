import re
from datetime import UTC, datetime, timedelta

from token_tracker.adapters.types import DailyStats
from token_tracker.ui.heatmap import render_daily_heatmap
from token_tracker.ui.theme import _heat_level, _heat_thresholds


def test_heat_level_zero_and_negative():
    assert _heat_level(0, [1, 2, 3]) == 0
    assert _heat_level(-5, [1, 2, 3]) == 0


def test_heat_level_buckets():
    th = [10, 20, 30]
    assert _heat_level(5, th) == 1
    assert _heat_level(10, th) == 1
    assert _heat_level(15, th) == 2
    assert _heat_level(25, th) == 3
    assert _heat_level(100, th) == 4


def test_heat_thresholds_nonzero_only():
    th = _heat_thresholds([0, 0, 100, 200, 300, 400])
    assert len(th) == 3
    assert all(x > 0 for x in th)
    assert th == sorted(th)


def test_heat_thresholds_all_zero():
    assert _heat_thresholds([0, 0]) == [1, 1, 1]


def test_render_heatmap_outputs_truecolor(capsys, monkeypatch):
    # 产品明确尊重 NO_COLOR；本用例只验证真彩渲染，必须隔离调用环境里的全局禁色变量。
    monkeypatch.delenv("NO_COLOR", raising=False)
    # 用相对当天的近期日期，保证数据落在最近一年范围内、能渲染出多档绿
    today = datetime.now(UTC).date()
    stats = [
        DailyStats(date=(today - timedelta(days=1)).isoformat(),
                   total_tokens=1000, cost_usd=1.0, message_count=3, session_count=1),
        DailyStats(date=(today - timedelta(days=10)).isoformat(),
                   total_tokens=50000, cost_usd=5.0, message_count=10, session_count=2),
        DailyStats(date=(today - timedelta(days=20)).isoformat(),
                   total_tokens=200000, cost_usd=20.0, message_count=30, session_count=5),
    ]
    render_daily_heatmap(stats, ["Claude Code"])
    out = capsys.readouterr().out
    from token_tracker.i18n import t
    wk = t("weekday_grid").split(",")
    assert wk[0] in out and wk[-1] in out  # 星期标签（跟随语言）
    colors = set(re.findall(r"38;2;\d+;\d+;\d+", out))
    assert len(colors) >= 2  # 空格档 + 至少一档绿，验证 24-bit truecolor 多档
    months = t("month_short").split(",")
    assert any(m in out for m in months)  # 月份表头存在（跟随语言）
    assert "by stormzhang" not in out  # daily 图例不再带署名


def test_render_heatmap_empty(capsys):
    render_daily_heatmap([], ["Claude Code"])
    out = capsys.readouterr().out
    assert out  # 不报错，输出 no_data 提示
