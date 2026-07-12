"""tt sidebar 面板：活跃会话列表 + 各自最近提示词，为窄窗格（终端分屏 / tmux pane）常驻设计。

每会话一块：状态点 + 项目名 + agent/模型 + 距上次活动；下面缩进列最近提示词
（时间正序，最新一条常亮、更早的调暗）。头行单行截断不折行；提示词最多折
`_PROMPT_MAX_LINES` 行、超出末行加省略号、正文右侧固定留 `_RIGHT_PAD` 格空白。
"""

from datetime import UTC, datetime

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.text import Text

from ..i18n import t
from ..sidebar import ATTENTION, IDLE, RUNNING, WAITING, LiveSession
from ..tz import system_tz
from .format import AGENT_SHORT, _model_short
from .theme import _S

_STATE_DOTS = {RUNNING: "●", ATTENTION: "●", WAITING: "●", IDLE: "○"}

_PROMPT_MAX_LINES = 2  # 每条提示词最多折 N 行，超出末行省略号
_PREFIX_WIDTH = 4      # 左侧前缀「  └ 」/「  │ 」占 4 格
_RIGHT_PAD = 2         # 正文右侧留白，折行不顶到窗格右缘


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


def _one_line(text: str, limit: int = 300) -> str:
    """提示词压成单行（换行折叠成空格），粗剪到 limit 字符，精细折行/截断交给 _PromptText。"""
    flat = " ".join(text.split())
    return flat[:limit]


class _PromptText:
    """单条提示词：渲染期按实际宽度折行，最多 _PROMPT_MAX_LINES 行、超出末行加省略号。

    首行带树枝符（└/│）；续行在「│」条目下延续竖线保持轨道连贯、「└」条目下留空。
    宽度在 __rich_console__ 里才拿得到，故做成 renderable 而非预构建 Text——
    Rich 快照（--once）与 Textual Static 两个渲染路径通用。
    """

    def __init__(self, text: str, rail: str, style: str) -> None:
        self.text = text
        self.rail = rail  # "└"（最新一条）或 "│"
        self.style = style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        avail = max(10, options.max_width - _PREFIX_WIDTH - _RIGHT_PAD)
        wrapped = Text(self.text, style=self.style).wrap(console, avail)
        kept = list(wrapped[:_PROMPT_MAX_LINES])
        if len(wrapped) > _PROMPT_MAX_LINES and kept:
            last = kept[-1]
            last.rstrip()
            last.truncate(avail - 1)
            last.append("…", self.style or None)
        for i, body in enumerate(kept):
            out = Text("  ", no_wrap=True)
            if i == 0:
                out.append(f"{self.rail} ", style=_S.dim)
            else:
                out.append("│ " if self.rail == "│" else "  ", style=_S.dim)
            out.append_text(body)
            yield out


def render_sidebar(sessions: list[LiveSession]) -> Group:
    lines: list[Text | _PromptText] = []
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
        # 会话头行一律可点（Textual 派发到 App.action_jump_to；--once 纯 Rich 路径 meta 无害）。
        # 必须带 app. 命名空间前缀——meta 点击的默认派发目标是被点的 Static，不带前缀会静默失败。
        # 无终端定位的会话点击后由 action 弹 toast 说明，好过无声无息。
        head.apply_meta({"@click": f"app.jump_to('{s.session_id}')"})
        lines.append(head)
        for i, p in enumerate(s.prompts):
            newest = i == len(s.prompts) - 1
            lines.append(_PromptText(
                text=_one_line(p.text),
                rail="└" if newest else "│",
                style="" if newest else _S.dim,
            ))
    return Group(*lines)
