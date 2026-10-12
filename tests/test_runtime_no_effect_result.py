"""Only a host-observed no-write branch can settle an exact effect intent."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import json

import pytest

from packages.core.ai.runtime.tool_results import (
    RuntimeNoEffectReason,
    RuntimeNoEffectToolResult,
    RuntimePreflightValidationError,
)
from packages.core.constants.tool_effects import ToolEffectSettlementOutcome
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory


def test_no_effect_proof_does_not_survive_public_serialization():
    result = RuntimeNoEffectToolResult.document_target_exists()
    assert result.reason is RuntimeNoEffectReason.DOCUMENT_TARGET_EXISTS
    payload = json.loads(result)
    assert payload["contract"] == "tool_error"
    assert payload["status"] == "error"
    assert payload["error"] == "file_already_exists"
    assert payload["details"] == {"created": False, "modified": False}
    assert "hint" not in payload and "recovery_hint" not in payload
    assert result.matches_call("generate_file", {"kind": "document"})
    assert not result.matches_call("generate_file", {"kind": "video"})
    assert not result.matches_call("patch_file", {"kind": "document"})
    assert not isinstance(result, RuntimePreflightValidationError)
    assert not isinstance(str(result), RuntimeNoEffectToolResult)
    assert not isinstance(json.loads(json.dumps(result)), RuntimeNoEffectToolResult)
    copied = deepcopy(result)
    assert isinstance(copied, RuntimeNoEffectToolResult)
    assert copied.reason is result.reason
    with pytest.raises(ValueError):
        RuntimeNoEffectToolResult("error", reason="provider_says_no_effect")


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("persist_fails", [False, True])
async def test_handler_result_barrier_is_before_return_and_cannot_be_swallowed(
    monkeypatch, dynamic, persist_fails,
):
    from packages.core.ai.runtime import tool_execution as execution
    from packages.core.ai.runtime.tool_effect_intents import (
        RuntimeToolEffectPersistenceError,
        runtime_tool_effect_checkpoint_scope,
        runtime_tool_effect_result_scope,
    )

    events = []
    result = RuntimeNoEffectToolResult.document_target_exists()
    arguments = {"kind": "document", "name": "report.md", "content": "body"}
    prepared = execution.RuntimePreparedToolExecution(arguments={
        **arguments, "_runtime_tool_call_id_from_context": "call-existing",
    })
    checkpoint = {"pending_tool_call": {"id": "call-existing", "name": "generate_file"}}

    async def prepare(**kwargs):
        return prepared

    async def observe(name, args, returned, call_id, classification, pending):
        events.append("durability")
        assert name == "generate_file" and args == arguments
        assert returned is result and call_id == "call-existing"
        assert pending == checkpoint
        if persist_fails:
            raise ValueError("lost execution fence")

    def handler(*args, **kwargs):
        events.append("handler")
        return result

    async def settle(**kwargs):
        events.append("provider_settlement")

    monkeypatch.setattr(execution, "runtime_prepare_tool_execution", prepare)
    monkeypatch.setattr(execution, "runtime_settle_provider_approval_result", settle)

    async def execute():
        if dynamic:
            return await execution.runtime_execute_scoped_dynamic_tool_handler(
                tool_name="generate_file", arguments=arguments, handler=handler,
                entity_id="entity", user_id="user",
            )
        return await execution.runtime_execute_prepared_tool_handler(
            tool_name="generate_file", handler=handler, prepared=prepared,
            public_arguments=arguments, entity_id="entity", user_id="user",
        )

    with runtime_tool_effect_checkpoint_scope({"call-existing": checkpoint}), runtime_tool_effect_result_scope(observe):
        if persist_fails:
            with pytest.raises(RuntimeToolEffectPersistenceError) as raised:
                await execute()
            assert raised.value.allow_alternate_path is False
            assert events == ["handler", "durability"]
        else:
            assert await execute() is result
            assert events == ["handler", "durability", "provider_settlement"]


@pytest.mark.asyncio
async def test_existing_document_is_proven_no_effect_before_file_admission(tmp_path, monkeypatch):
    from packages.core.ai.runtime import file_actions, generated_files

    target = tmp_path / "report.md"
    target.write_text("original bytes", encoding="utf-8")
    inside_lock = False

    @asynccontextmanager
    async def mutation_lock(root):
        nonlocal inside_lock
        assert root == str(tmp_path)
        inside_lock = True
        try:
            yield
        finally:
            inside_lock = False

    async def no_mutation(**kwargs):
        raise AssertionError("occupied CREATE target must stop before write admission")

    original_lexists = generated_files.os.path.lexists

    def check_target(path):
        assert inside_lock
        return original_lexists(path)

    monkeypatch.setattr(file_actions, "runtime_entity_file_root", lambda _: str(tmp_path))
    monkeypatch.setattr(file_actions, "runtime_entity_filesystem_mutation_lock", mutation_lock)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", no_mutation)
    monkeypatch.setattr(generated_files.os.path, "lexists", check_target)
    result = await generated_files.runtime_generate_document_file(
        entity_id="entity", user_id="user", conversation_id="", name="report.md",
        content="must not replace", file_type="md",
    )
    assert isinstance(result, RuntimeNoEffectToolResult)
    assert target.read_text(encoding="utf-8") == "original bytes"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.md"]


def _intent(step_id="step", generation="claim"):
    return ToolEffectEvidenceFactory.bind_effect_id(ToolEffectEvidenceFactory.effect(
        "pending", {
            "tool": "generate_file", "arguments_hash": "exact-args",
            "tool_call_id": "call-create", "effect_generation": generation,
            "owner_step_id": step_id, "owner_plan_id": "plan",
        },
    ))


def _settlement(intent):
    return ToolEffectEvidenceFactory.bind_effect_id(ToolEffectEvidenceFactory.settlement(
        ToolEffectSettlementOutcome.RUNTIME_CONFIRMED_NOT_APPLIED,
        {**intent["fields"], "reason": RuntimeNoEffectReason.DOCUMENT_TARGET_EXISTS.value},
    ))


def test_runtime_no_effect_is_server_owned_and_settles_only_exact_generation():
    pending = _intent()
    other_step = _intent("other-step")
    other_generation = _intent(generation="new-claim")
    settlement = _settlement(pending)
    existing = [pending, other_step, other_generation]
    assert ToolEffectEvidenceFactory.is_server_owned(settlement)
    assert ToolEffectEvidenceFactory.merge_untrusted(existing, [settlement]) == existing
    settled = ToolEffectEvidenceFactory.append_server_owned(existing, [settlement])
    assert pending not in settled
    assert other_step in settled and other_generation in settled
    assert settlement in settled
    # Subsequent worker evidence cannot discard or counterfeit this authority.
    assert ToolEffectEvidenceFactory.merge_untrusted(settled, [pending]) == settled


def test_runtime_no_effect_with_tampered_identity_cannot_settle():
    pending = _intent()
    settlement = _settlement(pending)
    settlement["fields"]["owner_step_id"] = "foreign"
    assert pending in ToolEffectEvidenceFactory.append_server_owned([pending], [settlement])
