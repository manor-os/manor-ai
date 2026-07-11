from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface


RUNTIME_TOOL_CONTEXT_KEYS = frozenset(
    {
        "_agent_id_from_context",
        "_user_id_from_context",
        "_active_user_message_from_context",
        "_runtime_artifact_urls_from_context",
        "_dependency_artifact_urls_from_context",
        "_manual_skill_selected_from_context",
        "_manual_skill_slugs_from_context",
        "_legacy_tool_profile_from_context",
        "_runtime_envelope_from_context",
        "_allowed_tool_names_from_context",
        "_llm_metadata_from_context",
        "_llm_model_from_context",
        "workspace_id",
        "conversation_id",
        "task_id",
    }
)


def _string_set(values: Iterable[Any] | None) -> set[str]:
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in values if str(value or "").strip()}


def _url_set(values: Iterable[Any] | None) -> set[str]:
    return _string_set(values)


def runtime_allowed_tool_names_from_context(kwargs: dict[str, Any]) -> set[str] | None:
    allowed_tool_names = kwargs.get("_allowed_tool_names_from_context")
    if isinstance(allowed_tool_names, (list, tuple, set)):
        return _string_set(allowed_tool_names)
    return None


def runtime_manual_skill_slugs_from_context(kwargs: dict[str, Any]) -> set[str]:
    return {value.lower() for value in _string_set(kwargs.get("_manual_skill_slugs_from_context"))}


def runtime_manual_skill_selected_from_context(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("_manual_skill_selected_from_context"))


def runtime_active_user_message_from_context(kwargs: dict[str, Any]) -> str | None:
    message = kwargs.get("_active_user_message_from_context")
    return message if isinstance(message, str) else None


@dataclass(frozen=True)
class RuntimeToolCallContext:
    agent_id: str | None = None
    user_id: str | None = None
    active_user_message: str | None = None
    runtime_artifact_urls: frozenset[str] = frozenset()
    dependency_artifact_urls: frozenset[str] = frozenset()
    manual_skill_selected: bool = False
    manual_skill_slugs: frozenset[str] = frozenset()
    legacy_tool_profile: str | None = None
    runtime_envelope: Any | None = None
    allowed_tool_names: frozenset[str] | None = None
    llm_metadata: dict[str, Any] | None = None
    llm_model: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None


def runtime_tool_call_context_from_kwargs(kwargs: dict[str, Any]) -> RuntimeToolCallContext:
    allowed = runtime_allowed_tool_names_from_context(kwargs)
    return RuntimeToolCallContext(
        agent_id=str(kwargs.get("_agent_id_from_context") or "") or None,
        user_id=str(kwargs.get("_user_id_from_context") or "") or None,
        active_user_message=runtime_active_user_message_from_context(kwargs),
        runtime_artifact_urls=frozenset(_url_set(kwargs.get("_runtime_artifact_urls_from_context"))),
        dependency_artifact_urls=frozenset(_url_set(kwargs.get("_dependency_artifact_urls_from_context"))),
        manual_skill_selected=runtime_manual_skill_selected_from_context(kwargs),
        manual_skill_slugs=frozenset(runtime_manual_skill_slugs_from_context(kwargs)),
        legacy_tool_profile=str(kwargs.get("_legacy_tool_profile_from_context") or "") or None,
        runtime_envelope=kwargs.get("_runtime_envelope_from_context"),
        allowed_tool_names=frozenset(allowed) if allowed is not None else None,
        llm_metadata=(
            dict(kwargs["_llm_metadata_from_context"])
            if isinstance(kwargs.get("_llm_metadata_from_context"), dict)
            else None
        ),
        llm_model=str(kwargs.get("_llm_model_from_context") or "") or None,
        workspace_id=str(kwargs.get("workspace_id") or "") or None,
        conversation_id=str(kwargs.get("conversation_id") or "") or None,
        task_id=str(kwargs.get("task_id") or "") or None,
    )


def runtime_tool_call_context_is_external_customer(kwargs: dict[str, Any]) -> bool:
    """Return true when a tool is executing for a customer/external surface."""

    context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = context.runtime_envelope
    surface = getattr(envelope, "surface", None)
    profile = getattr(envelope, "profile", None)
    surface_value = getattr(surface, "value", surface)
    profile_value = getattr(profile, "value", profile)
    return surface_value in {
        ChatSurface.PUBLIC_CUSTOMER_CHAT.value,
        ChatSurface.EXTERNAL_CHANNEL_CHAT.value,
    } or profile_value in {
        RuntimeProfile.EXTERNAL_CUSTOMER_SAFE.value,
        RuntimeProfile.EXTERNAL_CHANNEL_SAFE.value,
    }


def runtime_tool_call_context_is_public_customer(kwargs: dict[str, Any]) -> bool:
    """Return true only for QR/public webchat customer turns."""

    context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = context.runtime_envelope
    surface = getattr(envelope, "surface", None)
    surface_value = getattr(surface, "value", surface)
    return surface_value == ChatSurface.PUBLIC_CUSTOMER_CHAT.value


def runtime_injected_tool_context_args(
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    active_user_message: str | None = None,
    runtime_artifact_urls: Iterable[str] | None = None,
    dependency_artifact_urls: Iterable[str] | None = None,
    manual_skill_selected: bool = False,
    manual_skill_slugs: Iterable[str] | None = None,
    legacy_tool_profile: str | None = None,
    runtime_envelope: Any | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "_agent_id_from_context": agent_id,
        "_user_id_from_context": user_id,
        "_active_user_message_from_context": active_user_message,
        "_manual_skill_selected_from_context": manual_skill_selected,
        "_manual_skill_slugs_from_context": list(manual_skill_slugs or []),
        "_legacy_tool_profile_from_context": legacy_tool_profile,
    }
    from packages.core.ai.runtime.artifacts import (
        runtime_current_artifact_urls,
        runtime_current_dependency_artifact_urls,
    )

    merged_runtime_artifact_urls = set(runtime_current_artifact_urls()) | _url_set(runtime_artifact_urls)
    merged_dependency_artifact_urls = set(runtime_current_dependency_artifact_urls()) | _url_set(
        dependency_artifact_urls
    )

    if runtime_envelope is not None:
        args["_runtime_envelope_from_context"] = runtime_envelope
    if runtime_artifact_urls is not None or merged_runtime_artifact_urls:
        args["_runtime_artifact_urls_from_context"] = sorted(
            str(url).strip() for url in merged_runtime_artifact_urls if str(url or "").strip()
        )
    if dependency_artifact_urls is not None or merged_dependency_artifact_urls:
        args["_dependency_artifact_urls_from_context"] = sorted(
            str(url).strip() for url in merged_dependency_artifact_urls if str(url or "").strip()
        )
    if isinstance(llm_metadata, dict) and llm_metadata:
        args["_llm_metadata_from_context"] = dict(llm_metadata)
    if llm_model:
        args["_llm_model_from_context"] = str(llm_model)
    if allowed_tool_names is not None:
        args["_allowed_tool_names_from_context"] = sorted(
            str(name) for name in allowed_tool_names if str(name or "").strip()
        )
    if workspace_id:
        args["workspace_id"] = workspace_id
    if conversation_id:
        args["conversation_id"] = conversation_id
    if task_id:
        args["task_id"] = task_id
    return args
