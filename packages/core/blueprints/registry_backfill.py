"""Give existing Blueprints and installs their place in the release history.

Three steps, each idempotent (see scripts/backfill_blueprint_registry.py):

1. ``backfill_registry`` — every listing and client delivery gets its package;
   a published one (or a withdrawn platform one, whose payload is still its
   last release) also gets its current payload as a ``backfill`` release
   tagged ``latest``. Drafts get only the package.
2. ``recover_git_history`` — platform installs whose recorded fingerprint
   matches no release are matched against every committed version of the
   platform configs. Only an exactly equal fingerprint is adopted, as a
   hidden ``git_history`` release with its full payload.
3. ``adopt_workspace`` — writes ``_blueprint.package`` on an install that
   lacks it. Nothing else on the Workspace changes.
4. ``synthesize_workspace_baseline`` — an exact install without a v3 merge
   baseline gets one built from its release's payload, so its next upgrade
   updates what was left untouched and asks only about local edits. This
   changes that upgrade's preview, so it is opt-in.
"""
from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
import json
import logging
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any

from sqlalchemy import select

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    FIRST_CONTENT_VERSION,
    SECTION_FINGERPRINTS_KEY,
    blueprint_content_fingerprint,
    installed_blueprint_record,
)
from packages.core.blueprints.registry import (
    BASE_EXACT,
    BASE_UNVERIFIED,
    LATEST_TAG,
    ORIGIN_BACKFILL,
    ORIGIN_GIT_HISTORY,
    PACKAGE_RECORD_KEY,
    VISIBILITY_HIDDEN,
    BlueprintVersionConflictError,
    delivery_package_ref,
    ensure_package,
    package_record,
    package_spec_for_delivery,
    package_spec_for_listing,
    record_baseline_identity,
    record_release,
    release_for_fingerprint,
)
from packages.core.blueprints.seed import PLATFORM_BLUEPRINT_ID_PREFIX
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.blueprint_registry import (
    BlueprintDistTag,
    BlueprintPackage,
    BlueprintVersion,
)
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState
from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)

PLATFORM_CONFIG_DIR = "packages/core/blueprints/configs"


async def _record_backfill(db, ref, payload, version, *, spec, published_at, counts) -> None:
    if not isinstance(payload, dict) or not blueprint_content_fingerprint(payload):
        counts["skipped"] += 1
        return
    try:
        await record_release(
            db, ref, payload, str(version or FIRST_CONTENT_VERSION), spec=spec,
            origin=ORIGIN_BACKFILL, published_at=published_at,
        )
    except BlueprintVersionConflictError:
        logger.warning("blueprint backfill: %s@%s already holds other content", ref, version)
        counts["conflicts"] += 1
        return
    counts["versions"] += 1


async def backfill_registry(db) -> dict[str, int]:
    """Step 1. A ref that already has a package is skipped: every release since
    the registry existed went through ``record_release``. Caller commits."""
    counts = {"packages": 0, "versions": 0, "conflicts": 0, "skipped": 0}
    known = set((await db.execute(select(BlueprintPackage.ref))).scalars().all())

    listings = (await db.execute(select(
        WorkspaceBlueprint.id, WorkspaceBlueprint.entity_id, WorkspaceBlueprint.slug,
        WorkspaceBlueprint.author_handle, WorkspaceBlueprint.status,
        WorkspaceBlueprint.content_version, WorkspaceBlueprint.published_at,
    ).order_by(WorkspaceBlueprint.id))).all()
    for row in listings:
        if row.id in known:
            continue
        spec = package_spec_for_listing(row)
        counts["packages"] += 1
        # Seeding withdraws a retired platform listing without touching its
        # payload, so that payload is still its last release. A creator's
        # archived row may hold unreleased work instead.
        released = row.status == BlueprintStatus.PUBLISHED.value or (
            row.entity_id is None and row.status == BlueprintStatus.ARCHIVED.value
        )
        if not released:
            await ensure_package(db, spec)
            continue
        payload = (await db.execute(
            select(WorkspaceBlueprint.payload).where(WorkspaceBlueprint.id == row.id)
        )).scalar_one()
        await _record_backfill(
            db, row.id, payload, row.content_version, spec=spec,
            published_at=row.published_at, counts=counts,
        )

    deliveries = (await db.execute(select(
        ClientWorkspaceDelivery.id, ClientWorkspaceDelivery.entity_id,
        ClientWorkspaceDelivery.content_release, ClientWorkspaceDelivery.created_at,
    ).order_by(ClientWorkspaceDelivery.id))).all()
    for row in deliveries:
        ref = delivery_package_ref(row.id)
        if ref in known:
            continue
        counts["packages"] += 1
        release = row.content_release if isinstance(row.content_release, dict) else {}
        published_at = None
        if isinstance(release.get("published_at"), str):
            try:
                published_at = datetime.fromisoformat(release["published_at"])
            except ValueError:
                published_at = None
        payload = (await db.execute(
            select(ClientWorkspaceDelivery.payload).where(ClientWorkspaceDelivery.id == row.id)
        )).scalar_one()
        await _record_backfill(
            db, ref, payload, release.get("version") or FIRST_CONTENT_VERSION,
            spec=package_spec_for_delivery(row),
            published_at=published_at or row.created_at, counts=counts,
        )
    return counts


# ── Step 2: platform configs from git ───────────────────────────────────

def _normalize_platform_config(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply what today's catalogue applies when loading that config file."""
    if PurePosixPath(path).parent.name == "solo_company":
        from packages.core.blueprints.solo_company import normalize_payload

        return normalize_payload(payload)
    return payload


def platform_config_history(repo_root: Path) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """``(commit, committed_at, slug, payload)`` for every committed version of
    every platform config, newest first."""
    log = subprocess.run(
        ["git", "log", "--format=@%H %cI", "--name-only", "--diff-filter=AMR", "--", PLATFORM_CONFIG_DIR],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout
    commit = committed_at = ""
    for line in log.splitlines():
        if line.startswith("@"):
            commit, _, committed_at = line[1:].partition(" ")
            continue
        path = line.strip()
        if not path.endswith(".json"):
            continue
        shown = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=repo_root, capture_output=True, text=True,
        )
        if shown.returncode != 0:
            continue
        try:
            payload = _normalize_platform_config(path, json.loads(shown.stdout))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        manifest = payload.get("manifest") if isinstance(payload, dict) else None
        slug = str((manifest or {}).get("slug") or "").strip()
        if slug:
            yield commit, committed_at, slug, payload


async def _unmatched_platform_installs(db) -> tuple[dict[str, dict[str, str | None]], dict[str, str]]:
    """``ref -> {fingerprint: recorded version}`` for platform installs whose
    recorded content matches no release of their package, and the platform
    ref each slug names (its ``builtin:`` id, or the legacy row seeding
    adopted)."""
    platform_refs = dict((await db.execute(select(
        WorkspaceBlueprint.id, WorkspaceBlueprint.slug,
    ).where(WorkspaceBlueprint.entity_id.is_(None)))).all())
    records = (await db.execute(select(
        Workspace.settings[BLUEPRINT_SETTINGS_KEY],
    ).where(Workspace.deleted_at.is_(None)))).scalars().all()
    wanted: dict[str, dict[str, str | None]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("source") == "raw_payload":
            continue
        ref = str(record.get(BLUEPRINT_ID_KEY) or "")
        fingerprint = str(record.get(CONTENT_FINGERPRINT_KEY) or "")
        if ref in platform_refs and fingerprint:
            wanted.setdefault(ref, {}).setdefault(fingerprint, record.get(BLUEPRINT_VERSION_KEY))
    for ref, fingerprints in list(wanted.items()):
        known = set((await db.execute(select(BlueprintVersion.content_fingerprint).where(
            BlueprintVersion.package_ref == ref,
        ))).scalars().all())
        for fingerprint in known & set(fingerprints):
            fingerprints.pop(fingerprint)
        if not fingerprints:
            wanted.pop(ref)
    return wanted, {slug: ref for ref, slug in platform_refs.items()}


async def recover_git_history(db, repo_root: Path) -> dict[str, int]:
    """Step 2. Needs a git checkout with history. Caller commits."""
    counts = {"wanted": 0, "recovered": 0}
    wanted, ref_for_slug = await _unmatched_platform_installs(db)
    counts["wanted"] = sum(len(fingerprints) for fingerprints in wanted.values())
    if not wanted:
        return counts
    for _commit, committed_at, slug, payload in platform_config_history(repo_root):
        ref = ref_for_slug.get(slug) or f"{PLATFORM_BLUEPRINT_ID_PREFIX}{slug}"
        fingerprints = wanted.get(ref)
        if not fingerprints:
            continue
        fingerprint = blueprint_content_fingerprint(payload)
        if fingerprint not in fingerprints:
            continue
        recorded = fingerprints.pop(fingerprint)
        # A pre-release label sorts just before the version the install
        # recorded and can never collide with a listed release.
        await record_release(
            db, ref, payload, f"{recorded or '0.0.0'}-git.{fingerprint[:8]}",
            origin=ORIGIN_GIT_HISTORY, visibility=VISIBILITY_HIDDEN,
            published_at=datetime.fromisoformat(committed_at) if committed_at else None,
        )
        counts["recovered"] += 1
        if not any(wanted.values()):
            break
    return counts


# ── Step 3: adopt installs ──────────────────────────────────────────────

ADOPTED = "adopted"


async def adopt_workspace(db, workspace_id: str) -> str:
    """Step 3 for one Workspace; returns what happened. Caller commits.

    * recorded content equals a release → that release, ``base: exact``;
    * otherwise a v3 baseline exists → a hidden identity version, ``exact``;
    * otherwise → ``latest``, ``base: unverified`` (its first upgrade then
      treats every difference as a local change and asks);
    * a raw JSON install, or one with no Blueprint, is never adopted.
    """
    from packages.core.blueprints.reconfigure import _metadata

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if workspace is None or workspace.deleted_at is not None:
        return "missing"
    record = installed_blueprint_record(workspace.settings)
    ref = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    if not ref:
        return "not_from_blueprint"
    if record.get("source") == "raw_payload":
        return "raw_payload"
    if isinstance(record.get(PACKAGE_RECORD_KEY), dict):
        return "already_adopted"
    package = await db.get(BlueprintPackage, ref)
    if package is None:
        return "no_package"

    fingerprint = str(record.get(CONTENT_FINGERPRINT_KEY) or "").strip()
    recorded_version = str(record.get(BLUEPRINT_VERSION_KEY) or "") or None
    base = BASE_EXACT
    release = await release_for_fingerprint(db, ref, fingerprint, preferred_version=recorded_version)
    state = (await db.execute(select(WorkspaceBlueprintUpgradeState).where(
        WorkspaceBlueprintUpgradeState.workspace_id == workspace.id,
    ).with_for_update())).scalar_one_or_none()
    if release is None and fingerprint and state is not None and state.baseline:
        release = await record_baseline_identity(
            db, ref, installed_version=recorded_version, fingerprint=fingerprint,
            section_fingerprints=record.get(SECTION_FINGERPRINTS_KEY),
            manifest={"title": record.get("title"), "slug": record.get("blueprint_slug")},
        )
    if release is None:
        latest = (await db.execute(select(BlueprintDistTag.version).where(
            BlueprintDistTag.package_ref == ref, BlueprintDistTag.tag == LATEST_TAG,
        ))).scalar_one_or_none()
        release = (await db.execute(select(BlueprintVersion).where(
            BlueprintVersion.package_ref == ref, BlueprintVersion.version == latest,
        ))).scalar_one_or_none() if latest else None
        base = BASE_UNVERIFIED
    if release is None:
        return "no_release"

    metadata_before = _metadata(workspace)
    settings = deepcopy(workspace.settings or {})
    settings[BLUEPRINT_SETTINGS_KEY] = {
        **settings.get(BLUEPRINT_SETTINGS_KEY, {}),
        PACKAGE_RECORD_KEY: package_record(package, release, base=base),
    }
    workspace.settings = settings
    # The v3 undo refuses when the record changed after the upgrade. Adoption
    # is not such a change; keep a pending undo available.
    point = state.restore_point if state is not None else None
    if isinstance(point, dict) and point.get("metadata_after") == metadata_before:
        state.restore_point = {**point, "metadata_after": _metadata(workspace)}
    await db.flush()
    return ADOPTED if base == BASE_EXACT else f"{ADOPTED}_{BASE_UNVERIFIED}"


async def adoptable_workspace_ids(db) -> list[str]:
    """Workspaces with an install record and no package block yet."""
    rows = (await db.execute(select(
        Workspace.id, Workspace.settings[BLUEPRINT_SETTINGS_KEY],
    ).where(Workspace.deleted_at.is_(None)).order_by(Workspace.id))).all()
    return [
        workspace_id for workspace_id, record in rows
        if isinstance(record, dict) and record.get(BLUEPRINT_ID_KEY)
        and not isinstance(record.get(PACKAGE_RECORD_KEY), dict)
    ]


# ── Step 4: baselines for exact installs ────────────────────────────────

SYNTHESIZED = "synthesized"


def _installed_payload(workspace, payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` as the upgrade preview materializes it for this Workspace:
    only consumed variables, filled from the values saved at install."""
    from packages.core.blueprints.installer import resolve_install_variables
    from packages.core.blueprints.reconfigure import used_variable_payload

    usable = used_variable_payload(payload)
    declared = {
        str(item.get("key")) for item in (usable.get("contract") or {}).get("variables") or []
        if isinstance(item, dict) and item.get("key")
    }
    saved = (workspace.settings or {}).get("blueprint_personalization")
    values = {key: value for key, value in (saved if isinstance(saved, dict) else {}).items() if key in declared}
    materialized, _personalization = resolve_install_variables(usable, values)
    return materialized


async def synthesized_baseline(db, *, workspace, payload: dict[str, Any]) -> dict[str, Any]:
    """The v3 baseline an install of ``payload`` would have captured, built
    from what ``payload`` expected instead of what the Workspace holds now.

    A component's ``content`` and ``source`` are both its expected projection,
    so the unchanged merge sees an untouched component as ``update`` and an
    edited one as ``keep_yours``. A configuration entry's ``installed_content``
    is likewise its expected value: reading the live value would record every
    local edit as installed and let the next upgrade overwrite it. The runtime
    part stays unknown, as it is for any install without a baseline, since
    exports also read it.
    """
    from packages.core.blueprints.component_upgrade import capture_components
    from packages.core.blueprints.configuration_upgrade import capture_configuration

    components = await capture_components(db, workspace=workspace, payload=payload)
    for entry in components.values():
        # A linked component the release no longer declares keeps its live
        # content and no source, exactly as an install capture records it.
        if entry["source"] is not None:
            entry["content"] = deepcopy(entry["source"])
    # The capture resolves each entry's row identity; only its content is
    # replaced.
    configuration = await capture_configuration(db, workspace=workspace, payload=payload)
    for entry in configuration["entries"].values():
        entry["installed_content"] = deepcopy(entry["source_content"])
    return {"components": components, "configuration": configuration}


async def synthesize_workspace_baseline(db, workspace_id: str) -> str:
    """Step 4 for one Workspace; returns what happened. Caller commits.

    Only an install whose package block is ``base: exact`` and that has no
    v3 baseline gets one, built from that exact release's payload. Run it
    after adoption; nothing besides the baseline row changes.
    """
    from packages.core.blueprints.installer import InstallError

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if workspace is None or workspace.deleted_at is not None:
        return "missing"
    record = installed_blueprint_record(workspace.settings)
    package = record.get(PACKAGE_RECORD_KEY)
    if not isinstance(package, dict) or package.get("base") != BASE_EXACT:
        return "not_exact"
    state = (await db.execute(select(WorkspaceBlueprintUpgradeState).where(
        WorkspaceBlueprintUpgradeState.workspace_id == workspace.id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if state is not None and state.baseline:
        return "has_baseline"
    release = (await db.execute(select(BlueprintVersion).where(
        BlueprintVersion.package_ref == package.get("ref"),
        BlueprintVersion.version == package.get("version"),
    ))).scalar_one_or_none()
    # A baseline identity version carries no payload; its install already
    # had a baseline, so there is nothing to rebuild it from.
    if release is None or not isinstance(release.payload, dict):
        return "no_payload"
    if release.content_fingerprint != str(record.get(CONTENT_FINGERPRINT_KEY) or ""):
        return "fingerprint_mismatch"
    try:
        baseline = await synthesized_baseline(
            db, workspace=workspace, payload=_installed_payload(workspace, release.payload),
        )
    except (InstallError, ValueError, KeyError, TypeError):
        logger.warning("blueprint baseline synthesis failed for workspace %s", workspace.id, exc_info=True)
        return "unbuildable"
    if state is None:
        db.add(WorkspaceBlueprintUpgradeState(
            workspace_id=workspace.id, entity_id=workspace.entity_id, baseline=baseline,
        ))
    else:
        state.baseline = baseline
        state.revision += 1
    await db.flush()
    return SYNTHESIZED


async def synthesizable_workspace_ids(db) -> list[str]:
    """Exact installs with no v3 baseline."""
    rows = (await db.execute(select(
        Workspace.id, Workspace.settings[BLUEPRINT_SETTINGS_KEY],
    ).where(Workspace.deleted_at.is_(None)).order_by(Workspace.id))).all()
    exact = [
        workspace_id for workspace_id, record in rows
        if isinstance(record, dict) and isinstance(record.get(PACKAGE_RECORD_KEY), dict)
        and record[PACKAGE_RECORD_KEY].get("base") == BASE_EXACT
    ]
    if not exact:
        return []
    known = set((await db.execute(select(WorkspaceBlueprintUpgradeState.workspace_id).where(
        WorkspaceBlueprintUpgradeState.workspace_id.in_(exact),
        WorkspaceBlueprintUpgradeState.baseline != {},
    ))).scalars().all())
    return [workspace_id for workspace_id in exact if workspace_id not in known]
