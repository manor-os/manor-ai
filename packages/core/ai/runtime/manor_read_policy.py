"""Typed authorization policy for read-only Manor composite actions."""

from __future__ import annotations

from enum import StrEnum

from packages.core.permissions import Permission


class ManorReadCapability(StrEnum):
    """Closed capability vocabulary for Manor reads."""

    DISCOVERY = "runtime.discovery"
    DOCUMENTS = "file.read"
    WORKSPACES = "workspace.search"
    TASKS = Permission.TASKS_READ.value
    AGENTS = Permission.AGENTS_READ.value
    ENTITY = Permission.ENTITY_READ.value
    INTEGRATIONS = Permission.INTEGRATIONS_READ.value
    PEOPLE = Permission.USERS_READ.value
    BILLING = Permission.ADMIN_BILLING.value
    SETTINGS = Permission.ADMIN_SETTINGS.value
    CHAT = Permission.CHAT_USE.value
    CONVERSATIONS = Permission.CHAT_VIEW_ALL.value


class ManorReadCapabilityFactory:
    """Resolve each reviewed read-only action to one explicit capability."""

    _BY_ACTION: dict[str, ManorReadCapability] = {
        "search": ManorReadCapability.DISCOVERY,
        "list_tasks": ManorReadCapability.TASKS,
        "get_task_details": ManorReadCapability.TASKS,
        "list_task_categories": ManorReadCapability.TASKS,
        "get_task_health": ManorReadCapability.TASKS,
        "list_documents": ManorReadCapability.DOCUMENTS,
        "search_documents": ManorReadCapability.DOCUMENTS,
        "list_workspace_artifacts": ManorReadCapability.DOCUMENTS,
        "get_document": ManorReadCapability.DOCUMENTS,
        "list_document_folders": ManorReadCapability.DOCUMENTS,
        "list_document_groups": ManorReadCapability.DOCUMENTS,
        "list_agents": ManorReadCapability.AGENTS,
        "get_agent": ManorReadCapability.AGENTS,
        "list_agent_tools": ManorReadCapability.AGENTS,
        "get_dashboard_summary": ManorReadCapability.ENTITY,
        "get_system_health": ManorReadCapability.SETTINGS,
        "list_token_usage": ManorReadCapability.BILLING,
        "list_workspaces": ManorReadCapability.WORKSPACES,
        "get_workspace": ManorReadCapability.WORKSPACES,
        "get_workspace_daily_summary": ManorReadCapability.WORKSPACES,
        "get_operating_model": ManorReadCapability.WORKSPACES,
        "get_workspace_agents": ManorReadCapability.WORKSPACES,
        "get_goal_status": ManorReadCapability.WORKSPACES,
        "get_workspace_activity": ManorReadCapability.WORKSPACES,
        "get_workspace_dashboard": ManorReadCapability.WORKSPACES,
        "get_entity_info": ManorReadCapability.ENTITY,
        "list_integrations": ManorReadCapability.INTEGRATIONS,
        "list_ready_integrations": ManorReadCapability.INTEGRATIONS,
        "list_users": ManorReadCapability.PEOPLE,
        "list_skills": ManorReadCapability.DISCOVERY,
        "get_skill": ManorReadCapability.DISCOVERY,
        "list_scheduled_jobs": ManorReadCapability.SETTINGS,
        "list_notifications": ManorReadCapability.CHAT,
        "list_conversations": ManorReadCapability.CONVERSATIONS,
        "list_channel_bindings": ManorReadCapability.INTEGRATIONS,
        "list_staff": ManorReadCapability.PEOPLE,
        "get_staff": ManorReadCapability.PEOPLE,
        "list_roles": ManorReadCapability.PEOPLE,
        "list_clients": ManorReadCapability.PEOPLE,
        "get_client": ManorReadCapability.PEOPLE,
        "list_orders": ManorReadCapability.PEOPLE,
        "get_order": ManorReadCapability.PEOPLE,
    }

    @classmethod
    def supports(cls, action: str) -> bool:
        return str(action or "").strip() in cls._BY_ACTION

    @classmethod
    def create(cls, action: str) -> ManorReadCapability:
        normalized = str(action or "").strip()
        try:
            return cls._BY_ACTION[normalized]
        except KeyError as exc:
            raise ValueError(
                f"Manor action {normalized!r} has no reviewed read capability"
            ) from exc

    @classmethod
    def actions(cls) -> frozenset[str]:
        return frozenset(cls._BY_ACTION)


MANOR_READ_ONLY_ACTIONS = ManorReadCapabilityFactory.actions()


__all__ = [
    "MANOR_READ_ONLY_ACTIONS",
    "ManorReadCapability",
    "ManorReadCapabilityFactory",
]
