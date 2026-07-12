"""表格渲染（weekly/monthly 卡片、Trend 柱状/进度条、Project/Model 分布）。

格式化/主题已拆到 format.py / theme.py；daily 走 heatmap.py（贡献图），
本模块聚焦 weekly/monthly 两类报表的组装。
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..adapters.types import DailyStats, MonthlyStats, WeeklyStats
from ..i18n import t
from ..tz import system_tz
from .console import forced_color_console, get_console
from .format import (
    _fmt_cost,
    _fmt_tokens,
    _model_short,
    _project_short,
    append_metric,
    brand_line,
)
from .report_stats import merge_months, merge_weeks, month_span
from .theme import _S

__all__ = [
    "render_monthly",
    "render_weekly",
]


def render_weekly(stats: list[WeeklyStats], agents: list[str] | None = None,
                  daily: list[DailyStats] | None = None) -> None:
    if not stats:
        get_console().print(f"[{_S.warn}]{t('no_data')}[/{_S.warn}]")
        return

    weeks = merge_weeks(stats)
    cur = weeks[-1]
    prev = weeks[-2] if len(weeks) >= 2 else None

    with forced_color_console():
        active_days = len({d.date for d in daily if d.date >= cur.week}) if daily else 0
        _render_week_summary(cur, prev, agents or ["Claude Code"], active_days)
        if daily:
            by_date: dict[str, int] = {}
            for d in daily:
                by_date[d.date] = by_date.get(d.date, 0) + d.total_tokens
            _render_daily_barchart(by_date)
        _render_weekly_trend(weeks)
        _render_distribution("Project Trend", "Project", cur.projects, _project_short, _S.pink, min_pct=2)
        _render_distribution("Model Trend", "Model", cur.models, _model_short, _S.blue, min_pct=2)
        get_console().print(Text("  tt · by stormzhang", style=_S.dim))


def _render_daily_barchart(by_date: dict[str, int], days_back: int = 30, height: int = 6) -> None:
    """最近 days_back 天的每日 token 垂直柱状图（▁-█ sub-cell 精度，列间留白）：每天都画一列，
    峰值柱上方标日期、底部两端标起止日期。无可见高度的天（空白天、或量极小不足一格）
    在最底行画一格基线，基线色比真实矮柱更暗、一眼可辨；时间轴连续、空白天一目了然；
    不再做前导裁剪或中段空段压缩，整段窗口如实全显示。"""
    today = datetime.now(system_tz()).date()  # 与聚合分桶同口径（系统时区）
    dates = [today - timedelta(days=i) for i in range(days_back - 1, -1, -1)]
    vals = [by_date.get(d.isoformat(), 0) for d in dates]
    max_v = max(vals) or 1
    blocks = " ▁▂▃▄▅▆▇█"

    width = len(vals) * 2 - 1
    top3 = set(sorted(range(len(vals)), key=lambda k: vals[k], reverse=True)[:3])
    peak_k = max(range(len(vals)), key=lambda k: vals[k])

    title = Text()
    title.append("[Daily Trend]", style=f"bold {_S.peach}")
    title.append(f" (last {len(dates)}d)", style=f"dim {_S.peach}")
    lines: list[Text] = [title, Text()]
    peak_label = dates[peak_k].strftime("%m/%d")
    top = [" "] * width
    pos = max(0, min(width - len(peak_label), peak_k * 2 - len(peak_label) // 2))
    for jx, ch in enumerate(peak_label):
        top[pos + jx] = ch
    lines.append(Text("".join(top), style=_S.peach))
    # 基线（空白天 / 量极小不足一格的天）专用更暗的 peach，和真实矮柱（dim peach）拉开、一眼可辨
    baseline_style = f"dim {_darken(_S.peach, 0.4)}"
    for row in range(height, 0, -1):
        line = Text()
        for k, v in enumerate(vals):
            bar_h = v / max_v * height
            if round(bar_h * 8) == 0:  # 没有可见高度（空白天或量极小）→ 最底行画暗基线占位
                line.append("▁" if row == 1 else " ", style=baseline_style)
            else:
                diff = bar_h - (row - 1)
                ch = "█" if diff >= 1 else " " if diff <= 0 else blocks[round(diff * 8)]
                line.append(ch, style=_S.peach if k in top3 else f"dim {_S.peach}")
            if k < len(vals) - 1:
                line.append(" ")
        lines.append(line)
    start, end = dates[0].strftime("%m/%d"), dates[-1].strftime("%m/%d")
    gap = max(1, width - len(start) - len(end))
    lines.append(Text(start + " " * gap + end, style=_S.dim))
    get_console().print(Padding(Group(*lines), (0, 0, 0, 2), expand=False))
    get_console().print()


def _render_weekly_barchart(weeks: list[WeeklyStats], weeks_back: int = 30, height: int = 6) -> None:
    """最近 weeks_back 个日历周的每周 token 垂直柱状图（▁-█ sub-cell，列间留白），橙色，仿 Daily Trend 但按周：
    固定窗口、每周都画一列（含没用过的空白周），峰值柱上方标该周起止日期、底部两端标起止周。
    无可见高度的周（空白周、或量极小不足一格）画一格基线，基线色比真实矮柱更暗、一眼可辨。"""
    if not weeks:
        return
    # 固定最近 weeks_back 个日历周窗口（对齐 daily 的固定 30 天）：没用过 / 缺失的周补 0。
    # week 是 monday 的 ISO 日期，从本周一往前逐周对齐
    by_week = {w.week: w for w in weeks}
    today = datetime.now(system_tz()).date()
    this_monday = today - timedelta(days=today.weekday())
    recent: list[WeeklyStats] = []
    for i in range(weeks_back - 1, -1, -1):
        mon = this_monday - timedelta(weeks=i)
        w = by_week.get(mon.isoformat())
        if w is None:
            sun = mon + timedelta(days=6)
            w = WeeklyStats(week=mon.isoformat(), week_start=mon.strftime("%m-%d"), week_end=sun.strftime("%m-%d"))
        recent.append(w)

    vals = [w.total_tokens for w in recent]
    labels = [f"{int(w.week_start[:2])}/{w.week_start[3:]}" for w in recent]  # "06-15" -> "6/15"
    max_v = max(vals) or 1
    blocks = " ▁▂▃▄▅▆▇█"
    width = len(recent) * 2 - 1
    top3 = set(sorted(range(len(vals)), key=lambda k: vals[k], reverse=True)[:3])
    peak_k = max(range(len(vals)), key=lambda k: vals[k])

    title = Text()
    title.append("[Weekly Trend]", style=f"bold {_S.peach}")
    title.append(f" (last {len(recent)} weeks)", style=f"dim {_S.peach}")
    lines: list[Text] = [title, Text()]
    top = [" "] * width
    pw = recent[peak_k]  # 峰值周：标该周起止日期（如 6/15-6/21）
    peak_label = f"{int(pw.week_start[:2])}/{pw.week_start[3:]}-{int(pw.week_end[:2])}/{pw.week_end[3:]}"
    pos = max(0, min(width - len(peak_label), peak_k * 2 - len(peak_label) // 2))
    for jx, ch in enumerate(peak_label):
        if pos + jx < width:  # 周数很少时 width 可能短于标签，越界部分丢弃
            top[pos + jx] = ch
    lines.append(Text("".join(top), style=_S.peach))
    # 基线（空白周 / 量极小不足一格的周）专用更暗的 peach，和真实矮柱（dim peach）拉开、一眼可辨
    baseline_style = f"dim {_darken(_S.peach, 0.4)}"
    for row in range(height, 0, -1):
        line = Text()
        for k, v in enumerate(vals):
            bar_h = v / max_v * height
            if round(bar_h * 8) == 0:  # 没有可见高度（空白周或量极小）→ 最底行画暗基线占位
                line.append("▁" if row == 1 else " ", style=baseline_style)
            else:
                diff = bar_h - (row - 1)
                ch = "█" if diff >= 1 else " " if diff <= 0 else blocks[round(diff * 8)]
                line.append(ch, style=_S.peach if k in top3 else f"dim {_S.peach}")
            if k < len(vals) - 1:
                line.append(" ")
        lines.append(line)
    gap = max(1, width - len(labels[0]) - len(labels[-1]))
    lines.append(Text(labels[0] + " " * gap + labels[-1], style=_S.dim))
    get_console().print(Padding(Group(*lines), (0, 0, 0, 2), expand=False))
    get_console().print()


def _darken(color: str, factor: float = 0.6) -> str:
    """把 #RRGGBB 按 factor 压暗（保持色相），用于突出排第一的项。"""
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _bar_text(ratio: float, fill_style: str, width: int = 20) -> Text:
    """半高进度条（▄ 仅占行下半，相邻行天然空半行）：填充用 fill_style、
    空槽用同色 dim 版（同色深浅对比，仿 daily 方格）。"""
    filled = round(max(0.0, min(1.0, ratio)) * width)
    t = Text()
    t.append("▄" * filled, style=fill_style)
    t.append("▄" * (width - filled), style=f"dim {fill_style}")
    return t


def _render_week_summary(cur: WeeklyStats, prev: WeeklyStats | None, agents: list[str],
                         active_days: int) -> None:
    """本周分析卡片：品牌行 + 分割线 + 本周区间；第二行 Tokens/Cost/Avg·Cost（橙）、
    第三行 Sessions/Msgs/Active Days（蓝），带环比上周。Avg/Cost = 本周成本 ÷ 已过天数（日均）。"""
    brand = brand_line(agents)
    body = Text()
    body.append("This Week", style=f"bold {_S.good}")
    body.append(f"  {cur.week_start} ~ {cur.week_end}", style=f"dim {_S.good}")
    body.append("\n")
    this_monday = datetime.fromisoformat(cur.week).date()
    days = max(1, min(7, (datetime.now(system_tz()).date() - this_monday).days + 1))
    cur_avg = cur.cost_usd / days
    prev_avg = prev.cost_usd / 7 if prev else None
    # 第二行（橙）：Tokens / Cost / Avg/Cost（日均花费）
    append_metric(body,"Tokens", _fmt_tokens(cur.total_tokens), _S.peach,
                   cur.total_tokens, prev.total_tokens if prev else None)
    body.append("   ")
    append_metric(body,"Cost", _fmt_cost(cur.cost_usd), _S.peach,
                   cur.cost_usd, prev.cost_usd if prev else None)
    body.append("   ")
    append_metric(body,"Avg/Cost", _fmt_cost(cur_avg), _S.peach, cur_avg, prev_avg)
    body.append("\n")
    # 第三行（粉）：Sessions / Msgs
    append_metric(body,"Sessions", str(cur.session_count), _S.blue,
                   cur.session_count, prev.session_count if prev else None)
    body.append("   ")
    append_metric(body,"Msgs", str(cur.message_count), _S.blue,
                   cur.message_count, prev.message_count if prev else None)
    body.append("   ")
    append_metric(body,t("active_days"), f"{active_days}/7", _S.blue, active_days, None)
    get_console().print(Panel(Group(brand, Rule(style=f"bold {_S.red}"), body),
                              expand=False, border_style=_S.blue, padding=(0, 1)))
    get_console().print()


def _render_weekly_trend(weeks: list[WeeklyStats], limit: int = 8) -> None:
    """逐周 token 进度条（最近若干周，最新在上；逐周同亮绿，本周在最上无需另高亮）。"""
    recent = weeks[-limit:]
    max_tok = max((w.total_tokens for w in recent), default=0) or 1
    table = Table(title=Text("[Weekly Trend]", style=f"bold {_S.good}"), title_justify="left", box=box.SIMPLE,
                  header_style="bold", padding=(0, 1), expand=False, border_style=_S.good)
    table.add_column("Week", style=_S.good, no_wrap=True)
    table.add_column("Token", justify="right")
    table.add_column("", min_width=20)
    for w in reversed(recent):
        table.add_row(
            f"{w.week_start} ~ {w.week_end}",
            _fmt_tokens(w.total_tokens),
            _bar_text(w.total_tokens / max_tok, _S.good),
        )
    get_console().print(Padding(table, (0, 0, 0, 2), expand=False))


def _render_distribution(title: str, name_col: str, data: dict[str, int],
                         short_fn: Callable[[str], str], accent: str, min_pct: float = 0.0) -> None:
    """通用分布表（Project / Model）：标题、名称列、进度条统一用模块主色 accent，按 token 降序。
    min_pct>0 时过滤掉占比 ≤min_pct% 的项（Project / Model 都取 >2%），=0 则全部显示；Token/Ratio 数值保持中性。"""
    if not data:
        return
    total = sum(data.values())
    items = sorted(data.items(), key=lambda x: x[1], reverse=True)
    table = Table(title=Text(f"[{title}]", style=f"bold {accent}"), title_justify="left", box=box.SIMPLE,
                  header_style="bold", padding=(0, 1), expand=False, border_style=accent)
    table.add_column(name_col, style=accent, no_wrap=True)
    table.add_column("Token", justify="right")
    table.add_column("", min_width=20)
    table.add_column("", justify="right", style=f"dim {accent}")
    for idx, (name, tokens) in enumerate(items):
        pct = tokens / total * 100 if total else 0
        if min_pct and pct <= min_pct:  # 过滤长尾小项（Project / Model 只留 >2%）
            continue
        fill = accent if idx == 0 else _darken(accent)
        table.add_row(short_fn(name), _fmt_tokens(tokens),
                      _bar_text(pct / 100, fill), f"{pct:.1f}%")
    get_console().print(Padding(table, (0, 0, 0, 2), expand=False))


def _render_month_summary(cur: MonthlyStats, prev: MonthlyStats | None, agents: list[str],
                          active_days: int) -> None:
    """本月分析卡片：品牌行 + 分割线 + 月份；第二行 Tokens/Cost/Avg·Cost（橙）、
    第三行 Sessions/Msgs/Active Days（蓝），带环比上月。Avg/Cost = 本月成本 ÷ 已过天数（日均）。"""
    brand = brand_line(agents)
    body = Text()
    body.append("This Month", style=f"bold {_S.good}")
    body.append(f"  {cur.month}", style=f"dim {_S.good}")
    body.append("\n")
    days_in_month, elapsed = month_span(cur.month)
    cur_avg = cur.cost_usd / max(1, elapsed)
    prev_avg = prev.cost_usd / max(1, month_span(prev.month)[0]) if prev else None
    # 第二行（橙）：Tokens / Cost / Avg/Cost（日均花费）
    append_metric(body, "Tokens", _fmt_tokens(cur.total_tokens), _S.peach,
                  cur.total_tokens, prev.total_tokens if prev else None)
    body.append("   ")
    append_metric(body, "Cost", _fmt_cost(cur.cost_usd), _S.peach,
                  cur.cost_usd, prev.cost_usd if prev else None)
    body.append("   ")
    append_metric(body, "Avg/Cost", _fmt_cost(cur_avg), _S.peach, cur_avg, prev_avg)
    body.append("\n")
    # 第三行（蓝）：Sessions / Msgs / Active Days
    append_metric(body, "Sessions", str(cur.session_count), _S.blue,
                  cur.session_count, prev.session_count if prev else None)
    body.append("   ")
    append_metric(body, "Msgs", str(cur.message_count), _S.blue,
                  cur.message_count, prev.message_count if prev else None)
    body.append("   ")
    append_metric(body, t("active_days"), f"{active_days}/{days_in_month}", _S.blue, active_days, None)
    get_console().print(Panel(Group(brand, Rule(style=f"bold {_S.red}"), body),
                              expand=False, border_style=_S.blue, padding=(0, 1)))
    get_console().print()


def _render_monthly_trend(months: list[MonthlyStats], limit: int = 12) -> None:
    """逐月 token 进度条（最近若干月，最新在上；逐月同亮绿，本月在最上无需另高亮）。"""
    recent = months[-limit:]
    max_tok = max((m.total_tokens for m in recent), default=0) or 1
    table = Table(title=Text("[Monthly Trend]", style=f"bold {_S.good}"), title_justify="left", box=box.SIMPLE,
                  header_style="bold", padding=(0, 1), expand=False, border_style=_S.good)
    table.add_column("Month", style=_S.good, no_wrap=True)
    table.add_column("Token", justify="right")
    table.add_column("", min_width=20)
    for m in reversed(recent):
        table.add_row(
            m.month,
            _fmt_tokens(m.total_tokens),
            _bar_text(m.total_tokens / max_tok, _S.good),
        )
    get_console().print(Padding(table, (0, 0, 0, 2), expand=False))


def render_monthly(stats: list[MonthlyStats], agents: list[str] | None = None,
                   daily: list[DailyStats] | None = None,
                   weekly: list[WeeklyStats] | None = None) -> None:
    if not stats:
        get_console().print(f"[{_S.warn}]{t('no_data')}[/{_S.warn}]")
        return

    months = merge_months(stats)
    cur = months[-1]
    prev = months[-2] if len(months) >= 2 else None

    with forced_color_console():
        active_days = len({d.date for d in daily if d.date.startswith(cur.month)}) if daily else 0
        _render_month_summary(cur, prev, agents or ["Claude Code"], active_days)
        if weekly:
            _render_weekly_barchart(merge_weeks(weekly))
        _render_monthly_trend(months)
        _render_distribution("Project Trend", "Project", cur.projects, _project_short, _S.pink, min_pct=2)
        _render_distribution("Model Trend", "Model", cur.models, _model_short, _S.blue, min_pct=2)
        get_console().print(Text("  tt · by stormzhang", style=_S.dim))
