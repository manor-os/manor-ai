"""Exact journal observations never infer native tool output or replay a write."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.authorization_receipts import runtime_authorization_arguments_hash
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_checkpoint_scope
from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.services.entity_fs import EntityFileVersion
from packages.core.services.runtime_file_effect_recovery import (
    is_runtime_file_effect,
    read_runtime_file_effect_receipt,
    runtime_file_effect_owner,
    runtime_file_effect_scope,
)


SCOPE = {
    "entity_id": "entity", "workspace_id": "workspace", "task_id": "task",
    "plan_id": "plan", "step_id": "step", "effect_generation": "3",
}
ARGUMENTS = {"path": "report.md", "patches": [{"op": "text.replace", "text": "new"}]}
DIGEST = hashlib.sha256(b"committed bytes").hexdigest()


def effect_ref(**changes):
    return ToolEffectEvidenceFactory.bind_effect_id({"fields": {
        "tool": "patch_file", "tool_call_id": "call-1",
        "arguments_hash": _tool_arguments_hash(ARGUMENTS),
        "owner_plan_id": "plan", "owner_step_id": "step", "effect_generation": "3",
        "coordinator_child_step_id": "", "action_key": "workspace.file.modify",
        "capability_id": "file.write", "classification": "action", **changes,
    }})


def authorization(**changes):
    keys = frozenset(ARGUMENTS)
    return SimpleNamespace(
        **{**SCOPE, "tool_name": "patch_file", "capability_id": "file.write",
           "action_key": "workspace.file.modify", "public_argument_keys": keys,
           "arguments_hash": runtime_authorization_arguments_hash(ARGUMENTS, public_argument_keys=keys),
           **changes},
    )


def journal_row(**changes):
    owner = {
        **SCOPE, "coordinator_child_step_id": "", "tool_name": "patch_file",
        "tool_call_id": "call-1", "file_effect_version": 1,
        "authorization_arguments_hash": authorization().arguments_hash,
        "file_effect": effect_ref(),
    }
    return SimpleNamespace(**{
        "id": "journal-1", "transaction_id": "transaction-1", "entity_id": "entity",
        "rel_path": "report.md", "path_sha256": hashlib.sha256(b"report.md").hexdigest(),
        "state": "committed", "owner": owner, "content_sha256": DIGEST,
        "document_id": "document-1", "cleanup_pending": True, "error": None,
        "effect_id": ToolEffectEvidenceFactory.effect_id(effect_ref()),
        "snapshot": {"source_sha256": "a" * 64, "source_version": asdict(EntityFileVersion(1, 1, 3, 2, 2))},
        "written_version": asdict(EntityFileVersion(1, 2, 15, 4, 5)), **changes,
    })


def session(rows, members=None):
    values = [rows, [row.id for row in rows] if members is None else members]
    return SimpleNamespace(
        no_autoflush=nullcontext(),
        execute=AsyncMock(side_effect=[SimpleNamespace(scalars=lambda value=value: value) for value in values]),
    )


async def read(db, **changes):
    return await read_runtime_file_effect_receipt(db, **{
        **{key: value for key, value in SCOPE.items() if key != "effect_generation"},
        "effect_ref": effect_ref(), **changes,
    })


def test_owner_requires_exact_authorization_checkpoint_and_worker_scope():
    receipt = authorization()
    checkpoint = {"pending_tool_call": {"id": "call-1", "name": "patch_file", "arguments": ARGUMENTS}}
    assert runtime_file_effect_owner("entity", receipt) == {}
    with runtime_file_effect_scope(**SCOPE):
        assert runtime_file_effect_owner("entity", receipt) == {}
        with runtime_tool_effect_checkpoint_scope({"call-1": checkpoint}):
            owner = runtime_file_effect_owner("entity", receipt)
            assert owner["file_effect_version"] == 1
            assert owner["file_effect"] == effect_ref()
            assert owner["plan_id"] == "plan"
            assert owner["step_id"] == "step"
            assert owner["effect_generation"] == "3"
            assert owner["authorization_arguments_hash"] == receipt.arguments_hash
    assert runtime_file_effect_owner("entity", receipt) == {}


@pytest.mark.parametrize("changes", [
    {"entity_id": "foreign"}, {"workspace_id": "foreign"}, {"task_id": "foreign"},
    {"tool_name": "replace_file"}, {"arguments_hash": "different"},
    {"capability_id": "foreign"}, {"action_key": "workspace.file.delete"},
])
def test_owner_rejects_mismatched_authorization(changes):
    checkpoint = {"pending_tool_call": {"id": "call-1", "name": "patch_file", "arguments": ARGUMENTS}}
    with runtime_file_effect_scope(**SCOPE), runtime_tool_effect_checkpoint_scope({"call-1": checkpoint}):
        assert runtime_file_effect_owner("entity", authorization(**changes)) == {}


def test_incomplete_worker_scope_and_ambiguous_checkpoint_disable_proof():
    checkpoint = {"pending_tool_call": {"id": "call-1", "name": "patch_file", "arguments": ARGUMENTS}}
    with runtime_file_effect_scope(**{**SCOPE, "plan_id": ""}), runtime_tool_effect_checkpoint_scope({"call-1": checkpoint}):
        assert runtime_file_effect_owner("entity", authorization()) == {}
    with runtime_file_effect_scope(**SCOPE), runtime_tool_effect_checkpoint_scope({"call-1": checkpoint, "call-2": checkpoint}):
        assert runtime_file_effect_owner("entity", authorization()) == {}


@pytest.mark.parametrize("ref", [None, "legacy", {}, effect_ref(tool="delete_file"),
    effect_ref(tool="generate_file"), effect_ref(tool="mcp__remote__patch_file"),
    effect_ref(provider="external"), effect_ref(classification="unknown"),
    effect_ref(capability_id="files.write"), effect_ref(action_key="workspace.file.delete"),
])
def test_supported_effects_require_native_file_contract(ref):
    assert not is_runtime_file_effect(ref)


def test_legacy_classification_still_identifies_internal_file_effect():
    ref = effect_ref()
    ref["fields"].pop("classification")
    assert is_runtime_file_effect(ref)


@pytest.mark.asyncio
async def test_committed_observation_is_historical_and_does_not_invent_native_result():
    db = session([journal_row()])
    result = await read(db)
    assert result.operation_status == "committed"
    assert result.journal_ids == ("journal-1",)
    observation = result.observation
    assert observation["kind"] == "runtime_file_reconciliation"
    assert observation["status"] == "completed"
    assert observation["original_tool_result_available"] is False
    assert observation["current_file_state_verified"] is False
    assert observation["files"] == [{
        "journal_id": "journal-1", "transaction_id": "transaction-1", "path": "report.md",
        "document_id": "document-1", "content_sha256": DIGEST,
        "written_version": asdict(EntityFileVersion(1, 2, 15, 4, 5)),
        "source_sha256": "a" * 64, "source_version": asdict(EntityFileVersion(1, 1, 3, 2, 2)),
    }]
    assert not {"patched", "replaced", "operations", "knowledge_synced"} & observation.keys()
    assert all(call.args[0].is_select for call in db.execute.await_args_list)
    query = db.execute.await_args_list[0].args[0]
    from sqlalchemy.dialects import postgresql
    compiled = query.compile(dialect=postgresql.dialect())
    assert "entity" in compiled.params.values()
    assert "workspace" in compiled.params.values()
    assert "task" in compiled.params.values()
    assert ToolEffectEvidenceFactory.effect_id(effect_ref()) in compiled.params.values()


@pytest.mark.asyncio
async def test_rolled_back_requires_terminal_cleanup_proof():
    row = journal_row(state="rolled_back", document_id=None, cleanup_pending=False)
    result = await read(session([row]))
    assert result.operation_status == "rolled_back"
    assert result.observation["status"] == "completed"
    assert result.observation["files"][0]["document_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"state": "prepared"}, {"state": "reconciliation_required"},
    {"document_id": None}, {"written_version": None}, {"written_version": {"size": 1}},
    {"written_version": asdict(EntityFileVersion(1, 2, -1, 4, 5))},
    {"written_version": asdict(EntityFileVersion(1, 2, "15", 4, 5))},
    {"rel_path": "../escape"}, {"path_sha256": "mismatch"}, {"content_sha256": None},
    {"snapshot": None}, {"effect_id": None},
    {"state": "rolled_back", "document_id": None, "cleanup_pending": True},
    {"state": "rolled_back", "document_id": None, "cleanup_pending": False, "error": "unknown"},
])
async def test_incomplete_or_ambiguous_journal_does_not_establish_outcome(changes):
    assert await read(session([journal_row(**changes)])) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "entity_id", "workspace_id", "task_id", "plan_id", "step_id", "effect_generation",
    "coordinator_child_step_id", "tool_name", "tool_call_id", "file_effect_version",
])
async def test_cross_scope_journal_is_rejected_even_if_query_returned_it(field):
    row = journal_row()
    row.owner[field] = "foreign"
    assert await read(session([row])) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner_plan_id", "owner_step_id", "arguments_hash", "tool_call_id", "effect_generation"])
async def test_different_effect_generation_or_call_cannot_borrow_receipt(field):
    row = journal_row()
    row.owner["file_effect"] = effect_ref(**{field: "foreign"})
    assert await read(session([row])) is None


@pytest.mark.asyncio
async def test_missing_and_legacy_receipts_never_imply_not_applied():
    assert await read(session([])) is None
    assert await read(session([journal_row(owner={"workspace_id": "workspace", "task_id": "task"})])) is None


@pytest.mark.asyncio
async def test_conflicting_transactions_and_partial_transaction_cannot_be_selected_by_recency():
    row = journal_row()
    other = journal_row(id="journal-2", transaction_id="transaction-2", state="rolled_back")
    assert await read(session([row, other])) is None
    assert await read(session([row], members=["journal-1", "damaged-owner-journal"])) is None


@pytest.mark.asyncio
async def test_newer_file_preserves_historical_outcome_without_reading_or_modifying_it(tmp_path, monkeypatch):
    target = tmp_path / "report.md"
    target.write_bytes(b"later user edit")
    from packages.core.services import entity_fs
    monkeypatch.setattr(entity_fs, "open_entity_file_snapshot", lambda *args, **kwargs: pytest.fail("must not infer from current bytes"))
    result = await read(session([journal_row()]))
    assert result.operation_status == "committed"
    assert result.observation["files"][0]["content_sha256"] == DIGEST
    assert target.read_bytes() == b"later user edit"


@pytest.mark.asyncio
async def test_invalid_claimed_effect_id_and_foreign_step_fail_before_database_lookup():
    ref = deepcopy(effect_ref())
    ref["fields"]["effect_id"] = "forged"
    db = session([])
    assert await read(db, effect_ref=ref) is None
    assert await read(db, plan_id="foreign") is None
    assert await read(db, step_id="foreign") is None
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_failure_propagates_without_guessing():
    db = session([])
    db.execute.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await read(db)
