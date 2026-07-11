"""Pydantic types mirroring the Manor v1 worker HTTP protocol.

These intentionally duplicate the schemas in ``apps/api/routers/workers.py``
rather than importing from there. Reason: the SDK must be installable
without the rest of Manor's codebase, and the duplication is small
(~100 lines) compared to the cost of cross-package coupling. Drift is
caught by the e2e smoke test which exercises both halves together.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Lease (server → worker) ──────────────────────────────────────────

class CredentialBundle(BaseModel):
    """Short-lived credential issued for one lease.

    The plaintext is in ``value``. Worker MUST treat as ephemeral —
    don't log, don't persist, don't reuse across leases. Manor revokes
    the underlying Vault sublease the moment the lease completes /
    fails / expires.
    """

    sublease_id: str
    integration_id: str
    provider: str
    type: Literal["oauth_token", "api_key", "basic_auth", "browser_session"]
    value: dict
    expires_at: datetime


class Lease(BaseModel):
    """One unit of work. Contains everything the worker needs to act
    without round-tripping back to Manor."""

    lease_id: str
    step_id: str
    plan_id: str
    workspace_id: Optional[str]
    subscription_id: Optional[str] = None
    service_key: Optional[str] = None
    agent: Optional[dict[str, Any]] = None
    bindings: dict[str, Any] = Field(default_factory=dict)

    kind: Literal["action", "llm", "subagent", "code"]
    provider: Optional[str]
    action_key: Optional[str]
    capability_id: Optional[str] = None
    integration_id: Optional[str]

    params: dict[str, Any] = Field(default_factory=dict)
    expected_input_schema: Optional[dict] = None
    expected_output_schema: Optional[dict] = None

    risk_level: Literal["low", "medium", "high"] = "low"
    lease_until: datetime
    budget_limit_usd: Optional[float] = None
    execution_mode: Literal["live", "dry_run", "sandbox"] = "live"

    credentials: list[CredentialBundle] = Field(default_factory=list)
    """Empty when the worker reported ``uses_manor_credentials=False``
    at registration; otherwise pre-leased on the worker's behalf."""

    model_config = {"extra": "allow"}


# ── Result (worker → server) ─────────────────────────────────────────

class LeaseResult(BaseModel):
    """What a handler returns for a successful lease."""

    result: Optional[dict] = None
    cost: Optional[dict] = None
    """``{llm_tokens_input, llm_tokens_output, api_calls, usd}`` —
    fields are optional; Manor accumulates whatever's present."""
    evidence_refs: Optional[list[str]] = None
    """Object-store keys (S3 / MinIO) for screenshots, raw responses,
    large blobs that don't belong inline in result."""


class NeedHumanInput(Exception):
    """Raise from a handler when the lease requires user attention
    (CAPTCHA, 2FA, ambiguous input, manual approval).

    The lease pauses; Manor surfaces ``prompt`` to the operator via
    the workspace_chat HITL flow. When the operator answers, Manor
    re-leases the step (next attempt) with the response in
    ``params.human_input_response``.
    """

    def __init__(self, prompt: str, *, kind: str = "ambiguous_input"):
        super().__init__(prompt)
        self.prompt = prompt
        self.kind = kind


# ── Heartbeat envelopes ──────────────────────────────────────────────

class HeartbeatActiveLease(BaseModel):
    lease_id: str
    progress: Optional[float] = None


class HeartbeatCompletedLease(BaseModel):
    lease_id: str
    status: Literal["done", "failed"]
    result: Optional[dict] = None
    error: Optional[dict] = None
    cost: Optional[dict] = None
    evidence_refs: Optional[list[str]] = None


class HeartbeatCapacity(BaseModel):
    can_accept_leases: int = 0
    filters: Optional[dict] = None


class HeartbeatRequest(BaseModel):
    state: Literal["idle", "busy", "shutting_down"] = "idle"
    timestamp: Optional[datetime] = None
    budget_remaining_usd: Optional[float] = None
    active_leases: list[HeartbeatActiveLease] = Field(default_factory=list)
    completed_since_last: list[HeartbeatCompletedLease] = Field(default_factory=list)
    capacity: HeartbeatCapacity = Field(default_factory=HeartbeatCapacity)
    capabilities: Optional[dict] = None


class HeartbeatInstruction(BaseModel):
    type: str
    payload: Optional[dict] = None


class HeartbeatResponse(BaseModel):
    server_time: datetime
    next_heartbeat_in_seconds: int
    new_leases: list[Lease] = Field(default_factory=list)
    instructions: list[HeartbeatInstruction] = Field(default_factory=list)
