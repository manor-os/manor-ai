"""Issue and consume Proposal-scoped external action authorizations."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select

from packages.core.constants.task import TaskStatus
from packages.core.models.task import Task


_YOUTUBE_PUBLIC_LABEL = re.compile(r"^(?:publish|发布)$", re.IGNORECASE)
_YOUTUBE_UPLOAD_ENTRY_LABEL = re.compile(
    r"^(?:upload videos|上传视频)$",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def issue_proposal_external_authorization(
    task: Task,
    *,
    details: dict[str, Any],
    actor_kind: str,
    actor_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind one approved external task to its Proposal and predecessor."""

    action = dict(details.get("external_action") or {})
    predecessor_key = str(action.get("predecessor_task_key") or "").strip()
    dependency_keys = list(details.get("depends_on_task_keys") or [])
    dependency_ids = list(details.get("depends_on_task_ids") or [])
    dependency_by_key = dict(zip(dependency_keys, dependency_ids))
    predecessor_task_id = str(dependency_by_key.get(predecessor_key) or "").strip()
    proposal_id = str(details.get("strategist_proposal_id") or "").strip()
    proposal_item_id = str(details.get("strategist_proposal_item_id") or "").strip()
    review_id = str(details.get("strategist_review_id") or "").strip()
    if not predecessor_task_id or not review_id:
        raise ValueError("external Proposal task is missing bound Proposal provenance")
    if not proposal_id and not proposal_item_id and actor_kind == "user" and actor_id:
        proposal_id = f"legacy:{review_id}"
        proposal_item_id = f"legacy:{task.id}"
    elif not proposal_id or not proposal_item_id:
        raise ValueError("external Proposal task is missing bound Proposal provenance")

    approved_at = now or _utcnow()
    expires_in_hours = int(action.get("expires_in_hours") or 24)
    return {
        "version": 1,
        "authorization_id": f"{proposal_item_id}:{task.id}",
        "workspace_id": task.workspace_id,
        "review_id": review_id,
        "proposal_id": proposal_id,
        "proposal_item_id": proposal_item_id,
        "task_id": task.id,
        "predecessor_task_id": predecessor_task_id,
        "provider": action.get("provider"),
        "action": action.get("action"),
        "destination": action.get("destination"),
        "visibility": action.get("visibility"),
        "intended_channel": action.get("intended_channel"),
        "max_executions": 1,
        "approved_at": approved_at.isoformat(),
        "expires_at": (approved_at + timedelta(hours=expires_in_hours)).isoformat(),
        "approved_by": actor_id or actor_kind,
        "approved_by_kind": actor_kind,
        "consumed_at": None,
    }


def proposal_youtube_public_request_matches(
    authorization: Any,
    provider_request: Any,
    *,
    task_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Check immutable runtime/provider fields before touching the database."""

    if not _proposal_youtube_public_browser_request_matches(
        authorization,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    label = str(provider_request.get("target_label") or "").strip()
    return bool(_YOUTUBE_PUBLIC_LABEL.fullmatch(label))


def proposal_youtube_public_upload_entry_request_matches(
    authorization: Any,
    provider_request: Any,
    *,
    task_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Match only the YouTube Studio action that starts one upload flow."""

    if not _proposal_youtube_public_browser_request_matches(
        authorization,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    label = str(provider_request.get("target_label") or "").strip()
    return bool(_YOUTUBE_UPLOAD_ENTRY_LABEL.fullmatch(label))


def proposal_youtube_public_upload_transfer_request_matches(
    authorization: Any,
    provider_request: Any,
    *,
    task_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Match one file transfer into the authorized YouTube Studio upload."""

    if not isinstance(provider_request, dict):
        return False
    if not _proposal_youtube_public_scope_matches(
        authorization,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    if provider_request.get("provider") != "chrome":
        return False
    if provider_request.get("confirmation_mode") != "preapproval_allowed":
        return False
    if provider_request.get("policy_category") != "file_upload":
        return False
    if str(provider_request.get("retry_tool") or "") != "mcp__chrome__upload":
        return False
    try:
        host = str(
            urlparse(str(provider_request.get("url") or "")).hostname or ""
        ).lower()
    except ValueError:
        return False
    if host != "studio.youtube.com":
        return False
    return len(_provider_upload_file_names(provider_request)) == 1


def _proposal_youtube_public_browser_request_matches(
    authorization: Any,
    provider_request: Any,
    *,
    task_id: str | None,
    workspace_id: str | None,
) -> bool:
    if not isinstance(provider_request, dict):
        return False
    if not _proposal_youtube_public_scope_matches(
        authorization,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    if provider_request.get("provider") != "chrome":
        return False
    if provider_request.get("confirmation_mode") != "always_action_time":
        return False
    if provider_request.get("policy_category") != "representational_communication":
        return False
    if str(provider_request.get("retry_tool") or "") != "mcp__chrome__click_element":
        return False
    try:
        host = str(urlparse(str(provider_request.get("url") or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == "studio.youtube.com"


def _proposal_youtube_public_scope_matches(
    authorization: Any,
    *,
    task_id: str | None,
    workspace_id: str | None,
) -> bool:
    if not isinstance(authorization, dict):
        return False
    return bool(
        authorization.get("version") == 1
        and str(authorization.get("authorization_id") or "")
        and str(authorization.get("task_id") or "") == str(task_id or "")
        and str(authorization.get("workspace_id") or "") == str(workspace_id or "")
        and authorization.get("provider") == "youtube"
        and authorization.get("action") == "publish_video"
        and authorization.get("destination") == "studio.youtube.com"
        and authorization.get("visibility") == "public"
        and authorization.get("intended_channel")
        == "paired_chrome_signed_in_channel"
        and authorization.get("max_executions") == 1
    )


def _proposal_authorization_is_active(
    authorization: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    if authorization.get("consumed_at"):
        return False
    try:
        expires_at = datetime.fromisoformat(
            str(authorization.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    return bool(
        expires_at.tzinfo is not None
        and expires_at.astimezone(timezone.utc) > now
    )


def _contains_mp4(value: Any) -> bool:
    if isinstance(value, str):
        return ".mp4" in value.lower()
    if isinstance(value, dict):
        return any(_contains_mp4(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_mp4(item) for item in value)
    return False


def _mp4_file_names(value: Any) -> set[str]:
    if isinstance(value, str):
        raw = value.strip()
        if ".mp4" not in raw.lower():
            return set()
        path = urlparse(raw).path if "://" in raw else raw
        name = PurePosixPath(unquote(path.replace("\\", "/"))).name
        return {name.lower()} if name.lower().endswith(".mp4") else set()
    if isinstance(value, dict):
        return set().union(*(_mp4_file_names(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_mp4_file_names(item) for item in value))
    return set()


def _provider_upload_file_names(provider_request: dict[str, Any]) -> set[str]:
    retry_arguments = provider_request.get("retry_arguments")
    if not isinstance(retry_arguments, dict):
        return set()
    files = retry_arguments.get("files") or retry_arguments.get("paths")
    if not isinstance(files, list) or len(files) != 1:
        return set()
    return _mp4_file_names(files)


async def proposal_youtube_public_step_is_authorized(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    now: datetime | None = None,
) -> bool:
    """Verify the approved Proposal already covers this task's step gate."""

    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if task is None or task.status not in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
        return False
    authorization = dict(
        (task.details or {}).get("proposal_external_authorization") or {}
    )
    if not _proposal_youtube_public_scope_matches(
        authorization,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    if not _proposal_authorization_is_active(
        authorization,
        now=now or _utcnow(),
    ):
        return False

    predecessor = (await db.execute(
        select(Task).where(
            Task.id == str(authorization.get("predecessor_task_id") or ""),
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    return bool(
        predecessor is not None
        and predecessor.status == "completed"
        and _contains_mp4(predecessor.actual_output)
    )


async def consume_proposal_youtube_public_authorization(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    runtime_authorization: dict[str, Any],
    provider_request: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Reserve the one-use publish scope in the caller's open transaction.

    The caller must commit only after Chrome proves that the action executed,
    and roll back every non-executed or ambiguous provider result.
    """

    if not proposal_youtube_public_request_matches(
        runtime_authorization,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS:
        return False
    details = dict(task.details or {})
    stored = dict(details.get("proposal_external_authorization") or {})
    if stored.get("authorization_id") != runtime_authorization.get("authorization_id"):
        return False
    if not proposal_youtube_public_request_matches(
        stored,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    current = now or _utcnow()
    if not _proposal_authorization_is_active(stored, now=current):
        return False

    predecessor_task_id = str(stored.get("predecessor_task_id") or "")
    predecessor = (await db.execute(
        select(Task).where(
            Task.id == predecessor_task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if (
        predecessor is None
        or predecessor.status != "completed"
        or not _contains_mp4(predecessor.actual_output)
    ):
        return False

    stored.setdefault("started_at", current.isoformat())
    stored["consumed_at"] = current.isoformat()
    stored["provider_approval_id"] = provider_request.get("provider_approval_id")
    stored["execution_count"] = 1
    details["proposal_external_authorization"] = stored
    task.details = details
    await db.flush()
    return True


async def begin_proposal_youtube_public_authorization(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    runtime_authorization: dict[str, Any],
    provider_request: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Reserve the upload entry in the caller's open transaction.

    The caller must commit only after Chrome proves that the action executed,
    and roll back every non-executed or ambiguous provider result.
    """

    if not proposal_youtube_public_upload_entry_request_matches(
        runtime_authorization,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS:
        return False
    details = dict(task.details or {})
    stored = dict(details.get("proposal_external_authorization") or {})
    if stored.get("authorization_id") != runtime_authorization.get("authorization_id"):
        return False
    if not proposal_youtube_public_upload_entry_request_matches(
        stored,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    current = now or _utcnow()
    if not _proposal_authorization_is_active(stored, now=current):
        return False
    if stored.get("started_at") or int(stored.get("execution_count") or 0) != 0:
        return False

    predecessor = (await db.execute(
        select(Task).where(
            Task.id == str(stored.get("predecessor_task_id") or ""),
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if (
        predecessor is None
        or predecessor.status != "completed"
        or not _contains_mp4(predecessor.actual_output)
    ):
        return False

    stored["started_at"] = current.isoformat()
    stored["execution_count"] = 1
    stored["upload_entry_provider_approval_id"] = provider_request.get(
        "provider_approval_id"
    )
    details["proposal_external_authorization"] = stored
    task.details = details
    await db.flush()
    return True


async def record_proposal_youtube_public_upload_transfer(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    runtime_authorization: dict[str, Any],
    provider_request: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Reserve the authorized MP4 transfer in the caller's transaction."""

    if not proposal_youtube_public_upload_transfer_request_matches(
        runtime_authorization,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS:
        return False
    details = dict(task.details or {})
    stored = dict(details.get("proposal_external_authorization") or {})
    if stored.get("authorization_id") != runtime_authorization.get("authorization_id"):
        return False
    if not proposal_youtube_public_upload_transfer_request_matches(
        stored,
        provider_request,
        task_id=task_id,
        workspace_id=workspace_id,
    ):
        return False
    current = now or _utcnow()
    if not _proposal_authorization_is_active(stored, now=current):
        return False
    if not stored.get("started_at") or int(stored.get("execution_count") or 0) != 1:
        return False
    if stored.get("upload_transfer_provider_approval_id"):
        return False

    predecessor = (await db.execute(
        select(Task).where(
            Task.id == str(stored.get("predecessor_task_id") or ""),
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    requested_names = _provider_upload_file_names(provider_request)
    if (
        predecessor is None
        or predecessor.status != "completed"
        or not requested_names
        or not requested_names.issubset(_mp4_file_names(predecessor.actual_output))
    ):
        return False

    stored["upload_transfer_at"] = current.isoformat()
    stored["upload_transfer_provider_approval_id"] = provider_request.get(
        "provider_approval_id"
    )
    details["proposal_external_authorization"] = stored
    task.details = details
    await db.flush()
    return True
