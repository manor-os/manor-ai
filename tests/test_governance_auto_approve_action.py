"""Retired standing-write helpers cannot bypass the versioned policy service."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.governance.service as gov


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["add_auto_approve_action", "add_auto_approve_capability",
                                    "remove_auto_approve_action", "remove_auto_approve_capability"])
@pytest.mark.parametrize("workspace_id", ["ws", ""])
async def test_retired_write_rejects_before_read_or_mutation(monkeypatch, helper, workspace_id):
    read, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(gov, "get_policy", read)
    monkeypatch.setattr(gov, "update_policy", write)
    with pytest.raises(ValueError, match="Retired approval write"):
        await getattr(gov, helper)(None, entity_id="entity", workspace_id=workspace_id,
                                  action_key="workspace.file.modify", changed_by="user")
    read.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_governance_card_does_not_offer_unversioned_standing_approval(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr("packages.core.workspace_chat.service.post_message", post)
    await gov.post_hitl_card(
        db=SimpleNamespace(flush=AsyncMock()), entity_id="entity", workspace_id="ws",
        plan_id="plan", step_id="step", step_key="generate_pdf", kind="subagent",
        action_key=None, capability_id="file.write", matched_rule="file-review",
        reason="Review file changes",
    )
    pending = post.await_args.kwargs["pending_action"]
    assert pending["action"] is None
    assert pending["capability_id"] == "file.write"
    assert pending["options"] == ["approve", "reject"]
