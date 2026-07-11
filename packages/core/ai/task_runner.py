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
from typing import Any, Dict, Optional

from packages.core.ai.runtime import (
    ChatSurface,
    RUNTIME_TASK_VERDICT_CONTINUE as VERDICT_CONTINUE,
    RUNTIME_TASK_VERDICT_DONE as VERDICT_DONE,
    RUNTIME_TASK_VERDICT_FAILED as VERDICT_FAILED,
    RUNTIME_TASK_VERDICT_NEEDS_HITL as VERDICT_NEEDS_HITL,
    RUNTIME_TASK_VERDICT_NEEDS_REPLAN as VERDICT_NEEDS_REPLAN,
    runtime_assemble_prompt_for_turn,
    runtime_classify_task_complexity,
    runtime_configure_task_engine_model,
    runtime_emit_task_runner_status_event,
    runtime_execute_task_agent_turn,
    runtime_execute_task_final_response,
    runtime_execution_metadata,
    runtime_merge_prompt_appendix,
    runtime_parse_task_supervisor_json,
    runtime_prepare_context_appendix_for_turn,
    runtime_prepare_prompt_appendix_for_turn,
    runtime_persist_task_runner_runtime_events,
    runtime_record_task_runner_execution_evidence,
    runtime_request_for_surface_turn,
    runtime_review_task_agent_output,
    runtime_task_engine,
    runtime_task_engine_model,
    runtime_task_initial_messages,
    runtime_task_llm_billing_context,
    runtime_task_billable_user_id,
    runtime_task_supervisor_feedback_message,
    runtime_task_system_prompt,
    runtime_task_ticket_prompt,
    runtime_task_user_prompt,
)
from packages.core.database import async_session
from packages.core.services.agent_service import get_agent
from packages.core.services.task_state_machine import (
    TaskStatusTransitionError,
    apply_task_status_transition,
)
from packages.core.services.task_service import add_task_log, update_task
from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS

logger = logging.getLogger(__name__)


async def _finalize_with_terminal_guard(
    db, task_id: str, entity_id: str, *, status, actual_output, details
):
    """Persist a task-run's final state, tolerating an already-terminal task.

    If a concurrent path already moved the task to a different terminal status
    (e.g. a failure handler marked it ``failed`` while this run decided
    ``completed``), the opposite-terminal transition is intentionally rejected
    by the state machine. That existing terminal state is authoritative: persist
    the run's output/details but leave the status, instead of raising
    ``TaskStatusTransitionError`` and making the Celery task retry a run that has
    already finished.
    """
    try:
        return await update_task(
            db, task_id, entity_id,
            status=status, actual_output=actual_output, details=details,
        )
    except TaskStatusTransitionError:
        logger.warning(
            "Task %s already terminal; persisting run output without overriding status to %s",
            task_id, status,
        )
        return await update_task(
            db, task_id, entity_id,
            actual_output=actual_output, details=details,
        )

_DEFAULT_MAX_TURNS = DEFAULT_AGENT_MAX_TURNS
_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


class TaskRunner:
    """Runs an agent against a task ticket with supervisor oversight.

    Lifecycle:
    1. Load task from DB; bail if already terminal
    2. Load agent config (system prompt, tool bindings)
    3. Build execution prompt from task details
    4. Multi-turn loop: agent executes → supervisor reviews → loop or finish
    5. Update task status and log all activity
    """

    def __init__(self, engine: Any | None = None, session_factory=None):
        self._engine = engine
        self._session_factory = session_factory

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
    ):
        """Build the same workspace-aware prompt and tool surface used by chat."""
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
            )
            assembled = await runtime_assemble_prompt_for_turn(
                prompt_db,
                request=runtime_request,
                legacy_runtime_profile=runtime.legacy_tool_profile,
                agent_id=agent_id,
                bound_tool_names=runtime.bound_tool_names,
                is_master=runtime.is_master,
                mcp_allowed_names=runtime.mcp_allowed_names,
                active_user_message=active_user_message,
                legacy_extra_context=runtime.extra_context,
            )
            return assembled

    async def run(self, task_id: str, agent_id: str | None = None) -> Dict[str, Any]:
        """Execute agent on task with supervisor review."""
        task_start = time.monotonic()

        async with self._get_session() as db:
            from sqlalchemy import select
            from packages.core.models.task import Task
            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"task_id": task_id, "status": "failed", "error": "task not found"}

            entity_id = task.entity_id
            task_user_id = runtime_task_billable_user_id(task)

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

            logger.info(
                "Task %s model=%s (role=%s), supervisor=%s",
                task_id,
                runtime_task_engine_model(self._engine),
                _model_role,
                self._worker_model,
            )

            if task.status in _TERMINAL_STATUSES:
                return {"task_id": task_id, "status": task.status, "error": "already terminal"}

            # Resolve agent
            effective_agent_id = agent_id or task.agent_id
            agent_name = "AI Agent"
            system_prompt = ""
            runtime_prompt_result = None
            from packages.core.constants.agents import is_master_agent, MANOR_AGENT_NAME
            is_master = is_master_agent(effective_agent_id, task.agent_type)
            from packages.core.services.workspace_runtime import resolve_workspace_runtime
            runtime = await resolve_workspace_runtime(
                db,
                entity_id=entity_id,
                user_id=task_user_id,
                agent_id=effective_agent_id,
                workspace_id=task.workspace_id,
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
                    )
                    system_prompt = runtime_prompt_result.prompt
                except Exception as exc:
                    logger.warning("Failed to build master prompt: %s", exc)
                    system_prompt = ""
            elif effective_agent_id:
                agent = await get_agent(db, effective_agent_id)
                if agent:
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
                                )
                                fallback_appendix = await runtime_prepare_context_appendix_for_turn(
                                    db,
                                    request=fallback_request,
                                    legacy_runtime_profile=runtime.legacy_tool_profile,
                                    legacy_extra_context=runtime.extra_context,
                                )
                                system_prompt = runtime_merge_prompt_appendix(
                                    system_prompt,
                                    fallback_appendix.context_section,
                                )
                            except Exception:
                                logger.debug("Failed to render runtime fallback context", exc_info=True)

            task_dict = {
                "id": task.id, "title": task.title, "description": task.description,
                "priority": task.priority, "task_type": task.task_type,
                "details": dict(task.details or {}), "status": task.status,
                "workspace_id": task.workspace_id,
                "conversation_id": task.conversation_id,
                "creator_id": task.creator_id,
                "owner_service_key": task.owner_service_key,
                "delegate_service_keys": list(task.delegate_service_keys or []),
                "started_at": task.started_at,
            }

            # Get done_when criteria from task details
            done_when = str((task.details or {}).get("done_when", "")).strip()

            apply_task_status_transition(task, "in_progress")
            await db.commit()

        # Stash for ``_log`` so subsequent log entries carry agent context
        # (id + name) in their metadata. UI uses this to resolve the
        # actual agent's avatar + name instead of "AI Agent" placeholder.
        self._agent_id = effective_agent_id
        self._agent_name = agent_name

        await self._log(task_id, "ai_execution_started", f"[AI] Execution started — agent: {agent_name}")

        # ── Multi-turn execution loop with supervisor ────────────────────
        user_prompt = runtime_task_ticket_prompt(task_dict)
        if runtime_prompt_result is None:
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
            )
            async with self._get_session() as prompt_db:
                runtime_prompt_result = await runtime_prepare_prompt_appendix_for_turn(
                    prompt_db,
                    request=runtime_request,
                    legacy_runtime_profile=runtime.legacy_tool_profile,
                    agent_id=effective_agent_id,
                    bound_tool_names=runtime.bound_tool_names,
                    is_master=runtime.is_master,
                    mcp_allowed_names=runtime.mcp_allowed_names,
                    active_user_message=user_prompt,
                    legacy_extra_context=runtime.extra_context,
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
        user_prompt_result = runtime_task_user_prompt(
            base_prompt=user_prompt,
            task_logs=task_logs,
            hitl_response=hitl_response,
            hitl_responded_by=hitl_by,
        )
        user_prompt = user_prompt_result.prompt
        if user_prompt_result.hitl_response:
            await self._log(task_id, "ai_hitl_resumed",
                f"[AI] Resuming after HITL — human said: {user_prompt_result.hitl_response[:200]}")

        max_turns = int((task_dict.get("details") or {}).get("max_turns") or _DEFAULT_MAX_TURNS)
        full_system_prompt = runtime_task_system_prompt(system_prompt)

        # All LLM calls (agent + supervisor) auto-record usage via billing context
        _billing_cm = runtime_task_llm_billing_context(
            entity_id=entity_id,
            user_id=task_user_id,
            agent_id=effective_agent_id,
            workspace_id=task_dict.get("workspace_id"),
            conversation_id=task_dict.get("conversation_id"),
        )
        await _billing_cm.__aenter__()

        messages = runtime_task_initial_messages(user_prompt)
        agent_response = ""
        turns_used = 0
        final_status = "failed"
        supervisor_verdict: Dict[str, Any] = {}
        tools_called_session: list[str] = []  # cross-turn record for supervisor
        loaded_tool_names = {
            t.get("function", {}).get("name")
            for t in tools
            if isinstance(t, dict)
        }
        _total_input_tokens = 0
        _total_output_tokens = 0

        for turn in range(max_turns):
            turns_used = turn + 1

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
                    legacy_tool_profile=runtime.legacy_tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    metadata=getattr(self, "_agent_llm_metadata", None),
                )
                messages = turn_result.messages
                tools = turn_result.tools
                loaded_tool_names = turn_result.loaded_tool_names

                # Accumulate token usage
                if turn_result.usage:
                    _total_input_tokens += int(turn_result.usage.get("prompt_tokens") or 0)
                    _total_output_tokens += int(turn_result.usage.get("completion_tokens") or 0)

                if turn_result.had_tool_calls:
                    tools_called_session.extend(turn_result.tool_names)
                    await self._log(task_id, "ai_agent_turn",
                        f"[AI] Turn {turns_used}/{max_turns} — tools: [{', '.join(turn_result.tool_names)}]")
                    continue  # Don't supervisor-review tool calls, go to next turn

                agent_response = turn_result.response_text
                await self._log(task_id, "ai_agent_turn",
                    f"[AI] Turn {turns_used}/{max_turns} — response ({len(agent_response)} chars)")

            except Exception as e:
                agent_response = f"Agent execution failed: {e}"
                logger.error("TaskRunner: turn %d failed: %s", turns_used, e, exc_info=True)
                await self._log(task_id, "ai_agent_turn",
                    f"[AI] Turn {turns_used}/{max_turns} FAILED — {e}")

            # ── Check for known error responses before supervisor ──
            _ERROR_MARKERS = ("sorry, the request failed", "agent execution failed", "error:")
            if any(agent_response.lower().startswith(m) for m in _ERROR_MARKERS):
                supervisor_verdict = {"verdict": VERDICT_FAILED, "reason": f"Agent returned error: {agent_response[:200]}", "retry_strategy": "retry_same"}
            else:
                # ── Supervisor review ──
                supervisor_verdict = await self._supervise(
                    task_id=task_id,
                    task_title=task_dict["title"],
                    agent_response=agent_response,
                    done_when=done_when,
                    turns_used=turns_used,
                    max_turns=max_turns,
                    tools_called=tools_called_session,
                )

            verdict = supervisor_verdict.get("verdict", VERDICT_DONE)
            await self._log(task_id, "ai_supervisor_verdict",
                f"[Supervisor] Verdict: {verdict} — {supervisor_verdict.get('reason', '')[:200]}")

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
                        f"[AI] Finalized after max tool turns — response ({len(agent_response)} chars)",
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
                )
            except Exception as exc:
                logger.warning("Supervisor at exhaustion failed: %s", exc)
                supervisor_verdict = {
                    "verdict": VERDICT_NEEDS_REPLAN,
                    "reason": f"Supervisor unreachable at exhaustion: {exc}",
                }

            verdict = supervisor_verdict.get("verdict", VERDICT_NEEDS_REPLAN)
            await self._log(
                task_id, "ai_supervisor_verdict",
                f"[Supervisor @ exhaustion] Verdict: {verdict} — "
                f"{supervisor_verdict.get('reason', '')[:200]}",
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

        # ── Update task in DB ────────────────────────────────────────────
        duration_ms = int((time.monotonic() - task_start) * 1000)
        error_message = (
            supervisor_verdict.get("reason")
            if supervisor_verdict and final_status in ("failed", "blocked")
            else None
        )

        actual_output = {
            "agent_id": effective_agent_id,
            "agent_name": agent_name,
            "response": agent_response[:5000] if agent_response else None,
            "turns_used": turns_used,
            "duration_ms": duration_ms,
            "verdict": supervisor_verdict.get("verdict") if supervisor_verdict else final_status,
            "error_type": "SupervisorRejected" if error_message else None,
            "error_message": error_message,
        }
        updated_details = {
            **(task_dict.get("details") or {}),
            "ai_result": {
                "agent_id": effective_agent_id,
                "agent_name": agent_name,
                "response": agent_response[:10000],
                "turns_used": turns_used,
                "duration_ms": duration_ms,
                "supervisor_verdict": supervisor_verdict,
                "error_type": "SupervisorRejected" if error_message else None,
                "error_message": error_message,
            },
        }

        async with self._get_session() as db:
            updated_task = await _finalize_with_terminal_guard(
                db, task_id, entity_id,
                status=final_status,
                actual_output=actual_output,
                details=updated_details,
            )
            try:
                await runtime_record_task_runner_execution_evidence(
                    db,
                    entity_id=entity_id,
                    workspace_id=task_dict.get("workspace_id"),
                    task_id=task_id,
                    plan_id=None,
                    task_status=getattr(updated_task, "status", None) or final_status,
                    plan_status=None,
                    task_title=str(task_dict.get("title") or ""),
                    task_description=str(task_dict.get("description") or ""),
                    owner_service_key=task_dict.get("owner_service_key"),
                    delegate_service_keys=list(task_dict.get("delegate_service_keys") or []),
                    agent_id=effective_agent_id,
                    steps=[],
                    actual_output=actual_output,
                    cost_tracking={},
                    started_at=task_dict.get("started_at"),
                    completed_at=None,
                )
            except Exception:
                logger.debug("TaskRunner: runtime evidence recording skipped for task %s", task_id, exc_info=True)
            await db.commit()
        await runtime_persist_task_runner_runtime_events(
            getattr(self, "_runtime_envelope", None),
        )

        event_type = None
        if final_status == "completed":
            event_type = "task.succeeded"
        elif final_status in ("failed", "blocked"):
            event_type = "task.failed"
        elif final_status == "waiting_on_customer":
            event_type = "task.hitl_requested"
        if event_type:
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
                        "error_type": "SupervisorRejected" if error_message else None,
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
        if agent_response and agent_response.strip():
            await self._log(task_id, "comment", agent_response[:5000])

        duration_str = f"{duration_ms / 1000:.1f}s"
        status_log = {
            "completed": "ai_execution_completed",
            "failed": "ai_execution_failed",
            "waiting_on_customer": "ai_hitl_requested",
            "blocked": "ai_needs_replan",
        }
        await self._log(task_id, status_log.get(final_status, "ai_execution_completed"),
            f"[AI] {final_status} — {turns_used} turn(s), {duration_str}")

        return {
            "task_id": task_id,
            "status": final_status,
            "response": agent_response,
            "turns_used": turns_used,
            "duration_ms": duration_ms,
            "supervisor_verdict": supervisor_verdict,
        }

    # ── Supervisor ──────────────────────────────────────────────────────

    async def _supervise(
        self, *, task_id: str, task_title: str, agent_response: str,
        done_when: str, turns_used: int, max_turns: int,
        tools_called: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Evaluate agent's work using the 5-verdict supervisor LLM call.

        Verdicts: continue, done, failed, needs_hitl, needs_replan.
        On any error, defaults to ``needs_replan`` (NOT ``done``) — we'd
        rather block a task than mark it falsely complete.
        """
        del task_id
        return await runtime_review_task_agent_output(
            engine=self._engine,
            task_title=task_title,
            agent_response=agent_response,
            done_when=done_when,
            turns_used=turns_used,
            max_turns=max_turns,
            tools_called=tools_called,
            worker_model=getattr(self, "_worker_model", None),
            metadata=getattr(self, "_worker_llm_metadata", None),
        )

    def _parse_supervisor_json(self, raw: str) -> Dict[str, Any]:
        """Extract JSON from supervisor response, tolerating markdown fences.

        On any parse failure or invalid verdict, returns ``needs_replan``
        (NOT ``done``) — opaque LLM output should never auto-mark a task
        complete.
        """
        return runtime_parse_task_supervisor_json(raw)

    # ── Logging ──────────────────────────────────────────────────────────

    async def _log(self, task_id: str, log_type: str, content: str) -> None:
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
        # Use the resolved agent's name as the displayed author when
        # we have one; falls back to the generic "AI Agent" label so
        # older clients (no meta-aware UI) still get something readable.
        display = agent_name or "AI Agent"
        try:
            async with self._get_session() as db:
                await add_task_log(db, task_id, log_type, content, created_by=display, metadata=meta)
                await db.commit()
        except Exception as exc:
            logger.debug("TaskRunner: log failed for task %s: %s", task_id, exc)
