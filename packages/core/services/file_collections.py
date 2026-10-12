"""Host-verified file collections and exact source-media archives."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransactionFactory
from packages.core.services.artifact_delivery import (
    ArtifactDeliveryError,
    verify_artifact_delivery_refs,
)
from packages.core.services.file_type_detection import mime_for_extension


FILE_COLLECTION_SCHEMA_VERSION = "workflow-file-collection/v1"
_ARCHIVE_ROLES = ("source_audio", "source_cover")


class FileCollectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _role(value: Any) -> str:
    role = _text(value)
    if not role or role != role.lower() or not role.replace("_", "").isalnum():
        raise FileCollectionError("file_role_invalid", "Each file role must be a non-empty snake_case label.")
    return role


def _roles(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise FileCollectionError("required_roles_invalid", "required_roles must be an array of snake_case labels.")
    output: list[str] = []
    for value in values:
        role = _role(value)
        if role not in output:
            output.append(role)
    return output


def _source_role_matches_mime(role: str, mime_type: str) -> bool:
    if role == "source_audio":
        return mime_type.startswith("audio/")
    if role == "source_cover":
        return mime_type.startswith("image/")
    return True


def _registered_media_type(document: Document) -> str:
    mime_type = _text(document.mime_type).lower()
    if mime_type and mime_type != "application/octet-stream":
        return mime_type
    for candidate in (_text(document.file_type), Path(_text(document.name)).suffix.lstrip(".")):
        inferred = mime_for_extension(candidate.lower())
        if inferred != "application/octet-stream":
            return inferred
    return mime_type or "application/octet-stream"


def _expected_sources(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        raise FileCollectionError("expected_sources_invalid", "expected_sources must contain the fixed source receipts for this run.")
    expected: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise FileCollectionError("expected_sources_invalid", "Each expected source must include document_id and role.")
        role = _role(item.get("role"))
        document_id = _text(item.get("document_id"))
        source_sha256 = _text(item.get("source_sha256")).lower()
        if role not in {"source_audio", "source_cover"} or not document_id:
            raise FileCollectionError("expected_sources_invalid", "Expected sources must be source_audio/source_cover Document receipts.")
        if source_sha256 and (len(source_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_sha256)):
            raise FileCollectionError("expected_sources_invalid", "An expected source SHA-256 digest is invalid.")
        if role in expected:
            raise FileCollectionError("expected_sources_invalid", "Each expected source role may appear once.")
        expected[role] = {"document_id": document_id, "source_sha256": source_sha256}
    if set(expected) != {"source_audio", "source_cover"}:
        raise FileCollectionError("expected_sources_invalid", "Expected source receipts must include source_audio and source_cover.")
    return expected


async def verify_file_collection(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    files: Any,
    required_roles: Any,
    expected_sources: Any,
    confirmed_version: Any,
    title: Any,
    summary: Any,
    pending_items: Any = None,
    verification_scope: str = "delivery",
) -> dict[str, Any]:
    """Verify explicit documents and return only canonical receipt metadata."""
    if not entity_id or not workspace_id:
        raise FileCollectionError("missing_workspace_context", "Entity and Workspace context are required.")
    if not isinstance(files, list) or not files:
        raise FileCollectionError("file_collection_missing", "files must be a non-empty array of explicit Document references.")
    if len(files) > 32:
        raise FileCollectionError("file_collection_limit", "A file collection may contain at most 32 files.")
    if pending_items is not None and not isinstance(pending_items, list):
        raise FileCollectionError("pending_items_invalid", "pending_items must be an array when supplied.")

    if verification_scope not in {"preparation", "delivery"}:
        raise FileCollectionError("verification_scope_invalid", "verification_scope must be preparation or delivery.")
    required = _roles(required_roles)
    if verification_scope == "preparation":
        if expected_sources != []:
            raise FileCollectionError("expected_sources_invalid", "Preparation checks require empty expected_sources; use delivery to verify source bindings.")
        expected = {}
    else:
        expected = _expected_sources(expected_sources)
    version = _text(confirmed_version)
    if not version:
        raise FileCollectionError("confirmed_version_invalid", "confirmed_version must be the fixed approved release or workflow version.")
    candidates: list[dict[str, str]] = []
    document_roles: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise FileCollectionError("file_reference_invalid", "Each file must be an object with document_id, role, and group.")
        document_id = _text(item.get("document_id"))
        if not document_id:
            raise FileCollectionError("file_reference_invalid", "Each file must include a document_id.")
        role = _role(item.get("role"))
        group = _text(item.get("group"))
        if not group:
            raise FileCollectionError("file_group_invalid", "Each file must include a group label.")
        prior = document_roles.get(document_id)
        if prior is not None and prior != role:
            raise FileCollectionError("file_role_conflict", "One Document cannot satisfy more than one collection role.")
        document_roles[document_id] = role
        if prior is None:
            candidates.append({"document_id": document_id, "role": role, "group": group})

    candidate_by_role = {candidate["role"]: candidate for candidate in candidates}
    for role, receipt in expected.items():
        candidate = candidate_by_role.get(role)
        if candidate is None or candidate["document_id"] != receipt["document_id"]:
            raise FileCollectionError("source_binding_missing", f"{role} must be the source receipt selected for this run.")
    for candidate in candidates:
        receipt = expected.get(candidate["role"])
        if receipt is not None and candidate["document_id"] != receipt["document_id"]:
            raise FileCollectionError("source_binding_missing", f"{candidate['role']} must be the source receipt selected for this run.")

    try:
        verified = await verify_artifact_delivery_refs(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            refs=[{
                "document_id": candidate["document_id"],
                **({"source_sha256": expected[candidate["role"]]["source_sha256"]} if expected.get(candidate["role"], {}).get("source_sha256") else {}),
            } for candidate in candidates],
        )
    except ArtifactDeliveryError as exc:
        raise FileCollectionError(exc.code, str(exc)) from exc

    snapshots = {snapshot["document_id"]: snapshot for snapshot in verified.snapshots}
    documents = list((await db.execute(select(Document).where(
        Document.entity_id == entity_id,
        Document.id.in_(snapshots),
        Document.is_trashed.is_(False),
    ).execution_options(populate_existing=True))).scalars().all())
    by_id = {document.id: document for document in documents}
    if set(by_id) != set(snapshots):
        raise FileCollectionError("artifact_changed", "A verified Document changed during collection assembly.")

    canonical_by_id = {
        ref["document_id"]: ref
        for ref in verified.projection.refs
        if isinstance(ref, dict) and _text(ref.get("document_id"))
    }
    output_files: list[dict[str, Any]] = []
    for candidate in candidates:
        document_id = candidate["document_id"]
        document = by_id.get(document_id)
        snapshot = snapshots.get(document_id)
        ref = canonical_by_id.get(document_id)
        if (
            document is None
            or snapshot is None
            or ref is None
            or (document.updated_at.isoformat() if document.updated_at else "")
            != snapshot.get("document_updated_at")
        ):
            raise FileCollectionError("artifact_changed", "A verified Document changed during collection assembly.")
        mime_type = _registered_media_type(document)
        if not _source_role_matches_mime(candidate["role"], mime_type):
            raise FileCollectionError(
                "source_media_type_invalid",
                f"{candidate['role']} must reference a Document with a matching registered MIME type.",
            )
        output_files.append({
            "document_id": document.id,
            "name": document.name,
            "role": candidate["role"],
            "group": candidate["group"],
            "mime_type": mime_type,
            "source_sha256": snapshot["source_sha256"],
            "size": snapshot["size"],
            "viewer_url": ref.get("viewer_url") or ref.get("open_url"),
        })

    actual_roles = {item["role"] for item in output_files}
    missing_roles = [role for role in required if role not in actual_roles]
    verified_collection = not missing_roles
    return {
        "schema_version": FILE_COLLECTION_SCHEMA_VERSION,
        "confirmed_version": version,
        "title": _text(title),
        "summary": _text(summary),
        "files": output_files,
        "pending_items": list(pending_items or []),
        "missing_roles": missing_roles,
        "delivery_verified": verified_collection and verification_scope == "delivery",
        "preparation_completed": verified_collection,
    }


def _archive_name(source_path: str, role: str, used_names: set[str]) -> str:
    name = Path(source_path).name
    if not name or name in {".", ".."}:
        name = f"{role}.bin"
    if name not in used_names:
        used_names.add(name)
        return name
    suffix = Path(name).suffix
    stem = Path(name).stem or role
    name = f"{stem}-{role}{suffix}"
    used_names.add(name)
    return name


def archive_file_collection_target_names(source_paths: dict[str, str]) -> dict[str, str]:
    used_names: set[str] = set()
    return {
        role: _archive_name(source_paths[role], role, used_names)
        for role in _ARCHIVE_ROLES
    }


async def archive_file_collection(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    workflow_run_id: str | None,
    workflow_lineage_root_run_id: str | None,
    collection: Any,
    write_guard: Callable[[list[str]], Awaitable[str | None]] | None = None,
) -> dict[str, Any]:
    """Copy verified source media into the trusted Workflow lineage archive."""
    run_id = _text(workflow_run_id)
    lineage = _text(workflow_lineage_root_run_id)
    if not entity_id or not workspace_id or not run_id or not lineage:
        raise FileCollectionError("missing_workflow_context", "Trusted Entity, Workspace, Workflow run, and lineage context are required.")
    if not isinstance(collection, dict) or not collection.get("delivery_verified"):
        raise FileCollectionError("file_collection_unverified", "Archive requires a verified file collection.")
    files = collection.get("files")
    if not isinstance(files, list):
        raise FileCollectionError("file_collection_invalid", "Archive requires canonical collection files.")
    sources = {str(item.get("role") or ""): item for item in files if isinstance(item, dict)}
    if set(_ARCHIVE_ROLES) - set(sources):
        raise FileCollectionError("source_binding_missing", "Archive requires source_audio and source_cover receipts.")

    run = (await db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())).scalar_one_or_none()
    workspace = (await db.execute(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.entity_id == entity_id, Workspace.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if (
        run is None or workspace is None or run.entity_id != entity_id or run.workspace_id != workspace_id
        or _text(run.lineage_root_run_id) != lineage or not workspace.artifact_folder_id
    ):
        raise FileCollectionError("workflow_context_invalid", "Workflow archive context no longer matches the active run and Workspace.")

    source_entry_ids = {_text(sources[role].get("document_id")) for role in _ARCHIVE_ROLES}
    source_documents = {
        document.id: document
        for document in (await db.execute(select(Document).where(
            Document.entity_id == entity_id, Document.id.in_(source_entry_ids), Document.is_trashed.is_(False),
        ))).scalars().all()
    }
    source_ids: dict[str, str] = {}
    for role in _ARCHIVE_ROLES:
        entry_id = _text(sources[role].get("document_id"))
        document = source_documents.get(entry_id)
        provenance = document.metadata_.get("file_collection_archive") if document and isinstance(document.metadata_, dict) else None
        if isinstance(provenance, dict):
            source_ids[role] = (
                _text(provenance.get("source_document_id"))
                if provenance.get("role") == role and provenance.get("workflow_lineage_root_run_id") == lineage
                else entry_id
            )
        else:
            source_ids[role] = entry_id
    source_refs = [{"document_id": source_ids[role], "source_sha256": _text(sources[role].get("source_sha256"))} for role in _ARCHIVE_ROLES]
    if any(not ref["document_id"] or not ref["source_sha256"] for ref in source_refs):
        raise FileCollectionError("file_collection_invalid", "Archive requires canonical source document receipts.")
    try:
        verified_sources = await verify_artifact_delivery_refs(
            db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, refs=source_refs,
        )
    except ArtifactDeliveryError as exc:
        raise FileCollectionError(exc.code, str(exc)) from exc
    snapshots = {snapshot["document_id"]: snapshot for snapshot in verified_sources.snapshots}

    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.generated_media_naming import workspace_execution_artifact_dir
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    archive_dir = workspace_execution_artifact_dir(
        workspace_artifact_storage_base(workspace.artifact_folder_id), workflow_run_id=run_id,
        workflow_lineage_root_run_id=lineage, entity_root=get_entity_root(entity_id),
    )
    target_names = archive_file_collection_target_names({
        role: snapshots[source_ids[role]]["fs_path"] for role in _ARCHIVE_ROLES
    })
    targets = {role: f"{archive_dir}/{target_names[role]}" for role in _ARCHIVE_ROLES}
    archived: dict[str, dict[str, Any]] = {}
    for role in _ARCHIVE_ROLES:
        source_id = source_ids[role]
        snapshot = snapshots[source_id]
        target = targets[role]
        provenance = {
            "source_document_id": source_id, "source_sha256": snapshot["source_sha256"],
            "workflow_lineage_root_run_id": lineage, "role": role,
        }
        existing = (await db.execute(select(Document).where(
            Document.entity_id == entity_id, Document.fs_path == target, Document.is_trashed.is_(False),
        ))).scalar_one_or_none()
        if existing is not None:
            if not isinstance(existing.metadata_, dict) or existing.metadata_.get("file_collection_archive") != provenance:
                raise FileCollectionError("archive_target_collision", "Archive target already belongs to different content.")
            try:
                checked = await verify_artifact_delivery_refs(
                    db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
                    refs=[{"document_id": existing.id, "source_sha256": snapshot["source_sha256"]}],
                )
            except ArtifactDeliveryError as exc:
                raise FileCollectionError("archive_target_collision", "Existing archive bytes do not match the verified source.") from exc
            archived[role] = {
                "document_id": existing.id, "snapshot": checked.snapshots[0], "target": target,
                "viewer_url": checked.projection.refs[0].get("viewer_url") or checked.projection.refs[0].get("open_url"),
            }
            continue
        if write_guard is not None:
            blocked = await write_guard([target])
            if blocked:
                raise FileCollectionError("archive_write_not_authorized", blocked)
        async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
            copied = await transaction.copy_file(
                target, str(Path(get_entity_root(entity_id)) / snapshot["fs_path"]),
                expected_content_sha256=snapshot["source_sha256"], expected_size=snapshot["size"], require_missing=True,
            )
            sync = await transaction.project_file(
                abs_path=copied, entity_root=get_entity_root(entity_id), source="agent", created_by=user_id or "ai-agent",
                force=True, workspace_id=workspace_id, user_id=user_id, tool_name="archive_file_collection",
                expected_content_sha256=snapshot["source_sha256"],
            )
            document = await transaction.db.get(Document, sync.document_id)
            if document is None:
                raise FileCollectionError("archive_projection_failed", "Archive copy could not be projected to Knowledge.")
            metadata = dict(document.metadata_ or {})
            metadata["file_collection_archive"] = provenance
            document.metadata_ = metadata
            await transaction.commit()
        try:
            checked = await verify_artifact_delivery_refs(
                db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
                refs=[{"document_id": sync.document_id, "source_sha256": snapshot["source_sha256"]}],
            )
        except ArtifactDeliveryError as exc:
            raise FileCollectionError("archive_projection_failed", "Archive copy could not be reverified after projection.") from exc
        archived[role] = {
            "document_id": sync.document_id, "snapshot": checked.snapshots[0], "target": target,
            "viewer_url": checked.projection.refs[0].get("viewer_url") or checked.projection.refs[0].get("open_url"),
        }

    output = dict(collection)
    output_files: list[dict[str, Any]] = []
    for item in files:
        role = _text(item.get("role")) if isinstance(item, dict) else ""
        if role not in archived:
            output_files.append(item)
            continue
        archive = archived[role]
        snapshot = archive["snapshot"]
        output_files.append({
            **item, "document_id": archive["document_id"], "name": Path(archive["target"]).name,
            "source_sha256": snapshot["source_sha256"], "size": snapshot["size"],
            "viewer_url": archive["viewer_url"],
            "source_document_id": source_ids[role], "archive_path": archive["target"],
        })
    output["files"] = output_files
    output["archive_folder"] = archive_dir
    output["archive_lineage_root_run_id"] = lineage
    return output
