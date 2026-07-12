"""GitHub 贡献图风格的 daily token 热力图渲染。

替代原 daily 逐日表格：紧凑总览 + 月份表头 + 7 行（星期）× N 列（周）的深浅绿方格 + 图例。
彩色靠 forced_color_console() 强制 24-bit 输出，因此终端直跑与会话内 `!tt daily` 都能看到颜色。
每格 = 方块 ■ + 间隔空格（_CELL_W 显示宽），分离成方格；按终端宽度自适应周数、soft_wrap 避免折行。
总览自己渲染（不复用 dashboard 的宽 header），紧凑单行、半屏不折。
"""

from datetime import date, datetime, timedelta

from rich.cells import cell_len
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from ..adapters.types import DailyStats, MonthlyStats, WeeklyStats
from ..i18n import t
from ..tz import system_tz
from .console import forced_color_console, get_console
from .format import _fmt_cost, _fmt_tokens, append_metric, brand_line, emit_metrics
from .report_stats import merge_months, merge_weeks, month_span
from .theme import _S, _heat_level, _heat_thresholds, heat_greens

_WEEKS = 53
_CELL_W = 2  # 每格显示宽：方块(1) + 间隔(1)；■ 在多数终端按 1 列渲染
_INDENT = 2       # 整面板左缩进
_LABEL_COL = 6    # 星期标签列显示宽（含间隔），容纳「周日」/「Sun」全称
_BLOCK_X = _INDENT + _LABEL_COL  # 方块起始列偏移（月份表头 / 图例对齐它）


def render_daily_heatmap(stats: list[DailyStats], agents: list[str] | None = None,
                          weekly: list[WeeklyStats] | None = None,
                          monthly: list[MonthlyStats] | None = None) -> None:
    if not stats:
        get_console().print(f"[{_S.warn}]{t('no_data')}[/{_S.warn}]")
        return

    # 按天合并 token
    tokens_by_date: dict[str, int] = {}
    for s in stats:
        tokens_by_date[s.date] = tokens_by_date.get(s.date, 0) + s.total_tokens

    with forced_color_console():
        # 顶部单一 Panel 内拼三段：Last 12 months / This Month / This Week（粗→细），
        # 共享一个品牌行 + 红 Rule；段间用 dim Rule 区分；This Month / This Week 各只一行 Tokens/Cost/Avg/Cost
        _render_summary(stats, agents, weekly, monthly)
        _render_grid(tokens_by_date)
        _render_legend()


def _render_summary(stats: list[DailyStats], agents: list[str] | None,
                    weekly: list[WeeklyStats] | None,
                    monthly: list[MonthlyStats] | None) -> None:
    today = datetime.now(system_tz()).date()  # 与聚合分桶同口径（系统时区）
    year_ago = (today - timedelta(days=365)).isoformat()
    rows = [s for s in stats if s.date >= year_ago]  # 过去一年（与热力图范围一致）

    # 品牌行（Token Tracker + agent 暗红）+ 红分割线
    brand = brand_line(agents or ["Claude Code"])
    avail = max(40, get_console().width - 6)  # 卡片可用内容宽 = 终端 - 缩进2 - 边框2 - padding2

    # --- Section 1：Last 12 months（保留原 3 行指标） ---
    body_year = Text()
    body_year.append("Last 12 months", style=f"bold {_S.good}")
    body_year.append(f"  {year_ago} ~ {today.isoformat()}", style=f"dim {_S.good}")
    body_year.append("\n")
    days = len({s.date for s in rows})
    total_cost = sum(s.cost_usd for s in rows)
    # 第一行（橙）：Tokens / Cost / Sessions / Avg/Cost / Active Days（Avg/Cost = 总成本 ÷ 活跃天数）
    emit_metrics(body_year, [
        ("Tokens", _fmt_tokens(sum(s.total_tokens for s in rows))),
        ("Cost", _fmt_cost(total_cost)),
        ("Sessions", str(sum(s.session_count for s in rows))),
        ("Avg/Cost", _fmt_cost(total_cost / days if days else 0)),
        (t("active_days"), str(days)),
    ], _S.peach, avail)
    body_year.append("\n")
    # 第二行（蓝）：单日峰值 token / 当前·最长连续活跃天数。
    # rows 可能为空（全部数据老于 12 个月）：峰值显示 "--"，不能对空列表 max()
    peak = max(rows, key=lambda s: s.total_tokens) if rows else None
    dts = sorted(date.fromisoformat(d) for d in {s.date for s in rows})
    cur_streak = longest_streak = 1 if dts else 0
    for i in range(1, len(dts)):
        cur_streak = cur_streak + 1 if (dts[i] - dts[i - 1]).days == 1 else 1
        longest_streak = max(longest_streak, cur_streak)
    if dts and (today - dts[-1]).days > 1:  # 最近活跃日距今 > 1 天，当前连续已断
        cur_streak = 0
    emit_metrics(body_year, [
        (t("daily_peak"), f"{peak.date[5:]} ({_fmt_tokens(peak.total_tokens)})" if peak else "--"),
        (t("daily_streak"), f"{cur_streak}/{longest_streak}{t('unit_day')}"),
    ], _S.blue, avail)

    parts: list = [brand, Rule(style=f"bold {_S.red}"), body_year]

    # --- Section 2：This Month（Tokens / Cost / Avg/Cost 带环比 + 活跃天数 X/月总天数） ---
    if monthly:
        months = merge_months(monthly)
        cur_m = months[-1]
        prev_m = months[-2] if len(months) >= 2 else None
        days_in_m, elapsed_m = month_span(cur_m.month)
        cur_m_avg = cur_m.cost_usd / max(1, elapsed_m)
        prev_m_avg = prev_m.cost_usd / max(1, month_span(prev_m.month)[0]) if prev_m else None
        active_m = len({s.date for s in stats if s.date.startswith(cur_m.month)})
        body_m = _period_section("This Month", cur_m.month, cur_m, prev_m,
                                 cur_m_avg, prev_m_avg, f"{active_m}/{days_in_m}")
        parts.extend([Rule(style=_S.dim), body_m])

    # --- Section 3：This Week（Tokens / Cost / Avg/Cost 带环比 + 活跃天数 X/7） ---
    if weekly:
        weeks_list = merge_weeks(weekly)
        cur_w = weeks_list[-1]
        prev_w = weeks_list[-2] if len(weeks_list) >= 2 else None
        this_monday = datetime.fromisoformat(cur_w.week).date()
        days_w = max(1, min(7, (today - this_monday).days + 1))
        cur_w_avg = cur_w.cost_usd / days_w
        prev_w_avg = prev_w.cost_usd / 7 if prev_w else None
        active_w = len({s.date for s in stats if s.date >= cur_w.week})
        body_w = _period_section("This Week", f"{cur_w.week_start} ~ {cur_w.week_end}",
                                 cur_w, prev_w, cur_w_avg, prev_w_avg, f"{active_w}/7")
        parts.extend([Rule(style=_S.dim), body_w])

    get_console().print(Padding(Panel(Group(*parts),
                                      expand=False, border_style=_S.blue, padding=(0, 1)),
                                (0, 0, 0, _INDENT), expand=False))
    get_console().print()


def _period_section(title: str, subtitle: str,
                    cur: WeeklyStats | MonthlyStats, prev: WeeklyStats | MonthlyStats | None,
                    cur_avg: float, prev_avg: float | None, active_str: str) -> Text:
    """This Month / This Week 段：标题 + 区间，单行橙色 Tokens/Cost/Avg/Cost（带环比）+ 活跃天数（不带环比）。
    cur/prev 鸭子类型——Monthly/WeeklyStats 都有 total_tokens / cost_usd。"""
    body = Text()
    body.append(title, style=f"bold {_S.good}")
    body.append(f"  {subtitle}", style=f"dim {_S.good}")
    body.append("\n")
    append_metric(body, "Tokens", _fmt_tokens(cur.total_tokens), _S.peach,
                  cur.total_tokens, prev.total_tokens if prev else None)
    body.append("   ")
    append_metric(body, "Cost", _fmt_cost(cur.cost_usd), _S.peach,
                  cur.cost_usd, prev.cost_usd if prev else None)
    body.append("   ")
    append_metric(body, "Avg/Cost", _fmt_cost(cur_avg), _S.peach, cur_avg, prev_avg)
    body.append("   ")
    append_metric(body, t("active_days"), active_str, _S.peach)
    return body


def _display_weeks() -> int:
    """要显示的周数，右对齐只保留最近若干周。宽度交给 Rich console 判定（它依次读 tty
    尺寸、`COLUMNS`，都拿不到才回落 80）；装不下整年时砍掉最左（最老）的周、不折行。
    `!` 非 tty 或窄屏想多显示几周，可 `export COLUMNS=<列数>` 精确控制。"""
    width = get_console().width
    return max(8, min(_WEEKS, (width - _BLOCK_X) // _CELL_W))  # 减去缩进 + 星期标签列


def _render_grid(tokens_by_date: dict[str, int]) -> None:
    today = datetime.now(system_tz()).date()
    days_since_sunday = (today.weekday() + 1) % 7  # Mon=0→1 … Sun=6→0
    this_sunday = today - timedelta(days=days_since_sunday)

    weeks = _display_weeks()
    start_sunday = this_sunday - timedelta(weeks=weeks - 1)

    thresholds = _heat_thresholds(list(tokens_by_date.values()))

    # 月份表头：月份变化处标月名（跟随语言；中文「1月」等 CJK 占 2 列也按 cell 宽对齐、不错位）。
    # prev_month 预设为首列月 → 跳过左端不完整的部分月；按已渲染 cell 宽防月名相互重叠。
    months = t("month_short").split(",")
    header = ""
    prev_month = start_sunday.month
    for c in range(weeks):
        col_day = start_sunday + timedelta(weeks=c)
        if col_day.month != prev_month:
            prev_month = col_day.month
            start = c * _CELL_W
            if cell_len(header) <= start:  # 不与上一个月名重叠才标
                header += " " * (start - cell_len(header)) + months[col_day.month - 1]
    get_console().print(Text(" " * _BLOCK_X + header, style=_S.dim), soft_wrap=True)

    # 7 行：星期标签 + 方块 + 间隔
    hg = heat_greens()
    day_labels = t("weekday_grid").split(",")  # 周日 / Sun 开头，跟随语言
    for r in range(7):
        line = Text(" " * _INDENT)
        label = day_labels[r]
        line.append(label + " " * (_LABEL_COL - cell_len(label)), style=_S.dim)  # pad 到统一列宽
        for c in range(weeks):
            d = start_sunday + timedelta(weeks=c, days=r)
            if d > today:
                line.append(" " * _CELL_W)  # 未来日期占位（保持列对齐）
                continue
            level = _heat_level(tokens_by_date.get(d.isoformat(), 0), thresholds)
            line.append("■", style=hg[level])
            line.append(" ")  # 格子间隔
        get_console().print(line, soft_wrap=True)


def _render_legend() -> None:
    get_console().print()
    line = Text()
    line.append(" " * _BLOCK_X + "Less ", style=_S.dim)  # 对齐方块起始；Less / More 不翻译
    for color in heat_greens():
        line.append("■", style=color)
        line.append(" ")
    line.append("More", style=_S.dim)
    get_console().print(line, soft_wrap=True)
