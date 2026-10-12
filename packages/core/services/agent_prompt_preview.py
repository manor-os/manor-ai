from __future__ import annotations

from packages.core.ai.runtime import runtime_execute_agent_prompt_preview_completion


async def preview_agent_prompt(
    *,
    entity_id: str,
    system_prompt: str,
    test_message: str,
    user_id: str | None = None,
) -> str:
    """Run an agent prompt preview through the Runtime completion boundary."""

    completion = await runtime_execute_agent_prompt_preview_completion(
        entity_id=entity_id,
        system_prompt=system_prompt,
        test_message=test_message,
        user_id=user_id,
    )
    return completion.content
