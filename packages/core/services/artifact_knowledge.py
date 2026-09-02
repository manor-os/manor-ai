"""Project materialized local artifacts into the Knowledge document index.

An entity filesystem path is storage, not a user-facing artifact contract.
Every local file that a completed step declares as an artifact must also have
a Document id so chat, task output, and Knowledge all point at the same thing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from packages.core.services.entity_fs import get_entity_root
from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    ArtifactReferenceIdentity,
    canonical_file_markdown_link,
)
from packages.core.services.knowledge_sync import (
    bind_document_to_workspace,
    sync_file_to_knowledge,
)


@dataclass(frozen=True)
class ArtifactKnowledgeProjection:
    refs: list[dict[str, Any]]
    knowledge_artifacts: list[dict[str, Any]]
    failures: list[dict[str, str]]


def _entity_relative_artifact_path(
    value: Any,
    *,
    entity_id: str,
    entity_root: str,
) -> str | None:
    """Resolve a local artifact reference without accepting another entity."""

    text = str(value or "").strip()
    if not text:
        return None

    parsed = urlsplit(text)
    path = unquote(parsed.path or "").replace("\\", "/")
    api_prefix = "/api/v1/fs/"
    if path.startswith(api_prefix):
        scoped = path[len(api_prefix):]
        scoped_entity, separator, rel_path = scoped.partition("/")
        if not separator or unquote(scoped_entity) != str(entity_id):
            return None
        path = rel_path
    elif parsed.scheme or parsed.netloc:
        return None
    elif path.lstrip("/").startswith(("viewer/", "api/v1/", "documents/")):
        return None

    root = os.path.realpath(entity_root)
    if os.path.isabs(path):
        absolute = os.path.realpath(path)
        try:
            if os.path.commonpath([root, absolute]) == root:
                return os.path.relpath(absolute, root).replace(os.sep, "/")
        except ValueError:
            return None
        # Artifact payloads frequently spell entity-relative paths with one
        # leading slash. Treat those as relative unless they identify a real
        # absolute file outside the entity root.
        if os.path.exists(absolute):
            return None

    rel_path = path.lstrip("/")
    if not rel_path:
        return None
    absolute = os.path.realpath(os.path.join(root, rel_path))
    try:
        if os.path.commonpath([root, absolute]) != root:
            return None
    except ValueError:
        return None
    return os.path.relpath(absolute, root).replace(os.sep, "/")


def _ref_local_path(
    ref: dict[str, Any],
    *,
    entity_id: str,
    entity_root: str,
) -> str | None:
    for key in ("fs_path", "path", "file_path", "local_path", "saved_to"):
        if ref.get(key):
            return _entity_relative_artifact_path(
                ref[key], entity_id=entity_id, entity_root=entity_root,
            )
    for key in ("url", "file_url", "result_url", "download_url"):
        value = ref.get(key)
        if value and "/api/v1/fs/" in str(value):
            return _entity_relative_artifact_path(
                value, entity_id=entity_id, entity_root=entity_root,
            )
    return None


def _artifact_name(ref: dict[str, Any], rel_path: str | None, document_id: str) -> str:
    explicit = str(ref.get("name") or ref.get("filename") or "").strip()
    if explicit:
        return explicit
    if rel_path:
        return rel_path.rstrip("/").rsplit("/", 1)[-1]
    return document_id


@dataclass(frozen=True)
class ArtifactDocumentResolution:
    document_id: str = ""
    failure_reason: str | None = None


class ArtifactDocumentResolver:
    """Resolve one artifact identity to a Document, with per-run caching."""

    def __init__(
        self,
        *,
        entity_id: str,
        entity_root: str,
        workspace_id: str | None,
        task_id: str | None,
        agent_id: str | None,
        user_id: str | None,
        tool_name: str,
    ) -> None:
        self.entity_id = entity_id
        self.entity_root = entity_root
        self.workspace_id = workspace_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.user_id = user_id
        self.tool_name = tool_name
        self._bind_cache: dict[str, str | None] = {}
        self._sync_cache: dict[str, ArtifactDocumentResolution] = {}

    async def resolve(
        self,
        identity: ArtifactReferenceIdentity,
        rel_path: str | None,
    ) -> ArtifactDocumentResolution:
        if identity.document_id and self.workspace_id:
            bound_document_id = await self._bind(identity.document_id)
            if bound_document_id:
                return ArtifactDocumentResolution(document_id=bound_document_id)

        if identity.document_id and (not self.workspace_id or not rel_path):
            return ArtifactDocumentResolution(document_id=identity.document_id)

        if rel_path:
            return await self._sync(rel_path)
        return ArtifactDocumentResolution()

    async def _bind(self, document_id: str) -> str | None:
        if document_id not in self._bind_cache:
            self._bind_cache[document_id] = await bind_document_to_workspace(
                entity_id=self.entity_id,
                document_id=document_id,
                workspace_id=self.workspace_id,
                task_id=self.task_id,
                agent_id=self.agent_id,
                user_id=self.user_id,
                tool_name=self.tool_name,
            )
        return self._bind_cache[document_id]

    async def _sync(self, rel_path: str) -> ArtifactDocumentResolution:
        if rel_path in self._sync_cache:
            return self._sync_cache[rel_path]

        abs_path = os.path.realpath(os.path.join(self.entity_root, rel_path))
        if not os.path.isfile(abs_path):
            resolution = ArtifactDocumentResolution(failure_reason="not_file")
        else:
            sync = await sync_file_to_knowledge(
                entity_id=self.entity_id,
                abs_path=abs_path,
                entity_root=self.entity_root,
                source="agent",
                created_by=self.user_id or self.agent_id or "ai-agent",
                force=True,
                workspace_id=self.workspace_id,
                task_id=self.task_id,
                agent_id=self.agent_id,
                user_id=self.user_id,
                tool_name=self.tool_name,
            )
            document_id = str(sync.document_id or "").strip()
            resolution = ArtifactDocumentResolution(
                document_id=document_id,
                failure_reason=None if sync.synced and document_id else (
                    sync.reason or "missing_document_id"
                ),
            )
        self._sync_cache[rel_path] = resolution
        return resolution


async def project_artifact_refs_to_knowledge(
    *,
    entity_id: str,
    refs: list[dict[str, Any]],
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    tool_name: str = "plan_artifact_finalize",
) -> ArtifactKnowledgeProjection:
    """Return canonical refs and fail any claimed local file without a Document."""

    entity_root = get_entity_root(entity_id)
    reference_factory = ArtifactReferenceFactory(entity_id=entity_id)
    document_resolver = ArtifactDocumentResolver(
        entity_id=entity_id,
        entity_root=entity_root,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        user_id=user_id,
        tool_name=tool_name,
    )
    projected: list[dict[str, Any]] = []
    knowledge_artifacts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_documents: set[str] = set()

    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            continue
        ref = dict(raw_ref)
        identity = reference_factory.inspect(ref)
        rel_path = _ref_local_path(
            ref,
            entity_id=entity_id,
            entity_root=entity_root,
        )
        resolution = await document_resolver.resolve(identity, rel_path)
        document_id = resolution.document_id
        if resolution.failure_reason and rel_path:
            failures.append({"fs_path": rel_path, "reason": resolution.failure_reason})

        if document_id:
            ref["document_id"] = document_id
            if rel_path:
                ref["fs_path"] = rel_path
            name = _artifact_name(ref, rel_path, document_id)
            ref.setdefault("name", name)
        canonical_ref = reference_factory.create(ref)
        if document_id:
            if document_id not in seen_documents:
                seen_documents.add(document_id)
                viewer_url = canonical_ref["viewer_url"]
                knowledge_artifacts.append({
                    "type": str(ref.get("type") or "file"),
                    "name": name,
                    "document_id": document_id,
                    "viewer_url": viewer_url,
                    "markdown_link": canonical_file_markdown_link(name, viewer_url),
                    **({"fs_path": rel_path} if rel_path else {}),
                })
        projected.append(canonical_ref)

    return ArtifactKnowledgeProjection(
        refs=projected,
        knowledge_artifacts=knowledge_artifacts,
        failures=failures,
    )


def attach_knowledge_artifacts(
    result: dict[str, Any],
    projection: ArtifactKnowledgeProjection,
) -> dict[str, Any]:
    """Add canonical document handles without discarding producer payload fields."""

    if not projection.knowledge_artifacts:
        return result
    enriched = dict(result)
    existing = enriched.get("knowledge_artifacts")
    values = [dict(item) for item in existing or [] if isinstance(item, dict)]
    seen = {str(item.get("document_id") or "") for item in values}
    for item in projection.knowledge_artifacts:
        document_id = str(item.get("document_id") or "")
        if document_id and document_id not in seen:
            seen.add(document_id)
            values.append(dict(item))
    enriched["knowledge_artifacts"] = values
    if len(values) == 1:
        enriched.setdefault("document_id", values[0]["document_id"])
        enriched.setdefault("viewer_url", values[0]["viewer_url"])
        if values[0].get("markdown_link"):
            enriched.setdefault("markdown_link", values[0]["markdown_link"])
    return enriched


__all__ = [
    "ArtifactKnowledgeProjection",
    "attach_knowledge_artifacts",
    "project_artifact_refs_to_knowledge",
]
