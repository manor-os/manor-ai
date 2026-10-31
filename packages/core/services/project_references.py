"""References a WorkflowProject holds for everything made inside it.

A project keeps the things every piece of its work must reuse (a narrator
voice, a character's model sheet, a key visual) under
``state.references.<kind>.<role>``. Lookup walks the project, its parent and
its ancestors (``state.parent_project_id``), so a series holds what all its
episodes share and an episode can still hold its own. A Blueprint declares
where a reference lives by giving that project an empty slot
(``{"references": {"voices": {"narrator": {}}}}``), or a whole kind when the
roles are not known in advance (``{"references": {"images": {}}}``: every
named image made inside it); without either, the outermost project holds it.

A voice is written once, on first use, and stays until someone replaces it on
purpose; a named image is replaced when it is made again under its name. Pinning does not move the project revision: the Workflow that
owns the project keeps patching it with the revision it read, and a patch
never clears a pinned reference (``preserve_pinned_references``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from packages.core.models.workflow import WorkflowProject


REFERENCES_KEY = "references"
PARENT_STATE_KEY = "parent_project_id"
MAX_PROJECT_DEPTH = 8


class ProjectReferenceError(LookupError):
    """The project, or a project in its parent chain, is not available."""


@dataclass(frozen=True)
class ProjectReference:
    """A pinned reference and where it lives.

    ``project_id`` is the project that holds it, ``requested_project_id`` the
    one the work runs in; they differ when the reference is inherited.
    """

    kind: str
    role: str
    value: dict[str, Any]
    project_id: str
    requested_project_id: str
    pinned_now: bool

    @property
    def inherited(self) -> bool:
        return self.project_id != self.requested_project_id

    def receipt(self) -> dict[str, Any]:
        return {
            "scope": "project",
            "kind": self.kind,
            "role": self.role,
            "project_id": self.project_id,
            "requested_project_id": self.requested_project_id,
            "inherited": self.inherited,
            "pinned_now": self.pinned_now,
        }


def _slots(state: Any, kind: str) -> dict[str, Any] | None:
    references = state.get(REFERENCES_KEY) if isinstance(state, dict) else None
    slots = references.get(kind) if isinstance(references, dict) else None
    return slots if isinstance(slots, dict) else None


def _pinned(value: Any) -> bool:
    return isinstance(value, dict) and bool(value)


def preserve_pinned_references(current: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep every reference ``current`` pins that ``incoming`` leaves empty or holds an older copy of.

    A Workflow patch rebuilds the whole state from what it read; a reference
    pinned or replaced in between must survive it. A patch may still set one.
    """
    current_refs = current.get(REFERENCES_KEY) if isinstance(current, dict) else None
    if not isinstance(current_refs, dict):
        return incoming
    merged = dict(incoming)
    incoming_refs = merged.get(REFERENCES_KEY)
    refs = dict(incoming_refs) if isinstance(incoming_refs, dict) else {}
    for kind, slots in current_refs.items():
        if not isinstance(slots, dict):
            continue
        kept = dict(refs.get(kind)) if isinstance(refs.get(kind), dict) else {}
        for role, value in slots.items():
            incoming_value = kept.get(role)
            # A reference made again after the caller read the state is newer.
            if _pinned(value) and (not _pinned(incoming_value) or str(value.get("pinned_at") or "")
                                   > str(incoming_value.get("pinned_at") or "")):
                kept[role] = value
        if kept:
            refs[kind] = kept
    if refs:
        merged[REFERENCES_KEY] = refs
    return merged


async def project_chain(
    db: Any, *, entity_id: str, workspace_id: str, project_id: str,
) -> list[WorkflowProject]:
    """The project, then its parent, up to the outermost ancestor."""
    chain: list[WorkflowProject] = []
    seen: set[str] = set()
    current = str(project_id or "").strip()
    while current and current not in seen and len(chain) < MAX_PROJECT_DEPTH:
        seen.add(current)
        row = (await db.execute(select(WorkflowProject).where(
            WorkflowProject.id == current,
            WorkflowProject.entity_id == entity_id,
            WorkflowProject.workspace_id == workspace_id,
        ))).scalar_one_or_none()
        if row is None:
            if not chain:
                raise ProjectReferenceError(f"Project {current} is not available in this Workspace.")
            break  # A parent outside this Workspace never holds its references.
        chain.append(row)
        current = str((row.state or {}).get(PARENT_STATE_KEY) or "").strip()
    return chain


def locate_reference(chain: list[WorkflowProject], kind: str, role: str) -> tuple[WorkflowProject, dict[str, Any] | None]:
    """The nearest project pinning the reference, else the one that should hold it.

    That is the nearest project declaring the role, then the nearest declaring
    the whole kind, then the outermost project.
    """
    for row in chain:
        value = (_slots(row.state, kind) or {}).get(role)
        if _pinned(value):
            return row, value
    for row in chain:
        if role in (_slots(row.state, kind) or {}):
            return row, None
    for row in chain:
        if _slots(row.state, kind) is not None:
            return row, None
    return chain[-1], None


def declares_reference(chain: list[WorkflowProject], kind: str, role: str) -> bool:
    """Whether any project in the chain holds or declares this reference, or its whole kind."""
    return any(_slots(row.state, kind) is not None for row in chain)


def held_references(chain: list[WorkflowProject], kind: str) -> dict[str, tuple[WorkflowProject, dict[str, Any]]]:
    """Every pinned reference of a kind visible from the chain's first project, nearest first."""
    found: dict[str, tuple[WorkflowProject, dict[str, Any]]] = {}
    for row in chain:
        for role, value in (_slots(row.state, kind) or {}).items():
            if role not in found and _pinned(value):
                found[role] = (row, value)
    return found


async def resolve_project_reference(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    project_id: str,
    kind: str,
    role: str,
    create: Callable[[dict[str, Any]], dict[str, Any]],
) -> ProjectReference:
    """Return the pinned reference, pinning ``create(sibling_slots)`` on first use.

    The holder row is locked while the slot is read and written, so two runs
    starting at once pin one value. ``create`` receives the holder's other
    references of this kind (e.g. voices other roles already use).
    """
    chain = await project_chain(db, entity_id=entity_id, workspace_id=workspace_id, project_id=project_id)
    holder, value = locate_reference(chain, kind, role)
    if value is not None:
        return ProjectReference(kind, role, value, holder.id, chain[0].id, False)
    locked = (await db.execute(
        select(WorkflowProject).where(WorkflowProject.id == holder.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    state = dict(locked.state or {})
    slots = dict(_slots(state, kind) or {})
    if _pinned(slots.get(role)):
        return ProjectReference(kind, role, slots[role], locked.id, chain[0].id, False)
    created = create({key: item for key, item in slots.items() if _pinned(item)})
    pinned = {**created, "pinned_at": datetime.now(UTC).isoformat()}
    references = dict(state.get(REFERENCES_KEY) or {})
    references[kind] = {**slots, role: pinned}
    state[REFERENCES_KEY] = references
    locked.state = state
    flag_modified(locked, "state")
    await db.commit()
    return ProjectReference(kind, role, pinned, locked.id, chain[0].id, True)


async def replace_project_reference(
    db: Any, *, entity_id: str, workspace_id: str, project_id: str, kind: str, role: str, value: dict[str, Any],
) -> ProjectReference:
    """Pin ``value`` where the reference lives, replacing what was there (made again on purpose)."""
    chain = await project_chain(db, entity_id=entity_id, workspace_id=workspace_id, project_id=project_id)
    holder, _current = locate_reference(chain, kind, role)
    locked = (await db.execute(
        select(WorkflowProject).where(WorkflowProject.id == holder.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    state = dict(locked.state or {})
    references = dict(state.get(REFERENCES_KEY) or {})
    pinned = {**value, "pinned_at": datetime.now(UTC).isoformat()}
    references[kind] = {**dict(_slots(state, kind) or {}), role: pinned}
    state[REFERENCES_KEY] = references
    locked.state = state
    flag_modified(locked, "state")
    await db.commit()
    return ProjectReference(kind, role, pinned, locked.id, chain[0].id, True)


async def read_project_reference(
    db: Any, *, entity_id: str, workspace_id: str, project_id: str, kind: str, role: str,
) -> ProjectReference | None:
    """The pinned reference, without pinning one."""
    chain = await project_chain(db, entity_id=entity_id, workspace_id=workspace_id, project_id=project_id)
    holder, value = locate_reference(chain, kind, role)
    if value is None:
        return None
    return ProjectReference(kind, role, value, holder.id, chain[0].id, False)
