"""Verify explicit existing-file deliveries without copying or rebinding them.

This is a read-only counterpart to artifact_knowledge's producer projection.
References come from a submitted payload, never a search/list result. The
caller owns the Step/lease fence; this service grants no tools or permissions.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database_runtime import release_read_transaction
from packages.core.contracts.artifact_delivery import ARTIFACT_DELIVERABLE_SCHEMA_KEY
from packages.core.contracts.task_output import OutputContractKind, output_contract_for_schema
from packages.core.models.document import Document
from packages.core.services.artifact_knowledge import ArtifactKnowledgeProjection
from packages.core.services.document_access import unreadable_document_paths
from packages.core.services.entity_fs import (
    EntityFilesystemBusyError,
    EntityFilesystemError,
    canonical_entity_root,
    entity_filesystem_read_lock,
    open_entity_file_snapshot,
)
from packages.core.services.file_refs import parse_file_reference
from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    canonical_document_id,
    canonical_fs_path,
)


_PATH_KEYS = ("fs_path", "path", "file_path", "local_path", "saved_to", "output_path")
_URL_KEYS = ("open_url", "viewer_url", "url", "file_url", "document_url", "download_url", "result_url")
_COLLECTION_KEYS = ("files", "artifacts", "knowledge_artifacts", "documents")
_MAX_REFS = 32
_MAX_FILE_BYTES = 512 * 1024 * 1024
_TEXT_EXTENSIONS = {"md", "txt", "csv", "tsv", "json", "yaml", "yml", "srt", "vtt", "ass", "html", "xml"}


class ArtifactDeliveryError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class VerifiedArtifactDelivery:
    projection: ArtifactKnowledgeProjection
    snapshots: list[dict[str, Any]]


@dataclass(frozen=True)
class ArtifactDeliverySubmission:
    """Selected references plus how the output contract selected them.

    The declaration is inspection context, not proof of artifact provenance or
    task completion. In particular, a unique required schema slot is only a
    candidate until the task's semantic acceptance review assesses its content.
    """

    refs: list[dict[str, Any]]
    declaration: dict[str, Any]


_MISSING = object()
_IDENTITY_KEYS = ("document_id", *_PATH_KEYS, *_URL_KEYS)
_REFERENCE_KEYS = ("document_id", "name", "filename", "type", "source_sha256", *_PATH_KEYS, *_URL_KEYS)
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_NODES = 4096
_SCHEMA_MAPPING_CHILDREN = frozenset({"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"})
_SCHEMA_SINGLE_CHILDREN = frozenset({
    "items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems",
    "contains", "not", "if", "then", "else", "propertyNames",
})
_SCHEMA_ARRAY_CHILDREN = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_UNSUPPORTED_DELIVERY_KEYWORDS = frozenset({
    "$dynamicRef", "allOf", "anyOf", "oneOf", "prefixItems", "contains",
    "not", "if", "then", "else", "dependentSchemas", "patternProperties",
    "unevaluatedProperties", "unevaluatedItems", "additionalProperties",
})


def _pointer_child(pointer: str, token: str) -> str:
    return pointer + "/" + token.replace("~", "~0").replace("/", "~1")


def _schema_reference(root: dict[str, Any], schema: dict[str, Any], pointer: str) -> dict[str, Any]:
    """Resolve bounded local refs; external/dynamic schema paths confer no delivery slot."""
    seen: set[str] = set()
    for _ in range(_MAX_SCHEMA_DEPTH):
        ref = schema.get("$ref")
        if ref is None:
            return schema
        if not isinstance(ref, str) or (ref != "#" and not ref.startswith("#/")) or ref in seen:
            break
        seen.add(ref)
        target: Any = root
        for token in ref[2:].split("/") if ref != "#" else ():
            token = token.replace("~1", "/").replace("~0", "~")
            target = target.get(token, _MISSING) if isinstance(target, dict) else _MISSING
            if target is _MISSING:
                break
        if not isinstance(target, dict):
            break
        schema = {**target, **{key: value for key, value in schema.items() if key != "$ref"}}
    raise ArtifactDeliveryError(
        "artifact_schema_unsupported",
        "Artifact delivery schema uses an unsupported or cyclic reference.",
        details={"pointer": pointer},
    )


def _reject_unsupported_marked_schema(schema: dict[str, Any], pointer: str) -> str | None:
    """Do not let an unhandled schema branch hide an explicit final marker.

    Full JSON Schema branch evaluation is outside the bounded delivery-slot
    selector. Until supported, a marker anywhere in a composed/conditional
    schema must fail explicitly instead of silently choosing a legacy ref.
    """
    stack: list[tuple[dict[str, Any], str]] = [(schema, pointer)]
    seen: set[int] = set()
    marked_pointer: str | None = None
    unsupported: tuple[str, str] | None = None
    while stack:
        node, path = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if len(seen) > _MAX_SCHEMA_NODES:
            raise ArtifactDeliveryError(
                "artifact_reference_limit", "Artifact delivery schema exceeds the bounded traversal limit.",
                details={"schema_pointer": path},
            )
        if node.get(ARTIFACT_DELIVERABLE_SCHEMA_KEY) is True and marked_pointer is None:
            marked_pointer = path or "/"
        for key, value in node.items():
            child_path = _pointer_child(path, key)
            if key in _UNSUPPORTED_DELIVERY_KEYWORDS and unsupported is None:
                if key != "additionalProperties" or isinstance(value, dict):
                    unsupported = (child_path, key)
            if key in _SCHEMA_MAPPING_CHILDREN and isinstance(value, dict):
                stack.extend(
                    (child, _pointer_child(child_path, name))
                    for name, child in value.items() if isinstance(child, dict)
                )
            elif key in _SCHEMA_SINGLE_CHILDREN and isinstance(value, dict):
                stack.append((value, child_path))
            elif key in _SCHEMA_ARRAY_CHILDREN and isinstance(value, list):
                stack.extend(
                    (child, _pointer_child(child_path, str(index)))
                    for index, child in enumerate(value) if isinstance(child, dict)
                )
    if marked_pointer is not None and unsupported is not None:
        path, keyword = unsupported
        raise ArtifactDeliveryError(
            "artifact_schema_unsupported",
            "A marked delivery slot uses a JSON Schema branch this selector cannot evaluate.",
            details={"schema_pointer": path, "unsupported_keyword": keyword},
        )
    return marked_pointer


def _declared_artifact_refs(result: Any, output_schema: dict[str, Any]) -> ArtifactDeliverySubmission | None:
    """Select final refs by declared result structure, not runtime key searches."""
    contract = output_contract_for_schema(output_schema)
    schema = contract.payload_schema or output_schema
    if not isinstance(schema, dict):
        return None
    value = result
    pointer = ""
    if contract.kind is OutputContractKind.TASK_ENVELOPE:
        value = (result.get("outputs") or {}).get("data", _MISSING) if isinstance(result, dict) and isinstance(result.get("outputs"), dict) else _MISSING
        pointer = "/outputs/data"
    declared_marker_pointer = _reject_unsupported_marked_schema(schema, pointer)

    # Each tuple is one structural slot; an array slot can contribute several
    # concrete references without becoming several competing declarations.
    marked: list[tuple[str, Any, dict[str, Any]]] = []
    implicit: list[tuple[str, Any, dict[str, Any]]] = []
    visited_nodes = 0

    def count_node(path: str) -> None:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > _MAX_SCHEMA_NODES:
            raise ArtifactDeliveryError(
                "artifact_reference_limit", "Artifact delivery schema exceeds the bounded traversal limit.",
                details={"pointer": path},
            )

    def contains_marker(node: Any, path: str, depth: int) -> bool:
        if depth > _MAX_SCHEMA_DEPTH or not isinstance(node, dict):
            return False
        count_node(path)
        node = _schema_reference(schema, node, path)
        if node.get(ARTIFACT_DELIVERABLE_SCHEMA_KEY) is True:
            return True
        children = node.get("properties")
        if isinstance(children, dict) and any(
            contains_marker(child, _pointer_child(path, key), depth + 1)
            for key, child in children.items()
        ):
            return True
        return contains_marker(node.get("items"), _pointer_child(path, "*"), depth + 1)

    def visit(node: Any, current: Any, path: str, required: bool, depth: int) -> None:
        if depth > _MAX_SCHEMA_DEPTH:
            raise ArtifactDeliveryError(
                "artifact_reference_limit", "Artifact delivery schema exceeds the bounded nesting limit.",
                details={"pointer": path},
            )
        if not isinstance(node, dict):
            return
        count_node(path)
        node = _schema_reference(schema, node, path)
        explicit = node.get(ARTIFACT_DELIVERABLE_SCHEMA_KEY) is True
        properties = node.get("properties")
        identities = set(properties or {}) & set(_IDENTITY_KEYS) if isinstance(properties, dict) else set()
        if identities:
            candidate = (path or "/", current, node)
            if explicit:
                marked.append(candidate)
            elif required and identities & set(node.get("required") or []):
                implicit.append(candidate)
            return
        if explicit:
            items = node.get("items")
            if node.get("type") != "array" or not isinstance(items, dict):
                raise ArtifactDeliveryError(
                    "artifact_schema_unsupported",
                    "A marked delivery slot must declare an identity object or an array of identity objects.",
                    details={"pointer": path or "/"},
                )
            item_schema = _schema_reference(schema, items, _pointer_child(path, "*"))
            item_properties = item_schema.get("properties")
            if not isinstance(item_properties, dict) or not (set(item_properties) & set(_IDENTITY_KEYS)):
                raise ArtifactDeliveryError(
                    "artifact_schema_unsupported", "A marked delivery array must contain identity objects.",
                    details={"pointer": path or "/"},
                )
            marked.append((path or "/", current, node))
            return
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            item_schema = _schema_reference(schema, node["items"], _pointer_child(path, "*"))
            item_properties = item_schema.get("properties")
            item_identities = set(item_properties or {}) & set(_IDENTITY_KEYS) if isinstance(item_properties, dict) else set()
            if item_identities:
                candidate = (path or "/", current, node)
                if item_schema.get(ARTIFACT_DELIVERABLE_SCHEMA_KEY) is True:
                    marked.append(candidate)
                elif (
                    required and isinstance(node.get("minItems"), int)
                    and node["minItems"] > 0
                    and item_identities & set(item_schema.get("required") or [])
                ):
                    implicit.append(candidate)
                return
            items = current if isinstance(current, list) else []
            # A required property with an optional empty array does not prove
            # a final artifact. An explicit marker can still select it.
            child_required = required and isinstance(node.get("minItems"), int) and node["minItems"] > 0
            for index, item in enumerate(items[:_MAX_REFS + 1]):
                visit(node["items"], item, _pointer_child(path, str(index)), child_required, depth + 1)
            return
        if isinstance(properties, dict):
            required_keys = set(node.get("required") or [])
            for key, child in properties.items():
                child_value = current.get(key, _MISSING) if isinstance(current, dict) else _MISSING
                # Absent implicit fields cannot be submitted. Still inspect
                # absent explicit declarations so they fail with their path.
                if child_value is _MISSING and not required and not contains_marker(child, _pointer_child(path, key), depth + 1):
                    continue
                visit(child, child_value, _pointer_child(path, key), required and key in required_keys, depth + 1)

    visit(schema, value, pointer, True, 0)
    if declared_marker_pointer is not None and not marked:
        raise ArtifactDeliveryError(
            "artifact_schema_unsupported",
            "A marked delivery slot could not be resolved by the bounded schema selector.",
            details={"schema_pointer": declared_marker_pointer, "reason": "marked_delivery_slot_unresolved"},
        )
    slots = marked if marked else implicit
    if not slots:
        return None
    if not marked and len(slots) != 1:
        raise ArtifactDeliveryError(
            "artifact_reference_ambiguous",
            "Several schema-declared file identities could be final delivery; mark the intended slot explicitly.",
            details={"candidate_pointers": [path for path, _, _ in slots[:_MAX_REFS]]},
        )
    refs: list[dict[str, Any]] = []
    pointers: list[str] = []
    for path, current, node in slots:
        values = current if node.get("type") == "array" else [current]
        identity_schema = _schema_reference(schema, node["items"], _pointer_child(path, "*")) if node.get("type") == "array" else node
        if not isinstance(values, list) or not values:
            raise ArtifactDeliveryError(
                "artifact_reference_missing", "A declared final artifact reference was not submitted.",
                details={"pointer": path, "expected": "nonempty array of identity objects" if node.get("type") == "array" else "identity object", "actual": "missing" if current is _MISSING else type(current).__name__},
            )
        for index, item in enumerate(values):
            item_path = _pointer_child(path, str(index)) if node.get("type") == "array" else path
            if not isinstance(item, dict):
                raise ArtifactDeliveryError(
                    "artifact_reference_missing", "A declared final artifact must be an identity object.",
                    details={"pointer": item_path, "expected": "identity object", "actual": "missing" if item is _MISSING else type(item).__name__},
                )
            for key in _IDENTITY_KEYS:
                if key in item and item[key] is not None and (not isinstance(item[key], str) or not item[key].strip()):
                    raise ArtifactDeliveryError(
                        "artifact_identity_invalid", "A declared artifact identity must be a nonempty string.",
                        details={"pointer": _pointer_child(item_path, key), "expected": "nonempty string", "actual": type(item[key]).__name__},
                    )
            if not any(item.get(key) for key in _IDENTITY_KEYS):
                required_identities = [key for key in (identity_schema.get("required") or []) if key in _IDENTITY_KEYS]
                missing_key = required_identities[0] if required_identities else None
                raise ArtifactDeliveryError(
                    "artifact_reference_missing", "A declared final artifact lacks a Document ID or file path.",
                    details={"pointer": _pointer_child(item_path, missing_key) if missing_key else item_path, "expected": "nonempty artifact identity", "actual": "missing"},
                )
            refs.append({key: item[key] for key in _REFERENCE_KEYS if key in item})
            pointers.append(item_path)
            if len(refs) > _MAX_REFS:
                raise ArtifactDeliveryError("artifact_reference_limit", "Too many artifact delivery references.")
    return ArtifactDeliverySubmission(refs, {
        "basis": "explicit_schema" if marked else "unique_required_schema",
        "pointers": pointers,
        "requires_semantic_assessment": True,
    })


def resolve_artifact_delivery_submission(
    result: Any, *, output_schema: dict[str, Any] | None = None,
) -> ArtifactDeliverySubmission:
    """Return candidate deliveries with inspectable selection provenance.

    This does not establish that a selected file was produced by this Step or
    satisfies the Task. The host verifier and same-loop content assessment own
    those separate decisions.
    """
    if isinstance(output_schema, dict):
        declared = _declared_artifact_refs(result, output_schema)
        if declared is not None:
            return declared
    refs: list[dict[str, Any]] = []
    pointers: list[str] = []

    def add(ref: dict[str, Any], pointer: str) -> None:
        refs.append(ref)
        pointers.append(pointer or "/")

    def visit(value: Any, depth: int = 0, pointer: str = "") -> None:
        if depth > 8 or len(refs) > _MAX_REFS:
            raise ArtifactDeliveryError("artifact_reference_limit", "Artifact delivery references exceed the bounded nesting or count limit.")
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, depth + 1, _pointer_child(pointer, str(index)))
            return
        if not isinstance(value, dict):
            return
        if any(value.get(key) for key in ("document_id", *_PATH_KEYS, *_URL_KEYS)):
            add({key: value[key] for key in _REFERENCE_KEYS if key in value}, pointer)
        for key in _COLLECTION_KEYS:
            for index, item in enumerate(value.get(key) or [] if isinstance(value.get(key), list) else []):
                item_pointer = _pointer_child(_pointer_child(pointer, key), str(index))
                if isinstance(item, str):
                    add({"url" if item.startswith("/") else "fs_path": item}, item_pointer)
                elif isinstance(item, dict):
                    visit(item, depth + 1, item_pointer)
        outputs = value.get("outputs")
        if isinstance(outputs, dict):
            outputs_pointer = _pointer_child(pointer, "outputs")
            visit(outputs, depth + 1, outputs_pointer)
            visit(outputs.get("data"), depth + 1, _pointer_child(outputs_pointer, "data"))

    visit(result)
    if not refs:
        raise ArtifactDeliveryError("artifact_reference_missing", "Submit the existing file's document_id or exact fs_path in the declared deliverable; source text and search results are not delivery.")
    if len(refs) > _MAX_REFS:
        raise ArtifactDeliveryError("artifact_reference_limit", "Too many artifact delivery references.")
    return ArtifactDeliverySubmission(refs, {
        "basis": "legacy_explicit",
        "pointers": pointers,
        "requires_semantic_assessment": True,
    })


def submitted_artifact_refs(result: Any, *, output_schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Compatibility accessor for selected references and identity assertions."""
    return resolve_artifact_delivery_submission(result, output_schema=output_schema).refs


def verified_submission_refs(
    refs: list[dict[str, Any]], snapshots: list[dict[str, Any]], *, entity_id: str,
) -> list[dict[str, Any]]:
    """Project a submission's identities from an already verified manifest.

    Call only after verifying *all* original assertions. This cannot resolve
    missing Documents, grant access, or turn tool/input evidence into delivery.
    """
    selected: dict[str, dict[str, Any]] = {}
    factory = ArtifactReferenceFactory(entity_id=entity_id)
    for ref in refs:
        document_id, path = _identity(ref, entity_id)
        matches = [item for item in snapshots if (
            (not document_id or item.get("document_id") == document_id)
            and (not path or item.get("fs_path") == path)
        )]
        if len(matches) != 1:
            raise ArtifactDeliveryError("artifact_evidence_invalid", "Submitted artifact is absent from the verified delivery manifest.")
        item = matches[0]
        selected[item["document_id"]] = factory.create({
            "type": "file", "name": str(item["fs_path"]).rsplit("/", 1)[-1],
            "document_id": item["document_id"], "fs_path": item["fs_path"],
            **({"source_sha256": item["source_sha256"]} if item.get("source_sha256") else {}),
        })
    return list(selected.values())


def _identity(ref: dict[str, Any], entity_id: str) -> tuple[str, str]:
    document_ids: set[str] = set()
    paths: set[str] = set()
    if ref.get("document_id"):
        document_id = canonical_document_id(ref["document_id"])
        if not document_id:
            raise ArtifactDeliveryError("artifact_identity_invalid", "document_id must be a Knowledge Document identity.")
        document_ids.add(document_id)
    for key in _PATH_KEYS:
        if ref.get(key):
            raw = str(ref[key])
            if parse_file_reference(raw) is not None:
                _add_address(raw, entity_id, document_ids, paths)
            else:
                path = canonical_fs_path(raw)
                if not path:
                    raise ArtifactDeliveryError("artifact_identity_invalid", "Artifact path must be an exact entity-relative Knowledge path.")
                paths.add(path)
    for key in _URL_KEYS:
        if ref.get(key):
            _add_address(str(ref[key]), entity_id, document_ids, paths)
    if len(document_ids) > 1 or len(paths) > 1:
        raise ArtifactDeliveryError("artifact_identity_conflict", "Artifact document_id, path and viewer address must identify the same file.")
    if not document_ids and not paths:
        raise ArtifactDeliveryError("artifact_reference_missing", "An explicit existing Document or file path is required.")
    return next(iter(document_ids), ""), next(iter(paths), "")


def _add_address(value: str, entity_id: str, document_ids: set[str], paths: set[str]) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ArtifactDeliveryError("artifact_identity_invalid", "Artifact address is invalid.") from exc
    if parsed.scheme or parsed.netloc:
        raise ArtifactDeliveryError("artifact_identity_invalid", "Existing artifact delivery requires a local Knowledge reference, not a remote URL.")
    file_ref = parse_file_reference(value, entity_id=entity_id)
    if file_ref is not None and file_ref.entity_id == entity_id:
        if file_ref.document_id:
            document_ids.add(file_ref.document_id)
            return
        if file_ref.fs_path:
            paths.add(file_ref.fs_path)
            return
    raise ArtifactDeliveryError("artifact_identity_invalid", "Artifact address must identify a Document or file in the current entity.")


def _document_revision(document: Document) -> str:
    return document.updated_at.isoformat() if document.updated_at else ""


async def verify_artifact_snapshot_access(
    db: AsyncSession, *, entity_id: str, workspace_id: str | None,
    user_id: str | None, expected_snapshots: list[dict[str, Any]],
) -> None:
    """Recheck cached inspection access without re-hashing unrelated files.

    A page that exposes artifact text still requires an exact one-file hash
    check via ``verify_artifact_delivery_refs``. The final Task submission
    revalidates every artifact and the live Plan under its normal locks.
    """
    expected = {str(item.get("document_id") or ""): item for item in expected_snapshots}
    if not expected or len(expected) != len(expected_snapshots):
        raise ArtifactDeliveryError("artifact_evidence_invalid", "Cached artifact identities are incomplete.")
    documents = list((await db.execute(select(Document).where(
        Document.entity_id == entity_id,
        Document.id.in_(expected),
        Document.is_trashed.is_(False),
    ).execution_options(populate_existing=True))).scalars().all())
    if len(documents) != len(expected):
        raise ArtifactDeliveryError("artifact_unavailable", "A delivered Document is no longer available.")
    paths: list[str] = []
    for document in documents:
        saved = expected[document.id]
        path = str(document.fs_path or "")
        if path != saved.get("fs_path") or _document_revision(document) != saved.get("document_updated_at"):
            raise ArtifactDeliveryError("artifact_changed", "A delivered Document changed during inspection.")
        paths.append(path)
    if await unreadable_document_paths(
        db, entity_id=entity_id, rel_paths=paths, user_id=user_id,
        workspace_id=workspace_id, actor_type="agent",
    ):
        raise ArtifactDeliveryError("artifact_unavailable", "Artifact read access changed during inspection.")


def _read_snapshot(entity_id: str, path: str, *, expected_sha256: str | None, text_limit: int) -> dict[str, Any]:
    with open_entity_file_snapshot(entity_id, path, expected_content_sha256=expected_sha256) as snapshot:
        if snapshot.stat.st_size > _MAX_FILE_BYTES:
            raise ArtifactDeliveryError("artifact_too_large", "Artifact exceeds the bounded delivery verification limit.")
        with open(snapshot.descriptor_path, "rb") as source:
            digest = snapshot.content_sha256 or hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            text = source.read(text_limit).decode("utf-8", errors="replace") if text_limit and path.rsplit(".", 1)[-1].lower() in _TEXT_EXTENSIONS else None
        return {
            "source_sha256": digest,
            "size": snapshot.stat.st_size,
            "mtime_ns": snapshot.stat.st_mtime_ns,
            "text_available": text is not None,
            "text_truncated": bool(text is not None and snapshot.stat.st_size > text_limit),
            **({"text": text} if text is not None else {}),
        }


async def verify_artifact_delivery_refs(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    refs: list[dict[str, Any]],
    expected_snapshots: list[dict[str, Any]] | None = None,
    text_limit: int = 0,
) -> VerifiedArtifactDelivery:
    """Resolve exact identities and verify currently authorized pinned bytes.

    The nonwaiting shared lock avoids lock inversion when the caller already
    owns Task/Plan rows. Contention is a retryable observation, never permission
    to ignore a writer. No filesystem or Document mutations occur here.
    """
    if not refs or len(refs) > _MAX_REFS:
        raise ArtifactDeliveryError("artifact_reference_missing", "Submit a bounded, explicit list of existing artifacts.")
    expected = {str(item.get("document_id") or ""): item for item in expected_snapshots or []}
    if expected_snapshots is not None and (not expected or len(expected) != len(expected_snapshots)):
        raise ArtifactDeliveryError("artifact_evidence_invalid", "Existing artifact evidence is incomplete or ambiguous.")
    canonical: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    factory = ArtifactReferenceFactory(entity_id=entity_id)
    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id), timeout_seconds=0):
            for ref in refs:
                document_id, path = _identity(ref, entity_id)
                matches = list((await db.execute(select(Document).where(
                    Document.entity_id == entity_id,
                    Document.is_trashed.is_(False),
                    or_(Document.id == document_id, Document.fs_path == path) if document_id and path else (
                        Document.id == document_id if document_id else Document.fs_path == path
                    ),
                ).execution_options(populate_existing=True))).scalars().all())
                if len(matches) != 1:
                    raise ArtifactDeliveryError("artifact_unavailable", "The submitted artifact does not resolve to one available Document.")
                document = matches[0]
                if (document_id and document.id != document_id) or (path and document.fs_path != path):
                    raise ArtifactDeliveryError("artifact_identity_conflict", "The submitted Document and filesystem path identify different files.")
                path = str(document.fs_path or "")
                if not path or path != canonical_fs_path(path):
                    raise ArtifactDeliveryError("artifact_unavailable", "The Document has no canonical local artifact file.")
                revision = _document_revision(document)
                if await unreadable_document_paths(db, entity_id=entity_id, rel_paths=[path], user_id=user_id, workspace_id=workspace_id, actor_type="agent"):
                    raise ArtifactDeliveryError("artifact_unavailable", "The submitted artifact is not currently readable in this execution.")
                prior = expected.get(document.id)
                if expected_snapshots is not None and (prior is None or prior.get("fs_path") != path or prior.get("document_updated_at") != revision):
                    raise ArtifactDeliveryError("artifact_changed", "The artifact identity or revision changed; read and submit it again.")
                expected_digest = str((prior or {}).get("source_sha256") or ref.get("source_sha256") or "") or None
                await release_read_transaction(db)
                facts = await asyncio.to_thread(_read_snapshot, entity_id, path, expected_sha256=expected_digest, text_limit=max(0, min(text_limit, 200_000)))
                # Re-read policy and identity after physical I/O, rather than
                # keeping an earlier ORM/ACL snapshot as execution authority.
                current = (await db.execute(select(Document).where(
                    Document.id == document.id, Document.entity_id == entity_id, Document.is_trashed.is_(False),
                ).execution_options(populate_existing=True))).scalar_one_or_none()
                if current is None or current.fs_path != path or _document_revision(current) != revision:
                    raise ArtifactDeliveryError("artifact_changed", "The artifact changed during verification; read and submit it again.")
                if await unreadable_document_paths(db, entity_id=entity_id, rel_paths=[path], user_id=user_id, workspace_id=workspace_id, actor_type="agent"):
                    raise ArtifactDeliveryError("artifact_unavailable", "Artifact read access changed during verification.")
                if current.id in seen:
                    continue
                seen.add(current.id)
                canonical.append(factory.create({"type": "file", "name": current.name, "document_id": current.id, "fs_path": path}))
                snapshots.append({"document_id": current.id, "fs_path": path, "document_updated_at": revision, **facts})
    except EntityFilesystemBusyError as exc:
        raise ArtifactDeliveryError("artifact_busy", "The artifact is being changed; retry read-only verification shortly.") from exc
    except (EntityFilesystemError, OSError) as exc:
        raise ArtifactDeliveryError("artifact_changed", "Artifact bytes are unavailable or changed; read and submit the existing file again.") from exc
    if expected_snapshots is not None and seen != set(expected):
        raise ArtifactDeliveryError("artifact_evidence_invalid", "The submitted artifacts differ from the saved delivery evidence.")
    return VerifiedArtifactDelivery(
        projection=ArtifactKnowledgeProjection(refs=canonical, knowledge_artifacts=canonical, failures=[]),
        snapshots=snapshots,
    )
