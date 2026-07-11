from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.skill_forcing import (
    runtime_manual_skill_input,
    runtime_message_with_manual_skill_marker,
    runtime_resolve_manual_skill_refs,
    runtime_strip_manual_skill_tokens,
)


class ManualSkillResolutionError(ValueError):
    """Raised when a chat turn references skills outside the visible scope."""

    def __init__(self, missing_skill_ids: list[str]) -> None:
        self.missing_skill_ids = list(missing_skill_ids)
        super().__init__(
            "Skill not found or not available: "
            + ", ".join(self.missing_skill_ids)
        )


@dataclass(frozen=True)
class ChatManualSkillTurn:
    manual_skill_refs: list[dict[str, Any]]
    llm_base_message: str
    saved_user_base: str


async def prepare_chat_manual_skill_turn(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str | None,
    message: str,
    manual_skill_ids: str | None,
) -> ChatManualSkillTurn:
    """Resolve a chat turn's manual skill request and derive message variants."""

    manual_skill_refs, missing = await runtime_resolve_manual_skill_refs(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        manual_skill_ids=manual_skill_ids,
    )
    if missing:
        raise ManualSkillResolutionError(missing)

    if not manual_skill_refs:
        return ChatManualSkillTurn(
            manual_skill_refs=[],
            llm_base_message=message,
            saved_user_base=message,
        )

    llm_base_message = runtime_strip_manual_skill_tokens(message, manual_skill_refs)
    if not llm_base_message:
        llm_base_message = runtime_manual_skill_input(message)

    return ChatManualSkillTurn(
        manual_skill_refs=manual_skill_refs,
        llm_base_message=llm_base_message,
        saved_user_base=runtime_message_with_manual_skill_marker(
            message,
            manual_skill_refs,
        ),
    )
