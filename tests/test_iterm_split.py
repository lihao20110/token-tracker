from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("iterm2")

from token_tracker import iterm_split  # noqa: E402


def test_split_call_is_never_inside_transaction():
    source = Path(iterm_split.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        assert not any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "async_split_pane"
            for call in calls
        )


def test_no_fixed_async_sleep_in_launcher():
    source = Path(iterm_split.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
        and call.func.attr == "sleep"
        for call in calls
    )


def test_iterm_worker_does_not_retry_forever():
    source = Path(iterm_split.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    run_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Attribute) and call.func.attr == "run_until_complete"
    )
    retry = next(keyword.value for keyword in run_call.keywords if keyword.arg == "retry")
    assert isinstance(retry, ast.Constant)
    assert retry.value is False


def test_custom_command_waits_silently_for_final_width():
    profile = iterm_split._command_profile("exec sidebar", 40)
    command = profile.values["Command"]
    assert "COLUMNS <= _tt_limit" in command
    assert "_tt_limit=42" in command
    assert "exec sidebar" in command


async def test_timeout_names_failed_stage():
    async def never() -> None:
        await iterm_split.asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="创建右侧分屏超时"):
        await iterm_split._await_stage("创建右侧分屏", never(), 0.01)


async def test_finalize_layout_uses_transaction_after_split():
    events: list[str] = []

    class Transaction:
        def __init__(self, _connection) -> None:
            pass

        async def __aenter__(self) -> None:
            events.append("transaction:start")

        async def __aexit__(self, *_args) -> None:
            events.append("transaction:end")

    source = type("Source", (), {})()
    source.window = object()
    source.async_activate = AsyncMock(side_effect=lambda **_kwargs: events.append("focus"))
    sidebar = object()

    async def resize(_source, _sidebar) -> None:
        events.append("layout")

    async def restore(_window, _frame, _fullscreen) -> None:
        events.append("geometry")

    with (
        patch.object(iterm_split.iterm2, "Transaction", Transaction),
        patch.object(iterm_split, "_resize_one_third", resize),
        patch.object(iterm_split, "_restore_geometry", restore),
    ):
        await iterm_split._finalize_layout("connection", source, sidebar, (0, 0, 100, 100), False)

    assert events == ["transaction:start", "layout", "geometry", "focus", "transaction:end"]
