"""Provenance-preserving input bundle shared by Skill execution surfaces.

The bundle carries data, never authority.  Surface-specific resolvers must
authorize each source before constructing a material.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

from sqlalchemy import select

from packages.core.services.file_refs import parse_file_reference


# Chat's selected history may carry up to 40 refs in addition to this turn.
# The group adapter keeps its independent, stricter 30-source limit.
MAX_SKILL_REFERENCE_COUNT = 50
MAX_SKILL_REFERENCE_BYTES = 40 * 1024 * 1024
MAX_SKILL_HISTORY_REFERENCE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SkillReferenceMaterial:
    path: str
    data: bytes
    source: str
    name: str | None = None
    mime: str | None = None
    complete_source: bool = True


class SkillReferenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def build_skill_reference_files(
    materials: list[SkillReferenceMaterial],
) -> list[tuple[str, bytes]]:
    """Produce the exact staged files and a hash-bearing manifest."""
    if len(materials) > MAX_SKILL_REFERENCE_COUNT:
        raise SkillReferenceError("reference_too_large")
    total = 0
    seen: set[str] = set()
    staged: list[tuple[str, bytes]] = []
    manifest: list[dict] = []
    for material in materials:
        path = PurePosixPath(material.path)
        if (
            not material.path.startswith("/skill/inputs/")
            or str(path) != material.path
            or material.path.endswith("/")
            or path.name in {"", "manifest.json"}
            or ".." in path.parts
            or material.path in seen
        ):
            raise SkillReferenceError("reference_invalid_path")
        if not isinstance(material.data, bytes):
            raise SkillReferenceError("reference_invalid_data")
        seen.add(material.path)
        total += len(material.data)
        if total > MAX_SKILL_REFERENCE_BYTES:
            raise SkillReferenceError("reference_too_large")
        staged.append((material.path, material.data))
        manifest.append({
            "path": material.path,
            "source": material.source,
            "name": material.name,
            "mime": material.mime,
            "size": len(material.data),
            "sha256": hashlib.sha256(material.data).hexdigest(),
            "content_is_untrusted": True,
            "complete_source": material.complete_source,
        })
    staged.append((
        "/skill/inputs/manifest.json",
        json.dumps({"version": 1, "materials": manifest}, ensure_ascii=False, indent=2).encode("utf-8"),
    ))
    return staged


def skill_materials_with_web_observations(
    materials: list[SkillReferenceMaterial],
    observations: Iterable[Any],
) -> list[SkillReferenceMaterial]:
    """Append only host-recorded web text/snippets to authorized file inputs."""

    combined = list(materials)
    for observation in observations:
        combined.append(SkillReferenceMaterial(
            path=f"/skill/inputs/web-{len(combined) + 1:02d}.txt",
            data=observation.content.encode("utf-8"),
            source=(f"{observation.kind}:{observation.url}#offset={observation.offset}"
                    f"&source_sha256={observation.source_sha256}"),
            name=observation.url,
            mime="text/plain",
            complete_source=observation.complete_source,
        ))
    return combined


async def stage_skill_reference_files(
    sandbox_id: str,
    files: list[tuple[str, bytes]],
) -> None:
    """Stage an already-authorized bundle into one exact Skill sandbox."""
    from packages.core.ai.runtime.skills import runtime_stage_skill_reference_files

    await runtime_stage_skill_reference_files(sandbox_id, files)


async def resolve_skill_reference_materials(
    *,
    db: Any,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    workspace_id: str | None,
    runtime_envelope: Any | None,
    document_ids: Iterable[str] = (),
    artifact_urls: Iterable[str] = (),
) -> list[SkillReferenceMaterial]:
    """Resolve only server-issued references under the caller's existing file ACL.

    The envelope is created by the host, not the model.  Direct API callers
    may add Document IDs; raw paths and arbitrary URLs are never accepted.
    """
    from packages.core.ai.runtime.file_actions import (
        runtime_guard_file_read_access,
        runtime_open_entity_file_snapshot,
    )
    from packages.core.ai.runtime.file_context import (
        RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY,
        RUNTIME_HISTORY_ATTACHMENT_REFS_KEY,
    )
    from packages.core.models.document import Document
    from packages.core.services.entity_fs import EntityFilesystemError

    metadata = getattr(runtime_envelope, "metadata", None)
    refs: list[tuple[dict, bool]] = []
    if isinstance(metadata, dict):
        current = metadata.get(RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY)
        refs.extend((ref, True) for ref in (
            current.get("refs") if isinstance(current, dict) else None
        ) or () if isinstance(ref, dict))
    document_ids = tuple(dict.fromkeys(
        str(value).strip() for value in (document_ids or ()) if str(value).strip()
    ))
    if document_ids:
        rows = (await db.execute(select(Document).where(
            Document.id.in_(document_ids), Document.entity_id == entity_id,
            Document.is_trashed.is_(False),
        ))).scalars().all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(document_ids):
            raise SkillReferenceError("reference_missing")
        refs.extend(({
            "kind": "knowledge_document", "document_id": row.id,
            "path": row.fs_path, "name": row.name, "mime": row.mime_type,
        }, True) for row in (by_id[value] for value in document_ids))

    for value in artifact_urls or ():
        file_ref = parse_file_reference(value) if isinstance(value, str) else None
        if file_ref is not None and file_ref.entity_id == entity_id and file_ref.fs_path:
            refs.append(({"kind": "runtime_artifact", "path": file_ref.fs_path}, True))

    if isinstance(metadata, dict):
        refs.extend((ref, False) for ref in (
            metadata.get(RUNTIME_HISTORY_ATTACHMENT_REFS_KEY) or ()
        ) if isinstance(ref, dict))

    materials: list[SkillReferenceMaterial] = []
    seen: set[str] = set()
    total_bytes = 0
    history_bytes = 0
    for ref, required in refs:
        path = ref.get("path")
        if not isinstance(path, str) or not path.strip():
            if required:
                raise SkillReferenceError("reference_missing")
            continue
        path = path.strip()
        if path in seen:
            continue
        seen.add(path)
        # A current Document ID must still identify the same file; the model
        # cannot substitute a path through a stale or forged display ref.
        document_id = ref.get("document_id")
        if document_id:
            document = await db.scalar(select(Document).where(
                Document.id == document_id,
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
            ))
            if document is None or document.fs_path != path:
                if required:
                    raise SkillReferenceError("reference_changed")
                continue
        blocked = await runtime_guard_file_read_access(
            entity_id=entity_id, user_id=user_id,
            conversation_id=conversation_id, workspace_id=workspace_id,
            runtime_envelope=runtime_envelope, tool_name="invoke_skill",
            paths=[path],
        )
        if blocked:
            if required:
                raise SkillReferenceError("reference_forbidden")
            continue
        if len(materials) >= MAX_SKILL_REFERENCE_COUNT:
            if required:
                raise SkillReferenceError("reference_too_large")
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        try:
            with runtime_open_entity_file_snapshot(entity_id, path) as snapshot:
                if snapshot.stat.st_size > MAX_SKILL_REFERENCE_BYTES:
                    raise SkillReferenceError("reference_too_large")
                with os.fdopen(os.dup(snapshot.file_descriptor), "rb") as source:
                    data = source.read(MAX_SKILL_REFERENCE_BYTES + 1)
        except SkillReferenceError:
            if required:
                raise
            continue
        except (EntityFilesystemError, OSError, ValueError) as exc:
            if required:
                raise SkillReferenceError("reference_missing") from exc
            continue
        if (
            len(data) > MAX_SKILL_REFERENCE_BYTES
            or total_bytes + len(data) > MAX_SKILL_REFERENCE_BYTES
            or (not required and history_bytes + len(data) > MAX_SKILL_HISTORY_REFERENCE_BYTES)
        ):
            if required:
                raise SkillReferenceError("reference_too_large")
            continue
        total_bytes += len(data)
        if not required:
            history_bytes += len(data)
        source_id = (
            f"document:{document_id}" if document_id
            else f"{ref.get('kind') or 'runtime_file'}:{path}"
        )
        materials.append(SkillReferenceMaterial(
            path=f"/skill/inputs/source-{len(materials) + 1:02d}{suffix}",
            data=data, source=source_id,
            name=str(ref.get("name") or PurePosixPath(path).name),
            mime=str(ref.get("mime") or "") or None,
        ))
    return materials
