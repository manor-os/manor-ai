"""Current-storage verification for delegated runtime tool bindings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.services.runtime_authorization.domain import (
    RuntimeToolBindingDecision,
    RuntimeToolBindingSource,
)
from packages.core.services.runtime_authorization.interfaces import (
    RuntimeToolBindingVerifier,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationRequest,
    )


class CurrentRuntimeToolBindingVerifier(RuntimeToolBindingVerifier):
    """Verify durable bindings with a narrow fast path.

    Direct catalog bindings need one indexed join. MCP grants require their
    server/action scope. Workspace, task, operation, and skill grants are less
    common and retain the full contextual resolver as a final fallback.
    """

    async def verify(
        self,
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> RuntimeToolBindingDecision:
        from packages.core.ai.runtime.tool_visibility import runtime_tool_auto_pass_names

        # Discovery/control schemas are supplied by the Runtime itself, not
        # mutable AgentToolBinding rows. The caller has already revalidated
        # the Agent identity and this exact loop's tool allowlist.
        if request.tool_name in runtime_tool_auto_pass_names(is_master=False):
            return RuntimeToolBindingDecision(RuntimeToolBindingSource.CONTEXTUAL)

        if request.tool_name == "sandbox" and await self._has_active_sandbox_skill_binding(
            db,
            request,
        ):
            return RuntimeToolBindingDecision(RuntimeToolBindingSource.CONTEXTUAL)

        if request.tool_name and request.tool_name.startswith("mcp__"):
            if await self._has_mcp_binding(db, request):
                return RuntimeToolBindingDecision(RuntimeToolBindingSource.MCP)
        elif await self._has_direct_binding(db, request):
            return RuntimeToolBindingDecision(RuntimeToolBindingSource.DIRECT)

        if request.tool_name == "invoke_skill" and await self._has_visible_skill_binding(
            db,
            request,
        ):
            return RuntimeToolBindingDecision(RuntimeToolBindingSource.CONTEXTUAL)

        if self._may_have_contextual_binding(request):
            if await self._has_contextual_binding(db, request):
                return RuntimeToolBindingDecision(RuntimeToolBindingSource.CONTEXTUAL)

        return RuntimeToolBindingDecision(RuntimeToolBindingSource.REVOKED)

    @staticmethod
    async def _has_active_sandbox_skill_binding(
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> bool:
        """Recheck the Skill-backed Sandbox handoff for this exact actor.

        A successful ``invoke_skill`` call grants ``sandbox`` only on the
        active Runtime envelope.  This storage check prevents that ephemeral
        grant from surviving a Skill revocation, agent switch, or conversation
        switch.  The Sandbox handler separately binds the supplied sandbox_id
        to the same owner before every operation.
        """

        if (
            not request.conversation_id
            or not request.user_id
            or not request.principal_agent_id
        ):
            return False

        from packages.core.ai.runtime.sandbox import (
            runtime_load_sandbox_context,
            runtime_sandbox_context_owner_matches,
        )

        context = await runtime_load_sandbox_context(request.conversation_id)
        if not runtime_sandbox_context_owner_matches(
            context,
            entity_id=request.entity_id,
            user_id=request.user_id,
        ):
            return False
        if str((context or {}).get("agent_id") or "") != request.principal_agent_id:
            return False
        skill_ref = str((context or {}).get("skill_id") or "").strip()
        if not skill_ref:
            return False

        from packages.core.services.skill_service import list_skills_for_agent

        skills = await list_skills_for_agent(
            db,
            request.entity_id,
            request.principal_agent_id,
            workspace_id=request.workspace_id,
        )
        return any(
            skill_ref == str(getattr(skill, "id", "") or "")
            for skill in skills
        )

    @staticmethod
    async def _has_direct_binding(
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> bool:
        from sqlalchemy import select

        from packages.core.models.workspace import AgentToolBinding, ToolDefinition

        current = (
            await db.execute(
                select(AgentToolBinding.tool_id)
                .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
                .where(
                    AgentToolBinding.agent_id == request.principal_agent_id,
                    ToolDefinition.name == request.tool_name,
                    ToolDefinition.status == "active",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return current is not None

    @classmethod
    async def _has_mcp_binding(
        cls,
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> bool:
        if not request.principal_agent_id:
            return False
        from sqlalchemy import select

        from packages.core.models.mcp import AgentMCPBinding, MCPServer
        from packages.core.services.agent_permission_service import (
            parse_mcp_tool_name,
        )

        parsed = parse_mcp_tool_name(request.tool_name)
        if parsed is None:
            return False
        server_key, action = parsed
        row = (
            await db.execute(
                select(
                    AgentMCPBinding.id,
                    AgentMCPBinding.allowed_tools,
                    MCPServer.default_allowed_tools,
                )
                .select_from(MCPServer)
                .outerjoin(
                    AgentMCPBinding,
                    (AgentMCPBinding.mcp_server_id == MCPServer.id)
                    & (AgentMCPBinding.agent_id == request.principal_agent_id)
                    & (AgentMCPBinding.status == "active"),
                )
                .where(
                    MCPServer.server_key == server_key,
                    MCPServer.status == "active",
                )
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return False
        if row[0] is not None:
            allowed_tools = row[1] if row[1] is not None else row[2]
            if allowed_tools is None:
                return True
            allowed_names = {
                str(name).strip()
                for name in allowed_tools
                if str(name or "").strip()
            }
            if action in allowed_names or request.tool_name in allowed_names:
                return True

        # Agent settings may persist an individual MCP action in the generic
        # tool catalog instead of creating a server-wide MCP binding.
        return await cls._has_direct_binding(db, request)

    @staticmethod
    def _may_have_contextual_binding(request: "RuntimeAuthorizationRequest") -> bool:
        return bool(
            request.workspace_id
            or request.task_id
            or request.conversation_id
            or (
                request.tool_name == "invoke_skill"
                and request.runtime_surface == "public_customer_chat"
            )
        )

    @staticmethod
    async def _has_visible_skill_binding(
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> bool:
        # Public customer chat adds a surface/profile-specific skill contract;
        # retain the complete contextual resolver for that specialized case.
        if request.runtime_surface == "public_customer_chat":
            return False

        from packages.core.services.workspace_runtime import agent_has_visible_skills

        return await agent_has_visible_skills(
            db,
            entity_id=request.entity_id,
            agent_id=request.principal_agent_id,
        )

    @staticmethod
    async def _has_contextual_binding(
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> bool:
        from packages.core.ai.runtime.surfaces import ChatSurface
        from packages.core.ai.runtime.tool_visibility import runtime_tool_auto_pass_names
        from packages.core.constants.agents import is_master_agent
        from packages.core.services.workspace_runtime import resolve_workspace_runtime

        current = await resolve_workspace_runtime(
            db,
            entity_id=request.entity_id,
            user_id=request.user_id,
            agent_id=request.principal_agent_id,
            conversation_id=request.conversation_id,
            workspace_id=request.workspace_id,
            task_id=request.task_id,
            is_master=(
                is_master_agent(request.principal_agent_id)
                if request.principal_agent_id
                else None
            ),
            runtime_surface=request.runtime_surface,
            include_prompt_context=False,
        )
        if request.tool_name and request.tool_name.startswith("mcp__"):
            if request.tool_name in set(current.mcp_allowed_names or ()):
                return True
            if bool(getattr(current, "mcp_scope_unrestricted", False)):
                return True
            from packages.core.services.agent_permission_service import (
                parse_mcp_tool_name,
            )

            parsed = parse_mcp_tool_name(request.tool_name)
            if parsed is None:
                return False
            provider, action = parsed
            return any(
                item.provider == provider and item.allows(action)
                for item in (
                    getattr(current, "mcp_provider_scopes", ()) or ()
                )
            )
        if request.tool_name in set(current.bound_tool_names or ()):
            return True

        # Only server-owned defaults are implicit grants. Discoverable
        # business tools still require a current durable/contextual binding;
        # using the entire visible surface here would undo revocations.
        try:
            surface = ChatSurface(request.runtime_surface) if request.runtime_surface else None
        except ValueError:
            return False
        if surface in {ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT}:
            return False
        return request.tool_name in runtime_tool_auto_pass_names(
            is_master=False,
            tool_profile=current.tool_profile,
        )
