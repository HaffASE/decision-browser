"""finish() must surface post-terminal task failures on the public observer seam."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from decision_browser.runner import RunManager

_Finish = Callable[["RunManager", str], Coroutine[Any, Any, object]]


def _load_finish() -> _Finish:
    path = Path(__file__).resolve().parents[1] / "integration" / "test_runner.py"
    spec = importlib.util.spec_from_file_location("_finish_observer_helper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load finish helper from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    finish = module.finish
    return cast("_Finish", finish)


_finish = _load_finish()


class _Run:
    def __init__(self, task: asyncio.Task[object]) -> None:
        self.task = task


class _TaskView:
    def __init__(self, terminal: str, errors: tuple[BaseException, ...]) -> None:
        self.terminal = terminal
        self.errors = errors


class _FakeManager:
    def __init__(self, task: asyncio.Task[object], view: _TaskView) -> None:
        self._run: _Run | None = _Run(task)
        self._view = view
        self.last_run_id: str | None = None

    def get_task(self, run_id: str) -> _TaskView:
        self.last_run_id = run_id
        return self._view


def _as_manager(manager: _FakeManager) -> RunManager:
    return cast("RunManager", manager)


async def _raise_after_yield(exc: BaseException) -> None:
    await asyncio.sleep(0)
    raise exc


async def _return_after_yield() -> str:
    await asyncio.sleep(0)
    return "completed"


async def _block() -> None:
    await asyncio.Future()


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("post-terminal"), AssertionError("post-terminal")],
)
def test_post_terminal_task_error_propagates(exc: BaseException) -> None:
    asyncio.run(_expect_task_error(exc))


async def _expect_task_error(exc: BaseException) -> None:
    task: asyncio.Task[object] = asyncio.create_task(_raise_after_yield(exc))
    view = _TaskView("failed", (exc,))
    manager = _FakeManager(task, view)
    with pytest.raises(type(exc), match="post-terminal") as raised:
        await _finish(_as_manager(manager), "run-1")
    assert raised.value is exc
    assert manager.last_run_id == "run-1"
    assert task.done()
    assert not task.cancelled()


def test_completed_task_returns_same_get_task_view() -> None:
    asyncio.run(_expect_same_view())


async def _expect_same_view() -> None:
    task: asyncio.Task[object] = asyncio.create_task(_return_after_yield())
    view = _TaskView("completed", ())
    manager = _FakeManager(task, view)
    result = await _finish(_as_manager(manager), "run-1")
    assert result is view
    assert result.terminal == "completed"
    assert result.errors == ()
    assert manager.last_run_id == "run-1"
    assert task.result() == "completed"


def test_caller_cancel_while_open_cancels_underlying_task() -> None:
    asyncio.run(_expect_cancel_while_open())


async def _expect_cancel_while_open() -> None:
    task: asyncio.Task[object] = asyncio.create_task(_block())
    view = _TaskView("running", ())
    manager = _FakeManager(task, view)
    waiter = asyncio.create_task(_finish(_as_manager(manager), "run-1"))
    await asyncio.sleep(0)
    assert not waiter.done()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert task.cancelled()
    assert manager.last_run_id == "run-1"
