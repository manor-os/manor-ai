"""Declarative subject registries for RBAC and HITL authority mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from packages.core.humans.authority import ParticipantAuthority
from packages.core.permissions import Permission
from packages.core.services.runtime_authorization.domain import (
    AuthorizationSubject,
    WorkspaceAccessRequirement,
)
from packages.core.services.runtime_authorization.interfaces import (
    EntityPermissionMapper,
    HitlAuthorityMapper,
    WorkspaceRequirementMapper,
)


T = TypeVar("T")


@dataclass(frozen=True)
class SubjectRule(Generic[T]):
    result: T
    action_keys: frozenset[str] = frozenset()
    action_prefixes: tuple[str, ...] = ()
    capability_ids: frozenset[str] = frozenset()

    def matches(self, subject: AuthorizationSubject) -> bool:
        return (
            subject.action_key in self.action_keys
            or subject.action_key.startswith(self.action_prefixes)
            or subject.capability_id in self.capability_ids
        )


class RegistryEntityPermissionMapper(EntityPermissionMapper):
    _RULES: tuple[SubjectRule[Permission], ...] = (
        SubjectRule(
            Permission.TASKS_READ,
            capability_ids=frozenset({Permission.TASKS_READ.value}),
        ),
        SubjectRule(
            Permission.DOCS_READ,
            capability_ids=frozenset({"file.read"}),
        ),
        SubjectRule(
            Permission.AGENTS_READ,
            capability_ids=frozenset({Permission.AGENTS_READ.value}),
        ),
        SubjectRule(
            Permission.ENTITY_READ,
            capability_ids=frozenset({Permission.ENTITY_READ.value}),
        ),
        SubjectRule(
            Permission.INTEGRATIONS_READ,
            capability_ids=frozenset({Permission.INTEGRATIONS_READ.value}),
        ),
        SubjectRule(
            Permission.USERS_READ,
            capability_ids=frozenset({Permission.USERS_READ.value}),
        ),
        SubjectRule(
            Permission.ADMIN_BILLING,
            capability_ids=frozenset({Permission.ADMIN_BILLING.value}),
        ),
        SubjectRule(
            Permission.ADMIN_SETTINGS,
            capability_ids=frozenset({Permission.ADMIN_SETTINGS.value}),
        ),
        SubjectRule(
            Permission.CHAT_VIEW_ALL,
            capability_ids=frozenset({Permission.CHAT_VIEW_ALL.value}),
        ),
        SubjectRule(
            Permission.CHAT_USE,
            capability_ids=frozenset({Permission.CHAT_USE.value}),
        ),
        SubjectRule(
            Permission.WORKSPACES_READ,
            capability_ids=frozenset({"workspace.search"}),
        ),
        SubjectRule(
            Permission.DOCS_DELETE,
            action_prefixes=("workspace.file.delete",),
        ),
        SubjectRule(
            Permission.DOCS_UPLOAD,
            action_prefixes=("workspace.file.",),
            capability_ids=frozenset({"file.write"}),
        ),
        SubjectRule(
            Permission.TASKS_CREATE,
            action_prefixes=("workspace.task.create",),
        ),
        SubjectRule(
            Permission.TASKS_DELETE,
            action_prefixes=("workspace.task.delete",),
        ),
        SubjectRule(
            Permission.TASKS_UPDATE,
            action_prefixes=("workspace.task.",),
            capability_ids=frozenset({"workspace.task"}),
        ),
        SubjectRule(
            Permission.MCP_USE_PERSONAL,
            action_keys=frozenset({"channel.reply"}),
            action_prefixes=(
                "browser.",
                "chrome.",
                "social_post.",
                "social.",
                "email.",
                "external_message.",
            ),
            capability_ids=frozenset(
                {
                    "external.social",
                    "external.email",
                    "external.message",
                    "mcp.use_personal",
                }
            ),
        ),
        SubjectRule(
            Permission.WORKSPACES_UPDATE,
            action_prefixes=("workspace.",),
            capability_ids=frozenset(
                {
                    "workspace.governance",
                    "workspace.operate",
                    "automation.manage",
                    "workflow.manage",
                    "manor.composite",
                }
            ),
        ),
    )

    def permission_for(self, subject: AuthorizationSubject) -> Permission:
        for rule in self._RULES:
            if rule.matches(subject):
                return rule.result
        return Permission.CHAT_USE


class RegistryHitlAuthorityMapper(HitlAuthorityMapper):
    _RULES: tuple[SubjectRule[ParticipantAuthority], ...] = (
        SubjectRule(
            ParticipantAuthority.APPROVE_EXTERNAL_PUBLISH,
            action_keys=frozenset({"channel.reply"}),
            action_prefixes=(
                "social_post.",
                "social.",
                "email.",
                "external_message.",
            ),
            capability_ids=frozenset(
                {
                    "external.social",
                    "external.email",
                    "external.message",
                }
            ),
        ),
        SubjectRule(
            ParticipantAuthority.APPROVE_AUTOMATION_CHANGES,
            action_prefixes=(
                "workspace.automation.",
                "workspace.workflow.",
                "workspace.rule.",
                "workspace.operation.",
                "workspace.strategist.",
                "workspace.service.",
            ),
            capability_ids=frozenset(
                {
                    "automation.manage",
                    "workflow.manage",
                    "workspace.governance",
                    "workspace.operate",
                }
            ),
        ),
        SubjectRule(
            ParticipantAuthority.APPROVE_GOAL_CHANGES,
            action_prefixes=("workspace.goal.",),
            capability_ids=frozenset({"workspace.goal"}),
        ),
    )

    def authority_for(
        self,
        subject: AuthorizationSubject,
        *,
        standing: bool,
    ) -> ParticipantAuthority:
        if standing:
            return ParticipantAuthority.MANAGE_STANDING_GRANTS
        for rule in self._RULES:
            if rule.matches(subject):
                return rule.result
        return ParticipantAuthority.APPROVE_TASKS


class RegistryWorkspaceRequirementMapper(WorkspaceRequirementMapper):
    _RULES: tuple[SubjectRule[WorkspaceAccessRequirement], ...] = (
        SubjectRule(
            WorkspaceAccessRequirement.MANAGE,
            action_prefixes=(
                "workspace.rule.",
                "workspace.operation.",
                "workspace.strategist.",
                "workspace.service.",
            ),
            capability_ids=frozenset({"workspace.governance"}),
        ),
    )

    def requirement_for(
        self,
        subject: AuthorizationSubject,
    ) -> WorkspaceAccessRequirement:
        for rule in self._RULES:
            if rule.matches(subject):
                return rule.result
        return WorkspaceAccessRequirement.WRITE
