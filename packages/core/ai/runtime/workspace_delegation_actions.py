"""Runtime-owned workspace service delegation actions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _event_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_event_signature(name: str, args: dict[str, Any] | None) -> str:
    try:
        encoded = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = str(args or {})
    return f"{name}\0{encoded}"


def _bounded_max_rounds(
    value: Any,
    *,
    default: int = 12,
    upper: int = 20,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(parsed, upper))


def _delegation_prompt(raw_params: dict[str, Any]) -> str:
    for key in (
        "prompt",
        "instructions",
        "instruction",
        "task",
        "message",
        "request",
    ):
        value = raw_params.get(key)
        if value is None:
            continue
        text = _clean(value)
        if text:
            return text
    return ""


async def _workspace_service_options(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    from packages.core.models.workspace import Agent, AgentSubscription

    rows = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
        .order_by(
            AgentSubscription.service_key.asc().nulls_last(),
            Agent.name.asc(),
        )
    )).all()
    return [
        {
            "agent_subscription_id": sub.id,
            "service_key": sub.service_key,
            "agent_id": sub.agent_id,
            "agent_name": agent.name,
        }
        for sub, agent in rows
    ]


async def _resolve_workspace_service_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    raw_params: dict[str, Any],
) -> tuple[Any | None, Any | None, list[dict[str, Any]], str | None]:
    from packages.core.models.workspace import Agent, AgentSubscription

    available = await _workspace_service_options(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    subscription_id = _clean(
        raw_params.get("agent_subscription_id") or raw_params.get("subscription_id")
    )
    service_key = _clean(
        raw_params.get("service_key")
        or raw_params.get("target_service_key")
        or raw_params.get("service")
    )
    agent_id = _clean(raw_params.get("agent_id"))

    filters = [
        AgentSubscription.entity_id == entity_id,
        AgentSubscription.workspace_id == workspace_id,
        AgentSubscription.status == "active",
    ]
    selector = ""
    if subscription_id:
        filters.append(AgentSubscription.id == subscription_id)
        selector = "agent_subscription_id"
    elif service_key:
        filters.append(AgentSubscription.service_key == service_key)
        selector = "service_key"
    elif agent_id:
        filters.append(AgentSubscription.agent_id == agent_id)
        selector = "agent_id"
    else:
        return (
            None,
            None,
            available,
            "service_key, agent_subscription_id, or agent_id is required",
        )

    rows = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            *filters,
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
    )).all()
    if not rows:
        return None, None, available, f"No active workspace service agent matched {selector}."
    if len(rows) > 1:
        return None, None, available, (
            f"{selector} matched multiple active workspace services; pass service_key "
            "or agent_subscription_id to disambiguate."
        )
    sub, agent = rows[0]
    return sub, agent, available, None


def _service_prompt_appendix(sub: Any, agent: Any) -> str:
    service_key = _clean(getattr(sub, "service_key", None))
    service_label = (
        service_key
        or _clean(getattr(sub, "name", None))
        or _clean(getattr(agent, "name", None))
    )
    parts = [
        (
            "You are being called by Manor AI as a delegated workspace service "
            f"agent for `{service_label}`. Complete the delegated request using "
            "only your own visible tools, MCP integrations, skills, and workspace "
            "context. Return a concise result for Manor AI to relay to the user. "
            "If a required credential, approval, or user decision is missing, "
            "state exactly what is needed."
        )
    ]
    custom_prompt = _clean(getattr(sub, "custom_prompt", None))
    if custom_prompt:
        parts.append(f"Workspace service-specific instructions:\n{custom_prompt}")
    return "\n\n".join(parts)


async def runtime_workspace_delegate_service_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Delegate a workspace chat request to a service-bound sub-agent."""

    if not workspace_id:
        return _dumps({
            "error": "workspace_id is required; use this tool only inside workspace chat",
        })
    if not entity_id:
        return _dumps({"error": "entity_id is required"})
    raw_params = dict(params or {})
    prompt = _delegation_prompt(raw_params)
    if not prompt:
        return _dumps({"error": "prompt is required"})

    run_id = ""
    event_sink = None
    event_base: dict[str, Any] = {}
    try:
        from packages.core.ai.context import build_agent_context
        from packages.core.ai.runtime.harness import runtime_execute_chat_agent_loop
        from packages.core.ai.runtime.output_policy import (
            runtime_sanitize_assistant_content_after_loop,
        )
        from packages.core.ai.runtime.streams import (
            runtime_tool_arguments_for_chat,
            runtime_tool_status_for_chat,
            runtime_tool_stream_sink_var,
        )
        from packages.core.ai.runtime.surfaces import ChatSurface
        from packages.core.database import async_session
        from packages.core.models.base import generate_ulid

        async with async_session() as db:
            sub, agent, available_services, error = await _resolve_workspace_service_agent(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                raw_params=raw_params,
            )
            if error:
                return _dumps({
                    "error": "workspace_service_agent_not_found",
                    "message": error,
                    "available_services": available_services,
                })

            run_id = generate_ulid()
            event_sink = runtime_tool_stream_sink_var.get(None)
            event_base = {
                "run_id": run_id,
                "agent_id": sub.agent_id,
                "agent_subscription_id": sub.id,
                "agent_name": agent.name,
                "service_key": sub.service_key,
                "objective": prompt[:500],
            }
            if event_sink is not None and event_sink.active:
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "started",
                    "status": "running",
                    "updated_at": _event_time(),
                })

            ctx = await build_agent_context(
                db,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=sub.agent_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                active_user_message=prompt,
                model_role="primary",
                runtime_surface=ChatSurface.WORKSPACE_CHAT,
                extra_system_prompt=_service_prompt_appendix(sub, agent),
            )

        max_rounds = _bounded_max_rounds(raw_params.get("max_rounds"))
        tool_seq = [0]
        pending_tool_seqs: dict[str, list[int]] = {}

        def on_tool_start(name: str, args: dict[str, Any]) -> None:
            tool_seq[0] += 1
            seq = tool_seq[0]
            signature = _tool_event_signature(name, args)
            pending_tool_seqs.setdefault(signature, []).append(seq)
            if event_sink is not None and event_sink.active:
                safe_arguments = runtime_tool_arguments_for_chat(name, args)
                tool = {
                    "seq": seq,
                    "name": name,
                    "status": "running",
                }
                if safe_arguments is not None:
                    tool["arguments"] = safe_arguments
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "tool_started",
                    "status": "running",
                    "updated_at": _event_time(),
                    "tool": tool,
                })

        def on_tool_end(
            name: str,
            result: str,
            duration_ms: float = 0,
            args: dict[str, Any] | None = None,
        ) -> None:
            signature = _tool_event_signature(name, args)
            pending = pending_tool_seqs.get(signature) or []
            seq = pending.pop(0) if pending else None
            if not pending:
                pending_tool_seqs.pop(signature, None)
            if event_sink is not None and event_sink.active:
                safe_arguments = runtime_tool_arguments_for_chat(name, args)
                tool = {
                    "seq": seq,
                    "name": name,
                    "status": (
                        "error"
                        if runtime_tool_status_for_chat(result) == "error"
                        else "completed"
                    ),
                    "duration_ms": int(duration_ms),
                }
                if safe_arguments is not None:
                    tool["arguments"] = safe_arguments
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "tool_completed",
                    "status": "running",
                    "updated_at": _event_time(),
                    "tool": tool,
                })

        result = await runtime_execute_chat_agent_loop(
            runtime_envelope=ctx.runtime_envelope,
            system_prompt=ctx.system_prompt,
            user_message=prompt,
            tools=ctx.tools,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=sub.agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=ctx.task_id,
            active_user_message=prompt,
            tool_profile=ctx.tool_profile,
            allowed_tool_names=ctx.allowed_tool_names,
            model=ctx.model,
            temperature=getattr(ctx, "temperature", None),
            max_tokens=getattr(ctx, "max_tokens", None),
            max_rounds=max_rounds,
            metadata=getattr(ctx, "llm_metadata", None),
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
        )
        content = runtime_sanitize_assistant_content_after_loop(
            result.content or "",
            result.tool_calls_made or [],
        )
        run_status = (
            "failed"
            if result.error or result.stop_reason == "error"
            else "blocked"
            if result.stop_reason in {"blocked", "hitl_required", "needs_hitl"}
            else "completed"
        )
        if event_sink is not None and event_sink.active:
            event_sink.emit_sub_agent_event({
                **event_base,
                "event_type": "completed" if run_status == "completed" else run_status,
                "status": run_status,
                "updated_at": _event_time(),
                "content": content[:1200],
                "rounds": result.rounds,
                "tool_calls_made": list(result.tool_calls_made or [])[:20],
                "stop_reason": result.stop_reason,
                "error": result.error,
            })
        return _dumps({
            "delegated": True,
            "run_id": run_id,
            "status": run_status,
            "service": {
                "agent_subscription_id": sub.id,
                "service_key": sub.service_key,
                "agent_id": sub.agent_id,
                "agent_name": agent.name,
            },
            "content": content,
            "rounds": result.rounds,
            "tool_calls_made": list(result.tool_calls_made or []),
            "usage": result.usage or {},
            "stop_reason": result.stop_reason,
            "error": result.error,
        })
    except Exception as exc:  # noqa: BLE001
        if event_sink is not None and event_sink.active and event_base:
            event_sink.emit_sub_agent_event({
                **event_base,
                "event_type": "failed",
                "status": "failed",
                "updated_at": _event_time(),
                "error": str(exc),
            })
        logger.warning(
            "workspace service delegation failed: workspace=%s",
            workspace_id,
            exc_info=True,
        )
        return _dumps({
            "error": "workspace_service_delegation_failed",
            "message": str(exc),
        })
