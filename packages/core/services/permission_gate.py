"""Execution gate composed from current identity and resource policy adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document, DocumentFolder
from packages.core.models.permission import Capability, Classification
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    ResolvedUserActor,
    resolve_current_user_actor,
)
from packages.core.services.document_access import (
    DocumentAccessContext,
    effective_document_capabilities_for_user,
    effective_document_folder_policy,
    unreadable_document_paths,
    user_can_read_document,
)
from packages.core.services.workspace_access import (
    user_can_read_workspace_id,
    user_readable_workspace_ids,
)


_BATCH_QUERY_LIMIT = 500


def _id_batches(values: list[str]):
    for offset in range(0, len(values), _BATCH_QUERY_LIMIT):
        yield values[offset:offset + _BATCH_QUERY_LIMIT]


@dataclass(frozen=True)
class AuthorizedDocumentRead:
    """A current decision scoped to the caller's active DB transaction."""

    actor: ResolvedUserActor


@dataclass(frozen=True)
class AuthorizedDocumentBatchRead:
    """Readable current Documents and their batch policy context."""

    actor: ResolvedUserActor
    documents: tuple[Document, ...]
    document_ids: frozenset[str]
    access_context: DocumentAccessContext


@dataclass(frozen=True)
class AuthorizedFolderRead:
    """A current Folder decision scoped to the caller's active transaction."""

    actor: ResolvedUserActor


@dataclass(frozen=True)
class AuthorizedFolderBatchRead:
    """Readable current Folders and their batch policy context."""

    actor: ResolvedUserActor
    folders: tuple[DocumentFolder, ...]
    folder_ids: frozenset[str]
    access_context: DocumentAccessContext


@dataclass(frozen=True)
class AuthorizedWorkspaceRead:
    """A current Workspace decision scoped to the caller's active transaction."""

    actor: ResolvedUserActor


@dataclass(frozen=True)
class AuthorizedWorkspaceBatchRead:
    """Readable ids from one current identity and Workspace-policy snapshot."""

    actor: ResolvedUserActor
    workspace_ids: frozenset[str]


@dataclass(frozen=True)
class AuthorizedFilesystemPathBatch:
    """Filesystem paths denied by the canonical Document/Folder policy."""

    actor: ResolvedUserActor
    unreadable_paths: frozenset[str]


class DocumentDownloadNotAllowed(PermissionError):
    """The actor may view the Document but cannot perform a download."""


class ResourcePermissionGate:
    """Compose current identity with each migrated resource policy authority."""

    @staticmethod
    async def authorize_document_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        document: Document,
        download: bool = False,
    ) -> AuthorizedDocumentRead | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        if not await user_can_read_document(
            db,
            document,
            entity_id=actor.entity_id,
            user_id=actor.user_id,
            role=actor.role,
            allow_redacted=False,
            _actor=actor,
        ):
            return None
        if download:
            classification, _, _ = await effective_document_folder_policy(db, document)
            capabilities = await effective_document_capabilities_for_user(
                db,
                document=document,
                user_id=actor.user_id,
                role=actor.role,
                _actor=actor,
            )
            if classification == Classification.RESTRICTED or Capability.DOWNLOAD not in capabilities:
                raise DocumentDownloadNotAllowed
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedDocumentRead(actor=actor)

    @staticmethod
    async def authorize_document_batch_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        documents: list[Document],
        workspace_id: str | None = None,
        allow_redacted: bool = True,
    ) -> AuthorizedDocumentBatchRead | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        requested_ids = list(
            dict.fromkeys(
                str(document.id)
                for document in documents
                if document.entity_id == actor.entity_id
            )
        )
        current_by_id = {}
        for id_batch in _id_batches(requested_ids):
            documents_in_batch = (
                await db.execute(
                    select(Document)
                    .where(
                        Document.entity_id == actor.entity_id,
                        Document.id.in_(id_batch),
                        Document.is_trashed.is_(False),
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            current_by_id.update({
                str(document.id): document for document in documents_in_batch
            })
        entity_documents = [
            current_by_id[document_id]
            for document_id in requested_ids
            if document_id in current_by_id
        ]
        access_context = await DocumentAccessContext.load(
            db,
            entity_id=actor.entity_id,
            user_id=actor.user_id,
            role=actor.role,
            folder_ids={
                str(document.folder_id)
                for document in entity_documents
                if document.folder_id
            },
            document_ids={str(document.id) for document in entity_documents},
            _actor=actor,
        )
        await access_context.preload_documents(db, entity_documents)
        readable_documents: list[Document] = []
        for document in entity_documents:
            if await access_context.can_read_document(
                db,
                document,
                workspace_id=workspace_id,
                allow_redacted=allow_redacted,
            ):
                readable_documents.append(document)
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedDocumentBatchRead(
            actor=actor,
            documents=tuple(readable_documents),
            document_ids=frozenset(
                str(document.id) for document in readable_documents
            ),
            access_context=access_context,
        )

    @staticmethod
    async def authorize_folder_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        folder: DocumentFolder,
        allow_redacted: bool = False,
    ) -> AuthorizedFolderRead | None:
        authorized = await ResourcePermissionGate.authorize_folder_batch_read(
            db,
            credential=credential,
            folders=[folder],
            allow_redacted=allow_redacted,
        )
        if authorized is None or str(folder.id) not in authorized.folder_ids:
            return None
        return AuthorizedFolderRead(actor=authorized.actor)

    @staticmethod
    async def authorize_folder_batch_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        folders: list[DocumentFolder],
        allow_redacted: bool = True,
    ) -> AuthorizedFolderBatchRead | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        requested_ids = list(
            dict.fromkeys(
                str(folder.id)
                for folder in folders
                if folder.entity_id == actor.entity_id
            )
        )
        current_by_id = {}
        for id_batch in _id_batches(requested_ids):
            folders_in_batch = (
                await db.execute(
                    select(DocumentFolder)
                    .where(
                        DocumentFolder.entity_id == actor.entity_id,
                        DocumentFolder.id.in_(id_batch),
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            current_by_id.update({
                str(folder.id): folder for folder in folders_in_batch
            })
        entity_folders = [
            current_by_id[folder_id]
            for folder_id in requested_ids
            if folder_id in current_by_id
        ]
        access_context = await DocumentAccessContext.load(
            db,
            entity_id=actor.entity_id,
            user_id=actor.user_id,
            role=actor.role,
            folder_ids={str(folder.id) for folder in entity_folders},
            _actor=actor,
        )
        readable_folders: list[DocumentFolder] = []
        for folder in entity_folders:
            workspace_ids = access_context.folder_workspace_ids(folder.id)
            workspace_allowed = True
            for workspace_id in workspace_ids:
                if not await access_context.workspace_readable(db, workspace_id):
                    workspace_allowed = False
                    break
            if not workspace_allowed:
                continue
            if await access_context.folder_path_readable(
                db,
                folder.id,
                allow_redacted=allow_redacted,
            ):
                readable_folders.append(folder)
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedFolderBatchRead(
            actor=actor,
            folders=tuple(readable_folders),
            folder_ids=frozenset(str(folder.id) for folder in readable_folders),
            access_context=access_context,
        )

    @staticmethod
    async def authorize_workspace_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        workspace_id: str,
    ) -> AuthorizedWorkspaceRead | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        if not await user_can_read_workspace_id(
            db,
            workspace_id=workspace_id,
            entity_id=actor.entity_id,
            user_id=actor.user_id,
            role=actor.role,
            can_read_entity_workspaces=actor.can_read_workspaces,
        ):
            return None
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedWorkspaceRead(actor=actor)

    @staticmethod
    async def authorize_workspace_batch_read(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        workspace_ids: set[str],
    ) -> AuthorizedWorkspaceBatchRead | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        readable_ids = await user_readable_workspace_ids(
            db,
            entity_id=actor.entity_id,
            user_id=actor.user_id,
            role=actor.role,
            workspace_ids=workspace_ids,
            can_read_entity_workspaces=actor.can_read_workspaces,
        )
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedWorkspaceBatchRead(
            actor=actor,
            workspace_ids=frozenset(readable_ids),
        )

    @staticmethod
    async def authorize_filesystem_path_batch(
        db: AsyncSession,
        *,
        credential: AuthenticatedUserCredential,
        rel_paths: list[str],
        directory_paths: list[str] | None = None,
        workspace_id: str | None = None,
    ) -> AuthorizedFilesystemPathBatch | None:
        actor = await resolve_current_user_actor(db, credential)
        if actor is None:
            return None
        blocked = await unreadable_document_paths(
            db,
            entity_id=actor.entity_id,
            rel_paths=rel_paths,
            directory_paths=directory_paths,
            user_id=actor.user_id,
            role=actor.role,
            workspace_id=workspace_id,
            _actor=actor,
        )
        if await resolve_current_user_actor(db, credential) != actor:
            return None
        return AuthorizedFilesystemPathBatch(
            actor=actor,
            unreadable_paths=frozenset(blocked),
        )
