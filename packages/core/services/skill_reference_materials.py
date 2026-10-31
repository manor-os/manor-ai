"""Provenance-preserving input bundle shared by Skill execution surfaces.

The bundle carries data, never authority.  Surface-specific resolvers must
authorize each source before constructing a material.

Reference metadata and reference bytes are separate.  Resolution authorizes
each file and records its size and a streamed sha256; it never holds a file's
bytes.  Bytes are read only where they are used:

* ``skill_reference_text_projections`` reads a bounded text prefix of
  text-like materials for the projection every Skill receives in its input;
* ``build_skill_reference_files`` reads the files actually staged into a
  Sandbox Skill, and the staging byte caps apply to exactly those files.

A binary file that is never staged (for example a long WAV referenced by a
Prompt Skill) therefore costs one streamed hash, and the Skill opens it with
its own authorized tools from the manifest's ``file_path``/``document_id``.

Required vs optional: references the caller names explicitly (Document IDs,
explicit artifact URLs, the current turn's attachments) are required and fail
the run when unavailable. Context the host collects by itself (the run's own
artifacts, dependency artifacts, web observations) and chat history are
optional: listed while they fit the reference count, staged only when they
also fit the byte budget and are unchanged, never a reason to fail. The model
cannot remove host-collected context, so it must never be able to block a run.
Both budgets take required references first, then run context, then history.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Iterator

from sqlalchemy import select

from packages.core.services.file_refs import parse_file_reference


# References one Skill run lists and stages. Only required references can
# exceed it; optional ones beyond it are left out (``bound_skill_reference_
# materials``). Chat's selected history may carry up to 40 refs in addition
# to this turn. The group adapter keeps its independent, stricter 30-source
# limit.
MAX_SKILL_REFERENCE_COUNT = 50
# Byte caps apply to what is staged into a Sandbox Skill, never to what is
# merely referenced.
MAX_SKILL_REFERENCE_BYTES = 40 * 1024 * 1024
MAX_SKILL_HISTORY_REFERENCE_BYTES = 16 * 1024 * 1024
# Required references are hashed (streamed) up to this size. A larger file can
# never be staged; it is listed with ``sha256: null``. Optional references are
# hashed only up to the cap that could still stage them, for the same reason.
MAX_SKILL_REFERENCE_HASH_BYTES = 256 * 1024 * 1024
SKILL_REFERENCE_TEXT_BUDGET_CHARS = 60_000
SKILL_REFERENCE_TEXT_SOURCE_CHARS = 20_000
_READ_CHUNK_BYTES = 1024 * 1024
_TEXT_MIME_TYPES = frozenset({"application/json", "application/xml"})

_REFERENCE_ERROR_REASONS = {
    "reference_missing": "was not found or could not be opened",
    "reference_changed": "changed after it was referenced",
    "reference_forbidden": "is not readable by this run",
    "reference_too_large": "is too large for this Skill run",
    "reference_invalid_path": "has an invalid staging path",
    "reference_invalid_data": "has no readable data",
    "reference_stage_failed": "could not be written into the Skill sandbox",
}


@dataclass(frozen=True)
class SkillReferenceMaterial:
    """One authorized reference.

    ``path`` is the staging path under ``/skill/inputs/``.  ``data`` holds
    host-produced inline content (web observations, room transcripts); a file
    reference has ``data=None`` and is identified by ``entity_id`` +
    ``file_path`` with the ``size``/``sha256`` observed at authorization.
    """

    path: str
    data: bytes | None
    source: str
    name: str | None = None
    mime: str | None = None
    complete_source: bool = True
    required: bool = True
    # Optional chat history (stricter staging cap) vs optional run context.
    history: bool = False
    size: int | None = None
    sha256: str | None = None
    document_id: str | None = None
    entity_id: str | None = None
    file_path: str | None = None
    # Set by revalidation on an optional material that changed after
    # authorization: it stays listed but is never staged.
    not_staged_reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.data, bytes):
            if self.size is None:
                object.__setattr__(self, "size", len(self.data))
            if self.sha256 is None:
                object.__setattr__(self, "sha256", hashlib.sha256(self.data).hexdigest())


class SkillReferenceError(ValueError):
    """A reference failure that names the reference and the reason."""

    def __init__(
        self,
        code: str,
        *,
        name: str | None = None,
        path: str | None = None,
        document_id: str | None = None,
        size: int | None = None,
        limit: int | None = None,
        reason: str | None = None,
    ):
        self.code = code
        self.name = name
        self.path = path
        self.document_id = document_id
        self.size = size
        self.limit = limit
        self.reason = reason or _REFERENCE_ERROR_REASONS.get(code, code)
        super().__init__(f"{code}: {self.message}")

    @property
    def reference(self) -> dict[str, Any]:
        fields = {
            "name": self.name, "path": self.path, "document_id": self.document_id,
            "size": self.size, "limit": self.limit, "reason": self.reason,
        }
        return {key: value for key, value in fields.items() if value is not None}

    @property
    def message(self) -> str:
        label = self.name or self.path or self.document_id
        if label is None:
            text = self.reason[:1].upper() + self.reason[1:]
        else:
            details = [
                value for value in (
                    self.path if self.path and self.path != label else None,
                    f"document {self.document_id}"
                    if self.document_id and self.document_id != label else None,
                    f"{self.size:,} bytes" if self.size is not None else None,
                ) if value
            ]
            text = f"'{label}'" + (f" ({', '.join(details)})" if details else "")
            text = f"{text} {self.reason}"
        if self.limit is not None:
            text = f"{text}; the limit is {self.limit:,} bytes"
        return f"A referenced file is unavailable for this Skill run: {text}."

    def to_result(self) -> dict[str, Any]:
        """The tool-result payload a caller returns to the model."""
        return {"error": self.message, "code": self.code, "reference": self.reference}


def _material_error(
    code: str, material: SkillReferenceMaterial, **kwargs: Any,
) -> SkillReferenceError:
    return SkillReferenceError(
        code, name=material.name, path=material.file_path or material.path,
        document_id=material.document_id, **kwargs,
    )


def _is_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXT_MIME_TYPES


def _iter_snapshot_chunks(file_descriptor: int, *, limit: int) -> Iterator[bytes]:
    """Stream at most ``limit`` bytes of a pinned snapshot in bounded chunks.

    The single byte-reading primitive of this module (guarded by a test).
    ``pread`` never moves the shared descriptor offset.
    """
    offset = 0
    while offset < limit:
        chunk = os.pread(file_descriptor, min(_READ_CHUNK_BYTES, limit - offset), offset)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


def _snapshot_metadata(
    open_snapshot: Callable[..., Any], entity_id: str, path: str, hash_limit: int,
) -> tuple[int, str | None]:
    """Return ``(size, sha256)`` of an authorized file without holding its bytes."""
    with open_snapshot(entity_id, path) as snapshot:
        size = int(snapshot.stat.st_size)
        if size > hash_limit:
            return size, None
        digest = hashlib.sha256()
        observed = 0
        for chunk in _iter_snapshot_chunks(snapshot.file_descriptor, limit=size + 1):
            digest.update(chunk)
            observed += len(chunk)
    if observed != size:
        raise SkillReferenceError("reference_changed", path=path, size=size)
    return size, digest.hexdigest()


def _read_file_prefix(
    material: SkillReferenceMaterial, max_bytes: int,
) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` of a file material; report whether it is whole."""
    from packages.core.ai.runtime.file_actions import runtime_open_entity_file_snapshot

    with runtime_open_entity_file_snapshot(material.entity_id, material.file_path) as snapshot:
        prefix = b"".join(_iter_snapshot_chunks(snapshot.file_descriptor, limit=max_bytes + 1))
    whole = len(prefix) <= max_bytes and len(prefix) == material.size
    if whole and hashlib.sha256(prefix).hexdigest() != material.sha256:
        raise _material_error("reference_changed", material)
    return prefix[:max_bytes], whole


def _read_staged_bytes(material: SkillReferenceMaterial) -> bytes:
    """Read one file material for Sandbox staging and verify it is unchanged."""
    from packages.core.ai.runtime.file_actions import runtime_open_entity_file_snapshot
    from packages.core.services.entity_fs import EntityFilesystemError

    if not material.entity_id or not material.file_path or material.size is None:
        raise _material_error("reference_invalid_data", material)
    try:
        with runtime_open_entity_file_snapshot(material.entity_id, material.file_path) as snapshot:
            if int(snapshot.stat.st_size) != material.size:
                raise _material_error("reference_changed", material, size=material.size)
            data = b"".join(_iter_snapshot_chunks(
                snapshot.file_descriptor, limit=material.size + 1,
            ))
    except SkillReferenceError:
        raise
    except (EntityFilesystemError, OSError, ValueError) as exc:
        raise _material_error("reference_changed", material) from exc
    if len(data) != material.size or hashlib.sha256(data).hexdigest() != material.sha256:
        raise _material_error("reference_changed", material, size=material.size)
    return data


def _reference_priority(material: SkillReferenceMaterial) -> int:
    """Budget order shared by the count and byte caps: required, run context, history."""
    return 0 if material.required else 2 if material.history else 1


def bound_skill_reference_materials(
    materials: list[SkillReferenceMaterial],
) -> list[SkillReferenceMaterial]:
    """Keep the references one Skill run lists and stages, in their given order.

    The count cap is a budget like the staging byte cap: required references
    first, then run context, then history, each class in the order it was
    collected (history arrives newest first). Only required references can
    exceed it; optional ones beyond it are left out of the manifest and of
    staging, never a reason to fail. Deterministic for a given list, so the
    manifest stays a stable revalidation identity.
    """
    required = sum(1 for material in materials if material.required)
    if required > MAX_SKILL_REFERENCE_COUNT:
        raise SkillReferenceError(
            "reference_too_large",
            reason=(
                f"this Skill run names {required} required references, more than "
                f"the {MAX_SKILL_REFERENCE_COUNT} references one run accepts"
            ),
        )
    if len(materials) <= MAX_SKILL_REFERENCE_COUNT:
        return list(materials)
    ranked = sorted(range(len(materials)), key=lambda index: _reference_priority(materials[index]))
    kept = set(ranked[:MAX_SKILL_REFERENCE_COUNT])
    return [material for index, material in enumerate(materials) if index in kept]


def skill_context_artifact_urls(
    runtime_artifact_urls: Iterable[Any] | None,
    dependency_artifact_urls: Iterable[Any] | None,
) -> tuple[str, ...]:
    """Host-collected run context in a deterministic order.

    The run's own artifacts precede its dependencies' artifacts; each group is
    a set, so it is ordered by URL. The order decides which context fits the
    reference count budget.
    """
    ordered: dict[str, None] = {}
    for urls in (runtime_artifact_urls, dependency_artifact_urls):
        ordered.update(dict.fromkeys(sorted({url for url in urls or () if isinstance(url, str)})))
    return tuple(ordered)


def _validate_staging_paths(materials: list[SkillReferenceMaterial]) -> None:
    seen: set[str] = set()
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
            raise SkillReferenceError(
                "reference_invalid_path", name=material.name, path=material.path,
            )
        seen.add(material.path)
        if material.data is None:
            if not material.file_path or material.size is None:
                raise _material_error("reference_invalid_data", material)
        elif not isinstance(material.data, bytes):
            raise _material_error("reference_invalid_data", material)


def _manifest_entry(material: SkillReferenceMaterial) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": material.path,
        "source": material.source,
        "name": material.name,
        "mime": material.mime,
        "size": material.size,
        "sha256": material.sha256,
        "content_is_untrusted": True,
        "complete_source": material.complete_source,
        "required": material.required,
    }
    if material.document_id:
        entry["document_id"] = material.document_id
    if material.file_path:
        entry["file_path"] = material.file_path
    return entry


def skill_reference_manifest(materials: list[SkillReferenceMaterial]) -> list[dict[str, Any]]:
    """Metadata-only manifest of the count-bounded list; reads no bytes.

    Also the revalidation identity.
    """
    materials = bound_skill_reference_materials(materials)
    _validate_staging_paths(materials)
    return [_manifest_entry(material) for material in materials]


def _staging_selection(materials: list[SkillReferenceMaterial]) -> set[int]:
    """Choose what fits the staging byte caps: required, then run context, then history.

    A required material that does not fit fails the run with the file named;
    an optional material that does not fit is listed but not staged.
    """
    total = 0
    history = 0
    selected: set[int] = set()
    ordered = sorted(enumerate(materials), key=lambda item: _reference_priority(item[1]))
    for index, material in ordered:
        if material.not_staged_reason:
            continue
        size = material.size
        stageable = size is not None and material.sha256 is not None
        if material.required:
            if not stageable or total + size > MAX_SKILL_REFERENCE_BYTES:
                remaining = MAX_SKILL_REFERENCE_BYTES - total
                reason = (
                    "exceeds the byte limit for files staged into a Sandbox Skill"
                    if size is None or size > MAX_SKILL_REFERENCE_BYTES or not total
                    else (
                        "does not fit the Sandbox Skill staging budget "
                        f"({remaining:,} bytes remain after earlier references)"
                    )
                )
                raise _material_error(
                    "reference_too_large", material, size=size,
                    limit=MAX_SKILL_REFERENCE_BYTES, reason=reason,
                )
            total += size
            selected.add(index)
        elif (
            stageable
            and total + size <= MAX_SKILL_REFERENCE_BYTES
            and (not material.history or history + size <= MAX_SKILL_HISTORY_REFERENCE_BYTES)
        ):
            total += size
            if material.history:
                history += size
            selected.add(index)
    return selected


def build_skill_reference_files(
    materials: list[SkillReferenceMaterial],
) -> list[tuple[str, bytes]]:
    """Produce the exact staged files and a hash-bearing manifest.

    The only place reference bytes are loaded in full; the byte caps apply to
    what is staged here, after the count cap has bounded the list.
    """
    materials = bound_skill_reference_materials(materials)
    manifest = skill_reference_manifest(materials)
    selected = _staging_selection(materials)
    staged: list[tuple[str, bytes]] = []
    for index, (material, entry) in enumerate(zip(materials, manifest)):
        if index not in selected:
            entry.update(
                staged=False,
                not_staged_reason=material.not_staged_reason or "staging_byte_limit",
            )
            continue
        if material.data is not None:
            data = material.data
        else:
            try:
                data = _read_staged_bytes(material)
            except SkillReferenceError:
                if material.required:
                    raise
                entry.update(staged=False, not_staged_reason="reference_changed")
                continue
        entry["staged"] = True
        staged.append((material.path, data))
    staged.append((
        "/skill/inputs/manifest.json",
        json.dumps({"version": 1, "materials": manifest}, ensure_ascii=False, indent=2).encode("utf-8"),
    ))
    return staged


def _identity(material: SkillReferenceMaterial) -> tuple[str, str | None]:
    return material.source, material.file_path


def _identity_entry(material: SkillReferenceMaterial) -> dict[str, Any]:
    # The staging path is positional; identity is the authorized metadata.
    return {key: value for key, value in _manifest_entry(material).items() if key != "path"}


def revalidate_skill_reference_materials(
    authorized: list[SkillReferenceMaterial],
    current: list[SkillReferenceMaterial],
) -> list[SkillReferenceMaterial]:
    """Reconcile the bundle announced to the Skill with a fresh resolution.

    ``current`` comes from re-running the authorizing resolver after sandbox
    admission (a revoked required reference already raised there). A required
    material whose metadata (size, sha256, ...) changed fails the run with the
    file named; an optional one stays in the manifest but is not staged.
    References that appeared only after authorization are ignored: the Skill
    was never told about them.
    """
    by_identity = {_identity(material): material for material in current}
    reconciled: list[SkillReferenceMaterial] = []
    for material in authorized:
        fresh = by_identity.get(_identity(material))
        if fresh is not None and _identity_entry(fresh) == _identity_entry(material):
            reconciled.append(material)
        elif material.required:
            raise _material_error(
                "reference_changed", material,
                reason="changed between authorization and Sandbox staging",
            )
        else:
            reconciled.append(replace(material, not_staged_reason="reference_changed"))
    return reconciled


def skill_reference_text_projections(
    materials: list[SkillReferenceMaterial],
    *,
    budget_chars: int = SKILL_REFERENCE_TEXT_BUDGET_CHARS,
    source_chars: int = SKILL_REFERENCE_TEXT_SOURCE_CHARS,
) -> list[dict[str, Any]]:
    """Bounded, provenance-labelled text excerpts of text-like materials.

    Binary materials are skipped without reading; they are reachable through
    the manifest. A text file is read only up to the projection budget.
    """
    from packages.core.services.entity_fs import EntityFilesystemError

    projections: list[dict[str, Any]] = []
    remaining = budget_chars
    for material in materials:
        if remaining <= 0:
            break
        if material.mime and not _is_text_mime(material.mime):
            continue
        limit = min(source_chars, remaining)
        max_bytes = limit * 4  # UTF-8 encodes a character in at most 4 bytes.
        if material.data is not None:
            prefix, whole = material.data[:max_bytes], len(material.data) <= max_bytes
        else:
            try:
                prefix, whole = _read_file_prefix(material, max_bytes)
            except (SkillReferenceError, EntityFilesystemError, OSError, ValueError):
                continue
        try:
            decoded = codecs.getincrementaldecoder("utf-8")().decode(prefix, final=whole)
        except UnicodeDecodeError:
            continue
        excerpt = decoded[:limit]
        remaining -= len(excerpt)
        projections.append({
            "source": material.source, "text": excerpt,
            "complete": material.complete_source and whole and len(excerpt) == len(decoded),
        })
    return projections


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
            # Host-recorded context the model cannot withdraw: optional.
            required=False,
        ))
    return combined


async def stage_skill_reference_files(
    sandbox_id: str,
    files: list[tuple[str, bytes]],
    *,
    skill: str | None = None,
) -> None:
    """Stage an already-authorized bundle into one exact Skill sandbox (its /skill view)."""
    from packages.core.ai.runtime.skills import runtime_stage_skill_reference_files

    await runtime_stage_skill_reference_files(sandbox_id, files, **({"skill": skill} if skill else {}))


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
    context_artifact_urls: Iterable[str] = (),
) -> list[SkillReferenceMaterial]:
    """Resolve only server-issued references under the caller's existing file ACL.

    The envelope is created by the host, not the model.  Direct API callers
    may add Document IDs; raw paths and arbitrary URLs are never accepted.
    ``document_ids``/``artifact_urls`` and the current turn's attachments are
    explicit, required references. ``context_artifact_urls`` is run context
    the host collected by itself and, like chat history, is optional.
    Returns metadata only: a file's bytes are never held here.
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
    # (ref, required, history). Required refs precede optional ones, so a
    # path named both ways (deduped below) resolves as required.
    refs: list[tuple[dict, bool, bool]] = []
    if isinstance(metadata, dict):
        current = metadata.get(RUNTIME_ATTACHMENT_CONTEXT_METADATA_KEY)
        refs.extend((ref, True, False) for ref in (
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
        missing = [value for value in document_ids if value not in by_id]
        if missing:
            raise SkillReferenceError(
                "reference_missing", document_id=missing[0],
                reason="was not found in this entity's Knowledge (or is in the trash)",
            )
        refs.extend(({
            "kind": "knowledge_document", "document_id": row.id,
            "path": row.fs_path, "name": row.name, "mime": row.mime_type,
        }, True, False) for row in (by_id[value] for value in document_ids))

    from packages.core.services.ai_file_permissions import is_chat_upload_path, visible_user_paths

    for urls, required in ((artifact_urls, True), (context_artifact_urls, False)):
        for value in urls or ():
            file_ref = parse_file_reference(value) if isinstance(value, str) else None
            if file_ref is not None and file_ref.entity_id == entity_id and file_ref.fs_path:
                # Run artifacts are scraped from tool results, which also cite
                # host-internal staging copies (a video job's first frame under
                # uploads/media-references/). Those are not user files: requiring
                # one failed every later Skill call in the run as forbidden.
                if not (visible_user_paths([file_ref.fs_path]) or is_chat_upload_path(file_ref.fs_path)):
                    continue
                refs.append(({"kind": "runtime_artifact", "path": file_ref.fs_path}, required, False))

    if isinstance(metadata, dict):
        refs.extend((ref, False, True) for ref in (
            metadata.get(RUNTIME_HISTORY_ATTACHMENT_REFS_KEY) or ()
        ) if isinstance(ref, dict))

    materials: list[SkillReferenceMaterial] = []
    seen: set[str] = set()
    for ref, required, history in refs:
        path = ref.get("path")
        document_id = ref.get("document_id") or None
        if not isinstance(path, str) or not path.strip():
            if required:
                raise SkillReferenceError(
                    "reference_missing", name=ref.get("name") or None, document_id=document_id,
                    reason="has no file path",
                )
            continue
        path = path.strip()
        if path in seen:
            continue
        seen.add(path)
        name = str(ref.get("name") or PurePosixPath(path).name)
        # A current Document ID must still identify the same file; the model
        # cannot substitute a path through a stale or forged display ref.
        if document_id:
            document = await db.scalar(select(Document).where(
                Document.id == document_id,
                Document.entity_id == entity_id,
                Document.is_trashed.is_(False),
            ))
            if document is None or document.fs_path != path:
                if required:
                    raise SkillReferenceError(
                        "reference_changed", name=name, path=path, document_id=document_id,
                        reason=(
                            "no longer exists as a Knowledge document" if document is None
                            else "no longer identifies the same file as its Knowledge document"
                        ),
                    )
                continue
        blocked = await runtime_guard_file_read_access(
            entity_id=entity_id, user_id=user_id,
            conversation_id=conversation_id, workspace_id=workspace_id,
            runtime_envelope=runtime_envelope, tool_name="invoke_skill",
            paths=[path],
        )
        if blocked:
            if required:
                raise SkillReferenceError(
                    "reference_forbidden", name=name, path=path, document_id=document_id,
                )
            continue
        # Refs arrive in budget order (required, run context, history), so
        # this bounds the hashing work in the order the count cap keeps;
        # ``bound_skill_reference_materials`` applies the same order again
        # once host observations are added.
        if len(materials) >= MAX_SKILL_REFERENCE_COUNT:
            if required:
                raise SkillReferenceError(
                    "reference_too_large", name=name, path=path, document_id=document_id,
                    reason=(
                        f"exceeds the limit of {MAX_SKILL_REFERENCE_COUNT} "
                        "references one Skill run accepts"
                    ),
                )
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        hash_limit = (
            MAX_SKILL_REFERENCE_HASH_BYTES if required
            else MAX_SKILL_HISTORY_REFERENCE_BYTES if history
            else MAX_SKILL_REFERENCE_BYTES
        )
        try:
            # Streamed in a worker thread: a large file costs one hash pass,
            # never its size in memory, and never blocks the event loop.
            size, digest = await asyncio.to_thread(
                _snapshot_metadata, runtime_open_entity_file_snapshot,
                entity_id, path, hash_limit,
            )
        except SkillReferenceError as exc:
            if required:
                raise SkillReferenceError(
                    exc.code, name=name, path=path, document_id=document_id,
                    size=exc.size, reason=exc.reason,
                ) from exc
            continue
        except (EntityFilesystemError, OSError, ValueError) as exc:
            if required:
                raise SkillReferenceError(
                    "reference_missing", name=name, path=path, document_id=document_id,
                ) from exc
            continue
        source_id = (
            f"document:{document_id}" if document_id
            else f"{ref.get('kind') or 'runtime_file'}:{path}"
        )
        mime = str(ref.get("mime") or "") or mimetypes.guess_type(name)[0] or mimetypes.guess_type(path)[0]
        materials.append(SkillReferenceMaterial(
            path=f"/skill/inputs/source-{len(materials) + 1:02d}{suffix}",
            data=None, source=source_id, name=name, mime=mime,
            required=required, history=history, size=size, sha256=digest,
            document_id=document_id, entity_id=entity_id, file_path=path,
        ))
    return materials
