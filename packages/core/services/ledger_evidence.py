"""Domain-neutral evidence references for Workspace ledger records.

Ledger entries keep a stable reference to a Knowledge document (or an
external source record), not a copy of the evidence bytes.  The reference
captures the document/version identity and a content hash so later edits do
not silently change the evidence behind an old ledger entry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import os
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EvidenceReferenceError(ValueError):
    """Stable validation error for a ledger evidence reference."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class EvidenceType(StrEnum):
    KNOWLEDGE_DOCUMENT = "knowledge_document"
    EXTERNAL_RECORD = "external_record"


class EvidenceVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class LedgerEvidenceRef(BaseModel):
    """Immutable locator and fingerprint for one piece of ledger evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: NonEmptyText | None = None
    evidence_type: EvidenceType = EvidenceType.KNOWLEDGE_DOCUMENT
    workspace_id: NonEmptyText
    role: NonEmptyText
    document_id: NonEmptyText | None = None
    version_id: NonEmptyText | None = None
    version_number: int | None = Field(default=None, ge=1)
    logical_path: NonEmptyText | None = None
    external_uri: NonEmptyText | None = None
    source_record_id: NonEmptyText | None = None
    mime_type: NonEmptyText | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: Sha256 | None = None
    classification: NonEmptyText | None = None
    verification_status: EvidenceVerificationStatus = EvidenceVerificationStatus.UNVERIFIED
    captured_at: datetime | None = None
    verified_at: datetime | None = None
    source: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "LedgerEvidenceRef":
        if self.evidence_type == EvidenceType.KNOWLEDGE_DOCUMENT:
            if not self.document_id:
                raise ValueError("knowledge_document evidence requires document_id")
            if not self.sha256:
                raise ValueError("knowledge_document evidence requires sha256")
            if not (self.version_id or self.version_number):
                raise ValueError(
                    "knowledge_document evidence requires version_id or version_number"
                )
        elif not (self.external_uri or self.source_record_id):
            raise ValueError(
                "external_record evidence requires external_uri or source_record_id"
            )

        if self.verification_status == EvidenceVerificationStatus.VERIFIED and not self.verified_at:
            raise ValueError("verified evidence requires verified_at")
        if self.evidence_id is None:
            self.evidence_id = self._derived_id()
        return self

    def _derived_id(self) -> str:
        if self.evidence_type == EvidenceType.KNOWLEDGE_DOCUMENT:
            version = self.version_id or f"v{self.version_number}"
            return f"knowledge:{self.document_id}:{version}:{self.sha256}"
        return f"external:{self.source_record_id or self.external_uri}"


def normalize_evidence_refs(
    value: Any,
    *,
    workspace_id: str,
    require: bool = False,
) -> list[dict[str, Any]]:
    """Validate and normalize evidence references for one Workspace."""

    if value is None:
        refs: list[Any] = []
    elif isinstance(value, list):
        refs = value
    else:
        raise EvidenceReferenceError("evidence_invalid", "evidence_refs must be an array")
    if require and not refs:
        raise EvidenceReferenceError("evidence_required", "At least one evidence reference is required")

    expected_workspace = str(workspace_id or "").strip()
    if not expected_workspace:
        raise EvidenceReferenceError("missing_workspace_context", "Workspace context is required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in refs:
        if not isinstance(raw, dict):
            raise EvidenceReferenceError("evidence_invalid", "Each evidence reference must be an object")
        try:
            reference = LedgerEvidenceRef.model_validate(raw)
        except ValueError as exc:
            raise EvidenceReferenceError("evidence_invalid", str(exc)) from exc
        if reference.workspace_id != expected_workspace:
            raise EvidenceReferenceError(
                "evidence_scope_invalid",
                "Evidence reference belongs to another Workspace",
            )
        if reference.evidence_id in seen:
            raise EvidenceReferenceError(
                "evidence_duplicate",
                f"Evidence reference {reference.evidence_id} is duplicated",
            )
        seen.add(str(reference.evidence_id))
        normalized.append(reference.model_dump(mode="json"))
    return normalized


async def verify_evidence_refs(
    value: Any,
    *,
    entity_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Validate refs and verify any ``verified`` Knowledge claims.

    Unverified refs remain lightweight references. A verified Knowledge ref is
    different: it must resolve to the requested immutable DocumentVersion (or
    current file) and its bytes must match the supplied SHA-256. External
    records stay unverified until an application-specific provider resolver is
    available.
    """

    normalized = normalize_evidence_refs(value, workspace_id=workspace_id)
    verified = [
        ref for ref in normalized
        if ref.get("verification_status") == EvidenceVerificationStatus.VERIFIED.value
    ]
    if not verified:
        return normalized

    from packages.core.ai.runtime.file_actions import runtime_entity_file_root
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.models.document_version import DocumentVersion
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
        resolve_workspace_folder_binding,
    )
    from sqlalchemy import select

    root = runtime_entity_file_root(entity_id)
    if not root:
        raise EvidenceReferenceError(
            "evidence_unverifiable",
            "Verified Knowledge evidence requires an enabled Workspace filesystem",
        )
    root = os.path.realpath(root)

    async with async_session() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if workspace is None:
            raise EvidenceReferenceError("evidence_scope_invalid", "Evidence Workspace could not be resolved")

        for ref in verified:
            if ref.get("evidence_type") != EvidenceType.KNOWLEDGE_DOCUMENT.value:
                raise EvidenceReferenceError(
                    "evidence_unverifiable",
                    "External evidence cannot be marked verified without a provider resolver",
                )
            document = (await db.execute(
                select(Document).where(
                    Document.id == ref.get("document_id"),
                    Document.entity_id == entity_id,
                    Document.is_trashed.is_(False),
                ).limit(1)
            )).scalar_one_or_none()
            if document is None:
                raise EvidenceReferenceError("evidence_not_found", "Knowledge evidence document was not found")
            origin = document.metadata_ if isinstance(document.metadata_, dict) else {}
            origin = origin.get("origin") if isinstance(origin.get("origin"), dict) else {}
            in_workspace = origin.get("workspace_id") == workspace_id
            if not in_workspace and document.folder_id:
                binding = await resolve_workspace_folder_binding(
                    db,
                    entity_id=entity_id,
                    folder_id=document.folder_id,
                )
                in_workspace = bool(binding and binding.workspace_id == workspace_id)
            if not in_workspace and document.fs_path:
                in_workspace = (
                    artifact_folder_id_from_entity_storage_path(document.fs_path)
                    == workspace.artifact_folder_id
                )
            if not in_workspace:
                raise EvidenceReferenceError(
                    "evidence_scope_invalid",
                    "Knowledge evidence document is not bound to this Workspace",
                )

            version_id = str(ref.get("version_id") or "").strip()
            version_number = ref.get("version_number")
            version = None
            if version_id:
                version = (await db.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.id == version_id,
                        DocumentVersion.document_id == document.id,
                    ).limit(1)
                )).scalar_one_or_none()
            elif version_number:
                version = (await db.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.version_number == version_number,
                    ).limit(1)
                )).scalar_one_or_none()
            if version is None:
                raise EvidenceReferenceError("evidence_version_not_found", "Knowledge evidence version was not found")
            if version_number is not None and version.version_number != version_number:
                raise EvidenceReferenceError("evidence_version_mismatch", "Knowledge evidence version number does not match version_id")
            rel_path = str(version.fs_path or "").strip()
            if not rel_path:
                raise EvidenceReferenceError(
                    "evidence_unverifiable",
                    "Knowledge evidence version has no immutable file snapshot",
                )
            immutable_prefix = f".versions/documents/{document.id}/"
            if not rel_path.replace("\\", "/").startswith(immutable_prefix):
                raise EvidenceReferenceError(
                    "evidence_unverifiable",
                    "Knowledge evidence version is a legacy live-path reference, not an immutable snapshot",
                )
            full_path = os.path.realpath(os.path.join(root, rel_path))
            if os.path.commonpath([root, full_path]) != root or not os.path.isfile(full_path):
                raise EvidenceReferenceError("evidence_unverifiable", "Knowledge evidence version file is unavailable")
            actual_sha256 = await asyncio.to_thread(_sha256_file, full_path)
            if actual_sha256.lower() != str(ref.get("sha256") or "").lower():
                raise EvidenceReferenceError("evidence_hash_mismatch", "Knowledge evidence hash does not match the requested version")
            ref["verified_at"] = datetime.now(timezone.utc).isoformat()
            ref["source"] = ref.get("source") or "knowledge_hash"
    return normalized


__all__ = [
    "EvidenceReferenceError",
    "EvidenceType",
    "EvidenceVerificationStatus",
    "LedgerEvidenceRef",
    "normalize_evidence_refs",
    "verify_evidence_refs",
]
