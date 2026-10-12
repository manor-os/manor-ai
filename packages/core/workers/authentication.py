"""Bound expensive worker authentication without holding a database slot."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import time

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database_runtime import release_read_transaction
from packages.core.models.worker import Worker
from packages.core.workers.registry import (
    _verify_secret_hash, _worker_secret_is_usable, get_worker,
)


class WorkerAuthenticationBusy(Exception):
    """CPU admission failed; callers should retry, not discard credentials."""


class WorkerSecretVerifier:
    def __init__(
        self,
        concurrency: int = 2,
        max_waiters: int = 8,
        timeout: float = 0.25,
        *,
        cache_ttl: float = 300.0,
        cache_size: int = 1024,
    ):
        self._slots = asyncio.Semaphore(concurrency)
        self._max_waiters = max_waiters
        self._timeout = timeout
        self.waiting = 0
        self._running: set[asyncio.Task[bool]] = set()
        # Workers heartbeat every few seconds with the same secret, and bcrypt
        # costs ~250ms of CPU each time. Remember successful verifications of
        # an exact (stored hash, presented secret) pair. Rotation changes the
        # stored hash (a miss), and callers still re-read revocation/expiry
        # from the database after every verification.
        self._cache_ttl = cache_ttl
        self._cache_size = cache_size
        self._verified: OrderedDict[tuple[str, bytes], float] = OrderedDict()

    def _cache_key(self, secret_hash: str, presented_secret: str) -> tuple[str, bytes]:
        return secret_hash, hashlib.sha256(presented_secret.encode()).digest()

    def _cached(self, key: tuple[str, bytes]) -> bool:
        expires_at = self._verified.get(key)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            del self._verified[key]
            return False
        self._verified.move_to_end(key)
        return True

    def _remember(self, key: tuple[str, bytes]) -> None:
        if self._cache_ttl <= 0 or self._cache_size <= 0:
            return
        self._verified[key] = time.monotonic() + self._cache_ttl
        self._verified.move_to_end(key)
        while len(self._verified) > self._cache_size:
            self._verified.popitem(last=False)

    async def verify(self, secret_hash: str, presented_secret: str) -> bool:
        key = self._cache_key(secret_hash, presented_secret)
        if self._cached(key):
            return True
        verified = await self._verify_uncached(secret_hash, presented_secret)
        if verified:
            self._remember(key)
        return verified

    async def _verify_uncached(self, secret_hash: str, presented_secret: str) -> bool:
        if not self._slots.locked():
            # Semaphore.acquire does not yield when a slot is available. Reserve
            # it before accepting another request from the same arrival burst.
            await self._slots.acquire()
        else:
            if self.waiting >= self._max_waiters:
                raise WorkerAuthenticationBusy("Worker authentication queue is full")
            self.waiting += 1
            try:
                try:
                    await asyncio.wait_for(self._slots.acquire(), self._timeout)
                except TimeoutError as exc:
                    raise WorkerAuthenticationBusy("Worker authentication queue timed out") from exc
            finally:
                self.waiting -= 1

        # Only immutable credential values cross the thread boundary. Cancellation
        # of an HTTP/WS request must not free a slot while bcrypt is still running.
        verification = asyncio.create_task(asyncio.to_thread(
            _verify_secret_hash, secret_hash, presented_secret,
        ))
        # Keep shielded tasks alive after the request owning them disconnects.
        self._running.add(verification)

        def finished(task):
            self._running.discard(task)
            self._slots.release()
            if not task.cancelled():
                task.exception()  # Drain failures even if the caller disconnected.

        verification.add_done_callback(finished)
        return await asyncio.shield(verification)


def _verifier() -> WorkerSecretVerifier:
    # Every process/event loop owns its CPU budget, including test/CLI loops.
    loop = asyncio.get_running_loop()
    verifier = getattr(loop, "_manor_worker_secret_verifier", None)
    if verifier is None:
        verifier = WorkerSecretVerifier()
        loop._manor_worker_secret_verifier = verifier
    return verifier


async def authenticate_worker_secret(
    db: AsyncSession, worker_id: str, presented_secret: str,
) -> Worker | None:
    """Authenticate off-loop, then re-read current credentials and policy.

    Internal workers have no secret. Revoked and expired credentials fail;
    paused/quarantined workers may authenticate to receive control instructions.
    """
    worker = await get_worker(db, worker_id)
    secret_hash = worker.secret_hash if worker and _worker_secret_is_usable(worker) else None
    entity_id = worker.entity_id if worker else None
    await release_read_transaction(db)
    if not secret_hash or not await _verifier().verify(secret_hash, presented_secret):
        return None

    # A rotation, revocation, expiry or policy update may have happened while
    # bcrypt ran. Bypass the ORM identity map and return current authorization.
    worker = await get_worker(db, worker_id, fresh=True)
    valid = (
        worker is not None
        and worker.entity_id == entity_id
        and worker.secret_hash == secret_hash
        and _worker_secret_is_usable(worker)
    )
    await release_read_transaction(db)
    return worker if valid else None
