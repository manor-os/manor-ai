"""
Task Runner — autonomous agent execution for assigned tasks with supervisor review.

Flow:
  1. Load task + agent config from DB
  2. Build execution prompt from task details
  3. Multi-turn agentic loop (agent executes → supervisor reviews → repeat)
  4. Supervisor verdicts: continue, done, failed, needs_hitl, needs_replan
  5. Update task status and log all activity

Ported from manor-multi-agent's task_runner.py with supervisor mechanism.
"""
from __future__ import annotations

import logging
import time
import json
from typing import Any, Callable, Dict, Optional

from packages.core.ai.runtime.human_input import REQUEST_INPUT_TOOL_NAME
from packages.core.services.task_input_resume import (
    TASK_INPUT_QUESTION_KEY,
    task_input_log_meta,
)

from packages.core.ai.runtime import (
    ChatSurface,
    RUNTIME_TASK_VERDICT_CONTINUE as VERDICT_CONTINUE,
    RUNTIME_TASK_VERDICT_DONE as VERDICT_DONE,
    RUNTIME_TASK_VERDICT_FAILED as VERDICT_FAILED,
    RUNTIME_TASK_VERDICT_NEEDS_HITL as VERDICT_NEEDS_HITL,
    RUNTIME_TASK_VERDICT_NEEDS_REPLAN as VERDICT_NEEDS_REPLAN,
    RuntimeTaskProviderError,
    runtime_assemble_prompt_for_turn,
    runtime_classify_task_complexity,
    runtime_configure_task_engine_model,
    runtime_emit_task_runner_status_event,
    runtime_execute_task_agent_turn,
    runtime_execute_task_final_response,
    runtime_execution_metadata,
    runtime_merge_prompt_appendix,
    runtime_manual_skill_forced_tool_calls,
    runtime_parse_task_supervisor_json,
    runtime_prepare_context_appendix_for_turn,
    runtime_prepare_prompt_appendix_for_turn,
    runtime_persist_task_runner_runtime_events,
    runtime_record_task_runner_execution_evidence,
    runtime_request_for_surface_turn,
    runtime_review_task_agent_output,
    runtime_task_engine,
    runtime_task_engine_model,
    runtime_task_followup_message,
    runtime_task_initial_messages,
    runtime_task_llm_billing_context,
    runtime_resolve_task_billable_user_id,
    runtime_skill_ref_dict,
    runtime_task_supervisor_feedback_message,
    runtime_task_system_prompt,
    runtime_task_ticket_prompt,
    runtime_task_user_prompt,
)
from packages.core.services.workflow_run_execution_claim import (
    WorkflowRunExecutionClaim,
    commit_fenced_execution_boundary,
)
from packages.core.ai.llm_client import CreditExhaustedError, LLMRateLimited
from packages.core.ai.response_language import resolve_response_locale
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.database import async_session
from packages.core.i18n import t
from packages.core.services.agent_service import get_agent
from packages.core.services.task_state_machine import (
    TaskStatusTransitionError,
    apply_task_status_transition,
)
from packages.core.services.task_requester_identity import TaskRequesterIdentityError
from packages.core.services.task_service import add_task_log, update_task
from packages.core.services.task_runtime_approvals import (
    TASK_RUNTIME_HITL_DETAIL_KEY,
)
from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS

logger = logging.getLogger(__name__)


async def _close_task_billing_context_for_error(
    billing_context,
    exc: BaseException,
) -> None:
    """Restore billing context before a task attempt is handed back for retry."""
    try:
        await billing_context.__aexit__(type(exc), exc, exc.__traceback__)
    except Exception:
        logger.debug("TaskRunner: billing context cleanup failed", exc_info=True)


async def _finalize_with_terminal_guard(
    db, task_id: str, entity_id: str, *, status, actual_output, details,
    log_locale: str | None = None,
) -> tuple[Any, bool]:
    """Persist a task-run's final state, tolerating an already-terminal task.

    If a concurrent path already moved the task to a different terminal status
    (for example, a user cancelled while this worker was finishing), that
    committed terminal row is authoritative.  Do not write this runner's stale
    output/details back over it: those details may contain an older approval
    marker and could otherwise resurrect a cancelled continuation.  The bool
    reports whether this runner won the terminal compare-and-set.
    """
    try:
        updated = await update_task(
            db, task_id, entity_id,
            status=status, actual_output=actual_output, details=details,
            require_nonterminal=True,
            log_locale=log_locale,
        )
        return updated, True
    except TaskStatusTransitionError:
        from packages.core.services.task_service import get_task

        current = await get_task(db, task_id, entity_id)
        if current is not None and current.status in _TERMINAL_STATUSES:
            logger.warning(
                "Task %s already terminal; discarding stale runner finalization "
                "for %s",
                task_id,
                status,
            )
            return current, False
        # Transition errors are not synonymous with a concurrent terminal
        # writer (for example a Task may have moved to on_hold).  Hiding those
        # would make the worker report a terminal outcome it never persisted.
        raise

_DEFAULT_MAX_TURNS = DEFAULT_AGENT_MAX_TURNS
_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


def _task_runtime_resume_tool_names(metadata: dict[str, Any] | None) -> set[str]:
    """Return resolver-authored tool names needed by an exact resume."""

    raw_names = metadata.get("extra_tool_names") if isinstance(metadata, dict) else None
    if not isinstance(raw_names, (list, tuple, set, frozenset)):
        return set()
    return {
        name.strip()
        for name in raw_names
        if isinstance(name, str) and name.strip()
    }


def _task_runtime_resume_context(
    base: str | None,
    metadata: dict[str, Any] | None,
) -> str | None:
    guidance = (
        metadata.get("approval_resume_guidance")
        if isinstance(metadata, dict)
        else None
    )
    normalized_guidance = str(guidance or "").strip()
    normalized_base = str(base or "").strip()
    if normalized_base and normalized_guidance:
        return f"{normalized_base}\n\n{normalized_guidance}"
    return normalized_base or normalized_guidance or None


def _task_runtime_metadata(task: Any) -> dict[str, Any] | None:
    """Expose only Task-owned metadata that nested runtime calls must inherit."""

    details = getattr(task, "details", None)
    if not isinstance(details, dict):
        return None
    metadata: dict[str, Any] = {}
    authorization = details.get("proposal_external_authorization")
    if isinstance(authorization, dict):
        metadata["proposal_external_authorization"] = dict(authorization)
    from packages.core.services.agent_initiative_decision import (
        initiative_task_marker,
    )

    marker = initiative_task_marker(details)
    if marker is not None:
        metadata["agent_initiative"] = marker.as_dict()
    return metadata or None


def _hitl_supervisor_evidence(
    question: Any, answer: str, answered_by: Optional[str],
) -> Dict[str, str]:
    """Supervisor receipt for a request_input round-trip that spans two runs."""
    return {
        "tool_name": REQUEST_INPUT_TOOL_NAME,
        "result": json.dumps(
            {
                "status": "answered",
                "question": (
                    question.get("question") if isinstance(question, dict) else question
                ),
                "answer": answer,
                "answered_by": answered_by or "Human",
                "note": "asked in the previous run; the user answered and this run resumed with it",
            },
            ensure_ascii=False,
        ),
    }


def _input_request_text(question: Dict[str, Any]) -> str:
    """The agent's visible reply while parked: the question itself."""
    lines = [str(question.get("question") or "").strip()]
    options = question.get("options")
    if isinstance(options, list) and options:
        lines.append("Options: " + " / ".join(str(item) for item in options))
    fields = question.get("fields")
    if isinstance(fields, list) and fields:
        labels = [
            str(item.get("label") or item.get("name") or "").strip()
            for item in fields if isinstance(item, dict)
        ]
        if any(labels):
            lines.append("Please provide: " + ", ".join(label for label in labels if label))
    context = str(question.get("context") or "").strip()
    if context:
        lines.append(context)
    return "\n".join(line for line in lines if line)


class TaskRunner:
    """Runs an agent against a task ticket with supervisor oversight.

    Lifecycle:
    1. Load task from DB; bail if already terminal
    2. Load agent config (system prompt, tool bindings)
    3. Build execution prompt from task details
    4. Multi-turn loop: agent executes → supervisor reviews → loop or finish
    5. Update task status and log all activity
    """

    def __init__(
        self,
        engine: Any | None = None,
        session_factory=None,
        *,
        before_terminal_commit: Callable[[], None] | None = None,
        after_terminal_commit: Callable[[], None] | None = None,
        execution_claim: WorkflowRunExecutionClaim | None = None,
    ):
        self._engine = engine
        self._session_factory = session_factory
        self._before_terminal_commit = before_terminal_commit
        self._after_terminal_commit = after_terminal_commit
        self._execution_claim = execution_claim

    def _get_session(self):
        """Return a context-managed session — uses worker factory if provided, else global."""
        if self._session_factory:
            return self._session_factory()
        return async_session()

    async def _assemble_runtime_prompt(
        self,
        *,
        entity_id: str,
        user_id: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        runtime: Any,
        active_user_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        skill_refs: list[dict] | None = None,
        blocked_tool_names: list[str] | tuple[str, ...] | None = None,
    ):
        """Build the same workspace-aware prompt and tool surface used by chat."""
        resume_tool_names = _task_runtime_resume_tool_names(metadata)
        if resume_tool_names:
            from packages.core.ai.runtime.tool_registry import (
                runtime_tool_schemas_for_names,
            )

            resume_tool_schemas = runtime_tool_schemas_for_names(
                sorted(resume_tool_names)
            )
        else:
            resume_tool_schemas = []
        async with self._get_session() as prompt_db:
            runtime_request = runtime_request_for_surface_turn(
                surface=ChatSurface.SCHEDULED_AGENT_RUN,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=runtime.workspace_id,
                conversation_id=conversation_id,
                task_id=runtime.task_id,
                thread_ref_kind=runtime.thread_ref_kind,
                thread_ref_id=runtime.thread_ref_id,
                message=active_user_message or "",
                legacy_path="ai.task_runner._assemble_runtime_prompt",
                metadata=metadata,
            )
            assembled = await runtime_assemble_prompt_for_turn(
                prompt_db,
                request=runtime_request,
                tool_profile=runtime.tool_profile,
                agent_id=agent_id,
                bound_tool_names=runtime.bound_tool_names,
                is_master=runtime.is_master,
                mcp_allowed_names=runtime.mcp_allowed_names,
                mcp_provider_scopes=getattr(runtime, "mcp_provider_scopes", ()),
                mcp_scope_unrestricted=bool(
                    getattr(runtime, "mcp_scope_unrestricted", False)
                ),
                active_user_message=active_user_message,
                legacy_extra_context=_task_runtime_resume_context(
                    runtime.extra_context,
                    metadata,
                ),
                manual_skill_selected=bool(skill_refs),
                extra_tool_schemas=resume_tool_schemas,
                extra_allowed_tool_names=resume_tool_names,
                skill_refs=skill_refs,
                blocked_tool_names=blocked_tool_names,
            )
            return assembled

    async def _finish_rejected_runtime_resume(
        self,
        db,
        *,
        task,
        resume,
        initiative_marker,
        task_start: float,
    ) -> dict[str, Any]:
        """Close an explicit rejection before loading any Agent/LLM runtime."""

        progress_locale = await resolve_response_locale(resume.checkpoint.execution_user_id, db=db) or "en"
        is_initiative = initiative_marker is not None
        final_status = "completed" if is_initiative else "cancelled"
        response = (
            '{"version":"1","decision":"silent",'
            '"reason_code":"runtime_action_rejected","evidence_refs":[]}'
            if is_initiative
            else t("task.execution.action_rejected_response", locale=progress_locale)
        )
        initiative_decision = (
            {
                "version": "1",
                "decision": "silent",
                "reason_code": "runtime_action_rejected",
                "evidence_refs": [],
            }
            if is_initiative
            else None
        )
        initiative_decision_error = None
        supervisor_verdict = {
            "verdict": VERDICT_DONE if is_initiative else VERDICT_FAILED,
            "reason": t("task.execution.action_rejected_response", locale=progress_locale),
        }
        duration_ms = int((time.monotonic() - task_start) * 1000)
        details = dict(task.details or {})
        marker = details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
        if (
            not isinstance(marker, dict)
            or marker.get("hitl_id") != resume.checkpoint.hitl_id
            or marker.get("generation") != resume.checkpoint.generation
        ):
            raise RuntimeError("runtime approval resume marker changed before rejection")
        details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
            **marker,
            "state": "finished",
            "result_status": final_status,
        }
        details["ai_result"] = {
            "agent_id": task.agent_id,
            "agent_name": "AI Agent",
            "response": response,
            "turns_used": resume.checkpoint.turns_used,
            "duration_ms": duration_ms,
            "supervisor_verdict": supervisor_verdict,
            "error_type": None,
            "error_message": None,
        }
        actual_output: dict[str, Any] = {
            "agent_id": task.agent_id,
            "agent_name": "AI Agent",
            "response": response,
            "turns_used": resume.checkpoint.turns_used,
            "duration_ms": duration_ms,
            "verdict": supervisor_verdict["verdict"],
            "error_type": None,
            "error_message": None,
        }
        if is_initiative:
            actual_output["initiative_decision"] = initiative_decision
            actual_output["initiative_decision_error"] = None
            details["ai_result"]["initiative_decision"] = initiative_decision
            details["ai_result"]["initiative_decision_error"] = None
        updated_task, finalization_won = await _finalize_with_terminal_guard(
            db,
            task.id,
            task.entity_id,
            status=final_status,
            actual_output=actual_output,
            details=details,
            log_locale=progress_locale,
        )
        await commit_fenced_execution_boundary(
            db.commit,
            before_commit=self._before_terminal_commit,
            after_commit=self._after_terminal_commit,
            execution_claim=self._execution_claim,
            session=db,
        )
        self._agent_id = task.agent_id
        self._agent_name = "AI Agent"
        if finalization_won:
            await self._log(
                task.id,
                "ai_execution_completed",
                t("task.execution.action_rejected", locale=progress_locale),
            )
        else:
            # Cancellation/recovery may have terminalized the Task while this
            # rejection delivery was in flight.  Return only that committed
            # state; the local rejection projection never became authoritative.
            final_status = updated_task.status
            authoritative_output = dict(updated_task.actual_output or {})
            authoritative_details = dict(updated_task.details or {})
            authoritative_ai_result = authoritative_details.get("ai_result")
            if not isinstance(authoritative_ai_result, dict):
                authoritative_ai_result = {}
            response_value = authoritative_output.get("response")
            response = response_value if isinstance(response_value, str) else None
            supervisor_verdict = authoritative_ai_result.get("supervisor_verdict")
            initiative_decision = authoritative_output.get("initiative_decision")
            initiative_error_value = authoritative_output.get(
                "initiative_decision_error"
            )
            initiative_decision_error = (
                initiative_error_value
                if isinstance(initiative_error_value, str)
                else None
            )
            turns_value = authoritative_output.get("turns_used")
            if isinstance(turns_value, int):
                result_turns_used = turns_value
            else:
                result_turns_used = resume.checkpoint.turns_used
            duration_value = authoritative_output.get("duration_ms")
            if isinstance(duration_value, int):
                duration_ms = duration_value
        if finalization_won:
            result_turns_used = resume.checkpoint.turns_used
        result = {
            "task_id": task.id,
            "status": final_status,
            "response": response,
            "turns_used": result_turns_used,
            "duration_ms": duration_ms,
            "supervisor_verdict": supervisor_verdict,
        }
        if not finalization_won:
            result["stale_finalization_suppressed"] = True
        if is_initiative:
            result["initiative_decision"] = initiative_decision
            result["initiative_decision_error"] = initiative_decision_error
        return result

    async def run(
        self,
        task_id: str,
        agent_id: str | None = None,
        *,
        runtime_hitl_delivery_identity: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Execute agent on task with supervisor review."""
        task_start = time.monotonic()

        async with self._get_session() as db:
            from sqlalchemy import or_, select
            from packages.core.models.task import Task
            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"task_id": task_id, "status": "failed", "error": "task not found"}

            if task.status in _TERMINAL_STATUSES:
                return {
                    "task_id": task_id,
                    "status": task.status,
                    "error": "already terminal",
                }
            task_details = task.details if isinstance(task.details, dict) else {}
            if task.workspace_id:
                from packages.core.models.workspace import Workspace
                from packages.core.services.task_execution_origin import task_has_manual_execution
                from packages.core.services.workspace_autonomy import workspace_allows_manual_work

                workspace = await db.get(Workspace, task.workspace_id)
                if not workspace_allows_manual_work(workspace):
                    return {"task_id": task_id, "status": task.status, "reason": "workspace_unavailable"}
                if workspace.status == "paused" and not task_has_manual_execution(task):
                    return {"task_id": task_id, "status": task.status, "reason": "workspace_autonomy_paused"}
            runtime_hitl_marker = task_details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
            if (
                isinstance(runtime_hitl_marker, dict)
                and runtime_hitl_marker.get("state") == "resume_queued"
                and not isinstance(runtime_hitl_delivery_identity, dict)
            ):
                return {
                    "task_id": task_id,
                    "status": task.status,
                    "duplicate_suppressed": True,
                    "reason": "runtime_approval_resume_identity_required",
                }
            entity_id = task.entity_id
            task_runtime_metadata = _task_runtime_metadata(task)
            from packages.core.services.task_runtime_approvals import (
                TaskRuntimeApprovalError,
                fail_task_runtime_approval_task,
                load_task_runtime_approval_resume,
                mark_task_runtime_approval_execution_ambiguous,
                validate_task_runtime_resume_delivery,
            )

            if (
                isinstance(runtime_hitl_marker, dict)
                and runtime_hitl_marker.get("state") == "resuming"
                and isinstance(runtime_hitl_delivery_identity, dict)
            ):
                exact_abandoned_attempt = await validate_task_runtime_resume_delivery(
                    db,
                    task_id=task_id,
                    agent_id=runtime_hitl_delivery_identity.get("agent_id"),
                    scheduled_run_id=runtime_hitl_delivery_identity.get(
                        "scheduled_run_id"
                    ),
                    scheduled_job_id=runtime_hitl_delivery_identity.get(
                        "scheduled_job_id"
                    ),
                    runtime_hitl_id=str(
                        runtime_hitl_delivery_identity.get("runtime_hitl_id") or ""
                    ),
                    runtime_hitl_generation=runtime_hitl_delivery_identity.get(
                        "runtime_hitl_generation"
                    ),
                    runtime_hitl_checkpoint_digest=str(
                        runtime_hitl_delivery_identity.get(
                            "runtime_hitl_checkpoint_digest"
                        )
                        or ""
                    ),
                    runtime_hitl_checkpoint_signature=str(
                        runtime_hitl_delivery_identity.get(
                            "runtime_hitl_checkpoint_signature"
                        )
                        or ""
                    ),
                    runtime_hitl_resolution_signature=str(
                        runtime_hitl_delivery_identity.get(
                            "runtime_hitl_resolution_signature"
                        )
                        or ""
                    ),
                    allow_resuming=True,
                )
                if not exact_abandoned_attempt:
                    return {
                        "task_id": task_id,
                        "status": task.status,
                        "duplicate_suppressed": True,
                        "reason": "runtime_approval_resume_identity_mismatch",
                    }
                ambiguous_error = (
                    "The approved action may have crossed its side-effect "
                    "boundary before the previous worker stopped. It was not "
                    "replayed automatically."
                )
                await mark_task_runtime_approval_execution_ambiguous(
                    db,
                    task=task,
                    reason=ambiguous_error,
                )
                failed_task = await fail_task_runtime_approval_task(
                    db,
                    task=task,
                    reason="runtime_approval_execution_ambiguous",
                    error_type="RuntimeApprovalExecutionAmbiguous",
                )
                failed_task.actual_output = {
                    **dict(failed_task.actual_output or {}),
                    "error_message": ambiguous_error,
                    "response": ambiguous_error,
                }
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=self._before_terminal_commit,
                    after_commit=self._after_terminal_commit,
                    execution_claim=self._execution_claim,
                    session=db,
                )
                return {
                    "task_id": task_id,
                    "status": failed_task.status,
                    "turns_used": 0,
                    "error_type": "RuntimeApprovalExecutionAmbiguous",
                    "error": ambiguous_error,
                    "response": ambiguous_error,
                }

            try:
                task_runtime_resume = await load_task_runtime_approval_resume(
                    db,
                    task,
                    delivery_identity=runtime_hitl_delivery_identity,
                )
            except TaskRuntimeApprovalError as exc:
                await fail_task_runtime_approval_task(
                    db,
                    task=task,
                    reason=str(exc),
                    error_type=type(exc).__name__,
                )
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=self._before_terminal_commit,
                    after_commit=self._after_terminal_commit,
                    execution_claim=self._execution_claim,
                    session=db,
                )
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            if task_runtime_resume is None:
                # Fresh/recovery deliveries own only the Task lock.  Approval
                # resumes acquire HitlRequest → Task inside the loader above;
                # keeping those paths separate preserves the global lock order.
                task = (await db.execute(
                    select(Task)
                    .where(Task.id == task_id, Task.entity_id == entity_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one()
                if task.status in _TERMINAL_STATUSES:
                    return {
                        "task_id": task_id,
                        "status": task.status,
                        "error": "already terminal",
                    }
                locked_details = (
                    task.details if isinstance(task.details, dict) else {}
                )
                locked_marker = locked_details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
                if isinstance(locked_marker, dict):
                    marker_state = str(locked_marker.get("state") or "")
                    if marker_state == "waiting":
                        if task.status != TaskStatus.WAITING_ON_CUSTOMER:
                            await apply_task_status_transition(
                                task,
                                "waiting_on_customer",
                                db=db,
                            )
                            await commit_fenced_execution_boundary(
                                db.commit,
                                execution_claim=self._execution_claim,
                                session=db,
                            )
                        return {
                            "task_id": task_id,
                            "status": "waiting_on_customer",
                            "waiting_for_runtime_approval": True,
                        }
                    if marker_state in {"resume_queued", "resuming"}:
                        # This delivery was not carrying the signed resume
                        # envelope.  Only that exact broker generation may run.
                        return {
                            "task_id": task_id,
                            "status": task.status,
                            "duplicate_suppressed": True,
                            "reason": "runtime_approval_resume_identity_required",
                        }
            if task_runtime_resume is not None:
                task_runtime_metadata = {
                    **dict(task_runtime_metadata or {}),
                    **task_runtime_resume.runtime_metadata,
                }
            # Final provider-I/O fence under the canonical Task lock. Queue
            # admission and owner changes are only hints; a Plan/effect can be
            # committed while this stale agent delivery is waiting to run.
            task = (await db.execute(
                select(Task)
                .where(Task.id == task_id, Task.entity_id == entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one()
            from packages.core.constants.task import TaskRuntimeKind
            from packages.core.services.task_execution import resolve_task_execution

            runtime_dispatch = await resolve_task_execution(db, task)
            effective_delivery_agent_id = agent_id or task.agent_id
            if (
                runtime_dispatch.runtime_kind is not TaskRuntimeKind.LEGACY_AGENT
                or (
                    runtime_dispatch.target_id
                    and effective_delivery_agent_id
                    and runtime_dispatch.target_id != effective_delivery_agent_id
                )
            ):
                return {
                    "task_id": task.id,
                    "status": task.status,
                    "duplicate_suppressed": True,
                    "reason": "task_runtime_requires_factory",
                    "runtime_kind": runtime_dispatch.runtime_kind.value,
                    "runtime_factory_recheck": True,
                    "runtime_target_id": runtime_dispatch.target_id,
                }
            from packages.core.constants.execution import PLAN_TERMINAL_STATUSES
            from packages.core.dispatcher.effect_safety import (
                task_execution_effect_block_detail,
            )
            from packages.core.models.execution import ExecutionPlan

            active_plan = (await db.execute(
                select(ExecutionPlan.id)
                .where(
                    ExecutionPlan.task_id == task.id,
                    ExecutionPlan.entity_id == task.entity_id,
                    ExecutionPlan.status.notin_(
                        [status.value for status in PLAN_TERMINAL_STATUSES]
                    ),
                )
                .limit(1)
            )).scalar_one_or_none()
            if active_plan is not None:
                return {
                    "task_id": task.id,
                    "status": task.status,
                    "duplicate_suppressed": True,
                    "reason": "active_plan_owns_task_execution",
                    "plan_id": active_plan,
                }
            effect_block = await task_execution_effect_block_detail(db, task.id)
            if effect_block:
                await apply_task_status_transition(
                    task,
                    TaskStatus.FAILED.value,
                    db=db,
                    actor_kind=TaskActor.SYSTEM.value,
                )
                task.actual_output = {
                    "error_type": "TaskEffectReplayBlocked",
                    "error_message": effect_block,
                    "response": effect_block,
                }
                await add_task_log(
                    db,
                    task.id,
                    TaskLogType.AI_EXECUTION_FAILED,
                    effect_block,
                    actor=TaskActor.SYSTEM,
                    created_by="system",
                    metadata={"error_type": "TaskEffectReplayBlocked"},
                )
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=self._before_terminal_commit,
                    after_commit=self._after_terminal_commit,
                    execution_claim=self._execution_claim,
                    session=db,
                )
                return {
                    "task_id": task.id,
                    "status": task.status,
                    "error_type": "TaskEffectReplayBlocked",
                    "error": effect_block,
                    "response": effect_block,
                }
            from packages.core.services.agent_initiative_decision import (
                INITIATIVE_BILLING_SOURCE,
                InitiativeAdmissionRejected,
                has_initiative_task_marker,
                initiative_task_marker,
                mark_initiative_run_started,
            )

            initiative_marker = initiative_task_marker(task.details)
            if has_initiative_task_marker(task.details) and initiative_marker is None:
                await fail_task_runtime_approval_task(
                    db,
                    task=task,
                    reason="initiative_task_marker_invalid",
                    error_type="InitiativeAdmissionRejected",
                )
                await commit_fenced_execution_boundary(
                    db.commit,
                    before_commit=self._before_terminal_commit,
                    after_commit=self._after_terminal_commit,
                    execution_claim=self._execution_claim,
                    session=db,
                )
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error_type": "InitiativeAdmissionRejected",
                    "error": "initiative_task_marker_invalid",
                }
            if task_runtime_resume is not None and task_runtime_resume.choice == "rejected":
                return await self._finish_rejected_runtime_resume(
                    db,
                    task=task,
                    resume=task_runtime_resume,
                    initiative_marker=initiative_marker,
                    task_start=task_start,
                )
            task_user_id = await runtime_resolve_task_billable_user_id(db, task)
            initiative_blocked_tool_names = (
                [
                    str(name)
                    for name in (task.details or {}).get("blocked_tool_names", [])
                    if str(name or "").strip()
                ]
                if initiative_marker is not None
                else []
            )
            if initiative_marker is not None:
                try:
                    await mark_initiative_run_started(db, task)
                except InitiativeAdmissionRejected as exc:
                    await fail_task_runtime_approval_task(
                        db,
                        task=task,
                        reason=exc.reason_code,
                        error_type="InitiativeAdmissionRejected",
                    )
                    await commit_fenced_execution_boundary(
                        db.commit,
                        before_commit=self._before_terminal_commit,
                        after_commit=self._after_terminal_commit,
                        execution_claim=self._execution_claim,
                        session=db,
                    )
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "error_type": "InitiativeAdmissionRejected",
                        "error": exc.reason_code,
                    }
            scheduled_skill_refs: list[dict] = []
            scheduled_skill_id = str(
                (task.details or {}).get("scheduled_skill_id") or ""
            ).strip()
            if scheduled_skill_id:
                from packages.core.models.skill import Skill

                scheduled_skill = (await db.execute(select(Skill).where(
                    Skill.id == scheduled_skill_id,
                    Skill.status == "active",
                    or_(Skill.entity_id == entity_id, Skill.entity_id.is_(None)),
                ))).scalar_one_or_none()
                if scheduled_skill is None:
                    if task.status not in _TERMINAL_STATUSES:
                        await fail_task_runtime_approval_task(
                            db,
                            task=task,
                            reason="scheduled skill not found",
                            error_type="ScheduledSkillNotFound",
                        )
                        await commit_fenced_execution_boundary(
                            db.commit,
                            before_commit=self._before_terminal_commit,
                            after_commit=self._after_terminal_commit,
                            execution_claim=self._execution_claim,
                            session=db,
                        )
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "error": "scheduled skill not found",
                    }
                scheduled_skill_refs = [runtime_skill_ref_dict(scheduled_skill)]

            # Decide which catalog role to use:
            #   worker model (cheap) vs primary model (capable)
            # then resolve via the shared model_resolver. Supervisor
            # always uses the worker role since it's just JSON
            # classification.
            from packages.core.services.model_resolver import (
                resolve_llm_metadata_for_user,
                resolve_model_for_user,
            )
            _model_role = runtime_classify_task_complexity(task)
            resolved_model = await resolve_model_for_user(
                _model_role,
                user_id=task_user_id, entity_id=entity_id, db=db,
            )
            self._engine = runtime_configure_task_engine_model(
                runtime_task_engine(self._engine),
                resolved_model,
            )
            self._worker_model = await resolve_model_for_user(
                "worker",
                user_id=task_user_id, entity_id=entity_id, db=db,
            )
            self._agent_llm_metadata = await resolve_llm_metadata_for_user(
                _model_role,
                user_id=task_user_id,
                entity_id=entity_id,
                db=db,
            )
            self._worker_llm_metadata = await resolve_llm_metadata_for_user(
                "worker",
                user_id=task_user_id,
                entity_id=entity_id,
                db=db,
            )

            # Resolve agent
            effective_agent_id = agent_id or task.agent_id
            agent_name = "AI Agent"
            system_prompt = ""
            runtime_prompt_result = None
            self._agent_temperature = None
            self._agent_max_tokens = None
            from packages.core.constants.agents import is_master_agent, MANOR_AGENT_NAME
            is_master = is_master_agent(effective_agent_id, task.agent_type)
            from packages.core.services.workspace_runtime import resolve_workspace_runtime
            runtime = await resolve_workspace_runtime(
                db,
                entity_id=entity_id,
                user_id=task_user_id,
                agent_id=effective_agent_id,
                workspace_id=task.workspace_id,
                agent_subscription_id=task.owner_subscription_id,
                conversation_id=task.conversation_id,
                task_id=task.id,
                is_master=is_master,
            )

            if is_master:
                # Master agent — build prompt via PromptBuilder, gets all tools
                agent_name = MANOR_AGENT_NAME
                effective_agent_id = None  # TaskRunner uses master defaults
                active_task_text = "\n".join(
                    part for part in (task.title, task.description) if part
                )
                try:
                    runtime_prompt_result = await self._assemble_runtime_prompt(
                        entity_id=entity_id,
                        user_id=task_user_id,
                        agent_id=None,
                        conversation_id=task.conversation_id,
                        runtime=runtime,
                        active_user_message=active_task_text,
                        metadata=task_runtime_metadata,
                        skill_refs=scheduled_skill_refs,
                        blocked_tool_names=initiative_blocked_tool_names,
                    )
                    system_prompt = runtime_prompt_result.prompt
                except Exception as exc:
                    logger.warning("Failed to build master prompt: %s", exc)
                    system_prompt = ""
            elif effective_agent_id:
                agent = await get_agent(db, effective_agent_id)
                if agent:
                    from packages.core.services.agent_runtime_config import (
                        agent_runtime_config_for,
                    )
                    agent_config = agent_runtime_config_for(agent)
                    if agent_config.model:
                        self._engine = runtime_configure_task_engine_model(
                            self._engine,
                            agent_config.model,
                        )
                    self._agent_temperature = agent_config.temperature
                    self._agent_max_tokens = agent_config.max_tokens
                    agent_name = agent.name or "AI Agent"
                    active_task_text = "\n".join(
                        part for part in (task.title, task.description) if part
                    )
                    try:
                        runtime_prompt_result = await self._assemble_runtime_prompt(
                            entity_id=entity_id,
                            user_id=task_user_id,
                            agent_id=effective_agent_id,
                            conversation_id=task.conversation_id,
                            runtime=runtime,
                            active_user_message=active_task_text,
                            metadata=task_runtime_metadata,
                            skill_refs=scheduled_skill_refs,
                            blocked_tool_names=initiative_blocked_tool_names,
                        )
                        system_prompt = runtime_prompt_result.prompt
                    except Exception as exc:
                        logger.warning("Failed to build task agent prompt: %s", exc)
                        system_prompt = ""
                    if not system_prompt:
                        system_prompt = agent.system_prompt or ""
                        if runtime.extra_context:
                            try:
                                fallback_request = runtime_request_for_surface_turn(
                                    surface=ChatSurface.SCHEDULED_AGENT_RUN,
                                    entity_id=entity_id,
                                    user_id=task_user_id,
                                    agent_id=effective_agent_id,
                                    workspace_id=runtime.workspace_id,
                                    conversation_id=task.conversation_id,
                                    task_id=task.id,
                                    thread_ref_kind=runtime.thread_ref_kind,
                                    thread_ref_id=runtime.thread_ref_id,
                                    message=active_task_text,
                                    legacy_path="ai.task_runner.agent_prompt_fallback",
                                    metadata=task_runtime_metadata,
                                )
                                fallback_appendix = await runtime_prepare_context_appendix_for_turn(
                                    db,
                                    request=fallback_request,
                                    tool_profile=runtime.tool_profile,
                                    legacy_extra_context=runtime.extra_context,
                                    skill_refs=scheduled_skill_refs,
                                )
                                system_prompt = runtime_merge_prompt_appendix(
                                    system_prompt,
                                    fallback_appendix.context_section,
                                )
                            except Exception:
                                logger.debug("Failed to render runtime fallback context", exc_info=True)

            logger.info(
                "Task %s model=%s (role=%s), supervisor=%s",
                task_id,
                runtime_task_engine_model(self._engine),
                _model_role,
                self._worker_model,
            )

            task_dict = {
                "id": task.id, "title": task.title, "description": task.description,
                "priority": task.priority, "task_type": task.task_type,
                "details": dict(task.details or {}), "status": task.status,
                "workspace_id": task.workspace_id,
                "conversation_id": task.conversation_id,
                "creator_id": task.creator_id,
                "owner_service_key": task.owner_service_key,
                "owner_subscription_id": task.owner_subscription_id,
                "delegate_service_keys": list(task.delegate_service_keys or []),
                "started_at": task.started_at,
            }

            # Get done_when criteria from task details
            done_when = str((task.details or {}).get("done_when", "")).strip()

            await apply_task_status_transition(task, "in_progress", db=db)
            await commit_fenced_execution_boundary(
                db.commit,
                execution_claim=self._execution_claim,
                session=db,
            )

        # Stash for ``_log`` so subsequent log entries carry agent context
        # (id + name) in their metadata. UI uses this to resolve the
        # actual agent's avatar + name instead of "AI Agent" placeholder.
        self._agent_id = effective_agent_id
        self._agent_name = agent_name

        progress_locale = await resolve_response_locale(task_user_id) or "en"
        await self._log(
            task_id, "ai_execution_started",
            t("task.execution.started", locale=progress_locale, agent_name=agent_name),
        )

        # ── Multi-turn execution loop with supervisor ────────────────────
        user_prompt = runtime_task_ticket_prompt(task_dict)
        if runtime_prompt_result is None:
            resume_tool_names = _task_runtime_resume_tool_names(
                task_runtime_metadata
            )
            if resume_tool_names:
                from packages.core.ai.runtime.tool_registry import (
                    runtime_tool_schemas_for_names,
                )

                resume_tool_schemas = runtime_tool_schemas_for_names(
                    sorted(resume_tool_names)
                )
            else:
                resume_tool_schemas = []
            runtime_request = runtime_request_for_surface_turn(
                surface=ChatSurface.SCHEDULED_AGENT_RUN,
                entity_id=entity_id,
                user_id=task_user_id,
                agent_id=effective_agent_id,
                workspace_id=task_dict.get("workspace_id"),
                conversation_id=task_dict.get("conversation_id"),
                task_id=task_id,
                thread_ref_kind=runtime.thread_ref_kind,
                thread_ref_id=runtime.thread_ref_id,
                message=user_prompt,
                legacy_path="ai.task_runner.prompt_fallback",
                metadata=task_runtime_metadata,
            )
            async with self._get_session() as prompt_db:
                runtime_prompt_result = await runtime_prepare_prompt_appendix_for_turn(
                    prompt_db,
                    request=runtime_request,
                    tool_profile=runtime.tool_profile,
                    agent_id=effective_agent_id,
                    bound_tool_names=runtime.bound_tool_names,
                    is_master=runtime.is_master,
                    mcp_allowed_names=runtime.mcp_allowed_names,
                    mcp_provider_scopes=getattr(runtime, "mcp_provider_scopes", ()),
                    mcp_scope_unrestricted=bool(
                        getattr(runtime, "mcp_scope_unrestricted", False)
                    ),
                    active_user_message=user_prompt,
                    legacy_extra_context=_task_runtime_resume_context(
                        runtime.extra_context,
                        task_runtime_metadata,
                    ),
                    manual_skill_selected=bool(scheduled_skill_refs),
                    extra_tool_schemas=resume_tool_schemas,
                    extra_allowed_tool_names=resume_tool_names,
                    skill_refs=scheduled_skill_refs,
                    blocked_tool_names=initiative_blocked_tool_names,
                )
        tools = list(runtime_prompt_result.tool_schemas)
        allowed_tool_names = set(runtime_prompt_result.allowed_tool_names)
        runtime_envelope = runtime_prompt_result.envelope
        self._runtime_envelope = runtime_envelope

        task_logs = []
        try:
            async with self._get_session() as log_db:
                from packages.core.services.task_service import get_task_logs
                task_logs = await get_task_logs(log_db, task_id)
        except Exception:
            pass  # non-fatal — proceed without history

        hitl_response = (task_dict.get("details") or {}).get("_hitl_response")
        hitl_by = (task_dict.get("details") or {}).get("_hitl_responded_by", "Human")
        hitl_question = (task_dict.get("details") or {}).get(TASK_INPUT_QUESTION_KEY)
        user_prompt_result = runtime_task_user_prompt(
            base_prompt=user_prompt,
            task_logs=task_logs,
            hitl_response=hitl_response,
            hitl_responded_by=hitl_by,
            hitl_question=hitl_question,
            hitl_payload=(task_dict.get("details") or {}).get("_hitl_payload"),
        )
        user_prompt = user_prompt_result.prompt
        if user_prompt_result.hitl_response:
            await self._log(
                task_id, "ai_hitl_resumed",
                t("task.execution.hitl_resumed", locale=progress_locale, response=user_prompt_result.hitl_response[:200]),
            )

        max_turns = int((task_dict.get("details") or {}).get("max_turns") or _DEFAULT_MAX_TURNS)
        full_system_prompt = runtime_task_system_prompt(system_prompt)

        # All LLM calls (agent + supervisor) auto-record usage via billing context
        billing_kwargs = dict(
            entity_id=entity_id,
            user_id=task_user_id,
            agent_id=effective_agent_id,
            workspace_id=task_dict.get("workspace_id"),
            conversation_id=task_dict.get("conversation_id"),
        )
        if initiative_marker is not None:
            billing_kwargs["source"] = INITIATIVE_BILLING_SOURCE
        _billing_cm = runtime_task_llm_billing_context(**billing_kwargs)
        await _billing_cm.__aenter__()

        if task_runtime_resume is not None:
            messages = task_runtime_resume.messages
        else:
            messages = runtime_task_initial_messages(user_prompt)
        scheduled_forced_tool_calls = runtime_manual_skill_forced_tool_calls(
            scheduled_skill_refs,
            user_prompt,
        )
        if task_runtime_resume is not None:
            resumed_forced_calls = task_runtime_resume.runtime_metadata.get(
                "forced_tool_calls"
            )
            scheduled_forced_tool_calls = (
                [
                    dict(call)
                    for call in resumed_forced_calls
                    if isinstance(call, dict)
                ]
                if isinstance(resumed_forced_calls, list)
                else []
            )
            if (
                not task_runtime_resume.checkpoint.disable_followup_tools
                and not task_runtime_resume.runtime_metadata.get("approved_tool_call")
                and all(call.get("disable_followup_tools") is False for call in scheduled_forced_tool_calls)
            ):
                scheduled_forced_tool_calls.extend(
                    dict(call)
                    for call in task_runtime_resume.checkpoint.remaining_tool_calls
                )
        agent_response = ""
        turns_used = (
            task_runtime_resume.checkpoint.turns_used
            if task_runtime_resume is not None
            else 0
        )
        final_status = "failed"
        input_request: Optional[Dict[str, Any]] = None
        supervisor_verdict: Dict[str, Any] = {}
        blocking_human_gate: dict[str, Any] | None = None
        pending_human_gate_control: dict[str, Any] | None = None
        pending_disable_followup_tools = True
        blocking_tool_arguments: dict[str, Any] | None = None
        remaining_tool_calls: list[dict[str, Any]] = []
        terminal_control: dict[str, Any] | None = None
        tools_called_session: list[str] = (
            list(task_runtime_resume.checkpoint.tool_calls_made)
            if task_runtime_resume is not None
            else []
        )
        tool_evidence_session: list[dict[str, str]] = (
            list(task_runtime_resume.checkpoint.supervisor_evidence)
            if task_runtime_resume is not None
            else []
        )
        if user_prompt_result.hitl_response and hitl_question:
            # A resumed run: the request_input call happened in the previous
            # run. Replay it as supervisor evidence so the review judges the
            # answer-driven continuation, not a reply that "never asked".
            tools_called_session.append(REQUEST_INPUT_TOOL_NAME)
            tool_evidence_session.append(
                _hitl_supervisor_evidence(
                    hitl_question,
                    user_prompt_result.hitl_response,
                    user_prompt_result.hitl_by,
                )
            )
        loaded_tool_names = {
            t.get("function", {}).get("name")
            for t in tools
            if isinstance(t, dict)
        }
        resume_usage = (
            task_runtime_resume.checkpoint.usage
            if task_runtime_resume is not None
            else {}
        )
        _total_input_tokens = int(
            resume_usage.get("prompt_tokens")
            or resume_usage.get("input_tokens")
            or 0
        )
        _total_output_tokens = int(
            resume_usage.get("completion_tokens")
            or resume_usage.get("output_tokens")
            or 0
        )

        for turn in range(max_turns):
            turns_used += 1

            # ── Agent turn ──
            try:
                turn_result = await runtime_execute_task_agent_turn(
                    engine=self._engine,
                    messages=messages,
                    tools=tools,
                    loaded_tool_names=loaded_tool_names,
                    system_prompt=full_system_prompt,
                    runtime_envelope=runtime_envelope,
                    entity_id=entity_id,
                    user_id=task_user_id,
                    agent_id=effective_agent_id,
                    workspace_id=task_dict.get("workspace_id"),
                    conversation_id=task_dict.get("conversation_id"),
                    task_id=task_id,
                    active_user_message=user_prompt,
                    tool_profile=runtime.tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    metadata=getattr(self, "_agent_llm_metadata", None),
                    temperature=getattr(self, "_agent_temperature", None),
                    max_tokens=getattr(self, "_agent_max_tokens", None),
                    forced_tool_calls=(
                        scheduled_forced_tool_calls if turn == 0 else None
                    ),
                    performance_tool_call_id=f"task-agent-{turns_used}",
                )
                messages = turn_result.messages
                tools = turn_result.tools
                loaded_tool_names = turn_result.loaded_tool_names

                # Accumulate token usage
                if turn_result.usage:
                    _total_input_tokens += int(turn_result.usage.get("prompt_tokens") or 0)
                    _total_output_tokens += int(turn_result.usage.get("completion_tokens") or 0)

                if turn_result.blocking_control is not None:
                    pending_human_gate_control = dict(turn_result.blocking_control)
                    pending_disable_followup_tools = turn_result.disable_followup_tools
                    blocking_tool_arguments = dict(
                        getattr(turn_result, "blocking_tool_arguments", None) or {}
                    )
                    remaining_tool_calls = list(
                        getattr(turn_result, "remaining_tool_calls", None) or []
                    )
                    blocking_human_gate = {
                        "kind": "approval_required",
                        "tool_name": str(
                            turn_result.blocking_control.get("tool_name") or ""
                        ),
                    }
                    tools_called_session.extend(turn_result.tool_names)
                    tool_evidence_session.extend(turn_result.supervisor_evidence)
                    supervisor_verdict = {
                        "verdict": VERDICT_NEEDS_HITL,
                        "reason": (
                            "Runtime approval required before executing "
                            f"{blocking_human_gate['tool_name'] or 'the requested action'}"
                        ),
                        "tool_name": blocking_human_gate["tool_name"] or None,
                    }
                    final_status = "waiting_on_customer"
                    await self._log(
                        task_id,
                        "ai_agent_turn",
                        t("task.execution.turn_waiting_approval", locale=progress_locale,
                          turn=turns_used, max_turns=max_turns,
                          tool=blocking_human_gate['tool_name'] or t("task.execution.unknown_tool", locale=progress_locale)),
                    )
                    break

                if turn_result.terminal_control is not None:
                    terminal_control = dict(turn_result.terminal_control)
                    tools_called_session.extend(turn_result.tool_names)
                    tool_evidence_session.extend(turn_result.supervisor_evidence)
                    agent_response = turn_result.response_text
                    supervisor_verdict = {
                        "verdict": VERDICT_FAILED,
                        "reason": agent_response or "uncertain_result",
                        "retry_strategy": "do_not_retry",
                    }
                    final_status = "failed"
                    await self._log(
                        task_id,
                        "ai_agent_turn",
                        f"[AI] Turn {turns_used}/{max_turns} FAILED — uncertain mutation result",
                    )
                    break

                if turn_result.had_tool_calls:
                    tools_called_session.extend(turn_result.tool_names)
                    tool_evidence_session.extend(turn_result.supervisor_evidence)
                    await self._log(task_id, "ai_agent_turn",
                        t("task.execution.turn_tools", locale=progress_locale,
                          turn=turns_used, max_turns=max_turns, tools=', '.join(turn_result.tool_names)))
                    if turn_result.input_request is not None:
                        # The agent asked the user something (request_input).
                        # Park the task on the question now — no supervisor
                        # pass, no further turns; the answer re-dispatches
                        # the runner with the question + answer in context.
                        input_request = dict(turn_result.input_request)
                        agent_response = _input_request_text(input_request)
                        supervisor_verdict = {
                            "verdict": VERDICT_NEEDS_HITL,
                            "reason": "agent asked the user a question",
                        }
                        final_status = "waiting_on_customer"
                        break
                    continue  # Don't supervisor-review tool calls, go to next turn

                agent_response = turn_result.response_text
                await self._log(task_id, "ai_agent_turn",
                    t("task.execution.turn_response", locale=progress_locale,
                      turn=turns_used, max_turns=max_turns, chars=len(agent_response)))

            except (
                CreditExhaustedError,
                LLMRateLimited,
                TaskRequesterIdentityError,
            ) as e:
                await _close_task_billing_context_for_error(_billing_cm, e)
                raise
            except RuntimeTaskProviderError as e:
                if e.retryable:
                    await _close_task_billing_context_for_error(_billing_cm, e)
                    raise
                agent_response = f"Agent execution failed: {e}"
                logger.error(
                    "TaskRunner: terminal provider error on turn %d: %s",
                    turns_used,
                    e,
                )
                await self._log(
                    task_id,
                    "ai_agent_turn",
                    t("task.execution.turn_failed", locale=progress_locale,
                      turn=turns_used, max_turns=max_turns, error=str(e)),
                )
            except Exception as e:
                agent_response = f"Agent execution failed: {e}"
                logger.error("TaskRunner: turn %d failed: %s", turns_used, e, exc_info=True)
                await self._log(task_id, "ai_agent_turn",
                    t("task.execution.turn_failed", locale=progress_locale,
                      turn=turns_used, max_turns=max_turns, error=str(e)))

            # ── Check for known error responses before supervisor ──
            _ERROR_MARKERS = ("sorry, the request failed", "agent execution failed", "error:")
            if any(agent_response.lower().startswith(m) for m in _ERROR_MARKERS):
                supervisor_verdict = {"verdict": VERDICT_FAILED, "reason": f"Agent returned error: {agent_response[:200]}", "retry_strategy": "retry_same"}
            else:
                # ── Supervisor review ──
                try:
                    supervisor_verdict = await self._supervise(
                        task_id=task_id,
                        task_title=task_dict["title"],
                        agent_response=agent_response,
                        done_when=done_when,
                        turns_used=turns_used,
                        max_turns=max_turns,
                        tools_called=tools_called_session,
                        tool_evidence=tool_evidence_session,
                    )
                except (
                    CreditExhaustedError,
                    LLMRateLimited,
                    RuntimeTaskProviderError,
                    TaskRequesterIdentityError,
                ) as exc:
                    await _close_task_billing_context_for_error(_billing_cm, exc)
                    raise

            verdict = supervisor_verdict.get("verdict", VERDICT_DONE)
            await self._log(task_id, "ai_supervisor_verdict",
                t("task.execution.supervisor_verdict", locale=progress_locale,
                  verdict=t(f"task.verdict.{verdict}", locale=progress_locale),
                  reason=supervisor_verdict.get('reason', '')[:200]))

            if verdict == VERDICT_DONE:
                final_status = "completed"
                break
            elif verdict == VERDICT_FAILED:
                final_status = "failed"
                break
            elif verdict == VERDICT_NEEDS_HITL:
                final_status = "waiting_on_customer"
                break
            elif verdict == VERDICT_NEEDS_REPLAN:
                final_status = "blocked"
                break
            elif verdict == VERDICT_CONTINUE:
                # Inject supervisor instruction for next turn
                instruction = supervisor_verdict.get("instruction", "Continue working on the task.")
                messages.append(runtime_task_supervisor_feedback_message(instruction))
                continue
            else:
                final_status = "completed"
                break
        else:
            # Max turns exhausted — agent never produced a final text the
            # supervisor could approve. Run one final supervisor pass on
            # whatever artefacts we have (last assistant text, otherwise a
            # synthetic summary of tool calls) and respect its verdict
            # instead of hard-coding "completed". Default to "blocked"
            # when no judgement is reachable — running out of turns is
            # not a successful outcome.
            try:
                final_response = await runtime_execute_task_final_response(
                    engine=self._engine,
                    messages=messages,
                    system_prompt=full_system_prompt,
                    metadata=getattr(self, "_agent_llm_metadata", None),
                    temperature=getattr(self, "_agent_temperature", None),
                    max_tokens=getattr(self, "_agent_max_tokens", None),
                    performance_run_id=task_id,
                    performance_tool_call_id=f"task-final-{turns_used + 1}",
                )
                messages = final_response.messages
                if final_response.usage:
                    _total_input_tokens += int(final_response.usage.get("prompt_tokens") or 0)
                    _total_output_tokens += int(final_response.usage.get("completion_tokens") or 0)
                if final_response.finalized:
                    agent_response = final_response.response_text
                    await self._log(
                        task_id,
                        "ai_agent_turn",
                        t("task.execution.finalized", locale=progress_locale, chars=len(agent_response)),
                    )
            except (
                CreditExhaustedError,
                LLMRateLimited,
                TaskRequesterIdentityError,
            ) as exc:
                await _close_task_billing_context_for_error(_billing_cm, exc)
                raise
            except RuntimeTaskProviderError as exc:
                if exc.retryable:
                    await _close_task_billing_context_for_error(_billing_cm, exc)
                    raise
                logger.warning(
                    "TaskRunner: terminal provider error while finalizing after max turns: %s",
                    exc,
                )
            except Exception as exc:
                logger.warning("TaskRunner: final response after max turns failed: %s", exc)

            if not agent_response:
                for msg in reversed(messages):
                    if msg.role == "assistant" and msg.content:
                        agent_response = msg.content
                        break
            if not agent_response:
                # Tool-only run with no final text. Synthesize a summary so
                # the supervisor has *something* to judge (and the comment
                # log isn't empty).
                tool_msgs = [m for m in messages if m.role == "tool"]
                agent_response = (
                    f"[no final response] Agent made {len(tool_msgs)} tool "
                    f"call(s) across {turns_used} turn(s) but never produced "
                    f"a text reply explaining what was done."
                )

            try:
                supervisor_verdict = await self._supervise(
                    task_id=task_id,
                    task_title=task_dict["title"],
                    agent_response=agent_response,
                    done_when=done_when,
                    turns_used=turns_used,
                    max_turns=max_turns,
                    tools_called=tools_called_session,
                    tool_evidence=tool_evidence_session,
                )
            except (
                CreditExhaustedError,
                LLMRateLimited,
                RuntimeTaskProviderError,
                TaskRequesterIdentityError,
            ) as exc:
                await _close_task_billing_context_for_error(_billing_cm, exc)
                raise
            except Exception as exc:
                logger.warning("Supervisor at exhaustion failed: %s", exc)
                supervisor_verdict = {
                    "verdict": VERDICT_NEEDS_REPLAN,
                    "reason": f"Supervisor unreachable at exhaustion: {exc}",
                }

            verdict = supervisor_verdict.get("verdict", VERDICT_NEEDS_REPLAN)
            await self._log(
                task_id, "ai_supervisor_verdict",
                t("task.execution.supervisor_exhaustion", locale=progress_locale,
                  verdict=t(f"task.verdict.{verdict}", locale=progress_locale),
                  reason=supervisor_verdict.get('reason', '')[:200]),
            )

            if verdict == VERDICT_DONE:
                final_status = "completed"
            elif verdict == VERDICT_FAILED:
                final_status = "failed"
            elif verdict == VERDICT_NEEDS_HITL:
                final_status = "waiting_on_customer"
            else:
                # CONTINUE / NEEDS_REPLAN / unknown all mean "we ran out of
                # turns but more work was needed" — that's blocked, not done.
                final_status = "blocked"

        # Initiative output is a security boundary, not presentation text.
        # Accept strict JSON only; one no-tools repair is allowed and a second
        # failure closes the Task without projecting the raw response.
        initiative_decision = None
        initiative_decision_error = None
        initiative_waiting_for_hitl = (
            initiative_marker is not None
            and final_status == "waiting_on_customer"
        )
        if (
            initiative_marker is not None
            and not initiative_waiting_for_hitl
            and terminal_control is None
        ):
            from packages.core.services.agent_initiative_decision import (
                initiative_decision_repair_prompt,
                parse_initiative_decision,
            )

            initiative_decision, initiative_decision_error = (
                parse_initiative_decision(agent_response)
            )
            provider_failed = str(agent_response or "").lower().startswith(
                ("agent execution failed", "sorry, the request failed")
            )
            if initiative_decision is None and not provider_failed:
                messages.append(runtime_task_followup_message(
                    initiative_decision_repair_prompt(
                        initiative_decision_error or "invalid output"
                    )
                ))
                try:
                    repaired = await runtime_execute_task_final_response(
                        engine=self._engine,
                        messages=messages,
                        system_prompt=full_system_prompt,
                        metadata=getattr(self, "_agent_llm_metadata", None),
                        temperature=getattr(self, "_agent_temperature", None),
                        max_tokens=getattr(self, "_agent_max_tokens", None),
                        performance_run_id=task_id,
                        performance_tool_call_id=(
                            f"task-initiative-repair-{turns_used + 1}"
                        ),
                    )
                    messages = repaired.messages
                    if repaired.usage:
                        _total_input_tokens += int(
                            repaired.usage.get("prompt_tokens") or 0
                        )
                        _total_output_tokens += int(
                            repaired.usage.get("completion_tokens") or 0
                        )
                    if repaired.finalized:
                        agent_response = repaired.response_text
                    initiative_decision, initiative_decision_error = (
                        parse_initiative_decision(agent_response)
                    )
                except (
                    CreditExhaustedError,
                    LLMRateLimited,
                    TaskRequesterIdentityError,
                ) as exc:
                    await _close_task_billing_context_for_error(_billing_cm, exc)
                    raise
                except RuntimeTaskProviderError as exc:
                    if exc.retryable:
                        await _close_task_billing_context_for_error(_billing_cm, exc)
                        raise
                    initiative_decision_error = str(exc)
                except Exception as exc:
                    initiative_decision_error = str(exc)

            if initiative_decision is None:
                final_status = "failed"
                supervisor_verdict = {
                    "verdict": VERDICT_FAILED,
                    "reason": (
                        "initiative_output_schema_invalid: "
                        f"{initiative_decision_error or 'invalid output'}"
                    )[:2_000],
                }
            else:
                # Schema validity is the Initiative completion contract. The
                # service layer still applies mute/dedupe/cooldown/rate gates.
                final_status = "completed"
                supervisor_verdict = {
                    "verdict": VERDICT_DONE,
                    "reason": "valid Initiative decision",
                }
        elif initiative_waiting_for_hitl:
            # An Initiative does not pre-authorize future external writes.
            # Preserve the ordinary Task HITL state and parse/settle only when
            # the approved Task resumes and produces its final decision.
            initiative_decision_error = "initiative_waiting_on_customer"

        # ── Update task in DB ────────────────────────────────────────────
        duration_ms = int((time.monotonic() - task_start) * 1000)
        error_message = (
            supervisor_verdict.get("reason")
            if supervisor_verdict and final_status in ("failed", "blocked")
            else None
        )
        error_type = (
            "RuntimeTerminalUncertainty"
            if terminal_control is not None
            else "SupervisorRejected" if error_message else None
        )

        actual_output = {
            "agent_id": effective_agent_id,
            "agent_name": agent_name,
            "response": agent_response[:5000] if agent_response else None,
            "turns_used": turns_used,
            "duration_ms": duration_ms,
            "verdict": supervisor_verdict.get("verdict") if supervisor_verdict else final_status,
            "error_type": error_type,
            "error_message": error_message,
        }
        if terminal_control is not None:
            actual_output["terminal_control"] = terminal_control
        if initiative_marker is not None:
            actual_output["initiative_decision"] = (
                initiative_decision.model_dump(mode="json", exclude_none=True)
                if initiative_decision is not None
                else None
            )
            actual_output["initiative_decision_error"] = initiative_decision_error
        if blocking_human_gate is not None:
            actual_output["human_gate"] = blocking_human_gate
        updated_details = {
            **(task_dict.get("details") or {}),
            # The question lives on the task only while it is parked on it:
            # a finished run (or one parked by the supervisor instead) must
            # not replay an old question on the next resume.
            TASK_INPUT_QUESTION_KEY: input_request,
            **({"_hitl_response": None} if input_request is not None else {}),
            "ai_result": {
                "agent_id": effective_agent_id,
                "agent_name": agent_name,
                "response": agent_response[:10000],
                "turns_used": turns_used,
                "duration_ms": duration_ms,
                "supervisor_verdict": supervisor_verdict,
                "error_type": error_type,
                "error_message": error_message,
            },
        }
        if terminal_control is not None:
            updated_details["ai_result"]["terminal_control"] = terminal_control
        if initiative_marker is not None:
            updated_details["ai_result"]["initiative_decision"] = (
                actual_output["initiative_decision"]
            )
            updated_details["ai_result"]["initiative_decision_error"] = (
                initiative_decision_error
            )
        if blocking_human_gate is not None:
            updated_details["ai_result"]["human_gate"] = blocking_human_gate
        if task_runtime_resume is not None and pending_human_gate_control is None:
            resume_marker = updated_details.get(TASK_RUNTIME_HITL_DETAIL_KEY)
            if (
                isinstance(resume_marker, dict)
                and resume_marker.get("hitl_id")
                == task_runtime_resume.checkpoint.hitl_id
                and resume_marker.get("generation")
                == task_runtime_resume.checkpoint.generation
            ):
                updated_details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
                    **resume_marker,
                    "state": "finished",
                    "result_status": final_status,
                }

        hitl_message_projection: dict[str, Any] | None = None
        async with self._get_session() as db:
            if pending_human_gate_control is not None:
                from sqlalchemy import select

                from packages.core.models.task import Task
                from packages.core.services.task_runtime_approvals import (
                    persist_task_runtime_approval_gate,
                    task_runtime_hitl_details,
                )

                checkpoint_task = (await db.execute(
                    select(Task)
                    .where(
                        Task.id == task_id,
                        Task.entity_id == entity_id,
                    )
                    .execution_options(populate_existing=True)
                )).scalar_one()
                try:
                    # Keep partial provider registration/card writes inside a
                    # savepoint.  If checkpoint construction fails, the outer
                    # transaction remains usable for the fail-closed Task
                    # terminal update and cannot commit an orphan approval.
                    async with db.begin_nested():
                        gate_projection = await persist_task_runtime_approval_gate(
                            db,
                            task=checkpoint_task,
                            execution_user_id=task_user_id,
                            blocking_control=pending_human_gate_control,
                            tool_arguments=blocking_tool_arguments,
                            messages=messages,
                            usage={
                                "prompt_tokens": _total_input_tokens,
                                "completion_tokens": _total_output_tokens,
                            },
                            turns_used=turns_used,
                            tool_calls_made=tools_called_session,
                            supervisor_evidence=tool_evidence_session,
                            remaining_tool_calls=remaining_tool_calls,
                            disable_followup_tools=pending_disable_followup_tools,
                        )
                except Exception as exc:
                    logger.exception(
                        "TaskRunner: could not persist runtime approval checkpoint "
                        "for task %s",
                        task_id,
                    )
                    final_status = "failed"
                    blocking_human_gate = None
                    error_message = (
                        "runtime_approval_checkpoint_failed: "
                        f"{type(exc).__name__}"
                    )
                    supervisor_verdict = {
                        "verdict": VERDICT_FAILED,
                        "reason": error_message,
                    }
                    actual_output.update({
                        "verdict": VERDICT_FAILED,
                        "error_type": "RuntimeApprovalCheckpointError",
                        "error_message": error_message,
                    })
                    if initiative_marker is not None:
                        initiative_decision_error = error_message
                        actual_output["initiative_decision_error"] = error_message
                    updated_details["ai_result"].update({
                        "supervisor_verdict": supervisor_verdict,
                        "error_type": "RuntimeApprovalCheckpointError",
                        "error_message": error_message,
                    })
                    if initiative_marker is not None:
                        updated_details["ai_result"][
                            "initiative_decision_error"
                        ] = error_message
                    actual_output.pop("human_gate", None)
                    updated_details["ai_result"].pop("human_gate", None)
                    from packages.core.services.task_runtime_approvals import (
                        expire_task_runtime_approval_after_checkpoint_failure,
                    )

                    await expire_task_runtime_approval_after_checkpoint_failure(
                        db,
                        task=checkpoint_task,
                        blocking_control=pending_human_gate_control,
                    )
                else:
                    if gate_projection.get("kind") == "origin_terminal":
                        await commit_fenced_execution_boundary(
                            db.commit,
                            execution_claim=self._execution_claim,
                            session=db,
                        )
                        return {
                            "task_id": task_id,
                            "status": gate_projection.get("task_status")
                            or checkpoint_task.status,
                            "error": "already terminal",
                        }
                    blocking_human_gate = {
                        key: gate_projection.get(key)
                        for key in (
                            "kind",
                            "tool_name",
                            "hitl_id",
                            "message_id",
                            "generation",
                            "state",
                        )
                    }
                    actual_output["human_gate"] = blocking_human_gate
                    updated_details["ai_result"]["human_gate"] = (
                        blocking_human_gate
                    )
                    updated_details[TASK_RUNTIME_HITL_DETAIL_KEY] = (
                        task_runtime_hitl_details(gate_projection)
                    )
                    hitl_message_projection = {
                        "message_id": gate_projection.get("message_id"),
                        "conversation_id": task_dict.get("conversation_id"),
                    }
            if (
                task_runtime_resume is not None
                and task_runtime_resume.choice == "approved"
                and pending_human_gate_control is None
            ):
                from packages.core.services.task_runtime_approvals import (
                    complete_task_runtime_approval_execution,
                )

                try:
                    await complete_task_runtime_approval_execution(
                        db,
                        task_id=task_id,
                        checkpoint=task_runtime_resume.checkpoint,
                        result_status=final_status,
                        result_payload=actual_output,
                    )
                except TaskRuntimeApprovalError as exc:
                    # A model/runtime adapter that skipped the exact approved
                    # call must not be allowed to turn the Task green.  Close
                    # any still-live grant and persist a truthful failure.
                    receipt_error = (
                        "runtime_approval_execution_receipt_failed: "
                        f"{type(exc).__name__}: {exc}"
                    )[:2_000]
                    final_status = "failed"
                    actual_output.update({
                        "verdict": VERDICT_FAILED,
                        "error_type": "RuntimeApprovalExecutionReceiptError",
                        "error_message": receipt_error,
                        "response": receipt_error,
                    })
                    updated_details["ai_result"].update({
                        "supervisor_verdict": {
                            "verdict": VERDICT_FAILED,
                            "reason": receipt_error,
                        },
                        "error_type": "RuntimeApprovalExecutionReceiptError",
                        "error_message": receipt_error,
                        "response": receipt_error,
                    })
            updated_task, finalization_won = await _finalize_with_terminal_guard(
                db, task_id, entity_id,
                status=final_status,
                actual_output=actual_output,
                details=updated_details,
                log_locale=progress_locale,
            )
            if not finalization_won:
                # The row returned by the compare-and-set guard is the only
                # committed truth.  Never publish the local model response or
                # settle a ScheduledRun from the losing worker's stale result.
                final_status = updated_task.status
                actual_output = dict(updated_task.actual_output or {})
                authoritative_details = dict(updated_task.details or {})
                authoritative_ai_result = authoritative_details.get("ai_result")
                if not isinstance(authoritative_ai_result, dict):
                    authoritative_ai_result = {}
                response_value = actual_output.get("response")
                agent_response = (
                    response_value if isinstance(response_value, str) else None
                )
                verdict_value = authoritative_ai_result.get("supervisor_verdict")
                supervisor_verdict = (
                    verdict_value if isinstance(verdict_value, dict) else None
                )
                error_value = actual_output.get("error_message")
                error_message = error_value if isinstance(error_value, str) else None
                terminal_value = actual_output.get("terminal_control")
                terminal_control = (
                    terminal_value if isinstance(terminal_value, dict) else None
                )
                turns_value = actual_output.get("turns_used")
                if isinstance(turns_value, int):
                    turns_used = turns_value
                duration_value = actual_output.get("duration_ms")
                if isinstance(duration_value, int):
                    duration_ms = duration_value
                initiative_decision_error_value = actual_output.get(
                    "initiative_decision_error"
                )
                initiative_decision_error = (
                    initiative_decision_error_value
                    if isinstance(initiative_decision_error_value, str)
                    else None
                )
                blocking_human_gate = None
                hitl_message_projection = None
            if finalization_won:
                try:
                    await runtime_record_task_runner_execution_evidence(
                        db,
                        entity_id=entity_id,
                        workspace_id=task_dict.get("workspace_id"),
                        task_id=task_id,
                        plan_id=None,
                        task_status=(
                            getattr(updated_task, "status", None) or final_status
                        ),
                        plan_status=None,
                        task_title=str(task_dict.get("title") or ""),
                        task_description=str(task_dict.get("description") or ""),
                        owner_service_key=task_dict.get("owner_service_key"),
                        delegate_service_keys=list(
                            task_dict.get("delegate_service_keys") or []
                        ),
                        agent_id=effective_agent_id,
                        steps=[],
                        actual_output=actual_output,
                        cost_tracking={},
                        started_at=task_dict.get("started_at"),
                        completed_at=None,
                    )
                except Exception:
                    logger.debug(
                        "TaskRunner: runtime evidence recording skipped for task %s",
                        task_id,
                        exc_info=True,
                    )
            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=self._before_terminal_commit,
                after_commit=self._after_terminal_commit,
                execution_claim=self._execution_claim,
                session=db,
            )
        await runtime_persist_task_runner_runtime_events(
            getattr(self, "_runtime_envelope", None),
        )
        if hitl_message_projection is not None:
            from packages.core.services.task_runtime_approvals import (
                publish_task_runtime_approval_message,
            )

            try:
                await publish_task_runtime_approval_message(
                    entity_id=entity_id,
                    owner_user_id=task_dict.get("creator_id"),
                    workspace_id=task_dict.get("workspace_id"),
                    message_id=hitl_message_projection.get("message_id"),
                    conversation_id=hitl_message_projection.get("conversation_id"),
                )
            except Exception:
                logger.debug(
                    "TaskRunner: runtime approval realtime push skipped for task %s",
                    task_id,
                    exc_info=True,
                )

        event_type = None
        if final_status == "completed":
            event_type = "task.succeeded"
        elif final_status in ("failed", "blocked"):
            event_type = "task.failed"
        elif final_status == "waiting_on_customer":
            event_type = "task.hitl_requested"
        if event_type and finalization_won:
            try:
                runtime_emit_task_runner_status_event(
                    entity_id,
                    event_type,
                    payload={
                        "task_id": task_id,
                        "title": task_dict.get("title"),
                        "task_status": final_status,
                        "agent_id": effective_agent_id,
                        "agent_name": agent_name,
                        "turns_used": turns_used,
                        "duration_ms": duration_ms,
                        "error_type": actual_output.get("error_type"),
                        "error_message": error_message,
                    },
                )
            except Exception:
                logger.debug("TaskRunner: task event emit failed for task %s", task_id, exc_info=True)

        # Close billing context — all LLM calls within are now recorded
        try:
            await _billing_cm.__aexit__(None, None, None)
        except Exception:
            pass

        # Post agent's response as a comment so it shows in the Comments section
        # (alongside human comments — like a chat thread on the task)
        if (
            finalization_won
            and initiative_marker is None
            and agent_response
            and agent_response.strip()
        ):
            await self._log(task_id, "comment", agent_response[:5000])

        status_log = {
            "completed": "ai_execution_completed",
            "failed": "ai_execution_failed",
            "waiting_on_customer": "ai_hitl_requested",
            "blocked": "ai_needs_replan",
        }
        if finalization_won:
            await self._log(task_id, status_log.get(final_status, "ai_execution_completed"),
                t("task.execution.finished", locale=progress_locale,
                  status=t(f"task.status.{final_status}", locale=progress_locale),
                  turns=turns_used, seconds=duration_ms / 1000),
                metadata=task_input_log_meta(input_request) if input_request is not None else None)
            if input_request is not None:
                await self._post_task_input_card(task_id, entity_id, input_request)

        task_result = {
            "task_id": task_id,
            "status": final_status,
            "response": agent_response,
            "turns_used": turns_used,
            "duration_ms": duration_ms,
            "supervisor_verdict": supervisor_verdict,
        }
        if not finalization_won:
            task_result["stale_finalization_suppressed"] = True
        if initiative_marker is not None:
            task_result["initiative_decision"] = actual_output.get(
                "initiative_decision"
            )
            task_result["initiative_decision_error"] = initiative_decision_error
        if blocking_human_gate is not None:
            task_result["human_gate"] = blocking_human_gate
        if terminal_control is not None:
            task_result["terminal_control"] = terminal_control
        return task_result

    # ── Supervisor ──────────────────────────────────────────────────────

    async def _supervise(
        self, *, task_id: str, task_title: str, agent_response: str,
        done_when: str, turns_used: int, max_turns: int,
        tools_called: Optional[list[str]] = None,
        tool_evidence: Optional[list[dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Evaluate agent's work using the 5-verdict supervisor LLM call.

        Verdicts: continue, done, failed, needs_hitl, needs_replan.
        On any error, defaults to ``needs_replan`` (NOT ``done``) — we'd
        rather block a task than mark it falsely complete.
        """
        return await runtime_review_task_agent_output(
            engine=self._engine,
            task_title=task_title,
            agent_response=agent_response,
            done_when=done_when,
            turns_used=turns_used,
            max_turns=max_turns,
            tools_called=tools_called,
            tool_evidence=tool_evidence,
            worker_model=getattr(self, "_worker_model", None),
            metadata=getattr(self, "_worker_llm_metadata", None),
            performance_run_id=task_id,
            performance_tool_call_id=f"task-supervisor-{turns_used}",
        )

    def _parse_supervisor_json(self, raw: str) -> Dict[str, Any]:
        """Extract JSON from supervisor response, tolerating markdown fences.

        On any parse failure or invalid verdict, returns ``needs_replan``
        (NOT ``done``) — opaque LLM output should never auto-mark a task
        complete.
        """
        return runtime_parse_task_supervisor_json(raw)

    # ── Logging ──────────────────────────────────────────────────────────

    async def _post_task_input_card(
        self, task_id: str, entity_id: str, question: Dict[str, Any],
    ) -> None:
        """Mirror the parked question into workspace chat as an input card."""
        try:
            from packages.core.services.task_chat_hitl import ensure_task_input_hitl
            from packages.core.services.task_service import get_task

            async with self._get_session() as db:
                task = await get_task(db, task_id, entity_id)
                if task is not None:
                    await ensure_task_input_hitl(db, task, question=question)
                    await db.commit()
        except Exception:
            logger.warning(
                "TaskRunner: could not post the input card for task %s", task_id, exc_info=True,
            )

    async def _log(
        self,
        task_id: str,
        log_type: str,
        content: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Stamp the actual agent_id + agent_name into the log's meta so
        # the UI can render the agent's avatar + name instead of a
        # generic "AI Agent" label. ``self._agent_id`` and
        # ``self._agent_name`` are populated in ``run()`` once the
        # effective agent is resolved.
        meta = None
        agent_id = getattr(self, "_agent_id", None)
        agent_name = getattr(self, "_agent_name", None)
        runtime_metadata = runtime_execution_metadata(getattr(self, "_runtime_envelope", None))
        if agent_id or agent_name or runtime_metadata:
            meta = {}
            if agent_id:
                meta["agent_id"] = agent_id
            if agent_name:
                meta["agent_name"] = agent_name
            if runtime_metadata:
                meta.update(runtime_metadata)
        if metadata:
            meta = {**(meta or {}), **metadata}
        # Use the resolved agent's name as the displayed author when we have
        # one. With no name we are the master agent running as the workspace
        # agent, so say so instead of the old generic "AI Agent" label.
        from packages.core.constants.agents import MANOR_AGENT_NAME
        from packages.core.constants.task_actors import TaskActor

        display = agent_name or MANOR_AGENT_NAME
        actor = TaskActor.AGENT if agent_name and agent_name != MANOR_AGENT_NAME else TaskActor.MANOR
        try:
            async with self._get_session() as db:
                await add_task_log(
                    db, task_id, log_type, content,
                    actor=actor, created_by=display, metadata=meta,
                )
                await db.commit()
        except Exception as exc:
            logger.debug("TaskRunner: log failed for task %s: %s", task_id, exc)
