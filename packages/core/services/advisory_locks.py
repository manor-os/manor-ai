"""Small factory for dedicated PostgreSQL transaction advisory locks."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)


@dataclass
class TransactionAdvisoryLock:
    """A transaction lock lease held by one dedicated database connection."""

    connection: AsyncConnection | None
    namespace: int
    key: str

    @classmethod
    async def try_acquire(
        cls,
        db: Any,
        *,
        namespace: int,
        key: str,
    ) -> TransactionAdvisoryLock | None:
        engine = getattr(db, "bind", None)
        dialect = getattr(engine, "dialect", None)
        if engine is None or getattr(dialect, "name", None) != "postgresql":
            # PostgreSQL is the production contract. The no-op lease keeps
            # isolated unit tests and non-Postgres tooling usable.
            return cls(connection=None, namespace=namespace, key=key)

        # The dedicated connection matters because callers may commit while
        # provider/agent work is running. The open lock transaction also pins
        # one PostgreSQL backend when PgBouncer uses transaction pooling.
        connection = await engine.connect()
        try:
            acquired = bool(await connection.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    ":namespace, hashtext(:lock_key))"
                ),
                {"namespace": namespace, "lock_key": key},
            ))
        except BaseException:
            await connection.close()
            raise
        if not acquired:
            await connection.rollback()
            await connection.close()
            return None
        return cls(connection=connection, namespace=namespace, key=key)

    async def release(self) -> None:
        if self.connection is None:
            return
        try:
            # The transaction owns no writes; commit releases its xact lock.
            await self.connection.commit()
        except Exception:
            logger.exception(
                "Failed to release advisory lock namespace=%s key=%s",
                self.namespace,
                self.key,
            )
            await self.connection.invalidate()
        finally:
            await self.connection.close()
