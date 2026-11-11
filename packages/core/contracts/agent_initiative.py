"""Strict, versioned contracts for proactive Agent Initiative configuration.

These models are deliberately separate from the ORM rows.  JSONB is only a
storage representation; every write and every runtime read must pass through
these contracts so an Agent cannot smuggle new recipients, channels, or
schedule behaviour into an untyped object.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.core.cron import validate_cron_expression


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TriggerSpec(_StrictContract):
    version: Literal["1"] = "1"
    kind: Literal["cron", "interval", "at"]
    cron: str | None = Field(default=None, max_length=100)
    every_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    run_at: datetime | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=50)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "TriggerSpec":
        if self.kind == "cron":
            if not self.cron:
                raise ValueError("cron is required for a cron trigger")
            self.cron = validate_cron_expression(self.cron)
            if self.every_seconds is not None or self.run_at is not None:
                raise ValueError("cron triggers cannot include interval or run_at")
        elif self.kind == "interval":
            if self.every_seconds is None:
                raise ValueError("every_seconds is required for an interval trigger")
            if self.cron is not None or self.run_at is not None:
                raise ValueError("interval triggers cannot include cron or run_at")
        else:
            if self.run_at is None:
                raise ValueError("run_at is required for an at trigger")
            if self.cron is not None or self.every_seconds is not None:
                raise ValueError("at triggers cannot include cron or interval")
        return self


class ConditionSpec(_StrictContract):
    version: Literal["1"] = "1"
    semantic_condition: str | None = Field(default=None, max_length=4_000)


class DecisionPolicy(_StrictContract):
    version: Literal["1"] = "1"
    mode: Literal["always", "on_condition", "on_change"] = "on_condition"
    cooldown_seconds: int = Field(default=0, ge=0, le=31_536_000)
    max_notifications_per_day: int = Field(default=24, ge=1, le=1_000)


class DeliverySurface(_StrictContract):
    kind: Literal["conversation", "workspace_chat"]
    conversation_id: str = Field(min_length=1, max_length=100)


class NotificationPolicy(_StrictContract):
    enabled: bool = Field(
        default=False,
        description=(
            "Optional secondary fan-out. The Initiative always writes to its "
            "bound Chat surface independently of this setting."
        ),
    )
    recipient: Literal["owner"] = "owner"
    # Only channels notification routing can deliver to (SUPPORTED_CHANNELS).
    channels: list[
        Literal["inapp", "email", "telegram", "wechat", "slack", "discord"]
    ] = Field(default_factory=lambda: ["inapp"], min_length=1, max_length=6)
    content_mode: Literal["full", "summary_and_link"] = "summary_and_link"

    @field_validator("channels", mode="before")
    @classmethod
    def drop_undeliverable_channels(cls, value):
        # Earlier rows could name "whatsapp", which routing never delivered
        # to (every attempt failed in the outbox). Keep them loadable.
        if isinstance(value, list) and "whatsapp" in value:
            value = [channel for channel in value if channel != "whatsapp"] or ["inapp"]
        return value

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("notification channels must be unique")
        return value


class DeliveryPolicy(_StrictContract):
    version: Literal["1"] = "1"
    surface: DeliverySurface
    notification: NotificationPolicy = Field(default_factory=NotificationPolicy)


class QuietHours(_StrictContract):
    mode: Literal["inherit_user", "custom", "none"] = "inherit_user"
    start: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def validate_custom(self) -> "QuietHours":
        if self.mode == "custom":
            if not self.start or not self.end or not self.timezone:
                raise ValueError("custom quiet hours require start, end, and timezone")
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("unknown quiet-hours timezone") from exc
        elif self.start is not None or self.end is not None or self.timezone is not None:
            raise ValueError("only custom quiet hours accept start, end, or timezone")
        return self


class InterruptionPolicy(_StrictContract):
    version: Literal["1"] = "1"
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    during_quiet_hours: Literal["suppress_external"] = "suppress_external"


class EvidenceRef(_StrictContract):
    type: str = Field(min_length=1, max_length=64)
    id: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2_000)
    label: str | None = Field(default=None, max_length=500)


class InitiativeDecision(_StrictContract):
    """Only model output accepted by the Initiative settlement boundary."""

    version: Literal["1"] = "1"
    decision: Literal["notify", "silent"]
    message: str | None = Field(default=None, max_length=20_000)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    reason: str | None = Field(default=None, max_length=2_000)
    severity: Literal["info", "warn"] = "info"
    dedupe_key: str | None = Field(default=None, max_length=255)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_decision(self) -> "InitiativeDecision":
        if self.decision == "notify" and not (self.message or "").strip():
            raise ValueError("notify decisions require a message")
        if self.decision == "silent" and self.message not in (None, ""):
            raise ValueError("silent decisions cannot include a message")
        return self


#: The JSON Schema an Initiative Task declares for its run's structured result
#: (``details["result_schema"]``). It states every rule ``InitiativeDecision``
#: enforces, so a result the run's host accepts also settles.
INITIATIVE_DECISION_RESULT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "decision", "reason_code"],
    "properties": {
        "version": {"type": "string", "enum": ["1"]},
        "decision": {"type": "string", "enum": ["notify", "silent"]},
        "message": {
            "type": "string", "maxLength": 20_000,
            "description": "What the person is told. Required to notify; omitted when silent.",
        },
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[a-z0-9_]+$"},
        "reason": {"type": "string", "maxLength": 2_000},
        "severity": {"type": "string", "enum": ["info", "warn"]},
        "dedupe_key": {"type": "string", "maxLength": 255},
        "evidence_refs": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "minLength": 1, "maxLength": 64},
                    "id": {"type": "string", "maxLength": 500},
                    "url": {"type": "string", "maxLength": 2_000},
                    "label": {"type": "string", "maxLength": 500},
                },
            },
        },
    },
    "allOf": [
        {
            "if": {"properties": {"decision": {"const": "notify"}}},
            "then": {"required": ["message"], "properties": {"message": {"pattern": "\\S"}}},
        },
        {
            "if": {"properties": {"decision": {"const": "silent"}}},
            "then": {"properties": {"message": {"maxLength": 0}}},
        },
    ],
}


__all__ = [
    "INITIATIVE_DECISION_RESULT_SCHEMA",
    "ConditionSpec",
    "DecisionPolicy",
    "DeliveryPolicy",
    "DeliverySurface",
    "EvidenceRef",
    "InitiativeDecision",
    "InterruptionPolicy",
    "NotificationPolicy",
    "QuietHours",
    "TriggerSpec",
]
