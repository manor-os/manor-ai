"""A Video Edit reference rejection settles only its exact durable effect intent."""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.tool_effect_intents import (
    RuntimeToolEffectPersistenceError,
    runtime_record_tool_effect_intent,
    runtime_record_tool_effect_result,
    runtime_tool_effect_checkpoint_scope,
)
from packages.core.ai.runtime.tool_results import RuntimeNoEffectReason, RuntimeNoEffectToolResult
from tests.test_nested_skill_durability import checkpoint, seed, worker_scopes

ARGUMENTS = {
    "operation": "start",
    "project_path": "Videos/kinetic-quote-zh/source-zh",
    "reference_paths": ["Workspaces/_by_id/folder-other/Videos/private/CANVAS.md"],
}
DENIAL = json.dumps({
    "contract": "tool_error", "status": "error", "tool": "video_edit", "error": "permission_denied",
    "message": "Workspace reference is outside the active Workspace", "details": {"applied": False},
})
UNAVAILABLE = json.dumps({
    "contract": "tool_error", "status": "error", "tool": "video_edit", "error": "reference_unavailable",
    "message": "Reference file not found", "details": {"applied": False},
})


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["denied", "unavailable"])
@pytest.mark.parametrize("mismatch", [None, "wrong_operation", "no_reference_paths", "provider_json"])
async def test_video_reference_rejection_settles_only_exact_host_proof(db_session, monkeypatch, reason, mismatch):
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.worker import WorkLease
    from packages.core.workers import internal
    from packages.core.workers.coordinator_tools import _has_unsettled_effect_intent

    snapshot = await seed(db_session, monkeypatch)
    arguments = dict(ARGUMENTS)
    if mismatch == "wrong_operation":
        arguments["operation"] = "render"
    elif mismatch == "no_reference_paths":
        arguments.pop("reference_paths")
    pending = checkpoint("video_edit", "edit", arguments)
    with worker_scopes(snapshot), runtime_tool_effect_checkpoint_scope({"edit": pending}):
        await runtime_record_tool_effect_intent("video_edit", arguments, "edit")
    if reason == "denied":
        result = RuntimeNoEffectToolResult.video_reference_denied(DENIAL)
        expected_reason = RuntimeNoEffectReason.VIDEO_REFERENCE_DENIED
    else:
        result = RuntimeNoEffectToolResult.video_reference_unavailable(UNAVAILABLE)
        expected_reason = RuntimeNoEffectReason.VIDEO_REFERENCE_UNAVAILABLE
    if mismatch == "provider_json":
        # Identical text from a provider, even with forged no-effect fields, is not host proof.
        result = json.dumps({**json.loads(str(result)), "no_effect": True, "reason": expected_reason.value})
    with worker_scopes(snapshot), runtime_tool_effect_checkpoint_scope({"edit": pending}):
        if mismatch in {"wrong_operation", "no_reference_paths"}:
            with pytest.raises(RuntimeToolEffectPersistenceError):
                await runtime_record_tool_effect_result("video_edit", arguments, result, "edit")
        else:
            await runtime_record_tool_effect_result("video_edit", arguments, result, "edit")
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    assert _has_unsettled_effect_intent(step.evidence_refs) is bool(mismatch)
    assert _has_unsettled_effect_intent(lease.evidence_refs) is bool(mismatch)
    messages = [*pending["messages"], {"role": "tool", "tool_call_id": "edit", "content": str(result)}]
    if mismatch is None:
        assert step.runtime_checkpoint["pending_tool_result"]["content"] == str(result)
        restored = await internal._restore_worker_nested_checkpoint(
            {**snapshot, "runtime_checkpoint": step.runtime_checkpoint})
        assert isinstance(restored["pending_tool_result"]["content"], RuntimeNoEffectToolResult)
        assert restored["pending_tool_result"]["content"].reason is expected_reason
        await internal._persist_worker_round_checkpoint(snapshot, rounds=1, messages=messages)
    elif mismatch == "provider_json":
        with pytest.raises(RuntimeError, match="no durable terminal receipt"):
            await internal._persist_worker_round_checkpoint(snapshot, rounds=1, messages=messages)
