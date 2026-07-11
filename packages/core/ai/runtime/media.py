from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeMediaCredentials:
    api_key: str
    provider: str
    catalog_provider: str
    base_url_override: str = ""
    is_byok: bool = False


async def runtime_generate_image_media(
    *,
    entity_id: str,
    user_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Run image generation through the Runtime media boundary."""

    from packages.core.ai.tools.extended_tools import _generate_image_handler

    return await _generate_image_handler(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        name=name,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        **dict(params or {}),
    )


async def runtime_generate_audio_media(
    *,
    entity_id: str,
    user_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Run audio generation through the Runtime media boundary."""

    from packages.core.ai.tools.extended_tools import _generate_audio_handler

    return await _generate_audio_handler(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        name=name,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        agent_id=agent_id,
        **dict(params or {}),
    )


async def runtime_generate_video_media(
    *,
    entity_id: str,
    user_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    active_user_message: str | None = None,
    runtime_artifact_urls: Iterable[str] | None = None,
    dependency_artifact_urls: Iterable[str] | None = None,
) -> str:
    """Run video generation through the Runtime media boundary."""

    from packages.core.ai.tools.extended_tools import _generate_video_handler

    return await _generate_video_handler(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        name=name,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        _active_user_message_from_context=active_user_message,
        _runtime_artifact_urls_from_context=list(runtime_artifact_urls or []),
        _dependency_artifact_urls_from_context=list(dependency_artifact_urls or []),
        **dict(params or {}),
    )


async def runtime_resolve_video_recovery_credentials(
    *,
    user_id: str,
    entity_id: str,
    model: str,
    stored_provider: str,
) -> RuntimeMediaCredentials:
    """Resolve provider credentials for recovering a stranded video job."""

    from packages.core.ai.tools.extended_tools import (
        _catalog_provider,
        _platform_native_media_credential_async,
        _prefer_native_video_credentials,
        _resolve_user_media_credentials,
    )

    catalog_provider = _catalog_provider(model)
    provider_hint = str(stored_provider or "").lower()
    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role="video",
    )
    if provider_hint != "openrouter":
        if not is_byok and catalog_provider in {"bytedance", "kwaivgi"}:
            native_key, native_base_url = await _platform_native_media_credential_async(catalog_provider)
            if native_key:
                api_key = native_key
                base_url_override = native_base_url or base_url_override
                is_byok = False
        else:
            api_key, is_byok = _prefer_native_video_credentials(
                api_key,
                catalog_provider,
                is_byok,
            )
    if not api_key and provider_hint != "openrouter":
        api_key, native_base_url = await _platform_native_media_credential_async(catalog_provider)
        base_url_override = native_base_url or base_url_override
        is_byok = False

    provider = (
        "openrouter"
        if api_key.startswith("sk-or-") or provider_hint == "openrouter"
        else (provider_hint or catalog_provider)
    )
    return RuntimeMediaCredentials(
        api_key=api_key,
        provider=provider,
        catalog_provider=catalog_provider,
        base_url_override=base_url_override,
        is_byok=is_byok,
    )


async def runtime_resolve_video_generation_credentials(
    *,
    user_id: str,
    entity_id: str,
    model: str,
    stored_adapter_name: str,
    openrouter_adapter_name: str,
) -> RuntimeMediaCredentials:
    """Resolve provider credentials for a stored video generation job."""

    from packages.core.ai.tools.extended_tools import (
        _catalog_provider,
        _platform_native_media_credential_async,
        _prefer_native_video_credentials,
        _resolve_user_media_credentials,
    )

    catalog_provider = _catalog_provider(model)
    adapter_name = str(stored_adapter_name or "").strip()
    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role="video",
    )
    if adapter_name == openrouter_adapter_name:
        if not api_key.startswith("sk-or-"):
            api_key, _openrouter_base_url = await _platform_native_media_credential_async("openrouter")
            is_byok = False
        base_url_override = ""
    else:
        if not is_byok and catalog_provider in {"bytedance", "kwaivgi"}:
            native_key, native_base_url = await _platform_native_media_credential_async(catalog_provider)
            if native_key:
                api_key = native_key
                base_url_override = native_base_url or base_url_override
                is_byok = False
        else:
            api_key, is_byok = _prefer_native_video_credentials(
                api_key,
                catalog_provider,
                is_byok,
            )

    if not api_key:
        api_key = (
            ""
            if adapter_name == openrouter_adapter_name
            else (await _platform_native_media_credential_async(catalog_provider))[0]
        )
        if api_key:
            is_byok = False

    return RuntimeMediaCredentials(
        api_key=api_key,
        provider=catalog_provider,
        catalog_provider=catalog_provider,
        base_url_override=base_url_override,
        is_byok=is_byok,
    )
