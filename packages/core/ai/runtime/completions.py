from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
from typing import Any

from packages.core.ai.runtime.sources import (
    RUNTIME_AGENT_GREETING_SOURCE,
    RUNTIME_AGENTIC_LOOP_SOURCE,
    RUNTIME_ANNOUNCEMENT_DRAFT_SOURCE,
    RUNTIME_BRIEFING_SOURCE,
    RUNTIME_CHANNEL_HOLD_SOURCE,
    RUNTIME_CHANNEL_SOURCE,
    RUNTIME_CHAT_EXTRACTOR_SOURCE,
    RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE,
    RUNTIME_CHAT_SOURCE,
    RUNTIME_CHAT_STREAM_SOURCE,
    RUNTIME_COMPLETION_SOURCE,
    RUNTIME_CONVERSATION_SUMMARY_SOURCE,
    RUNTIME_DOCGEN_SOURCE,
    RUNTIME_DOCUMENT_AI_DRAFT_SOURCE,
    RUNTIME_EXTRACT_DATA_TOOL_SOURCE,
    RUNTIME_GOAL_MEASUREMENT_SOURCE,
    RUNTIME_INTERNAL_WORKER_SOURCE,
    RUNTIME_KNOWLEDGE_GEN_SOURCE,
    RUNTIME_MEMORY_SOURCE,
    RUNTIME_OUTCOME_EVALUATION_SOURCE,
    RUNTIME_PLAN_EXECUTOR_SOURCE,
    RUNTIME_PLANNER_SOURCE,
    RUNTIME_PLAN_SUPERVISOR_SOURCE,
    RUNTIME_PROMPT_PREVIEW_SOURCE,
    RUNTIME_SKILL_GENERATOR_SOURCE,
    RUNTIME_SKILL_MATCHER_SOURCE,
    RUNTIME_SKILL_SOURCE,
    RUNTIME_STRATEGIST_REVIEW_SOURCE,
    RUNTIME_STRATEGIST_SOURCE,
    RUNTIME_SUBAGENT_SOURCE,
    RUNTIME_SYSTEM_SOURCE,
    RUNTIME_TASK_RUNNER_SOURCE,
    RUNTIME_WORKER_SOURCE,
    RUNTIME_WORKFLOW_RUNNER_SOURCE,
    RUNTIME_WORKFLOW_SERVICE_SOURCE,
    RUNTIME_WORKFLOW_SOURCE,
    RUNTIME_WORKSPACE_ARCHITECT_SOURCE,
    RUNTIME_WORKSPACE_SETUP_SOURCE,
)


@dataclass(frozen=True)
class RuntimeTextCompletionResult:
    """Result for a Runtime-scoped one-shot text completion."""

    content: str
    usage: dict[str, Any]


def runtime_text_completion_platform_configured() -> bool:
    """Return whether platform-level text completion credentials are present."""

    return False


def runtime_validation_retry_user_prompt(
    base_user_prompt: str | None,
    validation_guidance: str,
) -> str:
    """Append the standard Runtime validation-retry instruction to a user prompt."""

    return (
        f"{base_user_prompt or ''}\n\n"
        "Your previous response failed validation. "
        f"{validation_guidance.strip()}"
    )


def runtime_prompt_with_output_schema(
    prompt: Any,
    schema: dict[str, Any] | None,
) -> str:
    """Append a Runtime output contract for model output schema validation."""

    text = str(prompt)
    if not schema:
        return text

    schema_json = json.dumps(schema, ensure_ascii=False, indent=2, default=str)
    return (
        f"{text}\n\n"
        "Output contract:\n"
        "Return ONLY a value that conforms to this JSON Schema. Do not include "
        "explanatory prose, markdown fences, or extra wrapper text.\n"
        "If the schema type is array and no records are found, return [].\n"
        "If the schema type is string, return a JSON string or plain text that "
        "is exactly the requested final value.\n"
        f"JSON Schema:\n{schema_json}"
    )


def runtime_one_shot_messages(
    *,
    user_prompt: Any,
    system_prompt: Any | None = None,
) -> list[dict[str, str]]:
    """Build Runtime-owned system/user messages for a one-shot completion."""

    messages: list[dict[str, str]] = []
    if system_prompt is not None and str(system_prompt).strip():
        messages.append({"role": "system", "content": str(system_prompt)})
    messages.append({"role": "user", "content": str(user_prompt)})
    return messages


def runtime_agent_prompt_preview_messages(
    *,
    system_prompt: str,
    test_message: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for previewing an agent prompt."""

    return runtime_one_shot_messages(
        system_prompt=system_prompt,
        user_prompt=test_message,
    )


RUNTIME_TEXT_COMPLETION_SOURCE_MODEL_ROLES: dict[str, str] = {
    RUNTIME_AGENT_GREETING_SOURCE: "primary",
    RUNTIME_AGENTIC_LOOP_SOURCE: "primary",
    RUNTIME_ANNOUNCEMENT_DRAFT_SOURCE: "primary",
    RUNTIME_BRIEFING_SOURCE: "briefing",
    RUNTIME_CHANNEL_HOLD_SOURCE: "primary",
    RUNTIME_CHANNEL_SOURCE: "primary",
    RUNTIME_CHAT_EXTRACTOR_SOURCE: "chat_extractor",
    RUNTIME_CHAT_INSIGHT_EXTRACTION_SOURCE: "chat_insight_extraction",
    RUNTIME_CHAT_SOURCE: "primary",
    RUNTIME_CHAT_STREAM_SOURCE: "primary",
    RUNTIME_COMPLETION_SOURCE: "primary",
    RUNTIME_CONVERSATION_SUMMARY_SOURCE: "conversation_summary",
    RUNTIME_DOCGEN_SOURCE: "docgen",
    RUNTIME_DOCUMENT_AI_DRAFT_SOURCE: "document_ai_draft",
    RUNTIME_EXTRACT_DATA_TOOL_SOURCE: "extract_data_tool",
    RUNTIME_GOAL_MEASUREMENT_SOURCE: "goal_measurement",
    RUNTIME_INTERNAL_WORKER_SOURCE: "worker",
    RUNTIME_KNOWLEDGE_GEN_SOURCE: "knowledge_gen",
    RUNTIME_MEMORY_SOURCE: "memory",
    RUNTIME_OUTCOME_EVALUATION_SOURCE: "outcome_evaluation",
    RUNTIME_PLAN_EXECUTOR_SOURCE: "plan_executor",
    RUNTIME_PLANNER_SOURCE: "planner",
    RUNTIME_PLAN_SUPERVISOR_SOURCE: "plan_supervisor",
    RUNTIME_PROMPT_PREVIEW_SOURCE: "prompt_preview",
    RUNTIME_SKILL_GENERATOR_SOURCE: "skill_generator",
    RUNTIME_SKILL_MATCHER_SOURCE: "skill_matcher",
    RUNTIME_SKILL_SOURCE: "skill",
    RUNTIME_STRATEGIST_REVIEW_SOURCE: "strategist",
    RUNTIME_STRATEGIST_SOURCE: "strategist",
    RUNTIME_SUBAGENT_SOURCE: "subagent",
    RUNTIME_SYSTEM_SOURCE: "system",
    RUNTIME_TASK_RUNNER_SOURCE: "task_runner",
    RUNTIME_WORKER_SOURCE: "worker",
    RUNTIME_WORKFLOW_RUNNER_SOURCE: "workflow_runner",
    RUNTIME_WORKFLOW_SERVICE_SOURCE: "workflow_service",
    RUNTIME_WORKFLOW_SOURCE: "workflow",
    RUNTIME_WORKSPACE_ARCHITECT_SOURCE: "workspace_architect",
    RUNTIME_WORKSPACE_SETUP_SOURCE: "workspace_setup",
}


def runtime_text_completion_model_role(source: str | None) -> str:
    return RUNTIME_TEXT_COMPLETION_SOURCE_MODEL_ROLES.get(
        str(source or "").strip(),
        "primary",
    )


async def runtime_resolve_text_completion_route(
    *,
    entity_id: str | None,
    user_id: str | None,
    source: str,
    model: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None, bool | None]:
    """Resolve tenant BYOK metadata/model for Runtime text completions."""

    if not entity_id:
        # Platform-scoped call (no tenant). Honor the admin-configured
        # platform backend model; unset falls through to the env default
        # inside chat_completion. API keys resolve per-provider from the
        # official platform keys as usual.
        if not model:
            try:
                from packages.core.services.model_settings import (
                    get_model_settings_cached,
                    platform_backend_model,
                )
                model = platform_backend_model(
                    await get_model_settings_cached()
                )
            except Exception:
                model = None
        return model, metadata, None

    resolved_metadata = metadata
    resolved_model = model
    byok: bool | None = None
    role = runtime_text_completion_model_role(source)
    try:
        from packages.core.ai.llm_client import metadata_has_native_byok
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )

        tenant_metadata = await resolve_llm_metadata_for_user(
            role,
            user_id=user_id,
            entity_id=entity_id,
            db=None,
        )
        if tenant_metadata:
            resolved_metadata = {**(metadata or {}), **tenant_metadata}
        if not resolved_model:
            resolved_model = await resolve_model_for_user(
                role,
                user_id=user_id,
                entity_id=entity_id,
                db=None,
            )
        routed_metadata = resolved_metadata
        if resolved_metadata and resolved_model:
            routed_metadata = {**resolved_metadata, "_resolved_model": resolved_model}
        if metadata_has_native_byok(routed_metadata):
            resolved_metadata = routed_metadata
            byok = True
    except Exception:
        resolved_metadata = metadata
        resolved_model = model

    return resolved_model, resolved_metadata, byok


async def runtime_execute_agent_prompt_preview_completion(
    *,
    entity_id: str,
    system_prompt: str,
    test_message: str,
) -> RuntimeTextCompletionResult:
    """Execute an agent prompt preview with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_agent_prompt_preview_messages(
            system_prompt=system_prompt,
            test_message=test_message,
        ),
        entity_id=entity_id,
        source=RUNTIME_PROMPT_PREVIEW_SOURCE,
        temperature=0.7,
        max_tokens=800,
    )


async def runtime_execute_text_completion(
    messages: Iterable[dict[str, Any]],
    *,
    entity_id: str | None = None,
    source: str = RUNTIME_COMPLETION_SOURCE,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    temperature: float = 0.7,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
    byok: bool | None = None,
    stream_handler: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimeTextCompletionResult:
    """Run a one-shot LLM completion with Runtime-owned billing context setup."""

    from packages.core.ai.llm_client import chat_completion
    from packages.core.ai.runtime.billing import runtime_ensure_billing_context

    resolved_model, resolved_metadata, resolved_byok = await runtime_resolve_text_completion_route(
        entity_id=entity_id,
        user_id=user_id,
        source=source,
        model=model,
        metadata=metadata,
    )
    if byok is not None:
        resolved_byok = byok

    if entity_id:
        billing_kwargs: dict[str, Any] = {
            "user_id": user_id,
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
        }
        if resolved_byok is not None:
            billing_kwargs["byok"] = resolved_byok
        runtime_ensure_billing_context(
            entity_id,
            source=source,
            **billing_kwargs,
        )
    content, usage = await chat_completion(
        list(messages),
        temperature=temperature,
        response_format=response_format,
        max_tokens=max_tokens,
        model=resolved_model,
        stream_handler=stream_handler,
        metadata=resolved_metadata,
    )
    return RuntimeTextCompletionResult(
        content=content or "",
        usage=dict(usage or {}),
    )
