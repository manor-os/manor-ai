from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.file_context import (
    editor_current_document_content,
    editor_context_without_inline_document_content,
    editor_file_identity_from_context,
    FILE_CONTEXT_METADATA_KEY,
    RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY,
)
from packages.core.ai.runtime.memory import (
    MEMORY_MOUNTS_METADATA_KEY,
    runtime_allows_workspace_memory_reader,
)
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.surfaces import ChatSurface

logger = logging.getLogger(__name__)

ContextBlockKind = Literal[
    "external_surface_context",
    "file_editor_live_edit",
    "file_editor_current_document",
    "voice_session",
    "workspace_scope",
    "manual_skill_selection",
    "legacy_extra_context",
    "workspace_summary",
    "runtime_mounts",
]


@dataclass(frozen=True)
class RuntimeContextBlock:
    kind: ContextBlockKind
    title: str
    content: str
    source: str
    key: str | None = None
    include_in_prompt: bool = True
    metadata: dict = field(default_factory=dict)

    def render(self) -> str:
        clean = (self.content or "").strip()
        if not clean:
            return ""
        return f"--- {self.title} ---\n{clean}"


def _should_include_workspace_summary(
    request: AIRuntimeRequest,
    envelope: RuntimeEnvelope,
) -> bool:
    return bool(
        request.workspace_id
        and runtime_allows_workspace_memory_reader(envelope, request.workspace_id)
    )


async def resolve_runtime_context_blocks(
    db: AsyncSession | None,
    request: AIRuntimeRequest,
    envelope: RuntimeEnvelope,
    *,
    legacy_extra_context: str | None = None,
    manual_skill_refs: Iterable[dict] | None = None,
) -> list[RuntimeContextBlock]:
    blocks: list[RuntimeContextBlock] = []
    external_context = _external_surface_context_block(request)
    if external_context:
        blocks.append(external_context)

    editor_context = _file_editor_live_edit_context_block(request)
    if editor_context:
        blocks.append(editor_context)
    current_document_context = _file_editor_current_document_context_block(request)
    if current_document_context:
        blocks.append(current_document_context)

    voice_context = _voice_session_context_block(request)
    if voice_context:
        blocks.append(voice_context)

    workspace_scope_context = _workspace_scope_context_block(request)
    if workspace_scope_context:
        blocks.append(workspace_scope_context)

    manual_skill_context = _manual_skill_selection_context_block(request, manual_skill_refs)
    if manual_skill_context:
        blocks.append(manual_skill_context)

    extra = (legacy_extra_context or "").strip()
    if extra:
        blocks.append(
            RuntimeContextBlock(
                kind="legacy_extra_context",
                title="Runtime Context",
                content=extra,
                source="workspace_runtime.extra_context",
                key=request.task_id or request.thread_ref_id or request.conversation_id,
            )
        )

    if db and _should_include_workspace_summary(request, envelope):
        try:
            from packages.core.workspace_chat.context import get_summary

            summary = await get_summary(db, request.workspace_id or "", request.entity_id or "")
            if summary:
                blocks.append(
                    RuntimeContextBlock(
                        kind="workspace_summary",
                        title="Workspace Context",
                        content=summary,
                        source="workspace_chat.context.get_summary",
                        key=request.workspace_id,
                    )
                )
        except Exception:
            logger.debug("Runtime workspace context block failed", exc_info=True)

    mount_lines: list[str] = []
    if envelope.memory_mounts:
        mount_lines.append("memory: " + ", ".join(envelope.memory_mounts))
    if envelope.file_context_mounts:
        mount_lines.append("files: " + ", ".join(envelope.file_context_mounts))
    if mount_lines:
        blocks.append(
            RuntimeContextBlock(
                kind="runtime_mounts",
                title="Runtime Mounts",
                content="\n".join(mount_lines),
                source="runtime.envelope",
                key=envelope.surface.value,
                include_in_prompt=False,
                metadata={
                    "memory_mounts": envelope.metadata.get(MEMORY_MOUNTS_METADATA_KEY, ()),
                    "file_context_mounts": envelope.metadata.get(FILE_CONTEXT_METADATA_KEY, ()),
                    "attachment_context": envelope.metadata.get(
                        RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY,
                        {},
                    ),
                },
            )
        )
    return blocks


def _external_surface_context_block(request: AIRuntimeRequest) -> RuntimeContextBlock | None:
    channel_context = request.channel_context
    if request.surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
        from packages.core.ai.runtime.prompt_guidance import runtime_public_webchat_context_preamble

        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        content = runtime_public_webchat_context_preamble(
            channel_label=metadata.get("channel_label")
            or (channel_context.channel_type if channel_context else None)
            or "public webchat",
            sender_display=(
                channel_context.display_name or channel_context.source_id
                if channel_context
                else None
            ),
            owner_entity_id=request.entity_id,
            visitor_entity_id=metadata.get("visitor_entity_id"),
            visitor_verified=bool(
                metadata.get(
                    "visitor_verified",
                    channel_context.is_verified if channel_context else False,
                )
            ),
            channel_language=metadata.get("channel_language")
            or (channel_context.channel_language if channel_context else None),
        )
        return RuntimeContextBlock(
            kind="external_surface_context",
            title="External Sender Context",
            content=content,
            source="runtime.prompt_guidance.public_webchat",
            key=channel_context.channel_contact_id if channel_context else request.conversation_id,
        )

    if request.surface == ChatSurface.EXTERNAL_CHANNEL_CHAT and channel_context:
        from packages.core.ai.runtime.prompt_guidance import runtime_external_channel_context_preamble

        content = runtime_external_channel_context_preamble(channel_context.as_dict())
        if not content:
            return None
        return RuntimeContextBlock(
            kind="external_surface_context",
            title="External Sender Context",
            content=content,
            source="runtime.prompt_guidance.external_channel",
            key=channel_context.channel_contact_id or channel_context.source_id,
        )

    return None


def _file_editor_live_edit_context_block(request: AIRuntimeRequest) -> RuntimeContextBlock | None:
    if request.surface != ChatSurface.FILE_EDITOR_CHAT:
        return None
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_file_editor_live_edit_guidance,
    )

    editor_context = request.editor_context or {}
    key = editor_file_identity_from_context(
        editor_context,
        fallback=request.conversation_id or "current_editor_file",
    )
    return RuntimeContextBlock(
        kind="file_editor_live_edit",
        title="File Editor Live Edit",
        content=runtime_file_editor_live_edit_guidance(editor_context),
        source="runtime.prompt_guidance.file_editor_live_edit",
        key=str(key) if key else None,
        metadata={
            "editor_context": editor_context_without_inline_document_content(editor_context)
        },
    )


def _file_editor_current_document_context_block(
    request: AIRuntimeRequest,
) -> RuntimeContextBlock | None:
    if request.surface != ChatSurface.FILE_EDITOR_CHAT:
        return None
    editor_context = request.editor_context or {}
    content = editor_current_document_content(editor_context)
    if content is None:
        return None
    key = editor_file_identity_from_context(
        editor_context,
        fallback=request.conversation_id or "current_editor_file",
    )
    return RuntimeContextBlock(
        kind="file_editor_current_document",
        title="Current Editor Document",
        content="\n".join([
            "<manor-current-document>",
            content,
            "</manor-current-document>",
        ]),
        source="runtime.editor_context.current_document",
        key=str(key) if key else None,
        metadata={
            "content_chars": len(content),
            "editor_type": editor_context.get("editor_type") or editor_context.get("editorType"),
            "file_type": editor_context.get("file_type") or editor_context.get("fileType"),
        },
    )


def _voice_session_context_block(request: AIRuntimeRequest) -> RuntimeContextBlock | None:
    if request.surface != ChatSurface.VOICE_CHAT:
        return None
    from packages.core.ai.runtime.prompt_guidance import runtime_voice_session_guidance

    return RuntimeContextBlock(
        kind="voice_session",
        title="Voice Session Runtime",
        content=runtime_voice_session_guidance(),
        source="runtime.prompt_guidance.voice_session",
        key=request.conversation_id or request.user_id,
    )


def _workspace_scope_context_block(request: AIRuntimeRequest) -> RuntimeContextBlock | None:
    if (
        not request.workspace_id
        or request.surface != ChatSurface.EXTERNAL_CHANNEL_CHAT
    ):
        return None
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_workspace_scope_context_preamble,
    )

    return RuntimeContextBlock(
        kind="workspace_scope",
        title="Workspace Scope",
        content=runtime_workspace_scope_context_preamble(),
        source="runtime.prompt_guidance.workspace_scope",
        key=request.workspace_id,
    )


def _manual_skill_selection_context_block(
    request: AIRuntimeRequest,
    manual_skill_refs: Iterable[dict] | None,
) -> RuntimeContextBlock | None:
    refs = list(manual_skill_refs or ())
    if not refs:
        return None
    from packages.core.ai.runtime.skill_forcing import runtime_manual_skill_context

    content = runtime_manual_skill_context(refs)
    if not content:
        return None
    return RuntimeContextBlock(
        kind="manual_skill_selection",
        title="Runtime Context",
        content=content,
        source="runtime.skill_forcing.manual_skill_context",
        key=request.conversation_id or request.thread_ref_id,
    )


def render_context_blocks(blocks: list[RuntimeContextBlock]) -> str:
    return "\n\n".join(
        rendered
        for block in blocks
        if block.include_in_prompt and (rendered := block.render())
    )
