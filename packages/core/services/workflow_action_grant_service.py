"""Create and validate durable, narrowly scoped workflow action grants."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.workflow import (
    WorkflowActionGrant,
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowProject,
    WorkflowRun,
)
from packages.core.proposals.constants import WORKFLOW_RUN_EXTERNAL_ACTION_KEY


YOUTUBE_PUBLICATION_GRANT_TYPE = "youtube_publication_v1"
_YOUTUBE_PUBLIC_LABEL = re.compile(r"^(?:publish|发布)$", re.IGNORECASE)
_YOUTUBE_SAVE_LABEL = re.compile(r"^(?:save|保存)$", re.IGNORECASE)
_YOUTUBE_UPLOAD_ENTRY_LABEL = re.compile(
    r"^(?:upload videos|上传视频)$",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
WORKFLOW_WAIT_YOUTUBE_ACTION_KEY = "workspace.workflow.youtube.publish"


class WorkflowActionGrantDenied(PermissionError):
    """Raised for absent, expired, revoked, or out-of-scope grants."""


def _denied() -> WorkflowActionGrantDenied:
    return WorkflowActionGrantDenied("Workflow action grant is unavailable or out of scope")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _entity_file_sha256(entity_id: str, fs_path: str) -> str:
    from packages.core.services import entity_fs

    entity_root = entity_fs.get_entity_root(entity_id)
    async with entity_fs.entity_filesystem_read_lock(entity_root):
        full_path = entity_fs.resolve_path(entity_id, fs_path)
        if not full_path or not os.path.isfile(full_path):
            return ""
        return await asyncio.to_thread(_sha256_file, full_path)


def _upload_files_match_filename(files: Any, filename: str) -> bool:
    if not isinstance(files, list) or len(files) != 1:
        return False
    upload_path = files[0]
    if not isinstance(upload_path, str) or not upload_path.strip():
        return False
    basename = upload_path.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return basename == filename and basename.lower().endswith(".mp4")


def _upload_argument_files(arguments: Any) -> list[Any] | None:
    if not isinstance(arguments, dict):
        return None
    files: list[Any] = []
    for key in ("files", "paths"):
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            return None
        files.extend(value)
    return files


def _normalized_knowledge_source(value: Any) -> str:
    source = str(value or "").strip().replace("\\", "/").lstrip("/")
    if source == "Knowledge":
        return ""
    if source.startswith("Knowledge/"):
        return source[len("Knowledge/") :]
    return source


def _upload_files_match_preparation(
    files: Any,
    *,
    scope: dict[str, Any],
    declaration: dict[str, Any],
) -> bool:
    preparation = scope.get("upload_preparation")
    artifact = declaration["artifact"]
    return bool(
        isinstance(files, list)
        and len(files) == 1
        and isinstance(files[0], str)
        and isinstance(preparation, dict)
        and files[0] == preparation.get("local_path")
        and _normalized_knowledge_source(preparation.get("source")) == _normalized_knowledge_source(artifact["fs_path"])
        and _upload_files_match_filename(files, artifact["filename"])
    )


def _upload_document_ids_match_artifact(
    arguments: dict[str, Any] | None,
    *,
    declaration: dict[str, Any],
) -> bool:
    if not isinstance(arguments, dict):
        return False
    if any(arguments.get(key) is not None for key in ("files", "paths", "source", "sources")):
        return False
    raw_ids = arguments.get("document_ids")
    has_document_id = arguments.get("document_id") is not None
    if raw_ids is not None and has_document_id:
        return False
    if raw_ids is None and has_document_id:
        raw_ids = [arguments["document_id"]]
    return bool(
        isinstance(raw_ids, list)
        and len(raw_ids) == 1
        and isinstance(raw_ids[0], str)
        and raw_ids[0] == declaration["artifact"]["document_id"]
    )


def _publication_asset_is_valid(asset: Any) -> bool:
    """Validate a declared post-publication image without naming a campaign."""
    if not isinstance(asset, dict) or set(asset) != {
        "role",
        "workflow_project_id",
        "document_id",
        "fs_path",
        "filename",
        "sha256",
        "state_path",
        "target_video_state_path",
    }:
        return False
    normalized = {
        key: str(asset.get(key) or "").strip()
        for key in (
            "role",
            "workflow_project_id",
            "document_id",
            "fs_path",
            "filename",
            "sha256",
            "state_path",
            "target_video_state_path",
        )
    }
    return bool(
        all(normalized.values())
        and normalized["role"] == "thumbnail"
        and _STATE_PATH.fullmatch(normalized["state_path"])
        and _STATE_PATH.fullmatch(normalized["target_video_state_path"])
        and _SHA256.fullmatch(normalized["sha256"])
        and "/" not in normalized["filename"]
        and "\\" not in normalized["filename"]
        and normalized["filename"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )


def _post_publish_asset_target_video_id(
    project_state: dict[str, Any],
    asset: dict[str, Any] | None,
) -> str | None:
    if not isinstance(asset, dict):
        return None
    value = _state_value(project_state, asset["target_video_state_path"])
    video_id = str(value or "").strip()
    return video_id if _YOUTUBE_VIDEO_ID.fullmatch(video_id) else None


def _upload_document_ids_match_publication_asset(
    arguments: dict[str, Any] | None,
    *,
    declaration: dict[str, Any],
) -> bool:
    if not isinstance(arguments, dict):
        return False
    if any(arguments.get(key) is not None for key in ("files", "paths", "source", "sources")):
        return False
    raw_ids = arguments.get("document_ids")
    has_document_id = arguments.get("document_id") is not None
    if raw_ids is not None and has_document_id:
        return False
    if raw_ids is None and has_document_id:
        raw_ids = [arguments["document_id"]]
    asset = declaration.get("post_publish_asset")
    return bool(
        isinstance(asset, dict)
        and isinstance(raw_ids, list)
        and len(raw_ids) == 1
        and isinstance(raw_ids[0], str)
        and raw_ids[0] == asset.get("document_id")
    )


def _upload_uses_document_ids(arguments: Any) -> bool:
    return isinstance(arguments, dict) and any(
        arguments.get(key) is not None for key in ("document_id", "document_ids")
    )


def _upload_uses_sources(arguments: Any) -> bool:
    return isinstance(arguments, dict) and any(arguments.get(key) is not None for key in ("source", "sources"))


def _upload_sources_match_artifact(
    arguments: Any,
    *,
    entity_id: str,
    declaration: dict[str, Any],
) -> bool:
    if not isinstance(arguments, dict):
        return False
    if any(arguments.get(key) is not None for key in ("document_id", "document_ids", "files", "paths")):
        return False
    has_source = arguments.get("source") is not None
    has_sources = arguments.get("sources") is not None
    if has_source == has_sources:
        return False
    sources = [arguments["source"]] if has_source else arguments["sources"]
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], str):
        return False
    return False


def proposal_workflow_authorization_for_inputs(
    declaration: Any,
    inputs: Any,
    *,
    workflow_steps: Any = None,
) -> dict[str, Any] | None:
    """Return one strict, normalized authorization only when its input condition matches."""

    if not isinstance(declaration, dict) or not isinstance(inputs, dict):
        return None
    allowed_fields = {
        "kind",
        "action_key",
        "when",
        "destination",
        "upload_step_id",
        "publish_step_id",
        "ttl_seconds",
    }
    if set(declaration) - allowed_fields:
        return None
    condition = declaration.get("when")
    if not isinstance(condition, dict) or set(condition) != {"input_key", "equals"}:
        return None
    input_key = str(condition.get("input_key") or "").strip()
    upload_step_id = str(declaration.get("upload_step_id") or "").strip()
    publish_step_id = str(declaration.get("publish_step_id") or "").strip()
    ttl_seconds = declaration.get("ttl_seconds", 86400)
    if (
        declaration.get("kind") != YOUTUBE_PUBLICATION_GRANT_TYPE
        or declaration.get("action_key") != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or declaration.get("destination") != "studio.youtube.com"
        or condition.get("equals") != "public"
        or not input_key
        or inputs.get(input_key) != "public"
        or not upload_step_id
        or not publish_step_id
        or upload_step_id == publish_step_id
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 86400
    ):
        return None
    if workflow_steps is not None:
        steps_by_id = (
            {str(step.get("id") or "").strip(): step for step in workflow_steps if isinstance(step, dict)}
            if isinstance(workflow_steps, list)
            else {}
        )
        for step_id in (upload_step_id, publish_step_id):
            step = steps_by_id.get(step_id)
            if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "agent":
                return None
    return {
        "kind": YOUTUBE_PUBLICATION_GRANT_TYPE,
        "action_key": WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        "when": {"input_key": input_key, "equals": "public"},
        "destination": "studio.youtube.com",
        "upload_step_id": upload_step_id,
        "publish_step_id": publish_step_id,
        "ttl_seconds": ttl_seconds,
    }


def youtube_publication_provider_request_kind(
    provider_request: Any,
    *,
    allow_upload_sources: bool = False,
) -> str | None:
    """Classify only the three allowlisted YouTube Studio approval requests."""

    if not isinstance(provider_request, dict) or provider_request.get("provider") != "chrome":
        return None
    try:
        host = str(urlparse(str(provider_request.get("url") or "")).hostname or "").lower()
    except ValueError:
        return None
    if host != "studio.youtube.com":
        return None
    retry_tool = str(provider_request.get("retry_tool") or "")
    if (
        provider_request.get("confirmation_mode") == "preapproval_allowed"
        and provider_request.get("policy_category") == "file_upload"
        and retry_tool == "mcp__chrome__upload"
    ):
        arguments = provider_request.get("retry_arguments")
        if _upload_uses_document_ids(arguments):
            return "upload_transfer"
        if allow_upload_sources and _upload_uses_sources(arguments):
            return "upload_transfer"
        files = _upload_argument_files(arguments)
        if isinstance(files, list) and len(files) == 1 and str(files[0]).lower().split("?", 1)[0].endswith(".mp4"):
            return "upload_transfer"
        return None
    if (
        provider_request.get("confirmation_mode") != "always_action_time"
        or provider_request.get("policy_category") != "representational_communication"
        or retry_tool != "mcp__chrome__click_element"
    ):
        return None
    label = str(provider_request.get("target_label") or "").strip()
    if _YOUTUBE_UPLOAD_ENTRY_LABEL.fullmatch(label):
        return "upload_entry"
    if _YOUTUBE_PUBLIC_LABEL.fullmatch(label):
        return "publish"
    if _YOUTUBE_SAVE_LABEL.fullmatch(label):
        return "save"
    return None


async def create_proposal_workflow_action_grant(
    db: AsyncSession,
    *,
    item: ProposalItemRecord,
    run: WorkflowRun,
    binding: WorkflowBinding,
    declaration: dict[str, Any],
    granted_by: str,
) -> WorkflowActionGrant:
    """Create the single runtime grant covered by one approved Proposal item."""

    workflow = await db.get(WorkflowDefinition, binding.workflow_id)
    normalized = proposal_workflow_authorization_for_inputs(
        declaration,
        (item.payload or {}).get("inputs"),
        workflow_steps=workflow.steps if workflow is not None else None,
    )
    snapshot = (item.payload or {}).get("_proposal_authorization_binding") if isinstance(item.payload, dict) else None
    root_run_id = str(run.lineage_root_run_id or run.id)
    if (
        normalized is None
        or workflow is None
        or not isinstance(snapshot, dict)
        or snapshot.get("binding_id") != binding.id
        or snapshot.get("workflow_id") != binding.workflow_id
        or snapshot.get("revision") != binding.revision
        or snapshot.get("declaration") != normalized
        or item.entity_id != run.entity_id
        or item.workspace_id != run.workspace_id
        or item.action_key != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or item.risk_level != "high"
        or str(item.status) != "approved"
        or run.binding_id != binding.id
        or run.trigger_source != "proposal"
        or run.lineage_is_legacy is not False
        or root_run_id != run.id
        or str(((run.trigger_data or {}).get("_proposal_context") or {}).get("proposal_item_id") or "") != item.id
    ):
        raise _denied()
    existing = (
        await db.execute(
            select(WorkflowActionGrant).where(
                WorkflowActionGrant.proposal_item_id == item.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.workflow_run_id == run.id
            and existing.workflow_lineage_root_run_id == root_run_id
            and existing.scope.get("declaration") == normalized
        ):
            return existing
        raise _denied()
    now = datetime.now(timezone.utc)
    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
        workflow_run_id=run.id,
        project_id=None,
        proposal_item_id=item.id,
        workflow_lineage_root_run_id=root_run_id,
        action_key=WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope={
            "source": "proposal",
            "binding_id": binding.id,
            "binding_revision": binding.revision,
            "declaration": normalized,
        },
        granted_by=granted_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=normalized["ttl_seconds"]),
    )
    db.add(grant)
    await db.flush()
    return grant


async def _validate_proposal_youtube_publication_scope(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    workflow_run_id: str,
    workflow_lineage_root_run_id: str,
    workflow_step_id: str,
    for_update: bool = False,
) -> tuple[WorkflowActionGrant, dict[str, Any]]:
    statement = select(WorkflowActionGrant).where(
        WorkflowActionGrant.id == grant_id,
        WorkflowActionGrant.entity_id == entity_id,
        WorkflowActionGrant.workspace_id == workspace_id,
        WorkflowActionGrant.granted_by == user_id,
        WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
        WorkflowActionGrant.action_key == WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        WorkflowActionGrant.workflow_lineage_root_run_id == workflow_lineage_root_run_id,
    )
    if for_update:
        statement = statement.with_for_update()
    grant = (await db.execute(statement)).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None or grant.consumed_at is not None:
        raise _denied()
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    declaration = scope.get("declaration")
    run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == workflow_run_id,
                WorkflowRun.entity_id == entity_id,
                WorkflowRun.workspace_id == workspace_id,
                WorkflowRun.lineage_root_run_id == workflow_lineage_root_run_id,
                WorkflowRun.binding_id == scope.get("binding_id"),
                WorkflowRun.trigger_source == "proposal",
            )
        )
    ).scalar_one_or_none()
    item = await db.get(ProposalItemRecord, grant.proposal_item_id)
    binding = await db.get(WorkflowBinding, scope.get("binding_id"))
    workflow = await db.get(WorkflowDefinition, binding.workflow_id) if binding is not None else None
    current_declaration = proposal_workflow_authorization_for_inputs(
        (binding.config or {}).get("proposal_authorization") if binding is not None else None,
        (item.payload or {}).get("inputs") if item is not None else None,
        workflow_steps=workflow.steps if workflow is not None else None,
    )
    proposal_context = (
        (run.trigger_data or {}).get("_proposal_context")
        if run is not None and isinstance(run.trigger_data, dict)
        else None
    )
    if (
        scope.get("source") != "proposal"
        or not isinstance(declaration, dict)
        or run is None
        or run.lineage_is_legacy is not False
        or item is None
        or item.entity_id != entity_id
        or item.workspace_id != workspace_id
        or item.execution_root_id != workflow_lineage_root_run_id
        or str(item.status) != "executing"
        or item.action_key != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or not isinstance(proposal_context, dict)
        or proposal_context.get("proposal_item_id") != item.id
        or binding is None
        or not binding.enabled
        or binding.status != "active"
        or binding.revision != scope.get("binding_revision")
        or current_declaration != declaration
    ):
        raise _denied()
    return grant, declaration


async def validate_proposal_youtube_publication_step(
    db: AsyncSession,
    *,
    browser_action: str,
    **kwargs: Any,
) -> WorkflowActionGrant:
    """Authorize only browser side effects owned by the declared Workflow step."""

    grant, declaration = await _validate_proposal_youtube_publication_scope(
        db,
        **kwargs,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    allowed_actions = (
        {"click_element", "upload"}
        if workflow_step_id == declaration.get("upload_step_id")
        else {"click_element", "fill_or_select"}
        if workflow_step_id == declaration.get("publish_step_id")
        else set()
    )
    if browser_action not in allowed_actions:
        raise _denied()
    return grant


async def validate_proposal_youtube_publication_grant(
    db: AsyncSession,
    *,
    provider_request: dict[str, Any],
    for_update: bool = False,
    **kwargs: Any,
) -> tuple[WorkflowActionGrant, str]:
    grant, declaration = await _validate_proposal_youtube_publication_scope(
        db,
        for_update=for_update,
        **kwargs,
    )
    request_kind = youtube_publication_provider_request_kind(provider_request)
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    expected_step_id = (
        declaration.get("publish_step_id")
        if request_kind == "publish"
        else declaration.get("upload_step_id")
        if request_kind in {"upload_entry", "upload_transfer"}
        else None
    )
    if not request_kind or expected_step_id != workflow_step_id:
        raise _denied()
    return grant, request_kind


async def consume_proposal_youtube_publication_grant(
    db: AsyncSession,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, request_kind = await validate_proposal_youtube_publication_grant(
        db,
        for_update=True,
        **kwargs,
    )
    if request_kind != "publish":
        raise _denied()
    grant.consumed_at = datetime.now(timezone.utc)
    grant.consumed_by_run_id = str(kwargs.get("workflow_run_id") or "")
    await db.flush()
    return grant


def workflow_wait_youtube_publication_authorization(
    declaration: Any,
    *,
    workflow_steps: Any = None,
) -> dict[str, Any] | None:
    """Normalize the exact publication authorized by one Workflow wait."""
    if not isinstance(declaration, dict):
        return None
    allowed_fields = {
        "kind",
        "source",
        "approved_wait_step_id",
        "channel_id",
        "channel_observation_step_id",
        "lease_path",
        "destination",
        "upload_step_id",
        "publish_step_id",
        "artifact",
        "publication",
        "post_publish_asset",
        "post_publish_asset_step_id",
        "ttl_seconds",
    }
    if set(declaration) - allowed_fields:
        return None
    artifact = declaration.get("artifact")
    publication = declaration.get("publication")
    post_publish_asset = declaration.get("post_publish_asset")
    post_publish_asset_step_id = str(declaration.get("post_publish_asset_step_id") or "").strip()
    if not isinstance(artifact, dict) or set(artifact) != {
        "workflow_project_id",
        "document_id",
        "fs_path",
        "filename",
        "sha256",
    }:
        return None
    if not isinstance(publication, dict) or set(publication) != {
        "title",
        "description",
        "audience",
        "visibility",
    }:
        return None
    source = str(declaration.get("source") or "").strip()
    approved_wait_step_id = str(declaration.get("approved_wait_step_id") or "").strip()
    channel_id = str(declaration.get("channel_id") or "").strip()
    channel_observation_step_id = str(declaration.get("channel_observation_step_id") or "").strip()
    lease_path = str(declaration.get("lease_path") or "").strip()
    upload_step_id = str(declaration.get("upload_step_id") or "").strip()
    publish_step_id = str(declaration.get("publish_step_id") or "").strip()
    normalized_artifact = {
        key: str(artifact.get(key) or "").strip()
        for key in (
            "workflow_project_id",
            "document_id",
            "fs_path",
            "filename",
            "sha256",
        )
    }
    normalized_publication = {
        "title": str(publication.get("title") or "").strip(),
        "description": str(publication.get("description") or ""),
        "audience": str(publication.get("audience") or "").strip().lower(),
        "visibility": str(publication.get("visibility") or "").strip().lower(),
    }
    ttl_seconds = declaration.get("ttl_seconds", 86400)
    if (
        declaration.get("kind") != YOUTUBE_PUBLICATION_GRANT_TYPE
        or source not in {"workflow_wait", "workflow_execution"}
        or declaration.get("destination") != "studio.youtube.com"
        or (source == "workflow_wait" and (not approved_wait_step_id or channel_id))
        or (
            source == "workflow_execution"
            and (
                not channel_id
                or not channel_observation_step_id
                or not _STATE_PATH.fullmatch(lease_path)
                or approved_wait_step_id
            )
        )
        or not upload_step_id
        or not publish_step_id
        or upload_step_id == publish_step_id
        or (source == "workflow_wait" and bool(post_publish_asset))
        or bool(post_publish_asset) != bool(post_publish_asset_step_id)
        or (
            post_publish_asset is not None
            and (
                not _publication_asset_is_valid(post_publish_asset)
                or post_publish_asset_step_id in {upload_step_id, publish_step_id}
                or post_publish_asset.get("workflow_project_id") != artifact.get("workflow_project_id")
            )
        )
        or not all(normalized_artifact.values())
        or not normalized_artifact["fs_path"].lower().endswith(".mp4")
        or not normalized_artifact["filename"].lower().endswith(".mp4")
        or "/" in normalized_artifact["filename"]
        or "\\" in normalized_artifact["filename"]
        or not _SHA256.fullmatch(normalized_artifact["sha256"])
        or not normalized_publication["title"]
        or len(normalized_publication["title"]) > 100
        or len(normalized_publication["description"]) > 5000
        or normalized_publication["audience"] not in {"made_for_kids", "not_made_for_kids"}
        or normalized_publication["visibility"] not in {"public", "unlisted", "private"}
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 86400
    ):
        return None
    if workflow_steps is not None:
        steps_by_id = (
            {str(step.get("id") or "").strip(): step for step in workflow_steps if isinstance(step, dict)}
            if isinstance(workflow_steps, list)
            else {}
        )
        step_ids = [upload_step_id, publish_step_id]
        if post_publish_asset_step_id:
            step_ids.append(post_publish_asset_step_id)
        for step_id in step_ids:
            step = steps_by_id.get(step_id)
            if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "agent":
                return None
    result = {
        "kind": YOUTUBE_PUBLICATION_GRANT_TYPE,
        "source": source,
        "destination": "studio.youtube.com",
        "upload_step_id": upload_step_id,
        "publish_step_id": publish_step_id,
        "artifact": normalized_artifact,
        "publication": normalized_publication,
        "ttl_seconds": ttl_seconds,
    }
    if source == "workflow_wait":
        result["approved_wait_step_id"] = approved_wait_step_id
    else:
        result["channel_id"] = channel_id
        result["channel_observation_step_id"] = channel_observation_step_id
        result["lease_path"] = lease_path
    if post_publish_asset is not None:
        result["post_publish_asset"] = {
            key: str(post_publish_asset.get(key) or "").strip()
            for key in (
                "role",
                "workflow_project_id",
                "document_id",
                "fs_path",
                "filename",
                "sha256",
                "state_path",
                "target_video_state_path",
            )
        }
        result["post_publish_asset_step_id"] = post_publish_asset_step_id
    return result


async def _workflow_wait_publication_artifact_is_current(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    declaration: dict[str, Any],
) -> bool:
    artifact = declaration["artifact"]
    publication = declaration["publication"]
    project = await db.get(WorkflowProject, artifact["workflow_project_id"])
    document = await db.get(Document, artifact["document_id"])
    project_state = project.state if project is not None and isinstance(project.state, dict) else {}
    project_publication = project_state.get("publication")
    document_metadata = document.metadata_ if document is not None and isinstance(document.metadata_, dict) else {}
    project_artifact = dict(artifact)
    project_artifact.pop("workflow_project_id", None)
    current_sha256 = await _entity_file_sha256(entity_id, artifact["fs_path"])
    post_publish_asset = declaration.get("post_publish_asset")
    return bool(
        project is not None
        and project.entity_id == entity_id
        and project.workspace_id == workspace_id
        and project.current_stage in {"video_ready", "youtube_saved"}
        and project_state.get("artifact") == project_artifact
        and isinstance(project_publication, dict)
        and project_publication.get("review_status") == "approved"
        and all(project_publication.get(key) == value for key, value in publication.items())
        and document is not None
        and document.entity_id == entity_id
        and document.is_trashed is not True
        and document.fs_path == artifact["fs_path"]
        and document.mime_type == "video/mp4"
        and current_sha256 == artifact["sha256"]
        and (
            not document_metadata.get("sha256")
            or str(document_metadata.get("sha256") or "").lower() == artifact["sha256"]
        )
        and await _declared_publication_asset_is_current(
            db,
            entity_id=entity_id,
            project_state=project_state,
            asset=post_publish_asset,
        )
    )


def _state_value(state: dict[str, Any], path: str) -> Any:
    value: Any = state
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


async def _declared_publication_asset_is_current(
    db: AsyncSession,
    *,
    entity_id: str,
    project_state: dict[str, Any],
    asset: Any,
) -> bool:
    if asset is None:
        return True
    if not isinstance(asset, dict):
        return False
    document = await db.get(Document, asset["document_id"])
    current_sha256 = await _entity_file_sha256(entity_id, asset["fs_path"])
    state_asset = _state_value(project_state, asset["state_path"])
    return bool(
        isinstance(state_asset, dict)
        and all(state_asset.get(key) == asset[key] for key in ("document_id", "fs_path", "filename", "sha256"))
        and document is not None
        and document.entity_id == entity_id
        and document.is_trashed is not True
        and document.fs_path == asset["fs_path"]
        and str(document.mime_type or "").lower().startswith("image/")
        and current_sha256 == asset["sha256"]
    )


async def _workflow_execution_publication_artifact_is_current(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    run_id: str,
    declaration: dict[str, Any],
    video_consumed: bool = False,
) -> bool:
    artifact = declaration["artifact"]
    publication = declaration["publication"]
    project = await db.get(WorkflowProject, artifact["workflow_project_id"])
    document = await db.get(Document, artifact["document_id"])
    project_state = project.state if project is not None and isinstance(project.state, dict) else {}
    project_artifact = dict(artifact)
    project_artifact.pop("workflow_project_id", None)
    document_metadata = document.metadata_ if document is not None and isinstance(document.metadata_, dict) else {}
    lease = _state_value(project_state, declaration["lease_path"])
    try:
        lease_expires_at = datetime.fromisoformat(str((lease or {}).get("expires_at") or ""))
        lease_is_live = lease_expires_at.tzinfo is not None and lease_expires_at.astimezone(
            timezone.utc
        ) > datetime.now(timezone.utc)
    except ValueError:
        lease_is_live = False
    current_sha256 = await _entity_file_sha256(entity_id, artifact["fs_path"])
    post_publish_asset = declaration.get("post_publish_asset")
    published_target_video_id = _post_publish_asset_target_video_id(
        project_state,
        post_publish_asset if isinstance(post_publish_asset, dict) else None,
    )
    return bool(
        project is not None
        and project.entity_id == entity_id
        and project.workspace_id == workspace_id
        and project.current_stage == "leased"
        and isinstance(lease, dict)
        and lease.get("run_id") == run_id
        and lease.get("due_path") == "due_at"
        and lease.get("pending_stage") == "ready"
        and lease_is_live
        and project_state.get("artifact") == project_artifact
        and (
            project_state.get("publication") == publication
            or (video_consumed and published_target_video_id is not None)
        )
        and document is not None
        and document.entity_id == entity_id
        and document.is_trashed is not True
        and document.fs_path == artifact["fs_path"]
        and document.mime_type == "video/mp4"
        and current_sha256 == artifact["sha256"]
        and (
            not document_metadata.get("sha256")
            or str(document_metadata.get("sha256") or "").lower() == artifact["sha256"]
        )
        and await _declared_publication_asset_is_current(
            db,
            entity_id=entity_id,
            project_state=project_state,
            asset=post_publish_asset,
        )
    )


async def create_workflow_wait_youtube_publication_grant(
    db: AsyncSession,
    *,
    root_run: WorkflowRun,
    executor_run: WorkflowRun,
    executor_workflow: WorkflowDefinition,
    declaration: dict[str, Any],
) -> WorkflowActionGrant:
    """Create the exact grant covered by an approved root Workflow wait."""
    from packages.core.services.workflow_run_trace import (
        workflow_definition_fingerprint,
    )

    normalized = workflow_wait_youtube_publication_authorization(
        declaration,
        workflow_steps=executor_workflow.steps,
    )
    root_snapshot = root_run.execution_snapshot if isinstance(root_run.execution_snapshot, dict) else {}
    executor_snapshot = executor_run.execution_snapshot if isinstance(executor_run.execution_snapshot, dict) else {}
    wait_step_id = str((normalized or {}).get("approved_wait_step_id") or "")
    wait_step = next(
        (
            step
            for step in root_snapshot.get("steps") or []
            if isinstance(step, dict) and str(step.get("id") or "") == wait_step_id
        ),
        None,
    )
    approval = (root_run.step_results or {}).get(wait_step_id) if isinstance(root_run.step_results, dict) else None
    approval = approval if isinstance(approval, dict) else {}
    child_trigger = executor_run.trigger_data if isinstance(executor_run.trigger_data, dict) else {}
    executor_fingerprint = workflow_definition_fingerprint(executor_workflow)
    approved_by = str(approval.get("approved_by") or "").strip()
    if (
        normalized is None
        or not root_run.workspace_id
        or root_run.entity_id != executor_run.entity_id
        or root_run.workspace_id != executor_run.workspace_id
        or str(root_run.started_by or "") != approved_by
        or wait_step is None
        or wait_step.get("type") != "wait"
        or approval.get("status") != "completed"
        or approval.get("approved") is not True
        or not approval.get("approved_at")
        or executor_run.workflow_id != executor_workflow.id
        or executor_run.started_by != root_run.started_by
        or executor_run.lineage_is_legacy is not False
        or str(executor_run.lineage_root_run_id or executor_run.id) != executor_run.id
        or str(child_trigger.get("parent_run_id") or "") != root_run.id
        or not str(child_trigger.get("parent_step_id") or "").strip()
        or executor_snapshot.get("fingerprint") != executor_fingerprint
        or child_trigger.get("_workflow_definition_fingerprint") != executor_fingerprint
        or not await _workflow_wait_publication_artifact_is_current(
            db,
            entity_id=root_run.entity_id,
            workspace_id=root_run.workspace_id,
            declaration=normalized,
        )
    ):
        raise _denied()

    existing = (
        (
            await db.execute(
                select(WorkflowActionGrant).where(
                    WorkflowActionGrant.workflow_run_id == executor_run.id,
                    WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
                )
            )
        )
        .scalars()
        .all()
    )
    expected_scope = {
        "source": "workflow_wait",
        "approval_root_run_id": root_run.id,
        "approval_wait_step_id": wait_step_id,
        "approval_root_fingerprint": root_snapshot.get("fingerprint"),
        "approved_at": approval["approved_at"],
        "executor_run_id": executor_run.id,
        "executor_workflow_id": executor_workflow.id,
        "executor_workflow_fingerprint": executor_fingerprint,
        "declaration": normalized,
    }
    for grant in existing:
        persisted_scope = dict(grant.scope or {})
        persisted_scope.pop("upload_preparation", None)
        if persisted_scope == expected_scope and grant.granted_by == approved_by:
            return grant
    if existing:
        raise _denied()

    now = datetime.now(timezone.utc)
    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=root_run.entity_id,
        workspace_id=root_run.workspace_id,
        workflow_run_id=executor_run.id,
        project_id=normalized["artifact"]["workflow_project_id"],
        proposal_item_id=None,
        workflow_lineage_root_run_id=executor_run.id,
        action_key=WORKFLOW_WAIT_YOUTUBE_ACTION_KEY,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope=expected_scope,
        granted_by=approved_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=normalized["ttl_seconds"]),
    )
    db.add(grant)
    trusted = (
        child_trigger.get("_workflow_runtime_context")
        if isinstance(child_trigger.get("_workflow_runtime_context"), dict)
        else {}
    )
    executor_run.trigger_data = {
        **child_trigger,
        "_workflow_runtime_context": {
            **trusted,
            "workflow_action_grant_id": grant.id,
        },
    }
    await db.flush()
    return grant


def _workflow_execution_channel_is_observed(
    run: WorkflowRun,
    declaration: dict[str, Any],
) -> bool:
    observation_step_id = declaration["channel_observation_step_id"]
    snapshot = run.execution_snapshot if isinstance(run.execution_snapshot, dict) else {}
    observation_step = next(
        (
            step
            for step in snapshot.get("steps") or []
            if isinstance(step, dict) and step.get("id") == observation_step_id
        ),
        None,
    )
    config = observation_step.get("config") if isinstance(observation_step, dict) else None
    if (
        not isinstance(observation_step, dict)
        or observation_step.get("type") != "tool"
        or not isinstance(config, dict)
        or config.get("tool") != "check_youtube_publication_setup"
    ):
        return False
    result = (run.step_results or {}).get(observation_step_id)
    if not isinstance(result, dict) or result.get("status") != "completed":
        return False
    observation = result.get("output")
    if isinstance(observation, str):
        try:
            import json

            observation = json.loads(observation)
        except (TypeError, ValueError):
            return False
    return bool(
        isinstance(observation, dict)
        and observation.get("ready") is True
        and observation.get("studio_authenticated") is True
        and str(observation.get("channel") or "").strip() == declaration["channel_id"]
    )


async def _execution_publication_retry_chain_is_valid(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    grant_run_id: str | None = None,
) -> bool:
    """Allow a first grant or reuse its exact origin through terminal retries."""
    root_id = run.lineage_root_run_id or run.id
    current = run
    seen = {run.id}
    for _ in range(100):
        if current.id == grant_run_id:
            return True
        if current.id == root_id:
            return grant_run_id is None and not current.retry_of_run_id
        parent = await db.get(WorkflowRun, current.retry_of_run_id) if current.retry_of_run_id else None
        if (
            parent is None
            or parent.id in seen
            or parent.entity_id != run.entity_id
            or parent.workspace_id != run.workspace_id
            or parent.workflow_id != run.workflow_id
            or parent.started_by != run.started_by
            or parent.lineage_is_legacy is not False
            or (parent.lineage_root_run_id or parent.id) != root_id
            or parent.status not in {"failed", "cancelled"}
            or parent.execution_snapshot != run.execution_snapshot
        ):
            return False
        if grant_run_id is None and await db.scalar(
            select(WorkflowActionGrant.id)
            .where(
                WorkflowActionGrant.workflow_run_id == parent.id,
                WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
            )
            .limit(1)
        ):
            return False
        seen.add(parent.id)
        current = parent
    return False


async def create_workflow_execution_youtube_publication_grant(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    grant_step_id: str,
    declaration: dict[str, Any],
) -> WorkflowActionGrant:
    from packages.core.services.workflow_run_trace import execution_snapshot_for_run

    workflow = await db.get(WorkflowDefinition, run.workflow_id)
    snapshot = execution_snapshot_for_run(workflow, run) or {}
    normalized = workflow_wait_youtube_publication_authorization(
        declaration,
        workflow_steps=snapshot.get("steps"),
    )
    step = next(
        (
            value
            for value in snapshot.get("steps") or []
            if isinstance(value, dict) and value.get("id") == grant_step_id
        ),
        None,
    )
    configured = ((step or {}).get("config") or {}).get("declaration") if isinstance(step, dict) else None
    fingerprint = snapshot.get("fingerprint")
    if (
        normalized is None
        or normalized.get("source") != "workflow_execution"
        or workflow is None
        or workflow.entity_id != run.entity_id
        or not run.workspace_id
        or not run.started_by
        or run.status != "running"
        or run.lineage_is_legacy is not False
        or not fingerprint
        or not isinstance(step, dict)
        or step.get("type") != "workflow_action_grant"
        or not isinstance(configured, dict)
        or configured.get("source") != "workflow_execution"
        or configured.get("kind") != YOUTUBE_PUBLICATION_GRANT_TYPE
        or not _workflow_execution_channel_is_observed(run, normalized)
        or not await _workflow_execution_publication_artifact_is_current(
            db,
            entity_id=run.entity_id,
            workspace_id=run.workspace_id,
            run_id=run.id,
            declaration=normalized,
        )
    ):
        raise _denied()

    scope = {
        "source": "workflow_execution",
        "executor_run_id": run.id,
        "executor_workflow_id": workflow.id,
        "executor_workflow_fingerprint": fingerprint,
        "grant_step_id": grant_step_id,
        "declaration": normalized,
    }
    existing = (
        (
            await db.execute(
                select(WorkflowActionGrant)
                .where(
                    WorkflowActionGrant.entity_id == run.entity_id,
                    WorkflowActionGrant.workspace_id == run.workspace_id,
                    WorkflowActionGrant.workflow_lineage_root_run_id == (run.lineage_root_run_id or run.id),
                    WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    if len(existing) == 1:
        grant = existing[0]
        expires_at = grant.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        # Re-executing the node after a native retry reuses the exact authority;
        # it must never refresh its deadline or replace a consumed grant.
        expected_scope = {**scope, "executor_run_id": grant.workflow_run_id}
        if (
            isinstance(grant.scope, dict)
            and all(grant.scope.get(key) == value for key, value in expected_scope.items())
            and grant.granted_by == run.started_by
            and grant.project_id == normalized["artifact"]["workflow_project_id"]
            and grant.action_key == WORKFLOW_WAIT_YOUTUBE_ACTION_KEY
            and grant.revoked_at is None
            and grant.consumed_at is None
            and expires_at > now
            and await _execution_publication_retry_chain_is_valid(db, run, grant_run_id=grant.workflow_run_id)
        ):
            return grant
    if existing or not await _execution_publication_retry_chain_is_valid(db, run):
        raise _denied()

    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=run.entity_id,
        workspace_id=run.workspace_id,
        workflow_run_id=run.id,
        project_id=normalized["artifact"]["workflow_project_id"],
        proposal_item_id=None,
        workflow_lineage_root_run_id=run.lineage_root_run_id or run.id,
        action_key=WORKFLOW_WAIT_YOUTUBE_ACTION_KEY,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope=scope,
        granted_by=run.started_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=normalized["ttl_seconds"]),
    )
    db.add(grant)
    await db.flush()
    return grant


async def _validate_workflow_wait_youtube_publication_scope(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    workflow_run_id: str,
    workflow_lineage_root_run_id: str,
    workflow_step_id: str,
    for_update: bool = False,
) -> tuple[WorkflowActionGrant, dict[str, Any]]:
    statement = select(WorkflowActionGrant).where(
        WorkflowActionGrant.id == grant_id,
        WorkflowActionGrant.entity_id == entity_id,
        WorkflowActionGrant.workspace_id == workspace_id,
        WorkflowActionGrant.granted_by == user_id,
        WorkflowActionGrant.workflow_lineage_root_run_id == workflow_lineage_root_run_id,
        WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
        WorkflowActionGrant.action_key == WORKFLOW_WAIT_YOUTUBE_ACTION_KEY,
    )
    if for_update:
        statement = statement.with_for_update()
    grant = (await db.execute(statement)).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None or grant.consumed_at is not None:
        raise _denied()
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    declaration = workflow_wait_youtube_publication_authorization(scope.get("declaration"))
    root_run = await db.get(WorkflowRun, scope.get("approval_root_run_id"))
    executor_run = await db.get(WorkflowRun, workflow_run_id)
    original_executor_run = await db.get(WorkflowRun, grant.workflow_run_id)
    executor_retry_chain_is_valid = False
    retry_cursor = executor_run
    seen_retry_ids: set[str] = set()
    while retry_cursor is not None and retry_cursor.id not in seen_retry_ids:
        seen_retry_ids.add(retry_cursor.id)
        if retry_cursor.id == grant.workflow_run_id:
            executor_retry_chain_is_valid = True
            break
        retry_of_run_id = str(retry_cursor.effective_retry_of_run_id or "").strip()
        if not retry_of_run_id:
            break
        retry_cursor = await db.get(WorkflowRun, retry_of_run_id)
    root_snapshot = (
        root_run.execution_snapshot if root_run is not None and isinstance(root_run.execution_snapshot, dict) else {}
    )
    executor_snapshot = (
        executor_run.execution_snapshot
        if executor_run is not None and isinstance(executor_run.execution_snapshot, dict)
        else {}
    )
    wait_step_id = str(scope.get("approval_wait_step_id") or "")
    wait_step = next(
        (
            step
            for step in root_snapshot.get("steps") or []
            if isinstance(step, dict) and str(step.get("id") or "") == wait_step_id
        ),
        None,
    )
    approval = (
        (root_run.step_results or {}).get(wait_step_id)
        if root_run is not None and isinstance(root_run.step_results, dict)
        else None
    )
    approval = approval if isinstance(approval, dict) else {}
    child_trigger = (
        original_executor_run.trigger_data
        if original_executor_run is not None and isinstance(original_executor_run.trigger_data, dict)
        else {}
    )
    parent_step_id = str(child_trigger.get("parent_step_id") or "").strip()
    parent_step_result = (
        (root_run.step_results or {}).get(parent_step_id)
        if root_run is not None and isinstance(root_run.step_results, dict)
        else None
    )
    parent_step_result = parent_step_result if isinstance(parent_step_result, dict) else {}
    root_run_state_is_valid = bool(
        root_run is not None
        and (
            root_run.status in {"running", "paused"}
            or (
                root_run.status == "failed"
                and root_run.current_step_id == parent_step_id
                and parent_step_result.get("status") == "failed"
                and parent_step_result.get("subrun_id") == grant.workflow_run_id
            )
        )
    )
    trusted = (
        executor_run.trigger_data.get("_workflow_runtime_context")
        if executor_run is not None
        and isinstance(executor_run.trigger_data, dict)
        and isinstance(
            executor_run.trigger_data.get("_workflow_runtime_context"),
            dict,
        )
        else {}
    )
    if (
        scope.get("source") != "workflow_wait"
        or declaration is None
        or root_run is None
        or executor_run is None
        or original_executor_run is None
        or not executor_retry_chain_is_valid
        or scope.get("executor_run_id") != original_executor_run.id
        or grant.workflow_lineage_root_run_id != original_executor_run.id
        or root_run.entity_id != entity_id
        or root_run.workspace_id != workspace_id
        or not root_run_state_is_valid
        or str(root_run.started_by or "") != user_id
        or root_snapshot.get("fingerprint") != scope.get("approval_root_fingerprint")
        or wait_step is None
        or wait_step.get("type") != "wait"
        or approval.get("status") != "completed"
        or approval.get("approved") is not True
        or approval.get("approved_by") != user_id
        or approval.get("approved_at") != scope.get("approved_at")
        or executor_run.entity_id != entity_id
        or executor_run.workspace_id != workspace_id
        or executor_run.workflow_id != scope.get("executor_workflow_id")
        or executor_run.status != "running"
        or str(executor_run.started_by or "") != user_id
        or executor_run.lineage_status != "canonical"
        or str(executor_run.lineage_root_run_id or executor_run.id) != workflow_lineage_root_run_id
        or executor_snapshot.get("fingerprint") != scope.get("executor_workflow_fingerprint")
        or str(child_trigger.get("parent_run_id") or "") != root_run.id
        or trusted.get("workflow_action_grant_id") != grant.id
        or not await _workflow_wait_publication_artifact_is_current(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            declaration=declaration,
        )
    ):
        raise _denied()
    return grant, declaration


async def validate_workflow_wait_youtube_publication_grant(
    db: AsyncSession,
    *,
    provider_request: dict[str, Any],
    for_update: bool = False,
    **kwargs: Any,
) -> tuple[WorkflowActionGrant, str]:
    grant, declaration = await _validate_workflow_wait_youtube_publication_scope(
        db,
        for_update=for_update,
        **kwargs,
    )
    request_kind = youtube_publication_provider_request_kind(
        provider_request,
        allow_upload_sources=True,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    if request_kind in {"upload_entry", "upload_transfer"}:
        if workflow_step_id != declaration["upload_step_id"]:
            raise _denied()
        if request_kind == "upload_transfer":
            arguments = provider_request.get("retry_arguments")
            if _upload_uses_document_ids(arguments):
                if not _upload_document_ids_match_artifact(arguments, declaration=declaration):
                    raise _denied()
            elif _upload_uses_sources(arguments):
                if not _upload_sources_match_artifact(
                    arguments,
                    entity_id=str(kwargs.get("entity_id") or ""),
                    declaration=declaration,
                ):
                    raise _denied()
            elif not _upload_files_match_preparation(
                _upload_argument_files(arguments),
                scope=grant.scope,
                declaration=declaration,
            ):
                raise _denied()
    elif request_kind in {"publish", "save"}:
        visibility = declaration["publication"]["visibility"]
        expected_kind = "publish" if visibility == "public" else "save"
        if workflow_step_id != declaration["publish_step_id"] or request_kind != expected_kind:
            raise _denied()
    else:
        raise _denied()
    return grant, request_kind


async def consume_workflow_wait_youtube_publication_grant(
    db: AsyncSession,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, request_kind = await validate_workflow_wait_youtube_publication_grant(
        db,
        for_update=True,
        **kwargs,
    )
    visibility = grant.scope["declaration"]["publication"]["visibility"]
    expected_kind = "publish" if visibility == "public" else "save"
    if request_kind != expected_kind:
        raise _denied()
    grant.consumed_at = datetime.now(timezone.utc)
    grant.consumed_by_run_id = str(kwargs.get("workflow_run_id") or "")
    await db.flush()
    return grant


async def validate_workflow_wait_youtube_publication_step(
    db: AsyncSession,
    *,
    browser_action: str,
    browser_arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, declaration = await _validate_workflow_wait_youtube_publication_scope(
        db,
        **kwargs,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    allowed_actions = (
        {"click_element", "upload"}
        if workflow_step_id == declaration["upload_step_id"]
        else {"click_element", "fill_or_select"}
        if workflow_step_id == declaration["publish_step_id"]
        else set()
    )
    if browser_action not in allowed_actions:
        raise _denied()
    if browser_action == "upload":
        if _upload_uses_document_ids(browser_arguments):
            if not _upload_document_ids_match_artifact(browser_arguments, declaration=declaration):
                raise _denied()
        elif not _upload_files_match_preparation(
            _upload_argument_files(browser_arguments), scope=grant.scope, declaration=declaration
        ):
            raise _denied()
    return grant


async def _validate_workflow_execution_youtube_publication_scope(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    workflow_run_id: str,
    workflow_lineage_root_run_id: str,
    workflow_step_id: str,
    for_update: bool = False,
) -> tuple[WorkflowActionGrant, dict[str, Any]]:
    from packages.core.services.workflow_run_trace import execution_snapshot_for_run

    statement = select(WorkflowActionGrant).where(
        WorkflowActionGrant.id == grant_id,
        WorkflowActionGrant.entity_id == entity_id,
        WorkflowActionGrant.workspace_id == workspace_id,
        WorkflowActionGrant.granted_by == user_id,
        WorkflowActionGrant.workflow_lineage_root_run_id == workflow_lineage_root_run_id,
        WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
        WorkflowActionGrant.action_key == WORKFLOW_WAIT_YOUTUBE_ACTION_KEY,
    )
    if for_update:
        statement = statement.with_for_update()
    grant = (await db.execute(statement)).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None or grant.consumed_at is not None:
        raise _denied()
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    declaration = workflow_wait_youtube_publication_authorization(scope.get("declaration"))
    run = await db.get(WorkflowRun, workflow_run_id)
    workflow = await db.get(WorkflowDefinition, run.workflow_id) if run is not None else None
    snapshot = (execution_snapshot_for_run(workflow, run) or {}) if run is not None else {}
    grant_step_id = str(scope.get("grant_step_id") or "")
    grant_step = next(
        (
            step
            for step in snapshot.get("steps") or []
            if isinstance(step, dict) and str(step.get("id") or "") == grant_step_id
        ),
        None,
    )
    configured = ((grant_step or {}).get("config") or {}).get("declaration") if isinstance(grant_step, dict) else None
    trusted = (
        run.trigger_data.get("_workflow_runtime_context")
        if run is not None
        and isinstance(run.trigger_data, dict)
        and isinstance(run.trigger_data.get("_workflow_runtime_context"), dict)
        else {}
    )
    fingerprint = snapshot.get("fingerprint")
    video_consumed = bool(scope.get("video_consumed_at"))
    post_publish_asset_step_id = (
        declaration.get("post_publish_asset_step_id") if declaration else None
    )
    if (
        scope.get("source") != "workflow_execution"
        or declaration is None
        or run is None
        or workflow is None
        or workflow.entity_id != entity_id
        or run.status != "running"
        or run.entity_id != entity_id
        or run.workspace_id != workspace_id
        or str(run.started_by or "") != user_id
        or run.lineage_is_legacy is not False
        or str(run.lineage_root_run_id or run.id) != workflow_lineage_root_run_id
        or scope.get("executor_run_id") != grant.workflow_run_id
        or not await _execution_publication_retry_chain_is_valid(
            db,
            run,
            grant_run_id=grant.workflow_run_id,
        )
        or scope.get("executor_workflow_id") != workflow.id
        or scope.get("executor_workflow_fingerprint") != fingerprint
        or not fingerprint
        or not isinstance(grant_step, dict)
        or grant_step.get("type") != "workflow_action_grant"
        or not isinstance(configured, dict)
        or configured.get("source") != "workflow_execution"
        or configured.get("kind") != YOUTUBE_PUBLICATION_GRANT_TYPE
        or trusted.get("workflow_action_grant_id") != grant.id
        or (video_consumed and workflow_step_id != post_publish_asset_step_id)
        or not _workflow_execution_channel_is_observed(run, declaration)
        or not await _workflow_execution_publication_artifact_is_current(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            run_id=run.id,
            declaration=declaration,
            video_consumed=video_consumed,
        )
    ):
        raise _denied()
    return grant, declaration


async def validate_workflow_execution_youtube_publication_step(
    db: AsyncSession,
    *,
    browser_action: str,
    browser_arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, declaration = await _validate_workflow_execution_youtube_publication_scope(
        db,
        **kwargs,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    allowed_actions = (
        # Upload recovery may search existing drafts before transferring bytes.
        # Final publication still requires its declared publication step.
        {"click_element", "fill_or_select", "upload"}
        if workflow_step_id == declaration["upload_step_id"]
        else {"click_element", "fill_or_select"}
        if workflow_step_id == declaration["publish_step_id"]
        else set()
    )
    if workflow_step_id == declaration.get("post_publish_asset_step_id"):
        # A thumbnail attachment has no authority to alter video metadata.
        # Studio still requires a click to select the image control and save it.
        allowed_actions = {"click_element", "upload"}
    if browser_action not in allowed_actions:
        raise _denied()
    if browser_action == "upload":
        if workflow_step_id == declaration.get("post_publish_asset_step_id"):
            if not _upload_document_ids_match_publication_asset(
                browser_arguments, declaration=declaration,
            ):
                raise _denied()
        elif _upload_uses_document_ids(browser_arguments):
            if not _upload_document_ids_match_artifact(browser_arguments, declaration=declaration):
                raise _denied()
        elif not _upload_files_match_preparation(
            _upload_argument_files(browser_arguments),
            scope=grant.scope,
            declaration=declaration,
        ):
            raise _denied()
    return grant


async def validate_workflow_execution_youtube_upload_preparation(
    db: AsyncSession,
    *,
    source: str,
    **kwargs: Any,
) -> dict[str, str]:
    _grant, declaration = await _validate_workflow_execution_youtube_publication_scope(
        db,
        **kwargs,
    )
    if str(kwargs.get("workflow_step_id") or "") != declaration["upload_step_id"] or _normalized_knowledge_source(
        source
    ) != _normalized_knowledge_source(declaration["artifact"]["fs_path"]):
        raise _denied()
    return dict(declaration["artifact"])


async def record_workflow_execution_youtube_upload_preparation(
    db: AsyncSession,
    *,
    source: str,
    local_path: str,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, declaration = await _validate_workflow_execution_youtube_publication_scope(
        db,
        for_update=True,
        **kwargs,
    )
    artifact = declaration["artifact"]
    if (
        str(kwargs.get("workflow_step_id") or "") != declaration["upload_step_id"]
        or _normalized_knowledge_source(source) != _normalized_knowledge_source(artifact["fs_path"])
        or not _upload_files_match_filename([local_path], artifact["filename"])
    ):
        raise _denied()
    grant.scope = {
        **grant.scope,
        "upload_preparation": {
            "source": artifact["fs_path"],
            "local_path": local_path,
        },
    }
    await db.flush()
    return grant


def youtube_studio_channel_url_matches(url: str, channel_id: str) -> bool:
    parsed = urlparse(url)
    channel_path = f"/channel/{channel_id}"
    return bool(
        channel_id
        and parsed.scheme == "https"
        and parsed.hostname == "studio.youtube.com"
        and (parsed.path == channel_path or parsed.path.startswith(channel_path + "/"))
    )


def youtube_studio_video_url_matches(url: str, video_id: str) -> bool:
    """Match the one Studio edit page that owns a declared attachment."""
    parsed = urlparse(url)
    return bool(
        _YOUTUBE_VIDEO_ID.fullmatch(video_id)
        and parsed.scheme == "https"
        and parsed.hostname == "studio.youtube.com"
        and parsed.path == f"/video/{video_id}/edit"
    )


async def workflow_execution_post_publish_asset_target_video_id(
    db: AsyncSession,
    *,
    declaration: dict[str, Any],
) -> str | None:
    """Resolve the verified external object that a declared asset may modify."""
    asset = declaration.get("post_publish_asset")
    if not isinstance(asset, dict):
        return None
    project = await db.get(WorkflowProject, asset["workflow_project_id"])
    project_state = project.state if project is not None and isinstance(project.state, dict) else {}
    return _post_publish_asset_target_video_id(project_state, asset)


async def validate_workflow_execution_youtube_publication_grant(
    db: AsyncSession,
    *,
    provider_request: dict[str, Any],
    for_update: bool = False,
    **kwargs: Any,
) -> tuple[WorkflowActionGrant, str]:
    grant, declaration = await _validate_workflow_execution_youtube_publication_scope(
        db,
        for_update=for_update,
        **kwargs,
    )
    request_kind = youtube_publication_provider_request_kind(
        provider_request,
        allow_upload_sources=True,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    is_post_publish_asset_step = workflow_step_id == declaration.get("post_publish_asset_step_id")
    if is_post_publish_asset_step:
        target_video_id = await workflow_execution_post_publish_asset_target_video_id(
            db,
            declaration=declaration,
        )
        if target_video_id is None or not youtube_studio_video_url_matches(
            str(provider_request.get("url") or ""),
            target_video_id,
        ):
            raise _denied()
        if request_kind == "upload_transfer":
            if not _upload_document_ids_match_publication_asset(
                provider_request.get("retry_arguments"),
                declaration=declaration,
            ):
                raise _denied()
            return grant, "post_publish_asset_upload"
        if request_kind == "save":
            return grant, "post_publish_asset_save"
        raise _denied()
    if not youtube_studio_channel_url_matches(
        str(provider_request.get("url") or ""),
        declaration["channel_id"],
    ):
        raise _denied()
    if request_kind in {"upload_entry", "upload_transfer"}:
        if workflow_step_id != declaration["upload_step_id"]:
            raise _denied()
        arguments = provider_request.get("retry_arguments")
        if request_kind == "upload_transfer":
            if _upload_uses_document_ids(arguments):
                if not _upload_document_ids_match_artifact(arguments, declaration=declaration):
                    raise _denied()
            elif _upload_uses_sources(arguments):
                if not _upload_sources_match_artifact(
                    arguments,
                    entity_id=str(kwargs.get("entity_id") or ""),
                    declaration=declaration,
                ):
                    raise _denied()
            elif not _upload_files_match_preparation(
                _upload_argument_files(arguments),
                scope=grant.scope,
                declaration=declaration,
            ):
                raise _denied()
    elif request_kind in {"publish", "save"}:
        expected_kind = "publish" if declaration["publication"]["visibility"] == "public" else "save"
        if workflow_step_id != declaration["publish_step_id"] or request_kind != expected_kind:
            raise _denied()
    else:
        raise _denied()
    return grant, request_kind


async def consume_workflow_execution_youtube_publication_grant(
    db: AsyncSession,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, request_kind = await validate_workflow_execution_youtube_publication_grant(
        db,
        for_update=True,
        **kwargs,
    )
    if request_kind not in {"publish", "save"}:
        raise _denied()
    declaration = grant.scope.get("declaration") if isinstance(grant.scope, dict) else None
    # A declared attachment remains narrowly usable only by its own final
    # Workflow step. That step revokes the grant after recording its receipt.
    if isinstance(declaration, dict) and declaration.get("post_publish_asset"):
        grant.scope = {
            **grant.scope,
            "video_consumed_at": datetime.now(timezone.utc).isoformat(),
            "video_consumed_by_run_id": str(kwargs.get("workflow_run_id") or ""),
        }
        await db.flush()
        return grant
    grant.consumed_at = datetime.now(timezone.utc)
    grant.consumed_by_run_id = str(kwargs.get("workflow_run_id") or "")
    await db.flush()
    return grant


async def validate_workflow_wait_youtube_upload_preparation(
    db: AsyncSession,
    *,
    source: str,
    **kwargs: Any,
) -> dict[str, str]:
    """Authorize local preparation of the exact approved publication artifact."""

    _grant, declaration = await _validate_workflow_wait_youtube_publication_scope(
        db,
        **kwargs,
    )
    if str(kwargs.get("workflow_step_id") or "") != declaration["upload_step_id"] or _normalized_knowledge_source(
        source
    ) != _normalized_knowledge_source(declaration["artifact"]["fs_path"]):
        raise _denied()
    return dict(declaration["artifact"])


async def record_workflow_wait_youtube_upload_preparation(
    db: AsyncSession,
    *,
    source: str,
    local_path: str,
    **kwargs: Any,
) -> WorkflowActionGrant:
    """Persist the exact approved source and returned local upload path."""

    grant, declaration = await _validate_workflow_wait_youtube_publication_scope(
        db,
        for_update=True,
        **kwargs,
    )
    artifact = declaration["artifact"]
    if (
        str(kwargs.get("workflow_step_id") or "") != declaration["upload_step_id"]
        or _normalized_knowledge_source(source) != _normalized_knowledge_source(artifact["fs_path"])
        or not _upload_files_match_filename([local_path], artifact["filename"])
    ):
        raise _denied()
    grant.scope = {
        **grant.scope,
        "upload_preparation": {
            "source": artifact["fs_path"],
            "local_path": local_path,
        },
    }
    await db.flush()
    return grant


async def validate_youtube_publication_step(
    db: AsyncSession,
    *,
    grant_id: str,
    browser_arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant = await db.get(WorkflowActionGrant, grant_id)
    scope = grant.scope if grant is not None and isinstance(grant.scope, dict) else {}
    if scope.get("source") == "workflow_execution":
        return await validate_workflow_execution_youtube_publication_step(
            db,
            grant_id=grant_id,
            browser_arguments=browser_arguments,
            **kwargs,
        )
    if scope.get("source") == "workflow_wait":
        return await validate_workflow_wait_youtube_publication_step(
            db,
            grant_id=grant_id,
            browser_arguments=browser_arguments,
            **kwargs,
        )
    return await validate_proposal_youtube_publication_step(
        db,
        grant_id=grant_id,
        **kwargs,
    )


async def validate_publication_workflow_browser_action(
    db: AsyncSession,
    *,
    grant_id: str,
    workflow_step_id: str,
    browser_action: str,
    browser_arguments: dict[str, Any],
    **kwargs: Any,
) -> bool | None:
    """Validate a publication grant without coupling the browser transport to a platform."""

    grant = await db.get(WorkflowActionGrant, grant_id)
    if grant is None:
        raise _denied()
    if grant.grant_type == "browser_capture":
        return None
    if grant.grant_type != YOUTUBE_PUBLICATION_GRANT_TYPE:
        raise WorkflowActionGrantDenied("Unsupported Workflow action grant type")
    if browser_action not in {"click_element", "fill_or_select", "upload"}:
        # The publication grant contributes no authority to other Chrome Skill
        # tools; their normal Runtime/provider authorization remains in force.
        return False

    declaration = (grant.scope or {}).get("declaration") if isinstance(grant.scope, dict) else None
    declared_steps = {
        str((declaration or {}).get("upload_step_id") or ""),
        str((declaration or {}).get("publish_step_id") or ""),
    }
    post_publish_asset_step_id = str((declaration or {}).get("post_publish_asset_step_id") or "")
    if post_publish_asset_step_id:
        declared_steps.add(post_publish_asset_step_id)
    if workflow_step_id not in declared_steps:
        raise _denied()
    await validate_youtube_publication_step(
        db,
        grant_id=grant_id,
        workflow_step_id=workflow_step_id,
        browser_action=browser_action,
        browser_arguments=browser_arguments,
        **kwargs,
    )
    return True


async def validate_youtube_publication_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    **kwargs: Any,
) -> tuple[WorkflowActionGrant, str]:
    grant = await db.get(WorkflowActionGrant, grant_id)
    scope = grant.scope if grant is not None and isinstance(grant.scope, dict) else {}
    if scope.get("source") == "workflow_execution":
        return await validate_workflow_execution_youtube_publication_grant(
            db,
            grant_id=grant_id,
            **kwargs,
        )
    if scope.get("source") == "workflow_wait":
        return await validate_workflow_wait_youtube_publication_grant(
            db,
            grant_id=grant_id,
            **kwargs,
        )
    return await validate_proposal_youtube_publication_grant(
        db,
        grant_id=grant_id,
        **kwargs,
    )


async def consume_youtube_publication_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant = await db.get(WorkflowActionGrant, grant_id)
    scope = grant.scope if grant is not None and isinstance(grant.scope, dict) else {}
    if scope.get("source") == "workflow_execution":
        return await consume_workflow_execution_youtube_publication_grant(
            db,
            grant_id=grant_id,
            **kwargs,
        )
    if scope.get("source") == "workflow_wait":
        return await consume_workflow_wait_youtube_publication_grant(
            db,
            grant_id=grant_id,
            **kwargs,
        )
    return await consume_proposal_youtube_publication_grant(
        db,
        grant_id=grant_id,
        **kwargs,
    )


async def create_workflow_action_grant(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    workflow_run_id: str,
    project_id: str,
    grant_type: str,
    scope: dict,
    granted_by: str,
    ttl_seconds: int = 86400,
) -> WorkflowActionGrant:
    now = datetime.now(timezone.utc)
    lifetime_seconds = max(1, min(int(ttl_seconds), 86400))
    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        grant_type=grant_type,
        scope=dict(scope),
        granted_by=granted_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=lifetime_seconds),
    )
    db.add(grant)
    await db.flush()
    return grant


async def validate_workflow_action_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    project_id: str,
    grant_type: str,
    plan_version: int,
    scene_id: str | None,
    action: str,
    allow_batch: bool = False,
) -> WorkflowActionGrant:
    grant = (
        await db.execute(
            select(WorkflowActionGrant).where(
                WorkflowActionGrant.id == grant_id,
                WorkflowActionGrant.entity_id == entity_id,
                WorkflowActionGrant.granted_by == user_id,
                WorkflowActionGrant.workspace_id == workspace_id,
                WorkflowActionGrant.project_id == project_id,
                WorkflowActionGrant.grant_type == grant_type,
            )
        )
    ).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        raise _denied()

    now = datetime.now(timezone.utc)
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    if scope.get("approved_plan_version") != plan_version:
        raise _denied()
    scene_ids = scope.get("scene_ids")
    if not isinstance(scene_ids, list) or not scene_ids:
        raise _denied()
    if scene_id:
        if scene_id not in scene_ids:
            raise _denied()
    elif not allow_batch:
        raise _denied()
    allowed_actions = scope.get("allowed_actions")
    if not isinstance(allowed_actions, list) or action not in allowed_actions:
        raise _denied()
    return grant


async def revoke_workflow_action_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    workspace_id: str,
) -> WorkflowActionGrant:
    grant = (
        await db.execute(
            select(WorkflowActionGrant)
            .where(
                WorkflowActionGrant.id == grant_id,
                WorkflowActionGrant.entity_id == entity_id,
                WorkflowActionGrant.workspace_id == workspace_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None:
        raise _denied()
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    return grant


async def revoke_current_workflow_action_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    workflow_run_id: str,
    project_id: str,
) -> WorkflowActionGrant:
    """Revoke one unconsumed grant bound to the active run and project."""

    grant = (
        await db.execute(
            select(WorkflowActionGrant)
            .where(
                WorkflowActionGrant.id == grant_id,
                WorkflowActionGrant.entity_id == entity_id,
                WorkflowActionGrant.granted_by == user_id,
                WorkflowActionGrant.workspace_id == workspace_id,
                WorkflowActionGrant.workflow_run_id == workflow_run_id,
                WorkflowActionGrant.project_id == project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None or grant.consumed_at is not None:
        raise _denied()
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    return grant
