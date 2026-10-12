"""Create new Blueprint starter files without overwriting operator content.

The caller owns the database transaction. Exact inode receipts undo new files
on an ordinary rollback (including a savepoint or session close). An ambiguous
commit acknowledgement retains bytes for reconciliation instead of risking a
committed Document losing its file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.entity_fs import (
    EntityFileRollbackSnapshot,
    EntityFileVersion,
    EntityFilesystemError,
    stage_entity_file_rollback_snapshot,
    write_entity_file_atomic,
)

logger = logging.getLogger(__name__)
_STATE_KEY = "blueprint_starter_files"


@dataclass
class _StarterFile:
    snapshot: EntityFileRollbackSnapshot
    version: EntityFileVersion | None = None

    def undo(self) -> None:
        try:
            self.snapshot.rollback(self.version)
        except EntityFilesystemError:
            # A later writer owns a different inode/version. Never delete it.
            logger.warning("Blueprint starter rollback retained changed file %s", self.snapshot.rel_path, exc_info=True)


def _before_commit(session) -> None:
    state = session.info.get(_STATE_KEY)
    if state:
        owner = session.get_nested_transaction() or session.get_transaction()
        state["committing"].add(owner)


def _after_commit(session) -> None:
    state = session.info.get(_STATE_KEY)
    if not state:
        return
    owner = session.get_nested_transaction() or session.get_transaction()
    files = state["files"].pop(owner, [])
    state["committing"].discard(owner)
    if owner is not None and owner.nested:
        state["files"].setdefault(owner.parent, []).extend(files)
    else:
        for file in files:
            file.snapshot.commit()


def _after_transaction_end(session, transaction) -> None:
    state = session.info.get(_STATE_KEY)
    if not state:
        return
    files = state["files"].pop(transaction, [])
    uncertain_commit = transaction in state["committing"]
    state["committing"].discard(transaction)
    if uncertain_commit and files:
        logger.error("Blueprint starter files retained after an unconfirmed database commit")
        return
    for file in reversed(files):
        file.undo()


def create_starter_file(db: AsyncSession, *, entity_id: str, path: str, body: str) -> None:
    """Write only a new path and attach its cleanup to the owning transaction."""
    session = db.sync_session
    owner = session.get_nested_transaction() or session.get_transaction()
    if owner is None:
        raise ValueError("Blueprint starter files require an active transaction")
    if _STATE_KEY not in session.info:
        session.info[_STATE_KEY] = {"files": {}, "committing": set()}
        event.listen(session, "before_commit", _before_commit)
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_transaction_end", _after_transaction_end)

    snapshot = stage_entity_file_rollback_snapshot(entity_id, path)
    if snapshot.existed:
        snapshot.discard_unwritten()
        raise ValueError("Blueprint starter path is already occupied; existing content was preserved")
    file = _StarterFile(snapshot)

    def require_new(_path: str, exists: bool, _full_path: str) -> None:
        if exists:
            raise ValueError("Blueprint starter path became occupied; existing content was preserved")

    def receipt(version: EntityFileVersion | None) -> None:
        file.version = version

    try:
        write_entity_file_atomic(
            entity_id, path, body.encode("utf-8"),
            write_precondition=require_new, installed_receipt=receipt,
        )
    except BaseException:
        if file.version is not None:
            file.undo()
        else:
            snapshot.discard_unwritten()
        raise
    session.info[_STATE_KEY]["files"].setdefault(owner, []).append(file)
