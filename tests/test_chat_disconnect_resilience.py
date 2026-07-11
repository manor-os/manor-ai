"""When a chat client disconnects after a tool/skill has started, the background
agentic-loop task must be detached (kept alive to completion + persist) rather
than cancelled — so a long-running skill is not thrown away on navigate-away.

The full SSE-disconnect path needs the whole runtime; here we cover the new
primitive that makes detaching safe: a detached task is held by a strong
reference until it finishes (so it is not garbage-collected mid-run) and is
cleaned up afterwards.
"""
import asyncio

import pytest

from packages.core.services import chat_service


@pytest.mark.asyncio
async def test_detached_chat_turn_survives_until_done_then_cleans_up():
    chat_service._DETACHED_CHAT_TURNS.clear()
    started = asyncio.Event()
    finished = {"ok": False}

    async def _work():
        started.set()
        await asyncio.sleep(0.05)
        finished["ok"] = True
        return "saved"

    task = asyncio.create_task(_work())
    chat_service._detach_chat_turn(task)

    await started.wait()
    # While the detached loop is still running, a strong reference is held so it
    # cannot be garbage-collected (which would silently kill the skill).
    assert task in chat_service._DETACHED_CHAT_TURNS

    result = await task
    assert result == "saved" and finished["ok"]

    # The done-callback removes it from the registry (no leak).
    await asyncio.sleep(0)
    assert task not in chat_service._DETACHED_CHAT_TURNS


@pytest.mark.asyncio
async def test_detached_chat_turn_not_cancelled_by_detach():
    # Detaching must NOT cancel the task — that is the whole point.
    chat_service._DETACHED_CHAT_TURNS.clear()

    async def _work():
        await asyncio.sleep(0.02)
        return 42

    task = asyncio.create_task(_work())
    chat_service._detach_chat_turn(task)
    result = await task
    assert not task.cancelled()
    assert result == 42
