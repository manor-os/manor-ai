"""Shared launch policy and orchestration for user-facing Workspace Flows."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_record_source import resolve_record_select_inputs
from packages.core.services.workspace_workflow_router import (
    WorkspaceChatEntrypoint,
    assemble_workspace_workflow_inputs,
    prepare_workspace_workflow_inputs,
    validate_workspace_workflow_inputs,
    workflow_attachment_descriptors,
)


async def _prepare_chat_inputs(
    db: AsyncSession,
    entrypoint: WorkspaceChatEntrypoint,
    *,
    message: str,
    attachment_refs: list[dict[str, Any]],
    entity_id: str,
    user_id: str | None,
    workspace_id: str,
    unmatched: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Prefill from Chat, offering record choices as their current Ledger options."""
    pinned = await resolve_record_select_inputs(
        db,
        entrypoint.run_inputs,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    return await prepare_workspace_workflow_inputs(
        replace(entrypoint, run_inputs=pinned),
        message=message,
        attachment_refs=attachment_refs,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        unmatched=unmatched,
    )


StarterPolicy = Literal["always_review", "only_missing"]
TransactionOwner = Literal["launcher", "caller"]


@dataclass(frozen=True)
class WorkspaceFlowLaunch:
    run: WorkflowRun
    conversation: Conversation
    origin_message: Message
    activity_message: Message
    starter_message: Message | None
    created: bool


def workspace_flow_requires_starter_input(
    entrypoint: WorkspaceChatEntrypoint,
    input_values: dict[str, Any],
    *,
    starter_policy: StarterPolicy,
) -> bool:
    """Return whether a launch must stop for editable starter input."""
    if not entrypoint.run_inputs:
        return False
    if starter_policy == "always_review":
        return True
    if starter_policy != "only_missing":
        raise ValueError(f"Unsupported starter policy: {starter_policy}")
    try:
        validate_workspace_workflow_inputs(entrypoint, input_values)
    except ValueError:
        return True
    return False


def workspace_flow_launch_key(
    *,
    source: str,
    origin_message_id: str,
    binding_id: str,
    proposal_item_id: str | None = None,
) -> str:
    if proposal_item_id:
        return f"proposal:{proposal_item_id}"
    return f"{source}:{origin_message_id}:{binding_id}"


async def _existing_launch(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    launch_key: str,
) -> WorkflowRun | None:
    return (await db.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.trigger_data["_workspace_flow_launch_key"].astext
            == launch_key,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def _validate_launch_context(
    db: AsyncSession,
    *,
    entrypoint: WorkspaceChatEntrypoint,
    binding: WorkflowBinding,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    conversation: Conversation,
    origin_message: Message,
    manual_run: bool = False,
) -> None:
    from packages.core.services.workspace_access import (
        user_can_write_workspace_artifacts,
    )

    user = await db.get(User, user_id)
    workspace = await db.get(Workspace, workspace_id)
    if user is None or str(user.entity_id) != entity_id:
        raise PermissionError("Flow launch user is outside the requested entity")
    if workspace is None or str(workspace.entity_id) != entity_id or workspace.deleted_at is not None:
        raise PermissionError("Flow launch Workspace is unavailable")
    if str(conversation.entity_id) != entity_id:
        raise PermissionError("Flow launch conversation is outside the requested entity")
    if (
        not conversation.workspace_id
        and conversation.user_id
        and str(conversation.user_id) != user_id
    ):
        raise PermissionError("Flow launch conversation belongs to another user")
    if conversation.workspace_id and str(conversation.workspace_id) != workspace_id:
        raise PermissionError("Flow launch conversation belongs to another Workspace")
    if str(origin_message.conversation_id) != str(conversation.id):
        raise PermissionError("Flow launch origin message belongs to another conversation")
    if (
        str(binding.entity_id) != entity_id
        or str(binding.workspace_id or "") != workspace_id
        or (
            (not bool(binding.enabled) or str(binding.status or "") != "active")
            and not (manual_run and str(binding.status or "") in {"active", "paused"})
        )
    ):
        raise PermissionError("Flow binding is not active in the requested Workspace")
    if (
        str(entrypoint.binding_id) != str(binding.id)
        or str(entrypoint.workflow_id) != str(binding.workflow_id)
        or str(entrypoint.workspace_id or "") != workspace_id
    ):
        raise PermissionError("Flow entrypoint does not match its Workspace binding")
    if not await user_can_write_workspace_artifacts(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        entity_role=user.role,
    ):
        raise PermissionError("User cannot run Flows in this Workspace")


def _workflow_result_value(result: Any, path: str) -> Any:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            pass
    current = result
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return deepcopy(current)


async def _prefill_workflow_result_inputs(
    db: AsyncSession,
    *,
    entrypoint: WorkspaceChatEntrypoint,
    values: dict[str, Any],
    explicit_keys: set[str],
    entity_id: str,
    workspace_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Use only approved results from this Workspace Chat as declared inputs."""
    requested = [
        item
        for item in entrypoint.run_inputs
        if item.get("prefill", {}).get("source") == "workflow_result"
        and str(item["key"]) not in explicit_keys
    ]
    if not requested:
        return values

    workflow_slugs = {
        str(item["prefill"]["workflow_slug"])
        for item in requested
    }
    rows = (await db.execute(
        select(
            WorkflowRun,
            WorkflowDefinition,
            WorkflowTemplateInstallation.component_key,
        )
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_id)
        .outerjoin(
            WorkflowTemplateInstallation,
            and_(
                WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id,
                WorkflowTemplateInstallation.entity_id == entity_id,
                WorkflowTemplateInstallation.component_key.in_(workflow_slugs),
            ),
        )
        .where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.status == "completed",
            WorkflowDefinition.entity_id == entity_id,
            or_(
                WorkflowDefinition.name.in_(workflow_slugs),
                WorkflowTemplateInstallation.id.is_not(None),
            ),
        )
        .order_by(
            WorkflowRun.completed_at.desc(),
            WorkflowRun.created_at.desc(),
        )
        .limit(100)
    )).all()

    latest: dict[tuple[str, str], WorkflowRun] = {}
    for run, workflow, component_key in rows:
        origin = (run.trigger_data or {}).get("_workflow_chat_origin")
        if not isinstance(origin, dict):
            origin = (run.trigger_data or {}).get("_workspace_chat_entrypoint")
        if (
            not isinstance(origin, dict)
            or str(origin.get("conversation_id") or "") != conversation_id
        ):
            continue
        terminal_step_id = str(run.current_step_id or "")
        latest.setdefault((str(workflow.name or ""), terminal_step_id), run)
        if component_key:
            latest.setdefault((str(component_key), terminal_step_id), run)

    result = dict(values)
    for item in requested:
        prefill = item["prefill"]
        source_run = latest.get((
            str(prefill["workflow_slug"]),
            str(prefill["terminal_step_id"]),
        ))
        if source_run is None:
            continue
        value = _workflow_result_value(
            (source_run.variables or {}).get("__result"),
            str(prefill.get("path") or ""),
        )
        if value is None:
            continue
        key = str(item["key"])
        single_input_entrypoint = replace(entrypoint, run_inputs=(item,))
        try:
            normalized = validate_workspace_workflow_inputs(
                single_input_entrypoint,
                {key: value},
            )
        except ValueError:
            continue
        result.update(normalized)
    return result


async def launch_workspace_flow(
    db: AsyncSession,
    *,
    source: str,
    entrypoint: WorkspaceChatEntrypoint,
    binding: WorkflowBinding,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    conversation_id: str,
    origin_message_id: str,
    source_brief: str,
    attachments: Any = None,
    input_values: dict[str, Any] | None = None,
    starter_policy: StarterPolicy = "always_review",
    proposal_context: dict[str, Any] | None = None,
    origin_metadata: dict[str, Any] | None = None,
    transaction_owner: TransactionOwner = "launcher",
    user_requested: bool = False,
) -> WorkspaceFlowLaunch:
    """Create or reuse one validated, conversation-projected Flow run."""
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.workflow_chat_projection import (
        workflow_progress_steps,
    )
    from packages.core.services.workflow_service import start_workflow_from_binding

    conversation = await db.get(Conversation, conversation_id)
    origin_message = await db.get(Message, origin_message_id)
    if conversation is None or origin_message is None:
        raise LookupError("Flow launch conversation or origin message not found")
    await _validate_launch_context(
        db,
        entrypoint=entrypoint,
        binding=binding,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation=conversation,
        origin_message=origin_message,
        manual_run=source == "manual" and bool(user_id),
    )

    proposal_item_id = str((proposal_context or {}).get("proposal_item_id") or "") or None
    launch_key = workspace_flow_launch_key(
        source=source,
        origin_message_id=origin_message_id,
        binding_id=binding.id,
        proposal_item_id=proposal_item_id,
    )
    # Serialize launches for one binding until the run and its launch key commit.
    # This closes the check-then-create race without adding a JSON-expression index.
    await db.execute(
        select(WorkflowBinding.id)
        .where(
            WorkflowBinding.id == binding.id,
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    existing = await _existing_launch(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        launch_key=launch_key,
    )
    if existing is not None:
        existing_context = dict((existing.trigger_data or {}).get("_workflow_chat_origin") or {})
        activity = await db.get(Message, existing_context.get("activity_message_id"))
        if activity is None:
            raise LookupError("Existing Flow launch activity message not found")
        return WorkspaceFlowLaunch(
            run=existing,
            conversation=conversation,
            origin_message=origin_message,
            activity_message=activity,
            starter_message=None,
            created=False,
        )

    explicit_keys = {str(key) for key in (input_values or {})}
    # Choices named in Chat that the input does not offer: ask, never guess.
    unmatched: dict[str, list[str]] = {}
    if input_values is None:
        initial_values = await _prepare_chat_inputs(
            db,
            entrypoint,
            message=source_brief,
            attachment_refs=workflow_attachment_descriptors(attachments),
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            unmatched=unmatched,
        )
    else:
        declared_inputs = {
            str(item["key"]): item
            for item in entrypoint.run_inputs
        }
        supplied_values = {
            str(key): deepcopy(value)
            for key, value in input_values.items()
            if str(key) in declared_inputs
        }
        try:
            initial_values = validate_workspace_workflow_inputs(
                entrypoint,
                supplied_values,
            )
        except ValueError:
            initial_values = await _prepare_chat_inputs(
                db,
                entrypoint,
                message=source_brief,
                attachment_refs=workflow_attachment_descriptors(attachments),
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                unmatched=unmatched,
            )
            for key, value in supplied_values.items():
                single_input_entrypoint = replace(
                    entrypoint,
                    run_inputs=(declared_inputs[key],),
                )
                try:
                    normalized = validate_workspace_workflow_inputs(
                        single_input_entrypoint,
                        {key: value},
                    )
                except ValueError:
                    continue
                initial_values.update(normalized)
    initial_values = await _prefill_workflow_result_inputs(
        db,
        entrypoint=entrypoint,
        values=initial_values,
        explicit_keys=explicit_keys,
        entity_id=entity_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    initial_variables = assemble_workspace_workflow_inputs(entrypoint, initial_values)
    requires_input = bool(unmatched) or workspace_flow_requires_starter_input(
        entrypoint,
        initial_values,
        starter_policy=starter_policy,
    )
    origin_context = {
        "enabled": True,
        "route_source": source,
        "conversation_id": conversation_id,
        "user_message_id": origin_message_id,
        "projection": entrypoint.projection,
        "wait_bridge": entrypoint.wait_bridge,
        **deepcopy(origin_metadata or {}),
    }
    trigger_data = {
        **deepcopy(initial_values),
        **deepcopy(initial_variables),
        "runtime_context": {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
        },
        "_workspace_flow_launch_key": launch_key,
        "_workflow_chat_origin": origin_context,
        "_workspace_chat_entrypoint": origin_context,
    }
    if proposal_context:
        trigger_data["_proposal_context"] = deepcopy(proposal_context)
    run = await start_workflow_from_binding(
        db,
        binding,
        variables=None,
        trigger_data=trigger_data,
        trigger_source="workspace_chat" if source in {"explicit", "intent"} else source,
        started_by=user_id,
        execution_workspace_id=workspace_id,
        user_requested=user_requested,
    )
    if requires_input:
        run.status = "paused"

    activity_status = "paused" if requires_input else "queued"
    activity_message = await add_message(
        db,
        conversation_id,
        role="system",
        content=(
            f"Selected {entrypoint.title}. Review the workflow inputs to continue."
            if requires_input
            else f"Selected {entrypoint.title}. Preparing the first workflow step."
        ),
        message_kind="workflow_activity",
        refs=[
            {"type": "workflow", "id": run.workflow_id, "title": entrypoint.title},
            {"type": "workflow_run", "id": run.id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_binding_id": binding.id,
            "workflow_title": entrypoint.title,
            "workflow_status": activity_status,
            "workflow_route_source": source,
            "workflow_steps": workflow_progress_steps(
                run,
                activity_status=activity_status,
            ),
        },
    )
    updated_trigger_data = dict(run.trigger_data or trigger_data)
    origin_context = dict(updated_trigger_data.get("_workflow_chat_origin") or {})
    origin_context["activity_message_id"] = activity_message.id
    updated_trigger_data["_workflow_chat_origin"] = origin_context
    updated_trigger_data["_workspace_chat_entrypoint"] = deepcopy(origin_context)
    run.trigger_data = updated_trigger_data

    starter_message = None
    if requires_input:
        starter_message = await add_message(
            db,
            conversation_id,
            role="system",
            content=f"Provide the inputs required to run {entrypoint.title}.",
            message_kind="hitl_request",
            refs=[
                {"type": "workflow", "id": run.workflow_id, "title": entrypoint.title},
                {"type": "workflow_run", "id": run.id},
            ],
            pending_action={
                "kind": PendingActionKind.WORKFLOW_STARTER_INPUT.value,
                "title": entrypoint.title,
                "description": entrypoint.description,
                "workflow_run_id": run.id,
                "workflow_binding_id": binding.id,
                "workflow_id": run.workflow_id,
                "inputs": [dict(item) for item in entrypoint.run_inputs],
                "values": initial_values,
                **({"input_notes": {
                    key: "Not found among the available choices: " + ", ".join(names)
                    for key, names in unmatched.items()
                }} if unmatched else {}),
                "options": ["run", "cancel"],
            },
            meta={
                "workflow_run_id": run.id,
                "workflow_input_stage": "starter",
            },
        )

    launched = WorkspaceFlowLaunch(
        run=run,
        conversation=conversation,
        origin_message=origin_message,
        activity_message=activity_message,
        starter_message=starter_message,
        created=True,
    )
    if transaction_owner == "caller":
        await db.flush()
        return launched
    if transaction_owner != "launcher":
        raise ValueError(f"Unsupported Flow transaction owner: {transaction_owner}")

    await db.commit()
    await enqueue_workspace_flow_launch(db, launched)
    return launched


async def enqueue_workspace_flow_launch(
    db: AsyncSession,
    launched: WorkspaceFlowLaunch,
) -> bool:
    """Publish a committed Flow run, preserving the launcher's failure projection."""

    if not launched.created or launched.starter_message is not None:
        return True
    return await enqueue_workflow_run_or_fail(db, launched.run)


async def enqueue_workflow_run_or_fail(db: AsyncSession, run: WorkflowRun) -> bool:
    """Hand a committed run to the worker queue, or fail it visibly.

    Workflow execution belongs on the worker queue, never inside the request
    that started it: a request process cannot be recovered mid-node. A run
    the broker refused is failed rather than left pending with no owner.
    """

    from packages.core.ai.workflow_runner import WorkflowRunner

    if WorkflowRunner.enqueue(run.id) is False:
        run.status = "failed"
        run.error = "Workflow could not be queued. Please start it again."
        run.completed_at = datetime.now(timezone.utc)
        from packages.core.services.workflow_run_trace import (
            update_workflow_history_summary,
        )

        update_workflow_history_summary(run)
        from packages.core.services.workflow_chat_projection import (
            project_workflow_run_status,
        )

        await project_workflow_run_status(db, run=run)
        await db.commit()
        return False
    return True


async def start_binding_run(
    db: AsyncSession,
    *,
    binding: WorkflowBinding,
    user: User,
    variables: dict | None,
    trigger_data: dict | None,
    workspace_scoped: bool,
) -> tuple[WorkflowRun, WorkspaceFlowLaunch | None]:
    """Start a Workflow binding as the person's manual run, uncommitted.

    A Workspace manual run goes through the Flow launcher (Chat origin, input
    policy, App entry); anything else starts straight from the binding. The
    caller commits, then publishes with ``enqueue_binding_run``.
    """
    from packages.core.services import workflow_service

    execution_binding = await workflow_service.execution_binding_for_automation(db, binding)
    trigger_data = dict(trigger_data or {"manual_test": True})
    trigger_data.pop("_workspace_user_request", None)
    if execution_binding.id != binding.id:
        trigger_data.setdefault("automation_binding_id", binding.id)
    launched = None
    if workspace_scoped and trigger_data.get("workspace_manual") is True:
        from packages.core.services.conversation_messages import add_message
        from packages.core.services.workspace_workflow_router import normalize_chat_entrypoint
        from packages.core.workspace_chat.service import ensure_main_conversation

        workflow = await workflow_service.get_workflow(
            db,
            execution_binding.workflow_id,
            user.entity_id,
        )
        entrypoint = (
            normalize_chat_entrypoint(execution_binding, workflow)
            if workflow is not None
            else None
        )
        if entrypoint is not None:
            conversation = await ensure_main_conversation(
                db,
                entity_id=user.entity_id,
                workspace_id=str(execution_binding.workspace_id),
            )
            origin_message = await add_message(
                db,
                conversation.id,
                role="system",
                content=f"Started {entrypoint.title} from Workspace Flows.",
                message_kind="system",
                refs=[
                    {
                        "type": "workflow",
                        "id": entrypoint.workflow_id,
                        "title": entrypoint.title,
                    },
                    {
                        "type": "workspace",
                        "id": execution_binding.workspace_id,
                    },
                ],
                meta={
                    "author_user_id": user.id,
                    "workflow_route_source": "workspace_manual",
                    "workflow_binding_id": execution_binding.id,
                },
            )
            input_values = dict(variables or {})
            input_values.update(trigger_data)
            launched = await launch_workspace_flow(
                db,
                source="manual",
                entrypoint=entrypoint,
                binding=execution_binding,
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=str(execution_binding.workspace_id),
                conversation_id=conversation.id,
                origin_message_id=origin_message.id,
                source_brief="",
                input_values=input_values,
                starter_policy="only_missing",
                origin_metadata={
                    "route_source": "workspace_manual",
                    "surface": "workspace_flows",
                },
                transaction_owner="caller",
            )
            run = launched.run
            run.trigger_data = {
                **trigger_data,
                **dict(run.trigger_data or {}),
            }
            declared_input_roots = {
                str(item.get("key") or "")
                for item in entrypoint.run_inputs
            } | {
                str(item.get("target") or item.get("key") or "").split(".", 1)[0]
                for item in entrypoint.run_inputs
            }
            extra_variables = {
                str(key): value
                for source in (
                    variables or {},
                    trigger_data,
                )
                for key, value in source.items()
                if str(key) not in declared_input_roots
                and not str(key).startswith("_")
            }
            run.variables = {
                **dict(run.variables or {}),
                **extra_variables,
                "trigger": dict(run.trigger_data),
            }
    if launched is None:
        run = await workflow_service.start_workflow_from_binding(
            db,
            execution_binding,
            variables=variables,
            trigger_data=trigger_data,
            trigger_source="manual",
            started_by=user.id,
        )
    return run, launched


async def enqueue_binding_run(db: AsyncSession, run: WorkflowRun, launched: WorkspaceFlowLaunch | None) -> None:
    """Publish a committed run from ``start_binding_run``."""
    if launched is not None:
        await enqueue_workspace_flow_launch(db, launched)
    else:
        await enqueue_workflow_run_or_fail(db, run)
