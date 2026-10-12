"""Initiative management/manual runs are separate from autonomous admission."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services import agent_initiative_decision as decision
from packages.core.services import agent_initiative_service as service


def _rows_db(*rows):
    results = [SimpleNamespace(scalar_one_or_none=lambda row=row: row) for row in rows]
    return SimpleNamespace(execute=AsyncMock(side_effect=results))


@pytest.mark.asyncio
@pytest.mark.parametrize("manual,deleted", [(False, False), (True, False), (True, True)])
async def test_paused_initiative_workspace_requires_manual_runtime(manual, deleted, monkeypatch):
    initiative = SimpleNamespace(
        agent_subscription_id="sub", owner_user_id="user", entity_id="entity", workspace_id="ws",
    )
    subscription = SimpleNamespace(id="sub", agent_id="manor-master", workspace_id="ws")
    owner = SimpleNamespace(role="owner")
    conversation = SimpleNamespace(id="conv", workspace_id="ws", scope="workspace_main")
    workspace = SimpleNamespace(status="paused", deleted_at=datetime.now(UTC) if deleted else None)
    db = _rows_db(subscription, owner, conversation, workspace)
    monkeypatch.setattr(decision, "resolve_effective_user_role_name", AsyncMock(return_value="owner"))
    monkeypatch.setattr(decision, "user_can_read_workspace_id", AsyncMock(return_value=True))

    call = decision._exact_runtime_authority(
        db, initiative=initiative, subscription_id="sub", owner_user_id="user",
        agent_id="manor-master", conversation_id="conv", allow_paused_workspace=manual,
    )
    if manual and not deleted:
        assert await call == (subscription, owner, conversation)
    else:
        with pytest.raises(decision.InitiativeAdmissionRejected, match="initiative_workspace_inactive"):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize("writable", [True, False])
async def test_paused_workspace_initiative_management_keeps_write_acl(monkeypatch, writable):
    subscription = SimpleNamespace(id="sub", agent_id="manor-master", workspace_id="ws")
    workspace = SimpleNamespace(status="paused", deleted_at=None)
    db = _rows_db(subscription, workspace)
    monkeypatch.setattr(service, "user_can_read_workspace_id", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "user_can_write_workspace_id", AsyncMock(return_value=writable))

    call = service._subscription_access(
        db, subscription_id="sub", entity_id="entity", user_id="user", user_role="member", manage=True,
    )
    if writable:
        assert (await call).can_manage
    else:
        with pytest.raises(service.AgentInitiativeServiceError, match="Workspace write access"):
            await call
