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

Only the first is overwritten. The second is reported and left exactly as it
is; deciding that someone's edit is stale is not a decision code gets to
make.

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

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    installed_blueprint_record,
)
from packages.core.revisions import (
    AGENT_CONTENT_REVISION_FIELDS,
    SKILL_CONTENT_REVISION_FIELDS,
    bump_revision,
    content_patch_for,
)

logger = logging.getLogger(__name__)

#: Where apply() leaves what revert() needs.
RESTORE_POINT_KEY = "restore_point"

#: Where apply() records the revision each item ended on, so the next
#: upgrade can tell its own bump apart from an operator's edit.
APPLIED_REVISIONS_KEY = "applied_revisions"

#: A fresh install that never behaviourally changed.
PRISTINE_REVISION = 1


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

    #: The workspace edited it. Reported, never touched.
    KEEP_YOURS = "keep_yours"

    #: Already matches the blueprint.
    UNCHANGED = "unchanged"

    #: The blueprint names something this workspace does not have.
    MISSING = "missing"


def _fields_for(kind: str) -> frozenset[str]:
    return SKILL_CONTENT_REVISION_FIELDS if kind == "skill" else AGENT_CONTENT_REVISION_FIELDS


def _desired_content(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The blueprint's values for the fields an upgrade may touch.

    Only behaviour-affecting fields. A blueprint retitling its skill is not
    something to overwrite a workspace for.
    """
    return {
        field: spec.get(field)
        for field in _fields_for(kind)
        if spec.get(field) is not None
    }


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
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
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


async def plan(
    db: AsyncSession,
    *,
    workspace,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """What an upgrade would do. Reads only — this is what gets confirmed."""
    from packages.core.blueprints.installer import _find_installed_skill
    from packages.core.models.workspace import Agent

    record = installed_blueprint_record(getattr(workspace, "settings", None))
    applied = record.get(APPLIED_REVISIONS_KEY)
    applied = applied if isinstance(applied, dict) else {}
    result: dict[str, Any] = {
        "workspace_id": workspace.id,
        "workspace_name": getattr(workspace, "name", ""),
        "blueprint_slug": record.get("blueprint_slug"),
        "items": [],
        "can_revert": bool(record.get(RESTORE_POINT_KEY)),
    }
    if not isinstance(payload, dict) or not record.get(BLUEPRINT_ID_KEY):
        return result

    embedded = payload.get("embedded") or {}
    entity_id = workspace.entity_id

    for kind, specs in (("skill", embedded.get("skills") or []),
                        ("agent", embedded.get("agents") or [])):
        for spec in specs:
            slug = str(spec.get("slug") or "").strip()
            if not slug:
                continue

            if kind == "skill":
                row = await _find_installed_skill(db, entity_id=entity_id, slug=slug)
            else:
                row = (await db.execute(
                    select(Agent).where(Agent.entity_id == entity_id, Agent.slug == slug)
                )).scalar_one_or_none()

            label = spec.get("display_name") or spec.get("name") or slug
            if row is None:
                result["items"].append({
                    "kind": kind, "slug": slug, "name": label,
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
                "name": getattr(row, "name", label),
                "id": row.id,
                "action": action.value,
                "changes": _describe(kind, patch, row) if patch else [],
                "new_content": _preview(patch) if patch else {},
            })

    return result


async def apply(
    db: AsyncSession,
    *,
    workspace,
    payload: dict[str, Any] | None,
    by_user_id: Optional[str] = None,
    current_version: Optional[str] = None,
) -> dict[str, Any]:
    """Overwrite the items the plan marked UPDATE. Caller commits.

    The previous values are captured before anything is written, so revert()
    has something to put back. Items the workspace edited are not touched.
    """
    from packages.core.blueprints.installer import _find_installed_skill
    from packages.core.models.workspace import Agent

    intended = await plan(db, workspace=workspace, payload=payload)
    embedded = (payload or {}).get("embedded") or {}
    by_slug = {
        (kind, str(spec.get("slug") or "").strip()): spec
        for kind, specs in (("skill", embedded.get("skills") or []),
                            ("agent", embedded.get("agents") or []))
        for spec in specs
    }

    restored: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    applied_now: dict[str, int] = {}

    for item in intended["items"]:
        if item["action"] != UpgradeAction.UPDATE.value:
            continue
        kind, slug = item["kind"], item["slug"]
        spec = by_slug.get((kind, slug))
        if not spec:
            continue

        if kind == "skill":
            row = await _find_installed_skill(db, entity_id=workspace.entity_id, slug=slug)
        else:
            row = await db.get(Agent, item["id"])
        if row is None:
            continue

        patch = content_patch_for(row, _desired_content(kind, spec), _fields_for(kind))
        if not patch:
            continue

        # Capture before writing — only the fields being overwritten.
        restored.append({
            "kind": kind,
            "id": row.id,
            "name": getattr(row, "name", slug),
            "before": {
                field: (list(getattr(row, field)) if isinstance(getattr(row, field, None), (list, tuple))
                        else getattr(row, field, None))
                for field in patch
            },
        })
        for field, value in patch.items():
            setattr(row, field, value)
        new_revision = await bump_revision(
            db, row, patch=patch,
            changed_by_kind="user" if by_user_id else "system",
            changed_by_id=by_user_id,
        )
        applied_now[str(row.id)] = new_revision
        updated.append({"kind": kind, "name": getattr(row, "name", slug), "changes": item["changes"]})

    settings = dict(getattr(workspace, "settings", None) or {})
    record = dict(settings.get(BLUEPRINT_SETTINGS_KEY) or {})
    if applied_now:
        carried = record.get(APPLIED_REVISIONS_KEY)
        record[APPLIED_REVISIONS_KEY] = {
            **(carried if isinstance(carried, dict) else {}),
            **applied_now,
        }
    if updated:
        record[RESTORE_POINT_KEY] = {
            "upgraded_at": datetime.now(timezone.utc).isoformat(),
            "upgraded_by": by_user_id,
            "from_fingerprint": record.get(CONTENT_FINGERPRINT_KEY),
            "from_version": record.get(BLUEPRINT_VERSION_KEY),
            "items": restored,
        }
    # The workspace now matches the blueprint it was compared against, so the
    # badge must go quiet whether or not anything needed writing. Freshness
    # compares versions BEFORE fingerprints, so the version has to move too —
    # updating only the fingerprint left the badge on forever after a
    # successful apply.
    record[CONTENT_FINGERPRINT_KEY] = blueprint_content_fingerprint(payload)
    record[SECTION_FINGERPRINTS_KEY] = blueprint_section_fingerprints(payload)
    if current_version:
        record[BLUEPRINT_VERSION_KEY] = current_version
    settings[BLUEPRINT_SETTINGS_KEY] = record
    workspace.settings = settings
    await db.flush()

    return {
        "workspace_id": workspace.id,
        "updated": updated,
        "kept_yours": [i for i in intended["items"] if i["action"] == UpgradeAction.KEEP_YOURS.value],
        "can_revert": bool(updated),
    }


async def revert(
    db: AsyncSession,
    *,
    workspace,
    by_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Put back what the most recent apply() overwrote. Caller commits.

    The workspace goes back to being behind its blueprint, and the badge
    comes back — that is the truth after a revert, not a failure of it.
    """
    from packages.core.models.skill import Skill
    from packages.core.models.workspace import Agent

    settings = dict(getattr(workspace, "settings", None) or {})
    record = dict(settings.get(BLUEPRINT_SETTINGS_KEY) or {})
    point = record.get(RESTORE_POINT_KEY)
    if not isinstance(point, dict) or not point.get("items"):
        return {"workspace_id": workspace.id, "reverted": [], "reason": "nothing to revert"}

    reverted: list[dict[str, Any]] = []
    applied_after_revert: dict[str, int] = {}
    for entry in point["items"]:
        model = Skill if entry.get("kind") == "skill" else Agent
        row = await db.get(model, entry.get("id"))
        if row is None:
            logger.warning(
                "blueprint revert: %s %s is gone, skipping", entry.get("kind"), entry.get("id"),
            )
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
    record.pop(SECTION_FINGERPRINTS_KEY, None)
    record.pop(RESTORE_POINT_KEY, None)
    settings[BLUEPRINT_SETTINGS_KEY] = record
    workspace.settings = settings
    await db.flush()

    return {"workspace_id": workspace.id, "reverted": reverted}
