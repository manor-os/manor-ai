"""Background maintenance guards accept only an explicit manual override."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["briefing", "evaluation", "insights"])
@pytest.mark.parametrize("manual", [False, True])
async def test_paused_maintenance_service_keeps_autonomous_guard(kind, manual, monkeypatch):
    from packages.core.briefing import service as briefing
    from packages.core.memory import chat_extractor
    from packages.core.strategist import evaluation

    class AdmissionReached(Exception):
        pass

    workspace = SimpleNamespace(
        id="ws", entity_id="entity", status="paused", deleted_at=None, settings={},
    )
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: workspace),
        AdmissionReached(),
    ]))
    if kind == "briefing":
        monkeypatch.setattr(briefing.inbox_mod, "supported_sources", lambda: [])
        monkeypatch.setattr(briefing, "is_sandbox_workspace", lambda _workspace: False)
        monkeypatch.setattr(briefing, "_gather_workspace_daily_summary", AsyncMock(side_effect=AdmissionReached))
        call = briefing.generate_briefing
    elif kind == "evaluation":
        call = evaluation.evaluate_workspace_outcomes
    else:
        monkeypatch.setattr(chat_extractor, "_fetch_operator_messages", AsyncMock(side_effect=AdmissionReached))
        call = chat_extractor.extract_chat_insights

    if manual:
        with pytest.raises(AdmissionReached):
            await call(db, "ws", require_autonomous_runtime=False)
    else:
        result = await call(db, "ws")
        assert result["skipped"] is True
        assert result["reason"] == "workspace_paused"
