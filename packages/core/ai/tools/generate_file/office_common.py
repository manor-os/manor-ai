from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import runtime_invoke_skill
from packages.core.ai.runtime.artifacts import runtime_input_with_artifact_context
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

from . import common


async def _invoke_builtin_skill(
    *,
    skill: str,
    prompt: str,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    name: str = "",
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
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
            task_id=task_id or None,
        )

    if "error" in result:
        return json.dumps({"error": result["error"], "skill": skill}, ensure_ascii=False)
    if result.get("stop_reason") == "error":
        return json.dumps(
            {
                "error": result.get("content") or result.get("error") or "skill failed",
                "skill": skill,
            },
            ensure_ascii=False,
        )
    return result.get("content") or json.dumps(result, ensure_ascii=False)


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
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
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
        name=name,
        default_subdir=default_subdir,
    )
    return await _invoke_builtin_skill(
        skill=skill,
        prompt=prompt,
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        name=scoped_name,
        params=params,
        workspace_id=workspace_id,
        task_id=kwargs.get("task_id") or None,
        agent_id=agent_id or None,
    )
