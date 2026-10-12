"""ManorWorker — the high-level user surface.

User code:

    from packages.worker_sdk import LeaseResult

    worker = ManorWorker(endpoint="https://manor.example.com",
                         worker_id="wkr_x", secret="wks_y",
                         max_concurrent_leases=4)

    @worker.handle(kind="action", provider="shopify")
    async def handle_shopify(lease, ctx) -> LeaseResult:
        ...
        return LeaseResult(result={...}, cost={...})

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
import random
from typing import Any, Awaitable, Callable, Optional

from packages.worker_sdk.client import ManorClient, WorkerClientError
from packages.worker_sdk.types import (
    HeartbeatActiveLease,
    HeartbeatCapacity,
    HeartbeatCompletedLease,
    HeartbeatRequest,
    Lease,
    LeaseResult,
    LeaseResultFactory,
    NeedHumanInput,
    TaskOutputValueKind,
)

logger = logging.getLogger(__name__)

_LEASE_ATTEMPT_TOKEN_CAPABILITY = "lease_attempt_token_version"
_LEASE_ATTEMPT_TOKEN_VERSION = 1


class NoHandlerError(Exception):
    """Raised inside the worker loop when a lease arrives with no
    matching handler. Reported back via fail_lease(will_retry=False)
    so Manor can mark the step failed and the operator gets a clear
    chat message instead of a perpetual retry."""


# Plain handler values are task payloads. Return ``LeaseResult`` only when the
# handler needs transport metadata such as cost or evidence references.
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
                self._lease.lease_id,
                extra_seconds=300,
                progress=fraction,
                attempt_token=self._lease.attempt_token,
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
        self._attempt_tokens: dict[str, str] = {}
        # Immutable terminal reports.  A batch is removed only after the
        # heartbeat carrying that exact snapshot is acknowledged.
        self._completions: list[HeartbeatCompletedLease] = []
        self._stop = asyncio.Event()
        self._heartbeat_task: Optional[asyncio.Task] = None

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

    def _reported_capabilities(self) -> dict[str, Any]:
        return {
            **self._capabilities,
            _LEASE_ATTEMPT_TOKEN_CAPABILITY: _LEASE_ATTEMPT_TOKEN_VERSION,
        }

    def _queue_completion(self, completion: HeartbeatCompletedLease) -> bool:
        """Freeze one terminal outcome until a heartbeat acknowledges it.

        A lease may be replayed while its direct completion request has an
        ambiguous transport outcome.  The first terminal report wins; a
        replay must not replace it or execute the application handler again.
        """
        existing = next(
            (
                pending
                for pending in self._completions
                if pending.lease_id == completion.lease_id
            ),
            None,
        )
        if existing is not None:
            if existing != completion:
                logger.error(
                    "manor SDK: conflicting terminal report ignored for lease %s",
                    completion.lease_id,
                )
            return False
        self._completions.append(completion.model_copy(deep=True))
        return True

    def _has_queued_completion(self, lease_id: str) -> bool:
        return any(
            completion.lease_id == lease_id
            for completion in self._completions
        )

    def _acknowledge_completions(
        self,
        completed_batch: list[HeartbeatCompletedLease],
    ) -> None:
        """Remove only reports present in the acknowledged request snapshot."""
        acknowledged = {id(completion) for completion in completed_batch}
        self._completions[:] = [
            completion
            for completion in self._completions
            if id(completion) not in acknowledged
        ]

    @staticmethod
    def _success_completion(
        lease: Lease,
        result: LeaseResult,
    ) -> HeartbeatCompletedLease:
        completion: dict[str, Any] = {
            "lease_id": lease.lease_id,
            "attempt_token": lease.attempt_token,
            "status": "done",
        }
        # Preserve field presence: ``LeaseResult()`` means the handler
        # omitted a result, while ``LeaseResult(result=None)`` is an
        # explicit JSON null that a nullable bare contract may accept.
        if "result" in result.model_fields_set:
            completion["result"] = result.result
        if "cost" in result.model_fields_set:
            completion["cost"] = result.cost
        if "evidence_refs" in result.model_fields_set:
            completion["evidence_refs"] = result.evidence_refs
        completion["task_output_value_kind"] = result.task_output_value_kind
        return HeartbeatCompletedLease(**completion)

    async def _report_success(self, lease: Lease, result: LeaseResult) -> None:
        try:
            await self._client.complete_lease(
                lease.lease_id,
                result,
                attempt_token=lease.attempt_token,
            )
        except Exception as exc:
            # The handler already succeeded.  Never turn an ambiguous
            # completion transport into a contradictory failure report; the
            # immutable heartbeat outbox retries the same terminal outcome.
            logger.warning(
                "complete_lease report failed for lease %s; queued for heartbeat retry: %s",
                lease.lease_id,
                exc,
            )

    async def _report_failure(
        self,
        lease: Lease,
        error: dict[str, Any],
        *,
        will_retry: Optional[bool] = None,
    ) -> None:
        try:
            await self._client.fail_lease(
                lease.lease_id,
                error=error,
                will_retry=will_retry,
                attempt_token=lease.attempt_token,
            )
        except Exception as exc:
            logger.warning(
                "fail_lease report failed for lease %s; queued for heartbeat retry: %s",
                lease.lease_id,
                exc,
            )

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
        last_heartbeat_in = next_heartbeat_in
        while not self._stop.is_set():
            try:
                next_heartbeat_in = await self._tick()
                last_heartbeat_in = next_heartbeat_in
            except asyncio.CancelledError:
                if self._stop.is_set():
                    break
                raise
            except WorkerClientError as exc:
                if exc.requires_operator_action:
                    if exc.status_code == 426:
                        logger.error(
                            "manor SDK: worker protocol registration rejected (426); "
                            "re-register this worker before restarting"
                        )
                    else:
                        logger.error(
                            "manor SDK: auth failed (%s) — exiting",
                            exc.status_code,
                        )
                    self._stop.set()
                    break
                logger.warning("manor SDK: heartbeat failed (%s) — backing off", exc)
                next_heartbeat_in = max(last_heartbeat_in, 10, exc.retry_after_seconds) + random.uniform(0, 2)
            except Exception:
                logger.exception("manor SDK: unexpected loop failure — backing off")
                next_heartbeat_in = max(last_heartbeat_in, 30) + random.uniform(0, 5)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=next_heartbeat_in)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> int:
        """One heartbeat round-trip + dispatch. Returns the server's
        suggested next-heartbeat cadence."""
        # Snapshot without removing.  Handlers may append while heartbeat I/O
        # is in flight; only this exact batch is removed after the response.
        completed_batch = list(self._completions)
        pending_completion_ids = {
            completion.lease_id for completion in self._completions
        }
        active = [
            HeartbeatActiveLease(
                lease_id=lid,
                attempt_token=self._attempt_tokens.get(lid),
            )
            for lid, task in self._active.items()
            if not task.done() and lid not in pending_completion_ids
        ]

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
            capabilities=self._reported_capabilities(),
        )
        if self._stop.is_set():
            raise asyncio.CancelledError
        # Shutdown cancels this task, including its current HTTP request or wait.
        self._heartbeat_task = asyncio.current_task()
        try:
            resp = await self._client.heartbeat(req)
        finally:
            self._heartbeat_task = None
        if self._stop.is_set():
            raise asyncio.CancelledError

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

        self._acknowledge_completions(completed_batch)

        return max(1, int(resp.next_heartbeat_in_seconds))

    # ── Per-lease execution ──────────────────────────────────────────

    def _spawn_handler(self, lease: Lease) -> None:
        if lease.lease_id in self._active:
            logger.warning("manor SDK: lease %s already active — server replayed",
                           lease.lease_id)
            return
        if self._has_queued_completion(lease.lease_id):
            logger.warning(
                "manor SDK: lease %s replayed while terminal report is pending",
                lease.lease_id,
            )
            return
        task = asyncio.create_task(self._run_handler(lease), name=f"lease-{lease.lease_id}")
        self._active[lease.lease_id] = task
        if lease.attempt_token:
            self._attempt_tokens[lease.lease_id] = lease.attempt_token

    async def _run_handler(self, lease: Lease) -> None:
        ctx = LeaseContext(client=self._client, lease=lease)
        handler = self.find_handler(lease)
        try:
            if handler is None:
                raise NoHandlerError(
                    f"no handler for kind={lease.kind!r} provider={lease.provider!r}"
                )

            raw = await handler(lease, ctx)
            result = LeaseResultFactory.from_handler_output(raw).model_copy(deep=True)

        except NeedHumanInput as nh:
            try:
                await self._client.need_human(
                    lease.lease_id,
                    prompt=nh.prompt,
                    attempt_token=lease.attempt_token,
                )
            except WorkerClientError as exc:
                logger.warning("need_human report failed: %s", exc)

        except NoHandlerError as exc:
            err = {
                "type": "NoHandler", "message": str(exc),
                "kind": lease.kind, "provider": lease.provider,
            }
            completion = HeartbeatCompletedLease(
                lease_id=lease.lease_id,
                attempt_token=lease.attempt_token,
                status="failed", error=err,
                task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
            )
            if self._queue_completion(completion):
                await self._report_failure(lease, err, will_retry=False)

        except Exception as exc:
            logger.exception("handler for lease %s raised", lease.lease_id)
            err = {"type": type(exc).__name__, "message": str(exc)}
            completion = HeartbeatCompletedLease(
                lease_id=lease.lease_id,
                attempt_token=lease.attempt_token,
                status="failed", error=err,
                task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
            )
            if self._queue_completion(completion):
                await self._report_failure(lease, err)

        else:
            completion = self._success_completion(lease, result)
            if self._queue_completion(completion):
                await self._report_success(lease, result)

        finally:
            self._active.pop(lease.lease_id, None)
            self._attempt_tokens.pop(lease.lease_id, None)

    # ── Lifecycle ────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                # Windows / nested loop — let user Ctrl-C propagate.
                pass

    def _request_stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()

    async def stop(self) -> None:
        """Programmatic shutdown — sets the stop flag, run_forever
        will drain in-flight leases and exit."""
        self._request_stop()

    async def _drain_then_close(self) -> None:
        if self._active:
            logger.info("manor SDK: draining %d in-flight lease(s)", len(self._active))
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        # One last heartbeat to flush completions cleanly.
        if self._completions:
            completed_batch = list(self._completions)
            try:
                req = HeartbeatRequest(
                    state="shutting_down",
                    timestamp=datetime.now(timezone.utc),
                    completed_since_last=completed_batch,
                    capacity=HeartbeatCapacity(can_accept_leases=0),
                    capabilities=self._reported_capabilities(),
                )
                await self._client.heartbeat(req)
                self._acknowledge_completions(completed_batch)
            except Exception:
                logger.warning("manor SDK: final flush failed", exc_info=True)
        await self._client.close()
