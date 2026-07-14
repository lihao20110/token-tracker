"""tt sidebar 的 Textual 壳：备用屏常驻 + 滚动 + 鼠标选择自动复制。

数据复用 `sidebar.scan_sessions`；默认总览走 `ui.sidebar.render_sidebar`，自动 1/3 分屏走
`render_split_sidebar`。Rich renderable 直接塞进 Static 整帧更新，滚动位置由
VerticalScroll 容器保持（滚轮 / 方向键 / PgUp/PgDn）。普通总览按 5 秒扫描数据、0.5 秒
刷新动画；自动分屏启动时只扫描一次历史，随后优先接 UserPromptSubmit FIFO，并增量 tail
当前会话 transcript 兜底，兼容 hook 未信任或会话早于 hook 创建的情况。
Rich Group 由 _SidebarBody 补 Textual 选择偏移与文本提取；split 选区逐行排除树线、序号与
悬挂缩进，只高亮并复制正文；拖拽时暂停整帧更新、松手自动复制。
本地分屏运行器可传 `initial_sessions` 复用预扫描首帧，compose 不再重复冷扫描。
配色继承终端：`ansi_color=True` + `ansi-dark/light` 主题让 app chrome（背景/滚动条/Footer）
走终端 ANSI 调色板、不糊 Textual 自己的深色底；正文内容色仍由 tt 主题（`_S` 运行时代理）给，
`tt sidebar --theme <名>` 可临时切。明暗跟随 tt 主题的 is_light。
本模块 import textual，cli 只在 live 模式延迟 import（照 questionary 先例，日常 tt 启动不加载）。
点击跳转依赖 statusline 携带 ITERM_SESSION_ID / TMUX_PANE 映射；tmux 与 iTerm2 已支持。
"""

import os
import select
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from rich.console import Group
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Footer, Static

from .. import config
from ..i18n import t
from ..sidebar import (
    RUNNING,
    LiveSession,
    Prompt,
    find_session_transcript,
    registry_update_hint,
    scan_sessions,
    terminal_info,
    transcript_line_prompt_event,
    transcript_line_turn_id,
)
from ..sidebar_events import MAX_EVENT_BYTES, PromptEvent, decode_prompt_event, prompt_fifo_path
from .sidebar import SPLIT_PROMPT_LIMIT, render_sidebar, render_split_sidebar
from .themes import get_theme

REFRESH_SECONDS = 5.0
SPINNER_SECONDS = 0.5  # 动画/时钟帧间隔——只用缓存会话重绘，不触发磁盘扫描
TRANSCRIPT_WATCH_SECONDS = 0.2  # 只 stat 当前会话文件；增长时才读取追加 JSONL
TRANSCRIPT_RETRY_SECONDS = 0.5  # 新会话 transcript 尚未创建时的定位重试间隔
_TARGET_GONE_MARKER = "tt_jump_target_gone"  # AppleScript 未匹配到目标窗格的信号（tab 已关闭）


def _jump_argvs(info: dict) -> list[list[str]] | None:
    """终端定位 → 跳转命令序列；无可用定位返回 None。

    tmux 优先（零授权、pane 级精确）：select-window 接受 pane 目标、会选中其所在 window。
    iTerm2 走 AppleScript：ITERM_SESSION_ID 形如 "w0t3p0:<UUID>"，session 的 AppleScript
    `id` 即冒号后的 UUID；逐层匹配后选中 tab/session 并把窗口带到最前（首次触发 macOS
    自动化授权弹窗属预期）。
    """
    if info.get("tmux"):
        pane = info["tmux"]
        return [["tmux", "select-window", "-t", pane], ["tmux", "select-pane", "-t", pane]]
    if info.get("iterm"):
        uuid = info["iterm"].rpartition(":")[2]
        # 找不到目标 session 时必须以 error 结束——否则 osascript 静默 rc=0、
        # 用户看到的是「点了没反应」（目标 tab 已关闭是常见场景）
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with tb in tabs of w
                    repeat with s in sessions of tb
                        if id of s is "{uuid}" then
                            tell w to select tb
                            select s
                            set index of w to 1
                            activate
                            return
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        error "{_TARGET_GONE_MARKER}"'''
        return [["osascript", "-e", script]]
    return None


class _SidebarBody(Static):
    """会话面板：链接（可跳转的会话头行）统一蓝色下划线语义。

    链接视觉定版（与主人多轮收敛）：静止=项目名蓝字蓝下划线（渲染层写死）；
    hover=整行蓝字+蓝下划线——Rich/Textual 均无独立下划线色通道（SGR 58 零支持，
    已核实源码），下划线颜色物理绑定字色，「整行统一蓝下划线」唯一实现就是整行变蓝。
    悬停高亮的连续性依赖渲染层 _click_style 的 link_id 跨帧稳定（曾因漂移断续）。
    """

    @property
    def link_style(self) -> RichStyle:
        return RichStyle()

    @property
    def link_style_hover(self) -> RichStyle:
        return RichStyle(color=get_theme(config.resolve_theme())["base"]["blue"], underline=True)

    def _split_prefix_width(self) -> int:
        """从已渲染首行读取 `├ 52. ` 的宽度；拖拽延迟刷新时不能改用新数据条数。"""
        app = self.app
        if not isinstance(app, SidebarApp) or app._variant != "split" or not self._render_cache.lines:
            return 0
        first = self._render_cache.lines[0].text
        if not first.startswith(("├ ", "└ ")):
            return 0
        number_end = first.find(". ", 2)
        if number_end < 0 or not first[2:number_end].strip().isdigit():
            return 0
        return number_end + 2

    @staticmethod
    def _split_body_span(line: str, span: tuple[int, int], prefix_width: int) -> tuple[int, int] | None:
        """把 split 选区夹到正文列；纯树线间隔行不参与高亮与复制。"""
        if line.rstrip() in ("", "│"):
            return None
        start, end = span
        if end != -1 and end <= prefix_width:
            return None
        return max(start, prefix_width), end

    def _split_selection(self, selection: Selection, lines: list[str], prefix_width: int) -> str:
        """提取 split 正文，移除每个视觉行的树线、序号与悬挂缩进。"""
        chunks: list[str] = []
        for y, line in enumerate(lines):
            span = selection.get_span(y)
            if span is None:
                continue
            if line in ("", "│"):
                chunks.append("")  # 条目间树线保留为空行，不进入剪贴板
                continue
            body_span = self._split_body_span(line, span, prefix_width)
            if body_span is None:
                continue
            start, end = body_span
            chunks.append(line[start:] if end == -1 else line[start:end])
        return "\n".join(chunks).strip("\n")

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """从已渲染行提取选区；split 去结构前缀，默认总览保持所见即所得。"""
        if self._dirty_regions:
            self._render_content()
        lines = [line.text.rstrip() for line in self._render_cache.lines]
        prefix_width = self._split_prefix_width()
        if prefix_width:
            return self._split_selection(selection, lines, prefix_width), "\n"
        return selection.extract("\n".join(lines)), "\n"

    def render_line(self, y: int) -> Strip:
        """给 Rich Group 补选择偏移，并在选区上叠加 Textual 高亮样式。

        Textual 自带 Strip.apply_offsets() 会给每个 Segment 新建 offset Style，并把
        新 Style 放在合并右侧；Rich 因此用 offset 的 link_id 覆盖整行共用的点击
        link_id，hover 只能命中鼠标所在的一段。这里反向合并，让点击 link_id 保持
        统一，同时仍给每段写入准确的选择坐标。
        """
        line = super().render_line(y)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            line_text = Text()
            for segment in line:
                if not segment.control:
                    line_text.append(segment.text, segment.style)
            prefix_width = self._split_prefix_width()
            if prefix_width:
                span = self._split_body_span(line_text.plain, span, prefix_width)
            if span is not None:
                start, end = span
                line_text.stylize(
                    self.screen.get_component_rich_style("screen--selection"),
                    start,
                    None if end == -1 else end,
                )
            line = Strip(line_text.render(self.app.console), line.cell_length)
        offset_x = 0
        segments: list[Segment] = []
        for segment in line:
            offset_style = RichStyle.from_meta({"offset": (offset_x, y)})
            style = offset_style + segment.style if segment.style else offset_style
            segments.append(Segment(segment.text, style, segment.control))
            offset_x += len(segment.text)
        return Strip(segments, line.cell_length)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        app = self.app
        if event.button == 1 and isinstance(app, SidebarApp):
            app._text_dragging = True


class SidebarApp(App[None]):
    BINDINGS = [
        ("q", "quit", "退出"),
        ("escape", "quit", "退出"),
        # Textual 部分版本默认把 ctrl+c 让给剪贴板，显式绑回退出、与终端习惯一致
        ("ctrl+c", "quit", "退出"),
    ]

    CSS = """
    /* 滚动只发生在 VerticalScroll 一层；Screen 禁滚，杜绝 resize 瞬间冒出第二条
       （Screen 默认滚动条宽 2 格）叠在容器滚动条旁边 */
    Screen { overflow-x: hidden; overflow-y: hidden; }
    /* 滚动条隐藏（主人定）：滚轮/方向键/PgUp 照常滚，只是不画位置指示条 */
    VerticalScroll { scrollbar-size: 0 0; }
    #sidebar-body { width: 1fr; }
    """

    def __init__(self, agent_ids: set[str] | None = None,
                 variant: Literal["default", "split"] = "default",
                 initial_sessions: list[LiveSession] | None = None,
                 prompt_session_id: str | None = None,
                 prompt_channel_dir: str | None = None,
                 prompt_transcript_path: str | None = None,
                 prompt_agent_id: str | None = None) -> None:
        # ansi_color=True：不把 ANSI 色转成 Textual 主题色，背景用终端自身默认色
        super().__init__(ansi_color=True)
        self._agent_ids = agent_ids
        self._variant = variant
        self._sessions = list(initial_sessions) if initial_sessions is not None else []
        self._needs_initial_scan = initial_sessions is None
        self._update_hint = variant == "default" and registry_update_hint(self._sessions)
        self._frame = 0
        self._text_dragging = False
        self._body_update_pending = False
        self._prompt_session_id = prompt_session_id
        self._prompt_channel_dir = prompt_channel_dir
        self._prompt_transcript_path = prompt_transcript_path
        self._prompt_agent_id = prompt_agent_id
        self._prompt_fifo_fd: int | None = None
        self._prompt_fifo_path: str | None = None
        self._prompt_fifo_inode: int | None = None
        self._seen_prompt_turns: set[str] = {
            prompt.event_id
            for session in self._sessions
            for prompt in session.prompts
            if prompt.event_id
        }
        self._prompt_watch_stop = threading.Event()
        self._prompt_watch_thread: threading.Thread | None = None

    def _render_body(self) -> Group:
        if self._variant == "split":
            return render_split_sidebar(self._sessions)
        return render_sidebar(self._sessions, self._frame, update_hint=self._update_hint)

    def compose(self) -> ComposeResult:
        if self._needs_initial_scan:
            self._scan()
            self._needs_initial_scan = False
        with VerticalScroll():
            yield _SidebarBody(self._render_body(), id="sidebar-body")
        if self._variant == "default":
            yield Footer()

    def on_mount(self) -> None:
        # chrome 配色映射到终端 ANSI 调色板；明暗跟随 tt 主题
        self.theme = "ansi-light" if get_theme(config.resolve_theme()).get("is_light") else "ansi-dark"
        self.query_one(VerticalScroll).focus()  # 容器持焦点，方向键/PgUp/PgDn 直接滚
        if self._variant == "default":
            self.set_interval(REFRESH_SECONDS, self._refresh)
            self.set_interval(SPINNER_SECONDS, self._tick_spinner)
        elif self._prompt_session_id:
            self._start_prompt_listener()

    def on_unmount(self) -> None:
        self._stop_prompt_listener()

    def _start_prompt_listener(self) -> None:
        """split 专属：FIFO 为快路径，当前 transcript 增量 tail 为免信任兜底。"""
        assert self._prompt_session_id is not None
        self._prompt_watch_stop.clear()
        path = prompt_fifo_path(self._prompt_session_id, self._prompt_channel_dir or ".")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.mkfifo(path, 0o600)
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._prompt_fifo_fd = fd
        self._prompt_fifo_path = path
        self._prompt_fifo_inode = os.stat(path).st_ino
        threading.Thread(
            target=self._listen_prompt_events,
            args=(fd, self._prompt_session_id),
            name="tt-sidebar-prompts",
            daemon=True,
        ).start()
        self._prompt_watch_thread = threading.Thread(
            target=self._watch_prompt_transcript,
            args=(self._prompt_session_id,),
            name="tt-sidebar-transcript",
            daemon=True,
        )
        self._prompt_watch_thread.start()

    def _stop_prompt_listener(self) -> None:
        self._prompt_watch_stop.set()
        fd = self._prompt_fifo_fd
        self._prompt_fifo_fd = None
        if fd is not None:
            try:
                os.write(fd, b"\0\0\0\0")  # 唤醒阻塞中的 select，让监听线程退出
            except OSError:
                pass
            os.close(fd)
        path = self._prompt_fifo_path
        inode = self._prompt_fifo_inode
        self._prompt_fifo_path = None
        self._prompt_fifo_inode = None
        if path and inode is not None:
            try:
                if os.stat(path).st_ino == inode:
                    os.unlink(path)
            except FileNotFoundError:
                pass
        watch_thread = self._prompt_watch_thread
        self._prompt_watch_thread = None
        if watch_thread is not None and watch_thread is not threading.current_thread():
            watch_thread.join(timeout=TRANSCRIPT_RETRY_SECONDS + 0.1)

    def _listen_prompt_events(self, fd: int, session_id: str) -> None:
        buffer = bytearray()
        while self._prompt_fifo_fd == fd:
            try:
                readable, _, _ = select.select([fd], [], [])
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                buffer.extend(os.read(fd, 65536))
            except (BlockingIOError, OSError):
                continue
            while len(buffer) >= 4:
                size = int.from_bytes(buffer[:4], "big")
                if size <= 0 or size > MAX_EVENT_BYTES:
                    buffer.clear()
                    break
                if len(buffer) < size + 4:
                    break
                raw = bytes(buffer[4:size + 4])
                del buffer[:size + 4]
                event = decode_prompt_event(raw, session_id)
                if event is not None:
                    try:
                        self.call_from_thread(self._accept_prompt_event, event)
                    except RuntimeError:
                        return

    def _watch_prompt_transcript(self, session_id: str) -> None:
        """只 stat / tail 当前会话文件；发现新提示词后才触发一次权威扫描。

        旧 Codex 会话可能没有加载后来新增的 hook，且非 managed hook 在信任前必定
        被跳过。增量 tail 因此是永久 safety net，但不会恢复全目录定时扫描。
        """
        source: tuple[str, Path] | None = None
        inode: int | None = None
        offset = 0
        partial = b""
        pending_turn_id = ""

        while not self._prompt_watch_stop.is_set():
            if source is None:
                hinted_path = Path(self._prompt_transcript_path) if self._prompt_transcript_path else None
                if hinted_path is not None and hinted_path.is_file() and self._prompt_agent_id:
                    source = self._prompt_agent_id, hinted_path
                else:
                    source = find_session_transcript(session_id)
                if source is None:
                    self._prompt_watch_stop.wait(TRANSCRIPT_RETRY_SECONDS)
                    continue
                try:
                    stat = source[1].stat()
                except OSError:
                    source = None
                    continue
                inode = stat.st_ino
                offset = stat.st_size  # 启动前历史已由 initial scan 注入，只 tail 后续追加
                partial = b""
                pending_turn_id = ""
                # 覆盖 initial scan 与 watcher 绑定之间的窗口；缓存命中时只是一次轻量对账。
                try:
                    self.call_from_thread(self._refresh_split_from_transcript, source[0], ())
                except RuntimeError:
                    return

            if self._prompt_watch_stop.wait(TRANSCRIPT_WATCH_SECONDS):
                return
            agent_id, path = source
            try:
                stat = path.stat()
            except OSError:
                source = None
                continue
            if inode != stat.st_ino or stat.st_size < offset:
                # rollout rename / compact：从新文件开头重建一次，完整 scanner 会负责去重。
                inode = stat.st_ino
                offset = 0
                partial = b""
                pending_turn_id = ""
            if stat.st_size == offset:
                continue
            try:
                with path.open("rb") as transcript:
                    transcript.seek(offset)
                    chunk = transcript.read()
                    offset = transcript.tell()
            except OSError:
                source = None
                continue
            if not chunk:
                continue

            rows = (partial + chunk).split(b"\n")
            partial = rows.pop()
            prompt_found = False
            prompt_event_ids: set[str] = set()
            for row in rows:
                if not row:
                    continue
                pending_turn_id = transcript_line_turn_id(row, agent_id) or pending_turn_id
                is_prompt, prompt_event_id = transcript_line_prompt_event(row, agent_id, session_id)
                if is_prompt:
                    prompt_found = True
                    event_id = prompt_event_id or pending_turn_id
                    if event_id:
                        prompt_event_ids.add(event_id)
                    pending_turn_id = ""
            if prompt_found:
                try:
                    self.call_from_thread(
                        self._refresh_split_from_transcript,
                        agent_id,
                        tuple(prompt_event_ids),
                    )
                except RuntimeError:
                    return

    def _refresh_split_from_transcript(self, agent_id: str, event_ids: tuple[str, ...] = ()) -> None:
        """transcript 命中后读取一次完整快照；与 FIFO 双通道天然收敛为单份历史。"""
        if self._variant != "split" or not self._prompt_session_id:
            return
        sessions = scan_sessions(
            agent_ids={agent_id},
            max_prompts=SPLIT_PROMPT_LIMIT,
            max_sessions=1000,
        )
        current = next(
            (session for session in sessions if session.session_id == self._prompt_session_id),
            None,
        )
        if current is None:
            return
        self._seen_prompt_turns.update(event_ids)
        self._seen_prompt_turns.update(
            prompt.event_id for prompt in current.prompts if prompt.event_id
        )
        if self._sessions == [current]:
            return
        self._sessions = [current]
        self._update_body()

    def _accept_prompt_event(self, event: PromptEvent) -> None:
        if self._variant != "split" or event.session_id != self._prompt_session_id:
            return
        if event.turn_id and event.turn_id in self._seen_prompt_turns:
            return
        if event.turn_id:
            self._seen_prompt_turns.add(event.turn_id)
        now = datetime.now(UTC)
        current = next((session for session in self._sessions if session.session_id == event.session_id), None)
        prompt = Prompt(event.prompt, now, event_id=event.turn_id)
        if current is None:
            current = LiveSession(
                agent_id=event.agent_id,
                session_id=event.session_id,
                project=Path(event.cwd).name or "unknown",
                last_activity=now,
                state=RUNNING,
                prompts=[prompt],
                model=event.model,
            )
        else:
            current = replace(
                current,
                last_activity=now,
                state=RUNNING,
                prompts=[*current.prompts, prompt],
                model=event.model or current.model,
            )
        self._sessions = [current]
        self._update_body()

    def _scan(self) -> bool:
        previous = self._sessions
        previous_hint = self._update_hint
        if self._variant == "split":
            sessions = scan_sessions(
                agent_ids=self._agent_ids,
                max_prompts=SPLIT_PROMPT_LIMIT,
                max_sessions=1000,
            )
            if self._prompt_session_id:
                sessions = [
                    session for session in sessions
                    if session.session_id == self._prompt_session_id
                ]
            self._sessions = sessions
        else:
            self._sessions = scan_sessions(agent_ids=self._agent_ids)
        self._update_hint = self._variant == "default" and registry_update_hint(self._sessions)
        return self._sessions != previous or self._update_hint != previous_hint

    @staticmethod
    def _restore_split_scroll(scroll: VerticalScroll, old_y: float, old_height: int) -> None:
        """顶部插入新提示词后补偿新增行高，让历史阅读位置保持在同一内容。"""
        height_delta = max(0, scroll.virtual_size.height - old_height)
        scroll.scroll_to(y=old_y + height_delta, animate=False, force=True, immediate=True)

    def _update_body(self) -> None:
        if self._text_dragging:
            self._body_update_pending = True
            return
        scroll = self.query_one(VerticalScroll)
        old_y = scroll.scroll_y
        old_height = scroll.virtual_size.height
        self.query_one("#sidebar-body", Static).update(self._render_body())
        if self._variant == "split" and old_y > 0:
            self.call_after_refresh(self._restore_split_scroll, scroll, old_y, old_height)
        self._body_update_pending = False

    def _refresh(self) -> None:
        if self._variant == "split":
            return
        if self._scan() or self._body_update_pending:
            self._update_body()

    def _tick_spinner(self) -> None:
        """动画/时钟帧：0.5s 重绘一次（纯内存渲染，5s 的磁盘扫描节奏不变）——
        运行中星形轮转 + 头部三时区时钟的秒针都靠它走。"""
        if self._variant == "split":
            return
        self._frame += 1
        self._update_body()

    def on_text_selected(self, event: events.TextSelected) -> None:
        """松开鼠标即复制；拖拽期间跳过的最新一帧在复制后补上。"""
        self._text_dragging = False
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            if sys.platform == "darwin":
                try:
                    subprocess.run(
                        ["/usr/bin/pbcopy"], input=selected, text=True, timeout=1, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass  # OSC 52 仍作为兜底；自动复制保持静默
        if self._body_update_pending:
            self._update_body()

    def action_jump_to(self, session_id: str) -> None:
        """点击会话头行触发：跳转焦点到该会话所在的终端窗格。

        定位现读 STATUS_FILE（比渲染时快照新）；命令跑 worker 线程——osascript 首次
        触发 macOS 自动化授权弹窗时会阻塞，不能卡住 UI 事件循环。
        """
        info = terminal_info(session_id)
        argvs = _jump_argvs(info)
        if argvs is None:
            self.notify(t("sidebar_jump_no_target"), severity="warning", timeout=4)
            return
        self.run_worker(lambda: self._run_jump(argvs), thread=True)

    def _run_jump(self, argvs: list[list[str]]) -> None:
        for argv in argvs:
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as e:
                self.call_from_thread(self.notify, t("sidebar_jump_failed", err=str(e)[:80]),
                                      severity="error", timeout=4)
                return
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                if _TARGET_GONE_MARKER in stderr:  # 映射还在、窗格已关（换成友好话术）
                    self.call_from_thread(self.notify, t("sidebar_jump_gone"),
                                          severity="warning", timeout=4)
                    return
                err = stderr[:80] or f"exit {proc.returncode}"
                self.call_from_thread(self.notify, t("sidebar_jump_failed", err=err),
                                      severity="error", timeout=4)
                return
