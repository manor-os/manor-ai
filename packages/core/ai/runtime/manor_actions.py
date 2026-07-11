from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_injected_tool_context_args


_TASK_PRIORITY_LABELS = {
    "minimal": 1,
    "min": 1,
    "low": 2,
    "medium": 3,
    "normal": 3,
    "high": 4,
    "critical": 5,
    "urgent": 5,
}
_TASK_ASSIGNEE_ID_KEYS = ("assignee_id", "staff_id", "user_id")
_TASK_ASSIGNEE_EMAIL_KEYS = ("assignee_email", "staff_email", "user_email", "email")
_TASK_ASSIGNEE_NAME_KEYS = (
    "assignee_name",
    "staff_name",
    "user_name",
    "assigned_to",
    "assignee",
)
_TASK_AGENT_ID_KEYS = ("agent_id",)
_TASK_ASSIGNMENT_INPUT_KEYS = frozenset(
    (
        *_TASK_ASSIGNEE_ID_KEYS,
        *_TASK_ASSIGNEE_EMAIL_KEYS,
        *_TASK_ASSIGNEE_NAME_KEYS,
        *_TASK_AGENT_ID_KEYS,
    )
)
_TASK_SERVICE_ASSIGNMENT_KEYS = {"assignee_id", "agent_id"}
_TASK_ASSIGNMENT_ALIAS_KEYS = _TASK_ASSIGNMENT_INPUT_KEYS - _TASK_SERVICE_ASSIGNMENT_KEYS
_TASK_ASSIGNEE_REQUIRED_PARAMS = [
    "task_id",
    "assignee_id or staff_id or assignee_name or assignee_email or agent_id",
]
_TASK_UPDATE_ALLOWED_FIELDS = frozenset({
    "actual_output",
    "agent_id",
    "agent_type",
    "assignee_id",
    "category_id",
    "client_visible",
    "deadline",
    "delegate_service_keys",
    "description",
    "details",
    "estimated_hours",
    "expected_output",
    "input_contract",
    "owner_id",
    "owner_service_key",
    "owner_subscription_id",
    "parent_task_id",
    "priority",
    "required_skills",
    "sla_policy_id",
    "status",
    "task_type",
    "template_id",
    "title",
    "vendor_id",
    "visibility",
    "workspace_id",
})
_TASK_UPDATE_DETAIL_FIELDS = ("scheduled_at", "duration_minutes")
_TASK_UPDATE_CLEARABLE_FIELDS = {
    "actual_output",
    "agent_id",
    "agent_type",
    "assignee_id",
    "category_id",
    "deadline",
    "parent_task_id",
    "sla_policy_id",
    "template_id",
    "vendor_id",
}
_TASK_UPDATE_CLEAR_VALUES = {"", "clear", "none", "null", "unset"}
_TASK_UPDATE_FIELD_ALIASES = {
    "due": "deadline",
    "due_date": "deadline",
}
_TASK_CREATE_ALLOWED_FIELDS = frozenset({
    "agent_id",
    "agent_type",
    "assignee_id",
    "category_id",
    "conversation_id",
    "creator_id",
    "deadline",
    "description",
    "details",
    "duration_minutes",
    "priority",
    "scheduled_at",
    "task_type",
    "title",
    "workspace_id",
})
_WORKSPACE_UPDATE_ALLOWED_FIELDS = frozenset({
    "address",
    "attribute_tags",
    "auto_pause_on_budget",
    "category",
    "cover_image_url",
    "description",
    "heartbeat_cadence",
    "heartbeat_enabled",
    "identity_label",
    "kind",
    "latitude",
    "longitude",
    "monthly_budget_usd",
    "name",
    "occupancy_status",
    "operating_context",
    "operating_model",
    "pms_property_id",
    "pms_unit_id",
    "primary_work",
    "property_type",
    "settings",
    "status",
})
_CLIENT_FIELDS = frozenset({
    "address",
    "email",
    "meta",
    "name",
    "phone",
    "status",
})
_ORDER_CREATE_ALLOWED_FIELDS = frozenset({
    "amount",
    "assignee_id",
    "client_id",
    "currency",
    "description",
    "details",
    "due_date",
    "notes",
    "order_type",
    "title",
})
_ORDER_UPDATE_ALLOWED_FIELDS = frozenset({
    "amount",
    "assignee_id",
    "client_id",
    "completed_at",
    "currency",
    "description",
    "details",
    "due_date",
    "notes",
    "order_type",
    "paid_amount",
    "payment_status",
    "status",
    "title",
})
_NOTIFICATION_LIST_ALLOWED_FIELDS = frozenset({
    "limit",
    "offset",
    "unread_only",
})
_RUNTIME_MANOR_READ_CACHE_TTL_SECONDS = int(os.getenv("MANOR_TOOL_READ_CACHE_TTL_SECONDS", "900"))
_RUNTIME_MANOR_CACHE_NAMESPACE_BY_ACTION = {
    "list_documents": "documents",
    "search_documents": "documents",
    "list_workspace_artifacts": "documents",
    "list_document_folders": "documents",
    "list_document_groups": "documents",
    "list_staff": "staff",
    "list_integrations": "integrations",
    "list_ready_integrations": "integrations",
}


@dataclass(frozen=True)
class _TaskAssignmentInput:
    assignee_id: str = ""
    agent_id: str = ""
    email: str = ""
    name: str = ""

    @property
    def search_text(self) -> str:
        return self.email or self.name or self.assignee_id


def _normalize_task_priority(value: Any, default: int = 3) -> int:
    if isinstance(value, str):
        mapped = _TASK_PRIORITY_LABELS.get(value.strip().lower())
        if mapped is not None:
            return mapped
    try:
        return max(1, min(int(value), 5))
    except (TypeError, ValueError):
        return default


def _clean_param_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _category_lookup_to_task_type(value: Any) -> str | None:
    text = _clean_param_text(value)
    if not text:
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or None


def _first_text(params: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_param_text(params.get(key))
        if value:
            return value
    return ""


def _looks_like_local_id(value: str) -> bool:
    return len(value) == 26 and all(ch.isalnum() and ch.upper() == ch for ch in value)


def _runtime_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_bounded_int(value: Any, default: int, maximum: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _runtime_filter_params(
    params: Mapping[str, Any] | None,
    allowed_fields: frozenset[str],
    *,
    keep_empty: bool = False,
) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key, value in dict(params or {}).items():
        if key not in allowed_fields:
            continue
        if not keep_empty and value in (None, "", [], ()):
            continue
        filtered[key] = value
    return filtered


def _runtime_task_datetime_iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def _runtime_manor_read_cache_key(
    entity_id: str,
    action: str,
    params: Mapping[str, Any],
) -> str:
    from packages.core.services.tool_cache_version import get_tool_cache_version

    namespace = _RUNTIME_MANOR_CACHE_NAMESPACE_BY_ACTION.get(action, action)
    version = await get_tool_cache_version(entity_id, namespace)
    raw = json.dumps(params or {}, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"tool:manor:{entity_id}:v{version}:{action}:{digest}"


async def _runtime_manor_get_cached_read(
    entity_id: str,
    action: str,
    params: Mapping[str, Any],
) -> str | None:
    try:
        from packages.core.cache import cache

        cached = await cache.get(await _runtime_manor_read_cache_key(entity_id, action, params))
        return cached if isinstance(cached, str) else None
    except Exception:
        return None


async def _runtime_manor_set_cached_read(
    entity_id: str,
    action: str,
    params: Mapping[str, Any],
    result: str,
) -> None:
    if _RUNTIME_MANOR_READ_CACHE_TTL_SECONDS <= 0:
        return
    try:
        from packages.core.cache import cache

        await cache.set(
            await _runtime_manor_read_cache_key(entity_id, action, params),
            result,
            ttl=_RUNTIME_MANOR_READ_CACHE_TTL_SECONDS,
        )
    except Exception:
        return


async def _runtime_manor_invalidate_read_cache(entity_id: str, *actions: str) -> None:
    try:
        from packages.core.services.tool_cache_version import bump_tool_cache_version

        namespaces = {
            _RUNTIME_MANOR_CACHE_NAMESPACE_BY_ACTION.get(action, action)
            for action in actions
        }
        await bump_tool_cache_version(entity_id, *sorted(namespaces))
    except Exception:
        return


def _runtime_want_details(params: Mapping[str, Any]) -> bool:
    return str(params.get("detail") or params.get("details") or "summary").lower() in {
        "details", "detail", "full", "true", "1",
    }


def _runtime_staff_summary(
    staff: Any,
    *,
    details: bool = False,
    include_assignment: bool = False,
) -> dict[str, Any]:
    assignment_id = getattr(staff, "user_id", None) or staff.id
    data = {
        "id": staff.id,
        "name": staff.name,
        "email": staff.email,
        "kind": getattr(staff, "kind", None),
        "title": staff.title,
        "status": staff.status,
    }
    if include_assignment:
        data.update({
            "staff_id": staff.id,
            "user_id": getattr(staff, "user_id", None),
            "assignment_id": assignment_id,
        })
    if details:
        meta = getattr(staff, "meta", None) or {}
        data.update({
            "company_name": getattr(staff, "company_name", None),
            "role_id": staff.role_id,
            "department": meta.get("department"),
            "role": meta.get("role"),
        })
    return data


def _runtime_doc_summary(doc: Any, *, details: bool = False) -> dict[str, Any]:
    data = {
        "id": doc.id,
        "name": doc.name,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
    }
    if details:
        data.update({
            "mime_type": getattr(doc, "mime_type", None),
            "source": getattr(doc, "source", None),
            "vector_status": getattr(doc, "vector_status", None),
            "folder_id": getattr(doc, "folder_id", None),
            "fs_path": getattr(doc, "fs_path", None),
        })
    return data


def _runtime_folder_rel_path(folder: Any, folder_by_id: Mapping[str, Any]) -> str:
    parts: list[str] = []
    current = folder
    seen: set[str] = set()
    while current and getattr(current, "id", None) not in seen:
        seen.add(current.id)
        parts.append(current.name)
        current = folder_by_id.get(current.parent_id) if current.parent_id else None
    return "/".join(reversed(parts))


def _runtime_folder_summary(
    folder: Any,
    *,
    folder_by_id: Mapping[str, Any] | None = None,
    document_count: int = 0,
) -> dict[str, Any]:
    data = {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "document_count": document_count,
        "created_at": folder.created_at.isoformat() if getattr(folder, "created_at", None) else None,
    }
    if folder_by_id is not None:
        data["path"] = _runtime_folder_rel_path(folder, folder_by_id)
    return data


async def _runtime_load_document_folders(
    db: Any,
    entity_id: str,
) -> tuple[list[Any], dict[str, Any]]:
    from sqlalchemy import select

    from packages.core.models.document import DocumentFolder

    result = await db.execute(
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
    )
    folders = list(result.scalars().all())
    return folders, {folder.id: folder for folder in folders}


async def _runtime_validate_document_folder_position(
    db: Any,
    entity_id: str,
    *,
    name: str,
    parent_id: str | None = None,
    folder_id: str | None = None,
) -> tuple[str, list[Any], dict[str, Any]]:
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Folder name is required")
    if "/" in clean_name or "\\" in clean_name:
        raise ValueError("Folder name cannot contain path separators")

    folders, folder_by_id = await _runtime_load_document_folders(db, entity_id)
    if parent_id in ("", "root"):
        parent_id = None
    if parent_id and parent_id not in folder_by_id:
        raise LookupError("Parent folder not found")

    if folder_id:
        if parent_id == folder_id:
            raise ValueError("Cannot move a folder into itself")
        current = folder_by_id.get(parent_id) if parent_id else None
        while current:
            if current.id == folder_id:
                raise ValueError("Cannot move a folder into its own child")
            current = folder_by_id.get(current.parent_id) if current.parent_id else None

    parent = folder_by_id.get(parent_id) if parent_id else None
    parent_parts = _runtime_folder_rel_path(parent, folder_by_id).split("/") if parent else []
    candidate_path = "/".join([part for part in [*parent_parts, clean_name] if part])
    if not is_user_visible_folder_path(candidate_path):
        raise ValueError("Cannot use hidden/system folder path")

    duplicate = next(
        (
            folder for folder in folders
            if folder.parent_id == parent_id
            and folder.name == clean_name
            and folder.id != folder_id
        ),
        None,
    )
    if duplicate:
        raise FileExistsError("A folder with that name already exists here")
    return clean_name, folders, folder_by_id


def _runtime_split_document_folder_path(name: str) -> list[str]:
    return [
        part.strip()
        for part in str(name or "").replace("\\", "/").split("/")
        if part.strip()
    ]


async def _runtime_create_document_folder_path(
    db: Any,
    entity_id: str,
    *,
    name: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import DocumentFolder

    parts = _runtime_split_document_folder_path(name)
    if not parts:
        raise ValueError("Folder name is required")
    if parent_id in ("", "root"):
        parent_id = None

    created_folders: list[Any] = []
    current_parent_id = parent_id
    leaf = None
    folder_by_id: dict[str, Any] = {}

    for part in parts:
        folders, folder_by_id = await _runtime_load_document_folders(db, entity_id)
        existing = next(
            (
                folder for folder in folders
                if folder.parent_id == current_parent_id and folder.name == part
            ),
            None,
        )
        if existing:
            leaf = existing
            current_parent_id = existing.id
            continue

        clean_name, _folders, folder_by_id = await _runtime_validate_document_folder_position(
            db,
            entity_id,
            name=part,
            parent_id=current_parent_id,
        )
        folder = DocumentFolder(
            id=generate_ulid(),
            entity_id=entity_id,
            name=clean_name,
            parent_id=current_parent_id,
        )
        db.add(folder)
        await db.flush()
        created_folders.append(folder)
        leaf = folder
        current_parent_id = folder.id

    await db.commit()
    folders, folder_by_id = await _runtime_load_document_folders(db, entity_id)
    if leaf is None:
        raise ValueError("Folder name is required")
    leaf = folder_by_id.get(leaf.id, leaf)
    return {
        "created": bool(created_folders),
        "existing": not bool(created_folders),
        "folder": _runtime_folder_summary(leaf, folder_by_id=folder_by_id),
        "created_folders": [
            _runtime_folder_summary(folder_by_id.get(folder.id, folder), folder_by_id=folder_by_id)
            for folder in created_folders
        ],
    }


def _task_assignment_input(params: Mapping[str, Any]) -> _TaskAssignmentInput:
    assignee_id = _first_text(params, *_TASK_ASSIGNEE_ID_KEYS)
    agent_id = _first_text(params, *_TASK_AGENT_ID_KEYS)
    email = _first_text(params, *_TASK_ASSIGNEE_EMAIL_KEYS)
    name = _first_text(params, *_TASK_ASSIGNEE_NAME_KEYS)

    if assignee_id and not _looks_like_local_id(assignee_id):
        if "@" in assignee_id and not email:
            email = assignee_id
        elif not name:
            name = assignee_id

    return _TaskAssignmentInput(
        assignee_id=assignee_id,
        agent_id=agent_id,
        email=email,
        name=name,
    )


def _has_task_assignment_input(params: Mapping[str, Any]) -> bool:
    return any(_clean_param_text(params.get(key)) for key in _TASK_ASSIGNMENT_INPUT_KEYS)


async def _resolve_task_assignment(
    db: Any,
    entity_id: str,
    params: Mapping[str, Any],
    *,
    workspace_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    from sqlalchemy import and_, or_, select

    from packages.core.constants.agents import (
        MANOR_AGENT_ID,
        MANOR_AGENT_NAME,
        MANOR_AGENT_TYPE,
        is_master_agent,
    )
    from packages.core.models.staff import Staff
    from packages.core.models.user import User
    from packages.core.models.workspace import Agent, WorkspaceStaff

    lookup = _task_assignment_input(params)
    if not (lookup.agent_id or lookup.search_text):
        return {}, {}, None

    if lookup.agent_id and is_master_agent(lookup.agent_id, params.get("agent_type")):
        return (
            {"assignee_id": None, "agent_id": MANOR_AGENT_ID, "agent_type": MANOR_AGENT_TYPE},
            {"assignee_type": "manor_agent", "assignee_name": MANOR_AGENT_NAME},
            None,
        )

    if lookup.search_text and is_master_agent(lookup.search_text, params.get("agent_type")):
        return (
            {"assignee_id": None, "agent_id": MANOR_AGENT_ID, "agent_type": MANOR_AGENT_TYPE},
            {"assignee_type": "manor_agent", "assignee_name": MANOR_AGENT_NAME},
            None,
        )

    if lookup.agent_id:
        result = await db.execute(
            select(Agent).where(
                Agent.id == lookup.agent_id,
                Agent.deleted_at.is_(None),
                or_(
                    Agent.entity_id == entity_id,
                    and_(Agent.entity_id.is_(None), Agent.is_public.is_(True)),
                ),
            )
        )
        agent = result.scalar_one_or_none()
        if agent:
            return (
                {"assignee_id": None, "agent_id": agent.id, "agent_type": "agent"},
                {"assignee_type": "agent", "assignee_name": agent.name, "agent_id": agent.id},
                None,
            )
        return {}, {}, {"error": "agent_not_found", "agent_id": lookup.agent_id}

    workspace_staff_ids: set[str] = set()
    workspace_user_ids: set[str] = set()
    if workspace_id:
        rows = (await db.execute(
            select(WorkspaceStaff.staff_id, WorkspaceStaff.user_id).where(
                WorkspaceStaff.workspace_id == workspace_id,
                WorkspaceStaff.status == "active",
            )
        )).all()
        workspace_staff_ids = {row.staff_id for row in rows if row.staff_id}
        workspace_user_ids = {row.user_id for row in rows if row.user_id}

    async def _staff_candidates() -> list[Any]:
        clauses = []
        if lookup.assignee_id:
            clauses.extend([
                Staff.id == lookup.assignee_id,
                Staff.user_id == lookup.assignee_id,
            ])
        if lookup.email:
            clauses.append(Staff.email.ilike(lookup.email))
        if lookup.name:
            pattern = f"%{lookup.name}%"
            clauses.extend([Staff.name.ilike(pattern), Staff.email.ilike(pattern)])
        if not clauses:
            return []
        result = await db.execute(
            select(Staff).where(
                Staff.entity_id == entity_id,
                Staff.deleted_at.is_(None),
                or_(*clauses),
            )
        )
        candidates = list(result.scalars().all())
        if workspace_staff_ids or workspace_user_ids:
            candidates.sort(
                key=lambda s: 0
                if (s.id in workspace_staff_ids or getattr(s, "user_id", None) in workspace_user_ids)
                else 1
            )
        return candidates

    staff_candidates = await _staff_candidates()
    if staff_candidates:
        staff = staff_candidates[0]
        assignment_id = staff.user_id or staff.id
        return (
            {"assignee_id": assignment_id, "agent_id": None, "agent_type": None},
            {
                "assignee_type": "staff",
                "assignee_id": assignment_id,
                "staff_id": staff.id,
                "assignee_name": staff.name,
                "assignee_email": staff.email,
            },
            None,
        )

    user_clauses = []
    if lookup.assignee_id:
        user_clauses.extend([
            User.id == lookup.assignee_id,
            User.entity_id == lookup.assignee_id,
        ])
    if lookup.email:
        user_clauses.append(User.email.ilike(lookup.email))
    if lookup.name:
        pattern = f"%{lookup.name}%"
        user_clauses.extend([
            User.display_name.ilike(pattern),
            User.email.ilike(pattern),
        ])
    if user_clauses:
        result = await db.execute(
            select(User).where(
                User.entity_id == entity_id,
                User.deleted_at.is_(None),
                or_(*user_clauses),
            )
        )
        users = list(result.scalars().all())
        if workspace_user_ids:
            users.sort(key=lambda u: 0 if u.id in workspace_user_ids else 1)
        if users:
            user = users[0]
            return (
                {"assignee_id": user.id, "agent_id": None, "agent_type": None},
                {
                    "assignee_type": "user",
                    "assignee_id": user.id,
                    "assignee_name": user.display_name or user.email,
                    "assignee_email": user.email,
                },
                None,
            )

    if lookup.name:
        pattern = f"%{lookup.name}%"
        result = await db.execute(
            select(Agent).where(
                Agent.deleted_at.is_(None),
                or_(
                    Agent.entity_id == entity_id,
                    and_(Agent.entity_id.is_(None), Agent.is_public.is_(True)),
                ),
                or_(Agent.name.ilike(pattern), Agent.slug.ilike(pattern)),
            )
        )
        agent = result.scalars().first()
        if agent:
            return (
                {"assignee_id": None, "agent_id": agent.id, "agent_type": "agent"},
                {"assignee_type": "agent", "assignee_name": agent.name, "agent_id": agent.id},
                None,
            )

    if lookup.assignee_id and not lookup.email and not lookup.name:
        return (
            {"assignee_id": lookup.assignee_id, "agent_id": None, "agent_type": None},
            {"assignee_type": "unknown_id", "assignee_id": lookup.assignee_id},
            None,
        )

    return {}, {}, {
        "error": "assignee_not_found",
        "message": (
            "Could not resolve the assignee. Use list_staff/list_users/list_agents, "
            "then pass assignee_id, staff_id, user_id, agent_id, assignee_email, or assignee_name."
        ),
        "lookup": lookup.search_text,
    }


def _remove_task_assignment_aliases(params: dict[str, Any]) -> None:
    for key in _TASK_ASSIGNMENT_ALIAS_KEYS:
        params.pop(key, None)


def _is_task_update_clear_value(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TASK_UPDATE_CLEAR_VALUES


def _merge_task_details_for_update(existing_details: Any, detail_patch: Any) -> dict[str, Any]:
    merged = dict(existing_details or {})
    if detail_patch is None:
        return merged
    if not isinstance(detail_patch, dict):
        raise ValueError("details must be an object")
    merged.update(detail_patch)
    return merged


async def _resolve_task_category_update(
    db: Any,
    entity_id: str,
    params: dict[str, Any],
) -> tuple[bool, str | None, dict[str, Any] | None]:
    from sqlalchemy import func, select

    from packages.core.models.task import TaskCategory

    source_key = None
    raw_value = None
    for key in ("category_id", "category_name", "category"):
        if key in params:
            source_key = key
            raw_value = params.pop(key)
            break

    params.pop("category_name", None)
    params.pop("category", None)

    if source_key is None:
        return False, None, None

    value = _clean_param_text(raw_value)
    if not value or _is_task_update_clear_value(raw_value):
        return True, None, None

    category = None
    if _looks_like_local_id(value):
        category = (await db.execute(
            select(TaskCategory).where(
                TaskCategory.entity_id == entity_id,
                TaskCategory.id == value,
            )
        )).scalar_one_or_none()
    if category is None:
        category = (await db.execute(
            select(TaskCategory).where(
                TaskCategory.entity_id == entity_id,
                func.lower(TaskCategory.name) == value.lower(),
            ).order_by(TaskCategory.sort_order, TaskCategory.name)
        )).scalars().first()

    if not category:
        return True, None, {
            "error": "category_not_found",
            "message": "Could not resolve task category. Use list_task_categories, then pass category_id or category_name.",
            "lookup": value,
        }
    return True, category.id, None


async def _prepare_task_update_fields(
    db: Any,
    entity_id: str,
    params: Mapping[str, Any],
    existing_task: Any,
    *,
    workspace_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    update_params = dict(params)
    update_params.pop("task_id", None)

    for alias, canonical in _TASK_UPDATE_FIELD_ALIASES.items():
        if alias in update_params and canonical not in update_params:
            update_params[canonical] = update_params.pop(alias)

    category_present, category_id, category_error = await _resolve_task_category_update(
        db,
        entity_id,
        update_params,
    )
    if category_error:
        return {}, {}, category_error
    if category_present:
        update_params["category_id"] = category_id

    assignment_fields, assignment_info, assignment_error = await _resolve_task_assignment(
        db,
        entity_id,
        update_params,
        workspace_id=update_params.get("workspace_id") or existing_task.workspace_id or workspace_id,
    )
    if assignment_error and _has_task_assignment_input(update_params):
        return {}, {}, assignment_error
    if assignment_fields:
        update_params.update(assignment_fields)
    _remove_task_assignment_aliases(update_params)

    details_patch_present = "details" in update_params or any(
        key in update_params for key in _TASK_UPDATE_DETAIL_FIELDS
    )
    if details_patch_present:
        try:
            details = _merge_task_details_for_update(
                existing_task.details,
                update_params.pop("details", None),
            )
        except ValueError as exc:
            return {}, {}, {"error": "invalid_details", "message": str(exc)}
        for key in _TASK_UPDATE_DETAIL_FIELDS:
            if key in update_params:
                value = update_params.pop(key)
                details[key] = None if _is_task_update_clear_value(value) else value
        update_params["details"] = details

    if "priority" in update_params:
        update_params["priority"] = _normalize_task_priority(update_params.get("priority"))

    update_fields: dict[str, Any] = {}
    ignored_fields: list[str] = []
    for key, value in update_params.items():
        if key not in _TASK_UPDATE_ALLOWED_FIELDS:
            ignored_fields.append(key)
            continue
        if key in _TASK_UPDATE_CLEARABLE_FIELDS and _is_task_update_clear_value(value):
            value = None
        update_fields[key] = value

    if not update_fields:
        return {}, {}, {
            "error": "no_update_fields",
            "message": "No supported task fields were provided for update_task.",
            "supported_fields": sorted(
                _TASK_UPDATE_ALLOWED_FIELDS | {"category", "category_name", *_TASK_UPDATE_DETAIL_FIELDS},
            ),
            "ignored_fields": sorted(ignored_fields),
        }

    info: dict[str, Any] = {
        "updated_fields": sorted(update_fields.keys()),
    }
    if ignored_fields:
        info["ignored_fields"] = sorted(ignored_fields)
    if assignment_info:
        info.update(assignment_info)
    return update_fields, info, None


def runtime_manor_action_params_with_context(
    params: Mapping[str, Any] | None,
    *,
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Return Manor action params enriched with Runtime tool context."""

    next_params = dict(params or {})
    for key, value in runtime_injected_tool_context_args(
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
    ).items():
        if value not in (None, "", [], ()):
            next_params.setdefault(key, value)
    return next_params


async def runtime_manor_list_tasks(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List tasks via the Runtime Manor task boundary."""

    from packages.core.services.task_service import list_tasks

    raw_params = dict(params or {})
    assignee_email = (
        raw_params.pop("assignee_email", None)
        or raw_params.pop("staff_email", None)
    )
    if assignee_email and not raw_params.get("assignee_id"):
        from sqlalchemy import select

        from packages.core.models.staff import Staff

        staff = (await db.execute(
            select(Staff).where(
                Staff.entity_id == entity_id,
                Staff.email.ilike(str(assignee_email)),
                Staff.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not staff:
            return json.dumps({
                "total": 0,
                "tasks": [],
                "reason": "assignee email not found",
            }, default=str)
        raw_params["assignee_id"] = staff.id
    hours = raw_params.pop("completed_last_hours", None)
    if hours and not raw_params.get("completed_after"):
        raw_params["completed_after"] = (
            datetime.now(timezone.utc) - timedelta(hours=float(hours))
        ).isoformat()

    allowed_filters = {
        "status",
        "workspace_id",
        "assignee_id",
        "completed_after",
        "completed_before",
        "parent_task_id",
        "limit",
        "offset",
        "include_automations",
    }
    service_params = {
        key: value
        for key, value in raw_params.items()
        if key in allowed_filters and value not in (None, "", [], ())
    }

    tasks, total = await list_tasks(db, entity_id, **service_params)
    return json.dumps({
        "total": total,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "assignee_id": t.assignee_id,
                "workspace_id": t.workspace_id,
                "completed_at": _runtime_task_datetime_iso(t.completed_at),
                "created_at": _runtime_task_datetime_iso(t.created_at),
                "updated_at": _runtime_task_datetime_iso(t.updated_at),
                "description": (t.description or "")[:500],
            }
            for t in tasks
        ],
    }, default=str)


async def runtime_manor_get_task_details(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get task details via the Runtime Manor task boundary."""

    from packages.core.services.task_service import get_task

    task = await get_task(db, (params or {}).get("task_id", ""), entity_id)
    if not task:
        return json.dumps({"error": "Task not found"})
    return json.dumps({
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "description": task.description,
        "assignee_id": task.assignee_id,
        "workspace_id": task.workspace_id,
        "started_at": _runtime_task_datetime_iso(task.started_at),
        "completed_at": _runtime_task_datetime_iso(task.completed_at),
        "created_at": _runtime_task_datetime_iso(task.created_at),
        "updated_at": _runtime_task_datetime_iso(task.updated_at),
        "actual_output": task.actual_output,
    }, default=str)


async def runtime_manor_list_task_categories(db: Any, *, entity_id: str) -> str:
    """List task categories via the Runtime Manor task boundary."""

    from packages.core.services.task_service import list_categories

    categories = await list_categories(db, entity_id)
    return json.dumps({
        "total": len(categories),
        "categories": [
            {
                "id": category.id,
                "name": category.name,
                "icon": category.icon,
                "color": category.color,
                "sort_order": category.sort_order,
            }
            for category in categories
        ],
        "assignment_hint": "Pass category_id, category_name, or category to update_task/create_task.",
    }, default=str)


async def runtime_manor_create_task(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    workspace_id: str = "",
) -> str:
    """Create a task via the Runtime Manor task boundary."""

    from packages.core.services.task_service import create_task

    create_params = dict(params or {})
    if "priority" in create_params:
        create_params["priority"] = _normalize_task_priority(create_params.get("priority"))
    category_warning = None
    category_present, category_id, category_error = await _resolve_task_category_update(
        db,
        entity_id,
        create_params,
    )
    if category_error:
        category_warning = category_error
        if not create_params.get("task_type"):
            create_params["task_type"] = (
                _category_lookup_to_task_type(category_error.get("lookup"))
                or "general"
            )
    if category_present:
        create_params["category_id"] = category_id
    assignment_fields, assignment_info, assignment_error = await _resolve_task_assignment(
        db,
        entity_id,
        create_params,
        workspace_id=create_params.get("workspace_id") or workspace_id,
    )
    if assignment_error and _has_task_assignment_input(create_params):
        return json.dumps(assignment_error, default=str)
    if assignment_fields:
        create_params.update(assignment_fields)
    _remove_task_assignment_aliases(create_params)
    create_params = _runtime_filter_params(create_params, _TASK_CREATE_ALLOWED_FIELDS)
    task = await create_task(db, entity_id, **create_params)
    await db.commit()
    result = {
        "id": task.id,
        "title": task.title,
        "status": "created",
        "assignee_id": task.assignee_id,
        "agent_id": task.agent_id,
        "agent_type": task.agent_type,
        "assigned": bool(task.assignee_id or task.agent_id),
    }
    if category_warning:
        result["category_warning"] = category_warning
    result.update(assignment_info or {})
    return json.dumps(result, default=str)


async def runtime_manor_update_task(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    workspace_id: str = "",
    task_id: str | None = None,
) -> str:
    """Update a task via the Runtime Manor task boundary."""

    from packages.core.services.task_service import get_task, update_task
    from packages.core.services.task_state_machine import TaskStatusTransitionError

    raw_params = dict(params or {})
    target_task_id = raw_params.get("task_id") or task_id
    if not target_task_id:
        return json.dumps({
            "error": "task_id_required",
            "message": "update_task requires task_id plus one or more fields to update.",
            "required_params": ["task_id"],
        })
    existing_task = await get_task(db, target_task_id, entity_id)
    if not existing_task:
        return json.dumps({"error": "Task not found"})
    update_fields, update_info, update_error = await _prepare_task_update_fields(
        db,
        entity_id,
        raw_params,
        existing_task,
        workspace_id=workspace_id,
    )
    if update_error:
        return json.dumps(update_error, default=str)
    try:
        task = await update_task(
            db,
            target_task_id,
            entity_id,
            user_id=user_id,
            **update_fields,
        )
    except TaskStatusTransitionError as exc:
        return json.dumps({
            "error": "invalid_status_transition",
            "message": str(exc),
            "old_status": exc.old_status,
            "new_status": exc.new_status,
        }, default=str)
    await db.commit()
    return json.dumps({
        "updated": True,
        "id": task.id if task else None,
        "title": task.title if task else None,
        "status": task.status if task else None,
        "priority": task.priority if task else None,
        "category_id": task.category_id if task else None,
        "assignee_id": task.assignee_id if task else None,
        "agent_id": task.agent_id if task else None,
        **update_info,
    }, default=str)


async def runtime_manor_assign_task(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    workspace_id: str = "",
    task_id: str | None = None,
) -> str:
    """Assign a task via the Runtime Manor task boundary."""

    from packages.core.services.task_service import get_task, update_task

    raw_params = dict(params or {})
    target_task_id = raw_params.get("task_id") or task_id
    if not target_task_id:
        return json.dumps({
            "error": "task_id_required",
            "message": "assign_task requires task_id plus an assignee identifier.",
            "required_params": _TASK_ASSIGNEE_REQUIRED_PARAMS,
        })
    existing_task = await get_task(db, target_task_id, entity_id)
    if not existing_task:
        return json.dumps({"error": "Task not found"})
    assignment_fields, assignment_info, assignment_error = await _resolve_task_assignment(
        db,
        entity_id,
        raw_params,
        workspace_id=raw_params.get("workspace_id") or existing_task.workspace_id or workspace_id,
    )
    if assignment_error:
        return json.dumps(assignment_error, default=str)
    if not assignment_fields:
        return json.dumps({
            "error": "assignee_required",
            "message": (
                "assign_task requires an assignee. Use list_staff/list_users/list_agents, "
                "then pass assignee_id/staff_id/user_id/agent_id, or pass assignee_name/email."
            ),
            "required_params": _TASK_ASSIGNEE_REQUIRED_PARAMS,
        })
    task = await update_task(
        db,
        target_task_id,
        entity_id,
        **assignment_fields,
    )
    await db.commit()
    if not task:
        return json.dumps({"error": "Task not found"})
    return json.dumps({
        "id": task.id,
        "assignee_id": task.assignee_id,
        "agent_id": task.agent_id,
        "agent_type": task.agent_type,
        "assigned": True,
        **(assignment_info or {}),
    }, default=str)


async def runtime_manor_add_task_comment(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    actor_agent_id: str | None = None,
) -> str:
    """Add a task comment/log entry via the Runtime Manor task boundary."""

    from packages.core.services.task_service import add_task_log, agent_log_authorship

    raw_params = params or {}
    created_by, meta = await agent_log_authorship(
        db, actor_agent_id, fallback=raw_params.get("user_id"),
    )
    log = await add_task_log(
        db,
        raw_params.get("task_id", ""),
        raw_params.get("action_type", "comment"),
        raw_params.get("content") or raw_params.get("comment", ""),
        created_by=created_by,
        metadata=meta,
    )
    await db.commit()
    return json.dumps({"id": getattr(log, "id", None), "added": True}, default=str)


async def runtime_manor_list_documents(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List Knowledge documents via the Runtime Manor document boundary."""

    from packages.core.services.document_access import list_visible_documents

    raw_params = dict(params or {})
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    if "search" in raw_params:
        raw_params["name_search"] = raw_params.pop("search")
    if "folder" in raw_params and "folder_id" not in raw_params:
        folder = raw_params.pop("folder")
        if folder:
            raw_params["folder_id"] = folder
    raw_params["limit"] = _runtime_bounded_int(raw_params.get("limit"), 20, 50, 1)
    raw_params["offset"] = _runtime_bounded_int(raw_params.get("offset"), 0, 10_000, 0)
    include_details = _runtime_want_details(raw_params)
    raw_params["detail"] = "details" if include_details else "summary"
    raw_params["user_id"] = user_id
    raw_params["workspace_id"] = workspace_scope

    cached = await _runtime_manor_get_cached_read(entity_id, "list_documents", raw_params)
    if cached is not None:
        return cached

    service_params = {
        key: value
        for key, value in raw_params.items()
        if key not in {"detail", "details", "user_id", "workspace_id"}
    }
    docs, total = await list_visible_documents(
        db,
        entity_id,
        user_id=user_id,
        workspace_id=workspace_scope,
        actor_type="agent",
        **service_params,
    )
    result = json.dumps({
        "total": total,
        "count": len(docs),
        "limit": raw_params["limit"],
        "offset": raw_params["offset"],
        "next_offset": (
            raw_params["offset"] + len(docs)
            if raw_params["offset"] + len(docs) < total
            else None
        ),
        "has_more": raw_params["offset"] + len(docs) < total,
        "documents": [
            _runtime_doc_summary(doc, details=include_details)
            for doc in docs
        ],
    }, default=str)
    await _runtime_manor_set_cached_read(entity_id, "list_documents", raw_params, result)
    return result


async def runtime_manor_search_documents(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Search Knowledge documents via the Runtime Manor document boundary."""

    from packages.core.services.document_access import list_visible_documents

    raw_params = params or {}
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    query = (
        raw_params.get("query")
        or raw_params.get("search")
        or raw_params.get("name_search")
        or ""
    )
    cache_params = {
        "query": str(query),
        "limit": _runtime_bounded_int(raw_params.get("limit"), 20, 50, 1),
        "offset": _runtime_bounded_int(raw_params.get("offset"), 0, 10_000, 0),
        "detail": "details" if _runtime_want_details(raw_params) else "summary",
        "user_id": user_id,
        "workspace_id": workspace_scope,
    }
    cached = await _runtime_manor_get_cached_read(entity_id, "search_documents", cache_params)
    if cached is not None:
        return cached

    docs, total = await list_visible_documents(
        db,
        entity_id,
        user_id=user_id,
        workspace_id=workspace_scope,
        actor_type="agent",
        name_search=cache_params["query"],
        limit=cache_params["limit"],
        offset=cache_params["offset"],
    )
    result = json.dumps({
        "total": total,
        "count": len(docs),
        "next_offset": (
            cache_params["offset"] + len(docs)
            if cache_params["offset"] + len(docs) < total
            else None
        ),
        "has_more": cache_params["offset"] + len(docs) < total,
        "documents": [
            _runtime_doc_summary(doc, details=cache_params["detail"] == "details")
            for doc in docs
        ],
    }, default=str)
    await _runtime_manor_set_cached_read(entity_id, "search_documents", cache_params, result)
    return result


async def runtime_manor_list_workspace_artifacts(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    workspace_id: str = "",
    task_id: str = "",
) -> str:
    """List workspace artifact documents via the Runtime Manor document boundary."""

    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.services.document_metadata import metadata_artifact, metadata_origin
    from packages.core.services.document_access import user_can_read_document

    raw_params = params or {}
    limit = _runtime_bounded_int(raw_params.get("limit"), 20, 50, 1)
    task_id_filter = str(raw_params.get("task_id") or raw_params.get("taskId") or task_id or "").strip()
    workspace_id_value = str(
        raw_params.get("workspace_id")
        or raw_params.get("workspaceId")
        or raw_params.get("workspace")
        or workspace_id
        or ""
    ).strip()
    if not workspace_id_value and task_id_filter:
        task = (
            await db.execute(
                select(Task).where(
                    Task.entity_id == entity_id,
                    Task.id == task_id_filter,
                )
            )
        ).scalar_one_or_none()
        workspace_id_value = str(getattr(task, "workspace_id", None) or "").strip()
    if not workspace_id_value:
        workspaces = (
            await db.execute(
                select(Workspace)
                .where(
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
                .order_by(Workspace.created_at.desc())
                .limit(6)
            )
        ).scalars().all()
        active_workspaces = [
            ws for ws in workspaces
            if getattr(ws, "status", None) == "active"
        ]
        candidates = active_workspaces or workspaces
        if len(candidates) == 1:
            workspace_id_value = str(candidates[0].id)
        else:
            return json.dumps({
                "error": "workspace_id is required",
                "message": (
                    "Multiple workspaces are available. Call again with workspace_id, "
                    "or run this from a workspace chat."
                ),
                "workspaces": [
                    {
                        "id": ws.id,
                        "name": ws.name,
                        "status": ws.status,
                    }
                    for ws in candidates[:5]
                ],
            }, ensure_ascii=False)

    cache_params = {
        "workspace_id": workspace_id_value,
        "task_id": task_id_filter,
        "limit": limit,
        "user_id": user_id,
    }
    cached = await _runtime_manor_get_cached_read(
        entity_id,
        "list_workspace_artifacts",
        cache_params,
    )
    if cached is not None:
        return cached

    stmt = (
        select(Document)
        .where(
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
            Document.metadata_["origin"]["workspace_id"].astext == workspace_id_value,
        )
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    if task_id_filter:
        stmt = stmt.where(Document.metadata_["origin"]["task_id"].astext == task_id_filter)
    docs = (await db.execute(stmt)).scalars().all()
    artifacts = []
    for doc in docs:
        if not await user_can_read_document(
            db,
            doc,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id_value,
            actor_type="agent",
        ):
            continue
        meta = doc.metadata_ or {}
        origin = metadata_origin(meta)
        artifact = metadata_artifact(meta)
        artifacts.append({
            "id": doc.id,
            "name": doc.name,
            "source": doc.source,
            "file_type": doc.file_type,
            "mime_type": doc.mime_type,
            "file_size": doc.file_size,
            "fs_path": doc.fs_path,
            "task_id": origin.get("task_id"),
            "artifact_role": artifact.get("role"),
            "agent_id": origin.get("agent_id"),
            "conversation_id": origin.get("conversation_id"),
            "tool_name": origin.get("tool_name"),
            "created_at": doc.created_at.isoformat() if getattr(doc, "created_at", None) else None,
        })
    result = json.dumps({
        "workspace_id": workspace_id_value,
        "count": len(artifacts),
        "artifacts": artifacts,
    }, default=str)
    await _runtime_manor_set_cached_read(
        entity_id,
        "list_workspace_artifacts",
        cache_params,
        result,
    )
    return result


async def runtime_manor_get_document(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get document details via the Runtime Manor document boundary."""

    from packages.core.services.document_access import get_visible_document

    raw_params = params or {}
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    doc = await get_visible_document(
        db,
        raw_params.get("document_id") or raw_params.get("doc_id") or raw_params.get("id") or "",
        entity_id,
        user_id=user_id,
        workspace_id=workspace_scope,
        actor_type="agent",
    )
    if not doc:
        return json.dumps({"error": "Document not found"})
    return json.dumps({"document": _runtime_doc_summary(doc, details=True)}, default=str)


async def runtime_manor_list_document_folders(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List Knowledge folders via the Runtime Manor document boundary."""

    from sqlalchemy import func as sqfunc, select

    from packages.core.models.document import Document
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    raw_params = params or {}
    parent_id = raw_params.get("parent_id")
    if parent_id in ("", "root"):
        parent_id = None
    cache_params = {"parent_id": parent_id}
    cached = await _runtime_manor_get_cached_read(
        entity_id,
        "list_document_folders",
        cache_params,
    )
    if cached is not None:
        return cached

    folders, folder_by_id = await _runtime_load_document_folders(db, entity_id)
    all_visible_folders = [
        folder for folder in folders
        if is_user_visible_folder_path(_runtime_folder_rel_path(folder, folder_by_id))
    ]
    visible_folders = all_visible_folders
    if "parent_id" in raw_params:
        visible_folders = [
            folder for folder in visible_folders
            if folder.parent_id == parent_id
        ]
    visible_folder_ids = {folder.id for folder in all_visible_folders}
    if visible_folder_ids:
        count_rows = (await db.execute(
            select(Document.folder_id, sqfunc.count())
            .where(
                Document.entity_id == entity_id,
                Document.folder_id.in_(visible_folder_ids),
                Document.is_trashed == False,  # noqa: E712
            )
            .group_by(Document.folder_id)
        )).all()
    else:
        count_rows = []
    direct_counts = {row[0]: int(row[1] or 0) for row in count_rows}
    child_ids_by_parent: dict[str | None, list[str]] = {}
    for folder in all_visible_folders:
        child_ids_by_parent.setdefault(folder.parent_id, []).append(folder.id)
    count_cache: dict[str, int] = {}

    def recursive_document_count(folder_id: str, seen: set[str] | None = None) -> int:
        if folder_id in count_cache:
            return count_cache[folder_id]
        if seen is None:
            seen = set()
        if folder_id in seen:
            return direct_counts.get(folder_id, 0)
        seen.add(folder_id)
        total = direct_counts.get(folder_id, 0)
        for child_id in child_ids_by_parent.get(folder_id, []):
            total += recursive_document_count(child_id, seen.copy())
        count_cache[folder_id] = total
        return total

    visible_folders.sort(key=lambda folder: getattr(folder, "created_at", None), reverse=True)
    result = json.dumps({
        "count": len(visible_folders),
        "folders": [
            _runtime_folder_summary(
                folder,
                folder_by_id=folder_by_id,
                document_count=recursive_document_count(folder.id),
            )
            for folder in visible_folders
        ],
    }, default=str)
    await _runtime_manor_set_cached_read(
        entity_id,
        "list_document_folders",
        cache_params,
        result,
    )
    return result


async def runtime_manor_list_document_groups(db: Any, *, entity_id: str) -> str:
    """List document groups via the Runtime Manor document boundary."""

    from packages.core.services.document_service import list_groups

    groups = await list_groups(db, entity_id)
    return json.dumps({
        "count": len(groups),
        "groups": [
            {
                "id": group.id,
                "name": group.name,
                "workspace_id": group.workspace_id,
            }
            for group in groups
        ],
    }, default=str)


async def runtime_manor_upload_document(
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    workspace_id: str = "",
    conversation_id: str | None = None,
    task_id: str | None = None,
    approval_token: str | None = None,
) -> str:
    """Upload a file into Knowledge via the Runtime Manor document boundary."""

    import shutil

    from packages.core.config import get_settings
    from packages.core.services.ai_file_permissions import guard_ai_file_mutation
    from packages.core.services.knowledge_sync import sync_file_to_knowledge
    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    settings = get_settings()
    raw_params = params or {}
    file_path = raw_params.get("file_path") or raw_params.get("path", "")
    name = normalize_rel_path(raw_params.get("name") or os.path.basename(file_path))
    if not file_path:
        return json.dumps({"error": "file_path is required"})
    if not is_user_visible_path(name):
        return json.dumps({"error": "Cannot upload hidden/system document path"})

    blocked = await guard_ai_file_mutation(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        tool_name="manor",
        action="upload_document",
        paths=[name],
        approval_token=approval_token or raw_params.get("approval_token"),
        content_preview={"upload": name, "source": file_path},
    )
    if blocked:
        return blocked

    if not os.path.isfile(file_path):
        entity_cwd = (
            os.path.join(settings.MANOR_FS_ROOT, entity_id)
            if settings.MANOR_FS_ENABLED
            else "/tmp"
        )
        alt = os.path.join(entity_cwd, file_path)
        if os.path.isfile(alt):
            file_path = alt
        else:
            return json.dumps({"error": f"File not found: {file_path}"})

    target = None
    entity_root = None
    if settings.MANOR_FS_ENABLED:
        import time as _time

        entity_root = os.path.join(settings.MANOR_FS_ROOT, entity_id)
        os.makedirs(entity_root, exist_ok=True)
        target = os.path.normpath(os.path.join(entity_root, name))
        if not target.startswith(os.path.normpath(entity_root)):
            return json.dumps({"error": "Path traversal detected"})
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            base, ext_part = os.path.splitext(os.path.basename(target))
            target = os.path.join(os.path.dirname(target), f"{base}_{int(_time.time())}{ext_part}")
        shutil.copy2(file_path, target)

    if not target or not entity_root:
        return json.dumps({"error": "Entity filesystem is not enabled"})

    sync = await sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=target,
        entity_root=entity_root,
        source="agent",
        created_by=user_id or "ai-agent",
        force=True,
        workspace_id=workspace_id or raw_params.get("workspace_id"),
        task_id=task_id or raw_params.get("task_id"),
        agent_id=raw_params.get("agent_id"),
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="manor.save_file",
    )
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
    )

    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        if sync.document_id:
            process_document_embeddings.delay(sync.document_id)
    except Exception:
        pass

    return json.dumps(
        {
            "id": sync.document_id,
            "name": os.path.basename(name),
            "status": "uploaded" if sync.synced else "skipped",
            "reason": sync.reason,
        },
        default=str,
    )


async def runtime_manor_delete_document(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Delete a Knowledge document via the Runtime Manor document boundary."""

    from packages.core.services.document_service import delete_document

    raw_params = params or {}
    ok = await delete_document(
        db,
        raw_params.get("document_id") or raw_params.get("doc_id") or raw_params.get("id") or "",
        entity_id,
    )
    await db.commit()
    if ok:
        await _runtime_manor_invalidate_read_cache(
            entity_id,
            "list_documents",
            "search_documents",
            "list_document_folders",
        )
    return json.dumps({"deleted": bool(ok)}, default=str)


async def runtime_manor_create_document_folder(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Create a Knowledge folder via the Runtime Manor document boundary."""

    raw_params = params or {}
    name = raw_params.get("name") or raw_params.get("folder_name") or ""
    parent_id = raw_params.get("parent_id")
    if parent_id in ("", "root"):
        parent_id = None
    try:
        payload = await _runtime_create_document_folder_path(
            db,
            entity_id,
            name=name,
            parent_id=parent_id,
        )
    except (FileExistsError, LookupError, ValueError) as exc:
        return json.dumps({"error": str(exc)})
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
        "list_document_folders",
    )
    return json.dumps(payload, default=str)


async def runtime_manor_rename_document_folder(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Rename a Knowledge folder via the Runtime Manor document boundary."""

    from sqlalchemy import select

    from packages.core.models.document import DocumentFolder

    raw_params = params or {}
    folder_id = raw_params.get("folder_id") or raw_params.get("id") or ""
    new_name = raw_params.get("name") or raw_params.get("new_name") or ""
    folder = (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == folder_id,
            DocumentFolder.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not folder:
        return json.dumps({"error": "Folder not found"})
    try:
        clean_name, _folders, folder_by_id = await _runtime_validate_document_folder_position(
            db,
            entity_id,
            name=new_name,
            parent_id=folder.parent_id,
            folder_id=folder.id,
        )
    except (FileExistsError, LookupError, ValueError) as exc:
        return json.dumps({"error": str(exc)})
    folder.name = clean_name
    await db.flush()
    await db.commit()
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
        "list_document_folders",
    )
    folder_by_id[folder.id] = folder
    return json.dumps({
        "updated": True,
        "folder": _runtime_folder_summary(folder, folder_by_id=folder_by_id),
    }, default=str)


async def runtime_manor_move_document_folder(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Move a Knowledge folder via the Runtime Manor document boundary."""

    from sqlalchemy import select

    from packages.core.models.document import DocumentFolder

    raw_params = params or {}
    folder_id = raw_params.get("folder_id") or raw_params.get("id") or ""
    parent_id = raw_params.get("parent_id")
    if parent_id in ("", "root"):
        parent_id = None
    folder = (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == folder_id,
            DocumentFolder.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if not folder:
        return json.dumps({"error": "Folder not found"})
    try:
        _clean_name, _folders, folder_by_id = await _runtime_validate_document_folder_position(
            db,
            entity_id,
            name=folder.name,
            parent_id=parent_id,
            folder_id=folder.id,
        )
    except (FileExistsError, LookupError, ValueError) as exc:
        return json.dumps({"error": str(exc)})
    folder.parent_id = parent_id
    await db.flush()
    await db.commit()
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
        "list_document_folders",
    )
    folder_by_id[folder.id] = folder
    return json.dumps({
        "moved": True,
        "folder": _runtime_folder_summary(folder, folder_by_id=folder_by_id),
    }, default=str)


async def runtime_manor_delete_document_folder(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Delete a Knowledge folder tree via the Runtime Manor document boundary."""

    from sqlalchemy import select

    from packages.core.models.document import Document, DocumentFolder, DocumentGroupMember

    raw_params = params or {}
    folder_id = raw_params.get("folder_id") or raw_params.get("id") or ""
    folders = list((await db.execute(
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
    )).scalars().all())
    folder_by_id = {folder.id: folder for folder in folders}
    folder = folder_by_id.get(folder_id)
    if not folder:
        return json.dumps({"error": "Folder not found"})
    child_ids_by_parent: dict[str | None, list[str]] = {}
    for item in folders:
        child_ids_by_parent.setdefault(item.parent_id, []).append(item.id)
    folder_ids: set[str] = set()
    stack = [folder_id]
    while stack:
        current_id = stack.pop()
        if current_id in folder_ids:
            continue
        folder_ids.add(current_id)
        stack.extend(child_ids_by_parent.get(current_id, []))
    folder_id_list = list(folder_ids)
    doc_ids = list((await db.execute(
        select(Document.id).where(
            Document.entity_id == entity_id,
            Document.folder_id.in_(folder_id_list),
        )
    )).scalars().all())
    if doc_ids:
        await db.execute(
            DocumentGroupMember.__table__.delete()
            .where(DocumentGroupMember.document_id.in_(doc_ids))
        )
        await db.execute(
            Document.__table__.delete()
            .where(Document.entity_id == entity_id, Document.id.in_(doc_ids))
        )
    await db.execute(
        DocumentFolder.__table__.delete()
        .where(DocumentFolder.entity_id == entity_id, DocumentFolder.id.in_(folder_id_list))
    )
    await db.flush()
    await db.commit()
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
        "list_document_folders",
    )
    return json.dumps({
        "deleted": True,
        "deleted_folder_count": len(folder_ids),
        "deleted_document_count": len(doc_ids),
    }, default=str)


async def runtime_manor_move_documents_to_folder(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Move one or more Knowledge documents via the Runtime Manor document boundary."""

    from packages.core.services.document_access import get_visible_document, list_visible_documents

    raw_params = params or {}
    workspace_scope = str(raw_params.get("workspace_id") or workspace_id or "").strip() or None
    folder_id = raw_params.get("folder_id")
    if folder_id in ("", "root"):
        folder_id = None
    folder_by_id: dict[str, Any] = {}
    target_folder_path: str | None = None
    if folder_id:
        from packages.core.services.knowledge_visibility import is_user_visible_folder_path

        _folders, folder_by_id = await _runtime_load_document_folders(db, entity_id)
        folder = folder_by_id.get(folder_id)
        if not folder:
            return json.dumps({"error": "Folder not found"})
        target_folder_path = _runtime_folder_rel_path(folder, folder_by_id)
        if not is_user_visible_folder_path(target_folder_path):
            return json.dumps({"error": "Folder not found"})

    raw_ids = (
        raw_params.get("document_ids")
        or raw_params.get("doc_ids")
        or raw_params.get("ids")
        or raw_params.get("document_id")
        or raw_params.get("doc_id")
        or raw_params.get("id")
    )
    document_ids: list[str] = []
    if raw_ids:
        if isinstance(raw_ids, str):
            document_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
        elif isinstance(raw_ids, list):
            document_ids = [str(item).strip() for item in raw_ids if str(item).strip()]

    if not document_ids and raw_params.get("all_visible"):
        source_folder_id = raw_params.get("source_folder_id")
        if source_folder_id in ("", "root"):
            source_folder_id = "root"
        docs, _total = await list_visible_documents(
            db,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_scope,
            actor_type="agent",
            folder_id=source_folder_id,
            limit=_runtime_bounded_int(raw_params.get("limit"), 200, 500, 1),
            offset=_runtime_bounded_int(raw_params.get("offset"), 0, 10_000, 0),
        )
        document_ids = [doc.id for doc in docs]

    if not document_ids:
        return json.dumps({"error": "document_ids required unless all_visible=true"})

    from packages.core.services.document_file_move import move_document_file_to_folder
    from packages.core.services.document_file_state import mark_document_file_missing

    moved: list[dict[str, Any]] = []
    missing: list[str] = []
    missing_files: list[str] = []
    filesystem_moved_count = 0
    for document_id in document_ids:
        doc = await get_visible_document(
            db,
            document_id,
            entity_id,
            user_id=user_id,
            workspace_id=workspace_scope,
            actor_type="agent",
        )
        if not doc:
            missing.append(document_id)
            continue
        fs_move = move_document_file_to_folder(
            doc,
            entity_id=entity_id,
            target_folder_path=target_folder_path,
        )
        if fs_move.reason == "fs_unavailable":
            return json.dumps({"error": "Document storage is temporarily unavailable"})
        if fs_move.reason == "missing_source":
            mark_document_file_missing(doc, source="manor_move")
            missing_files.append(document_id)
            continue
        if fs_move.moved:
            filesystem_moved_count += 1
        doc.folder_id = folder_id
        moved.append(_runtime_doc_summary(doc, details=True))
    await db.flush()
    await db.commit()
    await _runtime_manor_invalidate_read_cache(
        entity_id,
        "list_documents",
        "search_documents",
        "list_document_folders",
    )
    return json.dumps({
        "moved_count": len(moved),
        "missing_ids": missing,
        "missing_file_ids": missing_files,
        "filesystem_moved_count": filesystem_moved_count,
        "folder_id": folder_id,
        "documents": moved[:50],
        "truncated": len(moved) > 50,
    }, default=str)


async def runtime_manor_start_workspace_draft(
    *,
    entity_id: str,
    user_id: str = "",
    params: Mapping[str, Any] | None = None,
) -> str:
    """Start a workspace draft via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.workspace_drafts import runtime_start_workspace_draft_action

    return await runtime_start_workspace_draft_action(
        entity_id=entity_id,
        user_id=user_id or "",
        initial_brief=dict(params or {}).get("initial_brief"),
    )


async def runtime_manor_apply_workspace_operation_patch(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str = "",
    params: Mapping[str, Any] | None = None,
    source_action: str,
    patch: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Apply a single workspace operation patch via the Runtime Manor boundary."""

    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        OperationValidationError,
        apply_operation_draft,
        create_operation_draft,
    )

    target_workspace_id = str((params or {}).get("workspace_id") or workspace_id or "").strip()
    if not target_workspace_id:
        return {"error": "workspace_id is required"}
    draft = await create_operation_draft(
        db,
        target_workspace_id,
        entity_id,
        user_id=user_id,
        source_event_id=f"manor_tool.{source_action}",
        initial_patches=[dict(patch)],
    )
    if draft is None:
        return {"error": "Workspace not found"}
    try:
        return await apply_operation_draft(
            db,
            draft.id,
            entity_id,
            target_workspace_id,
            user_id=user_id,
            user_confirmation=True,
        )
    except OperationConflictError as exc:
        await db.rollback()
        return {"error": str(exc), "type": "operation_conflict"}
    except OperationValidationError as exc:
        await db.rollback()
        return {
            "error": "workspace operation validation failed",
            "validation": exc.validation,
        }


async def runtime_manor_list_agents(db: Any, *, entity_id: str) -> str:
    """List agents via the Runtime Manor action boundary."""

    from packages.core.services.agent_service import list_agents

    agents = await list_agents(db, entity_id)
    return json.dumps([
        {
            "id": a.id,
            "name": a.name,
            "category": a.category,
            "source": a.source,
            "status": a.status,
        }
        for a in agents
    ], default=str)


async def runtime_manor_get_dashboard_summary(db: Any, *, entity_id: str) -> str:
    """Return the entity dashboard summary via the Runtime Manor boundary."""

    from packages.core.services.analytics_service import get_dashboard_stats

    summary = await get_dashboard_stats(db, entity_id)
    return json.dumps(summary, default=str)


async def runtime_manor_list_workspaces(db: Any, *, entity_id: str) -> str:
    """List workspaces via the Runtime Manor action boundary."""

    from packages.core.services.entity_service import list_workspaces

    workspaces = await list_workspaces(db, entity_id)
    return json.dumps([
        {"id": w.id, "name": w.name, "category": w.category, "status": w.status}
        for w in workspaces
    ], default=str)


async def runtime_manor_get_workspace(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get workspace details via the Runtime Manor action boundary."""

    from packages.core.services.workspace_service import get_workspace_full

    ws = await get_workspace_full(db, (params or {}).get("workspace_id", ""), entity_id)
    if not ws:
        return json.dumps({"error": "Workspace not found"})
    return json.dumps(ws, default=str)


async def runtime_manor_update_workspace(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Update workspace fields via the Runtime Manor workspace boundary."""

    from packages.core.services.entity_service import update_workspace

    update_params = dict(params or {})
    workspace_id = update_params.pop("workspace_id", "")
    update_params = _runtime_filter_params(update_params, _WORKSPACE_UPDATE_ALLOWED_FIELDS)
    ws = await update_workspace(
        db,
        workspace_id,
        entity_id,
        **update_params,
    )
    await db.commit()
    if not ws:
        return json.dumps({"error": "Workspace not found"})
    return json.dumps({"id": ws.id, "name": ws.name, "updated": True}, default=str)


async def runtime_manor_get_workspace_daily_summary(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str = "",
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get deterministic workspace daily summary via the Runtime boundary."""

    from packages.core.services.workspace_daily_summary_service import (
        get_workspace_daily_summary,
    )

    raw_params = params or {}
    target_workspace_id = raw_params.get("workspace_id") or workspace_id or ""
    if not target_workspace_id:
        return json.dumps({"error": "workspace_id is required"})
    summary = await get_workspace_daily_summary(
        db,
        entity_id,
        target_workspace_id,
        date=raw_params.get("date"),
        timezone_name=raw_params.get("timezone") or raw_params.get("tz") or "UTC",
        limit_per_section=_runtime_bounded_int(
            raw_params.get("limit_per_section"), 8, 25, 1,
        ),
    )
    return json.dumps(summary, default=str)


async def runtime_manor_get_operating_model(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get a workspace operating model via the Runtime Manor boundary."""

    from packages.core.services.entity_service import get_workspace

    ws = await get_workspace(db, (params or {}).get("workspace_id", ""), entity_id)
    if not ws:
        return json.dumps({"error": "Workspace not found"})
    return json.dumps({
        "workspace_id": ws.id,
        "operating_model": ws.operating_model,
    }, default=str)


async def runtime_manor_get_workspace_agents(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get workspace agent-service mappings via the Runtime Manor boundary."""

    from packages.core.services.workspace_service import get_workspace_agent_mappings

    mappings = await get_workspace_agent_mappings(
        db, (params or {}).get("workspace_id", ""), entity_id,
    )
    return json.dumps(mappings, default=str)


async def runtime_manor_get_workspace_activity(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get workspace activity via the Runtime Manor action boundary."""

    from packages.core.services.workspace_service import list_activity

    raw_params = params or {}
    activities = await list_activity(
        db,
        raw_params.get("workspace_id", ""),
        entity_id,
        limit=raw_params.get("limit", 20),
        event_type=raw_params.get("event_type"),
    )
    return json.dumps(activities, default=str)


async def runtime_manor_get_workspace_dashboard(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get workspace dashboard stats via the Runtime Manor boundary."""

    from packages.core.services.workspace_dashboard_service import get_workspace_stats

    stats = await get_workspace_stats(db, entity_id, (params or {}).get("workspace_id", ""))
    return json.dumps(stats, default=str)


async def runtime_manor_get_entity_info(db: Any, *, entity_id: str) -> str:
    """Get entity info via the Runtime Manor entity boundary."""

    from packages.core.services.entity_service import get_entity

    entity = await get_entity(db, entity_id)
    if not entity:
        return json.dumps({"error": "Entity not found"})
    return json.dumps({
        "id": entity.id,
        "name": entity.name,
        "slug": entity.slug,
    }, default=str)


async def runtime_manor_list_integrations(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str = "",
    ready_action: bool = False,
) -> str:
    """List integration and MCP readiness through the Runtime Manor boundary."""

    from sqlalchemy import select

    from packages.core.models.mcp import MCPServer
    from packages.core.services.agent_permission_service import can_use_mcp_server
    from packages.core.services.integration_service import (
        coming_soon_servers,
        get_integration_inventory,
    )

    raw_params = params or {}
    ready_only = (
        ready_action
        or _runtime_truthy(raw_params.get("ready_only"))
        or _runtime_truthy(raw_params.get("usable_only"))
    )
    include_infra = _runtime_truthy(raw_params.get("include_infra"))
    provider_filter = (
        raw_params.get("provider")
        or raw_params.get("server_key")
        or raw_params.get("integration")
    )
    provider_filter = str(provider_filter).strip().lower() if provider_filter else None

    inventory = await get_integration_inventory(db, entity_id)
    configured_integrations = []
    for item in inventory.get("integrations", []):
        provider = str(item.get("provider") or "")
        if provider_filter and provider != provider_filter:
            continue
        if ready_only and not item.get("ready"):
            continue
        configured_integrations.append({
            "provider": provider,
            "type": item.get("type"),
            "status": item.get("status"),
            "ready": bool(item.get("ready")),
            "healthy": item.get("healthy"),
            "is_default": bool(item.get("is_default", False)),
            "has_credentials": bool(item.get("has_credentials", True)),
        })

    channels = []
    for ch in inventory.get("channels", []):
        key = str(ch.get("key") or "")
        if provider_filter and key != provider_filter and ch.get("required_provider") != provider_filter:
            continue
        if ready_only and not ch.get("ready"):
            continue
        channels.append({
            "key": key,
            "name": ch.get("name"),
            "ready": bool(ch.get("ready")),
            "required_provider": ch.get("required_provider"),
            "needs_integration": bool(ch.get("needs_integration", False)),
            "coming_soon": bool(ch.get("coming_soon", False)),
        })

    query = select(MCPServer).where(MCPServer.status == "active")
    if provider_filter:
        query = query.where(MCPServer.server_key == provider_filter)
    query = query.order_by(MCPServer.name).limit(
        _runtime_bounded_int(raw_params.get("limit"), 80, 200, 1)
    )
    server_rows = list((await db.execute(query)).scalars().all())
    coming_soon = coming_soon_servers()
    mcp_servers = []
    for server in server_rows:
        if server.server_key == "nango" and not include_infra:
            continue
        if server.server_key in coming_soon:
            item = {
                "server_key": server.server_key,
                "name": server.name,
                "auth_type": server.auth_type,
                "ready": False,
                "agent_can_use": False,
                "scope": "none",
                "reason": "Coming soon",
                "coming_soon": True,
            }
        else:
            decision = await can_use_mcp_server(
                db,
                user_id=user_id or "",
                entity_id=entity_id,
                server_key=server.server_key,
                allow_env_fallback=False,
            )
            item = {
                "server_key": server.server_key,
                "name": server.name,
                "auth_type": server.auth_type,
                "ready": bool(decision.allowed),
                "agent_can_use": bool(decision.allowed),
                "scope": decision.scope,
                "reason": decision.reason,
                "coming_soon": False,
            }
        if ready_only and not item["ready"]:
            continue
        mcp_servers.append(item)

    return json.dumps({
        "ready_only": ready_only,
        "user_id_provided": bool(user_id),
        "counts": {
            "configured_integrations": len(configured_integrations),
            "ready_configured_integrations": sum(1 for i in configured_integrations if i["ready"]),
            "mcp_servers": len(mcp_servers),
            "ready_mcp_servers": sum(1 for i in mcp_servers if i["ready"]),
            "channels": len(channels),
            "ready_channels": sum(1 for i in channels if i["ready"]),
        },
        "configured_integrations": configured_integrations,
        "mcp_servers": mcp_servers,
        "channels": channels,
        "note": (
            "MCP readiness is scoped to the current user/entity and does not use shared env fallback."
        ),
    }, ensure_ascii=False, default=str)


async def runtime_manor_create_scheduled_job(
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str = "",
    workspace_id: str = "",
    conversation_id: str = "",
    task_id: str = "",
) -> str:
    """Create a scheduled job via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.scheduling import runtime_create_scheduled_job_action

    raw_params = dict(params or {})
    return await runtime_create_scheduled_job_action(
        entity_id=entity_id,
        name=str(raw_params.get("name") or ""),
        schedule_kind=str(raw_params.get("schedule_kind") or "cron"),
        payload_message=str(raw_params.get("payload_message") or ""),
        cron_expr=str(raw_params.get("cron_expr") or ""),
        every_seconds=raw_params.get("every_seconds") or 0,
        run_at=str(raw_params.get("run_at") or ""),
        agent_id=str(raw_params.get("agent_id") or ""),
        timezone_str=str(raw_params.get("timezone") or "UTC"),
        workspace_id=workspace_id or str(raw_params.get("workspace_id") or "") or None,
        conversation_id=conversation_id or str(raw_params.get("conversation_id") or "") or None,
        user_id=user_id or str(raw_params.get("user_id") or "") or None,
        default_delivery_mode=raw_params.get("default_delivery_mode"),
        execution_target=raw_params.get("execution_target"),
        output_kind=str(raw_params.get("output_kind") or ""),
        file_kind=str(raw_params.get("file_kind") or ""),
        artifact_kind=str(raw_params.get("artifact_kind") or ""),
        requires_generated_file=raw_params.get("requires_generated_file"),
        requires_file_deliverable=raw_params.get("requires_file_deliverable"),
        max_turns=raw_params.get("max_turns"),
    )


async def runtime_manor_list_scheduled_jobs(*, entity_id: str) -> str:
    """List scheduled jobs via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.scheduling import runtime_list_scheduled_jobs_action

    return await runtime_list_scheduled_jobs_action(entity_id=entity_id)


async def runtime_manor_cancel_scheduled_job(
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Cancel a scheduled job via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.scheduling import runtime_cancel_scheduled_job_action

    return await runtime_cancel_scheduled_job_action(
        entity_id=entity_id,
        job_id=str(dict(params or {}).get("job_id") or ""),
    )


async def runtime_manor_toggle_scheduled_job(
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Toggle a scheduled job via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.scheduling import runtime_toggle_scheduled_job_action

    raw_params = dict(params or {})
    enabled_value = raw_params.get("enabled", True)
    return await runtime_toggle_scheduled_job_action(
        entity_id=entity_id,
        job_id=str(raw_params.get("job_id") or ""),
        enabled=enabled_value if isinstance(enabled_value, bool) else _runtime_truthy(enabled_value),
    )


async def runtime_manor_run_scheduled_job_now(
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Run a scheduled job immediately via the Runtime Manor action boundary."""

    from packages.core.ai.runtime.scheduling import runtime_run_scheduled_job_now_action

    return await runtime_run_scheduled_job_now_action(
        entity_id=entity_id,
        job_id=str(dict(params or {}).get("job_id") or ""),
    )


async def runtime_manor_list_notifications(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str | None = None,
) -> str:
    """List notifications via the Runtime Manor communication boundary."""

    from packages.core.services.notification_service import list_notifications

    if not user_id:
        return json.dumps({"error": "user_id_required"})
    raw_params = _runtime_filter_params(params, _NOTIFICATION_LIST_ALLOWED_FIELDS)
    notifs, total = await list_notifications(db, entity_id, user_id, **raw_params)
    return json.dumps({
        "total": total,
        "notifications": [
        {"id": n.id, "title": n.title, "read": n.read}
        for n in notifs
        ],
    }, default=str)


async def runtime_manor_list_conversations(db: Any, *, entity_id: str) -> str:
    """List conversation records via the Runtime Manor communication boundary."""

    from packages.core.services.conversation_records import list_conversations

    convos = await list_conversations(db, entity_id)
    return json.dumps([
        {"id": c.id, "title": c.title}
        for c in convos
    ], default=str)


async def runtime_manor_send_email(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    workspace_id: str = "",
    conversation_id: str | None = None,
) -> str:
    """Send an email via configured channels or platform SMTP."""

    import html

    from sqlalchemy import select

    from packages.core.models.channel import ChannelConfig

    raw_params = dict(params or {})
    to_addr = (
        raw_params.get("to")
        or raw_params.get("recipient")
        or raw_params.get("recipient_email")
        or raw_params.get("to_address")
    )
    subject = raw_params.get("subject") or raw_params.get("title") or ""
    content = (
        raw_params.get("content")
        or raw_params.get("body")
        or raw_params.get("text")
        or raw_params.get("message")
        or ""
    )
    html_content = raw_params.get("html_content") or raw_params.get("html")
    if not to_addr:
        return json.dumps({"error": "recipient email is required"})
    if not subject:
        return json.dumps({"error": "subject is required"})
    if not content and not html_content:
        return json.dumps({"error": "content/body is required"})

    channel_config_id = raw_params.get("channel_config_id")
    target_workspace_id = raw_params.get("workspace_id") or workspace_id
    if not html_content and content:
        html_content = "<p>" + html.escape(str(content)).replace("\n", "<br>") + "</p>"

    if not channel_config_id:
        q = select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.channel_type == "email",
            ChannelConfig.status == "active",
        )
        if target_workspace_id:
            q = q.where(
                (ChannelConfig.workspace_id == target_workspace_id)
                | (ChannelConfig.workspace_id.is_(None))
            )
        else:
            q = q.where(ChannelConfig.workspace_id.is_(None))
        configs = list((await db.execute(
            q.order_by(ChannelConfig.workspace_id.desc().nullslast(), ChannelConfig.created_at.desc())
        )).scalars().all())
        if configs:
            channel_config_id = configs[0].id

    if channel_config_id:
        from packages.core.services.channel_service import send_message

        log = await send_message(
            db,
            entity_id,
            channel_config_id=channel_config_id,
            to_address=str(to_addr),
            subject=str(subject),
            content=str(content or ""),
            html_content=str(html_content or ""),
            conversation_id=conversation_id,
        )
        await db.commit()
        return json.dumps({
            "sent": log.status == "sent",
            "status": log.status,
            "message_log_id": log.id,
            "channel_config_id": channel_config_id,
            "to": to_addr,
            "subject": subject,
            "error": log.error_message,
        }, default=str)

    from packages.core.services.email_service import send_common_email

    ok = await send_common_email(str(to_addr), str(subject), str(html_content or content))
    return json.dumps({
        "sent": bool(ok),
        "status": "sent" if ok else "failed",
        "delivery": "platform_smtp",
        "to": to_addr,
        "subject": subject,
    }, default=str)


async def runtime_manor_list_channel_bindings(db: Any, *, entity_id: str) -> str:
    """List channel bindings via the Runtime Manor communication boundary."""

    from packages.core.services.integration_service import list_channel_bindings

    rows = await list_channel_bindings(db, entity_id)
    return json.dumps(rows, default=str)


async def runtime_manor_bind_channel(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Bind a channel to an agent via the Runtime Manor boundary."""

    from packages.core.services.integration_service import upsert_channel_binding

    raw_params = params or {}
    cc_id = raw_params.get("channel_config_id")
    if not cc_id:
        return json.dumps({"error": "channel_config_id is required"})
    try:
        ch = await upsert_channel_binding(
            db,
            entity_id=entity_id,
            channel_config_id=cc_id,
            agent_id=raw_params.get("agent_id"),
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    await db.commit()
    return json.dumps({
        "channel_id": ch.id,
        "channel_config_id": cc_id,
        "agent_id": ch.agent_id,
        "status": ch.status,
    })


async def runtime_manor_unbind_channel(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Remove a channel binding via the Runtime Manor boundary."""

    from packages.core.services.integration_service import delete_channel_binding

    ch_id = (params or {}).get("channel_id")
    if not ch_id:
        return json.dumps({"error": "channel_id is required"})
    removed = await delete_channel_binding(db, entity_id, ch_id)
    if not removed:
        return json.dumps({"error": "Channel binding not found"})
    await db.commit()
    return json.dumps({"channel_id": ch_id, "unbound": True})


async def runtime_manor_list_staff(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List staff via the Runtime Manor team boundary."""

    from packages.core.services.people_service import list_staff

    raw_params = params or {}
    cache_params = {
        "department": raw_params.get("department"),
        "role": raw_params.get("role"),
        "kind": raw_params.get("kind"),
        "search": raw_params.get("search") or raw_params.get("query"),
        "email": raw_params.get("email"),
        "limit": _runtime_bounded_int(raw_params.get("limit"), 20, 50, 1),
        "offset": _runtime_bounded_int(raw_params.get("offset"), 0, 10_000, 0),
        "detail": "details" if _runtime_want_details(raw_params) else "summary",
    }
    cached = await _runtime_manor_get_cached_read(entity_id, "list_staff", cache_params)
    if cached is not None:
        return cached
    members_all = await list_staff(
        db,
        entity_id,
        department=cache_params["department"],
        role=cache_params["role"],
        kind=cache_params["kind"],
        search=cache_params["search"],
        email=cache_params["email"],
    )
    start = cache_params["offset"]
    end = start + cache_params["limit"]
    members = members_all[start:end]
    result = json.dumps({
        "total": len(members_all),
        "count": len(members),
        "limit": cache_params["limit"],
        "offset": cache_params["offset"],
        "next_offset": end if end < len(members_all) else None,
        "has_more": end < len(members_all),
        "assignment_hint": (
            "Use staff[].assignment_id as assignee_id for create_task/assign_task; "
            "staff_id and assignee_email are also accepted."
        ),
        "staff": [
            _runtime_staff_summary(
                s,
                details=cache_params["detail"] == "details",
                include_assignment=True,
            )
            for s in members
        ],
    }, default=str)
    await _runtime_manor_set_cached_read(entity_id, "list_staff", cache_params, result)
    return result


async def runtime_manor_get_staff(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get staff details via the Runtime Manor team boundary."""

    from packages.core.services.people_service import get_staff_member

    s = await get_staff_member(db, (params or {}).get("staff_id", ""), entity_id)
    if not s:
        return json.dumps({"error": "Staff member not found"})
    meta = s.meta or {}
    return json.dumps({
        "id": s.id,
        "name": s.name,
        "email": s.email,
        "phone": s.phone,
        "kind": getattr(s, "kind", None),
        "title": s.title,
        "role_id": s.role_id,
        "department_id": s.department_id,
        "skills": s.skills or [],
        "service_categories": getattr(s, "service_categories", None) or [],
        "company_name": getattr(s, "company_name", None),
        "tax_id": getattr(s, "tax_id", None),
        "billing_rate": float(s.billing_rate) if getattr(s, "billing_rate", None) is not None else None,
        "billing_currency": getattr(s, "billing_currency", None),
        "meta": meta,
        "status": s.status,
    }, default=str)


async def runtime_manor_list_roles(db: Any, *, entity_id: str) -> str:
    """List staff roles via the Runtime Manor team boundary."""

    from sqlalchemy import func, select

    from packages.core.models.staff import Staff
    from packages.core.services.staff_service import list_roles

    cache_params = {"compact": True}
    cached = await _runtime_manor_get_cached_read(entity_id, "list_roles", cache_params)
    if cached is not None:
        return cached
    roles = await list_roles(db, entity_id)
    out = []
    for r in roles:
        cnt = (await db.execute(
            select(func.count())
            .select_from(Staff)
            .where(Staff.role_id == r.id, Staff.deleted_at.is_(None))
        )).scalar() or 0
        out.append({
            "id": r.id,
            "name": r.name,
            "permissions": list(r.permissions or []),
            "is_default": bool(r.is_default),
            "staff_count": int(cnt),
        })
    result = json.dumps({"total": len(out), "roles": out}, default=str)
    await _runtime_manor_set_cached_read(entity_id, "list_roles", cache_params, result)
    return result


async def runtime_manor_list_clients(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List clients via the Runtime Manor CRM boundary."""

    from packages.core.services.people_service import list_clients

    raw_params = params or {}
    clients, total = await list_clients(
        db,
        entity_id,
        search=raw_params.get("search"),
        status=raw_params.get("status"),
        limit=int(raw_params.get("limit", 100)),
        offset=int(raw_params.get("offset", 0)),
    )
    return json.dumps({
        "total": total,
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "status": c.status,
            }
            for c in clients
        ],
    }, default=str)


async def runtime_manor_get_client(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get client details via the Runtime Manor CRM boundary."""

    from packages.core.services.people_service import get_client

    c = await get_client(db, (params or {}).get("client_id", ""), entity_id)
    if not c:
        return json.dumps({"error": "Client not found"})
    return json.dumps({
        "id": c.id,
        "name": c.name,
        "email": c.email,
        "phone": c.phone,
        "address": c.address,
        "metadata": c.meta or {},
        "status": c.status,
    }, default=str)


async def runtime_manor_create_client(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Create a client via the Runtime Manor CRM boundary."""

    from packages.core.services.people_service import create_client

    create_params = dict(params or {})
    if "metadata" in create_params:
        create_params["meta"] = create_params.pop("metadata")
    create_params = _runtime_filter_params(create_params, _CLIENT_FIELDS)
    c = await create_client(db, entity_id, **create_params)
    await db.commit()
    return json.dumps({"id": c.id, "name": c.name, "created": True}, default=str)


async def runtime_manor_update_client(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Update a client via the Runtime Manor CRM boundary."""

    from packages.core.services.people_service import update_client

    update_params = dict(params or {})
    if "metadata" in update_params:
        update_params["meta"] = update_params.pop("metadata")
    client_id = update_params.pop("client_id", "")
    update_params = _runtime_filter_params(update_params, _CLIENT_FIELDS)
    c = await update_client(
        db,
        client_id,
        entity_id,
        **update_params,
    )
    await db.commit()
    if not c:
        return json.dumps({"error": "Client not found"})
    return json.dumps({"id": c.id, "updated": True}, default=str)


async def runtime_manor_delete_client(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Delete a client via the Runtime Manor CRM boundary."""

    from packages.core.services.people_service import delete_client

    ok = await delete_client(db, (params or {}).get("client_id", ""), entity_id)
    await db.commit()
    return json.dumps({"deleted": bool(ok)}, default=str)


async def runtime_manor_list_orders(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """List orders via the Runtime Manor commerce boundary."""

    from packages.core.services.order_service import list_orders

    raw_params = params or {}
    orders = await list_orders(
        db,
        entity_id,
        status=raw_params.get("status"),
        limit=int(raw_params.get("limit", 100)),
        offset=int(raw_params.get("offset", 0)),
    )
    return json.dumps([
        {
            "id": o.id,
            "order_number": getattr(o, "order_number", None),
            "status": o.status,
            "total": float(o.total) if getattr(o, "total", None) is not None else None,
        }
        for o in orders
    ], default=str)


async def runtime_manor_get_order(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Get order details via the Runtime Manor commerce boundary."""

    from packages.core.services.order_service import get_order

    o = await get_order(db, entity_id, (params or {}).get("order_id", ""))
    if not o:
        return json.dumps({"error": "Order not found"})
    return json.dumps({
        "id": o.id,
        "order_number": getattr(o, "order_number", None),
        "status": o.status,
        "total": float(o.total) if getattr(o, "total", None) is not None else None,
        "client_id": getattr(o, "client_id", None),
        "notes": getattr(o, "notes", None),
    }, default=str)


async def runtime_manor_create_order(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
    user_id: str | None = None,
) -> str:
    """Create an order via the Runtime Manor commerce boundary."""

    from packages.core.services.order_service import create_order

    create_params = _runtime_filter_params(params, _ORDER_CREATE_ALLOWED_FIELDS)
    o = await create_order(db, entity_id, user_id or "system", **create_params)
    await db.commit()
    return json.dumps({
        "id": o.id,
        "order_number": getattr(o, "order_number", None),
        "created": True,
    }, default=str)


async def runtime_manor_update_order(
    db: Any,
    *,
    entity_id: str,
    params: Mapping[str, Any] | None = None,
) -> str:
    """Update an order via the Runtime Manor commerce boundary."""

    from packages.core.services.order_service import update_order

    update_params = dict(params or {})
    order_id = update_params.pop("order_id", "")
    update_params = _runtime_filter_params(update_params, _ORDER_UPDATE_ALLOWED_FIELDS)
    o = await update_order(
        db,
        entity_id,
        order_id,
        **update_params,
    )
    await db.commit()
    if not o:
        return json.dumps({"error": "Order not found"})
    return json.dumps({"id": o.id, "updated": True}, default=str)
