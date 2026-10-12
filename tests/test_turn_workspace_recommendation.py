"""The Workspace recommendation is classified beside the turn and attached at its end."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.services import chat_intent_routing
from packages.core.services.chat_service import (
    DEFERRED_WORKSPACE_RECOMMENDATION_WAIT_SECONDS,
    _TurnWorkspaceRecommendation,
)

_RECOMMENDATION = {
    "action": "create_new",
    "reason": "Recurring launch work needs durable state.",
    "request": "Run the launch every week",
    "workspace_id": None,
    "workspace_name": None,
}


def _ctx(metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(runtime_envelope=SimpleNamespace(metadata=metadata))


@pytest.mark.asyncio
async def test_deferred_recommendation_is_read_once_with_a_short_wait(monkeypatch):
    waits: list[tuple[str, float]] = []

    async def fake_wait(key, *, timeout):
        waits.append((key, timeout))
        return {"workspace_recommendation": dict(_RECOMMENDATION)}

    monkeypatch.setattr(chat_intent_routing, "wait_for_deferred_chat_intent", fake_wait)
    recommendation = _TurnWorkspaceRecommendation(_ctx({"deferred_chat_intent_key": "intent-1"}))

    assert await recommendation.get() == _RECOMMENDATION
    assert await recommendation.get() == _RECOMMENDATION  # assistant meta and stream end
    assert waits == [("intent-1", DEFERRED_WORKSPACE_RECOMMENDATION_WAIT_SECONDS)]


@pytest.mark.asyncio
async def test_missing_or_invalid_deferred_result_attaches_nothing(monkeypatch):
    async def fake_wait(key, *, timeout):
        return {"workspace_recommendation": {"action": "create_new", "reason": ""}}

    monkeypatch.setattr(chat_intent_routing, "wait_for_deferred_chat_intent", fake_wait)

    assert await _TurnWorkspaceRecommendation(
        _ctx({"deferred_chat_intent_key": "intent-1"})
    ).get() is None


@pytest.mark.asyncio
async def test_turns_without_deferred_intent_never_wait(monkeypatch):
    async def unexpected_wait(key, *, timeout):
        raise AssertionError("no deferred classification to wait for")

    monkeypatch.setattr(chat_intent_routing, "wait_for_deferred_chat_intent", unexpected_wait)

    assert await _TurnWorkspaceRecommendation(_ctx({})).get() is None
    # Runs queued before this change carry the recommendation in metadata.
    assert await _TurnWorkspaceRecommendation(_ctx({
        "workspace_recommendation": dict(_RECOMMENDATION),
        "deferred_chat_intent_key": "ignored",
    })).get() == _RECOMMENDATION
