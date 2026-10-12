"""A missing local patch target is no write, never an ambiguous provider error."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.tool_effect_intents import (
    RuntimeToolEffectPersistenceError,
    runtime_record_tool_effect_intent,
    runtime_record_tool_effect_result,
    runtime_tool_effect_checkpoint_scope,
)
from packages.core.ai.runtime.tool_results import RuntimeNoEffectReason, RuntimeNoEffectToolResult
from tests.test_nested_skill_durability import checkpoint, seed, worker_scopes


def _missing(path="missing.md"):
    return RuntimeNoEffectToolResult.patch_target_not_found(json.dumps({"error": f"File not found: {path}"}))


def test_missing_target_proof_is_exact_and_cannot_be_minted_by_json():
    proof = _missing()
    assert proof.matches_call("patch_file", {"path": "missing.md"})
    payload = json.loads(proof)
    assert payload["contract"] == "tool_error"
    assert payload["error"] == "File not found: missing.md"
    assert "hint" not in payload and "recovery_hint" not in payload
    assert not proof.matches_call("patch_file", {"path": "other.md"})
    assert not proof.matches_call("mcp__provider__patch_file", {"path": "missing.md"})
    assert not proof.matches_call("generate_file", {"kind": "document", "path": "missing.md"})
    assert not isinstance(json.loads(json.dumps(proof)), RuntimeNoEffectToolResult)
    assert not RuntimeNoEffectToolResult.patch_target_not_found('{"error":"write failed"}').matches_call("patch_file", {"path": "missing.md"})


def test_missing_target_diagnostics_preserve_candidates_without_selecting_a_repair():
    proof = RuntimeNoEffectToolResult.patch_target_not_found(json.dumps({
        "error": "File not found: missing.md",
        "candidates": ["documents/missing.md", "archive/missing.md"],
        "hint": "Retry one of the candidates",
        "recovery_hint": "Create a new file",
    }))
    assert json.loads(proof)["details"] == {
        "candidates": ["documents/missing.md", "archive/missing.md"],
    }
    assert proof.matches_call("patch_file", {"path": "missing.md"})
    assert not proof.matches_call("patch_file", {"path": "documents/missing.md"})


@pytest.mark.asyncio
async def test_missing_patch_target_stops_under_lock_before_mutation_admission(tmp_path, monkeypatch):
    from packages.core.ai.tools import file_tools

    locked = False

    @asynccontextmanager
    async def lock(root):
        nonlocal locked
        assert root == str(tmp_path)
        locked = True
        try:
            yield
        finally:
            locked = False

    async def locate(**kwargs):
        assert locked
        return None, []

    forbidden = AsyncMock(side_effect=AssertionError("missing target must not cross write admission"))
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(file_tools, "runtime_entity_filesystem_mutation_lock", lock)
    monkeypatch.setattr(file_tools, "_locate_entity_file", locate)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", forbidden)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", forbidden)
    monkeypatch.setattr(file_tools, "_commit_file_projection", forbidden)
    result = await file_tools._patch_file("entity", path="missing.md", operations=[{"op": "text.set", "text": "new"}])
    assert isinstance(result, RuntimeNoEffectToolResult)
    assert result.reason is RuntimeNoEffectReason.PATCH_TARGET_NOT_FOUND
    assert result.matches_call("patch_file", {"path": "missing.md"})
    assert list(tmp_path.iterdir()) == []
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_postcommit_patch_exception_is_not_no_effect(tmp_path, monkeypatch):
    from packages.core.ai.tools import file_tools

    target = tmp_path / "existing.md"
    target.write_text("before", encoding="utf-8")

    async def fail_after_write(**kwargs):
        target.write_bytes(kwargs["data"])
        raise OSError("projection failed after bytes were written")

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(file_tools, "_locate_entity_file", AsyncMock(return_value=(str(target), [])))
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", AsyncMock(return_value=None))
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", AsyncMock(return_value=None))
    monkeypatch.setattr(file_tools, "_commit_file_projection", fail_after_write)
    result = await file_tools._patch_file_locked("entity", path="existing.md", operations=[{"op": "text.set", "text": "after"}])
    assert target.read_text(encoding="utf-8") == "after"
    assert json.loads(result)["error"] == "projection failed after bytes were written"
    assert not isinstance(result, RuntimeNoEffectToolResult)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "claim", "call", "arguments", "generation", "proof_path", "provider_json", "postcommit_error"])
async def test_missing_patch_target_settles_only_exact_host_proof_before_round_continues(db_session, monkeypatch, mismatch):
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.worker import WorkLease
    from packages.core.workers import internal
    from packages.core.workers.coordinator_tools import _has_unsettled_effect_intent

    snapshot = await seed(db_session, monkeypatch)
    arguments = {"path": "missing.md", "operations": [{"op": "text.set", "text": "new"}]}
    pending = checkpoint("patch_file", "patch", arguments)
    with worker_scopes(snapshot), runtime_tool_effect_checkpoint_scope({"patch": pending}):
        await runtime_record_tool_effect_intent("patch_file", arguments, "patch")
    current = {**snapshot}
    if mismatch == "claim":
        current["execution_claim_id"] = "stale"
    elif mismatch == "generation":
        current["action_effect_generation"] = 1
    call_id = "different" if mismatch == "call" else "patch"
    observed = {**arguments, "path": "other.md"} if mismatch == "arguments" else arguments
    result = _missing("other.md" if mismatch == "proof_path" else "missing.md")
    if mismatch == "provider_json":
        result = json.dumps({"error": "File not found: missing.md", "no_effect": True, "reason": "patch_target_not_found"})
    elif mismatch == "postcommit_error":
        result = '{"error":"projection failed after bytes were written"}'
    with worker_scopes(current), runtime_tool_effect_checkpoint_scope({call_id: pending}):
        if mismatch in {"claim", "call", "arguments", "generation", "proof_path"}:
            with pytest.raises(RuntimeToolEffectPersistenceError):
                await runtime_record_tool_effect_result("patch_file", observed, result, call_id)
        else:
            await runtime_record_tool_effect_result("patch_file", arguments, result, call_id)
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    lease = await db_session.get(WorkLease, snapshot["lease_id"])
    assert _has_unsettled_effect_intent(step.evidence_refs) is bool(mismatch)
    assert _has_unsettled_effect_intent(lease.evidence_refs) is bool(mismatch)
    messages = [*pending["messages"], {"role": "tool", "tool_call_id": "patch", "content": str(result)}]
    if mismatch is None:
        assert step.runtime_checkpoint["pending_tool_result"]["content"] == str(result)
        restored = await internal._restore_worker_nested_checkpoint({**snapshot, "runtime_checkpoint": step.runtime_checkpoint})
        assert isinstance(restored["pending_tool_result"]["content"], RuntimeNoEffectToolResult)
        assert restored["pending_tool_result"]["content"].reason is RuntimeNoEffectReason.PATCH_TARGET_NOT_FOUND
        # A normal round can continue only after the exact settlement and
        # result above are visible in both durable Step and Lease evidence.
        await internal._persist_worker_round_checkpoint(snapshot, rounds=1, messages=messages)
    elif mismatch in {"provider_json", "postcommit_error"}:
        with pytest.raises(RuntimeError, match="no durable terminal receipt"):
            await internal._persist_worker_round_checkpoint(snapshot, rounds=1, messages=messages)
