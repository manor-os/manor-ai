"""Write bytes to the entity filesystem and register them in Knowledge in one
projection transaction.

Callers must already hold ``runtime_entity_filesystem_mutation_lock`` and have
run ``runtime_guard_file_mutation``. Raises ``RuntimeFileCommitError`` /
``RuntimeFileProjectionError`` unchanged.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KnowledgeFileRecord:
    document_id: str | None
    name: str
    file_size: int
    mime_type: str
    fs_path: str


async def project_bytes_into_knowledge(
    *,
    entity_id: str,
    entity_dir: str,
    rel_path: str,
    content_bytes: bytes,
    source: str,
    created_by: str,
    tool_name: str,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    transaction_factory: Any | None = None,
) -> KnowledgeFileRecord:
    if transaction_factory is None:
        from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransactionFactory

        transaction_factory = RuntimeFileProjectionTransactionFactory
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    async with transaction_factory.create(entity_id) as transaction:
        target = await transaction.write_bytes(
            rel_path,
            content_bytes,
            expected_content_sha256=content_sha256,
            expected_size=len(content_bytes),
            allow_empty=False,
            require_missing=True,
        )
        sync = await transaction.project_file(
            abs_path=target,
            entity_root=entity_dir,
            source=source,
            created_by=created_by,
            force=True,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=tool_name,
            expected_content_sha256=content_sha256,
        )
        await transaction.commit()
    rel = os.path.relpath(target, entity_dir).replace(os.sep, "/")
    canonical = getattr(sync, "fs_path", None) or rel
    name = getattr(sync, "name", None) or os.path.basename(target)
    size = getattr(sync, "file_size", None)
    mime = getattr(sync, "mime_type", None) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return KnowledgeFileRecord(
        document_id=getattr(sync, "document_id", None),
        name=name,
        file_size=len(content_bytes) if size is None else int(size),
        mime_type=mime,
        fs_path=canonical,
    )
