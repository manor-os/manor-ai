from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime import runtime_generate_document_file

from .diagram import generate_diagram_file


async def handle_document(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> str:
    content = kwargs.get("content")
    if content is None:
        content = params.get("content")
    requested_file_type = str(
        kwargs.get("file_type") or params.get("file_type") or ""
    ).strip().lower().lstrip(".")
    requested_diagram = (
        requested_file_type in {"diagram", "diagram.json"}
        or name.lower().endswith((".diagram", ".diagram.json"))
    )
    if not content and requested_diagram:
        if not prompt:
            return json.dumps({"error": "diagram documents require prompt or content"}, ensure_ascii=False)
        return await generate_diagram_file(
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            prompt=prompt,
            name=name,
            params=params,
            workspace_id=kwargs.get("workspace_id"),
            task_id=kwargs.get("task_id"),
            agent_id=agent_id,
            approval_token=kwargs.get("approval_token") or params.get("approval_token"),
            expected_sha256=kwargs.get("expected_sha256") or params.get("expected_sha256"),
        )
    if not content:
        return json.dumps({
            "error": "kind=document requires complete content. Use prompt for skill/media kinds.",
        }, ensure_ascii=False)
    return await runtime_generate_document_file(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        name=name or params.get("name") or "generated-document.md",
        content=str(content),
        file_type=kwargs.get("file_type") or params.get("file_type") or "md",
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        expected_sha256=kwargs.get("expected_sha256") or params.get("expected_sha256"),
        workspace_id=kwargs.get("workspace_id"),
        task_id=kwargs.get("task_id"),
        agent_id=agent_id,
    )
