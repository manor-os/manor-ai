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


def _trusted_workflow_arguments(workflow_context: dict[str, Any] | None) -> dict[str, Any]:
    """The trusted Workflow arguments (run, project) a media call was made under.

    Generation reads them to use what the project keeps: its voices and its
    named images. Anything else a caller passes is dropped.
    """
    from packages.core.ai.runtime.workflow_tools import RUNTIME_WORKFLOW_TOOL_CONTEXT_ARGUMENTS

    return {
        key: value for key, value in (workflow_context or {}).items()
        if key in RUNTIME_WORKFLOW_TOOL_CONTEXT_ARGUMENTS.values() and value is not None
    }


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
    workspace_shared: bool = False,
    workflow_context: dict[str, Any] | None = None,
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
        _workspace_shared_from_context=workspace_shared,
        **dict(params or {}),
        **_trusted_workflow_arguments(workflow_context),
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
    workflow_context: dict[str, Any] | None = None,
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
        **_trusted_workflow_arguments(workflow_context),
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
    workflow_context: dict[str, Any] | None = None,
    lineage: dict[str, str] | None = None,
) -> str:
    """Run video generation through the Runtime media boundary.

    ``lineage`` (``regenerated_from``/``lineage_root``) marks the job as a new
    version of an earlier one; only server code that loaded that job sets it.
    """

    from packages.core.ai.tools.extended_tools import _generate_video_handler

    return await _generate_video_handler(
        _media_lineage_from_context=dict(lineage or {}),
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
        # Caller params cannot claim a lineage; only ``lineage`` sets it.
        **{key: value for key, value in (params or {}).items() if key != "_media_lineage_from_context"},
        **_trusted_workflow_arguments(workflow_context),
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
        _resolve_user_video_model,
        _selectable_media_model,
    )

    catalog_provider = _catalog_provider(model)
    provider_hint = str(stored_provider or "").lower()
    if provider_hint == "vercel":
        from packages.core.services.model_gateway import resolve_gateway_credential

        credential = await resolve_gateway_credential(
            "vercel",
            reason="media.video.vercel_gateway_recovery_key",
        )
        return RuntimeMediaCredentials(
            api_key=credential.api_key if credential else "",
            provider="vercel",
            catalog_provider=catalog_provider,
            base_url_override=credential.base_url if credential else "",
            is_byok=False,
        )
    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role="video",
        **({"require_saved_base": True} if catalog_provider == "xai" else {}),
    )
    if catalog_provider == "xai":
        if not is_byok or await _resolve_user_video_model(user_id, entity_id) != model:
            api_key, is_byok = "", False
        return RuntimeMediaCredentials(api_key, "xai", catalog_provider, base_url_override, is_byok)
    if not _selectable_media_model(model, "video"):
        # Outside the catalog there is no managed route: only the Account's own
        # key, saved for this exact model.
        if not is_byok or await _resolve_user_video_model(user_id, entity_id) != model:
            api_key, is_byok = "", False
        return RuntimeMediaCredentials(api_key, catalog_provider, catalog_provider, base_url_override, is_byok)
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
    vercel_adapter_name: str = "vercel_gateway",
) -> RuntimeMediaCredentials:
    """Resolve provider credentials for a stored video generation job."""

    from packages.core.ai.tools.extended_tools import (
        _catalog_provider,
        _platform_native_media_credential_async,
        _prefer_native_video_credentials,
        _resolve_user_media_credentials,
        _resolve_user_video_model,
        _selectable_media_model,
    )

    catalog_provider = _catalog_provider(model)
    adapter_name = str(stored_adapter_name or "").strip()
    if adapter_name == vercel_adapter_name:
        from packages.core.services.model_gateway import resolve_gateway_credential

        credential = await resolve_gateway_credential(
            "vercel",
            reason="media.video.vercel_gateway_key",
        )
        return RuntimeMediaCredentials(
            api_key=credential.api_key if credential else "",
            provider="vercel",
            catalog_provider=catalog_provider,
            base_url_override=credential.base_url if credential else "",
            is_byok=False,
        )
    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role="video",
        **({"require_saved_base": True} if catalog_provider == "xai" else {}),
    )
    if catalog_provider == "xai":
        if not is_byok or await _resolve_user_video_model(user_id, entity_id) != model:
            api_key, is_byok = "", False
        return RuntimeMediaCredentials(api_key, "xai", catalog_provider, base_url_override, is_byok)
    if not _selectable_media_model(model, "video"):
        # Outside the catalog there is no managed route: only the Account's own
        # key, saved for this exact model.
        if not is_byok or await _resolve_user_video_model(user_id, entity_id) != model:
            api_key, is_byok = "", False
        return RuntimeMediaCredentials(api_key, catalog_provider, catalog_provider, base_url_override, is_byok)
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


# ── recording tool helpers for sibling tool modules ─────────────────────────
# Tool modules never import each other (``test_production_tool_to_tool_imports_
# stay_runtime_owned``); the creative studio tools reach recording_tools' schema
# and path helpers through these runtime-owned bridges.


def runtime_recording_tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    from packages.core.ai.tools.recording_tools import _schema

    return _schema(name, description, properties, required)


async def runtime_recording_output_scope(
    entity_id: str, workspace_id: str | None, task_id: str | None, output_directory: str,
) -> tuple[str, str]:
    from packages.core.ai.tools.recording_tools import _scope

    return await _scope(entity_id, workspace_id, task_id, output_directory)


async def runtime_recording_source_path(reference: str, ctx: dict) -> tuple[str, str]:
    from packages.core.ai.tools.recording_tools import _source_path

    return await _source_path(reference, ctx)


async def runtime_document_ref_paths(refs: Any, entity_id: str, *, limit: int) -> list[str]:
    """Knowledge paths of picked Documents, in order and without repeats.

    A ``documents`` run input carries refs as the browser sent them, so only the
    ``document_id`` is trusted: each path comes from the Document row. Callers
    still open each path through their normal file access check.
    """
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    if refs in (None, "", []):
        return []
    if not isinstance(refs, list) or not all(isinstance(ref, dict) for ref in refs):
        raise ValueError("Pick files from Knowledge or upload them")
    ids = list(dict.fromkeys(str(ref.get("document_id") or "").strip() for ref in refs))
    if "" in ids:
        raise ValueError("Each picked file needs its Knowledge document")
    if len(ids) > limit:
        raise ValueError(f"Pick at most {limit} file{'s' if limit != 1 else ''}")
    async with async_session() as db:
        rows = (await db.execute(select(Document.id, Document.fs_path).where(
            Document.entity_id == entity_id, Document.id.in_(ids), Document.is_trashed.is_(False),
        ))).all()
    found = dict(rows)
    if any(document_id not in found for document_id in ids):
        raise ValueError("A picked file is no longer in Knowledge; pick it again")
    if any(not found[document_id] for document_id in ids):
        raise ValueError("A picked Knowledge document has no stored file to read; upload the file instead")
    return [found[document_id] for document_id in ids]
