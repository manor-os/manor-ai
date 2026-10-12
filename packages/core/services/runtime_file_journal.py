"""Write-ahead file recovery; never replay a tool, authorize a write, or bill.

Callers hold the entity mutation lock. Prepared rows commit before installing
bytes; the committed marker commits with the Document. A missing exact inode
receipt is deliberately not repaired by guessing from a filename or hash.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.services.entity_fs import (
    EntityFileRollbackSnapshot,
    EntityFileVersion,
    canonical_entity_root,
)


class FileCommitState(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECONCILIATION_REQUIRED = "reconciliation_required"


def _snapshot_record(snapshot: EntityFileRollbackSnapshot) -> dict:
    from packages.core.services.entity_fs import open_entity_file_snapshot

    result = asdict(snapshot)
    result.pop("state")
    # Absolute mount locations are process-local, never durable identities.
    result.pop("resolved_path")
    root = canonical_entity_root(snapshot.entity_id)
    result["cleanup_parent_paths"] = [os.path.relpath(path, root) for path in snapshot.cleanup_parent_paths]
    if snapshot.existed:
        with open_entity_file_snapshot(snapshot.entity_id, snapshot.rel_path) as source:
            if not snapshot.source_version or not snapshot.source_version.matches(source.stat):
                raise ValueError("File changed before journal preparation")
            with open(source.descriptor_path, "rb") as handle:
                result["source_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def _snapshot(row: RuntimeFileCommit) -> EntityFileRollbackSnapshot:
    from packages.core.services.generated_file_refs import canonical_fs_path

    data = dict(row.snapshot)
    data.pop("source_sha256", None)
    if data.get("entity_id") != row.entity_id or data.get("rel_path") != row.rel_path or canonical_fs_path(row.rel_path) != row.rel_path:
        raise ValueError("File journal identity is inconsistent")
    root = canonical_entity_root(row.entity_id)
    backup = data.get("backup_name")
    if backup and (os.path.basename(backup) != backup or not backup.startswith(".")):
        raise ValueError("File journal backup is invalid")
    parents = data.get("cleanup_parent_paths") or []
    if any(canonical_fs_path(path) != path for path in parents):
        raise ValueError("File journal parent identity is invalid")
    data["cleanup_parent_paths"] = tuple(os.path.join(root, path) for path in parents)
    data["resolved_path"] = os.path.join(root, row.rel_path)
    if data.get("source_version"):
        data["source_version"] = EntityFileVersion(**data["source_version"])
    return EntityFileRollbackSnapshot(**data)


def _is_restored_source(row: RuntimeFileCommit, snapshot: EntityFileRollbackSnapshot, current) -> bool:
    """Prove an earlier rollback without treating matching bytes as ownership.

    Consuming a retained hard-link (including the atomic writer's own rollback)
    changes ctime. Require the original inode, size, mtime and pre-write digest
    instead; old rows without that digest remain conservative. This proof only
    permits settling/backup cleanup, never replacing a target.
    """
    source = snapshot.source_version
    digest = row.snapshot.get("source_sha256")
    if not source or not digest or not source.same_identity(current.stat):
        return False
    if source.size != current.stat.st_size or source.mtime_ns != current.stat.st_mtime_ns:
        return False
    with open(current.descriptor_path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == digest


class RuntimeFileJournal:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.transaction_id = generate_ulid()
        self.entries: dict[str, str] = {}

    async def prepare(self, snapshot: EntityFileRollbackSnapshot, digest: str) -> None:
        from packages.core.database import async_session
        from packages.core.ai.runtime.authorization_receipts import runtime_current_tool_authorization_receipt

        receipt = runtime_current_tool_authorization_receipt()
        owner = {name: getattr(receipt, name, None) for name in (
            "workspace_id", "task_id", "conversation_id", "tool_name", "tool_call_id",
        )} if receipt is not None else {}
        from packages.core.services.runtime_file_effect_recovery import runtime_file_effect_owner

        owner.update(runtime_file_effect_owner(self.entity_id, receipt))
        effect_id = (owner.get("file_effect") or {}).get("fields", {}).get("effect_id")
        row_id = generate_ulid()
        record = _snapshot_record(snapshot)
        # Retain the operation identity even if commit acknowledgement (or the
        # awaiting task) is lost. Never retry this INSERT under a fresh identity.
        self.entries[snapshot.rel_path] = row_id
        try:
            async with async_session() as db:
                db.add(RuntimeFileCommit(
                    id=row_id, entity_id=self.entity_id, transaction_id=self.transaction_id,
                    rel_path=snapshot.rel_path,
                    path_sha256=hashlib.sha256(snapshot.rel_path.encode("utf-8")).hexdigest(),
                    state=FileCommitState.PREPARED.value,
                    snapshot=record, content_sha256=digest, owner=owner, effect_id=effect_id,
                ))
                await db.commit()
        except Exception as exc:
            # Closing the first session resolves its transaction before this
            # independent, locked read establishes the exact prepare outcome.
            async with async_session() as db:
                row = (await db.execute(select(RuntimeFileCommit).where(
                    RuntimeFileCommit.id == row_id,
                ).with_for_update())).scalar_one_or_none()
                if row is not None:
                    if (row.entity_id == self.entity_id
                            and row.transaction_id == self.transaction_id
                            and row.rel_path == snapshot.rel_path
                            and row.snapshot == record and row.content_sha256 == digest
                            and row.owner == owner and row.effect_id == effect_id
                            and row.written_version is None
                            and row.state == FileCommitState.PREPARED.value):
                        return
                    raise RuntimeError("File prepare outcome has conflicting journal evidence") from exc
            self.entries.pop(snapshot.rel_path, None)
            if isinstance(exc, IntegrityError) and "uq_runtime_file_commit_unsettled_path" in str(exc):
                raise ValueError("This file has an unresolved earlier commit; reconcile that exact operation before writing it again.") from exc
            raise

    async def installed(self, path: str, version: EntityFileVersion) -> None:
        from packages.core.database import async_session

        async with async_session() as db:
            result = await db.execute(update(RuntimeFileCommit).where(
                RuntimeFileCommit.id == self.entries[path],
                RuntimeFileCommit.state == FileCommitState.PREPARED.value,
            ).values(written_version=asdict(version)))
            if result.rowcount != 1:
                raise RuntimeError("File commit journal lost its prepared identity")
            await db.commit()

    async def mark_committed(self, db, documents: dict[str, str]) -> None:
        """Use the SAME session/transaction that commits the Knowledge rows."""
        for path, row_id in self.entries.items():
            if path not in documents:
                raise RuntimeError("File commit is missing its Knowledge receipt")
            result = await db.execute(update(RuntimeFileCommit).where(
                RuntimeFileCommit.id == row_id,
                RuntimeFileCommit.state == FileCommitState.PREPARED.value,
                RuntimeFileCommit.written_version.is_not(None),
            ).values(state=FileCommitState.COMMITTED.value, document_id=documents[path]))
            if result.rowcount != 1:
                raise RuntimeError("File commit journal changed before Knowledge commit")

    async def committed(self) -> bool:
        """Resolve an ambiguous DB acknowledgement before touching any bytes."""
        from packages.core.database import async_session

        async with async_session() as db:
            rows = list((await db.execute(select(RuntimeFileCommit).where(
                RuntimeFileCommit.transaction_id == self.transaction_id,
            ).with_for_update())).scalars())
            if len(rows) != len(self.entries):
                raise RuntimeError("File commit outcome could not be established")
            states = {row.state for row in rows}
            if states == {FileCommitState.COMMITTED.value}:
                return True
            if states <= {FileCommitState.PREPARED.value}:
                return False
            raise RuntimeError("File commit journal has conflicting outcomes")

    async def settled(self, *, committed: bool, cleanup_pending: bool = False) -> None:
        if not self.entries:
            return
        from packages.core.database import async_session

        async with async_session() as db:
            await db.execute(update(RuntimeFileCommit).where(
                RuntimeFileCommit.transaction_id == self.transaction_id,
                RuntimeFileCommit.state == (FileCommitState.COMMITTED.value if committed else FileCommitState.PREPARED.value),
            ).values(state=FileCommitState.COMMITTED.value if committed else FileCommitState.ROLLED_BACK.value,
                     cleanup_pending=cleanup_pending))
            await db.commit()


async def recover_runtime_file_commits(entity_id: str, *, limit: int = 64) -> dict[str, int]:
    """Recover a bounded batch while the caller owns the entity mutation lock.

    Never install new bytes. An uncommitted exact installed version can be
    rolled back; a committed Document only needs backup cleanup. Unknown or
    externally changed targets retain their backup and their path fence.
    """
    from packages.core.database import async_session
    from packages.core.services.entity_fs import open_entity_file_snapshot

    report = {"recovered": 0, "unresolved": 0}
    async with async_session() as db:
        rows = list((await db.execute(select(RuntimeFileCommit).where(
            RuntimeFileCommit.entity_id == entity_id,
            RuntimeFileCommit.cleanup_pending.is_(True),
        ).order_by(RuntimeFileCommit.created_at).limit(limit).with_for_update(skip_locked=True))).scalars())
        for row in rows:
            try:
                snapshot = _snapshot(row)
                backup_exists = bool(snapshot.backup_rel_path and os.path.lexists(os.path.join(canonical_entity_root(entity_id), snapshot.backup_rel_path)))
                if backup_exists:
                    with open_entity_file_snapshot(entity_id, snapshot.backup_rel_path,
                                                   expected_content_sha256=row.snapshot.get("source_sha256")) as backup:
                        if not snapshot.source_version or not snapshot.source_version.same_identity(backup.stat):
                            raise ValueError("File recovery backup identity changed")
                if row.state == FileCommitState.COMMITTED.value:
                    snapshot.commit()
                elif row.state == FileCommitState.PREPARED.value:
                    if row.written_version is not None:
                        version = EntityFileVersion(**row.written_version)
                        if not snapshot.existed and not os.path.lexists(snapshot.resolved_path):
                            # A previous recovery already removed our CREATE.
                            # Do not touch newly-created parent directories.
                            pass
                        else:
                            with open_entity_file_snapshot(entity_id, row.rel_path) as current:
                                installed = version.matches(current.stat)
                                restored = not backup_exists and _is_restored_source(row, snapshot, current)
                                if not installed and not restored:
                                    raise ValueError("File recovery target has a different version")
                            if installed:
                                snapshot.rollback(version)
                    elif snapshot.existed:
                        with open_entity_file_snapshot(entity_id, row.rel_path) as current:
                            unchanged = snapshot.source_version and snapshot.source_version.matches(current.stat)
                            restored = _is_restored_source(row, snapshot, current)
                            if not unchanged and not restored:
                                raise ValueError("File installation outcome is not recorded")
                        if unchanged or backup_exists:
                            # The atomic writer may already have restored this
                            # inode, changing ctime while our own verified
                            # backup remains. Only discard that backup; do not
                            # install old bytes over the proven current source.
                            snapshot.discard_unwritten()
                    elif not os.path.lexists(snapshot.resolved_path):
                        snapshot.discard_unwritten()
                    else:
                        raise ValueError("File installation outcome is not recorded")
                    row.state = FileCommitState.ROLLED_BACK.value
                else:
                    continue
                row.cleanup_pending = False
                row.error = None
                report["recovered"] += 1
            except Exception as exc:
                if row.state == FileCommitState.PREPARED.value:
                    row.state = FileCommitState.RECONCILIATION_REQUIRED.value
                # Committed bytes and their Document are authoritative. A
                # transient backup cleanup failure must remain retryable.
                row.cleanup_pending = row.state == FileCommitState.COMMITTED.value
                row.error = str(exc)[:1000]
                report["unresolved"] += 1
        await db.commit()
    return report
