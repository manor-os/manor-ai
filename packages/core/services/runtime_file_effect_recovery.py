"""Read exact first-party file outcomes without replaying an uncertain tool.

The journal proves a historical file/Knowledge commit, not the original tool's
native result or the current contents of that path. Task owners must fence the
execution before using this observation to resume a pending checkpoint.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Literal, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.services.entity_fs import EntityFileVersion


# These handlers perform exactly one journaled file/Knowledge transaction and
# have no provider side effect. delete_file has no journal receipt; generate_file
# includes provider/Skill paths whose completion cannot be inferred from a file.
_FILE_TOOLS = frozenset({"patch_file", "replace_file"})
_execution_scope: ContextVar[dict[str, Any] | None] = ContextVar(
    "runtime_file_effect_execution_scope", default=None,
)


@contextmanager
def runtime_file_effect_scope(
    *, entity_id: str, workspace_id: str, task_id: str, plan_id: str,
    step_id: str, effect_generation: int | str,
    coordinator_child_step_id: str = "",
) -> Iterator[None]:
    """Bind server-owned worker identity; model tool arguments cannot supply it."""
    token = _execution_scope.set({
        "entity_id": entity_id, "workspace_id": workspace_id, "task_id": task_id,
        "plan_id": plan_id, "step_id": step_id,
        "effect_generation": str(effect_generation),
        "coordinator_child_step_id": coordinator_child_step_id,
    })
    try:
        yield
    finally:
        _execution_scope.reset(token)


def is_runtime_file_effect(ref: Mapping[str, Any]) -> bool:
    """Recognize only the supported native file-mutation contract."""
    if not isinstance(ref, Mapping):
        return False
    fields = ref.get("fields")
    return bool(
        isinstance(fields, dict)
        and isinstance(fields.get("tool"), str)
        and fields.get("tool") in _FILE_TOOLS
        and fields.get("capability_id") == "file.write"
        and fields.get("action_key") == "workspace.file.modify"
        and fields.get("classification") in (None, "action")
        and not fields.get("provider")
        and ToolEffectEvidenceFactory.has_valid_effect_id(ref)
    )


def runtime_file_effect_owner(entity_id: str, receipt: Any) -> dict[str, Any]:
    """Capture exact authorization, checkpoint and worker lineage at prepare."""
    from packages.core.ai.runtime.authorization_receipts import runtime_authorization_arguments_hash
    from packages.core.ai.runtime.nested_agent import runtime_scoped_tool_call_id
    from packages.core.ai.runtime.tool_effect_intents import runtime_current_tool_checkpoint
    from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash

    scope = _execution_scope.get()
    if not scope or receipt is None or receipt.tool_name not in _FILE_TOOLS:
        return {}
    if any(not scope.get(key) for key in (
        "entity_id", "workspace_id", "task_id", "plan_id", "step_id", "effect_generation",
    )):
        return {}
    if not re.fullmatch(r"0|[1-9][0-9]*", scope["effect_generation"]):
        return {}
    if entity_id != scope["entity_id"] or any(
        getattr(receipt, key, None) != scope[key]
        for key in ("entity_id", "workspace_id", "task_id")
    ):
        return {}
    checkpoint = runtime_current_tool_checkpoint()
    pending = checkpoint.get("pending_tool_call") if isinstance(checkpoint, dict) else None
    if not isinstance(pending, dict) or pending.get("name") != receipt.tool_name or not pending.get("id"):
        return {}
    arguments = pending.get("arguments")
    if not isinstance(arguments, dict) or runtime_authorization_arguments_hash(
        arguments, public_argument_keys=receipt.public_argument_keys,
    ) != receipt.arguments_hash:
        return {}
    effect = ToolEffectEvidenceFactory.bind_effect_id({"fields": {
        "tool": receipt.tool_name,
        "tool_call_id": runtime_scoped_tool_call_id(pending["id"]),
        "arguments_hash": _tool_arguments_hash(arguments),
        "owner_plan_id": scope["plan_id"], "owner_step_id": scope["step_id"],
        "effect_generation": scope["effect_generation"],
        "coordinator_child_step_id": scope["coordinator_child_step_id"],
        "capability_id": receipt.capability_id, "action_key": receipt.action_key,
        "classification": "action",
    }})
    if not is_runtime_file_effect(effect):
        return {}
    return {
        **scope, "file_effect_version": 1, "file_effect": effect,
        "tool_call_id": effect["fields"]["tool_call_id"],
        "authorization_arguments_hash": receipt.arguments_hash,
    }


@dataclass(frozen=True)
class RuntimeFileEffectRecovery:
    operation_status: Literal["committed", "rolled_back"]
    observation: dict[str, Any]
    journal_ids: tuple[str, ...]


async def read_runtime_file_effect_receipt(
    db: AsyncSession, *, entity_id: str, workspace_id: str, task_id: str,
    plan_id: str, step_id: str, effect_ref: Mapping[str, Any],
    runtime_checkpoint: dict[str, Any] | None = None,
) -> RuntimeFileEffectRecovery | None:
    """Read terminal evidence for one exact call; absence never means rollback.

    This helper neither commits nor locks/mutates files or database rows. The
    caller owns lifecycle/lease fencing and the decision to resume execution.
    Unsupported, historical, conflicting or incomplete evidence returns None.
    """
    if not all((entity_id, workspace_id, task_id, plan_id, step_id)) or not is_runtime_file_effect(effect_ref):
        return None
    identity = ToolEffectEvidenceFactory.identity(effect_ref)
    if (
        identity.owner_plan_id != plan_id or identity.owner_step_id != step_id
        or not identity.tool_call_id or not identity.arguments_hash
        or not re.fullmatch(r"0|[1-9][0-9]*", identity.effect_generation)
        or (identity.coordinator_child_step_id and identity.coordinator_child_step_id != step_id)
    ):
        return None
    if runtime_checkpoint is not None:
        from packages.core.ai.runtime.nested_agent import runtime_checkpoint_tool_owner
        from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash

        try:
            leaf = runtime_checkpoint_tool_owner(runtime_checkpoint, identity.tool_call_id)
            pending = leaf["pending_tool_call"]
            declarations = [
                call for message in leaf.get("messages", []) if message.get("role") == "assistant"
                for call in message.get("tool_calls") or []
                if isinstance(call, dict) and call.get("id") == pending["id"]
            ]
            if (
                pending.get("name") != identity.tool
                or _tool_arguments_hash(pending.get("arguments") or {}) != identity.arguments_hash
                or len(declarations) != 1
                or any(message.get("role") == "tool" and message.get("tool_call_id") == pending["id"]
                       for message in leaf.get("messages", []))
            ):
                return None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
    effect_id = ToolEffectEvidenceFactory.effect_id(effect_ref)
    # Fetch at most two: these allowlisted handlers each own exactly one row.
    # A second matching transaction is ambiguity, never "take the latest".
    with db.no_autoflush:
        rows = list((await db.execute(select(RuntimeFileCommit).where(
            RuntimeFileCommit.entity_id == entity_id,
            RuntimeFileCommit.owner["workspace_id"].astext == workspace_id,
            RuntimeFileCommit.owner["task_id"].astext == task_id,
            RuntimeFileCommit.effect_id == effect_id,
        ).limit(2).execution_options(populate_existing=True))).scalars())
    if len(rows) != 1:
        return None
    row = rows[0]
    owner = row.owner if isinstance(row.owner, dict) else {}
    stored_ref = owner.get("file_effect")
    expected_owner = {
        "entity_id": entity_id, "workspace_id": workspace_id, "task_id": task_id,
        "plan_id": plan_id, "step_id": step_id,
        "effect_generation": identity.effect_generation,
        "coordinator_child_step_id": identity.coordinator_child_step_id,
        "tool_name": identity.tool, "tool_call_id": identity.tool_call_id,
    }
    if (
        row.entity_id != entity_id or row.effect_id != effect_id
        or owner.get("file_effect_version") != 1
        or any(owner.get(key) != value for key, value in expected_owner.items())
        or not isinstance(stored_ref, dict) or not is_runtime_file_effect(stored_ref)
        or ToolEffectEvidenceFactory.identity(stored_ref) != identity
        or stored_ref["fields"].get("effect_id") != effect_id
        or not isinstance(owner.get("authorization_arguments_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", owner.get("authorization_arguments_hash") or "")
    ):
        return None
    from packages.core.services.generated_file_refs import canonical_fs_path

    if (
        not row.transaction_id or not row.rel_path
        or canonical_fs_path(row.rel_path) != row.rel_path
        or row.path_sha256 != hashlib.sha256(row.rel_path.encode("utf-8")).hexdigest()
        or not re.fullmatch(r"[0-9a-f]{64}", row.content_sha256 or "")
        or row.state not in {"committed", "rolled_back"}
        or not isinstance(row.snapshot, dict)
    ):
        return None
    # Refuse a partial transaction, including another row with damaged owner
    # metadata that the exact identity query above deliberately did not select.
    with db.no_autoflush:
        members = list((await db.execute(select(RuntimeFileCommit.id).where(
            RuntimeFileCommit.transaction_id == row.transaction_id,
        ).limit(2))).scalars())
    if members != [row.id]:
        return None
    version = None
    if row.written_version is not None:
        try:
            version = EntityFileVersion(**row.written_version)
        except (TypeError, ValueError):
            return None
        if any(type(value) is not int or value < 0 for value in asdict(version).values()):
            return None
    if row.state == "committed" and (not row.document_id or version is None):
        return None
    if row.state == "rolled_back" and (row.document_id or row.cleanup_pending or row.error):
        return None
    observation = {
        "kind": "runtime_file_reconciliation", "version": 1,
        # This observation completed; operation_status separately reports the
        # historical transaction outcome, not a reconstructed native response.
        "status": "completed",
        "operation_status": row.state, "tool": identity.tool,
        "operation": "patch" if identity.tool == "patch_file" else "replace",
        "effect_id": effect_id,
        "original_tool_result_available": False,
        "current_file_state_verified": False,
        "message": (
            "The exact file and Knowledge transaction committed. This is a historical "
            "journal observation, not the original tool response; the file may have changed since."
            if row.state == "committed" else
            "The exact file transaction rolled back. This is a journal observation, "
            "not the original tool error; no file mutation from this transaction remains."
        ),
        "files": [{
            "journal_id": row.id, "transaction_id": row.transaction_id,
            "path": row.rel_path, "document_id": row.document_id,
            "content_sha256": row.content_sha256,
            "written_version": asdict(version) if version is not None else None,
            "source_sha256": row.snapshot.get("source_sha256"),
            "source_version": deepcopy(row.snapshot.get("source_version")),
        }],
    }
    return RuntimeFileEffectRecovery(row.state, deepcopy(observation), (row.id,))
