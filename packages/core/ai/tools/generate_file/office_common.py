from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import runtime_invoke_skill
from packages.core.ai.runtime.artifacts import runtime_input_with_artifact_context
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.services.skill_reference_materials import skill_context_artifact_urls

from . import common


def _runtime_tool_context_args(runtime_context: Any | None) -> dict[str, Any]:
    """Forward durable-runtime identity so sandbox admission can reserve a runner."""

    run_id = getattr(runtime_context, "runtime_run_id", None)
    if not run_id:
        return {}
    return {
        "_runtime_run_id_from_context": run_id,
        "_runtime_tool_call_id_from_context": getattr(runtime_context, "runtime_tool_call_id", None),
        "_runtime_tool_attempt_from_context": getattr(runtime_context, "runtime_tool_attempt", 1),
    }


async def _invoke_builtin_skill(
    *,
    skill: str,
    prompt: str,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    sandbox_binding_key: str | None = None,
    name: str = "",
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    runtime_context: Any | None = None,
) -> Any:
    """Run an office skill and return the same structured handoff as ``invoke_skill``.

    A sandbox skill answers with ``sandbox_ready``; that must reach the model
    as JSON (so the loop loads the ``sandbox`` tool) and must grant that tool
    on the runtime envelope. Returning the bare ``content`` text left the
    caller with instructions it could not execute.
    """
    from packages.core.ai.runtime.control import is_runtime_tool_suspension
    from packages.core.ai.runtime.skills import (
        runtime_activate_sandbox_skill_handoff,
        runtime_format_invoke_skill_result,
        runtime_skill_invocation_metadata,
    )
    from packages.core.database import async_session
    from packages.core.services.skill_service import list_skills

    payload = {
        "prompt": prompt,
        "output_name": name,
        "params": params or {},
    }
    input_text = json.dumps(payload, ensure_ascii=False, indent=2)

    async with async_session() as db:
        await list_skills(db, entity_id)
        result = await runtime_invoke_skill(
            db,
            skill,
            entity_id,
            input_text,
            user_id=user_id or None,
            agent_id=agent_id or None,
            workspace_id=workspace_id or None,
            conversation_id=conversation_id or None,
            sandbox_binding_key=sandbox_binding_key or None,
            task_id=task_id or None,
            runtime_tool_context=_runtime_tool_context_args(runtime_context),
            runtime_envelope=getattr(runtime_context, "runtime_envelope", None),
            metadata=runtime_skill_invocation_metadata(runtime_context),
            active_user_message=getattr(runtime_context, "active_user_message", None),
            model=getattr(runtime_context, "llm_model", None),
            tool_profile=getattr(runtime_context, "tool_profile", None),
            allowed_tool_names=getattr(runtime_context, "allowed_tool_names", None),
            # Host-collected run context: optional references.
            context_artifact_urls=skill_context_artifact_urls(
                getattr(runtime_context, "runtime_artifact_urls", None),
                getattr(runtime_context, "dependency_artifact_urls", None),
            ),
        )

    if is_runtime_tool_suspension(result):
        return result
    if not isinstance(result, dict):
        return json.dumps({"error": "skill returned no result", "skill": skill}, ensure_ascii=False)
    if "error" in result or result.get("stop_reason") == "error":
        return json.dumps(
            {
                "error": result.get("error") or result.get("content") or "skill failed",
                "skill": skill,
                "code": result.get("code"),
                "retryable": bool(result.get("retryable")),
                **(
                    {"reference": result["reference"]}
                    if isinstance(result.get("reference"), dict) else {}
                ),
            },
            ensure_ascii=False,
        )
    from packages.core.ai.runtime.nested_usage import runtime_record_nested_usage

    runtime_record_nested_usage(result.get("usage"))
    if runtime_activate_sandbox_skill_handoff(runtime_context, result):
        result = {**result, "loaded_tools": ["sandbox"]}
    return runtime_format_invoke_skill_result(skill, result)


async def handle_office_skill(
    *,
    skill: str,
    default_subdir: str,
    kind: str,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> Any:
    # ``kwargs`` here is the caller's residual arguments: the actor and the
    # conversation arrived as named parameters, so they have to be handed back
    # to the context builder or this frame would resolve a narrower scope than
    # its caller and mint the wrong sandbox binding.
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
    )
    if not prompt:
        prompt = str(kwargs.get("content") or params.get("content") or "").strip()
    if not prompt and runtime_context.active_user_message:
        prompt = runtime_context.active_user_message.strip()
    if not prompt:
        return json.dumps({"error": f"kind={kind} requires prompt"}, ensure_ascii=False)
    prompt = runtime_input_with_artifact_context(
        prompt,
        runtime_artifact_urls=runtime_context.runtime_artifact_urls,
        dependency_artifact_urls=runtime_context.dependency_artifact_urls,
    )
    workspace_id = kwargs.get("workspace_id") or None
    scoped_name = await common._scope_workspace_output_name(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=runtime_context.task_id,
        name=name,
        default_subdir=default_subdir,
    )
    return await _invoke_builtin_skill(
        skill=skill,
        prompt=prompt,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id or None,
        # The office skill is a sandbox skill: this scope is what binds and
        # later scopes its sandbox, so a conversation-less task-worker run
        # hands over its binding key instead of an empty conversation. It
        # travels on its own keyword so it reaches sandbox admission only —
        # never the nested tool calls that follow ``conversation_id``. With a
        # real conversation the key is that conversation, so nothing changes.
        sandbox_binding_key=runtime_context.sandbox_binding_key,
        name=scoped_name,
        params=params,
        workspace_id=workspace_id,
        task_id=kwargs.get("task_id") or None,
        agent_id=agent_id or None,
        runtime_context=runtime_context,
    )
