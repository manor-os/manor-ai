"""HTTP client for the Manor worker protocol.

Thin wrapper over httpx — encapsulates auth headers, JSON serialisation,
basic retry on transport errors. Exposed to user code through
``ManorWorker``; direct use is for power users / tests.
"""
from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
from typing import Any, Optional

import httpx

from packages.worker_sdk.types import (
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseResult,
)

logger = logging.getLogger(__name__)


class WorkerClientError(Exception):
    """Raised on non-2xx HTTP responses or transport failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    @property
    def requires_operator_action(self) -> bool:
        """Whether retrying unchanged credentials/protocol cannot recover."""

        return self.status_code in {
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.UPGRADE_REQUIRED,
        }


class ManorClient:
    """Stateless-ish HTTP client. One per worker process."""

    PROTOCOL_HEADER = "Manor-Protocol-Version"
    WORKER_ID_HEADER = "Manor-Worker-Id"
    PROTOCOL_VERSION = "2"

    def __init__(
        self,
        endpoint: str,
        *,
        worker_id: Optional[str] = None,
        secret: Optional[str] = None,
        timeout_seconds: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._worker_id = worker_id
        self._secret = secret
        self._timeout = timeout_seconds
        self._transport = transport
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ManorClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._http is not None:
            return
        kw: dict[str, Any] = {
            "base_url": self._endpoint,
            "timeout": self._timeout,
            "headers": {"User-Agent": f"manor-worker-sdk/0.1 (proto/{self.PROTOCOL_VERSION})"},
        }
        if self._transport is not None:
            kw["transport"] = self._transport
        self._http = httpx.AsyncClient(**kw)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── Worker-auth headers ──────────────────────────────────────────

    def configure_credentials(self, *, worker_id: str, secret: str) -> None:
        """Set after a successful register() returns id+secret."""
        self._worker_id = worker_id
        self._secret = secret

    def _auth_headers(self) -> dict[str, str]:
        if not self._worker_id or not self._secret:
            raise WorkerClientError("worker_id + secret not configured")
        return {
            "Authorization": f"Bearer {self._secret}",
            self.WORKER_ID_HEADER: self._worker_id,
            self.PROTOCOL_HEADER: self.PROTOCOL_VERSION,
        }

    # ── Endpoints ────────────────────────────────────────────────────

    async def heartbeat(self, req: HeartbeatRequest) -> HeartbeatResponse:
        payload = req.model_dump(mode="json", exclude_none=True)
        # ``exclude_none`` recursively removes an explicitly supplied JSON
        # null from heartbeat completions. Restore it when the Pydantic field
        # was present so the server can distinguish null from omission.
        completed_payload = payload.get("completed_since_last")
        if isinstance(completed_payload, list):
            for index, completion in enumerate(req.completed_since_last):
                if "result" not in completion.model_fields_set:
                    continue
                if index >= len(completed_payload) or not isinstance(completed_payload[index], dict):
                    continue
                if completion.result is None:
                    completed_payload[index]["result"] = None
        body = await self._post("/api/v1/workers/heartbeat", json=payload)
        return HeartbeatResponse.model_validate(body)

    async def complete_lease(
        self, lease_id: str, result: LeaseResult,
    ) -> None:
        payload = result.model_dump(
            mode="json", exclude_unset=True, exclude_none=False,
        )
        # The discriminator is a protocol default, not an optional payload
        # field. ``exclude_unset`` is still required to preserve omitted
        # result versus explicit JSON null.
        payload["task_output_value_kind"] = result.task_output_value_kind.value
        await self._post(
            f"/api/v1/workers/leases/{lease_id}/complete",
            # ``exclude_unset`` preserves explicit ``result=None`` while still
            # omitting fields the handler never supplied (notably
            # ``LeaseResult()``'s omitted result).
            json=payload,
            expect_204=True,
        )

    async def fail_lease(
        self, lease_id: str, *, error: dict, will_retry: Optional[bool] = None,
    ) -> None:
        payload: dict[str, Any] = {"error": error}
        if will_retry is not None:
            payload["will_retry"] = will_retry
        await self._post(
            f"/api/v1/workers/leases/{lease_id}/fail",
            json=payload,
            expect_204=True,
        )

    async def need_human(self, lease_id: str, *, prompt: str) -> None:
        await self._post(
            f"/api/v1/workers/leases/{lease_id}/need-human",
            json={"prompt": prompt},
            expect_204=True,
        )

    async def extend_lease(
        self, lease_id: str, *, extra_seconds: float = 300, progress: Optional[float] = None,
    ) -> dict:
        payload: dict[str, Any] = {"extra_seconds": extra_seconds}
        if progress is not None:
            payload["progress"] = progress
        return await self._post(
            f"/api/v1/workers/leases/{lease_id}/extend",
            json=payload,
        )

    async def deregister(self) -> None:
        await self._post(
            "/api/v1/workers/me/deregister",
            json={},
            expect_204=True,
        )

    async def rotate_secret(self) -> str:
        body = await self._post("/api/v1/workers/me/rotate-secret", json={})
        new = body["worker_secret"]
        if self._worker_id:
            self.configure_credentials(worker_id=self._worker_id, secret=new)
        return new

    # ── Internals ────────────────────────────────────────────────────

    async def _post(
        self,
        path: str,
        *,
        json: dict,
        expect_204: bool = False,
        max_attempts: int = 3,
    ) -> Any:
        if self._http is None:
            await self.start()
        assert self._http is not None

        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                resp = await self._http.post(path, headers=self._auth_headers(), json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                wait = min(30.0, 0.5 * (2 ** attempt))
                logger.warning(
                    "manor SDK: transport error on %s (attempt %d): %s — backoff %.1fs",
                    path, attempt + 1, exc, wait,
                )
                await asyncio.sleep(wait)
                continue

            if expect_204:
                if resp.status_code == 204:
                    return None
                raise WorkerClientError(
                    f"POST {path}: expected 204, got {resp.status_code}",
                    status_code=resp.status_code,
                    body=_safe_body(resp),
                )

            if resp.status_code >= 400:
                response_error = WorkerClientError(
                    f"POST {path}: {resp.status_code}",
                    status_code=resp.status_code,
                    body=_safe_body(resp),
                )
                # Credentials and a rejected protocol registration cannot
                # repair themselves. Surface the first response immediately
                # so the worker loop can stop and ask the operator to act.
                if response_error.requires_operator_action:
                    raise response_error
                # Other 4xx / 5xx — back off + retry.
                last_exc = response_error
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(min(30.0, 0.5 * (2 ** attempt)))
                    continue
                raise last_exc

            try:
                return resp.json()
            except ValueError as exc:
                raise WorkerClientError(
                    f"POST {path}: invalid JSON response", body=resp.text,
                ) from exc

        # Exhausted all attempts.
        raise WorkerClientError(
            f"POST {path}: failed after {max_attempts} attempts",
        ) from last_exc


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]
