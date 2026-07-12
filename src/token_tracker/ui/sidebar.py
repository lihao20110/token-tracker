"""tt sidebar 面板：活跃会话列表 + 各自最近提示词，为窄窗格（终端分屏 / tmux pane）常驻设计。

每会话一块：状态点 + 项目名 + agent/模型 + 距上次活动；下面缩进列最近提示词
（时间正序，最新一条常亮、更早的调暗）。头行单行截断不折行；提示词最多折
`_PROMPT_MAX_LINES` 行、超出末行加省略号、正文右侧固定留 `_RIGHT_PAD` 格空白。
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from rich.cells import cell_len, chop_cells
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.text import Text

from ..i18n import t
from ..sidebar import ATTENTION, IDLE, RUNNING, WAITING, LiveSession
from .format import AGENT_SHORT, _model_short
from .theme import _S

# 头部双时区时钟（主人常驻北京、CLI 伪装 TZ=洛杉矶，两个都显式标出、不受 TZ 环境变量影响）
_CLOCK_ZONES = (("sidebar_tz_bj", "Asia/Shanghai"), ("sidebar_tz_la", "America/Los_Angeles"))

_STATE_DOTS = {RUNNING: "●", ATTENTION: "●", WAITING: "●", IDLE: "○"}
# 运行中的动画帧（CC 同款星形轮转）；spinner_frame 由 Textual 壳的动画 tick 递增
_SPINNER = "✢✳✻✽"

_PROMPT_MAX_LINES = 2  # 每条提示词最多折 N 行，超出末行省略号
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
    """提示词压成单行（换行折叠成空格），粗剪到 limit 字符，精细折行/截断交给 _WrappedLine。"""
    flat = " ".join(text.split())
    return flat[:limit]


def _fold(text: str, width: int) -> list[str]:
    """CJK 感知折行：宽字符（中文等）逐字可折、填满换行；窄字符连成的英文单词是
    原子 token——放不下就整个挪下一行，仅当单词比整行还长（长 URL 等）才按格硬切。
    Rich 词折行会把空格后的整段中文当一个词（提前断行留白）、chop_cells 会拦腰切
    英文单词，两者都不合适，故自写。"""
    tokens: list[str] = []
    word = ""
    for ch in text:
        if ch.isspace():
            if word:
                tokens.append(word)
                word = ""
            tokens.append(" ")
        elif cell_len(ch) > 1:
            if word:
                tokens.append(word)
                word = ""
            tokens.append(ch)
        else:
            word += ch
    if word:
        tokens.append(word)

    lines: list[str] = []
    cur, cur_w = "", 0
    for tok in tokens:
        w = cell_len(tok)
        if tok == " ":
            if cur_w == 0 or cur_w + 1 > width:  # 行首/行尾空格丢弃
                continue
            cur, cur_w = cur + " ", cur_w + 1
        elif cur_w + w <= width:
            cur, cur_w = cur + tok, cur_w + w
        elif w <= width:  # 整个 token 挪下一行
            lines.append(cur.rstrip())
            cur, cur_w = tok, w
        else:  # 单 token 超过整行宽：按格硬切
            if cur_w:
                lines.append(cur.rstrip())
            *full, cur = chop_cells(tok, width)
            lines.extend(full)
            cur_w = cell_len(cur)
    if cur.rstrip() or not lines:
        lines.append(cur.rstrip())
    return lines


class _WrappedLine:
    """树内一段内容：渲染期按实际宽度折行，最多 max_lines 行、超出末行加省略号。

    first / cont 是首行与续行的**带样式前缀**（等宽）；cont=None 时用等宽空格悬挂对齐
    （「下一步」行的续行对齐到正文起始列）。提示词的树状语义由前缀表达：
    首行分支符（├，末条 └）——数分支即数提示词；续行 │ 延续轨道、末条续行留空。
    宽度在 __rich_console__ 里才拿得到，故做成 renderable 而非预构建 Text——
    Rich 快照（--once）与 Textual Static 两个渲染路径通用。
    """

    def __init__(self, text: str, first: Text, cont: Text | None,
                 style: str, max_lines: int) -> None:
        self.text = text
        self.first = first
        self.cont = cont
        self.style = style
        self.max_lines = max_lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        prefix_width = self.first.cell_len
        cont = self.cont if self.cont is not None else Text(" " * prefix_width)
        avail = max(10, options.max_width - prefix_width - _RIGHT_PAD)
        pieces = _fold(self.text, avail)
        kept = pieces[:self.max_lines]
        truncated = len(pieces) > self.max_lines
        for i, piece in enumerate(kept):
            body = Text(piece.rstrip(), style=self.style)
            if truncated and i == len(kept) - 1:
                body.truncate(avail - 1)
                body.append("…", self.style or None)
            out = Text(no_wrap=True)
            out.append_text(self.first if i == 0 else cont)
            out.append_text(body)
            yield out


def _clock_lines() -> list[Text]:
    """双时区时钟行：标签（等宽对齐）+ 月-日 周几 时:分:秒。时区数据缺失时静默跳过。"""
    weekdays = t("weekday_grid").split(",")
    rows: list[tuple[str, datetime]] = []
    for label_key, tz_name in _CLOCK_ZONES:
        try:
            rows.append((t(label_key), datetime.now(ZoneInfo(tz_name))))
        except Exception:  # noqa: BLE001 - 无 tzdata 的环境（如裸 Windows）跳过该行即可
            continue
    label_width = max((cell_len(lb) for lb, _ in rows), default=0)
    out: list[Text] = []
    for label, local in rows:
        ln = Text(no_wrap=True, overflow="ellipsis")
        ln.append(label, style=_S.blue)
        ln.append(" " * (label_width - cell_len(label) + 1))
        ln.append(local.strftime("%m-%d "), style=_S.dim)
        ln.append(weekdays[(local.weekday() + 1) % 7] + " ", style=_S.dim)
        ln.append(local.strftime("%H:%M:%S"))
        out.append(ln)
    return out


def render_sidebar(sessions: list[LiveSession], spinner_frame: int = 0) -> Group:
    lines: list[Text | _WrappedLine] = []
    header = Text(no_wrap=True, overflow="ellipsis")
    header.append("✳ tt sidebar", style=_S.accent)
    header.append(f" · {t('sidebar_active_count', n=len(sessions))}", style=_S.peach)
    lines.append(header)
    lines.extend(_clock_lines())

    if not sessions:
        lines.append(Text(""))
        lines.append(Text(t("sidebar_empty"), style=_S.dim))
        return Group(*lines)

    now = datetime.now(UTC)
    for s in sessions:
        lines.append(Text(""))
        head = Text(no_wrap=True, overflow="ellipsis")
        dot = (_SPINNER[spinner_frame % len(_SPINNER)] if s.state == RUNNING
               else _STATE_DOTS.get(s.state, "●"))
        head.append(dot + " ", style=_state_style(s.state))
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
            last = i == len(s.prompts) - 1
            lines.append(_WrappedLine(
                text=_one_line(p.text),
                first=Text("└ " if last else "├ ", style=_S.dim),
                cont=Text("  " if last else "│ ", style=_S.dim),
                style="" if last else _S.dim,
                max_lines=_PROMPT_MAX_LINES,
            ))
        if s.next_hint:
            # 逐行显示（行结构由数据层 _hint_text 压缩整理、上限 5 行）：
            # 一行原文 = 一行展示，超宽单行截断省略号；续行等宽空格悬挂、正文列对齐
            label = Text()
            label.append("  ↳ ", style=_S.dim)
            label.append(f"{t('sidebar_next')}: ", style=_S.peach)
            hang = Text(" " * label.cell_len)
            for i, hint_line in enumerate(s.next_hint.splitlines()):
                lines.append(_WrappedLine(
                    text=hint_line,
                    first=label if i == 0 else hang,
                    cont=None,
                    style=f"dim {_S.peach}",  # 与「下一步」标签同色系但 dim
                    max_lines=1,
                ))
    return Group(*lines)
