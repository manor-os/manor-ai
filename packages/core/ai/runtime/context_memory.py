"""Runtime bridge between independent Memory and Context Window services."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging

from packages.core.ai.runtime.context_window import context_section, manage_prompt_sections
from packages.core.ai.runtime.memory import (
    runtime_allows_agent_memory_reader, runtime_allows_workspace_memory_reader,
)

logger = logging.getLogger(__name__)


def _join(sections):
    return "\n\n".join(s["content"] for s in sections if s["content"])


class RuntimeContextMemory:
    """Stage prompt replacements; publish their manifest only after validation."""

    def __init__(self, ctx, *, normalize_prompt=None):
        self.target = ctx
        self.ctx = replace(ctx, db=None)
        self.sections = deepcopy(ctx.runtime_context_sections)
        self.normalize_prompt = normalize_prompt or (lambda prompt: prompt)
        self.base = self.normalize_prompt(_join(self.sections))
        self.pending = None

    async def prepare(self, history, archive_id):
        from packages.core.memory.compaction import prepare_compaction_memory

        # Mounts authorize reading evidence. Candidate admission performs its
        # own current membership/learning checks; it never applies a rule.
        allowed = self._workspace_allowed() if self.ctx.workspace_id else self._agent_allowed()
        if allowed:
            await prepare_compaction_memory(self.ctx, history, archive_id)

    def _agent_allowed(self):
        return runtime_allows_agent_memory_reader(self.ctx.runtime_envelope)

    def _workspace_allowed(self):
        return bool(self.ctx.workspace_id) and runtime_allows_workspace_memory_reader(
            self.ctx.runtime_envelope, self.ctx.workspace_id,
        )

    async def refresh(self, messages):
        from packages.core.database import async_session
        from packages.core.memory.compaction import authorize_memory_scope
        from packages.core.services.memory_service import get_context_memories
        from packages.core.memory.manager import select_prompt_memories
        from packages.core.ai.runtime.prompt_context import (
            runtime_agent_identity_prompt, runtime_workspace_operating_memory_prompt,
        )

        self.pending = None
        indexes = [i for i, m in enumerate(messages) if m.get("role") == "system"
                   and isinstance(m.get("content"), str) and self.base
                   and m["content"].count(self.base) == 1]
        if len(indexes) != 1:
            raise ValueError("Cannot locate the assembled context for memory replacement")
        fresh = []
        async with async_session() as db:
            workspace = await authorize_memory_scope(db, self.ctx)
            if self._agent_allowed():
                identity = runtime_agent_identity_prompt(
                    entity_id=self.ctx.entity_id, agent_id=self.ctx.agent_id,
                    user_id=self.ctx.user_id, strict=True,
                )
                fresh.extend(s for s in identity.sections if s["category"] == "memory_files")
                # Exact personal scope: never make None mean "all users" or
                # mix workspace records into a personal-memory section.
                content = await get_context_memories(
                    db, self.ctx.entity_id, agent_id=self.ctx.agent_id, user_id=self.ctx.user_id,
                    query=self.ctx.active_user_message, sync_files=False,
                )
                if content:
                    fresh.append(context_section("agent_memories", "## Agent Memories\n" + content))
            if self._workspace_allowed():
                runtime_workspace_operating_memory_prompt(
                    envelope=self.ctx.runtime_envelope, entity_id=self.ctx.entity_id,
                    workspace_id=self.ctx.workspace_id, workspace=workspace,
                    agent_id=self.ctx.agent_id, sections_out=fresh, strict=True,
                )

        def reloadable(s):
            source = s.get("source", "")
            return (source.startswith(("workspace:", "workspace_agent:"))
                    or source == "prompt_sections:agent_memories"
                    or (source.startswith("agent_files:") and s["category"] == "memory_files"))

        # Keep non-memory sections byte-for-byte and replace each source once.
        # Missing/deleted sources disappear; genuinely failed reads raise above.
        by_key = {s["key"]: s for s in fresh}
        combined = []
        for section in self.sections:
            if reloadable(section):
                updated = by_key.pop(section["key"], None)
                if updated:
                    combined.append(updated)
            else:
                combined.append(section)
        # Sections a split build sends as turn context stay there: refreshing
        # must not move per-message memory into the cacheable system prompt.
        turn_keys = {section["key"] for section in getattr(self.target, "runtime_turn_context_sections", None) or []}
        combined.extend(section for key, section in by_key.items() if key not in turn_keys)
        combined = manage_prompt_sections(await select_prompt_memories(self.ctx, combined))
        base = self.normalize_prompt(_join(combined))
        index = indexes[0]
        messages[index]["content"] = messages[index]["content"].replace(self.base, base, 1)
        # The caller still has to validate capacity and persist the checkpoint.
        self.pending = (combined, base)
        return messages

    def accept(self):
        if self.pending is not None:
            self.sections, self.base = self.pending
            self.target.runtime_context_sections = deepcopy(self.sections)
            self.pending = None
            logger.info("[context-memory] conversation=%s refreshed sources=%s", self.ctx.conversation_id, len(self.sections))
