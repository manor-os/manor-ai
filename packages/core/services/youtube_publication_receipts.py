"""Stable Workspace receipt for the latest verified YouTube publication."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Literal

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
    runtime_write_entity_file_atomic,
)
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
)
from packages.core.services.youtube_public_video import (
    YouTubePublicVideoMetrics,
    canonicalize_youtube_public_url,
)


RECEIPT_DIRECTORY = "technical"
RECEIPT_FILENAME = "latest-youtube-publication.json"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "video_id",
        "public_url",
        "title",
        "channel_id",
        "channel_name",
        "published_at",
        "recorded_at",
        "source_kind",
        "source_task_id",
    }
)
PublicationSourceKind = Literal["workspace_publication", "verified_backfill"]


class YouTubePublicationReceiptError(ValueError):
    """A stable, user-reportable receipt validation or storage failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _required_text(receipt: dict[str, Any], key: str) -> str:
    value = str(receipt.get(key) or "").strip()
    if not value:
        raise YouTubePublicationReceiptError(
            "invalid_receipt",
            f"Publication receipt is missing {key}",
        )
    return value


def _validate_aware_timestamp(receipt: dict[str, Any], key: str) -> str:
    value = _required_text(receipt, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise YouTubePublicationReceiptError(
            "invalid_receipt",
            f"Publication receipt has an invalid {key}",
        ) from exc
    if parsed.tzinfo is None:
        raise YouTubePublicationReceiptError(
            "invalid_receipt",
            f"Publication receipt {key} must include a timezone",
        )
    return value


def validate_publication_receipt(receipt: Any) -> dict[str, Any]:
    """Validate and normalize the version-one publication receipt contract."""

    if not isinstance(receipt, dict):
        raise YouTubePublicationReceiptError(
            "invalid_receipt",
            "Publication receipt must be a JSON object",
        )
    missing = RECEIPT_KEYS - set(receipt)
    if missing:
        raise YouTubePublicationReceiptError(
            "invalid_receipt",
            f"Publication receipt is missing {sorted(missing)[0]}",
        )
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise YouTubePublicationReceiptError(
            "unsupported_receipt",
            "Publication receipt schema version is unsupported",
        )

    video_id = _required_text(receipt, "video_id")
    public_url, url_video_id = canonicalize_youtube_public_url(
        _required_text(receipt, "public_url")
    )
    if url_video_id != video_id:
        raise YouTubePublicationReceiptError(
            "video_mismatch",
            "Publication receipt URL and video ID do not match",
        )

    source_kind = str(receipt.get("source_kind") or "").strip()
    source_task_id = receipt.get("source_task_id")
    source_workflow_run_id = receipt.get("source_workflow_run_id")
    if source_kind == "workspace_publication":
        source_task_id = str(source_task_id or "").strip() or None
        source_workflow_run_id = str(source_workflow_run_id or "").strip() or None
        if not source_task_id and not source_workflow_run_id:
            raise YouTubePublicationReceiptError(
                "missing_source_execution",
                "source_task_id or source_workflow_run_id is required for workspace publication",
            )
    elif source_kind == "verified_backfill":
        if source_task_id is not None or source_workflow_run_id is not None:
            raise YouTubePublicationReceiptError(
                "invalid_backfill_source",
                "verified backfill must not claim a source task or Workflow run",
            )
    else:
        raise YouTubePublicationReceiptError(
            "invalid_source_kind",
            "Publication receipt source_kind is invalid",
        )

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "video_id": video_id,
        "public_url": public_url,
        "title": _required_text(receipt, "title"),
        "channel_id": _required_text(receipt, "channel_id"),
        "channel_name": _required_text(receipt, "channel_name"),
        "published_at": _validate_aware_timestamp(receipt, "published_at"),
        "recorded_at": _validate_aware_timestamp(receipt, "recorded_at"),
        "source_kind": source_kind,
        "source_task_id": source_task_id,
        "source_workflow_run_id": source_workflow_run_id,
    }


def build_publication_receipt(
    *,
    metrics: YouTubePublicVideoMetrics,
    expected_title: str,
    expected_video_id: str = "",
    expected_channel_name: str = "",
    source_kind: PublicationSourceKind,
    source_task_id: str | None,
    source_workflow_run_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build a receipt only when public identity matches the verified Studio result."""

    title = str(expected_title or "").strip()
    video_id = str(expected_video_id or "").strip()
    channel_name = str(expected_channel_name or "").strip()
    if video_id and metrics.video_id != video_id:
        raise YouTubePublicationReceiptError(
            "video_mismatch",
            "Public video ID does not match the verified Studio video ID",
        )
    if metrics.title != title:
        raise YouTubePublicationReceiptError(
            "title_mismatch",
            "Public title does not match the verified Studio title",
        )
    if channel_name and metrics.channel_name != channel_name:
        raise YouTubePublicationReceiptError(
            "channel_mismatch",
            "Public channel does not match the verified Studio channel",
        )
    return validate_publication_receipt(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "video_id": metrics.video_id,
            "public_url": metrics.public_url,
            "title": metrics.title,
            "channel_id": metrics.channel_id,
            "channel_name": metrics.channel_name,
            "published_at": metrics.published_at,
            "recorded_at": recorded_at
            or datetime.now(timezone.utc).isoformat(),
            "source_kind": source_kind,
            "source_task_id": source_task_id,
            "source_workflow_run_id": source_workflow_run_id,
        }
    )


async def _receipt_location(
    *,
    entity_id: str,
    workspace_id: str,
) -> tuple[Any, str, str]:
    if not entity_id or not workspace_id:
        raise YouTubePublicationReceiptError(
            "missing_workspace_context",
            "Entity and Workspace context are required",
        )
    directory = await ensure_workspace_artifact_directory(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory_path=RECEIPT_DIRECTORY,
    )
    rel_path = f"{directory.storage_path}/{RECEIPT_FILENAME}"
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        raise YouTubePublicationReceiptError(
            "filesystem_unavailable",
            "Workspace filesystem is not enabled",
        )
    return directory, rel_path, entity_root


async def write_publication_receipt(
    *,
    entity_id: str,
    workspace_id: str,
    receipt: dict[str, Any],
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Atomically replace the latest Workspace publication receipt."""

    validated = validate_publication_receipt(receipt)
    directory, rel_path, entity_root = await _receipt_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    data = (
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    abs_path = runtime_write_entity_file_atomic(
        entity_id,
        rel_path,
        data,
        expected_size=len(data),
    )
    document = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source="youtube_publication",
        created_by=agent_id or "youtube_publication",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_youtube_publication",
    )
    document_id = (
        document.get("document_id") or document.get("id")
        if isinstance(document, dict)
        else getattr(document, "id", None)
    )
    return {
        "path": rel_path,
        "display_path": f"{directory.display_path}/{RECEIPT_FILENAME}",
        "document_id": document_id,
        "receipt": validated,
    }


async def read_publication_receipt(
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Read the latest receipt from its deterministic Workspace path."""

    _, rel_path, entity_root = await _receipt_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    abs_path = os.path.realpath(os.path.join(entity_root, rel_path))
    root = os.path.realpath(entity_root)
    if os.path.commonpath([root, abs_path]) != root:
        raise YouTubePublicationReceiptError(
            "invalid_receipt_path",
            "Publication receipt path escaped the Workspace filesystem",
        )
    if not os.path.isfile(abs_path):
        raise YouTubePublicationReceiptError(
            "receipt_missing",
            "No verified YouTube publication receipt exists for this Workspace",
        )
    try:
        with open(abs_path, encoding="utf-8") as receipt_file:
            receipt = json.load(receipt_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubePublicationReceiptError(
            "receipt_unreadable",
            "The YouTube publication receipt cannot be read",
        ) from exc
    return validate_publication_receipt(receipt)
