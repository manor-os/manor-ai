from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from packages.core.ai.runtime.requests import AIRuntimeRequest, ChannelRuntimeContext
from packages.core.ai.runtime.skill_forcing import runtime_message_text_for_intent
from packages.core.ai.runtime.surfaces import (
    ChatSurface,
    infer_chat_surface,
    normalize_surface,
    surface_for_channel_context,
)


def runtime_channel_context_from_value(
    value: ChannelRuntimeContext | Mapping[str, Any] | None,
) -> ChannelRuntimeContext | None:
    if isinstance(value, ChannelRuntimeContext):
        return value
    if isinstance(value, Mapping):
        return ChannelRuntimeContext.from_mapping(dict(value))
    return None


def runtime_manual_skill_ids_from_refs(
    skill_refs: Iterable[Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    seen: set[str] = set()
    ids: list[str] = []
    for ref in skill_refs or ():
        value = str(ref.get("id") or ref.get("slug") or ref.get("name") or "").strip()
        if value and value not in seen:
            seen.add(value)
            ids.append(value)
    return tuple(ids)


def runtime_input_preview(message: str | list[dict] | None, *, limit: int = 500) -> str | None:
    if message is None:
        return None
    return runtime_message_text_for_intent(message)[:limit]


def _runtime_metadata(
    *,
    legacy_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(metadata or {})
    if legacy_path:
        out.setdefault("legacy_path", legacy_path)
    return out


def runtime_request_for_chat_turn(
    *,
    surface: ChatSurface | str | None = None,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    message: str | list[dict] | None = None,
    manual_skill_refs: Iterable[Mapping[str, Any]] | None = None,
    channel_context: ChannelRuntimeContext | Mapping[str, Any] | None = None,
    editor_context: dict[str, Any] | None = None,
    ephemeral: bool = False,
    legacy_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AIRuntimeRequest:
    return AIRuntimeRequest(
        surface=infer_chat_surface(
            surface=surface,
            workspace_id=workspace_id,
            agent_id=agent_id,
            ephemeral=ephemeral,
        ),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        input_preview=runtime_input_preview(message),
        manual_skill_ids=runtime_manual_skill_ids_from_refs(manual_skill_refs),
        channel_context=runtime_channel_context_from_value(channel_context),
        editor_context=editor_context,
        metadata=_runtime_metadata(legacy_path=legacy_path, metadata=metadata),
    )


def runtime_request_for_surface_turn(
    *,
    surface: ChatSurface | str,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    message: str | list[dict] | None = None,
    manual_skill_refs: Iterable[Mapping[str, Any]] | None = None,
    channel_context: ChannelRuntimeContext | Mapping[str, Any] | None = None,
    editor_context: dict[str, Any] | None = None,
    legacy_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AIRuntimeRequest:
    resolved_surface = normalize_surface(surface)
    if resolved_surface is None:
        raise ValueError("Runtime surface is required for non-chat AI entrypoints")
    return AIRuntimeRequest(
        surface=resolved_surface,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        input_preview=runtime_input_preview(message),
        manual_skill_ids=runtime_manual_skill_ids_from_refs(manual_skill_refs),
        channel_context=runtime_channel_context_from_value(channel_context),
        editor_context=editor_context,
        metadata=_runtime_metadata(legacy_path=legacy_path, metadata=metadata),
    )


def runtime_request_for_channel_turn(
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    message: str | list[dict] | None = None,
    sender_context: ChannelRuntimeContext | Mapping[str, Any] | None = None,
    legacy_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AIRuntimeRequest:
    channel_context = runtime_channel_context_from_value(sender_context)
    return AIRuntimeRequest(
        surface=surface_for_channel_context(
            channel_type=channel_context.channel_type if channel_context else None,
            is_verified=channel_context.is_verified if channel_context else False,
            role=channel_context.role if channel_context else None,
            user_id=channel_context.user_id if channel_context else None,
            workspace_id=workspace_id,
            agent_id=agent_id,
        ),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        input_preview=runtime_input_preview(message),
        channel_context=channel_context,
        metadata=_runtime_metadata(legacy_path=legacy_path, metadata=metadata),
    )
