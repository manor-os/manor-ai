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
    tool_profile: str | None = None
    runtime_surface: str | None = None
    runtime_profile_name: str | None = None
    runtime_envelope: Any = None
    runtime_context_blocks: list[Any] = field(default_factory=list)
    runtime_skill_descriptors: list[Any] = field(default_factory=list)
    runtime_workspace_operation_skills: list[dict] = field(default_factory=list)
    model: str | None = None
    llm_metadata: dict[str, Any] | None = None
    turn_execution_plan: Any = None
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
    # Credential-free, actor-scoped account snapshot shared by prompt sections
    # during this turn. Execution still revalidates durable account and Agent
    # authority immediately before provider I/O.
    integration_account_registry: Any = None
    integration_account_registry_loaded: bool = False
    integration_account_registry_status: str | None = None

    # Tool Discovery v2 (A3 intent-path memory): the tool names surfaced in
    # this turn's cache-first hint (resolve_runtime_chat_context), so the
    # chat-layer tool-exec callback can pass it to
    # agentic_loop._maybe_record_tool_path for hinted-failure recording.
    # Empty when there is no match or lookup failed.
    hinted_tool_names: set[str] = field(default_factory=set)

    extra_context: str | None = None

    prompt_source: str | None = None
    agent_files_loaded: dict[str, str] = field(default_factory=dict)
    runtime_context_sections: list[dict] = field(default_factory=list)
    # A section renderer that produces several context sections (identity
    # files, memory layers) leaves them here under its own name.
    runtime_section_outputs: dict[str, list[dict]] = field(default_factory=dict)
    # Set when the turn's history comes from the conversation transcript; the
    # chat service appends this run's messages to it after the reply is saved.
    conversation_transcript: Any = None
    transcript_initial_messages: list[dict] = field(default_factory=list)
    # Split builds: the system prompt keeps what is the same every turn; what
    # follows the current message, turn state or the clock is rendered here and
    # sent after the history (see agentic_loop turn_context).
    prompt_split_turn_context: bool = False
    runtime_turn_context: str = ""
    runtime_turn_context_sections: list[dict] = field(default_factory=list)
    runtime_workspace_operation_selected_guides: str | None = None
    # Discovered tools restored from earlier turns, reachable through call_tool.
    gateway_tool_schemas: list[dict] = field(default_factory=list)

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
        self._sections: list[tuple[str, SectionFn, set[PromptMode], bool]] = []

    def add(
        self,
        fn: SectionFn,
        *,
        name: str | None = None,
        modes: list[PromptMode] | None = None,
        turn_scoped: bool = False,
    ) -> "PromptBuilder":
        allowed = set(modes) if modes else {"full", "minimal"}
        self._sections.append((name or fn.__name__, fn, allowed, turn_scoped))
        return self

    async def build(self, ctx: ChatContext, *, split_turn_context: bool = False) -> str:
        """Render the system prompt.

        With ``split_turn_context`` the turn-scoped sections (and memory chosen
        for this message) go to ``ctx.runtime_turn_context`` instead, so the
        returned prompt is identical from turn to turn.
        """
        import inspect

        from packages.core.ai.runtime.context_window import context_section, manage_prompt_sections

        sections: list[dict] = []
        turn_section_ids: set[int] = set()
        ctx.prompt_split_turn_context = split_turn_context
        ctx.runtime_turn_context = ""
        ctx.runtime_turn_context_sections = []
        ctx.runtime_workspace_operation_skills = []
        ctx.runtime_section_outputs = {}
        for name, fn, allowed_modes, turn_scoped in self._sections:
            if ctx.mode not in allowed_modes:
                continue
            try:
                result = fn(ctx)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    produced = ctx.runtime_section_outputs.pop(name, None) or [context_section(name, result)]
                    sections.extend(produced)
                    if turn_scoped:
                        turn_section_ids.update(id(section) for section in produced)
            except Exception:
                logger.warning("Prompt section '%s' failed", name, exc_info=True)
        from packages.core.memory.manager import (
            prompt_memory_selection_candidates,
            select_prompt_memories,
        )
        if not split_turn_context:
            sections = await select_prompt_memories(ctx, sections)
            ctx.runtime_context_sections = manage_prompt_sections(sections)
            return "\n\n".join(section["content"] for section in ctx.runtime_context_sections if section["content"])

        # Memory chosen for this message is turn context too.
        turn_section_ids.update(id(section) for section in prompt_memory_selection_candidates(ctx, sections))
        turn_keys = [id(section) in turn_section_ids for section in sections]
        sections = await select_prompt_memories(ctx, sections)
        stable = [section for section, turn in zip(sections, turn_keys) if not turn]
        turn = [section for section, is_turn in zip(sections, turn_keys) if is_turn]
        # Separate budgets: a large turn section must never shorten the stable
        # prompt, or the stable prompt would change with the message after all.
        ctx.runtime_context_sections = manage_prompt_sections(stable)
        ctx.runtime_turn_context_sections = manage_prompt_sections(turn)
        ctx.runtime_turn_context = "\n\n".join(
            section["content"] for section in ctx.runtime_turn_context_sections if section["content"]
        )
        return "\n\n".join(section["content"] for section in ctx.runtime_context_sections if section["content"])


def build_default_prompt_builder() -> PromptBuilder:
    builder = PromptBuilder()
    renderers = runtime_prompt_section_renderers()
    for spec in runtime_prompt_section_specs():
        fn = renderers.get(spec.name)
        if fn is None:
            raise RuntimeError(f"Runtime prompt section '{spec.name}' has no renderer")
        builder.add(fn, name=spec.name, modes=list(spec.modes), turn_scoped=spec.turn_scoped)
    return builder
