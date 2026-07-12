"""报表渲染共享的周/月合并与周期计算。"""

import calendar
from datetime import datetime

from ..adapters.types import MonthlyStats, WeeklyStats
from ..tz import system_tz


def merge_weeks(stats: list[WeeklyStats]) -> list[WeeklyStats]:
    """跨 Agent 合并同一周，按周升序返回。"""
    merged: dict[str, WeeklyStats] = {}
    for stat in stats:
        current = merged.get(stat.week)
        if current is None:
            current = merged[stat.week] = WeeklyStats(
                week=stat.week, week_start=stat.week_start, week_end=stat.week_end,
            )
        _add_period_fields(current, stat)
    return [merged[key] for key in sorted(merged)]


def merge_months(stats: list[MonthlyStats]) -> list[MonthlyStats]:
    """跨 Agent 合并同一月，按月升序返回。"""
    merged: dict[str, MonthlyStats] = {}
    for stat in stats:
        current = merged.get(stat.month)
        if current is None:
            current = merged[stat.month] = MonthlyStats(month=stat.month)
        _add_period_fields(current, stat)
    return [merged[key] for key in sorted(merged)]


def _add_period_fields(target: WeeklyStats | MonthlyStats,
                       source: WeeklyStats | MonthlyStats) -> None:
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    target.cache_creation_tokens += source.cache_creation_tokens
    target.cache_read_tokens += source.cache_read_tokens
    target.total_tokens += source.total_tokens
    target.cost_usd += source.cost_usd
    target.session_count += source.session_count
    target.message_count += source.message_count
    for key, value in source.models.items():
        target.models[key] = target.models.get(key, 0) + value
    for key, value in source.projects.items():
        target.projects[key] = target.projects.get(key, 0) + value


def month_span(month: str) -> tuple[int, int]:
    """返回（本月总天数，已过天数）；历史月按整月。"""
    year, mon = int(month[:4]), int(month[5:7])
    total = calendar.monthrange(year, mon)[1]
    today = datetime.now(system_tz()).date()
    elapsed = today.day if (year, mon) == (today.year, today.month) else total
    return total, elapsed
