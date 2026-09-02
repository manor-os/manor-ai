"""Row locks that serialize reusable-resource bindings with hard purge."""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Agent


RESOURCE_AGENT = "agent"
RESOURCE_SKILL = "skill"
RESOURCE_WORKFLOW = "workflow"

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
logger = logging.getLogger(__name__)


class ReusableResourceUnavailableError(ValueError):
    """A deployment target disappeared while its binding was being written."""


def is_reusable_resource_local_id(value: Any) -> bool:
    """Return whether a value can name a locally owned reusable resource."""
    return bool(_ULID_RE.fullmatch(str(value or "").strip()))


def _entity_lifecycle_lock_key(entity_id: str) -> int:
    digest = hashlib.sha256(
        f"reusable-resource-lifecycle\0{entity_id}".encode()
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_reusable_resource_lifecycle(
    db: AsyncSession,
    *,
    entity_id: str,
) -> None:
    """Serialize resource-topology writes and purge within one entity."""
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(select(func.pg_advisory_xact_lock(
            _entity_lifecycle_lock_key(entity_id)
        )))


@asynccontextmanager
async def reusable_resource_lifecycle_lease(
    db: AsyncSession,
    *,
    entity_id: str,
) -> AsyncIterator[None]:
    """Hold the entity lifecycle fence only while a narrower row is locked.

    A session advisory lock on the caller's existing connection can be
    released as soon as it owns its ScheduledJob row.  The narrower row lock
    remains transaction-scoped, without doubling every scheduler worker's
    PostgreSQL connection usage.
    """
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is None or getattr(bind.dialect, "name", None) != "postgresql":
        yield
        return

    connection = await db.connection()
    acquired = False
    body_failed = False
    try:
        await connection.execute(select(func.pg_advisory_lock(
            _entity_lifecycle_lock_key(entity_id)
        )))
        acquired = True
        try:
            yield
        except BaseException:
            body_failed = True
            raise
    finally:
        if acquired:
            try:
                unlocked = (await connection.execute(
                    select(func.pg_advisory_unlock(
                        _entity_lifecycle_lock_key(entity_id)
                    ))
                )).scalar_one()
                if not unlocked:
                    raise RuntimeError("PostgreSQL lifecycle lease was not owned")
            except Exception:
                # A failed SQL statement may leave the transaction unusable.
                # Invalidating closes the physical connection and therefore
                # releases any session lock instead of poisoning the pool.
                logger.exception(
                    "Failed to release reusable-resource lifecycle lease "
                    "entity=%s",
                    entity_id,
                )
                await connection.invalidate()
                if not body_failed:
                    raise


async def lock_reusable_resource_reference(
    db: AsyncSession,
    *,
    entity_id: str,
    resource_type: str,
    resource_id: str,
) -> None:
    """Lock and revalidate a reusable resource before adding a reference.

    ``purge_workspace`` locks the same definition rows before deciding whether
    they are exclusive. Whichever transaction obtains the row first therefore
    wins: purge observes a committed binding, or the binding recheck observes
    that purge deleted the definition and fails without creating an orphan.
    """
    resource_type = str(resource_type or "").strip().lower()
    reference_groups = {
        RESOURCE_AGENT: {"agent_ids": (resource_id,)},
        RESOURCE_SKILL: {"skill_ids": (resource_id,)},
        RESOURCE_WORKFLOW: {"workflow_ids": (resource_id,)},
    }
    references = reference_groups.get(resource_type)
    if references is None:
        raise ValueError(f"Unsupported reusable resource type: {resource_type}")
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        **references,
    )


async def lock_reusable_resource_references(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_ids: Iterable[str] = (),
    skill_ids: Iterable[str] = (),
    workflow_ids: Iterable[str] = (),
) -> None:
    """Lock reusable definitions in the same global order as purge."""
    references = {
        RESOURCE_AGENT: sorted({str(value).strip() for value in agent_ids if value}),
        RESOURCE_SKILL: sorted({str(value).strip() for value in skill_ids if value}),
        RESOURCE_WORKFLOW: sorted({
            str(value).strip() for value in workflow_ids if value
        }),
    }
    if not any(references.values()):
        return

    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    models = {
        RESOURCE_AGENT: Agent,
        RESOURCE_SKILL: Skill,
        RESOURCE_WORKFLOW: WorkflowDefinition,
    }
    for resource_type in (RESOURCE_AGENT, RESOURCE_SKILL, RESOURCE_WORKFLOW):
        model = models[resource_type]
        for resource_id in references[resource_type]:
            statement = (
                select(model.id)
                .where(
                    model.id == resource_id,
                    or_(model.entity_id == entity_id, model.entity_id.is_(None)),
                )
                .with_for_update()
            )
            found = (await db.execute(statement)).scalar_one_or_none()
            if found is None:
                raise ReusableResourceUnavailableError(
                    f"{resource_type.title()} is no longer available"
                )


def reusable_resource_ids_from_payload(
    payload: Any,
) -> tuple[set[str], set[str], set[str]]:
    """Extract exact reusable-resource IDs from nested runtime configuration.

    Portable and historical payloads can carry slugs in fields that later
    became ``*_id`` fields. Local reusable-resource identities are ULIDs, so
    only ULID-shaped values participate in lifecycle locking; direct SQL
    columns remain strict at their service boundary.
    """
    from packages.core.constants.agents import is_master_agent

    agent_ids: set[str] = set()
    skill_ids: set[str] = set()
    workflow_ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            ref = str(item or "").strip() if not isinstance(item, (dict, list)) else ""
            if (
                key == "agent_id"
                and _ULID_RE.fullmatch(ref)
                and not is_master_agent(ref)
            ):
                agent_ids.add(ref)
            elif key == "skill_id" and _ULID_RE.fullmatch(ref):
                skill_ids.add(ref)
            elif key == "skill" and ref and _ULID_RE.fullmatch(ref):
                skill_ids.add(ref)
            elif key == "workflow_id" and _ULID_RE.fullmatch(ref):
                workflow_ids.add(ref)
            visit(item)

    visit(payload)
    return agent_ids, skill_ids, workflow_ids


def reusable_resource_reference_delta(
    before: Any,
    after: Any,
) -> tuple[set[str], set[str], set[str]]:
    """Return only reusable IDs newly introduced by a payload update."""
    before_groups = reusable_resource_ids_from_payload(before)
    after_groups = reusable_resource_ids_from_payload(after)
    return (
        after_groups[0] - before_groups[0],
        after_groups[1] - before_groups[1],
        after_groups[2] - before_groups[2],
    )


def reusable_skill_ids_from_runtime_state(state: Any) -> set[str]:
    """Extract local Skill ids from Workspace operation runtime state."""
    if not isinstance(state, dict):
        return set()
    bindings = state.get("skill_bindings")
    if not isinstance(bindings, list):
        return set()
    refs: set[str] = set()
    for binding in bindings:
        if isinstance(binding, dict):
            ref = str(binding.get("skill_id") or "").strip()
        else:
            ref = str(binding or "").strip()
        if _ULID_RE.fullmatch(ref):
            refs.add(ref)
    return refs


async def lock_reusable_resource_payload_references(
    db: AsyncSession,
    *,
    entity_id: str,
    payload: Any,
) -> None:
    """Lock exact reusable-resource IDs embedded in a JSON payload."""
    agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(payload)
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        workflow_ids=workflow_ids,
    )


async def lock_reusable_resource_payload_reference_delta(
    db: AsyncSession,
    *,
    entity_id: str,
    before: Any,
    after: Any,
) -> None:
    """Lock only IDs added by an update, preserving historical bad refs."""
    agent_ids, skill_ids, workflow_ids = reusable_resource_reference_delta(
        before,
        after,
    )
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        workflow_ids=workflow_ids,
    )


async def lock_agent_skill_binding_references(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str,
    skill_id: str,
) -> None:
    """Lock an Agent then Skill, matching purge's global lock order."""
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        agent_ids=(agent_id,),
        skill_ids=(skill_id,),
    )
