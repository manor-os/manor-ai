"""Bring an installed workspace up to its blueprint's current content.

Installing copies a blueprint's skills and agents into the workspace once.
When the blueprint is later corrected the workspace keeps what it got — the
faceless-stickman workspace ran a 636-character stand-in for its video skill
for five days after the real 4664-character procedure had shipped.

Re-installing does not fix it. The installer, meeting a skill that already
exists, deliberately reconciles only ``tools`` and ``status`` and leaves
``system_prompt`` alone: at install time it cannot tell a workspace's own
wording from a stale copy, so it touches neither.

This module can tell, because ``revision`` already answers it. It moves only
when a behaviour-affecting field actually changes, and the operator's own
edits go through skill_service/agent_service, which bump it. So:

    revision == 1  →  installed and never behaviourally edited
    revision > 1   →  the workspace made this its own

Only the first is overwritten automatically. The second is reported and left
exactly as it is unless the operator explicitly chooses the Blueprint version
for that conflict after reviewing the new content.

Three separate acts, because they carry different risk:

    plan()    reads, writes nothing, and is what the operator confirms
    apply()   overwrites the safe items, recording the old values first
    revert()  puts those old values back

The restore point holds only the fields apply() actually overwrote — a few
KB, not a snapshot of the workspace — and only for the most recent upgrade.
Deeper history is a different feature; one step back covers "applied it, saw
it was wrong, undo".
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    blueprint_upgrade_unsupported_fingerprint,
    installed_blueprint_record,
    normalize_blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.blueprints.workflow_dependencies import (
    WorkflowDependencyError,
    WorkflowDependencyFactory,
)
from packages.core.revisions import (
    AGENT_CONTENT_REVISION_FIELDS,
    SKILL_CONTENT_REVISION_FIELDS,
    bump_revision,
    content_patch_for,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_payload_reference_delta,
)

logger = logging.getLogger(__name__)

#: Where apply() leaves what revert() needs.
RESTORE_POINT_KEY = "restore_point"

#: Where apply() records the revision each item ended on, so the next
#: upgrade can tell its own bump apart from an operator's edit.
APPLIED_REVISIONS_KEY = "applied_revisions"

# Creator-owned variable schema is portable Blueprint provenance; installer
# values stay in the Workspace settings block, outside provenance.
VARIABLE_DECLARATIONS_KEY = "variable_declarations"
PERSONALIZATION_SETTINGS_KEY = "blueprint_personalization"
LIVE_SETUP_STATE_KEYS = ("live_setup_requirements", "install_todos", "blocking_todo_count")

#: A fresh install that never behaviourally changed.
PRISTINE_REVISION = 1

# WorkflowDefinition revisions move when the executable graph or variables
# change. Keep this set aligned with workflow_service's revision fields so an
# operator edit is never overwritten merely because another field is not
# revision-tracked.
WORKFLOW_CONTENT_REVISION_FIELDS: frozenset[str] = frozenset(
    {"steps", "variables"}
)

WORKFLOW_INSTALL_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "trigger_type",
    "trigger_config",
    "steps",
    "variables",
    "category",
    "tags",
    "is_active",
    "version",
    "status",
)
WORKFLOW_DELETE_GUARD_FIELDS: tuple[str, ...] = (
    "created_by",
    "workspace_id",
    "visibility",
    "icon",
    *WORKFLOW_INSTALL_FIELDS,
)
WORKFLOW_BINDING_FIELDS: tuple[str, ...] = (
    "workflow_id",
    "workspace_id",
    "business_line",
    "name",
    "trigger_type",
    "trigger_config",
    "variables",
    "config",
    "enabled",
    "status",
)
WORKFLOW_INSTALLATION_FIELDS: tuple[str, ...] = (
    "template_id",
    "component_key",
    "workflow_id",
    "installed_version",
    "installed_by",
    "source_type",
    "installation_metadata",
)
KNOWLEDGE_DELETE_GUARD_FIELDS: tuple[str, ...] = (
    "name",
    "fs_path",
    "file_url",
    "file_size",
    "file_type",
    "mime_type",
    "source",
    "metadata_",
    "folder_id",
    "is_trashed",
    "trashed_at",
    "trashed_by",
    "visibility",
    "classification",
    "owner_id",
    "client_visible",
    "pii_detected",
    "quarantine_status",
)


def _row_snapshot(row: Any, fields: Any) -> dict[str, Any]:
    """Copy JSON-safe mutable state used by apply/revert CAS guards."""
    return {
        field: copy.deepcopy(getattr(row, field, None))
        for field in fields
    }


def _row_matches_snapshot(row: Any, snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return True
    return all(
        getattr(row, field, None) == value
        for field, value in snapshot.items()
    )


def _is_workspace_edited(row: Any, applied: dict[str, Any]) -> bool:
    """Has the workspace changed this item, as opposed to an upgrade?

    ``revision`` answers "was this behaviourally changed", but apply() bumps
    it too — so a single upgrade would otherwise mark every item it touched
    as the workspace's own and lock it out of the next one. The revision each
    item ended an upgrade on is recorded; sitting on that value still means
    untouched.
    """
    revision = int(getattr(row, "revision", PRISTINE_REVISION) or PRISTINE_REVISION)
    if revision <= PRISTINE_REVISION:
        return False
    return revision != int(applied.get(str(row.id), 0) or 0)


class UpgradeAction(str, Enum):
    """What the plan intends to do with one installed item."""

    #: Blueprint content differs and the workspace never edited it.
    UPDATE = "update"

    #: The workspace edited it. Kept unless the operator resolves the conflict.
    KEEP_YOURS = "keep_yours"

    #: Already matches the blueprint.
    UNCHANGED = "unchanged"

    #: The blueprint names something this workspace does not have.
    MISSING = "missing"

    #: A legacy install lacks enough provenance to prove full synchronization.
    BASELINE_UNKNOWN = "baseline_unknown"

    #: Safe component updates may proceed, but other Blueprint configuration
    #: requires explicit reconfiguration before the Workspace can be current.
    RECONFIGURE = "reconfigure"


class BlueprintUpgradePlanChangedError(ValueError):
    """The reviewed Blueprint plan is no longer the one being applied."""


class BlueprintUpgradeIncompleteError(ValueError):
    """The Workspace is missing a Blueprint component apply cannot restore."""


class BlueprintUpgradeAccessDeniedError(PermissionError):
    """The actor cannot edit a component selected by the upgrade."""


async def _require_component_access(
    db: AsyncSession,
    *,
    row: Any,
    kind: str,
    entity_id: str,
    actor: Any | None,
    capability: str,
) -> None:
    """Route user-initiated component writes through resource_access."""
    if actor is None or kind not in {"agent", "skill", "workflow"}:
        return

    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import (
        ResourceDescriptor,
        user_can_access_resource,
    )

    resource_type = {
        "agent": ResourceType.AGENT,
        "skill": ResourceType.SKILL,
        "workflow": ResourceType.WORKFLOW,
    }[kind]
    allowed = await user_can_access_resource(
        db,
        descriptor=ResourceDescriptor.from_row(row, resource_type),
        entity_id=entity_id,
        user_id=getattr(actor, "id", None),
        role=getattr(actor, "role", None),
        capability=capability,
    )
    if not allowed:
        raise BlueprintUpgradeAccessDeniedError(
            f"Insufficient permissions to change Blueprint {kind} {getattr(row, 'name', '')!r}"
        )


async def _require_component_edit_access(
    db: AsyncSession,
    *,
    row: Any,
    kind: str,
    entity_id: str,
    actor: Any | None,
) -> None:
    from packages.core.models.permission import Capability

    await _require_component_access(
        db,
        row=row,
        kind=kind,
        entity_id=entity_id,
        actor=actor,
        capability=Capability.EDIT,
    )


async def _require_component_delete_access(
    db: AsyncSession,
    *,
    row: Any,
    kind: str,
    entity_id: str,
    actor: Any | None,
) -> None:
    from packages.core.models.permission import Capability

    await _require_component_access(
        db,
        row=row,
        kind=kind,
        entity_id=entity_id,
        actor=actor,
        capability=Capability.DELETE,
    )


async def _require_document_access(
    db: AsyncSession,
    *,
    row: Any,
    actor: Any | None,
    capability: str,
) -> None:
    """Apply the document-specific owner/admin/grant rules."""
    if actor is None:
        return

    from packages.core.models.permission import Capability
    from packages.core.permissions import (
        Permission,
        effective_user_has_permission,
        user_is_effective_entity_admin,
    )
    from packages.core.services.document_access import user_has_document_capability

    user_id = getattr(actor, "id", None)
    allowed = (
        await user_is_effective_entity_admin(db, actor)
        or getattr(row, "owner_id", None) == user_id
        or getattr(row, "created_by", None) == user_id
    )
    if (
        not allowed
        and capability == Capability.DELETE
        and await effective_user_has_permission(db, actor, Permission.DOCS_DELETE)
    ):
        allowed = True
    if not allowed:
        allowed = await user_has_document_capability(
            db,
            document=row,
            user_id=user_id,
            capabilities={capability},
        )
    if not allowed:
        raise BlueprintUpgradeAccessDeniedError(
            f"Insufficient permissions to change Blueprint Knowledge document {getattr(row, 'name', '')!r}"
        )


def _fields_for(kind: str) -> frozenset[str]:
    if kind == "skill":
        return SKILL_CONTENT_REVISION_FIELDS
    if kind == "workflow":
        return WORKFLOW_CONTENT_REVISION_FIELDS
    return AGENT_CONTENT_REVISION_FIELDS


def _desired_content(
    kind: str,
    spec: dict[str, Any],
    *,
    workflow_id_by_key: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The blueprint's values for the fields an upgrade may touch.

    Only behaviour-affecting fields. A blueprint retitling its skill is not
    something to overwrite a workspace for.
    """
    source = spec
    if kind == "workflow":
        if workflow_id_by_key is not None:
            source = copy.deepcopy(spec)
            source["steps"] = WorkflowDependencyFactory.to_runtime(
                list(spec.get("steps") or []),
                workflow_id_by_key=workflow_id_by_key,
            )
        from packages.core.blueprints.installer import (
            _blueprint_workflow_definition_values,
        )

        source = _blueprint_workflow_definition_values(source)
    return {
        field: source.get(field)
        for field in _fields_for(kind)
        if source.get(field) is not None
    }


def _planned_workflow_content(
    spec: dict[str, Any],
    *,
    workflow_id_by_key: dict[str, str],
) -> dict[str, Any]:
    """Project a Flow for preview without requiring missing peers yet.

    An upgrade plan is also the inventory of components that must be restored.
    A partially installed Blueprint can therefore contain a parent Flow before
    all of its referenced child Flows are present.  Runtime materialization is
    still strict during apply, after missing definitions have been installed;
    preview falls back to the portable definition so it can report every
    missing component instead of aborting at the first dependency.
    """
    steps = list(spec.get("steps") or [])
    if WorkflowDependencyFactory.missing_source_keys(
        steps,
        workflow_id_by_key=workflow_id_by_key,
    ):
        return _desired_content("workflow", spec)
    return _desired_content(
        "workflow",
        spec,
        workflow_id_by_key=workflow_id_by_key,
    )


#: How much of the new content to carry into the confirmation dialog. Long
#: enough to judge what the new version says, short enough that a plan for
#: several workspaces is not a payload problem.
PREVIEW_CHARS = 4000


def _preview(patch: dict[str, Any]) -> dict[str, str]:
    """The new version's actual content, for the operator to read.

    "instructions 636 → 4664 characters" says how much changes, not what it
    now says. Someone approving an overwrite of the instructions their agents
    run should be able to read them.
    """
    out: dict[str, str] = {}
    for field, value in patch.items():
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        elif isinstance(value, str):
            text = value.strip()
        else:
            continue
        if not text:
            continue
        out[field] = text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"
    return out


def _describe(kind: str, patch: dict[str, Any], row: Any) -> list[str]:
    """Say what changes in a way an operator can judge.

    "system_prompt differs" is not actionable; "636 → 4664 characters" is.
    """
    notes: list[str] = []
    for field, new_value in sorted(patch.items()):
        old_value = getattr(row, field, None)
        if field == "system_prompt":
            notes.append(
                f"instructions {len(str(old_value or ''))} → {len(str(new_value or ''))} characters"
            )
        elif field == "steps":
            notes.append(
                f"workflow graph {len(old_value or [])} → {len(new_value or [])} steps"
            )
        elif isinstance(new_value, (list, tuple)):
            added = sorted(set(map(str, new_value)) - set(map(str, old_value or [])))
            removed = sorted(set(map(str, old_value or [])) - set(map(str, new_value)))
            if added:
                notes.append(f"{field}: +{', '.join(added)}")
            if removed:
                notes.append(f"{field}: -{', '.join(removed)}")
        else:
            notes.append(f"{field} changes")
    return notes


async def _installed_blueprint_workflows(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    source_blueprint_ids: list[str],
    source_template_ids: list[str],
    workflow_component_keys: dict[str, str],
    internal_slugs: set[str] | None = None,
) -> tuple[dict[str, Any], set[str], set[str]]:
    """Return the blueprint Flow definitions installed for this Workspace.

    Exact Workspace-scoped Marketplace links win. Installation rows are a
    controlled fallback for pre-link installs; same-name definitions are never
    identity. User-facing Flows must additionally retain their Workspace
    binding, while internal orchestration Flows intentionally have no binding.
    """
    from packages.core.models.marketplace_resource_link import (
        MarketplaceResourceLink,
    )
    from packages.core.models.workflow import (
        WorkflowBinding,
        WorkflowDefinition,
        WorkflowTemplateInstallation,
    )

    workflow_component_keys = {
        slug: component_key
        for slug, component_key in workflow_component_keys.items()
        if slug and component_key
    }
    workflow_slugs = set(workflow_component_keys)
    if not workflow_slugs:
        return {}, set(), set()
    slug_by_component_key = {
        component_key: slug
        for slug, component_key in workflow_component_keys.items()
    }
    source_blueprint_ids = [
        source_id for source_id in source_blueprint_ids if source_id
    ]
    source_template_ids = [
        source_id for source_id in source_template_ids if source_id
    ]
    if not source_template_ids and not source_blueprint_ids:
        return {}, set(), set()
    exact_links: list[MarketplaceResourceLink] = []
    if source_blueprint_ids:
        from packages.core.services.marketplace_resource_links import (
            RELATIONSHIP_INSTALLED_COMPONENT,
            RESOURCE_WORKFLOW,
            RESOURCE_WORKSPACE_BLUEPRINT,
            SCOPE_WORKSPACE,
        )

        exact_links = list((await db.execute(
            select(MarketplaceResourceLink).where(
                MarketplaceResourceLink.entity_id == entity_id,
                MarketplaceResourceLink.marketplace_resource_type
                == RESOURCE_WORKSPACE_BLUEPRINT,
                MarketplaceResourceLink.marketplace_resource_id.in_(
                    source_blueprint_ids
                ),
                MarketplaceResourceLink.relationship
                == RELATIONSHIP_INSTALLED_COMPONENT,
                MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                MarketplaceResourceLink.scope_id == workspace_id,
                MarketplaceResourceLink.local_resource_type == RESOURCE_WORKFLOW,
                MarketplaceResourceLink.component_key.in_(
                    set(workflow_component_keys.values())
                ),
            )
        )).scalars().all())

    installations = []
    if source_template_ids:
        installations = list((await db.execute(
            select(WorkflowTemplateInstallation).where(
                WorkflowTemplateInstallation.entity_id == entity_id,
                WorkflowTemplateInstallation.template_id.in_(source_template_ids),
                WorkflowTemplateInstallation.component_key.in_(workflow_slugs),
            )
        )).scalars().all())
    source_priority = {
        source_id: index for index, source_id in enumerate(source_template_ids)
    }
    installations.sort(
        key=lambda row: source_priority.get(row.template_id, len(source_priority))
    )
    installation_by_slug: dict[str, WorkflowTemplateInstallation] = {}
    for installation in installations:
        installation_by_slug.setdefault(installation.component_key, installation)
    blueprint_priority = {
        source_id: index for index, source_id in enumerate(source_blueprint_ids)
    }
    exact_links.sort(key=lambda row: blueprint_priority.get(
        row.marketplace_resource_id, len(blueprint_priority)
    ))
    workflow_id_by_slug: dict[str, str] = {}
    for link in exact_links:
        slug = slug_by_component_key.get(link.component_key)
        if slug:
            workflow_id_by_slug.setdefault(slug, link.local_resource_id)
    workflow_id_by_slug.update({
        slug: row.workflow_id for slug, row in installation_by_slug.items()
        if slug not in workflow_id_by_slug
    })
    if not workflow_id_by_slug:
        return {}, set(), set()

    definitions = list((await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.id.in_(set(workflow_id_by_slug.values())),
        )
    )).scalars().all())
    by_id = {row.id: row for row in definitions}
    internal_slugs = {slug for slug in (internal_slugs or set()) if slug}

    bindings = list((await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.workflow_id.in_(set(workflow_id_by_slug.values())),
        )
    )).scalars().all())
    bound_slugs: set[str] = set()
    valid_binding_source_ids = set(source_blueprint_ids) | set(source_template_ids)
    for binding in bindings:
        config = binding.config if isinstance(binding.config, dict) else {}
        slug = str(config.get("workspace_blueprint_workflow_slug") or "").strip()
        if (
            config.get("source") == "blueprint"
            and config.get("source_template_id") in valid_binding_source_ids
            and workflow_id_by_slug.get(slug) == binding.workflow_id
        ):
            bound_slugs.add(slug)

    installed = {
        slug: by_id[workflow_id]
        for slug, workflow_id in workflow_id_by_slug.items()
        if workflow_id in by_id
    }
    missing_bindings = {
        slug
        for slug in installed
        if slug not in internal_slugs and slug not in bound_slugs
    }
    obsolete_bindings = {
        slug
        for slug in installed
        if slug in internal_slugs and slug in bound_slugs
    }
    return installed, missing_bindings, obsolete_bindings


def _workflow_source_template_id(
    record: dict[str, Any],
    payload: dict[str, Any] | None,
) -> str:
    return str(
        record.get(BLUEPRINT_ID_KEY)
        or f"blueprint-payload:{blueprint_content_fingerprint(payload)}"
    )


def _legacy_workflow_source_template_id(
    record: dict[str, Any],
    payload: dict[str, Any] | None,
) -> str:
    manifest = (payload or {}).get("manifest") or {}
    slug = str(record.get("blueprint_slug") or manifest.get("slug") or "").strip()
    return f"blueprint:{slug}" if slug else _workflow_source_template_id(record, payload)


def _workflow_source_template_ids(
    record: dict[str, Any],
    payload: dict[str, Any] | None,
    resolved_blueprint_id: str | None = None,
) -> list[str]:
    """Exact current source followed by controlled legacy install ids."""
    values: list[str] = []
    stored_id = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    if stored_id:
        values.append(stored_id)
    elif resolved_blueprint_id:
        # For slug-only historical installs, mappings were created before the
        # canonical Marketplace id was persisted and may use blueprint:<slug>.
        values.append(str(resolved_blueprint_id))
        values.append(_legacy_workflow_source_template_id(record, payload))
    else:
        values.append(_workflow_source_template_id(record, payload))
        values.append(_legacy_workflow_source_template_id(record, payload))
    if resolved_blueprint_id:
        values.insert(0, str(resolved_blueprint_id))
    return list(dict.fromkeys(value for value in values if value))


async def _installed_blueprint_component(
    db: AsyncSession,
    *,
    workspace: Any,
    source_blueprint_ids: list[str],
    kind: str,
    spec: dict[str, Any],
) -> Any | None:
    """Resolve an installed Agent/Skill by exact link, then safe legacy scope.

    The fallback is intentionally read-only and only accepts one candidate
    already scoped or deployed into this Workspace. It never chooses the
    "best" same-slug row from the wider entity catalog.
    """
    from packages.core.blueprints.installer import normalize_skill_slug
    from packages.core.models.marketplace_resource_link import (
        MarketplaceResourceLink,
    )
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_INSTALLED_COMPONENT,
        RESOURCE_AGENT,
        RESOURCE_SKILL,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_WORKSPACE,
    )

    slug = str(spec.get("slug") or "").strip()
    component_key = str(
        spec.get("component_id") or spec.get("id") or slug
    ).strip()
    local_type = RESOURCE_SKILL if kind == "skill" else RESOURCE_AGENT
    if source_blueprint_ids:
        link = (await db.execute(
            select(MarketplaceResourceLink).where(
                MarketplaceResourceLink.entity_id == workspace.entity_id,
                MarketplaceResourceLink.marketplace_resource_type
                == RESOURCE_WORKSPACE_BLUEPRINT,
                MarketplaceResourceLink.marketplace_resource_id.in_(
                    source_blueprint_ids
                ),
                MarketplaceResourceLink.relationship
                == RELATIONSHIP_INSTALLED_COMPONENT,
                MarketplaceResourceLink.scope_type == SCOPE_WORKSPACE,
                MarketplaceResourceLink.scope_id == workspace.id,
                MarketplaceResourceLink.local_resource_type == local_type,
                MarketplaceResourceLink.component_key == component_key,
            ).limit(1)
        )).scalar_one_or_none()
        if link is not None:
            model = Skill if kind == "skill" else Agent
            row = await db.get(model, link.local_resource_id)
            if (
                row is not None
                and row.entity_id == workspace.entity_id
                and getattr(row, "status", None) == "active"
                and (
                    kind == "skill"
                    or getattr(row, "deleted_at", None) is None
                )
            ):
                return row

    if kind == "agent":
        deployed = list((await db.execute(
            select(Agent)
            .join(AgentSubscription, AgentSubscription.agent_id == Agent.id)
            .where(
                Agent.entity_id == workspace.entity_id,
                Agent.slug == slug,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
                AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.status == "active",
            )
        )).scalars().unique().all())
        if len(deployed) == 1:
            return deployed[0]
        scoped = list((await db.execute(
            select(Agent).where(
                Agent.entity_id == workspace.entity_id,
                Agent.workspace_id == workspace.id,
                Agent.slug == slug,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalars().all())
        if len(scoped) == 1:
            return scoped[0]
        legacy = list((await db.execute(
            select(Agent).where(
                Agent.entity_id == workspace.entity_id,
                Agent.slug == slug,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalars().all())
        if len(legacy) != 1:
            return None
        claimed = (await db.execute(
            select(MarketplaceResourceLink.id).where(
                MarketplaceResourceLink.entity_id == workspace.entity_id,
                MarketplaceResourceLink.relationship
                == RELATIONSHIP_INSTALLED_COMPONENT,
                MarketplaceResourceLink.local_resource_type == RESOURCE_AGENT,
                MarketplaceResourceLink.local_resource_id == legacy[0].id,
                MarketplaceResourceLink.scope_id != workspace.id,
            ).limit(1)
        )).scalar_one_or_none()
        return legacy[0] if claimed is None else None

    target = normalize_skill_slug(slug)
    scoped_skills = list((await db.execute(
        select(Skill).where(
            Skill.entity_id == workspace.entity_id,
            Skill.workspace_id == workspace.id,
            Skill.status == "active",
        )
    )).scalars().all())
    scoped_matches = [
        row for row in scoped_skills if normalize_skill_slug(row.slug) == target
    ]
    if len(scoped_matches) == 1:
        return scoped_matches[0]

    deployed_skills = list((await db.execute(
        select(Skill)
        .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
        .join(AgentSubscription, AgentSubscription.agent_id == AgentSkillBinding.agent_id)
        .where(
            Skill.entity_id == workspace.entity_id,
            Skill.status == "active",
            AgentSkillBinding.status == "active",
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        )
    )).scalars().unique().all())
    deployed_matches = [
        row for row in deployed_skills if normalize_skill_slug(row.slug) == target
    ]
    if len(deployed_matches) == 1:
        return deployed_matches[0]
    legacy_skills = list((await db.execute(
        select(Skill).where(
            Skill.entity_id == workspace.entity_id,
            Skill.status == "active",
        )
    )).scalars().all())
    legacy_matches = [
        row for row in legacy_skills if normalize_skill_slug(row.slug) == target
    ]
    if len(legacy_matches) != 1:
        return None
    claimed = (await db.execute(
        select(MarketplaceResourceLink.id).where(
            MarketplaceResourceLink.entity_id == workspace.entity_id,
            MarketplaceResourceLink.relationship
            == RELATIONSHIP_INSTALLED_COMPONENT,
            MarketplaceResourceLink.local_resource_type == RESOURCE_SKILL,
            MarketplaceResourceLink.local_resource_id == legacy_matches[0].id,
            MarketplaceResourceLink.scope_id != workspace.id,
        ).limit(1)
    )).scalar_one_or_none()
    return legacy_matches[0] if claimed is None else None


async def plan(
    db: AsyncSession,
    *,
    workspace,
    payload: dict[str, Any] | None,
    source_payload: dict[str, Any] | None = None,
    source_blueprint_id: str | None = None,
) -> dict[str, Any]:
    """What an upgrade would do. Reads only — this is what gets confirmed."""
    fingerprint_payload = (
        source_payload if isinstance(source_payload, dict) else payload
    )
    record = installed_blueprint_record(getattr(workspace, "settings", None))
    applied = record.get(APPLIED_REVISIONS_KEY)
    applied = applied if isinstance(applied, dict) else {}
    result: dict[str, Any] = {
        "workspace_id": workspace.id,
        "workspace_name": getattr(workspace, "name", ""),
        "blueprint_slug": record.get("blueprint_slug"),
        "blueprint_fingerprint": None,
        "items": [],
        "can_revert": bool(record.get(RESTORE_POINT_KEY)),
    }
    # ``payload`` is resolved by the API before the plan reaches us. Older
    # built-in installs recorded only ``blueprint_slug`` (the stable
    # ``builtin:<slug>`` id was added later), so requiring an id here makes
    # the workspace banner correctly say "update available" while this
    # dialog incorrectly returns an empty plan. A resolved payload is the
    # authority; the apply endpoint repairs the durable id when confirmed.
    has_blueprint_identity = bool(
        record.get(BLUEPRINT_ID_KEY) or record.get("blueprint_slug")
    )
    if not isinstance(payload, dict) or not has_blueprint_identity:
        return result

    result["blueprint_fingerprint"] = blueprint_content_fingerprint(
        fingerprint_payload
    )
    installed_content_fingerprint = str(
        record.get(CONTENT_FINGERPRINT_KEY) or ""
    ).strip()
    installed_unsupported_fingerprint = str(
        record.get(UPGRADE_UNSUPPORTED_FINGERPRINT_KEY) or ""
    ).strip()
    installed_unsupported_fingerprint = normalize_blueprint_upgrade_unsupported_fingerprint(
        installed_unsupported_fingerprint, fingerprint_payload,
    )
    installed_materialized_unsupported_fingerprint = str(
        record.get(MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY) or ""
    ).strip()
    current_unsupported_fingerprint = blueprint_upgrade_unsupported_fingerprint(
        fingerprint_payload
    )
    current_materialized_unsupported_fingerprint = (
        blueprint_upgrade_unsupported_fingerprint(payload)
    )
    current_materialized_comparison_payload = payload
    variable_contract_is_reconcilable = False
    previous_materialized_unsupported_fingerprint = ""
    recorded_variable_declarations = [
        item
        for item in record.get(VARIABLE_DECLARATIONS_KEY) or []
        if isinstance(item, dict)
    ]
    current_variable_declarations = (
        (fingerprint_payload.get("contract") or {}).get("variables") or []
        if isinstance(fingerprint_payload, dict)
        else []
    )
    if (
        isinstance(fingerprint_payload, dict)
        and VARIABLE_DECLARATIONS_KEY in record
        and installed_unsupported_fingerprint
        # Empty declarations are equivalent to no variables at all. Avoid
        # injecting ``variables: []`` into an otherwise unchanged contract:
        # doing so changes the hash and falsely blocks a version-only update.
        and bool(recorded_variable_declarations or current_variable_declarations)
    ):
        # contract.variables is now inside the surgical upgrade boundary. To
        # stay compatible with existing fingerprints, rebuild today's raw
        # payload with the declarations recorded at install/last upgrade. A
        # match proves every still-unsupported field remained unchanged.
        previous_contract_payload = copy.deepcopy(fingerprint_payload)
        previous_contract = dict(previous_contract_payload.get("contract") or {})
        previous_contract["variables"] = [
            dict(item)
            for item in record.get(VARIABLE_DECLARATIONS_KEY) or []
            if isinstance(item, dict)
        ]
        previous_contract_payload["contract"] = previous_contract
        installed_unsupported_fingerprint = normalize_blueprint_upgrade_unsupported_fingerprint(
            installed_unsupported_fingerprint, previous_contract_payload,
        )
        current_materialized_comparison_payload = copy.deepcopy(payload)
        current_materialized_contract = dict(
            current_materialized_comparison_payload.get("contract") or {}
        )
        current_materialized_contract["variables"] = copy.deepcopy(
            previous_contract["variables"]
        )
        current_materialized_comparison_payload["contract"] = (
            current_materialized_contract
        )
        current_materialized_unsupported_fingerprint = (
            blueprint_upgrade_unsupported_fingerprint(
                current_materialized_comparison_payload
            )
        )
        variable_contract_is_reconcilable = (
            installed_unsupported_fingerprint
            == blueprint_upgrade_unsupported_fingerprint(previous_contract_payload)
        )
        if not installed_materialized_unsupported_fingerprint:
            from packages.core.blueprints.installer import (
                InstallError,
                resolve_install_variables,
            )

            saved_personalization = (getattr(workspace, "settings", None) or {}).get(
                PERSONALIZATION_SETTINGS_KEY,
                {},
            )
            saved_values = (
                saved_personalization
                if isinstance(saved_personalization, dict)
                else {}
            )
            previous_keys = {
                str(item.get("key") or "").strip()
                for item in previous_contract.get("variables") or []
                if isinstance(item, dict) and str(item.get("key") or "").strip()
            }
            previous_materialized_payload = None
            # Defaults cannot recover one-shot install inputs. Only reconstruct
            # when every old value was explicitly retained and the unsupported
            # source still matches. Persist this proof before changing values.
            if variable_contract_is_reconcilable and all(
                bool(item.get("materialize")) and item.get("key") in saved_values
                for item in previous_contract.get("variables") or []
            ):
                try:
                    previous_materialized_payload, _ = resolve_install_variables(
                        previous_contract_payload,
                        {
                            key: value
                            for key, value in saved_values.items()
                            if key in previous_keys
                        },
                    )
                except InstallError:
                    pass
            if isinstance(previous_materialized_payload, dict):
                previous_materialized_unsupported_fingerprint = (
                    blueprint_upgrade_unsupported_fingerprint(
                        previous_materialized_payload
                    )
                )
    # Matching full fingerprints prove every portable section is unchanged,
    # so a legacy install can safely acquire the dedicated baseline. When the
    # full hash differs, the safe upgrader may still reconcile its supported
    # components but must not claim full synchronization.
    unsupported_baseline_unknown = (
        not installed_unsupported_fingerprint
        and installed_content_fingerprint != result["blueprint_fingerprint"]
    )
    materialized_unsupported_baseline = (
        installed_materialized_unsupported_fingerprint
        or previous_materialized_unsupported_fingerprint
    )
    if (
        not materialized_unsupported_baseline
        and installed_unsupported_fingerprint
        and not recorded_variable_declarations
    ):
        # Legacy installs without variables predate the dedicated materialized
        # baseline, but their raw and installed portable content are identical.
        # Their existing unsupported fingerprint is therefore an exact
        # materialized baseline rather than an unknown one.
        materialized_unsupported_baseline = installed_unsupported_fingerprint
    materialized_unsupported_baseline = normalize_blueprint_upgrade_unsupported_fingerprint(
        materialized_unsupported_baseline, current_materialized_comparison_payload,
    )
    materialized_unsupported_baseline_unknown = bool(
        (
            installed_unsupported_fingerprint
            or (fingerprint_payload.get("contract") or {}).get("variables")
        )
        and not materialized_unsupported_baseline
    )
    materialized_unsupported_changes = bool(
        materialized_unsupported_baseline
        and materialized_unsupported_baseline
        != current_materialized_unsupported_fingerprint
    )
    unsupported_baseline_unknown = (
        unsupported_baseline_unknown
        or materialized_unsupported_baseline_unknown
    )
    unsupported_changes = unsupported_baseline_unknown or bool(
        installed_unsupported_fingerprint
        and installed_unsupported_fingerprint != current_unsupported_fingerprint
        and not variable_contract_is_reconcilable
    ) or materialized_unsupported_changes
    result["unsupported_baseline_unknown"] = unsupported_baseline_unknown
    result["unsupported_changes"] = unsupported_changes
    result["materialized_unsupported_baseline"] = materialized_unsupported_baseline
    if unsupported_changes:
        if unsupported_baseline_unknown:
            unsupported_change = (
                "This installation predates safe upgrade tracking; "
                "safe Agent, Skill, and Workflow updates can be applied, but "
                "the Workspace will remain unconfirmed until reconfigured or reinstalled"
            )
        else:
            unsupported_change = (
                "Workspace shell, subscriptions, automations, Goals/Stats, "
                "governance, requirements, or Knowledge structure changed; "
                "review and reinstall/reconfigure instead of marking this "
                "Workspace current"
            )
        result["items"].append({
            "kind": "blueprint_configuration",
            "slug": "unsupported-portable-configuration",
            "name": "Blueprint configuration outside the safe upgrade scope",
            "action": (
                UpgradeAction.BASELINE_UNKNOWN.value
                if unsupported_baseline_unknown
                else UpgradeAction.RECONFIGURE.value
            ),
            "changes": [unsupported_change],
        })
    embedded = payload.get("embedded") or {}
    entity_id = workspace.entity_id
    source_blueprint_ids = _workflow_source_template_ids(
        record,
        payload,
        source_blueprint_id,
    )
    from packages.core.blueprints.installer import (
        blueprint_workflow_installation_source_id,
    )

    source_template_ids = [
        blueprint_workflow_installation_source_id(source_id, workspace.id)
        for source_id in source_blueprint_ids
    ] + source_blueprint_ids

    for kind, specs in (("skill", embedded.get("skills") or []),
                        ("agent", embedded.get("agents") or [])):
        for spec in specs:
            slug = str(spec.get("slug") or "").strip()
            if not slug:
                continue
            component_key = str(
                spec.get("component_id") or spec.get("id") or slug
            ).strip()

            row = await _installed_blueprint_component(
                db,
                workspace=workspace,
                source_blueprint_ids=source_blueprint_ids,
                kind=kind,
                spec=spec,
            )

            label = spec.get("display_name") or spec.get("name") or slug
            if row is None:
                result["items"].append({
                    "kind": kind, "slug": slug,
                    "component_key": component_key, "name": label,
                    "action": UpgradeAction.MISSING.value, "changes": [],
                })
                continue

            patch = content_patch_for(row, _desired_content(kind, spec), _fields_for(kind))
            if not patch:
                action = UpgradeAction.UNCHANGED
            elif _is_workspace_edited(row, applied):
                action = UpgradeAction.KEEP_YOURS
            else:
                action = UpgradeAction.UPDATE

            result["items"].append({
                "kind": kind,
                "slug": slug,
                "component_key": component_key,
                "name": getattr(row, "name", label),
                "id": row.id,
                "revision": int(getattr(row, "revision", PRISTINE_REVISION) or PRISTINE_REVISION),
                "action": action.value,
                "changes": _describe(kind, patch, row) if patch else [],
                "new_content": _preview(patch) if patch else {},
            })

    workflow_specs = ((payload.get("recipe") or {}).get("workflows") or [])
    workflow_component_keys = {
        str(spec.get("slug") or "").strip(): str(
            spec.get("component_id")
            or spec.get("id")
            or spec.get("slug")
            or ""
        ).strip()
        for spec in workflow_specs
        if isinstance(spec, dict)
    }
    internal_slugs = {
        str(spec.get("slug") or "").strip()
        for spec in workflow_specs
        if isinstance(spec, dict) and spec.get("internal")
    }
    (
        installed_workflows,
        missing_workflow_bindings,
        obsolete_workflow_bindings,
    ) = await _installed_blueprint_workflows(
        db,
        entity_id=entity_id,
        workspace_id=workspace.id,
        source_blueprint_ids=source_blueprint_ids,
        source_template_ids=source_template_ids,
        workflow_component_keys=workflow_component_keys,
        internal_slugs=internal_slugs,
    )
    workflow_id_by_key = {
        slug: row.id for slug, row in installed_workflows.items()
    }
    for spec in workflow_specs:
        if not isinstance(spec, dict):
            continue
        slug = str(spec.get("slug") or "").strip()
        if not slug:
            continue
        component_key = str(
            spec.get("component_id") or spec.get("id") or slug
        ).strip()
        row = installed_workflows.get(slug)
        label = spec.get("name") or slug
        if row is None:
            desired = _desired_content("workflow", spec)
            result["items"].append({
                "kind": "workflow", "slug": slug,
                "component_key": component_key, "name": label,
                "action": UpgradeAction.MISSING.value,
                "changes": ["installs Blueprint Flow and Workspace binding"],
                "new_content": _preview(desired),
            })
            continue

        patch = content_patch_for(
            row,
            _planned_workflow_content(
                spec,
                workflow_id_by_key=workflow_id_by_key,
            ),
            _fields_for("workflow"),
        )
        binding_missing = slug in missing_workflow_bindings
        binding_remove = slug in obsolete_workflow_bindings
        if not patch and not binding_missing and not binding_remove:
            action = UpgradeAction.UNCHANGED
        elif _is_workspace_edited(row, applied):
            action = UpgradeAction.KEEP_YOURS
        else:
            action = UpgradeAction.UPDATE
        result["items"].append({
            "kind": "workflow",
            "slug": slug,
            "component_key": component_key,
            "name": label,
            "id": row.id,
            "revision": int(getattr(row, "revision", PRISTINE_REVISION) or PRISTINE_REVISION),
            "action": action.value,
            "changes": (
                (_describe("workflow", patch, row) if patch else [])
                + (["restores missing Workspace binding"] if binding_missing else [])
                + (["removes obsolete Workspace binding"] if binding_remove else [])
            ),
            "new_content": _preview(patch) if patch else {},
            "binding_missing": binding_missing,
            "binding_remove": binding_remove,
        })

    # Inline starter documents used to be emitted as manual install todos,
    # which meant an installed Blueprint could advertise Workspace Knowledge
    # while its Knowledge Net was empty. Include absent starter documents in
    # the explicit upgrade plan so existing workspaces can be repaired with
    # the same operator confirmation as any other Blueprint change.
    from packages.core.blueprints.installer import (
        _blueprint_document_template,
        _knowledge_pack_document_rows,
        _matches_blueprint_starter_document,
    )
    from packages.core.models.document import DocumentGroup

    for kp in embedded.get("knowledge_packs") or []:
        if not isinstance(kp, dict) or kp.get("mode") != "inline_text":
            continue
        pack_slug = str(kp.get("slug") or "").strip()
        pack_title = str(kp.get("title") or pack_slug).strip()
        if not pack_slug or not pack_title:
            continue
        group = (await db.execute(
            select(DocumentGroup).where(
                DocumentGroup.entity_id == entity_id,
                DocumentGroup.workspace_id == workspace.id,
                DocumentGroup.name == pack_title,
            )
        )).scalar_one_or_none()
        # A missing pack is a separately missing Blueprint resource, not a
        # document-content upgrade. Legacy installs always created the group;
        # repair only those real legacy groups here so partial test/install
        # surfaces are not silently expanded by an unrelated upgrade.
        if group is None:
            continue
        rows = await _knowledge_pack_document_rows(
            db, entity_id=entity_id, group_id=group.id,
            knowledge_pack_slug=pack_slug,
            documents=[item for item in kp.get("starter_documents") or [] if isinstance(item, dict)],
        )
        for document in kp.get("starter_documents") or []:
            if not isinstance(document, dict):
                continue
            path = str(document.get("path") or "").strip()
            body = str(document.get("body_md") or "")
            document_key = str(document.get("key") or "").strip()
            if not path or not body:
                continue
            existing_row = next((
                row
                for row in rows
                if _matches_blueprint_starter_document(
                    row,
                    knowledge_pack_slug=pack_slug,
                    path=path,
                    document_key=document_key,
                )
            ), None)
            template = _blueprint_document_template(document)
            existing_metadata = (
                existing_row.metadata_
                if existing_row is not None and isinstance(existing_row.metadata_, dict)
                else {}
            )
            template_binding_current = bool(
                existing_row is not None
                and (
                    template is None
                    or (
                        (
                            existing_metadata.get("blueprint_document_key")
                            == document_key
                            if document_key
                            else (
                                existing_metadata.get("blueprint_knowledge_pack_slug")
                                == pack_slug
                                and existing_metadata.get("blueprint_starter_path")
                                == path
                            )
                        )
                        and existing_metadata.get("blueprint_template") == template
                    )
                )
            )
            if existing_row is None:
                action = UpgradeAction.UPDATE.value
                changes = ["adds starter Knowledge document"]
                new_content = {"body_md": body}
            elif not template_binding_current:
                action = UpgradeAction.UPDATE.value
                changes = ["updates live Knowledge template binding"]
                new_content = {"template": template}
            else:
                action = UpgradeAction.UNCHANGED.value
                changes = []
                new_content = {}
            result["items"].append({
                "kind": "knowledge_document",
                "slug": f"{pack_slug}:{document_key or path}",
                "knowledge_pack_slug": pack_slug,
                "document_key": document_key,
                "document_path": path,
                "name": path,
                "action": action,
                "changes": changes,
                "new_content": new_content,
            })

    return result


def _resolved_conflict_updates(
    intended: dict[str, Any],
    conflict_resolutions: list[dict[str, Any]] | None,
) -> set[tuple[str, str]]:
    """Validate a reviewed conflict set and return explicit overwrite keys.

    A non-``None`` list comes from the interactive conflict UI. It must name
    every conflict in the freshly recomputed plan and carry the revision the
    operator reviewed. This prevents a late Workspace edit from being
    overwritten by a stale browser decision.
    """
    if conflict_resolutions is None:
        return set()

    current = {
        (str(item.get("kind") or ""), str(item.get("slug") or "")): item
        for item in intended.get("items") or []
        if item.get("action") == UpgradeAction.KEEP_YOURS.value
    }
    submitted: dict[tuple[str, str], dict[str, Any]] = {}
    for resolution in conflict_resolutions:
        key = (
            str(resolution.get("kind") or "").strip(),
            str(resolution.get("slug") or "").strip(),
        )
        if not all(key) or key in submitted:
            raise BlueprintUpgradePlanChangedError(
                "Blueprint upgrade conflicts changed. Review the latest plan and try again."
            )
        submitted[key] = resolution

    if set(submitted) != set(current):
        raise BlueprintUpgradePlanChangedError(
            "Blueprint upgrade conflicts changed. Review the latest plan and try again."
        )

    explicit_updates: set[tuple[str, str]] = set()
    for key, resolution in submitted.items():
        item = current[key]
        if int(resolution.get("expected_revision") or 0) != int(item.get("revision") or 0):
            raise BlueprintUpgradePlanChangedError(
                "A conflicted Workspace item changed after review. Review the latest plan and try again."
            )
        decision = str(resolution.get("resolution") or "")
        if decision == "use_blueprint":
            explicit_updates.add(key)
        elif decision != "keep_yours":
            raise BlueprintUpgradePlanChangedError(
                "Blueprint upgrade conflict resolution is invalid. Review the latest plan and try again."
            )
    return explicit_updates


async def _isolate_legacy_blueprint_workflow(
    db: AsyncSession,
    *,
    row: Any,
    entity_id: str,
    workspace_id: str,
    component_key: str,
    source_template_id: str,
    source_blueprint_id: str,
    source_version: str,
    installed_by: Optional[str],
) -> Any:
    """Clone a pre-scope Blueprint Flow before mutating one Workspace.

    Older installs intentionally shared one entity-level definition. Upgrade
    is Workspace-scoped, so changing that row would silently rewrite every
    other Workspace binding. The clone and mapping are durable infrastructure:
    revert restores its old content but keeps the Workspace isolated.
    """
    from packages.core.models.base import generate_ulid
    from packages.core.models.permission import Visibility
    from packages.core.models.workflow import (
        WorkflowBinding,
        WorkflowDefinition,
        WorkflowTemplateInstallation,
    )

    existing_installation = (await db.execute(
        select(WorkflowTemplateInstallation)
        .where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.template_id == source_template_id,
            WorkflowTemplateInstallation.component_key == component_key,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if existing_installation is not None:
        clone = await db.get(
            WorkflowDefinition,
            existing_installation.workflow_id,
            populate_existing=True,
            with_for_update=True,
        )
        if clone is None or clone.workspace_id != workspace_id:
            raise BlueprintUpgradePlanChangedError(
                "Blueprint Flow identity is ambiguous. Review the Workspace before upgrading."
            )
        existing_installation.installed_version = source_version
    else:
        values = _row_snapshot(row, WORKFLOW_INSTALL_FIELDS)
        clone = WorkflowDefinition(
            id=generate_ulid(),
            entity_id=entity_id,
            created_by=installed_by or getattr(row, "created_by", None),
            workspace_id=workspace_id,
            visibility=Visibility.WORKSPACE,
            icon=getattr(row, "icon", "flow"),
            revision=int(
                getattr(row, "revision", PRISTINE_REVISION)
                or PRISTINE_REVISION
            ),
            **values,
        )
        db.add(clone)
        await db.flush()
        db.add(WorkflowTemplateInstallation(
            id=generate_ulid(),
            entity_id=entity_id,
            template_id=source_template_id,
            component_key=component_key,
            workflow_id=clone.id,
            installed_version=source_version,
            installed_by=installed_by,
            source_type="workspace_blueprint",
            installation_metadata={
                "source_workflow_key": component_key,
                "source_blueprint_id": source_blueprint_id,
            },
        ))

    bindings = list((await db.execute(
        select(WorkflowBinding)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.workflow_id == row.id,
        )
        .with_for_update()
    )).scalars().all())
    for binding in bindings:
        config = dict(binding.config or {})
        binding_slug = str(
            config.get("workspace_blueprint_workflow_slug") or ""
        ).strip()
        if binding_slug and binding_slug != component_key:
            continue
        binding.workflow_id = clone.id
        config["source_template_id"] = source_blueprint_id
        binding.config = config
    await db.flush()
    return clone


async def apply(
    db: AsyncSession,
    *,
    workspace,
    payload: dict[str, Any] | None,
    source_payload: dict[str, Any] | None = None,
    personalization: dict[str, Any] | None = None,
    by_user_id: Optional[str] = None,
    current_version: Optional[str] = None,
    source_blueprint_id: Optional[str] = None,
    expected_blueprint_fingerprint: Optional[str] = None,
    conflict_resolutions: list[dict[str, Any]] | None = None,
    channel_config_ids: Optional[dict[str, str]] = None,
    actor: Any | None = None,
    require_complete_workspace: bool = False,
) -> dict[str, Any]:
    """Overwrite planned updates and explicitly resolved conflicts. Caller commits.

    The previous values are captured before anything is written, so revert()
    has something to put back. Workspace edits remain untouched by default.
    """
    from packages.core.models.skill import Skill
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace import Agent, Workspace

    await lock_reusable_resource_lifecycle(
        db,
        entity_id=workspace.entity_id,
    )

    fingerprint_payload = (
        source_payload if isinstance(source_payload, dict) else payload
    )
    current_fingerprint = (
        blueprint_content_fingerprint(fingerprint_payload)
        if isinstance(fingerprint_payload, dict)
        else None
    )
    if (
        expected_blueprint_fingerprint is not None
        and expected_blueprint_fingerprint != current_fingerprint
    ):
        raise BlueprintUpgradePlanChangedError(
            "The Blueprint changed after review. Review the latest plan and try again."
        )

    locked_workspace = await db.get(
        Workspace,
        workspace.id,
        populate_existing=True,
        with_for_update=True,
    )
    if (
        locked_workspace is None
        or str(locked_workspace.entity_id) != str(workspace.entity_id)
    ):
        raise BlueprintUpgradePlanChangedError(
            "The Workspace changed after review. Review the latest plan and try again."
        )
    workspace = locked_workspace

    install_record = installed_blueprint_record(
        getattr(workspace, "settings", None)
    )
    canonical_source_id = str(
        source_blueprint_id
        or install_record.get(BLUEPRINT_ID_KEY)
        or ""
    ).strip()
    source_blueprint_ids = _workflow_source_template_ids(
        install_record,
        payload,
        canonical_source_id or None,
    )
    from packages.core.blueprints.installer import (
        blueprint_workflow_installation_source_id,
    )

    source_template_ids = [
        blueprint_workflow_installation_source_id(source_id, workspace.id)
        for source_id in source_blueprint_ids
    ] + source_blueprint_ids
    intended = await plan(
        db,
        workspace=workspace,
        payload=payload,
        source_payload=fingerprint_payload,
        source_blueprint_id=canonical_source_id or None,
    )
    unavailable_missing = [
        item for item in intended.get("items") or []
        if item.get("action") == UpgradeAction.MISSING.value
        and item.get("kind") != "workflow"
    ]
    if require_complete_workspace and unavailable_missing:
        names = ", ".join(
            str(item.get("name") or item.get("slug") or item.get("kind") or "component")
            for item in unavailable_missing
        )
        raise BlueprintUpgradeIncompleteError(
            f"Workspace is missing Blueprint components that must be restored before upgrade: {names}"
        )
    explicit_conflict_updates = _resolved_conflict_updates(
        intended, conflict_resolutions,
    )
    embedded = (payload or {}).get("embedded") or {}
    by_slug = {
        (kind, str(spec.get("slug") or "").strip()): spec
        for kind, specs in (("skill", embedded.get("skills") or []),
                            ("agent", embedded.get("agents") or []))
        for spec in specs
    }
    by_slug.update({
        ("workflow", str(spec.get("slug") or "").strip()): spec
        for spec in ((payload or {}).get("recipe") or {}).get("workflows") or []
        if isinstance(spec, dict)
    })
    knowledge_documents = {
        (
            str(kp.get("slug") or "").strip(),
            str(document.get("path") or "").strip(),
        ): (kp, document)
        for kp in embedded.get("knowledge_packs") or []
        if isinstance(kp, dict)
        for document in kp.get("starter_documents") or []
        if isinstance(document, dict)
    }

    restored: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    applied_now: dict[str, int] = {}
    knowledge_created_document_ids: set[str] = set()
    workflow_id_by_key = {
        str(item.get("slug") or ""): str(item.get("id") or "")
        for item in intended.get("items") or []
        if item.get("kind") == "workflow" and item.get("id")
    }

    manifest = (payload or {}).get("manifest") or {}
    missing_workflow_items = [
        item
        for item in intended.get("items") or []
        if item.get("kind") == "workflow"
        and item.get("action") == UpgradeAction.MISSING.value
    ]
    precreated_missing_workflow_ids: dict[str, str] = {}
    if missing_workflow_items:
        from packages.core.blueprints.installer import _install_workflow

        workflow_source_id = (
            canonical_source_id
            or _workflow_source_template_id(install_record, payload)
        )
        source_template_id = blueprint_workflow_installation_source_id(
            workflow_source_id,
            workspace.id,
        )
        source_version = str(
            current_version
            or install_record.get(BLUEPRINT_VERSION_KEY)
            or manifest.get("blueprint_version")
            or "1.0.0"
        )

        async def reject_changed_missing_workflow(
            _row: WorkflowDefinition,
        ) -> None:
            raise BlueprintUpgradePlanChangedError(
                "A missing Blueprint Flow appeared after review. "
                "Review the latest plan and try again."
            )

        # Match the full installer: establish every local Flow identity first,
        # then materialize portable subworkflow keys in the main pass below.
        # This supports forward references and cycles without payload ordering.
        for missing_item in missing_workflow_items:
            slug = str(missing_item.get("slug") or "").strip()
            spec = by_slug.get(("workflow", slug))
            if not spec:
                raise BlueprintUpgradeIncompleteError(
                    f"Missing Blueprint Flow specification for {slug!r}"
                )
            workflow_id = await _install_workflow(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                w=spec,
                source_template_id=source_template_id,
                source_version=source_version,
                installed_by=by_user_id,
                adopt_unmapped=False,
                authorize_existing_update=reject_changed_missing_workflow,
            )
            if not workflow_id:
                raise BlueprintUpgradeIncompleteError(
                    f"Blueprint Flow {slug!r} could not be installed"
                )
            precreated_missing_workflow_ids[slug] = workflow_id
            workflow_id_by_key[slug] = workflow_id
            missing_item["id"] = workflow_id

    for item in intended["items"]:
        item_key = (str(item.get("kind") or ""), str(item.get("slug") or ""))
        resolves_conflict = item_key in explicit_conflict_updates
        installs_missing_workflow = (
            item["kind"] == "workflow"
            and item["action"] == UpgradeAction.MISSING.value
        )
        if (
            item["action"] != UpgradeAction.UPDATE.value
            and not resolves_conflict
            and not installs_missing_workflow
        ):
            continue
        kind, slug = item["kind"], item["slug"]
        if kind == "knowledge_document":
            pack_slug = str(item.get("knowledge_pack_slug") or "").strip()
            document_key = str(item.get("document_key") or "").strip()
            document_path = str(item.get("document_path") or "").strip()
            source = knowledge_documents.get((pack_slug, document_path))
            if not source:
                continue
            pack, document = source
            from packages.core.blueprints.installer import (
                _install_knowledge_pack,
                _knowledge_pack_document_rows,
                _matches_blueprint_starter_document,
                _workspace_blueprint_document_rows,
            )
            from packages.core.models.document import DocumentGroup

            group = (await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.entity_id == workspace.entity_id,
                    DocumentGroup.workspace_id == workspace.id,
                    DocumentGroup.name == (pack.get("title") or pack_slug),
                )
            )).scalar_one_or_none()
            existing_row = None
            if group is not None:
                existing_rows = await _knowledge_pack_document_rows(
                    db, entity_id=workspace.entity_id, group_id=group.id,
                    knowledge_pack_slug=pack_slug, documents=[document],
                )
                existing_row = next((
                    candidate for candidate in existing_rows
                    if _matches_blueprint_starter_document(
                        candidate,
                        knowledge_pack_slug=pack_slug,
                        path=document_path,
                        document_key=document_key,
                    )
                ), None)
            existing_workspace_row = existing_row
            if existing_workspace_row is None and document_key:
                workspace_rows = await _workspace_blueprint_document_rows(
                    db,
                    entity_id=workspace.entity_id,
                    workspace_id=workspace.id,
                    document_key=document_key,
                )
                existing_workspace_row = next((
                    candidate for candidate in workspace_rows
                    if _matches_blueprint_starter_document(
                        candidate,
                        knowledge_pack_slug=pack_slug,
                        path=document_path,
                        document_key=document_key,
                    )
                ), None)
            existing_metadata = (
                dict(existing_workspace_row.metadata_)
                if existing_workspace_row is not None
                and isinstance(existing_workspace_row.metadata_, dict)
                else None
            )
            if existing_workspace_row is not None:
                from packages.core.models.permission import Capability

                await _require_document_access(
                    db,
                    row=existing_workspace_row,
                    actor=actor,
                    capability=Capability.MANAGE_METADATA,
                )
            await _install_knowledge_pack(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                kp={**pack, "starter_documents": [document]},
                todos=[],
            )
            group = group or (await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.entity_id == workspace.entity_id,
                    DocumentGroup.workspace_id == workspace.id,
                    DocumentGroup.name == (pack.get("title") or pack_slug),
                )
            )).scalar_one_or_none()
            if group is None:
                continue
            rows = await _knowledge_pack_document_rows(
                db, entity_id=workspace.entity_id, group_id=group.id,
                knowledge_pack_slug=pack_slug, documents=[document],
            )
            row = next((
                candidate for candidate in rows
                if _matches_blueprint_starter_document(
                    candidate,
                    knowledge_pack_slug=pack_slug,
                    path=document_path,
                    document_key=document_key,
                )
            ), None)
            if row is None:
                continue
            if existing_workspace_row is None and by_user_id:
                row.owner_id = by_user_id
                row.created_by = by_user_id
            if row.id not in knowledge_created_document_ids:
                document_created = existing_workspace_row is None
                if document_created:
                    knowledge_created_document_ids.add(row.id)
                membership_created = (
                    not document_created and existing_row is None
                )
                metadata_changed = bool(
                    existing_metadata is not None
                    and row.metadata_ != existing_metadata
                )
                guard_fields = (
                    KNOWLEDGE_DELETE_GUARD_FIELDS
                    if document_created
                    else ("metadata_",)
                )
                if document_created or membership_created or metadata_changed:
                    restored.append({
                        "kind": kind,
                        "id": row.id,
                        "name": document_path,
                        "delete_on_revert": document_created,
                        "after": _row_snapshot(row, guard_fields),
                        **(
                            {"remove_group_membership_on_revert": group.id}
                            if membership_created or document_created
                            else {}
                        ),
                        **(
                            {"before": {"metadata_": existing_metadata}}
                            if metadata_changed
                            else {}
                        ),
                    })
            updated.append({
                "kind": kind,
                "name": document_path,
                "changes": item["changes"],
            })
            continue
        spec = by_slug.get((kind, slug))
        if not spec:
            continue

        if installs_missing_workflow:
            from packages.core.blueprints.installer import (
                _blueprint_workflow_definition_values,
                _install_workflow,
                _install_workflow_binding,
            )
            from packages.core.models.workflow import (
                WorkflowBinding,
                WorkflowTemplateInstallation,
            )

            workflow_source_id = (
                canonical_source_id
                or _workflow_source_template_id(install_record, payload)
            )
            source_template_id = blueprint_workflow_installation_source_id(
                workflow_source_id,
                workspace.id,
            )
            source_version = str(
                current_version
                or install_record.get(BLUEPRINT_VERSION_KEY)
                or manifest.get("blueprint_version")
                or "1.0.0"
            )

            existing_workflow_before: dict[str, Any] | None = None
            existing_workflow_patch: dict[str, Any] = {}
            existing_workflow_revision: int | None = None
            existing_installation: WorkflowTemplateInstallation | None = None
            existing_installation_before: dict[str, Any] | None = None
            resolved_spec = spec
            if WorkflowDependencyFactory.has_dependencies(
                list(spec.get("steps") or [])
            ):
                try:
                    resolved_spec = copy.deepcopy(spec)
                    resolved_spec["steps"] = WorkflowDependencyFactory.to_runtime(
                        list(spec.get("steps") or []),
                        workflow_id_by_key=workflow_id_by_key,
                    )
                except WorkflowDependencyError as exc:
                    raise BlueprintUpgradeIncompleteError(str(exc)) from exc
            desired_workflow = _blueprint_workflow_definition_values(resolved_spec)

            async def authorize_existing_workflow(row: WorkflowDefinition) -> None:
                nonlocal existing_workflow_before
                nonlocal existing_workflow_patch
                nonlocal existing_workflow_revision
                nonlocal existing_installation
                nonlocal existing_installation_before

                if row.id == precreated_missing_workflow_ids.get(slug):
                    return

                await _require_component_edit_access(
                    db,
                    row=row,
                    kind="workflow",
                    entity_id=workspace.entity_id,
                    actor=actor,
                )
                existing_workflow_before = _row_snapshot(
                    row, WORKFLOW_INSTALL_FIELDS,
                )
                existing_workflow_patch = content_patch_for(
                    row,
                    desired_workflow,
                    WORKFLOW_CONTENT_REVISION_FIELDS,
                )
                existing_workflow_revision = int(
                    getattr(row, "revision", PRISTINE_REVISION)
                    or PRISTINE_REVISION
                )
                existing_installation = (await db.execute(
                    select(WorkflowTemplateInstallation)
                    .where(
                        WorkflowTemplateInstallation.entity_id
                        == workspace.entity_id,
                        WorkflowTemplateInstallation.template_id
                        == source_template_id,
                        WorkflowTemplateInstallation.component_key == slug,
                    )
                    .with_for_update()
                )).scalar_one_or_none()
                if existing_installation is not None:
                    existing_installation_before = _row_snapshot(
                        existing_installation,
                        WORKFLOW_INSTALLATION_FIELDS,
                    )

            workflow_id = await _install_workflow(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                w=resolved_spec,
                source_template_id=source_template_id,
                source_version=source_version,
                installed_by=by_user_id,
                # Upgrade is not an install migration: a merely same-name Flow
                # is user-owned and must never be claimed or later deleted by
                # Undo. Only an existing source mapping may be reconciled.
                adopt_unmapped=False,
                authorize_existing_update=authorize_existing_workflow,
            )
            if not workflow_id:
                continue
            workflow_id_by_key[slug] = workflow_id
            workflow_row = await db.get(
                WorkflowDefinition,
                workflow_id,
                populate_existing=True,
                with_for_update=True,
            )
            if workflow_row is None:
                raise BlueprintUpgradePlanChangedError(
                    "A Blueprint Flow changed during upgrade. Review the latest plan and try again."
                )
            item["id"] = workflow_id
            if existing_workflow_before is not None and existing_workflow_patch:
                new_revision = await bump_revision(
                    db,
                    workflow_row,
                    patch=existing_workflow_patch,
                    changed_by_kind="user" if by_user_id else "system",
                    changed_by_id=by_user_id,
                )
                applied_now[str(workflow_row.id)] = new_revision
            else:
                new_revision = int(
                    getattr(workflow_row, "revision", PRISTINE_REVISION)
                    or PRISTINE_REVISION
                )

            prior_bindings = list((await db.execute(
                select(WorkflowBinding)
                .where(
                    WorkflowBinding.entity_id == workspace.entity_id,
                    WorkflowBinding.workspace_id == workspace.id,
                    WorkflowBinding.workflow_id == workflow_id,
                )
                .with_for_update()
            )).scalars().all())
            binding_before_by_id = {
                row.id: _row_snapshot(row, WORKFLOW_BINDING_FIELDS)
                for row in prior_bindings
            }
            binding_id = None
            if not bool(spec.get("internal")):
                binding_id = await _install_workflow_binding(
                    db,
                    entity_id=workspace.entity_id,
                    workspace_id=workspace.id,
                    workflow_id=workflow_id,
                    w=resolved_spec,
                    source_template_id=workflow_source_id,
                )
            binding_row = (
                await db.get(
                    WorkflowBinding,
                    binding_id,
                    populate_existing=True,
                    with_for_update=True,
                )
                if binding_id
                else None
            )
            installation = (await db.execute(
                select(WorkflowTemplateInstallation)
                .where(
                    WorkflowTemplateInstallation.entity_id == workspace.entity_id,
                    WorkflowTemplateInstallation.template_id == source_template_id,
                    WorkflowTemplateInstallation.component_key == slug,
                    WorkflowTemplateInstallation.workflow_id == workflow_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if installation is None:
                raise BlueprintUpgradePlanChangedError(
                    "A Blueprint Flow mapping changed during upgrade. Review the latest plan and try again."
                )

            restore_entry = {
                "kind": "workflow",
                "id": workflow_id,
                "binding_id": binding_id,
                "binding_delete_on_revert": (
                    bool(binding_id) and binding_id not in binding_before_by_id
                ),
                **(
                    {
                        "binding_before": binding_before_by_id[binding_id],
                        "binding_after": _row_snapshot(
                            binding_row, WORKFLOW_BINDING_FIELDS,
                        ),
                    }
                    if binding_row is not None and binding_id in binding_before_by_id
                    else {
                        "binding_after": _row_snapshot(
                            binding_row, WORKFLOW_BINDING_FIELDS,
                        ),
                    }
                    if binding_row is not None
                    else {}
                ),
                "name": spec.get("name") or slug,
                "source_template_id": source_template_id,
                "component_key": slug,
                "installation_id": installation.id,
                "installation_delete_on_revert": existing_installation is None,
                "installation_after": _row_snapshot(
                    installation, WORKFLOW_INSTALLATION_FIELDS,
                ),
                **(
                    {"installation_before": existing_installation_before}
                    if existing_installation_before is not None
                    else {}
                ),
                "delete_on_revert": existing_workflow_before is None,
                "after_revision": new_revision,
                "after": _row_snapshot(
                    workflow_row,
                    WORKFLOW_DELETE_GUARD_FIELDS
                    if existing_workflow_before is None
                    else WORKFLOW_INSTALL_FIELDS,
                ),
                **(
                    {
                        "before": existing_workflow_before,
                        "before_revision": existing_workflow_revision,
                    }
                    if existing_workflow_before is not None
                    else {}
                ),
            }
            restored.append(restore_entry)
            updated.append({
                "kind": "workflow",
                "name": spec.get("name") or slug,
                "changes": item["changes"],
            })
            continue

        model = (
            Skill
            if kind == "skill"
            else WorkflowDefinition
            if kind == "workflow"
            else Agent
        )
        row = await db.get(
            model,
            item["id"],
            populate_existing=True,
            with_for_update=True,
        )
        if row is None:
            raise BlueprintUpgradePlanChangedError(
                "A Workspace item changed after review. Review the latest plan and try again."
            )
        planned_revision = int(item.get("revision") or PRISTINE_REVISION)
        current_revision = int(
            getattr(row, "revision", PRISTINE_REVISION) or PRISTINE_REVISION
        )
        if current_revision != planned_revision:
            raise BlueprintUpgradePlanChangedError(
                "A Workspace item changed after review. Review the latest plan and try again."
            )

        if kind == "workflow" and row.workspace_id != workspace.id:
            await _require_component_edit_access(
                db,
                row=row,
                kind=kind,
                entity_id=workspace.entity_id,
                actor=actor,
            )
            workflow_source_id = (
                canonical_source_id
                or _workflow_source_template_id(install_record, payload)
            )
            row = await _isolate_legacy_blueprint_workflow(
                db,
                row=row,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                component_key=slug,
                source_template_id=blueprint_workflow_installation_source_id(
                    workflow_source_id,
                    workspace.id,
                ),
                source_blueprint_id=workflow_source_id,
                source_version=str(
                    current_version
                    or install_record.get(BLUEPRINT_VERSION_KEY)
                    or manifest.get("blueprint_version")
                    or "1.0.0"
                ),
                installed_by=by_user_id,
            )
            item["id"] = row.id

        try:
            desired_content = _desired_content(
                kind,
                spec,
                workflow_id_by_key=workflow_id_by_key,
            )
        except WorkflowDependencyError as exc:
            raise BlueprintUpgradeIncompleteError(str(exc)) from exc
        patch = content_patch_for(
            row,
            desired_content,
            _fields_for(kind),
        )
        binding_missing = bool(
            kind == "workflow" and item.get("binding_missing")
        )
        binding_remove = bool(
            kind == "workflow" and item.get("binding_remove")
        )
        if not patch and not binding_missing and not binding_remove:
            continue
        if kind == "workflow" and "steps" in patch:
            await lock_reusable_resource_payload_reference_delta(
                db,
                entity_id=workspace.entity_id,
                before=row.steps,
                after=patch["steps"],
            )
        await _require_component_edit_access(
            db,
            row=row,
            kind=kind,
            entity_id=workspace.entity_id,
            actor=actor,
        )

        # Capture before writing — only the fields being overwritten.
        restore_entry = {
            "kind": kind,
            "id": row.id,
            "name": getattr(row, "name", slug),
            "before": _row_snapshot(row, patch),
        }
        restored.append(restore_entry)
        workflow_installation = None
        if kind == "workflow":
            from packages.core.models.workflow import WorkflowTemplateInstallation

            lookup_source_ids = list(dict.fromkeys(
                [canonical_source_id, *source_template_ids]
            ))
            workflow_installation = (await db.execute(
                select(WorkflowTemplateInstallation)
                .where(
                    WorkflowTemplateInstallation.entity_id == workspace.entity_id,
                    WorkflowTemplateInstallation.template_id.in_(lookup_source_ids),
                    WorkflowTemplateInstallation.component_key == slug,
                    WorkflowTemplateInstallation.workflow_id == row.id,
                )
                .with_for_update()
            )).scalars().first()
            if workflow_installation is not None:
                restore_entry.update({
                    "installation_id": workflow_installation.id,
                    "installation_delete_on_revert": False,
                    "installation_before": _row_snapshot(
                        workflow_installation, WORKFLOW_INSTALLATION_FIELDS,
                    ),
                })
                if current_version:
                    workflow_installation.installed_version = current_version
                workflow_installation.installation_metadata = {
                    **dict(workflow_installation.installation_metadata or {}),
                    "source_workflow_key": slug,
                }
        for field, value in patch.items():
            setattr(row, field, value)
        if patch:
            new_revision = await bump_revision(
                db, row, patch=patch,
                changed_by_kind="user" if by_user_id else "system",
                changed_by_id=by_user_id,
            )
            applied_now[str(row.id)] = new_revision
        else:
            new_revision = current_revision

        if kind == "workflow" and not bool(spec.get("internal")) and (
            patch or binding_missing
        ):
            from packages.core.blueprints.installer import _install_workflow_binding

            prior_bindings = list((await db.execute(
                select(WorkflowBinding).where(
                    WorkflowBinding.entity_id == workspace.entity_id,
                    WorkflowBinding.workspace_id == workspace.id,
                    WorkflowBinding.workflow_id == row.id,
                )
            )).scalars().all())
            prior_binding_snapshots = {
                binding.id: _row_snapshot(binding, WORKFLOW_BINDING_FIELDS)
                for binding in prior_bindings
            }
            binding_id = await _install_workflow_binding(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                workflow_id=row.id,
                w=spec,
                source_template_id=(
                    canonical_source_id
                    or _workflow_source_template_id(install_record, payload)
                ),
            )
            if binding_id:
                binding = await db.get(
                    WorkflowBinding,
                    binding_id,
                    populate_existing=True,
                    with_for_update=True,
                )
                if binding is None:
                    raise BlueprintUpgradePlanChangedError(
                        "A Blueprint Flow binding changed during upgrade. Review and try again."
                    )
                restore_entry.update({
                    "binding_id": binding.id,
                    "binding_delete_on_revert": (
                        binding.id not in prior_binding_snapshots
                    ),
                    "binding_after": _row_snapshot(
                        binding, WORKFLOW_BINDING_FIELDS,
                    ),
                    **(
                        {"binding_before": prior_binding_snapshots[binding.id]}
                        if binding.id in prior_binding_snapshots
                        else {}
                    ),
                })
        elif kind == "workflow" and bool(spec.get("internal")) and binding_remove:
            valid_source_ids = {
                source_id
                for source_id in (
                    canonical_source_id,
                    *source_blueprint_ids,
                    *source_template_ids,
                )
                if source_id
            }
            obsolete_bindings = list((await db.execute(
                select(WorkflowBinding)
                .where(
                    WorkflowBinding.entity_id == workspace.entity_id,
                    WorkflowBinding.workspace_id == workspace.id,
                    WorkflowBinding.workflow_id == row.id,
                )
                .with_for_update()
            )).scalars().all())
            obsolete_bindings = [
                binding
                for binding in obsolete_bindings
                if (
                    dict(binding.config or {}).get("source") == "blueprint"
                    and dict(binding.config or {}).get("source_template_id")
                    in valid_source_ids
                    and str(
                        dict(binding.config or {}).get(
                            "workspace_blueprint_workflow_slug"
                        )
                        or ""
                    ).strip()
                    == slug
                )
            ]
            if not obsolete_bindings:
                raise BlueprintUpgradePlanChangedError(
                    "A Blueprint Flow binding changed during upgrade. Review and try again."
                )
            restore_entry["removed_bindings"] = [
                {
                    "id": binding.id,
                    "before": _row_snapshot(binding, WORKFLOW_BINDING_FIELDS),
                }
                for binding in obsolete_bindings
            ]
            for binding in obsolete_bindings:
                await db.delete(binding)
        restore_entry["after"] = _row_snapshot(row, patch)
        restore_entry["after_revision"] = new_revision
        if workflow_installation is not None:
            restore_entry["installation_after"] = _row_snapshot(
                workflow_installation, WORKFLOW_INSTALLATION_FIELDS,
            )
        updated.append({"kind": kind, "name": getattr(row, "name", slug), "changes": item["changes"]})

    if knowledge_created_document_ids:
        from packages.core.models.document import DocumentGroupMember

        # One starter may be referenced by multiple packs. Capture every link
        # created in this transaction, not just the first pack encountered.
        memberships = (await db.execute(select(
            DocumentGroupMember.document_id, DocumentGroupMember.group_id,
        ).where(DocumentGroupMember.document_id.in_(knowledge_created_document_ids)))).all()
        for entry in restored:
            if entry.get("kind") == "knowledge_document" and entry.get("delete_on_revert"):
                entry["created_group_ids"] = sorted(
                    group_id for document_id, group_id in memberships if document_id == entry["id"]
                )

    fully_synchronized = not bool(intended.get("unsupported_changes"))
    if fully_synchronized and isinstance(payload, dict):
        from packages.core.blueprints.installer import _bind_blueprint_channel_configs

        channel_changes = await _bind_blueprint_channel_configs(
            db,
            workspace=workspace,
            channel_requirements=list((payload.get("contract") or {}).get("channels") or []),
            selected_channel_config_ids=channel_config_ids or {},
            user_id=by_user_id,
        )
        restored.extend(channel_changes)
        updated.extend(
            {"kind": "channel", "name": entry["name"], "changes": ["channel binding"]}
            for entry in channel_changes
        )

    settings = dict(getattr(workspace, "settings", None) or {})
    record = dict(settings.get(BLUEPRINT_SETTINGS_KEY) or {})
    if applied_now:
        carried = record.get(APPLIED_REVISIONS_KEY)
        record[APPLIED_REVISIONS_KEY] = {
            **(carried if isinstance(carried, dict) else {}),
            **applied_now,
        }
    previous_personalization_present = PERSONALIZATION_SETTINGS_KEY in settings
    previous_personalization = copy.deepcopy(
        settings.get(PERSONALIZATION_SETTINGS_KEY)
    )
    previous_personalization_values = (
        previous_personalization
        if isinstance(previous_personalization, dict)
        else {}
    )
    personalization_changed = (
        personalization is not None
        and previous_personalization_values != personalization
    )
    previous_variable_declarations_present = VARIABLE_DECLARATIONS_KEY in record
    previous_variable_declarations = copy.deepcopy(
        record.get(VARIABLE_DECLARATIONS_KEY)
    )
    variable_declarations: list[dict[str, Any]] | None = None
    variable_declarations_changed = False
    if fully_synchronized and isinstance(source_payload, dict):
        variable_declarations = [
            dict(item)
            for item in (source_payload.get("contract") or {}).get("variables") or []
            if isinstance(item, dict)
        ]
        variable_declarations_changed = (
            (
                previous_variable_declarations
                if isinstance(previous_variable_declarations, list)
                else []
            )
            != variable_declarations
        )
    setup_before = {
        key: copy.deepcopy(record[key]) for key in LIVE_SETUP_STATE_KEYS if key in record
    }
    setup_after = setup_before
    setup_changed = False
    if fully_synchronized and isinstance(payload, dict):
        from packages.core.blueprints.installer import (
            sync_workspace_live_setup_requirements,
        )

        # Capture the actual setup delta before advancing provenance. A change
        # such as required -> optional has no Channel row edit but needs undo.
        settings[BLUEPRINT_SETTINGS_KEY] = record
        workspace.settings = settings
        await sync_workspace_live_setup_requirements(
            db,
            workspace=workspace,
            payload=payload,
        )
        settings = dict(workspace.settings or {})
        record = dict(settings.get(BLUEPRINT_SETTINGS_KEY) or {})
        setup_after = {
            key: copy.deepcopy(record[key]) for key in LIVE_SETUP_STATE_KEYS if key in record
        }
        # Initializing a legacy receipt (or merely backfilling empty fields)
        # must not invent an undo for an otherwise version-only confirmation.
        # Any existing setup contract, including an empty one, is reversible.
        empty_setup = {
            "live_setup_requirements": [], "install_todos": [], "blocking_todo_count": 0,
        }
        setup_changed = bool(setup_before) and (
            (empty_setup | setup_before) != (empty_setup | setup_after)
        )
    restore_settings = personalization_changed or variable_declarations_changed or setup_changed
    if updated or restore_settings:
        restore_point = {
            "upgraded_at": datetime.now(timezone.utc).isoformat(),
            "upgraded_by": by_user_id,
            "from_fingerprint": record.get(CONTENT_FINGERPRINT_KEY),
            "from_version": record.get(BLUEPRINT_VERSION_KEY),
            "from_section_fingerprints_present": SECTION_FINGERPRINTS_KEY in record,
            "from_section_fingerprints": copy.deepcopy(
                record.get(SECTION_FINGERPRINTS_KEY)
            ),
            "from_unsupported_fingerprint_present": (
                UPGRADE_UNSUPPORTED_FINGERPRINT_KEY in record
            ),
            "from_unsupported_fingerprint": record.get(
                UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
            ),
            "from_materialized_unsupported_fingerprint_present": (
                MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY in record
            ),
            "from_materialized_unsupported_fingerprint": record.get(
                MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY
            ),
            "items": restored,
        }
        if personalization_changed:
            restore_point["personalization_present"] = (
                previous_personalization_present
            )
            restore_point["personalization"] = previous_personalization
        if variable_declarations_changed:
            restore_point["variable_declarations_present"] = (
                previous_variable_declarations_present
            )
            restore_point["variable_declarations"] = (
                previous_variable_declarations
            )
        if fully_synchronized:
            restore_point["setup_before"] = setup_before
            restore_point["setup_after"] = setup_after
        record[RESTORE_POINT_KEY] = restore_point
    if intended.get("materialized_unsupported_baseline"):
        # A partial upgrade may save new personalization. Keep the original
        # materialized baseline so the next preview cannot reconstruct it from
        # those new values and silently mark untouched content synchronized.
        record[MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = intended[
            "materialized_unsupported_baseline"
        ]
    if fully_synchronized:
        # Only a fully reconciled portable contract may silence freshness.
        record[CONTENT_FINGERPRINT_KEY] = blueprint_content_fingerprint(
            fingerprint_payload
        )
        record[SECTION_FINGERPRINTS_KEY] = blueprint_section_fingerprints(
            fingerprint_payload
        )
        record[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = (
            blueprint_upgrade_unsupported_fingerprint(fingerprint_payload)
        )
        record[MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = (
            blueprint_upgrade_unsupported_fingerprint(payload)
        )
    if personalization is not None:
        if personalization:
            settings[PERSONALIZATION_SETTINGS_KEY] = dict(personalization)
        else:
            settings.pop(PERSONALIZATION_SETTINGS_KEY, None)
    if variable_declarations is not None:
        record[VARIABLE_DECLARATIONS_KEY] = variable_declarations
    if canonical_source_id:
        from packages.core.services.marketplace_resource_links import (
            RELATIONSHIP_INSTALLED_COMPONENT,
            RELATIONSHIP_INSTALLED_FROM,
            RESOURCE_AGENT,
            RESOURCE_SKILL,
            RESOURCE_WORKFLOW,
            RESOURCE_WORKSPACE,
            RESOURCE_WORKSPACE_BLUEPRINT,
            SCOPE_WORKSPACE,
            rekey_marketplace_resource_link,
        )

        record[BLUEPRINT_ID_KEY] = canonical_source_id
        await rekey_marketplace_resource_link(
            db,
            entity_id=workspace.entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=canonical_source_id,
            relationship=RELATIONSHIP_INSTALLED_FROM,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace.id,
            local_resource_type=RESOURCE_WORKSPACE,
            local_resource_id=workspace.id,
            marketplace_version=(
                current_version if fully_synchronized else record.get(BLUEPRINT_VERSION_KEY)
            ),
            linked_by=by_user_id,
            metadata={"source_slug": record.get("blueprint_slug")},
        )
        local_types = {
            "agent": RESOURCE_AGENT,
            "skill": RESOURCE_SKILL,
            "workflow": RESOURCE_WORKFLOW,
        }
        for item in intended.get("items") or []:
            local_type = local_types.get(str(item.get("kind") or ""))
            local_id = str(item.get("id") or "").strip()
            component_key = str(
                item.get("component_key") or item.get("slug") or ""
            ).strip()
            if not local_type or not local_id or not component_key:
                continue
            await rekey_marketplace_resource_link(
                db,
                entity_id=workspace.entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=canonical_source_id,
                relationship=RELATIONSHIP_INSTALLED_COMPONENT,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace.id,
                local_resource_type=local_type,
                local_resource_id=local_id,
                component_key=component_key,
                marketplace_version=(
                    current_version if fully_synchronized else record.get(BLUEPRINT_VERSION_KEY)
                ),
                linked_by=by_user_id,
                metadata={"source_slug": item.get("slug")},
            )
    if current_version and fully_synchronized:
        record[BLUEPRINT_VERSION_KEY] = current_version
    settings[BLUEPRINT_SETTINGS_KEY] = record
    workspace.settings = settings
    await db.flush()

    return {
        "workspace_id": workspace.id,
        "updated": updated,
        "kept_yours": [
            item for item in intended["items"]
            if item["action"] == UpgradeAction.KEEP_YOURS.value
            and (item["kind"], item["slug"]) not in explicit_conflict_updates
        ],
        "can_revert": bool(record.get(RESTORE_POINT_KEY)),
        "fully_synchronized": fully_synchronized,
    }


async def revert(
    db: AsyncSession,
    *,
    workspace,
    by_user_id: Optional[str] = None,
    actor: Any | None = None,
) -> dict[str, Any]:
    """Put back what the most recent apply() overwrote. Caller commits.

    The workspace goes back to being behind its blueprint, and the badge
    comes back — that is the truth after a revert, not a failure of it.
    """
    from packages.core.constants.agents import is_master_agent
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel, Document
    from packages.core.models.permission import Capability
    from packages.core.models.skill import Skill
    from packages.core.models.workflow import (
        WorkflowBinding,
        WorkflowDefinition,
        WorkflowRun,
        WorkflowTemplateInstallation,
    )
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.channel_bindings import (
        CHANNEL_BINDING_SNAPSHOT_FIELDS,
        preserve_channel_binding_route_order,
        snapshot_channel_binding,
    )

    await lock_reusable_resource_lifecycle(
        db,
        entity_id=workspace.entity_id,
    )

    locked_workspace = await db.get(
        Workspace,
        workspace.id,
        populate_existing=True,
        with_for_update=True,
    )
    if locked_workspace is None:
        raise BlueprintUpgradePlanChangedError(
            "The Workspace changed after upgrade. Refresh and try again."
        )
    workspace = locked_workspace
    settings = dict(getattr(workspace, "settings", None) or {})
    record = dict(settings.get(BLUEPRINT_SETTINGS_KEY) or {})
    point = record.get(RESTORE_POINT_KEY)
    has_settings_restore = isinstance(point, dict) and (
        "personalization_present" in point
        or "variable_declarations_present" in point
        or "setup_before" in point
    )
    if not isinstance(point, dict) or (
        not point.get("items") and not has_settings_restore
    ):
        return {"workspace_id": workspace.id, "reverted": [], "reason": "nothing to revert"}
    recorded_revisions = record.get(APPLIED_REVISIONS_KEY)
    recorded_revisions = (
        recorded_revisions if isinstance(recorded_revisions, dict) else {}
    )

    def changed() -> None:
        raise BlueprintUpgradePlanChangedError(
            "A Blueprint-upgraded item changed after upgrade. Review the Workspace before undoing."
        )

    def require_current(row: Any, entry: dict[str, Any]) -> None:
        expected_revision = entry.get("after_revision")
        if expected_revision is None:
            expected_revision = recorded_revisions.get(str(entry.get("id") or ""))
        if expected_revision is not None and int(
            getattr(row, "revision", PRISTINE_REVISION) or PRISTINE_REVISION
        ) != int(expected_revision):
            changed()
        if not _row_matches_snapshot(row, entry.get("after")):
            changed()

    if "setup_after" in point and point["setup_after"] != {
        key: record[key] for key in LIVE_SETUP_STATE_KEYS if key in record
    }:
        changed()

    # Match the attach path's account -> binding lock order. A transferred or
    # disabled credential source must not be resurrected by an old receipt.
    channel_accounts: dict[str, ChannelConfig] = {}
    channel_account_ids = set()
    for entry in point["items"]:
        if entry.get("kind") != "channel":
            continue
        after = entry.get("after")
        if not isinstance(after, dict) or not isinstance(after.get("config"), dict):
            changed()
        account_id = after["config"].get("channel_config_id")
        if not isinstance(account_id, str) or not account_id:
            changed()
        channel_account_ids.add(account_id)
    if channel_account_ids:
        accounts = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id.in_(sorted(channel_account_ids)),
                ChannelConfig.entity_id == workspace.entity_id,
            ).order_by(ChannelConfig.id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all()
        channel_accounts = {account.id: account for account in accounts}

    # Preflight every row and permission before changing any of them. This is
    # deliberately separate from the mutation pass: one denied or edited
    # component must not leave a half-reverted Workspace in direct callers.
    prepared: list[dict[str, Any]] = []
    for raw_entry in point["items"]:
        entry = dict(raw_entry or {})
        kind = str(entry.get("kind") or "")
        if kind == "channel":
            row = (await db.execute(
                select(Channel).where(
                    Channel.id == entry.get("id"),
                    Channel.entity_id == workspace.entity_id,
                    Channel.workspace_id == workspace.id,
                ).with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if row is None or not isinstance(entry.get("after"), dict):
                changed()
            after = entry["after"]
            if snapshot_channel_binding(row) != after:
                changed()
            account = channel_accounts.get(after["config"]["channel_config_id"])
            if (
                account is None or account.status != "active"
                or account.channel_type != row.type
                or account.owner_user_id != after.get("user_id")
                or account.workspace_id not in (None, workspace.id)
            ):
                changed()
            before = entry.get("before")
            if before is not None:
                if (
                    not isinstance(before, dict)
                    or set(before) != set(after)
                    or any(before[key] != after[key] for key in ("entity_id", "workspace_id", "type"))
                    or not isinstance(before.get("config"), dict)
                    or before["config"].get("channel_config_id") != account.id
                ):
                    changed()
                if before.get("status") == "active":
                    subscription_id = before.get("agent_subscription_id")
                    if subscription_id:
                        subscription = (await db.execute(
                            select(AgentSubscription).where(
                                AgentSubscription.id == subscription_id,
                                AgentSubscription.entity_id == workspace.entity_id,
                                AgentSubscription.workspace_id == workspace.id,
                                AgentSubscription.status == "active",
                                AgentSubscription.agent_id == before.get("agent_id"),
                            ).with_for_update().execution_options(populate_existing=True)
                        )).scalar_one_or_none()
                        service_key = before["config"].get("linked_service_key")
                        if subscription is None or (
                            service_key and subscription.service_key != service_key
                        ):
                            changed()
                    if before.get("agent_id") and not is_master_agent(before["agent_id"]):
                        agent = await db.get(
                            Agent, before["agent_id"], populate_existing=True, with_for_update=True,
                        )
                        if (
                            agent is None or agent.entity_id not in (None, workspace.entity_id)
                            or agent.status != "active" or agent.deleted_at is not None
                        ):
                            changed()
                    if row.type == "slack" or row.status != "active":
                        conflict = (await db.execute(
                            select(Channel.id).where(
                                Channel.entity_id == workspace.entity_id,
                                Channel.type == row.type,
                                Channel.status == "active",
                                Channel.config["channel_config_id"].astext == account.id,
                                Channel.id != row.id,
                            ).limit(1)
                        )).scalar_one_or_none()
                        if conflict is not None:
                            changed()
            prepared.append({"type": "channel", "entry": entry, "row": row, "account": account})
            continue
        is_installed_workflow = kind == "workflow" and (
            "installation_id" in entry
            or "binding_id" in entry
            or bool(entry.get("removed_bindings"))
            or bool(entry.get("delete_on_revert"))
        )
        if is_installed_workflow:
            # Old destructive restore points cannot prove that the row is
            # still the one created by the upgrade. Refuse the unsafe delete.
            if entry.get("delete_on_revert") and not isinstance(entry.get("after"), dict):
                changed()
            row = await db.get(
                WorkflowDefinition,
                entry.get("id"),
                populate_existing=True,
                with_for_update=True,
            )
            if row is None:
                changed()
            require_current(row, entry)
            if entry.get("delete_on_revert"):
                await _require_component_delete_access(
                    db,
                    row=row,
                    kind="workflow",
                    entity_id=workspace.entity_id,
                    actor=actor,
                )
            else:
                await _require_component_edit_access(
                    db,
                    row=row,
                    kind="workflow",
                    entity_id=workspace.entity_id,
                    actor=actor,
                )

            binding = None
            if entry.get("binding_id"):
                binding = await db.get(
                    WorkflowBinding,
                    entry.get("binding_id"),
                    populate_existing=True,
                    with_for_update=True,
                )
                if binding is None or not _row_matches_snapshot(
                    binding, entry.get("binding_after"),
                ):
                    changed()

            removed_bindings = []
            for removed in entry.get("removed_bindings") or []:
                removed_id = str((removed or {}).get("id") or "")
                removed_before = (removed or {}).get("before")
                if not removed_id or not isinstance(removed_before, dict):
                    changed()
                if await db.get(
                    WorkflowBinding,
                    removed_id,
                    populate_existing=True,
                    with_for_update=True,
                ) is not None:
                    changed()
                removed_bindings.append({
                    "id": removed_id,
                    "before": removed_before,
                })

            installation = None
            if entry.get("installation_id"):
                installation = await db.get(
                    WorkflowTemplateInstallation,
                    entry.get("installation_id"),
                    populate_existing=True,
                    with_for_update=True,
                )
                if installation is None or not _row_matches_snapshot(
                    installation, entry.get("installation_after"),
                ):
                    changed()
            prepared.append({
                "type": "installed_workflow",
                "entry": entry,
                "row": row,
                "binding": binding,
                "removed_bindings": removed_bindings,
                "installation": installation,
            })
            continue

        if kind == "knowledge_document":
            if not isinstance(entry.get("after"), dict):
                changed()
            row = await db.get(
                Document,
                entry.get("id"),
                populate_existing=True,
                with_for_update=True,
            )
            if row is None:
                if isinstance(entry.get("after"), dict):
                    changed()
                continue
            require_current(row, entry)
            if entry.get("delete_on_revert"):
                from packages.core.models.document import DocumentGroup, DocumentGroupMember

                # The receipt owns only the membership it created, never a
                # later share into another Knowledge group. Older receipts
                # without membership identity cannot authorize deletion.
                group_ids = entry.get("created_group_ids") or [
                    entry.get("remove_group_membership_on_revert"),
                ]
                if not all(isinstance(group_id, str) and group_id for group_id in group_ids):
                    changed()
                owned_groups = (await db.execute(select(DocumentGroup.id).where(
                    DocumentGroup.id.in_(group_ids),
                    DocumentGroup.entity_id == workspace.entity_id,
                    DocumentGroup.workspace_id == workspace.id,
                ).order_by(DocumentGroup.id).with_for_update())).scalars().all()
                if set(owned_groups) != set(group_ids):
                    changed()
                shared = (await db.execute(select(DocumentGroupMember.group_id).where(
                    DocumentGroupMember.document_id == row.id,
                    DocumentGroupMember.group_id.not_in(group_ids),
                ).limit(1))).scalar_one_or_none()
                # Document membership writers take this Document row lock too.
                # Preserve a shared row unchanged, removing only our own link.
                entry["delete_on_revert"] = shared is None
                entry["created_group_ids"] = group_ids
            await _require_document_access(
                db,
                row=row,
                actor=actor,
                capability=(
                    Capability.DELETE
                    if entry.get("delete_on_revert")
                    else Capability.MANAGE_METADATA
                ),
            )
            prepared.append({"type": "knowledge", "entry": entry, "row": row})
            continue

        model = {
            "skill": Skill,
            "workflow": WorkflowDefinition,
        }.get(kind, Agent)
        row = await db.get(
            model,
            entry.get("id"),
            populate_existing=True,
            with_for_update=True,
        )
        if row is None:
            if isinstance(entry.get("after"), dict):
                changed()
            logger.warning(
                "blueprint revert: %s %s is gone, skipping", kind, entry.get("id"),
            )
            continue
        require_current(row, entry)
        await _require_component_edit_access(
            db,
            row=row,
            kind=kind,
            entity_id=workspace.entity_id,
            actor=actor,
        )
        prepared.append({"type": "component", "entry": entry, "row": row})

    reverted: list[dict[str, Any]] = []
    applied_after_revert: dict[str, int] = {}
    for prepared_entry in prepared:
        entry = prepared_entry["entry"]
        row = prepared_entry["row"]
        if prepared_entry["type"] == "channel":
            before = entry.get("before")
            if row.type == "twilio_voice" and (
                before is None
                or any(
                    getattr(row, field) != before.get(field)
                    for field in (
                        "agent_id",
                        "agent_subscription_id",
                        "workspace_id",
                        "type",
                        "status",
                    )
                )
            ):
                from packages.core.services.voice.call_sessions import (
                    cancel_unconnected_call_sessions_for_binding,
                )

                await cancel_unconnected_call_sessions_for_binding(
                    db,
                    channel_config_id=prepared_entry["account"].id,
                    channel_binding_id=row.id,
                    reason=(
                        "Twilio Voice Blueprint binding was reverted before the "
                        "call connected."
                    ),
                )
            if entry.get("before") is None:
                # Same unbind operation as the channel API: retain the account
                # and its conversations/logs; remove only this routing row.
                await db.delete(row)
            else:
                for field in CHANNEL_BINDING_SNAPSHOT_FIELDS:
                    setattr(row, field, copy.deepcopy(before[field]))
                # Undo routing, never undo the repair of a legacy missing owner.
                row.user_id = prepared_entry["account"].owner_user_id
                preserve_channel_binding_route_order(row, before["updated_at"])
            reverted.append({"kind": "channel", "name": entry.get("name")})
            continue
        if prepared_entry["type"] == "installed_workflow":
            binding = prepared_entry["binding"]
            if binding is not None:
                if entry.get("binding_delete_on_revert"):
                    await db.delete(binding)
                else:
                    binding_before = entry.get("binding_before") or {}
                    binding_patch = {
                        field: value
                        for field, value in binding_before.items()
                        if getattr(binding, field, None) != value
                    }
                    for field, value in binding_patch.items():
                        setattr(binding, field, value)
                    if binding_patch:
                        await bump_revision(
                            db,
                            binding,
                            patch=binding_patch,
                            changed_by_kind="user" if by_user_id else "system",
                            changed_by_id=by_user_id,
                        )
            for removed in prepared_entry.get("removed_bindings") or []:
                db.add(WorkflowBinding(
                    id=removed["id"],
                    entity_id=workspace.entity_id,
                    **removed["before"],
                ))

            installation = prepared_entry["installation"]
            if installation is not None:
                if entry.get("installation_delete_on_revert"):
                    await db.delete(installation)
                else:
                    for field, value in (entry.get("installation_before") or {}).items():
                        setattr(installation, field, value)

            if entry.get("delete_on_revert"):
                has_runs = (await db.execute(
                    select(WorkflowRun.id).where(
                        WorkflowRun.workflow_id == row.id,
                    ).limit(1)
                )).scalar_one_or_none()
                if has_runs is None:
                    await db.delete(row)
                else:
                    row.is_active = False
                    row.status = "inactive"
            else:
                before = entry.get("before") or {}
                patch = content_patch_for(
                    row, before, WORKFLOW_CONTENT_REVISION_FIELDS,
                )
                for field, value in before.items():
                    setattr(row, field, value)
                if patch:
                    new_revision = await bump_revision(
                        db,
                        row,
                        patch=patch,
                        changed_by_kind="user" if by_user_id else "system",
                        changed_by_id=by_user_id,
                    )
                    applied_after_revert[str(row.id)] = new_revision
            reverted.append({
                "kind": entry.get("kind"),
                "name": entry.get("name"),
            })
            continue

        if prepared_entry["type"] == "knowledge":
            if entry.get("delete_on_revert"):
                from packages.core.models.document import DocumentGroupMember

                await db.execute(
                    delete(DocumentGroupMember).where(
                        DocumentGroupMember.document_id == row.id,
                        DocumentGroupMember.group_id.in_(entry["created_group_ids"]),
                    )
                )
                await db.delete(row)
            else:
                remove_group_ids = entry.get("created_group_ids") or [
                    entry.get("remove_group_membership_on_revert"),
                ]
                if any(remove_group_ids):
                    from packages.core.models.document import DocumentGroupMember

                    await db.execute(
                        delete(DocumentGroupMember).where(
                            DocumentGroupMember.document_id == row.id,
                            DocumentGroupMember.group_id.in_(remove_group_ids),
                        )
                    )
                before = entry.get("before") or {}
                if "metadata_" in before:
                    row.metadata_ = before["metadata_"]
            reverted.append({"kind": entry.get("kind"), "name": entry.get("name")})
            continue

        before = entry.get("before") or {}
        patch = content_patch_for(row, before, _fields_for(str(entry.get("kind"))))
        for field, value in before.items():
            setattr(row, field, value)
        if patch:
            new_revision = await bump_revision(
                db, row, patch=patch,
                changed_by_kind="user" if by_user_id else "system",
                changed_by_id=by_user_id,
            )
            # The revert is ours too — leave the row looking untouched so a
            # later upgrade can offer it again.
            applied_after_revert[str(row.id)] = new_revision
        reverted.append({"kind": entry.get("kind"), "name": entry.get("name")})

    # Back to the fingerprint AND version this workspace had before the
    # upgrade, so the update badge returns. A revert that left it looking
    # current would hide the very state the operator just chose. Restore
    # points from before versions were recorded carry no from_version — drop
    # the key entirely then, so freshness falls back to the fingerprint,
    # which the next line restores.
    record[CONTENT_FINGERPRINT_KEY] = point.get("from_fingerprint")
    if point.get("from_version") is not None:
        record[BLUEPRINT_VERSION_KEY] = point.get("from_version")
    else:
        record.pop(BLUEPRINT_VERSION_KEY, None)
    if applied_after_revert:
        carried = record.get(APPLIED_REVISIONS_KEY)
        record[APPLIED_REVISIONS_KEY] = {
            **(carried if isinstance(carried, dict) else {}),
            **applied_after_revert,
        }
    if point.get("from_section_fingerprints_present"):
        record[SECTION_FINGERPRINTS_KEY] = copy.deepcopy(
            point.get("from_section_fingerprints")
        )
    else:
        record.pop(SECTION_FINGERPRINTS_KEY, None)
    if point.get("from_unsupported_fingerprint_present"):
        record[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = point.get(
            "from_unsupported_fingerprint"
        )
    elif "from_unsupported_fingerprint_present" in point:
        record.pop(UPGRADE_UNSUPPORTED_FINGERPRINT_KEY, None)
    if point.get("from_materialized_unsupported_fingerprint_present"):
        record[MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] = point.get(
            "from_materialized_unsupported_fingerprint"
        )
    elif "from_materialized_unsupported_fingerprint_present" in point:
        record.pop(MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY, None)
    if "personalization_present" in point:
        if point.get("personalization_present"):
            settings[PERSONALIZATION_SETTINGS_KEY] = copy.deepcopy(
                point.get("personalization")
            )
        else:
            settings.pop(PERSONALIZATION_SETTINGS_KEY, None)
    if "variable_declarations_present" in point:
        if point.get("variable_declarations_present"):
            record[VARIABLE_DECLARATIONS_KEY] = copy.deepcopy(
                point.get("variable_declarations")
            )
        else:
            record.pop(VARIABLE_DECLARATIONS_KEY, None)
    if "setup_before" in point:
        for key in LIVE_SETUP_STATE_KEYS:
            if key in point["setup_before"]:
                record[key] = copy.deepcopy(point["setup_before"][key])
            else:
                record.pop(key, None)
    record.pop(RESTORE_POINT_KEY, None)
    settings[BLUEPRINT_SETTINGS_KEY] = record
    workspace.settings = settings
    await db.flush()

    return {"workspace_id": workspace.id, "reverted": reverted}
