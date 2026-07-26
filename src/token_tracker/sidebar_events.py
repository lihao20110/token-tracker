"""自动分屏 sidebar 的 UserPromptSubmit 本地事件通道。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_EVENT_BYTES = 4 * 1024 * 1024
_AGENT_IDS = {"claude-code", "codex"}
_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_-]*(:[A-Za-z0-9_-]+)?(\s|$)")


@dataclass(frozen=True)
class PromptEvent:
    session_id: str
    prompt: str
    agent_id: str
    cwd: str = ""
    model: str = ""
    turn_id: str = ""


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def prompt_fifo_path(session_id: str, channel_dir: str | os.PathLike[str]) -> str:
    """生成按本机用户和会话隔离的 FIFO 路径。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
    uid = getattr(os, "getuid", lambda: 0)()
    return str(Path(channel_dir) / f".tt-sidebar-{uid}-{digest}.fifo")


def prompt_socket_port(session_id: str) -> int:
    """Windows fallback: a deterministic loopback port scoped to a session."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return 49152 + int.from_bytes(digest[:2], "big") % 16384


def prompt_event_from_hook(data: Any, agent_id: str) -> PromptEvent | None:
    """把 CC/Codex UserPromptSubmit stdin 规范化为 sidebar 事件。"""
    if not isinstance(data, dict) or agent_id not in _AGENT_IDS:
        return None
    if data.get("hook_event_name") != "UserPromptSubmit":
        return None
    session_id = data.get("session_id")
    prompt = data.get("prompt")
    if not isinstance(session_id, str) or not session_id.strip() or not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    if not prompt:
        return None
    # CC 的 slash command 本来就不会进入 transcript 提示词列表；事件流保持同一口径。
    if agent_id == "claude-code" and _SLASH_COMMAND_RE.match(prompt):
        return None
    return PromptEvent(
        session_id=session_id.strip(),
        prompt=prompt,
        agent_id=agent_id,
        cwd=_string(data, "cwd"),
        model=_string(data, "model"),
        # Codex 使用 turn_id；Claude Code 的同类稳定标识叫 prompt_id。
        turn_id=_string(data, "turn_id") or _string(data, "prompt_id"),
    )


def encode_prompt_event(event: PromptEvent) -> bytes:
    return json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_prompt_event(raw: bytes, expected_session_id: str) -> PromptEvent | None:
    if not raw or len(raw) > MAX_EVENT_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    event = PromptEvent(
        session_id=_string(data, "session_id"),
        prompt=_string(data, "prompt"),
        agent_id=_string(data, "agent_id"),
        cwd=_string(data, "cwd"),
        model=_string(data, "model"),
        turn_id=_string(data, "turn_id"),
    )
    if event.session_id != expected_session_id or event.agent_id not in _AGENT_IDS or not event.prompt:
        return None
    return event


def send_prompt_event(
    event: PromptEvent,
    timeout: float = 0.2,
    channel_dir: str | os.PathLike[str] = ".",
) -> bool:
    """Best-effort local delivery; never creates a listener or persists a prompt."""
    payload = encode_prompt_event(event)
    if len(payload) > MAX_EVENT_BYTES:
        return False
    frame = len(payload).to_bytes(4, "big") + payload
    if os.name == "nt":
        try:
            with socket.create_connection(("127.0.0.1", prompt_socket_port(event.session_id)), timeout=timeout) as conn:
                conn.sendall(frame)
            return True
        except OSError:
            return False
    fd: int | None = None
    try:
        fd = os.open(prompt_fifo_path(event.session_id, channel_dir), os.O_WRONLY | os.O_NONBLOCK)  # type: ignore[attr-defined]
        sent = 0
        deadline = time.monotonic() + timeout
        while sent < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                return False
            sent += os.write(fd, frame[sent:])
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
    return True
