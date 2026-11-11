"""A team's own Blueprint packages: the file is the release, wherever it was built.

A ``.manor`` package (or its JSON payload) is self-contained configuration. The
same file installed on any Manor must behave the same, and a newer file must
reach the Workspaces installed from it through the ordinary upgrade (v3 plan,
apply, revert) instead of a fresh install.

Marketplace listings and client deliveries already have release histories. A
file a team installs itself had none: the install was recorded as
``raw_payload`` and could never be told about a newer version. Here a team
package gets one:

* Its identity is ``manifest.package.name``, carried in the file, so local,
  staging and production agree on which package a file is. Each team keeps its
  own history of it (``team-package:<entity>:<name>``).
* Installing a file records it as a release of that package and links the
  Workspace to it, exactly as a Marketplace install links to its listing.
* Publishing a later file to a Workspace records the next release (same
  validation and semver floor as every release); the Workspace then shows the
  update and takes it through its normal upgrade review.
* A Workspace installed before this (no package) adopts one: its components
  are linked to the package's components by exact install receipts or an
  unambiguous match inside that Workspace, and its current content becomes the
  merge base, so the first upgrade brings it to the package without touching
  anything else.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from sqlalchemy import select

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    FIRST_CONTENT_VERSION,
    blueprint_content_fingerprint,
    installed_blueprint_record,
)
from packages.core.blueprints.registry import (
    BlueprintVersionConflictError,
    PACKAGE_RECORD_KEY,
    PackageSpec,
    latest_release,
    next_release_version,
    record_release,
    release_for_fingerprint,
)

TEAM_PACKAGE_PREFIX = "team-package:"
PACKAGE_KIND_TEAM = "team"
#: ``workspace.settings._blueprint.source`` of an install linked to a team package.
SOURCE_TEAM_PACKAGE = "team_package"
#: Stored as the installed fingerprint by an adoption: the Workspace's content
#: is not any release yet, so it reads as behind until its first upgrade.
ADOPTED_FINGERPRINT = "adopted"

# Short enough that ``team-package:<entity>:<name>`` fits the 160-character ref.
_NAME = re.compile(r"^@[a-z0-9][a-z0-9._-]{0,39}/[a-z0-9][a-z0-9._-]{0,69}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


class TeamPackageError(ValueError):
    """The file cannot be a release of this Workspace's package."""


def _segment(value: Any) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return text.strip("-._")


def declared_package(payload: dict[str, Any]) -> dict[str, Any]:
    package = ((payload or {}).get("manifest") or {}).get("package")
    return package if isinstance(package, dict) else {}


def package_name(payload: dict[str, Any], *, scope: str | None = None) -> str:
    """The package a file declares, or the name its first export gives it."""
    declared = str(declared_package(payload).get("name") or "").strip().lower()
    if declared:
        if not _NAME.match(declared):
            raise TeamPackageError(
                f"manifest.package.name {declared!r} must look like @scope/name "
                "(lowercase letters, digits, dot, dash or underscore)"
            )
        return declared
    manifest = (payload or {}).get("manifest") or {}
    name = _segment(_segment(manifest.get("slug") or manifest.get("title"))[:70]) or "workspace"
    return f"@{_segment(_segment(scope)[:40]) or 'team'}/{name}"


async def package_scope(db, payload: dict[str, Any], entity_id: str) -> str:
    """The ``@scope`` a file without a declared package name is filed under:
    its author's handle (the same in every environment), else the team."""
    from packages.core.models.user import Entity

    author = ((payload or {}).get("manifest") or {}).get("author") or {}
    handle = str(author.get("handle") or "").strip() if isinstance(author, dict) else ""
    if handle:
        return handle
    entity = await db.get(Entity, entity_id)
    return str(getattr(entity, "name", "") or "team")


def stamp_package(payload: dict[str, Any], name: str, version: str | None = None) -> dict[str, Any]:
    """Write the package identity into the manifest (outside the fingerprint)."""
    manifest = payload.setdefault("manifest", {})
    manifest["package"] = {"name": name, **({"version": version} if version else {})}
    return payload


def team_package_ref(entity_id: str, name: str) -> str:
    return f"{TEAM_PACKAGE_PREFIX}{entity_id}:{name}"


def is_team_package_ref(value: Any) -> bool:
    return str(value or "").startswith(TEAM_PACKAGE_PREFIX)


def team_package_owner(ref: str) -> str:
    return ref.removeprefix(TEAM_PACKAGE_PREFIX).split(":", 1)[0]


async def release_team_package(
    db, *, entity_id: str, payload: dict[str, Any], scope: str | None = None,
    package_ref: str | None = None, changelog: str | None = None, published_by: str | None = None,
):
    """Record ``payload`` as a release of its team package; the existing
    release when this content was released before. Caller commits."""
    name = package_name(payload, scope=scope)
    ref = package_ref or team_package_ref(entity_id, name)
    declared = str(declared_package(payload).get("version") or "").strip() or None
    if declared and not _SEMVER.match(declared):
        raise TeamPackageError(f"manifest.package.version {declared!r} is not a version like 1.2.0")
    fingerprint = blueprint_content_fingerprint(payload)
    existing = await release_for_fingerprint(db, ref, fingerprint, preferred_version=declared)
    if existing is not None and (declared is None or existing.version == declared):
        return ref, existing
    previous = await latest_release(db, ref)
    version = declared or (
        await next_release_version(
            db, ref, payload, current_version=previous.version,
            stored_fingerprint=previous.content_fingerprint,
        ) if previous is not None else FIRST_CONTENT_VERSION
    )
    payload = stamp_package(deepcopy(payload), name, version)
    spec = PackageSpec(ref=ref, name=name, kind=PACKAGE_KIND_TEAM, owner_entity_id=entity_id)
    release = await record_release(
        db, ref, payload, version, spec=spec, changelog=changelog, published_by=published_by,
    )
    return ref, release


def check_release_file(current_payload: dict[str, Any] | None, payload: dict[str, Any], *, name: str) -> dict[str, Any]:
    """A file checked as the next release of package ``name``."""
    from packages.core.blueprints.payload import validate_payload
    from packages.core.blueprints.release_diff import _components

    if not isinstance(payload, dict):
        raise TeamPackageError("The file is not a Blueprint")
    payload = deepcopy(payload)
    validate_payload(payload)
    declared = str(declared_package(payload).get("name") or "").strip().lower()
    if declared and declared != name:
        raise TeamPackageError(f"The file is package {declared}; this Workspace runs {name}")
    current, uploaded = set(_components(current_payload or {})), set(_components(payload))
    if current and not current & uploaded:
        # Every component would be retired and replaced: almost certainly
        # another Workspace's file.
        raise TeamPackageError(
            "This file shares no Workflows, Skills or Agents with the Workspace's current version; "
            "it looks like a different Workspace."
        )
    return stamp_package(payload, name, str(declared_package(payload).get("version") or "").strip() or None)


# ── Adopting a package ──────────────────────────────────────────────────

async def _workspace_candidates(db, workspace) -> dict[str, list]:
    """The Agents, Skills and Flows this Workspace runs, by kind."""
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace import Agent, AgentSubscription

    agents = list((await db.execute(select(Agent).join(
        AgentSubscription, AgentSubscription.agent_id == Agent.id,
    ).where(
        AgentSubscription.workspace_id == workspace.id, Agent.entity_id == workspace.entity_id,
    ))).scalars().unique().all())
    agent_ids = [agent.id for agent in agents]
    skills = list((await db.execute(select(Skill).where(
        Skill.entity_id == workspace.entity_id, Skill.status == "active",
        (Skill.workspace_id == workspace.id) | Skill.id.in_(
            select(AgentSkillBinding.skill_id).where(
                AgentSkillBinding.agent_id.in_(agent_ids), AgentSkillBinding.status == "active",
            )
        ),
    ))).scalars().all()) if agent_ids else list((await db.execute(select(Skill).where(
        Skill.entity_id == workspace.entity_id, Skill.workspace_id == workspace.id, Skill.status == "active",
    ))).scalars().all())
    bound = select(WorkflowBinding.workflow_id).where(WorkflowBinding.workspace_id == workspace.id)
    flows = list((await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == workspace.entity_id,
        WorkflowDefinition.is_active.is_(True),
        (WorkflowDefinition.workspace_id == workspace.id) | WorkflowDefinition.id.in_(bound),
    ))).scalars().all())
    return {"agent": agents, "skill": skills, "workflow": flows}


async def _receipts(db, workspace, payload) -> dict[str, Any]:
    """Components an earlier install of this Workspace already recorded exactly."""
    from packages.core.blueprints.component_upgrade import _inventory

    try:
        return await _inventory(db, workspace=workspace, payload=payload)
    except Exception:  # noqa: BLE001 — an install without any receipts has none to offer
        return {}


async def match_components(db, *, workspace, payload: dict[str, Any]) -> dict[str, Any]:
    """Which local Agent, Skill or Flow each package component is.

    Exact install receipts first; otherwise the one row in this Workspace with
    the component's slug (Agents, Skills) or name (Flows). Two candidates is
    ambiguous and nothing is guessed: the component counts as missing, and the
    upgrade review shows it as one to install.
    """
    from packages.core.blueprints.component_upgrade import _specs

    specs = _specs(payload)
    receipts = await _receipts(db, workspace, payload)
    candidates = await _workspace_candidates(db, workspace)
    taken: set[str] = {row.id for row in receipts.values()}
    matched: dict[str, Any] = {}
    unmatched: dict[str, str] = {}
    for key, spec in sorted(specs.items()):
        kind = key.split(":", 1)[0]
        if key in receipts:
            matched[key] = receipts[key]
            continue
        if kind == "workflow":
            rows = [row for row in candidates[kind] if row.name == (spec.get("name") or spec["slug"])]
        else:
            rows = [row for row in candidates[kind] if row.slug == spec["slug"]]
        rows = [row for row in rows if row.id not in taken]
        if len(rows) == 1:
            matched[key] = rows[0]
            taken.add(rows[0].id)
        else:
            unmatched[key] = "ambiguous" if rows else "not_found"
    return {"matched": matched, "unmatched": unmatched, "specs": specs}


def describe_match(match: dict[str, Any]) -> dict[str, Any]:
    return {
        "matched": [
            {"component": key, "id": row.id, "name": getattr(row, "name", None)}
            for key, row in sorted(match["matched"].items())
        ],
        "unmatched": [
            {"component": key, "reason": reason} for key, reason in sorted(match["unmatched"].items())
        ],
    }


async def _claim_binding(db, *, workspace, row, slug: str) -> None:
    """Mark the Workspace's one active binding of an adopted Flow as the
    package's, the receipt the upgrade reads its binding from."""
    from packages.core.models.workflow import WorkflowBinding

    bindings = list((await db.execute(select(WorkflowBinding).where(
        WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id,
        WorkflowBinding.status == "active",
    ))).scalars().all())
    if len(bindings) != 1:
        return
    binding = bindings[0]
    config = dict(binding.config or {})
    if config.get("source") == "blueprint" and config.get("workspace_blueprint_workflow_slug") == slug:
        return
    binding.config = {**config, "source": "blueprint", "workspace_blueprint_workflow_slug": slug}


async def adopt_package(
    db, *, workspace, payload: dict[str, Any], package_ref: str, release, by_user_id: str | None,
) -> dict[str, Any]:
    """Link a Workspace with no package to ``release`` of ``package_ref``.

    Nothing the Workspace runs changes here. Its matched components are linked
    to the package's component keys and its current content becomes the merge
    base, so the next upgrade review shows exactly what the package changes.
    Caller commits.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.blueprints.component_upgrade import _link, capture_components
    from packages.core.blueprints.configuration_upgrade import capture_configuration
    from packages.core.blueprints.reconfigure import _state
    from packages.core.blueprints.upgrade import legacy_restore_point
    from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState

    match = await match_components(db, workspace=workspace, payload=payload)
    settings = dict(workspace.settings or {})
    record = dict(installed_blueprint_record(settings))
    manifest = payload.get("manifest") or {}
    record.update({
        BLUEPRINT_ID_KEY: package_ref,
        "blueprint_slug": manifest.get("slug") or record.get("blueprint_slug"),
        "source": SOURCE_TEAM_PACKAGE,
        BLUEPRINT_VERSION_KEY: None,
        CONTENT_FINGERPRINT_KEY: ADOPTED_FINGERPRINT,
        "adopted_package": {"ref": package_ref, "version": release.version},
    })
    record.pop(PACKAGE_RECORD_KEY, None)
    settings["_blueprint"] = record
    workspace.settings = settings
    flag_modified(workspace, "settings")
    await db.flush()

    for key, row in match["matched"].items():
        await _link(db, workspace=workspace, source_id=package_ref, key=key, row=row, by_user_id=by_user_id)
        if key.startswith("workflow:"):
            await _claim_binding(db, workspace=workspace, row=row, slug=match["specs"][key]["slug"])
    await db.flush()

    # The merge base is what the Workspace runs now: unchanged since "install",
    # so every difference the package brings reads as an update to apply.
    components = await capture_components(db, workspace=workspace, payload=payload)
    for entry in components.values():
        entry["source"] = deepcopy(entry["content"])
    configuration = await capture_configuration(db, workspace=workspace, payload=payload)
    for entry in (configuration.get("entries") or {}).values():
        entry["source_content"] = deepcopy(entry.get("installed_content"))
    runtime = deepcopy(record.get("upgrade_runtime_contract")) or {
        "channels": record.get("channel_requirements", []),
        "sessions": record.get("session_requirements", []),
        "live_setup_requirements": record.get("live_setup_requirements", []),
    }
    baseline = {
        "components": components,
        "configuration": configuration,
        "runtime": {"source": runtime, "installed": deepcopy(runtime)},
    }
    state = await _state(db, workspace, lock=True)
    if state is None:
        db.add(WorkspaceBlueprintUpgradeState(
            workspace_id=workspace.id, entity_id=workspace.entity_id, baseline=baseline,
        ))
    else:
        state.baseline = baseline
        # A new merge base voids a v3 receipt; the legacy undo does not use it.
        if not legacy_restore_point(state):
            state.restore_point = None
        state.revision += 1
    await db.flush()
    return describe_match(match)


__all__ = [
    "ADOPTED_FINGERPRINT",
    "BlueprintVersionConflictError",
    "PACKAGE_KIND_TEAM",
    "SOURCE_TEAM_PACKAGE",
    "TEAM_PACKAGE_PREFIX",
    "TeamPackageError",
    "adopt_package",
    "check_release_file",
    "declared_package",
    "describe_match",
    "is_team_package_ref",
    "match_components",
    "package_name",
    "package_scope",
    "release_team_package",
    "stamp_package",
    "team_package_owner",
    "team_package_ref",
]
