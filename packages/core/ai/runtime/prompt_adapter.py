from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.prompt_sections import (
    RuntimePromptMode,
    runtime_prompt_section_renderers,
    runtime_prompt_section_specs,
)

logger = logging.getLogger(__name__)

PromptMode = RuntimePromptMode


@dataclass
class ChatContext:
    """All context available when assembling a runtime prompt."""

    db: AsyncSession | None = None
    entity_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    thread_ref_kind: str | None = None
    thread_ref_id: str | None = None
    runtime_profile: str | None = None
    legacy_runtime_profile: str | None = None
    runtime_surface: str | None = None
    runtime_profile_name: str | None = None
    runtime_envelope: Any = None
    runtime_context_blocks: list[Any] = field(default_factory=list)
    runtime_skill_descriptors: list[Any] = field(default_factory=list)
    model: str | None = None
    llm_metadata: dict[str, Any] | None = None
    active_user_message: str | None = None
    manual_skill_selected: bool = False
    mode: PromptMode = "full"

    user: Any = None
    entity: Any = None
    agent: Any = None
    workspace: Any = None

    tools: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    allowed_tool_names: set[str] = field(default_factory=set)
    auto_forced_tool_calls: list[dict] = field(default_factory=list)

    extra_context: str | None = None

    prompt_source: str | None = None
    agent_files_loaded: dict[str, str] = field(default_factory=dict)

    async def resolve(self) -> None:
        """Load user/entity/agent/workspace objects from DB."""
        if not self.db:
            return
        from sqlalchemy import select

        if self.user_id and not self.user:
            from packages.core.models.user import User
            result = await self.db.execute(select(User).where(User.id == self.user_id))
            self.user = result.scalar_one_or_none()

        if self.entity_id and not self.entity:
            from packages.core.models.user import Entity
            result = await self.db.execute(select(Entity).where(Entity.id == self.entity_id))
            self.entity = result.scalar_one_or_none()

        if self.agent_id and not self.agent:
            from packages.core.models.workspace import Agent
            result = await self.db.execute(select(Agent).where(Agent.id == self.agent_id))
            self.agent = result.scalar_one_or_none()

        if self.workspace_id and not self.workspace:
            from packages.core.models.workspace import Workspace
            result = await self.db.execute(
                select(Workspace).where(Workspace.id == self.workspace_id)
            )
            self.workspace = result.scalar_one_or_none()


SectionFn = Callable[[ChatContext], str | None | Awaitable[str | None]]


class PromptBuilder:
    """Composable runtime prompt assembler with mode-based section gating."""

    def __init__(self) -> None:
        self._sections: list[tuple[str, SectionFn, set[PromptMode]]] = []

    def add(
        self,
        fn: SectionFn,
        *,
        name: str | None = None,
        modes: list[PromptMode] | None = None,
    ) -> "PromptBuilder":
        allowed = set(modes) if modes else {"full", "minimal"}
        self._sections.append((name or fn.__name__, fn, allowed))
        return self

    async def build(self, ctx: ChatContext) -> str:
        import inspect

        parts: list[str] = []
        for name, fn, allowed_modes in self._sections:
            if ctx.mode not in allowed_modes:
                continue
            try:
                result = fn(ctx)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    parts.append(result)
            except Exception:
                logger.warning("Prompt section '%s' failed", name, exc_info=True)
        return "\n\n".join(parts)


def build_default_prompt_builder() -> PromptBuilder:
    builder = PromptBuilder()
    renderers = runtime_prompt_section_renderers()
    for spec in runtime_prompt_section_specs():
        fn = renderers.get(spec.name)
        if fn is None:
            raise RuntimeError(f"Runtime prompt section '{spec.name}' has no renderer")
        builder.add(fn, name=spec.name, modes=list(spec.modes))
    return builder
