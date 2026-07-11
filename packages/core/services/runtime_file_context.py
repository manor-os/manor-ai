from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY
from packages.core.services.file_context import FileAttachments, build_file_context


_HASH_REF_RE = re.compile(r"#\[([^\]]*)\]\(doc:([^)]+)\)")


@dataclass(frozen=True)
class RuntimeFileContextTurn:
    """Prepared file attachments for one Runtime chat turn."""

    cleaned_message: str
    attachments: FileAttachments = field(default_factory=FileAttachments)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)


def extract_runtime_inline_document_refs(message: str) -> tuple[str, list[str]]:
    """Extract ``#[name](doc:id)`` tokens and keep a readable marker in text."""

    doc_ids: list[str] = []

    def _replace(match: re.Match) -> str:
        doc_ids.append(match.group(2))
        return f"#{match.group(1)}"

    return _HASH_REF_RE.sub(_replace, message or ""), doc_ids


def runtime_file_context_metadata(attachments: FileAttachments) -> dict[str, Any]:
    context = attachments.to_runtime_context()
    if not context:
        return {}
    return {RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY: context}


async def prepare_runtime_file_context_turn(
    *,
    message: str,
    document_ids: str | list[str] | tuple[str, ...] | None,
    files: list[UploadFile],
    entity_id: str,
    db: AsyncSession,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> RuntimeFileContextTurn:
    cleaned_message, inline_doc_ids = extract_runtime_inline_document_refs(message)
    doc_ids = _normalize_document_ids(document_ids)
    doc_ids.extend(inline_doc_ids)
    attachments = await build_file_context(
        files,
        doc_ids,
        entity_id,
        db,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return RuntimeFileContextTurn(
        cleaned_message=cleaned_message,
        attachments=attachments,
        runtime_metadata=runtime_file_context_metadata(attachments),
    )


def runtime_message_with_file_attachments(
    base_message: str,
    attachments: FileAttachments,
) -> str | list[dict[str, Any]]:
    text_part = base_message
    if attachments.text_context:
        text_part = (
            f"{base_message}\n\n<attached_files>\n"
            f"{attachments.text_context}\n</attached_files>"
        )
    if not attachments.image_blocks:
        return text_part
    return [
        {"type": "text", "text": text_part},
        *attachments.image_blocks,
    ]


def runtime_saved_message_with_file_references(
    base_message: str,
    attachments: FileAttachments,
) -> str:
    if not attachments.image_urls:
        return base_message
    reference_lines = attachments.image_reference_lines or [
        f"[Image: {idx + 1} -> {url}]"
        for idx, url in enumerate(attachments.image_urls)
    ]
    return "\n".join([base_message, *reference_lines]).strip()


def _normalize_document_ids(
    document_ids: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    if isinstance(document_ids, str):
        return [value.strip() for value in document_ids.split(",") if value.strip()]
    return [
        str(value).strip()
        for value in document_ids or ()
        if str(value).strip()
    ]
