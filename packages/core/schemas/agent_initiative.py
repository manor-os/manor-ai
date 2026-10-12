"""Public API schemas for Agent Initiative management and history."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.core.contracts.agent_initiative import (
    ConditionSpec,
    DecisionPolicy,
    DeliveryPolicy,
    InitiativeDecision,
    InterruptionPolicy,
    TriggerSpec,
)


class _StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentInitiativeCreateRequest(_StrictSchema):
    name: str = Field(min_length=1, max_length=255)
    instruction: str = Field(min_length=1, max_length=20_000)
    trigger: TriggerSpec
    condition: ConditionSpec = Field(default_factory=ConditionSpec)
    decision_policy: DecisionPolicy = Field(default_factory=DecisionPolicy)
    delivery_policy: DeliveryPolicy
    interruption_policy: InterruptionPolicy = Field(default_factory=InterruptionPolicy)
    status: Literal["active", "paused"] = "active"


class AgentInitiativePatch(_StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    instruction: str | None = Field(default=None, min_length=1, max_length=20_000)
    trigger: TriggerSpec | None = None
    condition: ConditionSpec | None = None
    decision_policy: DecisionPolicy | None = None
    delivery_policy: DeliveryPolicy | None = None
    interruption_policy: InterruptionPolicy | None = None

    @model_validator(mode="after")
    def nonempty(self) -> "AgentInitiativePatch":
        if not self.model_fields_set:
            raise ValueError("patch must include at least one field")
        null_fields = [
            field_name
            for field_name in self.model_fields_set
            if getattr(self, field_name) is None
        ]
        if null_fields:
            raise ValueError(
                "patch fields cannot be null: " + ", ".join(sorted(null_fields))
            )
        return self


class AgentInitiativeUpdateRequest(_StrictSchema):
    expected_revision: int = Field(ge=1)
    patch: AgentInitiativePatch


class AgentInitiativeActionRequest(_StrictSchema):
    expected_revision: int = Field(ge=1)


class AgentInitiativeDeleteRequest(_StrictSchema):
    expected_revision: int = Field(ge=1)


class AgentInitiativeRunRequest(_StrictSchema):
    idempotency_key: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    confirm_cost: bool = False


class AgentInitiativeRunQueuedResponse(BaseModel):
    initiative_id: str
    queued_at: datetime
    idempotency_key: str
    scheduled_run_id: str
    published: bool


class AgentInitiativeDeliveryResponse(BaseModel):
    id: str
    kind: str
    recipient_user_id: str | None = None
    status: str
    message_id: str | None = None
    notification_id: str | None = None
    suppressed_reason: str | None = None
    error: str | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None


class AgentInitiativeRunResponse(BaseModel):
    id: str
    initiative_id: str
    scheduled_run_id: str | None = None
    task_id: str | None = None
    occurrence_key: str
    trigger_type: str
    status: str
    candidate_decision: InitiativeDecision | None = None
    final_decision: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    message: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deliveries: list[AgentInitiativeDeliveryResponse] = Field(default_factory=list)


class AgentInitiativeResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: str | None = None
    agent_subscription_id: str
    owner_user_id: str
    name: str
    instruction: str
    trigger: TriggerSpec
    condition: ConditionSpec
    decision_policy: DecisionPolicy
    delivery_policy: DeliveryPolicy
    interruption_policy: InterruptionPolicy
    status: str
    revision: int
    next_run_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_notified_at: datetime | None = None
    last_silent_reason: str | None = None
    can_manage: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentInitiativeConversationResponse(BaseModel):
    id: str
    title: str | None = None
    workspace_id: str | None = None
    scope: str


class AgentInitiativeConversationListResponse(BaseModel):
    conversation: AgentInitiativeConversationResponse
    items: list[AgentInitiativeResponse]


class AgentInitiativeRunListResponse(BaseModel):
    items: list[AgentInitiativeRunResponse]
    next_cursor: str | None = None


__all__ = [
    "AgentInitiativeActionRequest",
    "AgentInitiativeConversationListResponse",
    "AgentInitiativeConversationResponse",
    "AgentInitiativeCreateRequest",
    "AgentInitiativeDeleteRequest",
    "AgentInitiativeDeliveryResponse",
    "AgentInitiativePatch",
    "AgentInitiativeResponse",
    "AgentInitiativeRunListResponse",
    "AgentInitiativeRunRequest",
    "AgentInitiativeRunQueuedResponse",
    "AgentInitiativeRunResponse",
    "AgentInitiativeUpdateRequest",
]
