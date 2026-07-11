"""ManorWorker — the high-level user surface.

User code:

    worker = ManorWorker(endpoint="https://manor.example.com",
                          worker_id="wkr_x", secret="wks_y",
                          max_concurrent_leases=4)

    @worker.handle(kind="action", provider="shopify")
    async def handle_shopify(lease, ctx) -> dict:
        ...
        return {"result": {...}, "cost": {...}}

    await worker.run_forever()

What we own:
  * heartbeat loop (cadence comes from server response)
  * lease checkout (via heartbeat capacity)
  * concurrent lease execution (asyncio.gather, capped at max_concurrent_leases)
  * lease lifecycle reporting (complete / fail / need-human)
  * graceful shutdown (SIGINT / SIGTERM → drain in-flight then exit)
  * retry on transient transport errors (delegated to ManorClient)

What user code owns:
  * handler functions for each (kind, provider) tuple they support
  * crashes inside handlers are caught and reported via fail_lease
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from packages.worker_sdk.client import ManorClient, WorkerClientError
from packages.worker_sdk.types import (
    HeartbeatActiveLease,
    HeartbeatCapacity,
    HeartbeatCompletedLease,
    HeartbeatRequest,
    Lease,
    LeaseResult,
    NeedHumanInput,
)

logger = logging.getLogger(__name__)


class NoHandlerError(Exception):
    """Raised inside the worker loop when a lease arrives with no
    matching handler. Reported back via fail_lease(will_retry=False)
    so Manor can mark the step failed and the operator gets a clear
    chat message instead of a perpetual retry."""


# Handler signature: ``async def fn(lease, ctx) -> LeaseResult|dict|None``
LeaseHandler = Callable[["Lease", "LeaseContext"], Awaitable[Any]]


class LeaseContext:
    """Per-lease context handed to handlers.

    Currently exposes ``client`` (for ``extend_lease`` mid-run) and
    ``progress(0..1)`` for chunky steps to report progress without
    waiting for the next heartbeat."""

    def __init__(self, *, client: ManorClient, lease: Lease):
        self._client = client
        self._lease = lease

    @property
    def lease(self) -> Lease:
        return self._lease

    async def progress(self, fraction: float) -> None:
        """Push a progress update + lease extension. Use sparingly —
        excessive extends will appear as ``extended_count`` in audit."""
        try:
            await self._client.extend_lease(
                self._lease.lease_id, extra_seconds=300, progress=fraction,
            )
        except WorkerClientError as exc:
            logger.warning("progress update failed: %s", exc)


class ManorWorker:
    def __init__(
        self,
        *,
        endpoint: str,
        worker_id: str,
        secret: str,
        max_concurrent_leases: int = 1,
        capabilities: Optional[dict[str, Any]] = None,
        client: Optional[ManorClient] = None,
    ):
        self._client = client or ManorClient(
            endpoint=endpoint, worker_id=worker_id, secret=secret,
        )
        self._max_concurrent = max_concurrent_leases
        self._capabilities = dict(capabilities or {})
        self._handlers: dict[tuple[str, Optional[str]], LeaseHandler] = {}
        self._active: dict[str, asyncio.Task] = {}
        # Reported in next heartbeat then cleared.
        self._completions: list[HeartbeatCompletedLease] = []
        self._stop = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None

    # ── Handler registration ─────────────────────────────────────────

    def handle(
        self, *, kind: str, provider: Optional[str] = None,
    ) -> Callable[[LeaseHandler], LeaseHandler]:
        """Register a handler. Decorator form:

            @worker.handle(kind="action", provider="shopify")
            async def fn(lease, ctx): ...
        """
        def _wrap(fn: LeaseHandler) -> LeaseHandler:
            self._handlers[(kind, provider)] = fn
            return fn
        return _wrap

    def find_handler(self, lease: Lease) -> Optional[LeaseHandler]:
        """Most-specific match wins: (kind, provider) > (kind, None)."""
        return (
            self._handlers.get((lease.kind, lease.provider))
            or self._handlers.get((lease.kind, None))
        )

    def update_capabilities(self, capabilities: dict[str, Any] | None) -> None:
        """Update capability metadata reported on subsequent heartbeats."""
        self._capabilities = dict(capabilities or {})

    # ── Run loop ─────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        await self._client.start()
        self._install_signal_handlers()
        try:
            await self._loop()
        finally:
            await self._drain_then_close()

    async def _loop(self) -> None:
        next_heartbeat_in = 2
        while not self._stop.is_set():
            try:
                next_heartbeat_in = await self._tick()
            except WorkerClientError as exc:
                if exc.status_code in (401, 403):
                    logger.error("manor SDK: auth failed (%s) — exiting", exc.status_code)
                    self._stop.set()
                    break
                logger.warning("manor SDK: heartbeat failed (%s) — backing off", exc)
                next_heartbeat_in = max(next_heartbeat_in, 10)
            except Exception:
                logger.exception("manor SDK: unexpected loop failure — backing off")
                next_heartbeat_in = max(next_heartbeat_in, 30)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=next_heartbeat_in)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> int:
        """One heartbeat round-trip + dispatch. Returns the server's
        suggested next-heartbeat cadence."""
        # Snapshot active + completions.
        active = [
            HeartbeatActiveLease(lease_id=lid)
            for lid, task in self._active.items()
            if not task.done()
        ]
        completed_batch = self._completions
        self._completions = []

        capacity_n = max(0, self._max_concurrent - len(active))
        state = "shutting_down" if self._stop.is_set() else (
            "busy" if len(active) >= self._max_concurrent else "idle"
        )

        req = HeartbeatRequest(
            state=state,
            timestamp=datetime.now(timezone.utc),
            active_leases=active,
            completed_since_last=completed_batch,
            capacity=HeartbeatCapacity(can_accept_leases=capacity_n),
            capabilities=self._capabilities or None,
        )
        resp = await self._client.heartbeat(req)

        # Dispatch new leases concurrently.
        for lease in resp.new_leases:
            self._spawn_handler(lease)

        # Honour out-of-band instructions.
        for ins in resp.instructions:
            if ins.type == "shutdown":
                logger.info("manor SDK: server-initiated shutdown")
                self._stop.set()
            elif ins.type == "pause":
                logger.info("manor SDK: paused by server (%s)",
                            (ins.payload or {}).get("reason", "no reason"))
                # Server-side pause = next heartbeat will return 0 leases
                # and another `pause` instruction; we keep heartbeating
                # so the operator can resume cleanly.

        return max(1, int(resp.next_heartbeat_in_seconds))

    # ── Per-lease execution ──────────────────────────────────────────

    def _spawn_handler(self, lease: Lease) -> None:
        if lease.lease_id in self._active:
            logger.warning("manor SDK: lease %s already active — server replayed",
                           lease.lease_id)
            return
        task = asyncio.create_task(self._run_handler(lease), name=f"lease-{lease.lease_id}")
        self._active[lease.lease_id] = task

    async def _run_handler(self, lease: Lease) -> None:
        ctx = LeaseContext(client=self._client, lease=lease)
        handler = self.find_handler(lease)
        try:
            if handler is None:
                raise NoHandlerError(
                    f"no handler for kind={lease.kind!r} provider={lease.provider!r}"
                )

            raw = await handler(lease, ctx)
            result = self._coerce_result(raw)
            await self._client.complete_lease(lease.lease_id, result)
            self._completions.append(HeartbeatCompletedLease(
                lease_id=lease.lease_id, status="done",
                result=result.result, cost=result.cost,
                evidence_refs=result.evidence_refs,
            ))

        except NeedHumanInput as nh:
            try:
                await self._client.need_human(lease.lease_id, prompt=nh.prompt)
            except WorkerClientError as exc:
                logger.warning("need_human report failed: %s", exc)

        except NoHandlerError as exc:
            err = {
                "type": "NoHandler", "message": str(exc),
                "kind": lease.kind, "provider": lease.provider,
            }
            await self._client.fail_lease(lease.lease_id, error=err, will_retry=False)
            self._completions.append(HeartbeatCompletedLease(
                lease_id=lease.lease_id, status="failed", error=err,
            ))

        except Exception as exc:
            logger.exception("handler for lease %s raised", lease.lease_id)
            err = {"type": type(exc).__name__, "message": str(exc)}
            try:
                await self._client.fail_lease(lease.lease_id, error=err)
            except WorkerClientError as report_exc:
                logger.warning("fail_lease report failed: %s", report_exc)
            self._completions.append(HeartbeatCompletedLease(
                lease_id=lease.lease_id, status="failed", error=err,
            ))

        finally:
            self._active.pop(lease.lease_id, None)

    @staticmethod
    def _coerce_result(raw: Any) -> LeaseResult:
        """Handlers may return: a LeaseResult, a dict, or None.
        Normalise to LeaseResult."""
        if raw is None:
            return LeaseResult()
        if isinstance(raw, LeaseResult):
            return raw
        if isinstance(raw, dict):
            # If it looks like a LeaseResult shape (top-level result/cost/...)
            # pass through; otherwise wrap as result.
            keys = set(raw.keys())
            if keys & {"result", "cost", "evidence_refs"}:
                return LeaseResult(**{k: raw.get(k) for k in ("result", "cost", "evidence_refs")})
            return LeaseResult(result=raw)
        return LeaseResult(result={"value": raw})

    # ── Lifecycle ────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                # Windows / nested loop — let user Ctrl-C propagate.
                pass

    async def stop(self) -> None:
        """Programmatic shutdown — sets the stop flag, run_forever
        will drain in-flight leases and exit."""
        self._stop.set()

    async def _drain_then_close(self) -> None:
        if self._active:
            logger.info("manor SDK: draining %d in-flight lease(s)", len(self._active))
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        # One last heartbeat to flush completions cleanly.
        if self._completions:
            try:
                req = HeartbeatRequest(
                    state="shutting_down",
                    timestamp=datetime.now(timezone.utc),
                    completed_since_last=self._completions,
                    capacity=HeartbeatCapacity(can_accept_leases=0),
                    capabilities=self._capabilities or None,
                )
                await self._client.heartbeat(req)
                self._completions = []
            except Exception:
                logger.warning("manor SDK: final flush failed", exc_info=True)
        await self._client.close()
