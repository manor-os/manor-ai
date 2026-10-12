"""V3 typed LLM Steps use the same resumable text-only loop as Chat."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from packages.core.workers import internal


@pytest.mark.asyncio
async def test_v3_llm_step_routes_to_checkpointed_text_loop(monkeypatch):
    resumed = AsyncMock(return_value={"result": {"text": "done"}})
    one_shot = AsyncMock(side_effect=AssertionError("V3 cannot use one-shot LLM"))
    monkeypatch.setattr(internal, "_exec_subagent", resumed)
    monkeypatch.setattr(internal, "_exec_llm", one_shot)

    result = await internal._execute_by_kind({
        "kind": "llm",
        "params": {"prompt": "Write"},
        "task_coordinator_version": 3,
    })
    assert result == {"result": {"text": "done"}}
    resumed.assert_awaited_once()
    one_shot.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_llm_step_keeps_its_existing_route(monkeypatch):
    resumed = AsyncMock(side_effect=AssertionError("Legacy route changed"))
    one_shot = AsyncMock(return_value={"result": {"text": "legacy"}})
    monkeypatch.setattr(internal, "_exec_subagent", resumed)
    monkeypatch.setitem(internal._KIND_HANDLERS, "llm", one_shot)

    result = await internal._execute_by_kind({
        "kind": "llm",
        "params": {"prompt": "Write"},
    })
    assert result == {"result": {"text": "legacy"}}
    one_shot.assert_awaited_once()
    resumed.assert_not_awaited()
