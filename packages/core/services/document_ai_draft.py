from __future__ import annotations

from packages.core.ai.runtime import runtime_execute_document_ai_draft_completion


async def generate_document_ai_draft_content(
    *,
    entity_id: str,
    user_id: str | None,
    prompt: str,
    file_type: str,
    document_id: str | None = None,
) -> str:
    """Generate AI draft content through the Runtime completion boundary."""

    completion = await runtime_execute_document_ai_draft_completion(
        entity_id=entity_id,
        user_id=user_id,
        prompt=prompt,
        file_type=file_type,
        document_id=document_id,
    )
    return completion.content
