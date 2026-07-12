"""tt sidebar 面板：活跃会话列表 + 各自最近提示词，为窄窗格（终端分屏 / tmux pane）常驻设计。

每会话一块：状态点 + 项目名 + agent/模型 + 距上次活动；下面缩进列最近提示词
（时间正序，最新一条常亮、更早的调暗）。整体单行截断不折行，窄屏不破版。
"""

from datetime import UTC, datetime

from rich.console import Group
from rich.text import Text

from ..i18n import t
from ..sidebar import ATTENTION, IDLE, RUNNING, WAITING, LiveSession
from ..tz import system_tz
from .format import AGENT_SHORT, _model_short
from .theme import _S

_STATE_DOTS = {RUNNING: "●", ATTENTION: "●", WAITING: "●", IDLE: "○"}


def _state_style(state: str) -> str:
    return {RUNNING: _S.good, ATTENTION: _S.bad, WAITING: _S.warn}.get(state, _S.dim)


def _fmt_ago(now: datetime, ts: datetime) -> str:
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _one_line(text: str, limit: int = 200) -> str:
    """提示词压成单行（换行折叠成空格），粗剪到 limit 字符，精细截断交给 Rich ellipsis。"""
    flat = " ".join(text.split())
    return flat[:limit]


def render_sidebar(sessions: list[LiveSession]) -> Group:
    lines: list[Text] = []
    header = Text(no_wrap=True, overflow="ellipsis")
    header.append("✳ tt sidebar", style=_S.accent)
    header.append(datetime.now(system_tz()).strftime("  %H:%M:%S"), style=_S.dim)
    lines.append(header)

    if not sessions:
        lines.append(Text(""))
        lines.append(Text(t("sidebar_empty"), style=_S.dim))
        return Group(*lines)

    now = datetime.now(UTC)
    for s in sessions:
        lines.append(Text(""))
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(_STATE_DOTS.get(s.state, "●") + " ", style=_state_style(s.state))
        head.append(s.project, style="bold")
        head.append(f" · {AGENT_SHORT.get(s.agent_id, s.agent_id)}", style=_S.blue)
        if s.model:
            head.append(f" · {_model_short(s.model)}", style=_S.dim)
        head.append(f" · {_fmt_ago(now, s.last_activity)}", style=_S.dim)
        head.append(f"  {t('sidebar_state_' + s.state)}", style=_state_style(s.state))
        lines.append(head)
        for i, p in enumerate(s.prompts):
            newest = i == len(s.prompts) - 1
            line = Text("  ", no_wrap=True, overflow="ellipsis")
            line.append("└ " if newest else "│ ", style=_S.dim)
            line.append(_one_line(p.text), style="" if newest else _S.dim)
            lines.append(line)
    return Group(*lines)
